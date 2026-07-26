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
import stat
import threading
import time
from pathlib import Path
from typing import Any

from bot_squad_worker import frontmatter as _frontmatter

log = logging.getLogger(__name__)

_MAX_TEXT_LEN = 4000
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
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN]
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

    Anything else is treated as a literal SID (returned as-is — see ``send``
    docstring: an unknown SID still gets a per-sid inbox so the recipient
    will pick it up on their next read).

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
    if to in {"teamlead", "dev", "all"}:
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
        return sids
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
) -> dict:
    """Append a message line to recipient inboxes.

    Returns ``{"ok": True, "delivered_to": [sid, ...]}``.

    T-0157: ``user`` overrides the linux-user scope for role-keyword fan-out
    (``teamlead``/``dev``/``all``); without it the scope is the sender's own
    linux user parsed from ``from_sid``. See ``_resolve_recipients``.
    """
    sanitized = _sanitize(text)
    recipients = _resolve_recipients(cfg, slug, to, from_sid=from_sid, user=user)
    line = f"{_now_iso()}\t[from {from_sid}]\t{sanitized}\n"
    delivered: list[str] = []
    for sid in recipients:
        inbox = _inbox_path(cfg, slug, sid)
        with inbox.open("ab") as f:
            f.write(line.encode("utf-8"))
        delivered.append(sid)
    return {"ok": True, "delivered_to": delivered}


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
