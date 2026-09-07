"""Cross-session message bus.

Ports the cctv-backend ``ops/intersession.sh`` pattern into a worker action
surface. Three primitives:

- ``send`` appends a message line to the recipient's inbox log; recipients
  can be a literal SID or a role keyword (``teamlead`` / ``dev`` / ``all``).
- ``inbox_read`` drains lines since the high-water mark.
- ``inbox_wait`` long-polls until the inbox file grows past the mark or a
  timeout elapses. Designed to be invoked from Claude Code with
  ``run_in_background: true`` so the harness's task-notification fires when
  this returns — that's the no-polling push primitive.

File layout under ``data/<slug>/_chat/``:
    inbox-<sid>.log    append-only, one message per line
    seen-<sid>         single int (byte offset), read high-water mark
    heartbeat-<sid>    mtime touched while wait/read is running
"""
from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any

from bot_squad_worker import frontmatter as _frontmatter

log = logging.getLogger(__name__)

_MAX_TEXT_LEN = 4000
# Recipient specs that are a ROLE fan-out rather than a literal SID. Single
# source of truth: ``_resolve_recipients`` dispatches on it, ``send`` uses it to
# tell a redirect from a fan-out, and the ``peer_send`` action uses it to decide
# whether a target needs cross-project SID resolution (T-0624). ``operator``
# joined in T-0790 — see ``_resolve_recipients``.
#: T-0943 reopen, gap 1: EVERY role in the model is addressable by role, not
#: just the four that happened to be here. `bsq peer send user-conversation` was
#: REFUSED -- "recipient SID has no registered session under any known project"
#: -- so the one role whose entire purpose is being a human's address could not
#: be addressed by role, which is how three hours of operator mail reached an
#: attendant. Derived from `sessions.DECLARABLE_ROLES` rather than re-listed,
#: because a hand-kept second copy of a role enum is exactly what went stale
#: here (T-0778 is the same lesson one layer down).
#:
#: `all` is not a role; it is the broadcast, and it stays.
def _model_role_keywords() -> frozenset:
    try:
        from bot_squad_worker.sessions import DECLARABLE_ROLES
    except Exception:  # noqa: BLE001 — never let this break the bus at import
        DECLARABLE_ROLES = ("user-conversation", "operator", "dev", "teamlead",
                            "prod-teamlead", "qa", "routine-handler")
    return frozenset(DECLARABLE_ROLES) | {"all"}


_ROLE_KEYWORDS = _model_role_keywords()

#: The two that resolve by TASK SHAPE rather than by held role, and keep doing
#: so. `dev` has meant "a session holding a ticket" since the bus existed and
#: ~12 internal callers rely on that; `teamlead` means "a coordinator with no
#: ticket". T-0943 UNIONS the role holders into both rather than replacing the
#: walk -- additive, so nobody who received before stops receiving, and a
#: session that DECLARED the role now receives too.
_TASK_SHAPED_KEYWORDS = frozenset({"teamlead", "dev", "all"})

#: T-0943: role keywords that mean "hand this UP to somebody". A send to one of
#: these that reaches NOBODY is refused rather than reported as sent — see the
#: refusal in :func:`send`.
#:
#: Two sets, because "is this an escalation" is not answerable from the keyword
#: alone. ``operator`` always is: all three of its internal callers are "needs a
#: human look" alerts, and the ONE thing an agent addresses to it is work it
#: cannot do itself. ``teamlead`` is a fan-out that is ALSO the dev contract's
#: first escalation hop, so it qualifies only when the sender declared the block
#: (``--blocked``, T-0977) — which is precisely the sender saying "I am STOPPED
#: until this is answered". Widening it unconditionally would break the T-0790
#: decision that an empty teamlead/dev/all fan-out stays quiet
#: (``test_empty_dev_fanout_is_not_warned``), which is a real prior ruling about
#: log noise, not an oversight.
#:
#: ``dev``/``all`` are never here: they are broadcasts, addressed to no one in
#: particular, and an empty one is an ordinary frequent state.
_ESCALATION_ROLES = frozenset({"operator"})
_BLOCKED_ESCALATION_ROLES = frozenset({"operator", "teamlead"})
# T-0091: cap raised 1800→7200 (2h). A long-blocking inbox_wait costs the
# worker nothing (a single condition-variable wait per SID), but every clean
# timeout fires a harness <task-notification> in the operator's pane and burns
# a prompt-cache re-arm cycle. 7200 lets an operator/TL/dev honoring a
# low-cadence idle preference ("re-arm every ~2h") actually get that cadence
# instead of churning every 30 minutes.
_MAX_WAIT_TIMEOUT = 7200
_HEARTBEAT_INTERVAL = 10.0

# T-0119: shutdown signal threaded in by __main__ so in-flight inbox_wait
# long-polls abort within one poll tick (≈1s) on SIGTERM instead of being
# SIGKILL'd 90s later by systemd. Set via set_shutdown_event() at startup;
# tests can pass their own Event into inbox_wait() directly.
_SHUTDOWN_EVENT: threading.Event | None = None


def set_shutdown_event(ev: threading.Event | None) -> None:
    """Install the process-wide shutdown event read by inbox_wait."""
    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = ev


def _chat_dir(cfg: Any, slug: str) -> Path:
    """Return data/<slug>/_chat, creating it 770 if missing."""
    d = Path(cfg.data_dir) / slug / "_chat"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(stat.S_IRWXU | stat.S_IRWXG)  # 770
        except OSError:
            pass
    return d


def _sanitize(text: str) -> str:
    """Flatten a message onto one line; REFUSE over-cap text, never truncate.

    T-0827: this used to end ``text = text[:_MAX_TEXT_LEN]`` — a bare slice
    with no error, no marker, no log line and no signal at either end. The
    sender was told ``sent to 1 inbox(es)``; the recipient got a message
    stopping mid-word and, BY CONSTRUCTION, could not know what it was
    supposed to receive — a truncated brief reads as a complete brief.
    Measured over both installs' inbox logs on 2026-07-30: 33 confirmed
    truncations (+2 ambiguous) out of 15,744 messages, overwhelmingly
    dev→operator, including two READY reports that were the certification
    basis for closed tickets, and one case where an operator accepted a
    ticket and pushed its commit on a report it could not see the end of.

    The remedy is CARRIED ACROSS, not invented: ``task_body`` hit the same cap
    constant, took the same silent loss (F-2026-07-05-bsq-30844bca41) and
    answered it by raising. This was duplicate divergence where the fix
    already existed in the tree — the peer bus simply never got it.

    REFUSE rather than split, deliberately (T-0827 DoD item 3). A split
    preserves content but this bus has no message framing: ``send`` appends one
    line per message and ``inbox_read`` returns them in FILE order, so a
    concurrent sender interleaves between parts and part 2/3 can arrive after
    somebody else's message — the same reordering ``rebind_sid`` already
    refuses a silent merge over. A refusal makes the SENDER act, which is what
    every session has been doing by hand since 14:33Z today, and it pushes
    evidence to the durable record: this bus is a NOTIFICATION channel that has
    been used as an EVIDENCE channel, and it has no durability guarantee
    appropriate to evidence.

    Raising is safe here because ``send`` — the ONLY caller — catches it and
    reports the refusal in its RETURN VALUE, preserving its documented "never
    raises" contract for the ~12 internal best-effort notifiers (T-0827 DoD
    item 4: the twin's raise is right for a CLI caller and wrong for a tick).
    """
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if len(text) > _MAX_TEXT_LEN:
        raise ValueError(
            f"peer message is {len(text)} chars, over the {_MAX_TEXT_LEN}-char cap — "
            "refusing to truncate (silent loss, T-0827; same refusal task_body.py "
            "made after F-2026-07-05-bsq-30844bca41). Split it into parts under "
            f"{_MAX_TEXT_LEN} chars, or record the long content on the ticket "
            "(`bsq ticket note`) and send a pointer."
        )
    return text


def _list_session_sids(cfg: Any, slug: str) -> list[tuple[str, dict]]:
    """Parse session md frontmatter for every session in data/<slug>/sessions/.

    Returns (sid, meta) tuples. T-0075: meta comes from the shared pyyaml
    parser (``~`` → None, lists typed), not the old line-based reader.
    """
    sess_dir = Path(cfg.data_dir) / slug / "sessions"
    if not sess_dir.exists():
        return []
    out: list[tuple[str, dict]] = []
    for md in sorted(sess_dir.glob("*.md")):
        parsed = _frontmatter.parse_or_none(md.read_text())
        if parsed is None:
            continue
        meta = parsed[0]
        sid = meta.get("sid") or md.stem
        out.append((str(sid), meta))
    return out


# T-0790: the window stem of an SID ``S-<user>-<window>-p<pane>``. A recycle
# mints a BRAND-NEW SID in the SAME window (operator p374 → p455), so the stem
# is what identifies "the session now holding this seat". Same heuristic
# ``scripts/cli/migrate_orphan_inboxes.py`` (T-0072) already uses to map an
# orphaned inbox to its successor — reused rather than re-derived.
_SID_STEM_RE = re.compile(r"^S-(?P<user>[^-]+)-(?P<stem>.+)-p\d+$")


def _sid_stem(sid: str) -> tuple[str, str] | None:
    """Return ``(linux_user, window_stem)`` parsed from ``sid``, or None."""
    m = _SID_STEM_RE.match(sid or "")
    return (m.group("user"), m.group("stem")) if m else None


def _linux_user_from_sid(sid: str) -> str:
    """T-0157: linux user segment of an SID ``S-<user>-<window>-p<pane>`` ("" if none)."""
    if sid and sid.startswith("S-"):
        parts = sid.split("-", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return ""


def _session_linux_user(sid: str, meta: dict) -> str:
    """T-0157: explicit ``linux_user`` field wins, else the SID prefix."""
    v = (meta or {}).get("linux_user")
    if v and v != "~":
        return str(v)
    return _linux_user_from_sid(sid)


def _session_status(cfg: Any, slug: str, sid: str) -> dict | None:
    """Frontmatter of ``data/<slug>/sessions/<sid>.md``, or None when absent."""
    md = Path(cfg.data_dir) / slug / "sessions" / f"{sid}.md"
    if not md.is_file():
        return None
    try:
        parsed = _frontmatter.parse_or_none(md.read_text())
    except OSError:
        return None
    return parsed[0] if parsed else None


# T-0896: WHY a successor lookup came back empty. ``live_successor_sid``
# answers only "who", which is all the peer bus ever needed — a redirect either
# happens or the caller keeps its existing behaviour. The TG inbound path has a
# harder job: when no successor exists it must tell a human WHAT went wrong
# (DoD 4 — "недоставка перестаёт быть тишиной"), and a lookup that returns a
# bare ``None`` forces that caller to re-derive the cause from the same session
# mds. That re-derivation would be a SECOND copy of this boundary, and two
# copies of a boundary is the defect class this ticket's sibling (T-0712) is
# about. So the reason is decided HERE, once, and ``live_successor_sid`` stays
# the thin "who" view over the same single implementation.
SUCCESSOR_FOUND = "found"
SUCCESSOR_TARGET_LIVE = "target_live"
SUCCESSOR_NO_SESSION_MD = "no_session_md"
SUCCESSOR_UNPARSEABLE_SID = "unparseable_sid"
SUCCESSOR_NONE_LIVE = "none_live"
SUCCESSOR_AMBIGUOUS = "ambiguous"


def successor_lookup(cfg: Any, slug: str, sid: str) -> dict:
    """T-0790/T-0896: who now holds ``sid``'s seat — and, when nobody does, why.

    A session that is recycled is not resumed — it is REPLACED: a brand-new SID
    is minted in the SAME tmux window (measured on the live install: operator
    ``…-p374`` suspended at 16:20:00Z, ``…-p455`` started 16:20:10Z). The
    predecessor's md survives as ``status: suspended`` and its inbox is never
    renamed (``rebind_sid`` only fires on ``resume()``, which rotates in place),
    so every later write to it is a black hole.

    Names a successor only when the answer is UNAMBIGUOUS — exactly one live
    holder shares the target's window stem and linux user, and it is not the
    target itself. Ambiguity (two live sessions in one window) or absence
    resolves to no successor and the caller keeps its existing behaviour: a
    wrong redirect is worse than the loss it would prevent.

    Deliberately NOT applied to a live target: a live holder reads its own
    inbox, so there is nothing to redirect. That check is also FIRST on purpose
    — every literal-SID send runs this, and the common case (a live target) costs
    one file read and never reaches the sessions-dir walk below.

    Returns ``{"sid": <successor|None>, "reason": <SUCCESSOR_*>, "candidates":
    [<live sids sharing the window>]}``. The two reasons that are NOT "there is
    simply nobody there" are the ones T-0896 measured and the ones a refusal
    has to be able to name apart:

    * ``no_session_md`` — the predecessor's own md is gone (session mds are
      reaped; the lifecycle json outlives them), so there is no window stem to
      match against and NO lookup is possible. Not "no successor exists".
    * ``ambiguous`` — two live sessions share the window and this function
      REFUSES to guess. Correct behaviour, but silently it is worth no more
      than the loss, which is why the reason has to travel to the caller.
    """
    from bot_squad_worker.sessions import _is_live_holder

    meta = _session_status(cfg, slug, sid)
    if meta is None:
        return {"sid": None, "reason": SUCCESSOR_NO_SESSION_MD, "candidates": []}
    if _is_live_holder(meta):
        return {"sid": None, "reason": SUCCESSOR_TARGET_LIVE, "candidates": []}
    want = _sid_stem(sid)
    if want is None:
        return {"sid": None, "reason": SUCCESSOR_UNPARSEABLE_SID, "candidates": []}
    matches = [
        cand for cand, cand_meta in _list_session_sids(cfg, slug)
        if cand != sid and _is_live_holder(cand_meta) and _sid_stem(cand) == want
    ]
    if len(matches) == 1:
        return {"sid": matches[0], "reason": SUCCESSOR_FOUND, "candidates": matches}
    if matches:
        log.warning(
            "intersession: %s is not live and %d live sessions share its "
            "window %r — refusing to guess a successor (slug=%s)",
            sid, len(matches), want[1], slug,
        )
        return {"sid": None, "reason": SUCCESSOR_AMBIGUOUS, "candidates": matches}
    return {"sid": None, "reason": SUCCESSOR_NONE_LIVE, "candidates": []}


def live_successor_sid(cfg: Any, slug: str, sid: str) -> str | None:
    """The LIVE session now holding ``sid``'s seat, or None (T-0790).

    The "who" view over :func:`successor_lookup` — same decision, same log
    line, contract unchanged for the peer bus that has always called this.
    """
    return successor_lookup(cfg, slug, sid)["sid"]


def _resolve_recipients(
    cfg: Any,
    slug: str,
    to: str,
    from_sid: str | None = None,
    user: str | None = None,
) -> list[str]:
    """Map a recipient spec to a list of SIDs.

    Role keywords:
      - ``teamlead``: every LIVE session with no task_id (or task_id == "~")
      - ``dev``:      every LIVE session with a real task_id
      - ``all``:      every LIVE session listed in data/<slug>/sessions/
      - ``operator``: every LIVE operator-role session (T-0790)

    Anything else is treated as a literal SID (returned as-is — see ``send``
    docstring: an unknown SID still gets a per-sid inbox so the recipient
    will pick it up on their next read), EXCEPT a SID that has been SUPERSEDED
    by a recycle, which resolves to its live successor (T-0790, see
    ``live_successor_sid``).

    T-0790 (``operator``): ``operator`` was NOT a role keyword, so it fell
    through to the literal-SID branch and wrote ``_chat/inbox-operator.log`` —
    a file no session owns or drains. Three internal escalation callers address
    it that way — ``autocompact._alert_orphaned_handoff``,
    ``uc_redrive._notify_operator_stuck`` and ``recovery._do_park``, all of them
    variations on "needs a human look"; the live install had 27 undrained lines
    in that file, 22 of them uc_redrive escalations spanning 2026-07-04…07-27.
    It is also WHY the operator hop is the one that broke silently: with no role
    keyword for the role the product is built around, every relay to it had to
    name a remembered SID. Resolution delegates to
    :func:`dispatch.live_operator_sids`, the operator-identity SSOT (T-0523), so
    this does not add divergent identity logic — which also means it catches the
    canonical md-less operator pane that a session-md scan misses.

    T-0683: role-keyword fan-out is scoped to LIVE sessions only (the same
    ``_is_live_holder`` check ``bsq team status``'s default roster and the
    dispatch/binding paths already use — status active/paused, not archived).
    Without this, a role broadcast walked EVERY session md ever written for
    the project, including long-dead/archived ones, and fired an
    ``inject_input`` "check mail" nudge at each — a project with a large
    historical fleet turned one broadcast into a burst of hundreds of
    "no live pane for sid" 400s. A literal-SID target is never filtered:
    addressing a specific SID is already an explicit choice (see
    ``test_send_to_unknown_sid_still_writes_inbox``).

    T-0157 (multi-user boundary): role-keyword fan-out is scoped to a single
    linux user so a TL on one user's tmux can't message another user's
    sessions by default. The scope user is the explicit ``user`` override
    when given, else the sender's own linux user (parsed from ``from_sid``).

    Scoping only engages when the project actually has MORE THAN ONE distinct
    linux user among its LIVE sessions — a single-user project (the common
    case) behaves exactly as pre-T-0157 ("works just as good as one user").
    When no scope user resolves (legacy non-SID sender like "stakeholder", no
    override) the fan-out is also unscoped, preserving cross-user
    notifications such as ``bind_task``'s stakeholder→SID notify (a literal
    SID anyway). A literal-SID target is never scoped: addressing a specific
    ``S-<user>-…`` SID is already an explicit choice.
    """
    if to == "operator":
        # T-0943: the ROUTING resolver, not the singleton guard. A solo session
        # holds the operator role among others (``sessions.roles_of``), so an
        # escalation addressed to ``operator`` reaches the session that is
        # actually driving the board instead of resolving to nothing. A
        # DEDICATED operator still wins whenever one is live — see
        # :func:`dispatch.operator_role_holders`.
        from bot_squad_worker.dispatch import operator_role_holders
        return operator_role_holders(cfg, slug)
    # NB: ``operator`` returned above — it is a role fan-out but resolves via the
    # identity SSOT, not this task_id-shaped walk. Keep this set literal so
    # reordering the branches can't silently route ``operator`` through here.
    if to in _TASK_SHAPED_KEYWORDS:
        from bot_squad_worker.sessions import _is_live_holder
        rows = [
            (sid, meta) for sid, meta in _list_session_sids(cfg, slug)
            if _is_live_holder(meta)
        ]
        distinct_users = {
            u for u in (_session_linux_user(sid, meta) for sid, meta in rows) if u
        }
        multi_user = len(distinct_users) > 1
        scope_user = (user or "").strip() or _linux_user_from_sid(from_sid or "")
        # An explicit `user` override always scopes (the caller is deliberately
        # crossing/selecting a user); the sender's implicit user only scopes
        # when the project is genuinely multi-user.
        apply_scope = bool(scope_user) and (bool((user or "").strip()) or multi_user)
        sids: list[str] = []
        for sid, meta in rows:
            if apply_scope and _session_linux_user(sid, meta) != scope_user:
                continue
            tid = meta.get("task_id", "") or ""
            is_dev = bool(tid) and tid != "~"
            if to == "all":
                sids.append(sid)
            elif to == "teamlead" and not is_dev:
                sids.append(sid)
            elif to == "dev" and is_dev:
                sids.append(sid)
        # T-0943: a session that DECLARED the role receives it too, even when
        # its task shape says otherwise -- a solo session holds `dev` with no
        # ticket, and a talking-operator holds none. Union, never replacement:
        # the walk above is the legacy contract and it keeps its recipients.
        if to in ("dev", "teamlead"):
            from bot_squad_worker.sessions import role_holders
            # DECLARED holders only — see `role_holders`. `dev` is
            # `_derive_role`'s default, so a derived union delivers every
            # dev broadcast to every coordinator.
            for sid in role_holders(cfg, slug, to, declared_only=True):
                if sid not in sids and not (
                        apply_scope and _linux_user_from_sid(sid) != scope_user):
                    sids.append(sid)
        return sids
    # T-0943 reopen: every other model role -- `user-conversation` above all --
    # resolves to whoever HOLDS it. This is the missing keyword, not a widening
    # of the resolver: an unknown target still falls through to the literal-SID
    # branch below and is still refused by `_action_peer_send` if it names no
    # registered session.
    if to in _ROLE_KEYWORDS:
        from bot_squad_worker.sessions import role_holders
        return role_holders(cfg, slug, to)
    successor = live_successor_sid(cfg, slug, to)
    if successor is not None:
        log.warning(
            "intersession: %s was RECYCLED — routing to its live successor %s "
            "instead of a dead inbox (slug=%s)", to, successor, slug,
        )
        return [successor]
    return [to]


def _inbox_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"inbox-{sid}.log"


def _seen_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"seen-{sid}"


def _heartbeat_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"heartbeat-{sid}"


def _touch(p: Path) -> None:
    try:
        p.touch(exist_ok=True)
    except OSError:
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def reap_chat_sidecars(cfg: Any, slug: str, sid: str) -> list[str]:
    """T-0447 (#4): free a session's per-SID peer-bus scratch once it is
    archived/historical.

    Removes ``inbox-<sid>.log``, ``seen-<sid>`` and ``heartbeat-<sid>`` under
    ``data/<slug>/_chat`` — the malloc-with-no-free that otherwise grows one
    triple per session forever. Counterpart to ``rebind_sid`` (which renames the
    triple): this is the terminal free.

    NEVER raises (a reap failure must not wedge the archive) and never creates
    the ``_chat`` dir: if it doesn't exist there's nothing to reap. Missing
    files are skipped (the triple is created lazily, so not all three always
    exist). Returns the list of paths actually removed (empty on no-op / re-run,
    so it's idempotent).
    """
    removed: list[str] = []
    if not sid:
        return removed
    chat = Path(cfg.data_dir) / slug / "_chat"
    if not chat.exists():
        return removed
    for name in (f"inbox-{sid}.log", f"seen-{sid}", f"heartbeat-{sid}"):
        p = chat / name
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except OSError:
            log.warning("reap_chat_sidecars: failed to remove %s", p, exc_info=True)
    return removed


def rebind_sid(cfg: Any, slug: str, old_sid: str, new_sid: str) -> dict:
    """T-0072: atomically rename a peer-bus triple from ``old_sid`` to ``new_sid``.

    Touches:
      - ``inbox-<sid>.log``    — append-only message log
      - ``seen-<sid>``         — read high-water mark (byte offset)
      - ``heartbeat-<sid>``    — long-poll heartbeat marker

    SID rotation happens on ``sessions.resume()`` (new pane → new SID) and
    on the SessionStart hook's ``tmux break-pane`` block (agent-teams
    teammate gets its own window → new SID). Without this primitive the
    pre-rotation messages stay in ``inbox-<old_sid>.log`` and any peer that
    still addresses ``old_sid`` lands in a dead inbox.

    No-op on self-rebind (``old_sid == new_sid``). On collision (target file
    already exists), the source is left in place and a warning is logged —
    a silent merge would re-order messages relative to whatever already
    landed in the target inbox. Counts only files that actually moved in
    the returned ``renamed`` list; missing sources are simply skipped (the
    inbox triple is created lazily, not all three always exist).
    """
    if not old_sid or not new_sid:
        return {"ok": True, "renamed": [], "reason": "empty-sid"}
    if old_sid == new_sid:
        return {"ok": True, "renamed": [], "reason": "self"}
    chat = _chat_dir(cfg, slug)
    suffixes = [
        ("inbox-", ".log"),
        ("seen-", ""),
        ("heartbeat-", ""),
    ]
    renamed: list[str] = []
    collisions: list[str] = []
    for prefix, suffix in suffixes:
        old_p = chat / f"{prefix}{old_sid}{suffix}"
        new_p = chat / f"{prefix}{new_sid}{suffix}"
        if not old_p.exists():
            continue
        if new_p.exists():
            log.warning(
                "rebind_sid: collision at %s — leaving both, not merging "
                "(old_sid=%s new_sid=%s slug=%s)",
                new_p.name, old_sid, new_sid, slug,
            )
            collisions.append(new_p.name)
            continue
        os.rename(old_p, new_p)
        renamed.append(old_p.name)
    return {"ok": True, "renamed": renamed, "collisions": collisions}


def send(
    cfg: Any,
    slug: str,
    from_sid: str,
    to: str,
    text: str,
    user: str | None = None,
    blocked: bool = False,
) -> dict:
    """Append a message line to recipient inboxes.

    Returns ``{"ok": True, "delivered_to": [sid, ...]}``, plus
    ``"redirected": {"from": <requested sid>, "to": <live successor>,
    "reason": "recycled"}`` when the requested SID had been superseded by a
    recycle (T-0790). Callers that only read ``delivered_to`` are unaffected —
    it already names where the message actually went.

    Never raises on an undeliverable target; the routing FACTS are returned (and
    logged) and the ``peer_send`` action turns the agent-facing ones into an
    error. ~12 internal callers treat this as a best-effort notify, so raising
    here would trade a message loss for a broken tick.

    T-0157: ``user`` overrides the linux-user scope for role-keyword fan-out
    (``teamlead``/``dev``/``all``); without it the scope is the sender's own
    linux user parsed from ``from_sid``. See ``_resolve_recipients``.

    T-0943: an ESCALATION role keyword (``operator``/``teamlead``) that has NO
    live holder is likewise REFUSED — ``{"ok": False, "reason": "role-unfilled",
    "error": <what to do>, "delivered_to": []}``. Handing work to a role nobody
    holds is a no-op that reads to the sender exactly like a delivery, and a
    solo session obeying that clause spent a whole session producing nothing
    and reported it as compliance (the T-0943 incident). ``dev``/``all`` stay
    silent on an empty fan-out — they are broadcasts, not escalations.

    T-0827: over-cap text is REFUSED — ``{"ok": False, "reason":
    "text-over-cap", "error": <what to do>, "delivered_to": []}``, and NOTHING
    is written to any inbox. The forbidden outcome this kills is "caller
    believes it sent, recipient got part", so the refusal is total: a partial
    delivery is never preferable to a failure the sender can see. Callers that
    only read ``delivered_to`` still cannot mistake it for success — it is
    empty. The refusal is returned rather than raised so the internal
    best-effort notifiers keep the never-raises contract above; it is also
    logged at ERROR, because a caller that ignores the return must still leave
    a trace rather than repeating the silence this ticket exists to end.
    """
    try:
        sanitized = _sanitize(text)
    except ValueError as exc:
        log.error(
            "intersession: REFUSED an over-cap peer message from %s to %r "
            "(slug=%s): %s", from_sid, to, slug, exc,
        )
        return {
            "ok": False,
            "delivered_to": [],
            "reason": "text-over-cap",
            "error": str(exc),
        }
    recipients = _resolve_recipients(cfg, slug, to, from_sid=from_sid, user=user)
    line = f"{_now_iso()}\t[from {from_sid}]\t{sanitized}\n"
    delivered: list[str] = []
    for sid in recipients:
        inbox = _inbox_path(cfg, slug, sid)
        with inbox.open("ab") as f:
            f.write(line.encode("utf-8"))
        delivered.append(sid)
    # T-0790: a consequence of routing ``operator`` through the identity SSOT
    # instead of the literal-SID branch — with no live operator it now resolves
    # to NOTHING, where before it wrote (an unread) ``inbox-operator.log``. So
    # the one case where that matters gets a log line: the three callers using
    # this keyword are all "needs a human look" escalations, and an escalation
    # that reached nobody must not be silent. Deliberately NOT warned for
    # teamlead/dev/all — an empty dev fan-out is an ordinary, frequent state.
    escalation_roles = (_BLOCKED_ESCALATION_ROLES if blocked
                        else _ESCALATION_ROLES)
    if to in escalation_roles and not delivered:
        # T-0943 — ESCALATION IS CONDITIONED ON A RECIPIENT EXISTING.
        #
        # The stakeholder raised a project's only session and asked what it had
        # produced. Its answer: "None. My role is user-conversation only —
        # attend the thread, file asks, and ESCALATE TO THE OPERATOR/DEV
        # SESSIONS." There was no operator and no dev; it had handed the work
        # to nobody and reported that as compliance. An escalation to an
        # unfilled role is a no-op that renders identically to a delivered one,
        # which is what let a whole session go by producing nothing.
        #
        # So it is now a REFUSAL the sender sees, not a log line in a file
        # nobody reads (the live install had 27 undrained escalations spanning
        # three weeks — the same silence, one layer down). ``ok: False`` is what
        # ``_action_peer_send`` turns into an error for the agent; the ~12
        # internal best-effort notifiers read ``delivered_to``, which is empty
        # either way, so none of them change behaviour.
        #
        # ``dev`` and ``all`` are deliberately NOT here: an empty dev fan-out is
        # an ordinary, frequent state and not an escalation to anyone in
        # particular.
        log.warning(
            "intersession: peer_send to role %r reached NOBODY — no live "
            "holder for %s; escalation from %s was not delivered: %.200s",
            to, slug, from_sid, sanitized,
        )
        return {
            "ok": False,
            "delivered_to": [],
            "reason": "role-unfilled",
            "error": (
                f"no live session holds the {to!r} role on {slug!r} — NOTHING "
                f"was delivered. This is not a queue: an unfilled role has no "
                f"inbox that will ever be drained. If you hold that role "
                f"yourself, do the work here rather than hand it off (`bsq "
                f"brief` names every role you hold; a project's only session "
                f"holds user-conversation + operator + dev at once). Otherwise "
                f"bud one off (`bsq bud operator`) or page the stakeholder "
                f"(`bsq tg ping`)."
            ),
        }
    out: dict = {"ok": True, "delivered_to": delivered}
    if to not in _ROLE_KEYWORDS and delivered and delivered != [to]:
        out["redirected"] = {"from": to, "to": delivered[0], "reason": "recycled"}
    return out


def send_notice(
    cfg: Any,
    slug: str,
    from_sid: str,
    to: str,
    text: str,
    user: str | None = None,
) -> dict:
    """Deliver a MACHINE-GENERATED notification, SPLITTING instead of refusing.

    The companion to :func:`send`, and the split of the two is by ONE question:
    **is there anybody to tell?**

    * :func:`send` refuses over-cap text. Right for a caller that owns the text
      and can act on a failure — the ``peer_send`` action (an agent at a CLI),
      ``sync_channel`` (an agent's live message). A refusal makes them shorten
      and retry, and it keeps evidence off a notification channel.
    * :func:`send_notice` splits. Right for a tick — ``telemetry``,
      ``deploy_monitor``, ``autocompact``'s orphan alert, ``bind_task``'s notify.
      These have NOWHERE to report a refusal: no caller frame is watching, and
      the message is a machine-composed alert whose loss is the whole cost.

    T-0827 made this distinction necessary. Handing a tick a refusal would have
    replaced SILENT TRUNCATION with SILENT TOTAL LOSS — for those callers a
    strictly worse outcome, introduced by the change whose purpose was to
    abolish silent loss on this path. The bar the operator set, and the one this
    function exists to meet: **no path may end in "the caller believes it sent
    and the recipient got nothing."**

    Splitting is acceptable HERE and not in ``send`` for the reason the
    anti-split argument was made in the first place: the objection is
    reordering against other traffic, which costs a human reader a coherent
    message. A tick's alert is one machine-composed paragraph, each part is
    labelled ``[part i/N]``, and no reader is reconstructing an argument from
    it. Measured today, every one of these callers emits a short template well
    under the cap — so this is a BACKSTOP against a future template growing,
    not a live need, and that is exactly why it must not be a comment saying
    "keep these short".

    ⚠ DO NOT DELETE THIS AS DEAD CODE WHEN YOU FIND NO LIVE INSTANCE. That
    measurement is the same one made here, and finding it again is confirmation
    the backstop works — not evidence it is unused. A guard whose value is that
    it has never fired is the hardest kind to keep, so the argument lives here
    rather than waiting to be re-derived. It is not left to prose either:
    ``test_send_notice_splits_instead_of_refusing`` and
    ``test_send_notice_leaves_an_under_cap_notice_completely_untouched`` both go
    red on removal, and the second is the one that catches a "simplify it back
    to ``send``" edit, since that is what a deletion actually looks like.

    Returns ``{"ok": True, "delivered_to": [...], "parts": n}``. ``ok`` is False
    when a part was itself undeliverable — never by length, but since T-0943
    also when the target role keyword has NO live holder, in which case nothing
    was written for any part. That is not a split failure and this function
    does not soften it: a notice addressed to a role nobody holds has the same
    "believed sent, received by nobody" shape the whole split exists to avoid,
    and the tick callers here already log their own miss off ``delivered_to``.

    Known bound, stated rather than discovered later: each part resolves its
    recipients independently, so a role fan-out whose roster changes mid-split
    could deliver part 1 and part 2 to different sets. Ticks address literal
    SIDs or ``operator``; the window is milliseconds; a split is rare. Not
    worth a roster snapshot, worth writing down.
    """
    parts = _split_for_bus(text)
    if len(parts) == 1:
        return {**send(cfg, slug, from_sid, to, parts[0], user=user), "parts": 1}
    delivered: list[str] = []
    ok = True
    for i, part in enumerate(parts, 1):
        out = send(cfg, slug, from_sid, to, f"[part {i}/{len(parts)}] {part}", user=user)
        ok = ok and bool(out.get("ok", True))
        for sid in out.get("delivered_to", []):
            if sid not in delivered:
                delivered.append(sid)
    log.warning(
        "intersession: split an over-cap notification from %s to %r into %d parts "
        "(slug=%s, %d chars) — a tick's template has outgrown the bus cap",
        from_sid, to, len(parts), slug, len(text or ""),
    )
    return {"ok": ok, "delivered_to": delivered, "parts": len(parts)}


#: Room reserved for the ``[part i/N] `` marker. Generous on purpose: the
#: marker is added AFTER the split, so an under-estimate would push a part back
#: over the cap and `send` would refuse it — turning the fix into the bug.
_PART_MARKER_ROOM = 24


def _split_for_bus(text: str) -> list[str]:
    """Chunk ``text`` so every part fits the cap once a marker is prefixed.

    Measured on the FLATTENED length, because that is what ``_sanitize``
    produces and what the cap is checked against. Flattening only ever shortens
    (``\\r\\n`` → one space), so chunking the raw text is conservative in the
    safe direction.
    """
    text = text or ""
    if len(text) <= _MAX_TEXT_LEN:
        return [text]
    size = _MAX_TEXT_LEN - _PART_MARKER_ROOM
    return [text[i:i + size] for i in range(0, len(text), size)]


def inbox_read(cfg: Any, slug: str, sid: str) -> dict:
    """Drain inbox lines since the seen-<sid> byte offset."""
    _touch(_heartbeat_path(cfg, slug, sid))
    inbox = _inbox_path(cfg, slug, sid)
    seen = _seen_path(cfg, slug, sid)
    if not inbox.exists():
        # Touch a zero-byte inbox so subsequent waits have something to watch.
        inbox.touch()
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0
    size = inbox.stat().st_size
    messages: list[str] = []
    if size > offset:
        with inbox.open("rb") as f:
            f.seek(offset)
            buf = f.read(size - offset)
        text = buf.decode("utf-8", errors="replace")
        # Split on real newlines, drop the trailing empty from the final \n.
        messages = [m for m in text.split("\n") if m]
        seen.write_text(str(size))
    return {"ok": True, "messages": messages, "count": len(messages)}


def inbox_wait(
    cfg: Any,
    slug: str,
    sid: str,
    timeout: float,
    shutdown_event: threading.Event | None = None,
) -> dict:
    """Long-poll for inbox growth.

    Returns ``{"ok": True, "ready": bool, "elapsed_sec": float}``. ``ready``
    True means "you have new mail, call ``inbox_read``"; False means the
    timeout expired without new mail. ``timeout`` is clamped to
    ``_MAX_WAIT_TIMEOUT`` (7200s / 2h as of T-0091).

    T-0119: if the process-wide shutdown event (or one passed via
    ``shutdown_event``) is set, returns early with
    ``{"ok": True, "ready": False, "elapsed_sec": <x>, "reason": "shutdown"}``
    so curl clients see a clean response instead of an SIGKILL-induced
    empty reply when systemd restarts the worker.
    """
    timeout = max(0.0, min(float(timeout), float(_MAX_WAIT_TIMEOUT)))
    inbox = _inbox_path(cfg, slug, sid)
    seen = _seen_path(cfg, slug, sid)
    hb = _heartbeat_path(cfg, slug, sid)
    if not inbox.exists():
        inbox.touch()
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0

    ev = shutdown_event if shutdown_event is not None else _SHUTDOWN_EVENT

    start = time.monotonic()
    deadline = start + timeout
    last_hb = 0.0
    # Plain stat() poll (1s) — keeps the implementation portable. Linux
    # inotify would only buy us sub-second wake latency, and the message
    # bus's latency budget is "human-perceptible turn", not sub-second.
    poll_interval = 1.0
    while True:
        now = time.monotonic()
        if ev is not None and ev.is_set():
            return {
                "ok": True,
                "ready": False,
                "elapsed_sec": now - start,
                "reason": "shutdown",
            }
        if now - last_hb >= _HEARTBEAT_INTERVAL:
            _touch(hb)
            last_hb = now
        try:
            size = inbox.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > offset:
            return {"ok": True, "ready": True, "elapsed_sec": now - start}
        if now >= deadline:
            return {"ok": True, "ready": False, "elapsed_sec": now - start}
        sleep_for = min(poll_interval, deadline - now)
        if sleep_for > 0:
            time.sleep(sleep_for)
