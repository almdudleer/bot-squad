"""Worker action allowlist.

Every action is a typed function registered here. The dispatcher refuses
unknown action names and unexpected parameters. This is the security
boundary: even if the API is compromised, the attacker can only invoke
actions on this allowlist with their declared parameter shapes.

v1 ships `noop` (proof-of-life) and `tg_verify_login` (HMAC verification
proxied from the API — the bot token lives only in the worker post spec #3).
Spec #3 adds `tg_notify`.

Phase 2 multi-user: each action carries a mode tag — coordinator_only,
tmux_only, or both. The dispatcher checks the running worker's mode
(set by __main__ from BOT_SQUAD_MODE env) before invoking.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

# T-0721: the TG transport module owns the message cap + the ONE long-message
# splitter (see tg.split_for_tg). Import-safe at module level — tg imports
# nothing from this package at import time.
from bot_squad_worker import tg as _tg_mod

log = logging.getLogger(__name__)


class ActionError(Exception):
    """Raised when an action call is invalid (unknown name, bad params)."""


# ---------------------------------------------------------------------------
# Worker mode — set by __main__.py at startup from BOT_SQUAD_MODE env.
# "coordinator": runs scheduler + all actions (single-host).
# "user-worker": tmux ops only; coordinator actions return ActionError.
# None (unset): legacy single-process — behaves like coordinator.
# ---------------------------------------------------------------------------

_MODE: str | None = None  # one of {"coordinator", "user-worker", None}


def set_mode(mode: str | None) -> None:
    """Called once at startup; tests can call directly to flip modes."""
    global _MODE
    _MODE = mode


def get_mode() -> str:
    """Return effective mode — unset is treated as coordinator (back-compat)."""
    return _MODE or "coordinator"


# ---------------------------------------------------------------------------
# Config accessor — injected at startup by __main__.py via set_config().
# Tests override it via monkeypatch.setattr(A, "_get_config", lambda: cfg).
# ---------------------------------------------------------------------------

_CONFIG: Any = None  # will be set to a Config instance


def set_config(cfg: Any) -> None:
    """Called once at startup (and in integration tests) to inject the live config."""
    global _CONFIG
    _CONFIG = cfg


def _get_config() -> Any:
    if _CONFIG is None:
        raise ActionError("worker config not initialised")
    return _CONFIG


# ---------------------------------------------------------------------------
# Scheduler singleton — injected at startup by __main__.py via set_scheduler().
# Tests can monkeypatch _SCHED with a stub BackgroundScheduler.
# ---------------------------------------------------------------------------

_SCHED: Any = None  # BackgroundScheduler | None


def set_scheduler(sched: Any) -> None:
    """Called once at startup after the scheduler is created."""
    global _SCHED
    _SCHED = sched


# ---------------------------------------------------------------------------
# TgClient singleton — created lazily on first use.
# Tests replace _TG or monkeypatch _get_tg_client directly.
# ---------------------------------------------------------------------------

_TG: Any = None  # TgClient | None


def _get_tg_client(cfg: Any) -> Any:
    """Return the module-level TgClient, creating it on first call."""
    global _TG
    if _TG is None:
        from bot_squad_worker.tg import TgClient
        _TG = TgClient(cfg)
    return _TG


# T-0247: MaxClient singleton — mirror of the TgClient one above.
_MAX: Any = None  # MaxClient | None


def _get_max_client(cfg: Any) -> Any:
    """Return the module-level MaxClient, creating it on first call."""
    global _MAX
    if _MAX is None:
        from bot_squad_worker.max import MaxClient
        _MAX = MaxClient(cfg)
    return _MAX


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_noop(params: dict[str, Any]) -> dict[str, Any]:
    """Proof-of-life: takes no params, returns {ok, ts}."""
    if params:
        raise ActionError(f"noop takes no params, got: {sorted(params)}")
    return {"ok": True, "ts": time.time()}


def _action_tg_verify_login(params: dict[str, Any]) -> dict[str, Any]:
    """Verify a Telegram Login Widget payload using the bot token from secrets.toml.

    Expected params: {"payload": <tg login dict>}
    Returns: {"ok": true, "user": {...}} or {"ok": false, "error": "..."}
    """
    extra = set(params) - {"payload"}
    if extra:
        raise ActionError(f"tg_verify_login got unexpected params: {sorted(extra)}")
    if "payload" not in params:
        raise ActionError("tg_verify_login missing required param: payload")

    cfg = _get_config()
    if not cfg.tg_bot_token:
        raise ActionError("tg_verify_login: bot token not configured")

    try:
        from bot_squad_worker.auth import verify_tg_login
        user = verify_tg_login(params["payload"], cfg.tg_bot_token, cfg.tg_auth_age_max)
        return {"ok": True, "user": user}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_TG_NOTIFY_ALLOWED = {
    "slug", "chat_id", "message", "sid", "user", "urgent", "topic_id",
    # T-0241: process→user "needs input" enrichment (tmux-attach command).
    "needs_input", "tmux_session",
    # T-0386: route by message CLASS (feedback/deploy_logs/team_queries) — the
    # class resolves to the project's forum thread-id via tg_topics.
    "topic",
    # T-0569: an interactive relay (e.g. a session-authored conversation reply)
    # wants every send delivered, not deduped against a recent identical
    # payload — mirrors the debounce=False the channel abstraction already uses
    # for interactive replies (tg_listener._channel_notify).
    "debounce",
    # T-0635: explicit task id for the truncation-pointer deep link — see
    # _page_detail_link. Optional; falls back to a sessions-page link when
    # absent.
    "task_id",
    # T-0660: direct-write into a task's forum topic — resolves chat_id/
    # topic_id from the ticket's bound topic (tg_bindings.find_by_ticket)
    # instead of the caller spelling out chat_id/topic_id itself.
    "ticket_id",
    # T-0755: suppress the outbound-content record for a caller that already
    # wrote this same text into the same conversation thread itself — today,
    # only the API's session-writeback relay. Default True (audited).
    "record_outbound",
    # T-0758: the raw SID of the session that COMPOSED the text, for the
    # transport's sender tag. NOT a second `sid` — it is label-only and has no
    # destination or reply-map effect, which is exactly what the relay needs.
    "sender_sid",
}


def _resolve_tmux_session(cfg: Any, slug: str, sid: str) -> str:
    """Best-effort tmux session name for a SID (its SessionMd ``tmux_session``).

    Used by the T-0241 needs-input enrichment to build the ``tmux attach``
    command when the caller didn't pass an explicit ``tmux_session``. Returns
    "" when it can't be resolved (the footer then omits the attach line)."""
    if not sid or not slug:
        return ""
    try:
        from bot_squad_worker import sessions as _sessions
        meta = _sessions.resolve_session(cfg, slug, sid)
    except Exception:
        meta = None
    return str((meta or {}).get("tmux_session") or "")


def _sender_task_ids(cfg: Any, slug: str, sid: str) -> list[str]:
    """The task ids the sending session is bound to right now — its SessionMd
    ``task_id`` plus any ``extra_task_ids`` (a bundled/BIND_TASK dev owns
    several). Ordered: the primary binding first. ``~``/empty sentinels and
    duplicates are dropped; unresolvable session -> ``[]``.

    Deliberately does NOT read ``last_task_id`` (the reaped-binding keepsake,
    see sessions.py): a finished task's topic is no longer the session's home.
    """
    if not sid or not slug:
        return []
    try:
        from bot_squad_worker import sessions as _sessions
        meta = _sessions.resolve_session(cfg, slug, sid) or {}
    except Exception:  # noqa: BLE001 — routing must never fail on a bad md
        return []
    out: list[str] = []
    extra = meta.get("extra_task_ids") or []
    for raw in [meta.get("task_id"), *(extra if isinstance(extra, list) else [])]:
        tid = str(raw or "").strip()
        if tid and tid != "~" and tid not in out:
            out.append(tid)
    return out


def _own_topic_binding(cfg: Any, sid: str, slug: str) -> dict | None:
    """T-0723 rung 3: the forum topic that BELONGS to the SENDING session, or
    ``None`` when it doesn't own one.

    Two ways a session owns a topic today, checked in this order:

    1. a binding that NAMES it (``session_id``) — a T-0677 direct-mode/pinned
       topic, or a T-0660 Phase-2 task topic created with its originating
       session (``tg_bindings.find_by_session``);
    2. a topic bound to one of the session's OWN tasks (``task_id`` /
       ``extra_task_ids``) — the T-0660 per-task topic, which is frequently
       created without a ``session_id`` (``tg_bindings.find_by_ticket``).

    This is resolved from the caller's own IDENTITY (the ``sid`` it already
    passes) and never guessed from the conversation locus — guessing "the
    current topic" from where the human last wrote IS the T-0723 bug.

    A General-feed binding (``thread_id is None``) is not a topic of one's own,
    so it returns ``None`` there: such a sender keeps T-0667's locus behaviour
    unchanged. ``slug``, when the caller passed one, is a GUARD rather than a
    lookup key — a binding into a different project is never a valid
    destination for this project's send.
    """
    if not sid:
        return None
    from bot_squad_worker import tg_bindings
    own = tg_bindings.find_by_session(cfg, sid)
    if own is None:
        for tid in _sender_task_ids(cfg, slug, sid):
            found = tg_bindings.find_by_ticket(cfg, tid)
            if found is not None:
                own = found
                break
    if own is None or own.get("thread_id") is None:
        return None
    if slug and own.get("slug") and own["slug"] != slug:
        return None
    return own


def _action_tg_notify(params: dict[str, Any]) -> dict[str, Any]:
    """Send a Telegram message, with optional SID prefix and debounce.

    Params (all optional except ``message``):
        message  : str  — required; the text to send
        chat_id  : str  — explicit chat; takes precedence over slug
        slug     : str  — project slug; resolved to tg_chat in projects.toml
        sid      : str  — the SENDING session (e.g. "S-almdudleer-claude-p5"):
                   its display prefix, its reply-map route (T-0719) and — when
                   no chat/ticket/topic is spelled out — the identity its OWN
                   topic is resolved from (T-0723, rung 3 of the precedence
                   ladder below)
        user     : str  — user prefix component
        debounce : bool — default True; T-0569 pass False to force delivery
                   even if the exact same payload was just sent (an interactive
                   relay reply must not be silently deduped).
        sender_sid : str — T-0758; the raw SID of the session that COMPOSED
                   the message, used ONLY for the transport's `[<slug> <role>]`
                   sender tag. Pass it when the destination is already spelled
                   out (an explicit `chat_id`) and only the AUTHOR is missing —
                   putting the sid in `sid` instead would also enter the
                   destination ladder and the reply map.

    If neither ``chat_id`` nor ``slug`` is given, falls back to the first
    project's tg_chat (there is usually only one project).  Unknown slug
    raises ActionError.

    T-0665: this action is never treated as an automated PAGE — every caller of
    the ``tg_notify`` action is an explicitly-addressed, agent/API-initiated
    conversational send (``bsq tg ping``, ``bsq topic say``, the T-0569 relay,
    admin test-pings), not an automated stall/deploy/autopilot alert page.
    Those alert pages call ``_send_stakeholder_dm`` directly. The distinction
    used to decide who got truncated; since T-0721 nothing is truncated
    anywhere, and it decides only whether a split message carries the
    "подробнее" detail link on its final part.

    Returns {ok: true, sent: <bool>}.
    """
    extra = set(params) - _TG_NOTIFY_ALLOWED
    if extra:
        raise ActionError(f"tg_notify got unexpected params: {sorted(extra)}")
    if "message" not in params:
        raise ActionError("tg_notify missing required param: message")

    cfg = _get_config()

    # --- resolve chat_id (+ project-bound forum topic, T-0156) ---
    # DESTINATION PRECEDENCE — the SSOT, in strict order (T-0723; it used to be
    # implicit in branch order, which is how the locus came to override a
    # sender's own topic). Each rung is only consulted when no earlier one
    # resolved; `topic_id` is laddered independently of `chat_id`, so an
    # explicit `topic_id` param always survives whichever rung supplied the
    # chat:
    #
    #   1. explicit `chat_id` / `topic_id` params   — the caller spelled the
    #                                                 destination out
    #   2. explicit `ticket_id` param      (T-0660) — direct-write into that
    #                                                 task's topic
    #   3. the SENDER'S OWN topic          (T-0723) — `sid`'s per-task (T-0660)
    #                                                 or pinned/direct-mode
    #                                                 (T-0677) topic
    #   4. topic CLASS (`topic` param)     (T-0386) — class -> the project's
    #                                                 own thread for it
    #   5. conversation LOCUS              (T-0667) — where the human last
    #                                                 wrote about this project
    #   6. the project's static tg_chat / tg_topic_id (T-0156) — pre-gateway
    #                                                 default
    #
    # Rungs 3 and 5 are the T-0723 fix: a sender WITH a topic of its own posts
    # there; a sender WITHOUT one still follows the conversation exactly as
    # T-0667 intended (`bsq tg ping` from a topic-less session, a project-level
    # escalation). The ordering is encoded in tests (test_actions.py, the
    # "T-0723" block), not left to branch order.
    chat_id: str | None = params.get("chat_id") or None
    topic_id: int | None = _coerce_topic_id(params.get("topic_id"))
    slug: str = params.get("slug") or ""
    ticket_id: str = params.get("ticket_id") or ""
    task_topic_binding: dict | None = None
    if not chat_id and ticket_id:
        # T-0660: direct-write into a task's forum topic (`bsq topic say` /
        # a dev-TL-orchestrator session posting into its own task's topic) —
        # resolve straight from the ticket id, ahead of slug/locus
        # resolution below (a task topic is more specific than the
        # project's General room).
        from bot_squad_worker import tg_bindings
        binding = tg_bindings.find_by_ticket(cfg, ticket_id)
        if binding is None:
            raise ActionError(f"tg_notify: no topic bound to ticket {ticket_id!r}")
        chat_id = binding["chat_id"]
        if topic_id is None:
            topic_id = binding["thread_id"]
        task_topic_binding = binding
        # T-0676 item 4: a ticket-topic direct-write (`bsq topic say`) rarely
        # passes `slug` explicitly — without it, the sender label built below
        # (see `_send_stakeholder_dm`'s sid_label) loses project context, one
        # of the "messages arrive unattributed" complaints. The binding
        # itself names the project, so default from it rather than requiring
        # every caller to pass a slug it may not have on hand.
        if not slug:
            slug = binding.get("slug") or slug
    if not chat_id:
        # Rung 3 (T-0723): the sending session's OWN topic, resolved from the
        # `sid` it already passes. Ahead of the locus because a sender with a
        # home topic must not be dragged to wherever the human last wrote —
        # the reported bug ("dev sessions are writing into the wrong topic").
        own = _own_topic_binding(cfg, params.get("sid") or "", slug)
        if own is not None:
            chat_id = own["chat_id"]
            if topic_id is None:
                topic_id = own["thread_id"]
            if not slug:
                slug = own.get("slug") or slug
            if own.get("ticket_id"):
                # Landing in a task's topic is a task-topic direct-write no
                # matter which rung resolved it, so it earns the same T-0660
                # mechanic #3 FYI append into the attendant thread as an
                # explicit `ticket_id` send does.
                task_topic_binding = own
    if not chat_id:
        if slug:
            project = cfg.projects.get(slug)
            if project is None:
                raise ActionError(f"tg_notify: unknown project slug {slug!r}")
            # Rung 4 (T-0386, ordered ahead of the locus by T-0723): a message
            # CLASS names a thread inside the PROJECT'S OWN supergroup, so it
            # pairs with tg_chat — pairing a class thread with the locus's chat
            # would address a thread id in the wrong chat. Resolves to None
            # when the project has provisioned no thread for the class, which
            # falls through to the locus below unchanged.
            if topic_id is None and params.get("topic"):
                from bot_squad_worker import tg_topics as _tg_topics
                class_topic = _tg_topics.resolve(cfg, slug, params["topic"])
                if class_topic is not None:
                    chat_id = project.tg_chat
                    topic_id = class_topic
            if not chat_id:
                # Rung 5 (T-0667): prefer the conversation LOCUS (where the
                # user most recently wrote about this project) over the
                # project's static tg_chat — a slug-only send from a session
                # with NO topic of its own (e.g. `bsq tg ping`, a stall
                # escalation) must land in the same place the conversation is
                # actually happening, not always the old default DM. T-0723
                # narrowed WHEN this applies (rung 3 above), never WHETHER.
                from bot_squad_worker import conversation_locus
                locus = conversation_locus.latest_for_slug(cfg, slug)
                if locus:
                    chat_id = locus["chat_id"]
                    if topic_id is None:
                        topic_id = locus.get("thread_id")
                else:
                    # Rung 6: the project's static binding.
                    chat_id = project.tg_chat
                    if topic_id is None:
                        topic_id = project.tg_topic_id
        elif cfg.tg_default_chat_id:
            # T-0171: per-server default chat for the local (detached/standalone)
            # bot — preferred over the first-project guess when configured.
            chat_id = cfg.tg_default_chat_id
        else:
            # Fallback: first registered project's chat (single-project setups)
            if cfg.projects:
                project = next(iter(cfg.projects.values()))
                chat_id = project.tg_chat
                if topic_id is None:
                    topic_id = project.tg_topic_id
            else:
                raise ActionError("tg_notify: no chat_id, no slug, and no projects configured")

    # T-0171 / T-0178 dispatcher seam: this server sends DIRECTLY via its own
    # bot token (TgClient → api.telegram.org) — the "detached / standalone"
    # branch. The "attached" branch (POST to the mothership's relay so it sends
    # via @bot_squad_bot, with a 5-min connectivity fallback to the local token)
    # is intentionally NOT built here — it is net-new cross-server infra
    # deferred to the non-active detach-sequence initiative. When that lands,
    # branch here on the attached-consumer state. See T-0178.
    # T-0241: when a process flags it needs human input, enrich the DM with a
    # join-this-session footer — the exact `tmux attach -t <session>` command
    # (plus a "reply here works too" hint), reusing the tg_stall escalation
    # composer for a consistent format. Force urgent so the quiet-hours gate
    # never drops a blocked process's input request (T-0188).
    # T-0386: when a message CLASS is given (and no explicit numeric topic), map
    # it to the project's forum thread so deploy-logs/team-queries/feedback land
    # in their own thread. Falls back to the legacy single topic / general feed.
    # T-0723: the slug-resolution ladder above already applies the class as its
    # rung 4 (so it beats the locus, which used to preempt it). This is the same
    # resolution for the destinations that ladder never reaches — an explicit
    # `chat_id`/`ticket_id` send, the default-chat fallbacks — and is a no-op
    # whenever a rung already produced a topic.
    topic_class = params.get("topic") or ""
    if topic_id is None and topic_class and slug:
        from bot_squad_worker import tg_topics as _tg_topics
        topic_id = _tg_topics.resolve(cfg, slug, topic_class)

    message = params["message"]
    urgent = bool(params.get("urgent", False))
    # T-0665: the tg_notify ACTION is always an agent/API-initiated, explicitly-
    # addressed send (`bsq tg ping`, `bsq topic say`, the T-0569 conversation
    # relay, the /tg/test / tg-chat-id/test admin pings) — never the automated
    # stall/deploy/autopilot alert pages, which page the human via a DIRECT
    # `_send_stakeholder_dm` call (see tg_stall.py/autopilot.py/telemetry.py/
    # jobs.py/routines.py/autoupdate_apply.py) and control `do_slim` there.
    # Per the SSOT docstring's own stated rule (_send_stakeholder_dm), an
    # explicitly-addressed send is conversational content, not a page — so
    # this action never page-slims; automated page-slimming stays scoped to
    # those direct callers, unaffected by this change.
    do_slim = False
    if bool(params.get("needs_input", False)):
        from bot_squad_worker import tg_stall as _tg_stall
        session_name = params.get("tmux_session") or _resolve_tmux_session(
            cfg, params.get("slug", ""), params.get("sid", "")
        )
        # T-0610 pre-slimmed the QUESTION here so the SSOT's blanket slim
        # couldn't cut the tmux-attach footer off the end. T-0721 removed the
        # blanket slim: the composed page is now split into numbered parts, so
        # a long question no longer costs the footer — and no longer costs the
        # question either. Compose from the full text.
        message = _tg_stall.build_escalation_text(
            cfg, params.get("sid", ""), message, session_name
        )
        urgent = True

    # An EXPLICIT chat_id/topic_id is a TG group/forum target (MAX has no such
    # binding), so those stay on TG. This decision is computed from the RAW
    # params (NOT the resolved topic_id / topic-class) — a topic-class must never
    # disqualify MAX-primary (T-0386 flaw-watch). Delivery itself is the
    # _send_stakeholder_dm SSOT (T-0394).
    explicit_tg_target = bool(params.get("chat_id")) or (params.get("topic_id") not in (None, ""))
    debounce = bool(params.get("debounce", True))
    # T-0799: the ONE automated type that arrives through this action rather
    # than a direct SSOT call — a process flagging `needs_input` is the
    # "a session is blocked and waiting on YOU" page (the same composer
    # tg_stall._escalate uses, which is tagged at its own call site). Tagged
    # ONLY when the destination was NOT spelled out: with an explicit
    # chat_id/topic_id the caller has already said where this goes, and the
    # per-type map must not second-guess a stated address. Every other use of
    # this action is an explicitly-addressed conversational send (T-0665) and
    # deliberately carries no type at all, so it can never be re-routed.
    msg_type = (
        "needs_input"
        if bool(params.get("needs_input", False)) and not explicit_tg_target
        else ""
    )
    result = _send_stakeholder_dm(
        cfg,
        message=message,
        sid=params.get("sid", ""),
        user=params.get("user", ""),
        urgent=urgent,
        tg_chat_id=chat_id,
        tg_topic_id=topic_id,
        prefer_tg=explicit_tg_target,
        msg_type=msg_type,
        debounce=debounce,
        do_slim=do_slim,
        slug=slug,
        task_id=str(params.get("task_id") or ""),
        record_outbound=bool(params.get("record_outbound", True)),
        sender_sid=str(params.get("sender_sid") or ""),
    )
    if task_topic_binding is not None and result.get("sent"):
        # T-0723: the ticket may come from the `ticket_id` param (rung 2) or
        # from the sender's own task topic (rung 3) — take whichever named it.
        _fyi_record_task_topic_direct_write(
            cfg, task_topic_binding["slug"], sid=params.get("sid", ""),
            ticket_id=ticket_id or str(task_topic_binding.get("ticket_id") or ""),
            text=params["message"],
        )
    return result


def _fyi_record_task_topic_direct_write(cfg: Any, slug: str, *, sid: str, ticket_id: str, text: str) -> None:
    """T-0660 mechanic #3: after a session's direct-write into its task's
    topic actually sends, ALSO record a passive FYI append into the
    project's (slug, gid) attendant thread — so the user-conversation
    attendant keeps context of what was said directly to the stakeholder,
    without treating it as its own inbox item.

    ``gid`` is resolved via the conversation LOCUS (T-0667: "most recently
    seen" for this slug) — bot-squad's single-operator-per-project model
    makes this a reasonable proxy for "the" stakeholder even without an
    explicit gid at the call site (mirrors the same resolution `tg_notify`'s
    slug-only path already uses). Best-effort: no gid resolvable (never
    talked to this project via TG yet) -> silently skipped, never raises —
    the direct-write itself already succeeded and must not be undone by a
    context-recording nicety failing."""
    from bot_squad_worker import conversation_locus, tg_listener as _tg_listener
    locus = conversation_locus.latest_for_slug(cfg, slug)
    gid = locus.get("gid") if locus else None
    if not gid:
        return
    sid_label = sid or "?"
    _tg_listener.append_conversation_fyi(
        cfg, slug, gid, author=f"session:{sid_label}",
        text=f"Сессия {sid_label} написала пользователю напрямую (тикет {ticket_id}): {text}",
    )


_PAGE_MODES = ("auto", "tg", "max")
# T-0610 wanted stakeholder-facing pages short-form (headline + refs, detail in
# tasks/threads) and enforced it by TRUNCATING at 400 chars. T-0721 (stakeholder,
# 2026-07-26: "long ones should just split, that's it") overrode that: the limit
# is now a per-part CHUNK SIZE, not a cutoff, and it sits just under TG's 4096
# hard cap so a long page becomes a couple of full messages instead of a swarm
# of 400-char fragments. Brevity stays a writing concern, not a delivery one.
_PAGE_CHUNK_LIMIT = _tg_mod.TG_PART_CHUNK


def _page_mode_path(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "_worker" / "page_channel.json"


def _get_page_mode(cfg: Any) -> str:
    """Current page-channel mode (T-0610 temp-switch): 'auto' = TG-primary
    (the default), 'tg' = same but explicit, 'max' = temporarily page via MAX
    (stakeholder-issued from the TG thread — he cannot write to the MAX bot).
    Missing/corrupt state file = 'auto'."""
    try:
        raw = json.loads(_page_mode_path(cfg).read_text())
    except (OSError, ValueError):
        return "auto"
    mode = str(raw.get("mode", "auto"))
    return mode if mode in _PAGE_MODES else "auto"


def _set_page_mode(cfg: Any, mode: str, *, by: str = "") -> dict[str, Any]:
    p = _page_mode_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mode": mode, "set_by": by, "set_at": _now_iso()}
    p.write_text(json.dumps(payload))
    return payload


def _split_page(text: str, link: str = "") -> list[str]:
    """T-0721: a page that doesn't fit one TG message is SPLIT into numbered
    parts — never truncated.

    Stakeholder, 2026-07-26: «Bot-squad messages are all getting cut off with
    an ellipsis now. That's pointless — long ones should just split, that's
    it.» That is an explicit override of T-0610's slim-page design (cut at 400
    chars + a "… подробнее" pointer *instead of* the rest), so the whole text
    now arrives; the old cutoff is gone.

    Chunking is the shared ``tg.split_for_tg`` (same code as the T-0586
    🎙-echo, per-part budget ~3800 so each part fills a TG message rather than
    emitting a swarm of tiny 400-char ones), with ``tg.part_marker``'s ``(n/N)``
    prefix so a split page reads as one message, not N unrelated alerts.

    A text that fits comes back as ONE unchanged element (byte-identical send,
    no marker). ``link`` (T-0635, best-effort, built by ``_page_detail_link``)
    is kept as a pointer to the full record — but only on the FINAL part of a
    page that actually split, where it is an affordance rather than a
    replacement for the content.
    """
    body = text or ""
    chunks = _tg_mod.split_for_tg(body, limit=_PAGE_CHUNK_LIMIT)
    if len(chunks) == 1:
        return [body]
    total = len(chunks)
    parts = [f"{_tg_mod.part_marker(n, total)} {c}" for n, c in enumerate(chunks, 1)]
    if link:
        parts[-1] = f"{parts[-1]}\nподробнее: {link}"
    return parts


def _page_detail_link(cfg: Any, *, slug: str = "", tg_chat_id: str = "",
                       sid: str = "", task_id: str = "") -> str:
    """T-0635: best-effort staging-web URL for a truncated page's continuation
    pointer. Resolves a project from whatever the call site has on hand —
    explicit ``slug`` first, else a reverse lookup by ``tg_chat_id`` (every
    ``_send_stakeholder_dm`` caller already has this), else the sole project
    on a single-project install — then links to the specific task
    (``task_id``) when known, else the sessions view (scoped to ``sid`` when
    it looks like a real session id, e.g. "S-..." — labels like "autopilot"
    or "routine:<id>" aren't a session to filter on). Empty when no project
    can be resolved (multi-project install, no chat match) — the caller falls
    back to the old generic text rather than a broken link. ``getattr``
    throughout: some test doubles stand in a bare ``SimpleNamespace`` for
    ``Project`` with only the fields that test exercises.

    T-0657: the ``/p/<slug>/...`` route lives ONLY inside bot-squad's own
    web dashboard (``web/src/App.tsx``), deployed once at whichever project
    is flagged ``mothership = true`` in projects.toml. A non-mothership
    project's own ``staging_url`` is a DIFFERENT real deployment (that
    project's own product host, e.g. watchrobot's
    signal-staging.dev.uzinvestapi.com) with no such route — using it as the
    link base produced a garbage/404 stakeholder-DM link. The base host is
    therefore always the mothership project's ``staging_url``; only
    ``resolved_slug``/``sid``/``task_id`` in the path identify the target
    project's session. Falls back to the resolved project's own
    ``staging_url`` when no project is flagged mothership (e.g. a bare
    single-project test fixture) so existing single-project behaviour is
    unchanged.
    """
    resolved_slug = slug
    project = cfg.projects.get(slug) if slug else None
    if project is None and tg_chat_id:
        for k, p in cfg.projects.items():
            if getattr(p, "tg_chat", "") == tg_chat_id:
                project, resolved_slug = p, k
                break
    if project is None and len(cfg.projects) == 1:
        resolved_slug, project = next(iter(cfg.projects.items()))
    staging_url = getattr(project, "staging_url", "") if project else ""
    if not staging_url or not resolved_slug:
        return ""
    dashboard_url = ""
    for p in cfg.projects.values():
        if getattr(p, "mothership", False):
            dashboard_url = getattr(p, "staging_url", "") or ""
            break
    base = (dashboard_url or staging_url).rstrip("/")
    if task_id:
        return f"{base}/p/{resolved_slug}/t/{task_id}"
    if sid.startswith("S-"):
        return f"{base}/p/{resolved_slug}/sessions?sid={sid}"
    return f"{base}/p/{resolved_slug}/sessions"


def _send_stakeholder_dm(
    cfg: Any,
    *,
    message: str,
    sid: str = "",
    user: str = "",
    urgent: bool = False,
    tg_chat_id: str = "",
    tg_topic_id: int | None = None,
    prefer_tg: bool = False,
    group_record: bool = False,
    debounce: bool = True,
    do_slim: bool = True,
    slug: str = "",
    task_id: str = "",
    record_outbound: bool = True,
    sender_sid: str = "",
    msg_type: str = "",
    route_slug: str = "",
) -> dict[str, Any]:
    """SSOT for paging the human (T-0247 lineage, T-0394 dedupe, T-0610 inversion).

    T-0591 (F5.3) descope: deliberately NOT routed through
    ``channels.get_channel`` — this function IS a primary/reserve failover
    across two DIFFERENT addresses (``tg_chat_id`` vs ``max_default_chat_id``),
    which ``get_channel`` doesn't model (it selects ONE channel per call, not
    a fallback chain). Forcing the highest-criticality paging path through an
    abstraction extension it doesn't have yet is out of scope for a cleanup
    batch; F5.3's other call sites (voice_intake, deploy pause/resume,
    peer_send tg-mirror) are wired instead.

    TG is PRIMARY: the DPI-block premise behind the old MAX-primary logic died
    2026-07-04 (dead proxy removed, direct TG works). MAX is the RESERVE — it
    delivers when TG errors or no TG chat is configured (auto-failover; do NOT
    remove the MAX transport, this host has DPI history), or while the
    stakeholder's temporary 'max' page-mode is set (``page_channel`` action).

    ONE page = ONE delivery (T-0610 DoD): the MAX-ping + TG-group-record pair
    was the duplicate he complained about. ``group_record`` is now a compat
    no-op — the TG-primary delivery already lands in the group/topic the
    record used to go to.

    NOTHING IS EVER TRUNCATED HERE (T-0721, stakeholder override of T-0610's
    slim-page cut): a message too long for one TG send goes out as sequential
    numbered parts via ``_split_page``. This covers every sender kind, page and
    conversational alike — the T-0665 ``bsq tg ping`` exemption is untouched
    (an explicitly-addressed send was never slimmed and still isn't; it is now
    merely split when it exceeds TG's hard 4096-char API cap instead of being
    rejected with an API 400).

    ``do_slim`` no longer slims — it marks a DEFAULT-ROUTED PAGE, the only kind
    that gets the ``_page_detail_link`` pointer appended to its final part when
    it splits. ``do_slim=False`` (explicitly-addressed sends, needs-input
    escalations that already carry their own tmux-attach footer) just means "no
    pointer"; the full text arrives either way.

    ``slug``/``task_id`` (T-0635, both optional) feed ``_page_detail_link`` so
    a split page's pointer is a real staging-web link instead of the bare words
    "см. задачу/тред" — see that function for the fallback chain when a caller
    doesn't have them on hand.

    ``prefer_tg`` (an explicit group/forum target MAX can't honor) stays
    TG-only: no MAX fallback for group-addressed content; TG errors propagate
    to the caller as before.

    ``record_outbound`` (T-0755, default True): the transport now writes every
    delivered message to the outbound log, which is then mirrored into the
    conversation thread. Pass False for the two callers that ALREADY put this
    exact text in that same thread themselves — the API's session-writeback
    relay and ``task_chat``'s lifecycle notice — so the reader gets one line per
    message instead of two near-identical ones. It suppresses the RECORD only,
    never the delivery, and it is deliberately opt-OUT: a send path added later
    is audited by default, which is the failure mode this ticket is about.

    ``sender_sid`` (T-0758): the raw SID of the session that COMPOSED this
    text, forwarded to the transport for the ``[<slug> <role>]`` sender tag.
    Distinct from ``sid`` on purpose — ``sid`` is a display label that ALSO
    drives ``tg_notify``'s destination ladder and the reply map, so a caller
    that knows only the author (the API's session-writeback relay) can state it
    here without also re-routing the message. Absent → the tag falls back to
    ``sid_label``, exactly as every send behaves today.

    ``msg_type`` (T-0799): the automated message TYPE this page is, from
    ``msg_routes.TYPES``. Present ⇒ the per-type destination map may REPLACE
    ``tg_chat_id``/``tg_topic_id`` with whatever the stakeholder configured for
    that type; absent (the default) ⇒ the destination the caller computed is
    used verbatim, exactly as it has been. So the mechanism is a no-op until a
    type is both tagged here and routed by him — and an explicitly-addressed
    send (``bsq tg ping --chat``, ``bsq topic say``, the relay) names no type and
    can never be redirected.

    ``route_slug`` (T-0799): which project's route map to consult, when that is
    NOT the project this page is tagged as belonging to. Distinct from ``slug``
    for the same reason ``sender_sid`` is distinct from ``sid`` — ``slug`` is an
    identity claim that reaches the ``[<slug> <role>]`` sender tag, and the three
    install-wide pagers (``oauth_refresh``, ``autoupdate_apply``,
    ``outbound_liveness``) deliberately claim NO project while still resolving
    their chat from one. They need to say which map to read without also
    claiming to be about that project. Defaults to ``slug``.

    Returns ``{ok, sent, channel}``. ``channel: "none"`` (ok=False) when no
    transport could deliver — logged loudly, never a silent no-op.
    """
    del group_record  # T-0610: compat no-op — one page, one delivery
    # T-0799: the per-message-TYPE destination map. `default_chat_id` keeps the
    # PRE-route chat because `_page_detail_link` reverse-looks-up the project
    # from it (T-0635) — a page routed into a forum supergroup that is no
    # project's `tg_chat` would otherwise lose its detail link as a side effect
    # of being re-routed.
    default_chat_id = tg_chat_id
    if msg_type:
        from bot_squad_worker import msg_routes as _msg_routes
        routed = _msg_routes.route(
            cfg, msg_type, slug=route_slug or slug,
            chat_id=tg_chat_id, topic_id=tg_topic_id,
        )
        tg_chat_id, tg_topic_id = routed.chat_id, routed.topic_id
        if routed.applied and routed.topic_id is not None:
            # A forum thread is a TG-only address — MAX has no notion of one, so
            # failing over would deliver a message he routed to a Logs topic
            # into his MAX DM instead. Same rule `tg_notify` already applies to
            # an explicitly-addressed group send (`explicit_tg_target`).
            prefer_tg = True
    link = (
        _page_detail_link(
            cfg, slug=slug, tg_chat_id=default_chat_id, sid=sid, task_id=task_id,
        )
        if do_slim and not prefer_tg
        else ""
    )
    # One page can be several DELIVERIES when it's long (T-0721) — that is not
    # the T-0610 duplicate (which was the same text twice on two channels).
    parts = _split_page(message, link)

    # T-0644: slug-qualified label for the [<sid>] prefix — falls back to the
    # bare sid when no slug is on hand (see sid_display_label). T-0676 item 5:
    # compact '<slug> <role>' style for this TG-facing SSOT (every tg_notify/
    # tg_ping/topic-say/relay/needs-input page funnels through here), with a
    # T-0662 alias preferred when one is set.
    from bot_squad_worker import sessions as _sessions
    sid_label = (
        _sessions.sid_display_label(sid, slug, compact=True, data_dir=cfg.data_dir)
        if slug else sid
    )

    # T-0719: `sid_label` is DISPLAY only (compact since T-0676 item 5 — no raw
    # SID in it), so hand the send the real routing sid separately for the
    # reply-map. This is the SSOT funnel for every tg_notify/tg_ping/topic-say/
    # relay/needs-input page, so wiring it here covers every sender kind that
    # pages the stakeholder. Passed ONLY for a real routing SID: a synthetic
    # sender (`deploy_monitor`, `oauth_refresh`, …) is not a session, has no
    # pane to inject into, and its replies belong on the attendant path exactly
    # as before — so there is nothing to record and no kwarg to forward.
    from bot_squad_worker import tg_reply_map as _tg_reply_map
    _route: dict[str, Any] = (
        {"route_sid": sid} if _tg_reply_map.is_routing_sid(sid) else {}
    )
    # T-0755: same opt-IN shape as `route_sid` above, and for the same reason —
    # test fakes have fixed `send` signatures, so a kwarg that is absent in the
    # default case keeps every existing call shape byte-identical. Only the two
    # callers that record this text themselves ever pass it.
    _rec: dict[str, Any] = {} if record_outbound else {"record_outbound": False}
    # T-0758: same opt-IN shape a third time. Only forwarded when a caller
    # actually names the composing session, so a fake `send` with a fixed
    # signature — and every send that names nobody — is untouched.
    _sender: dict[str, Any] = {"sender_sid": sender_sid} if sender_sid else {}

    def _deliver(channel: str, send_part: Callable[[str], bool]) -> dict[str, Any]:
        """Send every part in order over one channel (T-0721).

        Failure policy mirrors the single-message case: if the FIRST part
        fails, the exception propagates so the reserve channel gets its chance
        (nothing was delivered yet). Once any part has landed, a later failure
        is logged and the page is reported delivered-but-partial instead —
        failing over mid-page would re-deliver the earlier parts on the other
        channel, which is worse than a gap the log names.
        """
        sent = False
        for n, part in enumerate(parts, 1):
            try:
                sent = send_part(part) or sent
            except Exception:
                if not sent:
                    raise
                log.exception(
                    "_send_stakeholder_dm: %s part %d/%d failed after %d "
                    "delivered (sid=%s) — page is incomplete",
                    channel, n, len(parts), n - 1, sid)
                out = {"ok": True, "sent": True, "channel": channel, "partial": True}
                return _with_delivery(out)
        return _with_delivery({"ok": True, "sent": sent, "channel": channel})

    # T-0761: where Telegram says the page LANDED, not just that it was sent.
    # Collected per part and reported for the FIRST delivered one: every part
    # of one page goes to the same chat and topic, so the first receipt answers
    # "where did this page go" — and taking the first rather than the last
    # means a partial page still reports the destination it reached.
    receipts: list[dict[str, Any]] = []

    def _with_delivery(out: dict[str, Any]) -> dict[str, Any]:
        for r in receipts:
            if r:
                out["delivery"] = r
                break
        return out

    def _try_tg() -> dict[str, Any] | None:
        if not tg_chat_id:
            return None
        # Named `tg_client`, not `client`: the P2-08 guard
        # (test_notify_ssot_guard) tracks TG-bound variable NAMES module-wide,
        # so binding the TG client to a name another function also uses for its
        # MAX client would flag that one as a hidden bare-chat pager.
        tg_client = _get_tg_client(cfg)

        def _send_one(part: str) -> bool:
            # T-0761: a fresh receipt per part. `send` leaves it untouched when
            # the send is suppressed (debounce/quiet hours), so an empty dict
            # here means "nothing left" rather than "landed nowhere" — which is
            # why `_with_delivery` skips falsy receipts instead of reporting the
            # first one blindly.
            receipt: dict[str, Any] = {}
            receipts.append(receipt)
            return tg_client.send(
                chat_id=tg_chat_id, text=part, sid=sid_label, user=user,
                urgent=urgent, topic_id=tg_topic_id, debounce=debounce,
                delivery=receipt, **_route, **_rec, **_sender,
            )

        return _deliver("tg", _send_one)

    def _try_max() -> dict[str, Any] | None:
        max_chat = getattr(cfg, "max_default_chat_id", "") or ""
        if not max_chat:
            return None
        max_client = _get_max_client(cfg)
        return _deliver("max", lambda part: max_client.send(
            chat_id=max_chat, text=part, sid=sid_label, user=user, urgent=urgent,
            recipient_kind=getattr(cfg, "max_recipient_kind", "chat_id"),
            **_rec, **_sender,
        ))

    if prefer_tg:
        out = _try_tg()
        if out is None:
            log.warning(
                "_send_stakeholder_dm: prefer_tg page with no tg_chat_id "
                "dropped (sid=%s)", sid)
            return {"ok": False, "sent": False, "channel": "none"}
        return out

    mode = _get_page_mode(cfg)
    order = (_try_max, _try_tg) if mode == "max" else (_try_tg, _try_max)
    for attempt in order:
        try:
            out = attempt()
        except Exception:  # noqa: BLE001 — reserve channel gets its chance
            log.exception(
                "_send_stakeholder_dm: %s delivery failed — trying reserve",
                attempt.__name__)
            out = None
        if out is not None:
            return out
    log.warning(
        "_send_stakeholder_dm: NO channel delivered (tg_chat_id=%r, mode=%s, "
        "sid=%s) — page dropped", tg_chat_id, mode, sid)
    return {"ok": False, "sent": False, "channel": "none"}


def _coerce_topic_id(raw: Any) -> int | None:
    """Normalise a topic_id param to int|None. Empty/None → None."""
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ActionError(f"tg_notify: topic_id must be an integer, got {raw!r}")


_MAX_NOTIFY_ALLOWED = {
    "chat_id", "message", "sid", "user", "urgent", "recipient_kind",
}


def _action_max_notify(params: dict[str, Any]) -> dict[str, Any]:
    """Send a MAX (max.ru) DM — the T-0247 mirror of ``tg_notify``.

    The stakeholder's preferred channel. MAX has no per-project chat binding
    (projects.toml carries ``tg_chat``, not a MAX id), so the recipient is the
    explicit ``chat_id`` param when given, else the ``[max].default_chat_id``
    from system_settings.toml (the stakeholder's MAX id).

    Params (all optional except ``message``):
        message        : str  — required; the text to send
        chat_id        : str  — explicit MAX recipient id; wins over the default
        sid            : str  — SID prefix component
        user           : str  — user prefix component
        urgent         : bool — bypass the quiet-hours gate
        recipient_kind : str  — "chat_id" (default) or "user_id" for a DM;
                                overrides [max].recipient_kind for this call

    Returns {ok: true, sent: <bool>}.
    """
    extra = set(params) - _MAX_NOTIFY_ALLOWED
    if extra:
        raise ActionError(f"max_notify got unexpected params: {sorted(extra)}")
    if "message" not in params:
        raise ActionError("max_notify missing required param: message")

    cfg = _get_config()
    chat_id = params.get("chat_id") or cfg.max_default_chat_id
    if not chat_id:
        raise ActionError(
            "max_notify: no chat_id and no [max].default_chat_id configured"
        )

    client = _get_max_client(cfg)
    sent = client.send(
        chat_id=chat_id,
        text=params["message"],
        sid=params.get("sid", ""),
        user=params.get("user", ""),
        urgent=bool(params.get("urgent", False)),
        recipient_kind=params.get("recipient_kind"),
    )
    return {"ok": True, "sent": sent}


_PAGE_CHANNEL_ALLOWED = {"mode", "by"}


def _action_page_channel(params: dict[str, Any]) -> dict[str, Any]:
    """Read or set the page-channel mode (T-0610 temporary switch).

    The stakeholder can only issue the switch from the TG thread ("я должен
    просто иметь возможность сказать типа, давай сейчас переключим временно на
    Max") — the attendant translates that into this action.

    Params: {} reads the current mode; {"mode": "tg"|"max"|"auto"} sets it
    ("auto" = revert to the TG-primary default; optional "by" stamps who
    switched). The mode persists across worker restarts
    (data/_worker/page_channel.json) and is consulted by the
    ``_send_stakeholder_dm`` SSOT on every page.
    """
    extra = set(params) - _PAGE_CHANNEL_ALLOWED
    if extra:
        raise ActionError(f"page_channel got unexpected params: {sorted(extra)}")
    cfg = _get_config()
    mode = str(params.get("mode", "") or "")
    if not mode:
        return {"ok": True, "mode": _get_page_mode(cfg)}
    if mode not in _PAGE_MODES:
        raise ActionError(
            f"page_channel: mode must be one of {list(_PAGE_MODES)}, got {mode!r}")
    payload = _set_page_mode(cfg, mode, by=str(params.get("by", "")))
    log.info("page_channel: mode set to %s (by=%s)", mode, payload["set_by"] or "?")
    return {"ok": True, **payload}


# ---------------------------------------------------------------------------
# T-0386 / INI-04 Phase 1: per-project forum-topic provisioning + GC.
# The supergroup itself is a 1-time MANUAL setup (the Bot API cannot create
# groups); the bot owns the TOPICS inside it — created on project-create
# (provision_project_topics, wired at routes_projects.py).
#
# NOTE (next-wave #15, T-0450): the GC half (gc_project_topics) is a DORMANT
# primitive, NOT an active closed loop. There is no project archive/delete
# route and nothing in production calls it, so "topics closed on archive"
# cannot happen today — it awaits a project-archive concept (directional fork
# in the T-0438 ranked backlog). Until then gc_project_topics is dead-but-ready
# and deliberately NOT advertised as a loop that closes.
# ---------------------------------------------------------------------------

_TOPIC_PROVISION_ALLOWED = {"slug"}


def _resolve_topic_project(params: dict[str, Any], action: str):
    """Shared guard: require slug, a known project, and a configured supergroup."""
    if set(params) - _TOPIC_PROVISION_ALLOWED:
        raise ActionError(f"{action} got unexpected params: {sorted(set(params) - _TOPIC_PROVISION_ALLOWED)}")
    slug = params.get("slug") or ""
    if not slug:
        raise ActionError(f"{action} missing required param: slug")
    cfg = _get_config()
    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"{action}: unknown project slug {slug!r}")
    if not getattr(project, "tg_chat", ""):
        raise ActionError(f"{action}: project {slug!r} has no tg_chat supergroup configured")
    return cfg, slug, project


def _action_provision_project_topics(params: dict[str, Any]) -> dict[str, Any]:
    """Create the standard forum topics for a project's supergroup (idempotent).

    For each class in ``tg_topics.STANDARD_TOPICS`` not already provisioned,
    calls ``createForumTopic`` in ``project.tg_chat`` and persists the returned
    thread-id. Re-running creates only the missing ones. Best-effort per-topic
    (matching gc): a single create failure is logged + skipped, never fatal.
    Returns ``{ok, topics: {class: thread_id}, created: [...], failed: [...]}``.
    """
    from bot_squad_worker import tg_topics
    cfg, slug, project = _resolve_topic_project(params, "provision_project_topics")
    existing = tg_topics.load(cfg, slug)
    tg = _get_tg_client(cfg)
    created: list[str] = []
    failed: list[str] = []
    # T-0442: per-topic try + INCREMENTAL save (parity with _action_gc_project_topics).
    # Saving once AFTER the loop meant a mid-loop createForumTopic failure propagated
    # and LOST every thread-id already created this run — so a retry re-created them,
    # orphaning + duplicating the TG threads. Now a partial failure persists what
    # succeeded; the retry only creates the still-missing classes.
    for cls, title in tg_topics.STANDARD_TOPICS.items():
        if cls in existing:
            continue
        try:
            existing[cls] = tg.create_forum_topic(chat_id=project.tg_chat, name=title)
        except Exception:
            log.exception("provision_project_topics: failed to create %s topic (slug=%s)", cls, slug)
            failed.append(cls)
            continue
        created.append(cls)
        tg_topics.save(cfg, slug, existing)
    return {"ok": True, "topics": existing, "created": created, "failed": failed}


def _action_gc_project_topics(params: dict[str, Any]) -> dict[str, Any]:
    """Close every provisioned forum topic for a project.

    DORMANT primitive (next-wave #15, T-0450): intended as the archive-time GC
    for a project's topics, but no project archive/delete route exists and
    nothing in production calls this today — so it is NOT a live closed loop,
    just a ready action awaiting a project-archive concept. Wire it the moment
    archival lands; until then it is dead-but-ready, not a "no orphan topics"
    guarantee.

    Best-effort: a per-topic close failure is logged, not fatal, so the GC
    always makes progress. Returns ``{ok, closed: [class, …]}``.
    """
    from bot_squad_worker import tg_topics
    cfg, slug, project = _resolve_topic_project(params, "gc_project_topics")
    existing = tg_topics.load(cfg, slug)
    tg = _get_tg_client(cfg)
    closed: list[str] = []
    for cls, tid in existing.items():
        try:
            tg.close_forum_topic(chat_id=project.tg_chat, thread_id=tid)
            closed.append(cls)
        except Exception:
            log.exception("gc_project_topics: failed to close %s topic %s", cls, tid)
    return {"ok": True, "closed": closed}


_TG_STALL_CLEAR_REQUIRED = {"slug", "sid"}
_TG_STALL_CLEAR_ALLOWED = _TG_STALL_CLEAR_REQUIRED


def _action_tg_stall_clear(params: dict[str, Any]) -> dict[str, Any]:
    """T-0155: clear a session's stall marker (the agent is unblocked).

    Fired by the UserPromptSubmit hook: when a prompt is submitted into a
    pane, whoever was blocked there just got input (typically the stakeholder
    replying in tmux), so any pending TG escalation must be cancelled.

    Required params: slug, sid. Returns {ok, cleared: bool}.
    """
    extra = set(params) - _TG_STALL_CLEAR_ALLOWED
    if extra:
        raise ActionError(f"tg_stall_clear got unexpected params: {sorted(extra)}")
    missing = _TG_STALL_CLEAR_REQUIRED - set(params)
    if missing:
        raise ActionError(f"tg_stall_clear missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import tg_stall as _tg_stall
    cleared = _tg_stall.clear_blocked(cfg, params["slug"], params["sid"])
    return {"ok": True, "cleared": cleared}


# ---------------------------------------------------------------------------
# T-0639: runtime (chat_id, message_thread_id) -> project topic-binding
# surface. The stakeholder's explicit requirement: "не HardCotion, настраиваемо
# через чат ID" — a topic->project binding must be set/cleared at RUNTIME, not
# baked into code. `bsq topic bind/unbind/list` (scripts/cli/bsq) is the thin
# CLI surface over these actions; the worker-owned store lives in
# tg_bindings.py (single-writer JSON, mirrors tg_topics.py/pins_store).
# ---------------------------------------------------------------------------

_TG_TOPIC_BIND_REQUIRED = {"chat_id", "thread_id", "slug"}
_TG_TOPIC_BIND_ALLOWED = _TG_TOPIC_BIND_REQUIRED | {
    "ticket_id", "session_id", "clear_ticket_id", "clear_session_id",
}

#: How the two guarded fields are named on each surface, so a refusal can tell
#: the caller what to type wherever it is standing (T-0771).
_TG_BIND_FLAG = {
    "ticket_id": ("ticket_id", "--ticket", "clear_ticket_id", "--clear-ticket"),
    "session_id": ("session_id", "--session", "clear_session_id", "--clear-session"),
}


def _action_tg_topic_bind(params: dict[str, Any]) -> dict[str, Any]:
    """Bind ``(chat_id, thread_id)`` -> ``slug`` (T-0639 topic-supergroup
    routing). Idempotent — rebinding the same key rewrites it; a project may
    have multiple bound keys (a project isn't 1:1 with chat/topic ids).

    Required params: chat_id, thread_id, slug. ``thread_id`` may be ``None``
    (binds the chat's non-topic/General feed).

    Optional (T-0771): ``ticket_id``/``session_id`` — the per-task-topic fields
    ``tg_topic_create`` writes and this verb could not, which is what made
    every rebind of a task topic lossy AND left no supported way to restore the
    association short of creating a NEW topic. ``clear_ticket_id``/
    ``clear_session_id`` — the explicit opt-in to EMPTY one of them (clearing
    ``session_id`` to demote a direct-mode topic back to the attendant is a
    legitimate operation and must stay expressible).

    A rebind that would implicitly drop a ticket_id/session_id the key
    currently carries is REFUSED, naming the fields and their stored values —
    the store has no backup, so a dropped field cannot be recovered or even
    detected afterwards.

    Returns {ok, binding, changes, cleared} — ``changes`` is
    ``{field: {from, to}}`` for every field this write actually moved (empty
    when it moved none), because a lossy write and a faithful one produce
    records that look equally well-formed.
    """
    extra = set(params) - _TG_TOPIC_BIND_ALLOWED
    if extra:
        raise ActionError(f"tg_topic_bind got unexpected params: {sorted(extra)}")
    missing = _TG_TOPIC_BIND_REQUIRED - set(params)
    if missing:
        raise ActionError(f"tg_topic_bind missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"tg_topic_bind: unknown project slug {slug!r}")

    from bot_squad_worker import tg_bindings
    chat_id, thread_id = params["chat_id"], params["thread_id"]
    before = tg_bindings.resolve(cfg, chat_id, thread_id)
    try:
        rec = tg_bindings.set_binding(
            cfg, chat_id, thread_id, slug,
            ticket_id=params.get("ticket_id"),
            session_id=params.get("session_id"),
            clear_ticket_id=bool(params.get("clear_ticket_id")),
            clear_session_id=bool(params.get("clear_session_id")),
        )
    except tg_bindings.LossyRebindError as e:
        raise ActionError(
            f"tg_topic_bind: {e}. To KEEP a field, name it "
            f"({'; '.join(f'{_TG_BIND_FLAG[f][0]}={v!r} / {_TG_BIND_FLAG[f][1]} {v}' for f, v in e.fields.items())}). "
            f"To DROP it on purpose, say so "
            f"({'; '.join(f'{_TG_BIND_FLAG[f][2]}=true / {_TG_BIND_FLAG[f][3]}' for f in e.fields)}). "
            f"Nothing was written (T-0771)"
        ) from e
    except ValueError as e:
        raise ActionError(f"tg_topic_bind: {e}") from e

    return {
        "ok": True,
        "binding": rec,
        "changes": tg_bindings.change_summary(before, rec),
        # What the CALLER asked to empty — kept separate from `changes` on
        # purpose: clearing a field that was already empty changes nothing, and
        # the caller is still owed a straight answer about what it asked for.
        "cleared": [f for f in tg_bindings.GUARDED_FIELDS
                    if params.get(f"clear_{f}")],
    }


_TG_TOPIC_UNBIND_REQUIRED = {"chat_id", "thread_id"}
_TG_TOPIC_UNBIND_ALLOWED = _TG_TOPIC_UNBIND_REQUIRED


def _action_tg_topic_unbind(params: dict[str, Any]) -> dict[str, Any]:
    """Clear the ``(chat_id, thread_id)`` binding (T-0639). Idempotent.

    Required params: chat_id, thread_id. Returns {ok, cleared: bool}.
    """
    extra = set(params) - _TG_TOPIC_UNBIND_ALLOWED
    if extra:
        raise ActionError(f"tg_topic_unbind got unexpected params: {sorted(extra)}")
    missing = _TG_TOPIC_UNBIND_REQUIRED - set(params)
    if missing:
        raise ActionError(f"tg_topic_unbind missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import tg_bindings
    cleared = tg_bindings.clear_binding(cfg, params["chat_id"], params["thread_id"])
    return {"ok": True, "cleared": cleared}


def _action_tg_topic_list(params: dict[str, Any]) -> dict[str, Any]:
    """List every current ``(chat_id, thread_id)`` -> binding (T-0639).
    Read-only, no params. Returns {ok, bindings: {key: {slug, ticket_id,
    session_id}}}.
    """
    extra = set(params)
    if extra:
        raise ActionError(f"tg_topic_list got unexpected params: {sorted(extra)}")
    from bot_squad_worker import tg_bindings
    return {"ok": True, "bindings": tg_bindings.load(_get_config())}


# ---------------------------------------------------------------------------
# msg_routes.py (T-0799: per-message-TYPE destination map — «конфигурируемые
# chat_id все типы сообщений, но по дефолту всё ЛС»). Per-project, worker-owned
# single-writer JSON, mirroring the tg_topics/tg_bindings ops above.
# ---------------------------------------------------------------------------

_MSG_ROUTE_LIST_ALLOWED = {"slug"}


def _action_msg_route_list(params: dict[str, Any]) -> dict[str, Any]:
    """Every automated message type for ``slug``, with where it goes today
    (T-0799). Read-only.

    Required params: slug. Returns ``{ok, slug, rows, keys}`` — ``rows`` is one
    entry per registered type carrying its urgency class, its human summary and
    the ROUTE KEY that decides its destination (``"default"`` when none), and
    ``keys`` is every routable key so a caller can offer them without a second
    copy of the list.

    Reports the resolved key per type rather than the raw store on purpose: one
    ``class:log`` entry silently governs five types, and reading the file cannot
    tell you which.
    """
    extra = set(params) - _MSG_ROUTE_LIST_ALLOWED
    if extra:
        raise ActionError(f"msg_route_list got unexpected params: {sorted(extra)}")
    if not params.get("slug"):
        raise ActionError("msg_route_list missing required param: slug")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"msg_route_list: unknown project slug {slug!r}")
    from bot_squad_worker import msg_routes as _msg_routes
    return {
        "ok": True,
        "slug": slug,
        "rows": _msg_routes.describe(cfg, slug),
        "keys": _msg_routes.keys(),
    }


_MSG_ROUTE_SET_REQUIRED = {"slug", "msg_type"}
_MSG_ROUTE_SET_ALLOWED = _MSG_ROUTE_SET_REQUIRED | {"chat_id", "topic_id"}


def _action_msg_route_set(params: dict[str, Any]) -> dict[str, Any]:
    """Point one message type (or a whole urgency class) at a chat/topic
    (T-0799).

    Required params: slug, msg_type — a key from ``msg_route_list``'s ``keys``,
    i.e. a registered type or ``class:urgent`` / ``class:log``.
    Optional params: chat_id, topic_id. At least one must be given. ``chat_id``
    empty = "the project's own chat"; ``topic_id`` null = "no forum thread".
    Both are real destinations, so PARAM PRESENCE (not the value) is what says
    the caller named a field — a call that leaves a field unnamed while the
    stored route carries a value for it is REFUSED, naming both values, rather
    than silently dropping it (the T-0771 rule: re-pointing a type's chat while
    forgetting its topic would move it to the new chat's General feed and look
    exactly like a successful re-point).

    Returns {ok, slug, msg_type, route, changes} — ``changes`` is
    ``{field: {from, to}}`` for every field this write actually moved, because
    the record alone cannot say what it used to be.
    """
    extra = set(params) - _MSG_ROUTE_SET_ALLOWED
    if extra:
        raise ActionError(f"msg_route_set got unexpected params: {sorted(extra)}")
    missing = _MSG_ROUTE_SET_REQUIRED - set(params)
    if missing:
        raise ActionError(f"msg_route_set missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"msg_route_set: unknown project slug {slug!r}")

    from bot_squad_worker import msg_routes as _msg_routes
    key = str(params["msg_type"])
    before = _msg_routes.load(cfg, slug).get(key)
    kwargs: dict[str, Any] = {}
    if "chat_id" in params:
        kwargs["chat_id"] = params["chat_id"]
    if "topic_id" in params:
        kwargs["topic_id"] = params["topic_id"]
    try:
        rec = _msg_routes.set_route(cfg, slug, key, **kwargs)
    except _msg_routes.LossyRouteError as e:
        raise ActionError(
            f"msg_route_set: {e}. To KEEP a field, name it "
            f"({'; '.join(f'{f}={v!r}' for f, v in e.fields.items())}). To EMPTY "
            f"it on purpose, pass it explicitly (chat_id='' = the project's own "
            f"chat, topic_id=null = no thread). Nothing was written"
        ) from e
    except ValueError as e:
        raise ActionError(f"msg_route_set: {e}") from e

    return {
        "ok": True,
        "slug": slug,
        "msg_type": key,
        "route": rec,
        "changes": _msg_routes.change_summary(before, rec),
    }


_MSG_ROUTE_CLEAR_REQUIRED = {"slug", "msg_type"}
_MSG_ROUTE_CLEAR_ALLOWED = _MSG_ROUTE_CLEAR_REQUIRED


def _action_msg_route_clear(params: dict[str, Any]) -> dict[str, Any]:
    """Drop one route, restoring that type's default destination (T-0799).

    Required params: slug, msg_type. Idempotent. Returns {ok, cleared: bool}.
    """
    extra = set(params) - _MSG_ROUTE_CLEAR_ALLOWED
    if extra:
        raise ActionError(f"msg_route_clear got unexpected params: {sorted(extra)}")
    missing = _MSG_ROUTE_CLEAR_REQUIRED - set(params)
    if missing:
        raise ActionError(f"msg_route_clear missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"msg_route_clear: unknown project slug {slug!r}")
    from bot_squad_worker import msg_routes as _msg_routes
    cleared = _msg_routes.clear_route(cfg, slug, str(params["msg_type"]))
    return {"ok": True, "cleared": cleared}


# ---------------------------------------------------------------------------
# session_aliases.py (T-0662: human-readable label -> session SID aliases,
# single-writer global JSON, mirrors tg_bindings.py above).
# ---------------------------------------------------------------------------

_SESSION_ALIAS_SET_REQUIRED = {"label", "sid"}
_SESSION_ALIAS_SET_ALLOWED = _SESSION_ALIAS_SET_REQUIRED


def _action_session_alias_set(params: dict[str, Any]) -> dict[str, Any]:
    """Point a human-readable label at a session SID (T-0662).

    Required params: label, sid. Idempotent — rebinding an existing label
    repoints it (one label -> exactly one sid; multiple labels may point at
    the same sid). Returns {ok, label, sid, previous_sid} (previous_sid is
    None when the label was previously unset).
    """
    extra = set(params) - _SESSION_ALIAS_SET_ALLOWED
    if extra:
        raise ActionError(f"session_alias_set got unexpected params: {sorted(extra)}")
    missing = _SESSION_ALIAS_SET_REQUIRED - set(params)
    if missing:
        raise ActionError(f"session_alias_set missing required params: {sorted(missing)}")

    from bot_squad_worker import session_aliases
    cfg = _get_config()
    previous = session_aliases.resolve_alias(cfg.data_dir, params["label"])
    try:
        norm = session_aliases.set_alias(cfg.data_dir, params["label"], params["sid"])
    except session_aliases.InvalidLabelError as e:
        raise ActionError(f"session_alias_set: {e}") from e
    return {"ok": True, "label": norm, "sid": params["sid"], "previous_sid": previous}


_SESSION_ALIAS_REMOVE_REQUIRED = {"label"}
_SESSION_ALIAS_REMOVE_ALLOWED = _SESSION_ALIAS_REMOVE_REQUIRED


def _action_session_alias_remove(params: dict[str, Any]) -> dict[str, Any]:
    """Remove a session label (T-0662). Required params: label. Idempotent —
    removing an unknown label is not an error. Returns {ok, removed: bool}."""
    extra = set(params) - _SESSION_ALIAS_REMOVE_ALLOWED
    if extra:
        raise ActionError(f"session_alias_remove got unexpected params: {sorted(extra)}")
    missing = _SESSION_ALIAS_REMOVE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"session_alias_remove missing required params: {sorted(missing)}")

    from bot_squad_worker import session_aliases
    cfg = _get_config()
    removed = session_aliases.remove_alias(cfg.data_dir, params["label"])
    return {"ok": True, "removed": removed}


_SESSION_ALIAS_RESOLVE_REQUIRED = {"label"}
_SESSION_ALIAS_RESOLVE_ALLOWED = _SESSION_ALIAS_RESOLVE_REQUIRED


def _action_session_alias_resolve(params: dict[str, Any]) -> dict[str, Any]:
    """label -> sid lookup (T-0662). Required params: label. Returns {ok,
    sid} — sid is None when the label is unknown (this is the exact shape a
    future to-session <label> control-phrase resolver, T-0660 Addendum 1,
    will call)."""
    extra = set(params) - _SESSION_ALIAS_RESOLVE_ALLOWED
    if extra:
        raise ActionError(f"session_alias_resolve got unexpected params: {sorted(extra)}")
    missing = _SESSION_ALIAS_RESOLVE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"session_alias_resolve missing required params: {sorted(missing)}")

    from bot_squad_worker import session_aliases
    cfg = _get_config()
    return {"ok": True, "sid": session_aliases.resolve_alias(cfg.data_dir, params["label"])}


def _action_session_alias_list(params: dict[str, Any]) -> dict[str, Any]:
    """List every label -> sid mapping (T-0662). No params. Returns {ok,
    aliases: {label: sid}}."""
    extra = set(params)
    if extra:
        raise ActionError(f"session_alias_list got unexpected params: {sorted(extra)}")
    from bot_squad_worker import session_aliases
    cfg = _get_config()
    return {"ok": True, "aliases": session_aliases.load_aliases(cfg.data_dir)}


def _tg_call(fn, *, action: str):
    """Call a TgClient forum-topic method, re-raising a documented Bot API
    failure (tg.py's ``_call`` puts the API's own ``description`` — e.g.
    "CHAT_ADMIN_REQUIRED" — into the message) or a transport-level
    ``httpx.HTTPStatusError`` as ``ActionError`` with that reason intact.
    Without this, either bubbles up as an opaque, undiagnosable 500 at the
    action layer (T-0660 field note, TL p23 — a real can_manage_topics
    permission failure took manual digging to identify)."""
    import httpx
    try:
        return fn()
    except (RuntimeError, httpx.HTTPStatusError) as e:
        raise ActionError(f"{action}: {e}") from e


_TG_TOPIC_CREATE_REQUIRED = {"chat_id", "slug"}
_TG_TOPIC_CREATE_ALLOWED = (
    _TG_TOPIC_CREATE_REQUIRED | {"name", "ticket_id", "session_id", "caller_sid"}
)

# T-0669: a topic name that IS a raw routing SID (optionally wrapped in the
# '[<slug>] ' bracket sid_display_label itself produces) — the exact shape of
# the bug: a creating session named a topic with its own sid_display_label
# instead of a real title. Mirrors session_aliases._validate_label's
# SID-shape guard (that one just checks a `s-` prefix on a short label; a
# topic name is a longer free-text string so this anchors on the full
# `S-<user>-<window>-p<pane>` shape to avoid false positives on a title that
# merely starts with those letters).
_SID_NAME_RE = re.compile(r"^s-[a-z0-9_-]+-p\d+$", re.IGNORECASE)


def _looks_like_sid_name(name: str) -> bool:
    candidate = name.strip()
    if candidate.startswith("["):
        _, _, rest = candidate.partition("]")
        if rest.strip():
            candidate = rest.strip()
    return bool(_SID_NAME_RE.match(candidate))


def _looks_like_own_compact_label(
    name: str, caller_sid: str | None, slug: str, cfg: Any,
) -> bool:
    """T-0701: catch T-0669's regression under T-0676 item 5's NEW compact
    label shape (``"<slug> <role>"``, e.g. ``"watchrobot operator"``) —
    ``_looks_like_sid_name``'s ``_SID_NAME_RE`` only recognises the OLD
    bracket+raw-SID shape and no longer matches what a TG-facing session's
    own label actually looks like post-T-0676.

    Design call (see T-0701 "why not fixed inline"): thread the CALLING
    session's own sid through as an explicit ``caller_sid`` param rather than
    a generic "looks like `<slug> <role>`" regex — a generic heuristic would
    false-positive on any short legitimate topic title that happens to start
    with the project slug (e.g. a project literally named its General topic
    "watchrobot standup"). Threading the caller's own sid means this only
    ever rejects a name that is a LITERAL match of what ``caller_sid``'s own
    label renders as right now (both the compact and bracket forms — a
    caller could be running either), never a lookalike. No ``caller_sid`` ⇒
    no check here (falls back to the shape-only ``_looks_like_sid_name``
    guard above), so old/other callers that don't pass it are unaffected.
    """
    if not caller_sid:
        return False
    from bot_squad_worker import sessions as _sessions
    candidate = name.strip().casefold()
    own_compact = _sessions.sid_display_label(
        caller_sid, slug, compact=True, data_dir=cfg.data_dir
    )
    own_bracket = _sessions.sid_display_label(caller_sid, slug, compact=False)
    return candidate in (own_compact.strip().casefold(), own_bracket.strip().casefold())


def _derive_task_topic_name(cfg: Any, slug: str, ticket_id: str) -> str:
    """T-0669 root-cause fix: derive a task-topic's name from the TICKET,
    never the caller — ``[<slug>] <ticket title>``, matching D-0055's
    documented task-topic naming convention. Raises ActionError if the
    ticket can't be resolved so a topic is never created under a name nobody
    asked for.

    T-0680 (stakeholder: rename ``[watchrobot]`` -> ``WR``, ``[bot-squad]``
    -> ``BS``): the bracket carries the project's ``topic_abbrev`` when one
    is configured, else the full slug unchanged.
    """
    from bot_squad_worker import frontmatter as fm
    backlog_dir = Path(cfg.data_dir) / slug / "backlog"
    path = fm.resolve_id_file(backlog_dir, ticket_id)
    if path is None:
        raise ActionError(
            f"tg_topic_create: ticket {ticket_id!r} not found under {slug!r} backlog"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ActionError(f"tg_topic_create: could not read ticket {ticket_id!r}: {e}") from e
    parsed = fm.parse_or_none(text)
    if parsed is None:
        raise ActionError(f"tg_topic_create: ticket {ticket_id!r} md has no valid frontmatter")
    title = str(parsed[0].get("title") or "").strip()
    if not title:
        raise ActionError(f"tg_topic_create: ticket {ticket_id!r} has no title in frontmatter")
    project = cfg.projects.get(slug)
    prefix = (project.topic_abbrev if project and project.topic_abbrev else slug)
    return f"[{prefix}] {title}"


def _action_tg_topic_create(params: dict[str, Any]) -> dict[str, Any]:
    """T-0660: create a forum topic AND bind it to a project in one step —
    ``createForumTopic`` then ``tg_bindings.set_binding`` — so a caller never
    ends up with a TG topic that exists but isn't routed anywhere.

    Required: chat_id, slug. Optional: ticket_id (T-0660 per-task topics —
    binds ``{slug, ticket_id}`` instead of just ``{slug}``); session_id
    (Phase 2 — the ORIGINATING session working the task, so an inbound
    message in the new topic routes straight to it instead of the project's
    user-conversation attendant, see ``tg_listener._handle_topic_bound``);
    caller_sid (T-0701 — the CALLING session's own routing sid, used ONLY to
    detect it naming the topic after itself; distinct from session_id, which
    may name a different originating session).

    ``name``: for a per-task topic (``ticket_id`` given), the name is ALWAYS
    DERIVED from the ticket's own title (T-0669 — a caller-supplied ``name``
    is accepted but ignored, so an old caller isn't broken by the param
    becoming non-required). For a project-level topic (no ``ticket_id``),
    ``name`` is required and is rejected if it looks like a raw session SID
    (T-0669: the exact phantom-topic bug — a session named a topic with its
    own sid_display_label instead of a real title) or, when ``caller_sid`` is
    given, if it matches that session's own display label in either the
    bracket or compact form (T-0701 — the same mistake under T-0676 item 5's
    newer compact label shape, which doesn't match the SID-shape regex).

    Returns ``{ok, chat_id, thread_id, slug, name}``.
    """
    extra = set(params) - _TG_TOPIC_CREATE_ALLOWED
    if extra:
        raise ActionError(f"tg_topic_create got unexpected params: {sorted(extra)}")
    missing = _TG_TOPIC_CREATE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"tg_topic_create missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"tg_topic_create: unknown project slug {slug!r}")

    ticket_id = params.get("ticket_id") or ""
    if ticket_id:
        name = _derive_task_topic_name(cfg, slug, ticket_id)
    else:
        caller_name = str(params.get("name") or "").strip()
        if not caller_name:
            raise ActionError(
                "tg_topic_create missing required params: ['name'] "
                "(required unless ticket_id is given)"
            )
        if _looks_like_sid_name(caller_name):
            raise ActionError(
                f"tg_topic_create: name {caller_name!r} looks like a raw session "
                "SID, not a topic title (T-0669) — pass a real name"
            )
        if _looks_like_own_compact_label(caller_name, params.get("caller_sid"), slug, cfg):
            raise ActionError(
                f"tg_topic_create: name {caller_name!r} looks like the calling "
                "session's own display label, not a topic title (T-0701) — "
                "pass a real name"
            )
        name = caller_name

    tg = _get_tg_client(cfg)
    thread_id = _tg_call(
        lambda: tg.create_forum_topic(chat_id=params["chat_id"], name=name),
        action="tg_topic_create",
    )

    from bot_squad_worker import tg_bindings
    try:
        tg_bindings.set_binding(
            cfg, params["chat_id"], thread_id, slug,
            ticket_id=params.get("ticket_id"), session_id=params.get("session_id"),
        )
    except tg_bindings.LossyRebindError as e:
        # T-0771: unreachable in practice — a freshly created forum topic has an
        # id nothing is bound to. If it ever IS reached the map is stale for
        # that key, and overwriting is exactly the silent strip this guard
        # exists to stop; report the created topic so the bind can be finished
        # by hand rather than leave a 500 and an unexplained topic.
        raise ActionError(
            f"tg_topic_create: created topic thread_id={thread_id} in "
            f"chat_id={params['chat_id']}, but {e} — the topic EXISTS and is "
            f"NOT bound; finish it with `bsq topic bind` naming the fields you "
            f"mean to keep or clear (T-0771)"
        ) from e
    return {
        "ok": True, "chat_id": params["chat_id"], "thread_id": thread_id,
        "slug": slug, "name": name,
    }


_TG_TOPIC_RENAME_GENERAL_REQUIRED = {"chat_id", "name"}
_TG_TOPIC_RENAME_GENERAL_ALLOWED = _TG_TOPIC_RENAME_GENERAL_REQUIRED


def _action_tg_topic_rename_general(params: dict[str, Any]) -> dict[str, Any]:
    """T-0660: rename a forum's General topic (``editGeneralForumTopic``).

    General has no ``message_thread_id`` of its own (unlike a created topic),
    so this is a separate Bot API call from create/close — it doesn't touch
    the binding store (General's binding, if any, is set separately via
    ``tg_topic_bind`` with ``thread_id=None``).

    Required: chat_id, name. Returns ``{ok, chat_id, name}``.
    """
    extra = set(params) - _TG_TOPIC_RENAME_GENERAL_ALLOWED
    if extra:
        raise ActionError(f"tg_topic_rename_general got unexpected params: {sorted(extra)}")
    missing = _TG_TOPIC_RENAME_GENERAL_REQUIRED - set(params)
    if missing:
        raise ActionError(f"tg_topic_rename_general missing required params: {sorted(missing)}")

    cfg = _get_config()
    tg = _get_tg_client(cfg)
    _tg_call(
        lambda: tg.rename_general_forum_topic(chat_id=params["chat_id"], name=params["name"]),
        action="tg_topic_rename_general",
    )
    return {"ok": True, "chat_id": params["chat_id"], "name": params["name"]}


_TG_TOPIC_RENAME_REQUIRED = {"chat_id", "thread_id", "name"}
_TG_TOPIC_RENAME_ALLOWED = _TG_TOPIC_RENAME_REQUIRED


def _action_tg_topic_rename(params: dict[str, Any]) -> dict[str, Any]:
    """T-0669/T-0676 item 1: rename a REGULAR (non-General) forum topic
    (``editForumTopic``) — the ``tg_topic_rename_general`` counterpart for a
    topic that has its own ``message_thread_id``. Lets a bad/SID-named topic
    (the T-0669 phantom-SID bug) be relabeled without recreating it; doesn't
    touch the binding store — the (chat_id, thread_id) -> slug/ticket routing
    is unaffected by a display-name change.

    Required: chat_id, thread_id, name. Returns ``{ok, chat_id, thread_id, name}``.
    """
    extra = set(params) - _TG_TOPIC_RENAME_ALLOWED
    if extra:
        raise ActionError(f"tg_topic_rename got unexpected params: {sorted(extra)}")
    missing = _TG_TOPIC_RENAME_REQUIRED - set(params)
    if missing:
        raise ActionError(f"tg_topic_rename missing required params: {sorted(missing)}")

    cfg = _get_config()
    tg = _get_tg_client(cfg)
    try:
        thread_id = int(params["thread_id"])
    except (TypeError, ValueError):
        raise ActionError(f"tg_topic_rename: thread_id must be an integer, got {params['thread_id']!r}")
    _tg_call(
        lambda: tg.edit_forum_topic(
            chat_id=params["chat_id"], thread_id=thread_id, name=params["name"],
        ),
        action="tg_topic_rename",
    )
    return {"ok": True, "chat_id": params["chat_id"], "thread_id": thread_id, "name": params["name"]}


_TG_TOPIC_CLOSE_FOR_TICKET_REQUIRED = {"ticket_id"}
_TG_TOPIC_CLOSE_FOR_TICKET_ALLOWED = _TG_TOPIC_CLOSE_FOR_TICKET_REQUIRED


def _action_tg_topic_close_for_ticket(params: dict[str, Any]) -> dict[str, Any]:
    """T-0660: close a ticket's dedicated forum topic (if it has one) when the
    ticket reaches its terminal status. A no-op, not an error, when no topic
    is bound to this ticket — most tasks stay in the project's General room
    (T-0660 Addendum 2: a dedicated topic is opt-in, not automatic), so "no
    topic to close" is the common case, called from ``bsq ticket update
    <id> closed``.

    Clears the binding too (``tg_bindings.clear_binding``) — an inbound
    message can't land in a topic that no longer accepts them, so a stale
    binding routing into a closed topic would be a dead end.

    Required: ticket_id. Returns ``{ok, closed: bool, chat_id?, thread_id?}``.
    """
    extra = set(params) - _TG_TOPIC_CLOSE_FOR_TICKET_ALLOWED
    if extra:
        raise ActionError(f"tg_topic_close_for_ticket got unexpected params: {sorted(extra)}")
    missing = _TG_TOPIC_CLOSE_FOR_TICKET_REQUIRED - set(params)
    if missing:
        raise ActionError(f"tg_topic_close_for_ticket missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import tg_bindings
    binding = tg_bindings.find_by_ticket(cfg, params["ticket_id"])
    if binding is None or binding.get("thread_id") is None:
        # No dedicated topic (or, degenerately, a ticket "bound" to a chat's
        # General feed — closeForumTopic doesn't apply to General at all).
        return {"ok": True, "closed": False}

    tg = _get_tg_client(cfg)
    _tg_call(
        lambda: tg.close_forum_topic(chat_id=binding["chat_id"], thread_id=binding["thread_id"]),
        action="tg_topic_close_for_ticket",
    )
    tg_bindings.clear_binding(cfg, binding["chat_id"], binding["thread_id"])
    return {
        "ok": True, "closed": True,
        "chat_id": binding["chat_id"], "thread_id": binding["thread_id"],
    }


_CLONE_STATUS_REQUIRED = {"slug"}
_CLONE_STATUS_ALLOWED = _CLONE_STATUS_REQUIRED


def _action_clone_status(params: dict[str, Any]) -> dict[str, Any]:
    """Read-only clone health for a project (T-0296). Logic in clones.py.

    Required: slug. Returns the read-model (dev/prod present/branch/ahead-
    behind/clean + workspace + last-deploy)."""
    extra = set(params) - _CLONE_STATUS_ALLOWED
    if extra:
        raise ActionError(f"clone_status got unexpected params: {sorted(extra)}")
    missing = _CLONE_STATUS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"clone_status missing required params: {sorted(missing)}")
    from bot_squad_worker import clones as _clones
    try:
        return _clones.clone_status(_get_config(), params["slug"])
    except KeyError:
        raise ActionError(f"clone_status: unknown project slug {params['slug']!r}")


_PULL_MASTER_REQUIRED = {"slug", "requested_by"}
_PULL_MASTER_ALLOWED = _PULL_MASTER_REQUIRED


def _action_pull_master(params: dict[str, Any]) -> dict[str, Any]:
    """Fast-forward a project's prod (master) clone to origin (T-0296).

    Admin-gated at the API edge. Required: slug, requested_by. Returns
    ``{ok, detail, from_sha?, to_sha?}`` — never raises on a refused ff
    (diverged/dirty prod), only on an unknown slug / bad params."""
    extra = set(params) - _PULL_MASTER_ALLOWED
    if extra:
        raise ActionError(f"pull_master got unexpected params: {sorted(extra)}")
    missing = _PULL_MASTER_REQUIRED - set(params)
    if missing:
        raise ActionError(f"pull_master missing required params: {sorted(missing)}")
    from bot_squad_worker import clones as _clones
    try:
        return _clones.pull_master(_get_config(), params["slug"])
    except KeyError:
        raise ActionError(f"pull_master: unknown project slug {params['slug']!r}")


_DEPLOY_REQUIRED = {"slug", "target", "reason", "requested_by"}
# restart_worker (T-0181, optional): opt the deploy into a post-sync worker
# restart — see deploy.enqueue. Default OFF; the agent sets it only when the
# diff touches worker-loaded code (worker/.../actions.py et al.).
_DEPLOY_ALLOWED = _DEPLOY_REQUIRED | {"restart_worker"}


def _action_deploy(params: dict[str, Any]) -> dict[str, Any]:
    """Queue a deploy request for a registered project.

    Required params: slug, target, reason, requested_by
    Returns: {ok: true, queue_id: str, queued_at: float}

    Raises ActionError on unknown slug, unknown target, extra/missing params.
    """
    extra = set(params) - _DEPLOY_ALLOWED
    if extra:
        raise ActionError(f"deploy got unexpected params: {sorted(extra)}")
    missing = _DEPLOY_REQUIRED - set(params)
    if missing:
        raise ActionError(f"deploy missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    target = params["target"]

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"deploy: unknown project slug {slug!r}")

    from bot_squad_worker import deploy as _deploy
    # T-0458: echo the to-be-built commit (origin/<deploy_branch> tip) so the
    # requester sees WHICH commit this deploy will ship up front. Best-effort —
    # "" if it can't be resolved; the authoritative sha is re-parsed post-build.
    #
    # T-0754 moved this ABOVE the enqueue and hands the same value in, so the
    # echoed sha and the one persisted in the queue payload are one resolution.
    # Resolving twice around a push landing in between would let /api/health
    # compare drift against a commit the requester was never told about.
    target_sha = _deploy.resolve_target_sha(cfg, slug, target)
    try:
        queue_id = _deploy.enqueue(
            cfg,
            slug=slug,
            target=target,
            reason=params["reason"],
            requested_by=params["requested_by"],
            restart_worker=bool(params.get("restart_worker", False)),
            target_sha=target_sha,
        )
    except ValueError as e:
        raise ActionError(f"deploy: {e}") from e

    import time as _time
    return {
        "ok": True,
        "queue_id": queue_id,
        "queued_at": _time.time(),
        "target_sha": target_sha,
    }


_PAUSE_DEPLOYS_REQUIRED = {"slug", "reason", "requested_by"}
_PAUSE_DEPLOYS_ALLOWED = _PAUSE_DEPLOYS_REQUIRED


def _action_pause_deploys(params: dict[str, Any]) -> dict[str, Any]:
    """Pause the deploy queue for a project until resume_deploys is called.

    Required params: slug, reason, requested_by
    Returns: {ok: true, paused: <meta dict>, was_already_paused: bool}

    A PAUSED.json marker is written to data/<slug>/_jobs/deploy/. The
    deploy_monitor checks for it on every tick and silently defers when
    present — no TG spam during the pause. One TG ping is sent at pause
    time (and one at resume time) so the operator knows the state flipped.
    """
    extra = set(params) - _PAUSE_DEPLOYS_ALLOWED
    if extra:
        raise ActionError(f"pause_deploys got unexpected params: {sorted(extra)}")
    missing = _PAUSE_DEPLOYS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"pause_deploys missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"pause_deploys: unknown project slug {slug!r}")

    from bot_squad_worker import deploy as _deploy
    was_paused = _deploy.is_paused(cfg, slug) is not None
    meta = _deploy.pause(cfg, slug, params["reason"], params["requested_by"])

    if not was_paused:
        from bot_squad_worker import channels as _channels
        from bot_squad_worker import sessions as _sessions
        from bot_squad_worker import tg_topics as _tg_topics
        # T-0591 (F5.3): routed through the channel abstraction.
        # T-0799: LOG class — a queue-state flip is a record of a thing a human
        # just did on purpose, so it is never news to the person who did it.
        from bot_squad_worker import msg_routes as _msg_routes
        routed = _msg_routes.route(
            cfg, "deploy_queue", slug=slug,
            chat_id=project.tg_chat,  # type: ignore[attr-defined]
            topic_id=_tg_topics.resolve(cfg, slug, "deploy_logs"),
        )
        _channels.get_channel(cfg, project=slug).send(
            f"🟡 deploys paused for {slug} — {meta['reason']} (by {meta['paused_by']})",
            chat_id=routed.chat_id,
            sid=_sessions.sid_display_label("deploy_monitor", slug),
            topic_id=routed.topic_id,
        )

    return {"ok": True, "paused": meta, "was_already_paused": was_paused}


_RESUME_DEPLOYS_REQUIRED = {"slug"}
_RESUME_DEPLOYS_ALLOWED = _RESUME_DEPLOYS_REQUIRED | {"requested_by"}


def _action_resume_deploys(params: dict[str, Any]) -> dict[str, Any]:
    """Resume a paused deploy queue. No-op (idempotent) if not paused.

    Required params: slug
    Optional params: requested_by (for TG attribution)
    Returns: {ok: true, was_paused: bool}
    """
    extra = set(params) - _RESUME_DEPLOYS_ALLOWED
    if extra:
        raise ActionError(f"resume_deploys got unexpected params: {sorted(extra)}")
    missing = _RESUME_DEPLOYS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"resume_deploys missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"resume_deploys: unknown project slug {slug!r}")

    from bot_squad_worker import deploy as _deploy
    was_paused = _deploy.resume(cfg, slug)

    if was_paused:
        who = params.get("requested_by") or "?"
        from bot_squad_worker import channels as _channels
        from bot_squad_worker import sessions as _sessions
        from bot_squad_worker import tg_topics as _tg_topics
        # T-0591 (F5.3): routed through the channel abstraction.
        # T-0799: LOG class — the twin of the pause notice above.
        from bot_squad_worker import msg_routes as _msg_routes
        routed = _msg_routes.route(
            cfg, "deploy_queue", slug=slug,
            chat_id=project.tg_chat,  # type: ignore[attr-defined]
            topic_id=_tg_topics.resolve(cfg, slug, "deploy_logs"),
        )
        _channels.get_channel(cfg, project=slug).send(
            f"🟢 deploys resumed for {slug} (by {who})",
            chat_id=routed.chat_id,
            sid=_sessions.sid_display_label("deploy_monitor", slug),
            topic_id=routed.topic_id,
        )

    return {"ok": True, "was_paused": was_paused}


# ---------------------------------------------------------------------------
# Session management actions (spec #5)
# ---------------------------------------------------------------------------

_LIST_SESSIONS_REQUIRED = {"slug"}
_LIST_SESSIONS_ALLOWED = _LIST_SESSIONS_REQUIRED


def _action_list_sessions(params: dict[str, Any]) -> dict[str, Any]:
    """List all Claude sessions for a project (active + paused).

    Required params: slug
    Returns: [{sid, status, window, cwd, started_at, last_prompt_at, claude_uuid, task_id, ...}]
    """
    extra = set(params) - _LIST_SESSIONS_ALLOWED
    if extra:
        raise ActionError(f"list_sessions got unexpected params: {sorted(extra)}")
    missing = _LIST_SESSIONS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"list_sessions missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    # Wrap in a dict so the FastAPI response (typed `-> dict`) validates.
    return {"sessions": _sessions.list_sessions(cfg, params["slug"])}


_TELEMETRY_GET_REQUIRED = {"slug"}
_TELEMETRY_GET_ALLOWED = _TELEMETRY_GET_REQUIRED


def _action_telemetry_get(params: dict[str, Any]) -> dict[str, Any]:
    """T-0210: return persisted resource telemetry for a project.

    Required params: slug
    Returns: {sessions: [{sid, context:{tokens,pct,...}, memory:{...}, ...}],
              quota: {burn_tokens_per_hr, projected_exhaustion_at, throttled, ...}}
    """
    extra = set(params) - _TELEMETRY_GET_ALLOWED
    if extra:
        raise ActionError(f"telemetry_get got unexpected params: {sorted(extra)}")
    missing = _TELEMETRY_GET_REQUIRED - set(params)
    if missing:
        raise ActionError(f"telemetry_get missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import telemetry as _telemetry
    return _telemetry.read_telemetry(cfg, params["slug"])


_PAUSE_SESSION_REQUIRED = {"slug", "sid"}
_PAUSE_SESSION_ALLOWED = _PAUSE_SESSION_REQUIRED


def _action_pause_session(params: dict[str, Any]) -> dict[str, Any]:
    """Pause a running Claude session.

    Required params: slug, sid
    Returns: {ok: true, paused: true}
    """
    extra = set(params) - _PAUSE_SESSION_ALLOWED
    if extra:
        raise ActionError(f"pause_session got unexpected params: {sorted(extra)}")
    missing = _PAUSE_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"pause_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.pause(cfg, params["slug"], params["sid"])


_SUSPEND_SESSION_REQUIRED = {"slug", "sid"}
_SUSPEND_SESSION_ALLOWED = _SUSPEND_SESSION_REQUIRED


def _action_suspend_session(params: dict[str, Any]) -> dict[str, Any]:
    """Suspend a Claude session — close the pane to free resources.

    Required params: slug, sid
    Returns: {ok: true, suspended: true}
    The registry md is preserved so resume() can resurrect via --resume.
    """
    extra = set(params) - _SUSPEND_SESSION_ALLOWED
    if extra:
        raise ActionError(f"suspend_session got unexpected params: {sorted(extra)}")
    missing = _SUSPEND_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"suspend_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.suspend(cfg, params["slug"], params["sid"])


_RESUME_SESSION_REQUIRED = {"slug", "sid"}
_RESUME_SESSION_ALLOWED = _RESUME_SESSION_REQUIRED | {"initial_prompt", "task_id"}


def _action_resume_session(params: dict[str, Any]) -> dict[str, Any]:
    """Resume a paused Claude session.

    Required params: slug, sid
    Returns: {ok: true, sid: <new_sid>}
    """
    extra = set(params) - _RESUME_SESSION_ALLOWED
    if extra:
        raise ActionError(f"resume_session got unexpected params: {sorted(extra)}")
    missing = _RESUME_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"resume_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.resume(
        cfg, params["slug"], params["sid"],
        initial_prompt=params.get("initial_prompt"),
        task_id=params.get("task_id"),
    )


_SPAWN_SESSION_REQUIRED = {"slug", "window"}
# T-0128: parent_sid (the SID that requested this spawn) is an OPTIONAL param —
# the worker stamps it into the new session md so the tree survives a restart.
_SPAWN_SESSION_ALLOWED = _SPAWN_SESSION_REQUIRED | {
    "initial_prompt", "task_id", "initiative", "owner", "parent_sid",
    "owner_user",  # T-0321: per-user-scoping username
    "model",  # T-0623: explicit `claude --model` override; absent = role default
    "provider",  # explicit cross-provider override; model alone stays in project provider
}


def _action_spawn_session(params: dict[str, Any]) -> dict[str, Any]:
    """Spawn a new Claude session in the project's repo.

    Required params: slug, window
    Optional params: initial_prompt
    Returns: {ok: true, sid: <new_sid>}
    """
    extra = set(params) - _SPAWN_SESSION_ALLOWED
    if extra:
        raise ActionError(f"spawn_session got unexpected params: {sorted(extra)}")
    missing = _SPAWN_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"spawn_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions

    # T-0472: exactly one operator per project. The operator is a transient
    # dispatcher on the universal lifecycle — a second concurrent operator would
    # double-drive the backlog. Block a spawn whose window derives the operator
    # role when a live operator already holds this project. (A re-drive after the
    # prior incarnation exits sees no live operator and proceeds — see
    # dispatch.live_operator_sids.) Guard lives here, above sessions.spawn, so it
    # covers BOTH the API auto-spawn-on-create and `bsq spawn --window operator`.
    if _sessions._derive_role(params["window"], None, None) == "operator":
        # T-0523: operator-dispatches-only. The operator ORCHESTRATES — it spawns
        # devs for tickets and must NEVER self-claim/bind a dev assignment
        # (voice-03; the live regression had a re-driven operator bind & race a
        # dev ticket). Reject an operator spawn that carries a real task_id bind.
        # `~` is the registry's "unset" sentinel and is not a real binding.
        tid = params.get("task_id")
        if tid and tid != "~":
            raise ActionError(
                f"operator spawn must not bind a dev task ({tid!r}) — the "
                "operator orchestrates and spawns devs for tickets (T-0523)"
            )
        from bot_squad_worker import dispatch as _dispatch
        existing = _dispatch.live_operator_sids(cfg, params["slug"])
        if existing:
            raise ActionError(
                f"operator already running for {params['slug']!r}: {existing[0]} "
                "— exactly one operator per project (T-0472)"
            )

    return _sessions.spawn(
        cfg,
        params["slug"],
        params["window"],
        params.get("initial_prompt"),
        task_id=params.get("task_id"),
        initiative=params.get("initiative"),
        owner=params.get("owner"),
        parent_sid=params.get("parent_sid"),
        owner_user=params.get("owner_user"),
        model=params.get("model"),
        provider=params.get("provider"),
    )


# ---------------------------------------------------------------------------
# ensure_user_conversation action (T-0478, M2/F2.4)
# ---------------------------------------------------------------------------

_ENSURE_UCONV_REQUIRED = {"slug", "global_user_id"}
# T-0623: optional explicit model override for the fresh-spawn path; absent
# falls through to sessions.spawn's role default (user-conversation -> claude-sonnet-5).
# T-0676 items 3/6: optional thread_id — see _action_ensure_user_conversation.
_ENSURE_UCONV_ALLOWED = _ENSURE_UCONV_REQUIRED | {"message_ref", "model", "thread_id"}


def _thread_scoped_read_write_block(slug: str, global_user_id: str, thread_id: Any) -> str:
    """T-0676 items 3/6: when the triggering message came from a BOUND FORUM
    TOPIC, the attendant must read/reply into THAT topic's isolated thread —
    not the project's whole (mixed-topic) history — or it re-surfaces the
    cross-topic bleed (item 6) and misroutes its reply to the wrong topic
    (item 3). ``thread_id`` absent (DM / non-topic message) returns "" —
    caller falls back to the exact pre-T-0676 instructions, so a DM attendant
    is completely unaffected.

    T-0775: the route is the WORKER-token one (``/api/m/worker/...``). The
    attendant runs in worker context and holds the Bearer, not a UI cookie —
    see ``_user_conversation_boot_prompt`` for why the two nearby wrong
    answers (a bare ``/api/`` 404, the session-auth surface's 401) are both
    silent-looking to the session that hits them."""
    if thread_id is None or str(thread_id).strip() == "":
        return ""
    # T-0795: the topic id gets the shared highlighted rendering on its own
    # line, ahead of the prose that used to carry it mid-sentence — the ids in
    # a new-message ping are what a session needs to find first. No chat id at
    # this seam: see ``ping_ids.render``.
    from bot_squad_worker import ping_ids as _ping_ids
    return (
        f"\n{_ping_ids.render(thread_id=thread_id)}\n"
        f"This message arrived in a BOUND FORUM TOPIC — read and reply WITHIN "
        f"that topic's own isolated thread, not the project's full history:\n"
        f"  GET /api/m/worker/conversations/{slug}/{global_user_id}/messages?thread_id={thread_id}\n"
        f"  (reply by appending with thread_id={thread_id} so it relays back "
        f"into the SAME topic, not elsewhere)\n"
    )


def _group_prompt_block(cfg: Any, slug: str, global_user_id: str) -> str:
    """T-0591 (F5.10): the user's project-group prompt, if any — the one
    consumption seam ``project_groups_store.group_for_user`` (T-0496) was
    built for but never had a caller. Injected directly into the boot/resume
    prompt (in-process file read, not an HTTP round-trip — the worker already
    has ``cfg.data_dir`` on hand at spawn time). Empty string when the user
    has no group (default treatment, unchanged behaviour)."""
    from bot_squad_worker import project_groups_store as _groups

    try:
        group = _groups.group_for_user(cfg.data_dir, slug, global_user_id)
    except ValueError:
        return ""
    if not group or not str(group.get("prompt") or "").strip():
        return ""
    name = group.get("name") or group.get("id") or "?"
    role = group.get("role") or ""
    scope = group.get("access_scope") or ""
    header = f"\nYou are bound to project group `{name}`"
    if role:
        header += f" (role: {role})"
    if scope:
        header += f" — access scope: {scope}"
    return f"{header}. Group-specific instructions:\n{group['prompt'].strip()}\n"


def _user_conversation_boot_prompt(
    cfg: Any, slug: str, global_user_id: str, message_ref: str | None,
    thread_id: Any = None,
) -> str:
    """The initial prompt a freshly-spawned user-conversation session boots on.

    Unlike a dev brief (task-centric, assembled by `bsq spawn`), a user-
    conversation session holds no ticket — it attends a thread. So we orient it
    explicitly: run `bsq brief` for its full role contract (now resolved to
    user-conversation.md), then read its thread + the new inbound message. The
    behavioural mandate (verbatim-into-tasks, notify-operator, unrestricted)
    lives in the role contract; this prompt points at it and supplies the
    per-session context (which user, which message).

    T-0775: the read URL is the WORKER-token route ``/api/m/worker/...`` and
    it is spelled out with its base + auth header, because BOTH nearby wrong
    answers are quiet to the session that hits them. The bare
    ``/api/conversations/...`` this prompt used to emit is mounted NOWHERE —
    measured 404 on the live install with a valid token, while
    ``routes_conversations`` is included with prefix ``/api/m`` (main.py) —
    and the ``/api/m/conversations/...`` UI surface answers a worker Bearer
    with 401 (also measured), so half-correcting the prefix trades a loud
    failure for a quieter one. An attendant that reads its contract (which has
    named the ``/worker/`` path since T-0529) works; one that trusts this
    prompt did not. The contract also offers "read the store JSONL directly"
    as its API-unreachable fallback, so hitting the dead path looks exactly
    like choosing the expensive route rather than like a broken prompt.

    ``_api_base_url()`` is the same accessor the worker's own working callers
    use (tg_listener, task_chat, outbound_log) rather than a hardcoded
    ``127.0.0.1:8099``; unset env yields "" and the prompt degrades to the
    correct RELATIVE path, never to a wrong absolute one."""
    from bot_squad_worker import tg_listener as _tg_listener

    api_base = _tg_listener._api_base_url()
    new_msg = ""
    if message_ref and str(message_ref).strip():
        new_msg = (
            "\nThe message that triggered this spawn:\n"
            f"  {str(message_ref).strip()}\n"
        )
    group_block = _group_prompt_block(cfg, slug, global_user_id)
    thread_block = _thread_scoped_read_write_block(slug, global_user_id, thread_id)
    return f"""\
You are a USER-CONVERSATION session (system-controlled), spawned on incoming
user mail for project `{slug}`, attending the user `{global_user_id}`.

FIRST run `bsq brief` to load your full role contract (user-conversation.md) +
the product/protocol. Your mandate, in short:
  - Read this user's conversation thread (your durable memory) before replying:
    GET {api_base}/api/m/worker/conversations/{slug}/{global_user_id}/messages
    (worker-token route — send `Authorization: Bearer $WORKER_API_TOKEN`, the
    value in the install `.env`. Your contract explains why the UI read
    surface is not yours to call.)
  - Talk to the user; reply by appending to that same thread
    (author "session:<your-sid>") — the comms layer relays it back to them.
  - When the user ASKS FOR WORK, record it VERBATIM into a backlog task: mint
    the id via the `task_new` worker action (never hand-pick T-NNNN), then paste
    their EXACT words into the task's `## Verbatim request` (M8 — never
    paraphrase) and stamp provenance back to this user + message.
  - NOTIFY THE OPERATOR after recording (`bsq peer send <operator-sid> "<task
    id> — <one-liner>"`); the operator dispatches the build, not you.
  - You are UNRESTRICTED: you may spawn an operator/TL/ad-hoc session or fix
    things yourself in service of the user's ask.
{new_msg}{thread_block}{group_block}"""


# T-0720 (operator ruling 2026-07-26): there is deliberately NO
# resume-a-recycled-attendant path here. T-0575 shipped one at 74eef0f
# (``_resume_recycled_user_conversation`` + a resume wake prompt); it was
# removed because it is STRUCTURALLY unreachable, not merely unused:
#
#   ``recycle_gate.role_exempt()`` returns True for every ``user-conversation``
#   session (T-0564 — "the human's own live chat is never auto-recycled"), and
#   ``idle_timeout``'s compact-terminate-remember flow is the ONLY writer of the
#   ``resumable: true`` / ``recycled_at`` stamp this path searched for. So an
#   attendant can never reach that state: the finder always came back empty.
#   Confirmed by 12 days of production journal (T-0575 progress, 2026-07-18: 27
#   recycles, zero user-conversation) and by an operator-approved staged live
#   test (2026-07-26, synthetic attendant idle 85 min → compact-and-stay only).
#
# The ruling is that T-0564's exemption STANDS and this role loses nothing:
# T-0617's compact-and-stay is the better strategy here — it compacts context
# in place and never terminates, so the human's pane is never traded for a
# resume that might fail. The stakeholder's T-0575 ask ("compact, terminate,
# --resume") is served for RECYCLING roles instead, by T-0150 expert-resume and
# by ``dispatch.decide_dispatch``'s resume hints (both live). Re-adding a resume
# preference here requires narrowing ``recycle_gate`` first — do not.


def _action_ensure_user_conversation(params: dict[str, Any]) -> dict[str, Any]:
    """Ensure a live user-conversation session is attending ``(slug,
    global_user_id)``; spawn one on incoming user mail if none is running.

    This is the M5-firehose seam (T-0478): the comms/intake side (Cluster D)
    resolves the inbound TG sender to a ``global_user_id`` (T-0488), appends the
    message to the conversation store (T-0489), then calls THIS to make sure a
    session is attending that thread.

    Idempotent single-attendant: if a live user-conversation session already
    holds this user, the new message is routed to it (best-effort pane nudge)
    rather than spawning a duplicate — so a burst of messages does not fan out
    into N sessions.

    Required params: slug, global_user_id
    Optional params: message_ref (a reference/snippet of the inbound message,
                     surfaced in the boot prompt; the session reads the full
                     thread from the store); model (T-0623: explicit
                     `claude --model` override for a fresh spawn — absent
                     falls through to sessions.spawn's role default); thread_id
                     (T-0676 items 3/6: the bound forum topic this message
                     came from, when any — the ONE attendant per (slug, gid)
                     is unchanged, but the boot/resume/nudge it gets is told
                     to read/reply into THAT topic's isolated thread instead
                     of the project's whole mixed history, killing the
                     cross-topic bleed / misrouted-reply pair. Absent/None
                     behaves byte-identically to before this change).
    Returns: {ok, sid, spawned: bool}
    """
    extra = set(params) - _ENSURE_UCONV_ALLOWED
    if extra:
        raise ActionError(
            f"ensure_user_conversation got unexpected params: {sorted(extra)}")
    missing = _ENSURE_UCONV_REQUIRED - set(params)
    if missing:
        raise ActionError(
            f"ensure_user_conversation missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions

    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"ensure_user_conversation: unknown project slug {slug!r}")
    gid = params["global_user_id"]
    message_ref = params.get("message_ref")
    # T-0676 items 3/6: which bound forum topic (if any) triggered this ensure
    # — threaded through to the boot/resume/nudge prompts so the ONE
    # attendant per (slug, gid) reads/replies into THAT topic's isolated
    # thread for this message, instead of the whole mixed project history.
    # None (DM / non-topic message) leaves every prompt byte-identical to
    # before this change.
    thread_id = params.get("thread_id")

    # Validate the gid up front (raises on a crafted value): it is both the
    # spawn window AND the per-(slug,gid) lock-file segment below, so it must be
    # a single safe path/shell segment before either side effect.
    window = _sessions.user_conversation_window(gid)

    # T-0478 (REOPENED) fix — serialize the WHOLE check-and-spawn under a
    # per-(slug,gid) flock. Without it, two near-simultaneous inbound messages
    # for the SAME user both read "no live attendant" before either spawns, and
    # each spawns one → a duplicate session + duplicate verbatim ticket per
    # message (the observed fan-out). The lock is per-gid (the filename carries
    # the validated gid) so different users never contend, mirroring spawn()'s
    # ``.task-claim.lock`` pattern. The flock closes the TOCTOU; the rewritten
    # ``live_user_conversation_sid`` (md-scan, not slug-scoped pane-scan) closes
    # the structural miss — BOTH are required (a flock around the old broken
    # reuse check would still fan out, since the check never matched).
    sess_dir = cfg.data_dir / slug / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    claim_fd = open(sess_dir / f".uconv-claim-{gid}.lock", "w")
    try:
        fcntl.flock(claim_fd, fcntl.LOCK_EX)

        # Reuse: a live attendant already holds this (slug, gid) → route to it.
        existing = _sessions.live_user_conversation_sid(cfg, slug, gid)
        if existing is not None:
            if message_ref and str(message_ref).strip():
                # Best-effort wake — the attendant re-reads its thread for the
                # new message. A pane-timing hiccup must never fail the ensure
                # (the message is already durable in the store).
                nudge_text = "A new message arrived in your user-conversation thread — read it and respond."
                # T-0795: the topic id LEADS the nudge in the shared highlighted
                # rendering instead of sitting mid-sentence. This text must stay
                # ONE LINE — `inject_input` below is one-Enter-per-line, so a
                # multi-line block here would arrive as N composer submissions
                # (the transport measurement behind tg_direct_reply/T-0773),
                # which is why the convention is a single line everywhere.
                from bot_squad_worker import ping_ids as _ping_ids
                ids = _ping_ids.render(thread_id=thread_id)
                if ids:
                    # T-0676 items 3/6: point the SAME attendant at THIS
                    # topic's isolated thread, not the mixed project history.
                    # T-0775: worker-token route — the bare /api/conversations/
                    # prefix this used to name is mounted nowhere (404).
                    nudge_text = (
                        f"{ids} — a new message arrived in that topic of your "
                        f"user-conversation. Read that topic's isolated thread "
                        f"(GET /api/m/worker/conversations/{slug}/{gid}/messages"
                        f"?thread_id={thread_id}) and reply into it (append with "
                        f"thread_id={thread_id})."
                    )
                try:
                    _action_inject_input({
                        "sid": existing,
                        "text": nudge_text,
                    })
                except ActionError:
                    pass
            return {"ok": True, "sid": existing, "spawned": False}

        # Spawn: no live attendant → open one in the gid-keyed window. A
        # recycled-attendant resume is deliberately NOT attempted first —
        # see the T-0720 note above this function for why that state can
        # never exist for this role.
        result = _sessions.spawn(
            cfg,
            slug,
            window,
            _user_conversation_boot_prompt(cfg, slug, gid, message_ref, thread_id),
            model=params.get("model"),
        )
        return {"ok": True, "sid": result["sid"], "spawned": True}
    finally:
        # Closing the fd releases the flock (also on the spawn error path).
        claim_fd.close()


# ---------------------------------------------------------------------------
# Scheduler state action (spec #6)
# ---------------------------------------------------------------------------

def _action_scheduler_state(params: dict[str, Any]) -> dict[str, Any]:
    """Return current APScheduler state (jobs, uptime, heartbeat age).

    Takes no params. Returns {jobs, worker_started_at, last_heartbeat_age_seconds}.
    """
    if params:
        raise ActionError(f"scheduler_state takes no params, got: {sorted(params)}")

    cfg = _get_config()
    if _SCHED is None:
        raise ActionError("scheduler not initialised")

    from bot_squad_worker.scheduler import state_for_api
    return state_for_api(_SCHED, cfg)


# ---------------------------------------------------------------------------
# outbound_liveness action (T-0759)
# ---------------------------------------------------------------------------

def _action_outbound_liveness(params: dict[str, Any]) -> dict[str, Any]:
    """Is the outbound log still recording what we send?

    Takes no params. Returns the full verdict — ``{state, reason, scan,
    witness, last_accounted_ts, last_send_ts, lag_s, health}``, where ``state``
    is ok/idle/decayed/blind.

    READ-ONLY, and deliberately NOT the tick: it announces nothing and touches
    no state file, so an operator can ask the question by hand — which is what
    p298 did at 12:18 on 2026-07-27 — without perturbing the alarm's own
    persist window. ``scan`` rides on every answer because a zero from this
    read is only believable once the read is proven live.
    """
    if params:
        raise ActionError(f"outbound_liveness takes no params, got: {sorted(params)}")

    from bot_squad_worker import outbound_liveness as _ol

    return _ol.check(_get_config())


# ---------------------------------------------------------------------------
# inject_input action (spec #7)
# ---------------------------------------------------------------------------

_INJECT_INPUT_REQUIRED = {"sid", "text"}
_INJECT_INPUT_ALLOWED = _INJECT_INPUT_REQUIRED


def _action_inject_input(params: dict[str, Any]) -> dict[str, Any]:
    """Send text to the tmux pane for a SID (one Enter per line).

    Required params: sid, text
    Returns: {ok: true, pane_id, lines_sent: int}

    T-0578: the transport is ``input_mux.deliver_direct`` — content lands
    byte-identical (verbatim, uncaptioned, one submission per line: "check
    mail" stays "check mail", "/compact" stays a bare slash command), but the
    keystrokes are serialised under the per-sid delivery lock so a nudge can
    never interleave with a concurrent ``send_input`` flush, and briefly gate
    on live user typing.
    """
    extra = set(params) - _INJECT_INPUT_ALLOWED
    if extra:
        raise ActionError(f"inject_input got unexpected params: {sorted(extra)}")
    missing = _INJECT_INPUT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"inject_input missing required params: {sorted(missing)}")

    sid = params["sid"]
    text = params["text"]
    if not text.strip():
        raise ActionError("inject_input: empty text")

    from bot_squad_worker import sessions as S
    panes = S.list_panes()
    user = S._get_current_user()
    pane = next(
        (p for p in panes if S.compute_sid(user, p.window, p.pane_id) == sid),
        None,
    )
    if pane is None:
        raise ActionError(f"inject_input: no live pane for sid {sid!r}")

    from bot_squad_worker import input_mux
    cfg = _get_config()
    lines_sent = input_mux.deliver_direct(
        cfg.data_dir, sid, pane.pane_id, text,
    )
    return {"ok": True, "pane_id": pane.pane_id, "lines_sent": lines_sent}


# ---------------------------------------------------------------------------
# inject_prompt action (T-0770) — the BLOCK sibling of inject_input
# ---------------------------------------------------------------------------

_INJECT_PROMPT_REQUIRED = {"sid", "text"}
_INJECT_PROMPT_ALLOWED = _INJECT_PROMPT_REQUIRED


def _action_inject_prompt(params: dict[str, Any]) -> dict[str, Any]:
    """Deliver a multi-line block to a SID as ONE composer message.

    Required params: sid, text. Returns ``{ok: true, pane_id}``.

    WHY THIS EXISTS BESIDE ``inject_input`` (T-0770, measured, not assumed).
    ``inject_input``'s transport sends one send-keys + Enter PER LINE — correct
    for the single-line nudges it was written for ("check mail", "/compact"),
    and wrong for anything with a newline in it: a 3-line payload becomes THREE
    separate composer submissions, so the session starts answering line 1 while
    lines 2-3 are still arriving. That is already true of the stakeholder's own
    multi-line messages on the direct-mode topic path, and a multi-line
    provenance envelope on that transport would have been strictly worse than
    the bare text it replaces.

    The transport here is ``sessions._deliver_prompt`` — the T-0144/T-0201
    paste-buffer primitive ``drift_check`` and the spawn briefs already use:
    bracketed paste (embedded newlines stay newlines), then a separate
    confirm-then-Enter to submit, all under the per-sid mux delivery lock.

    The CONTRACT matches ``inject_input`` exactly, and that is load-bearing
    rather than tidy: it raises ``ActionError`` when there is no live pane and
    when the paste/submit never lands, so ``tg_listener._handle_reply``'s
    T-0746 undelivered fallback — the thing that stops a message evaporating
    when a session has been reaped — keeps working unchanged for callers that
    move from one to the other. This is deliberately NOT ``send_input``, whose
    queue DEFERS (never raises) when no pane exists: a message that quietly sits
    in a queue for a dead session is the silence this ticket is about."""
    extra = set(params) - _INJECT_PROMPT_ALLOWED
    if extra:
        raise ActionError(f"inject_prompt got unexpected params: {sorted(extra)}")
    missing = _INJECT_PROMPT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"inject_prompt missing required params: {sorted(missing)}")

    sid = params["sid"]
    text = params["text"]
    if not text.strip():
        raise ActionError("inject_prompt: empty text")

    pane_id = _send_input_pane_lookup(sid)
    if pane_id is None:
        raise ActionError(f"inject_prompt: no live pane for sid {sid!r}")

    from bot_squad_worker import sessions as S
    cfg = _get_config()
    S._deliver_prompt(pane_id, text, data_dir=cfg.data_dir, sid=sid)
    return {"ok": True, "pane_id": pane_id}


# ---------------------------------------------------------------------------
# send_input action (T-0469, M1/F1.6) — multiplexed, queue-backed input
# ---------------------------------------------------------------------------
#
# The agent-facing channel. Where ``inject_input`` is the raw primitive (one
# Enter per line, used internally for the check-mail nudge / /compact), agents
# write to a session via ``send_input``: writes are QUEUED (never rejected),
# COALESCED across concurrent callers, delivered BATCHED with author captions,
# and DEFERRED whenever the composer is busy so live user-typed text is never
# clobbered (T-0469 / voice-10 / SOURCE-VERBATIM Part A).

_SEND_INPUT_REQUIRED = {"sid", "text"}
_SEND_INPUT_ALLOWED = _SEND_INPUT_REQUIRED | {"author"}


def _send_input_pane_lookup(sid: str) -> str | None:
    """Resolve a live pane_id for ``sid`` the same way inject_input does.

    (list_panes + compute_sid — the per-user-worker tmux view, not autocompact's
    coordinator-side live_pane_map.)
    """
    from bot_squad_worker import sessions as S
    user = S._get_current_user()
    for p in S.list_panes():
        if S.compute_sid(user, p.window, p.pane_id) == sid:
            return p.pane_id
    return None


def _action_send_input(params: dict[str, Any]) -> dict[str, Any]:
    """Enqueue input to a target session, then flush (coalesced + captioned).

    Required params: sid, text. Optional: author (caption; default "system").
    Returns: {ok, queued, delivered, deferred, reason}. A concurrent write is
    never rejected — it queues and is delivered on this or a later flush.
    """
    extra = set(params) - _SEND_INPUT_ALLOWED
    if extra:
        raise ActionError(f"send_input got unexpected params: {sorted(extra)}")
    missing = _SEND_INPUT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"send_input missing required params: {sorted(missing)}")

    sid = params["sid"]
    text = params["text"]
    if not text.strip():
        raise ActionError("send_input: empty text")
    author = params.get("author") or "system"

    from bot_squad_worker import input_mux
    cfg = _get_config()
    queued = input_mux.enqueue(cfg.data_dir, sid, text, author)
    res = input_mux.flush(cfg.data_dir, sid, pane_lookup=_send_input_pane_lookup)
    return {
        "ok": True,
        "queued": queued,
        "delivered": res.get("delivered", 0),
        "deferred": bool(res.get("deferred")),
        "reason": res.get("reason"),
    }


# ---------------------------------------------------------------------------
# Autopilot actions (T-0153) — prompt-driven, time-boxed autonomous runs
# ---------------------------------------------------------------------------

_AUTOPILOT_START_REQUIRED = {"slug", "kind", "prompt"}
_AUTOPILOT_START_ALLOWED = _AUTOPILOT_START_REQUIRED | {
    "ref", "early_exit", "duration_hours", "stall_minutes", "watchdog_minutes", "created_by",
}


def _action_autopilot_start(params: dict[str, Any]) -> dict[str, Any]:
    """Start an autopilot run for a team / session / project target.

    Required params: slug, kind ("team"|"session"|"project"), prompt
    Optional params: ref (team name / sid; defaults to slug for project),
        early_exit, duration_hours (default 8), stall_minutes (default 60),
        watchdog_minutes (default 5), created_by
    Returns the autopilot handle: {ok, key, target_sid, expires_at, spawned, ...}
    """
    extra = set(params) - _AUTOPILOT_START_ALLOWED
    if extra:
        raise ActionError(f"autopilot_start got unexpected params: {sorted(extra)}")
    missing = _AUTOPILOT_START_REQUIRED - set(params)
    if missing:
        raise ActionError(f"autopilot_start missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import autopilot as _ap
    return _ap.start(
        cfg,
        params["slug"],
        kind=params["kind"],
        ref=params.get("ref", "") or "",
        prompt=params["prompt"],
        early_exit=params.get("early_exit", "") or "",
        duration_hours=params.get("duration_hours", 8.0),
        stall_minutes=params.get("stall_minutes", 60),
        watchdog_minutes=params.get("watchdog_minutes", 5),
        created_by=params.get("created_by", "") or "",
    )


_AUTOPILOT_STOP_REQUIRED = {"slug"}
_AUTOPILOT_STOP_ALLOWED = _AUTOPILOT_STOP_REQUIRED | {"key", "target_sid", "reason", "stopped_by"}


def _action_autopilot_stop(params: dict[str, Any]) -> dict[str, Any]:
    """End an autopilot early.

    Required params: slug, and at least one of {key, target_sid}.
    Optional params: reason (set ⟹ early-exit met), stopped_by.
    Returns {ok, key, status, exit_reason}.
    """
    extra = set(params) - _AUTOPILOT_STOP_ALLOWED
    if extra:
        raise ActionError(f"autopilot_stop got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autopilot_stop missing required param: slug")
    if not (params.get("key") or params.get("target_sid")):
        raise ActionError("autopilot_stop needs one of: key, target_sid")

    cfg = _get_config()
    from bot_squad_worker import autopilot as _ap
    return _ap.stop(
        cfg,
        params["slug"],
        key=params.get("key"),
        target_sid=params.get("target_sid"),
        reason=params.get("reason", "") or "",
        stopped_by=params.get("stopped_by", "") or "",
    )


_AUTOPILOT_STATUS_ALLOWED = {"slug"}


def _action_autopilot_status(params: dict[str, Any]) -> dict[str, Any]:
    """Return every autopilot state (active + recently ended) for a project.

    Required params: slug
    Returns {ok, slug, autopilots: [...]}.
    """
    extra = set(params) - _AUTOPILOT_STATUS_ALLOWED
    if extra:
        raise ActionError(f"autopilot_status got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autopilot_status missing required param: slug")

    cfg = _get_config()
    from bot_squad_worker import autopilot as _ap
    return _ap.status(cfg, params["slug"])


# ---------------------------------------------------------------------------
# Cross-session message bus actions (Phase 1)
# ---------------------------------------------------------------------------

_PEER_SEND_REQUIRED = {"slug", "from_sid", "to", "text"}
# T-0157: optional `user` overrides the linux-user scope for role-keyword
# fan-out (teamlead/dev/all) — cross-user messaging is opt-in.
_PEER_SEND_ALLOWED = _PEER_SEND_REQUIRED | {"user"}

# T-0035 (lean Option B): peer_send replies to a UI-shaped SID are mirrored
# to that user's bound Telegram chat so a stakeholder browsing the UI still
# sees responses while the in-UI chat panel is a follow-up.
# Format: S-<username>-ui-p<N> (Sessions.tsx::handleSend builds this).
_UI_SID_RE = re.compile(r"^S-([A-Za-z0-9_-]+)-ui-p\d+$")


def _parse_ui_sid_username(sid: str) -> str | None:
    """Return the username if ``sid`` looks like a UI-originated SID, else None."""
    m = _UI_SID_RE.match(sid)
    return m.group(1) if m else None


def _read_self_attachment(cfg: Any, global_user_id: str) -> dict | None:
    """Read the (user × this-install) Attachment JSON from the mothership store,
    or ``None`` when there's no self-server registry / attachment on disk.

    Mirrors ``api/app/mothership_users_store.py`` layout so the worker can apply
    the per-server + per-project notify overrides without importing the API."""
    mship = Path(cfg.data_dir) / "_mothership"
    servers_path = mship / "servers.json"
    if not servers_path.is_file():
        return None
    try:
        servers = json.loads(servers_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    self_id = next(
        (s.get("id") for s in servers.get("servers", []) if s.get("is_self")), None
    )
    if not self_id:
        return None
    att_path = mship / "attachments" / global_user_id / f"{self_id}.json"
    if not att_path.is_file():
        return None
    try:
        return json.loads(att_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_user_tg_chat_id(cfg: Any, username: str, slug: str = "") -> str:
    """Resolve ``username``'s personal Telegram chat with T-0218 precedence:
    ``project -> server(this install) -> global``, each falling through on empty.

    Global + the un-migrated project map come from ``auth.toml``; for migrated
    users (``attached_to_global_user`` set) the server + project levels come from
    the self-server Attachment JSON. Returns ``""`` when nothing resolves."""
    auth_path = Path(cfg.config_dir) / "auth.toml"
    if not auth_path.exists():
        return ""
    try:
        raw = tomllib.loads(auth_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("tg-notify resolve: failed to read auth.toml: %s", e)
        return ""
    meta = raw.get("user_meta", {}).get(username, {}) or {}
    global_chat = str(meta.get("tg_chat_id", "") or "")
    attached = str(meta.get("attached_to_global_user", "") or "")

    if not attached:
        # Un-migrated: the per-project map lives on user_meta itself.
        proj_map = meta.get("project_tg_chat_ids", {})
        if slug and isinstance(proj_map, dict) and proj_map.get(slug):
            return str(proj_map[slug])
        return global_chat

    att = _read_self_attachment(cfg, attached)
    if att is not None:
        proj_map = att.get("project_tg_chat_ids", {})
        if slug and isinstance(proj_map, dict) and proj_map.get(slug):
            return str(proj_map[slug])
        server_chat = str(att.get("tg_chat_id", "") or "")
        if server_chat:
            return server_chat
    return global_chat


def _action_peer_send(params: dict[str, Any]) -> dict[str, Any]:
    """Append a message to recipient inbox(es).

    Required params: slug, from_sid, to, text
    Returns: {ok: true, delivered_to: [sid, ...]}

    T-0035: any delivered SID matching ``S-<user>-ui-p<N>`` also fires a
    ``tg.send`` to that user's resolved personal chat. T-0218: resolution now
    honors the ``project -> server -> global`` precedence for the message's
    ``slug`` (see ``_resolve_user_tg_chat_id``). Mirror failures are logged but
    never break the bus write — delivered_to still reflects the inbox.

    T-0624: ``slug`` is only the SENDER's cwd-resolved project (see bsq's
    ``resolve_slug()``) — for a literal-SID ``to`` it is NOT necessarily the
    recipient's project. A cross-project send used to write silently into the
    sender's ``_chat`` dir while the recipient's ``bsq inbox check`` drained a
    different one (durable message loss, reported as "sent"). So a literal-SID
    target is delivered under ITS OWN project, resolved via a session-registry
    scan across every known project (``park._slug_for_sid``); an unresolvable
    recipient (no registered session anywhere) is a hard error rather than a
    silent misfile into the sender's project. Role-keyword targets
    (``teamlead``/``dev``/``all``/``operator``) keep using the caller's ``slug``
    — those are inherently an in-project broadcast, not addressed at a specific
    SID.

    T-0790: same silent-success signature, different resolution path — a target
    SID that a RECYCLE superseded (same project, dead session, live successor in
    its window) passed the T-0624 guard, since the predecessor's md still exists,
    and returned 200 having written into an inbox nobody drains. Two changes,
    both in ``intersession``'s resolution rather than here: such a target routes
    to its live successor (reported as ``redirected`` in the result), and
    ``operator`` became a real role keyword. This layer adds only the refusal for
    the one target no routing can save — archived, no successor.

    The ``inject_input`` 400 that pairs with this action's 200 is UNTOUCHED and
    stays the diagnostic it has been (operator p241 logged one on 2026-07-27
    before anyone knew what it meant). It still fires for a target with no live
    pane; what changed is that a RECYCLED target now has a live pane to nudge,
    so the pairing stops appearing for the case it was flagging.
    """
    extra = set(params) - _PEER_SEND_ALLOWED
    if extra:
        raise ActionError(f"peer_send got unexpected params: {sorted(extra)}")
    missing = _PEER_SEND_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_send missing required params: {sorted(missing)}")

    cfg = _get_config()
    to = params["to"]
    delivery_slug = params["slug"]
    from bot_squad_worker import intersession as _is
    if to not in _is._ROLE_KEYWORDS:
        from bot_squad_worker.park import _slug_for_sid
        recipient_slug = _slug_for_sid(cfg, to)
        if recipient_slug is None:
            raise ActionError(
                f"peer_send: recipient SID {to!r} has no registered session "
                f"under any known project — refusing to deliver into sender's "
                f"project {params['slug']!r} (would silently misfile)"
            )
        delivery_slug = recipient_slug
        # T-0790: an ARCHIVED target with no live successor can never read the
        # inbox we would write — ``archive_session`` already reaped its
        # sidecars, so the write would mint a fresh file nobody owns and return
        # 200. That is the silent-success signature this ticket exists to kill,
        # so refuse instead, and name the successor when one exists so the
        # sender can retry against a live seat.
        meta = _is._session_status(cfg, delivery_slug, to) or {}
        if (
            str(meta.get("archived", "")).lower() == "true"
            and _is.live_successor_sid(cfg, delivery_slug, to) is None
        ):
            raise ActionError(
                f"peer_send: recipient SID {to!r} is ARCHIVED and no live "
                f"session holds its window — refusing to write an inbox nobody "
                f"will ever drain (address a live SID, or a role keyword: "
                f"{', '.join(sorted(_is._ROLE_KEYWORDS))})"
            )

    result = _is.send(
        cfg, delivery_slug, params["from_sid"], to, params["text"],
        user=params.get("user"),
    )
    # T-0827: an over-cap message is refused whole rather than delivered
    # truncated, and the CLI caller is the one that can act on it. Raised
    # BEFORE the stall-watchdog hook and the TG mirror so a refused send marks
    # nothing and mirrors nothing — the sender must see one unambiguous
    # failure, not a 200 plus a partial side effect.
    if not result.get("ok", True):
        raise ActionError(f"peer_send: {result.get('error', 'refused')}")

    # T-0155: feed the stall-watchdog — a send to an operator-role session marks
    # the sender blocked on the stakeholder; an operator's send clears the
    # recipients' markers. Never let it break the bus write.
    try:
        from bot_squad_worker import tg_stall as _tg_stall
        _tg_stall.on_peer_send(
            cfg, params["slug"], params["from_sid"], result.get("delivered_to", []),
        )
    except Exception:  # noqa: BLE001
        log.exception("peer_send: tg_stall hook failed (non-fatal)")

    for recipient_sid in result.get("delivered_to", []):
        username = _parse_ui_sid_username(recipient_sid)
        if not username:
            continue
        chat_id = _resolve_user_tg_chat_id(cfg, username, slug=params.get("slug", ""))
        if not chat_id:
            log.debug(
                "peer_send tg-mirror: user %r has no tg_chat_id bound — skipping",
                username,
            )
            continue
        try:
            from bot_squad_worker import channels as _channels
            from bot_squad_worker import sessions as _sessions
            # T-0591 (F5.3): routed through the channel abstraction. This
            # mirror is deliberately TG-only regardless of the project's
            # configured channel (it targets a specific UI user's bound TG
            # chat, not the project's default), so the channel is forced
            # explicitly rather than selected via `project=`.
            _channels.get_channel(cfg, name="tg").send(
                params["text"],
                chat_id=chat_id,
                # T-0676 item 5: compact '<slug> <role>' style, alias-preferred.
                sid=_sessions.sid_display_label(
                    params["from_sid"], delivery_slug, compact=True, data_dir=cfg.data_dir,
                ),
                # T-0719: raw sender sid for the reply-map, so replying to a
                # mirrored peer message reaches the session that sent it.
                route_sid=str(params["from_sid"]),
                user=username,
            )
        except Exception:  # noqa: BLE001 — never let TG hiccups corrupt the bus reply
            log.exception(
                "peer_send tg-mirror: tg.send failed for user=%s recipient=%s",
                username, recipient_sid,
            )

    return result


_PEER_INBOX_READ_REQUIRED = {"slug", "sid"}
_PEER_INBOX_READ_ALLOWED = _PEER_INBOX_READ_REQUIRED


def _action_peer_inbox_read(params: dict[str, Any]) -> dict[str, Any]:
    """Drain new inbox lines since last read.

    Required params: slug, sid
    Returns: {ok: true, messages: [line, ...], count: N}
    """
    extra = set(params) - _PEER_INBOX_READ_ALLOWED
    if extra:
        raise ActionError(f"peer_inbox_read got unexpected params: {sorted(extra)}")
    missing = _PEER_INBOX_READ_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_inbox_read missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import intersession as _is
    return _is.inbox_read(cfg, params["slug"], params["sid"])


_TASK_PROGRESS_REQUIRED = {"slug", "task_id", "sid", "text"}
_TASK_PROGRESS_ALLOWED = _TASK_PROGRESS_REQUIRED


def _action_task_progress_add(params: dict[str, Any]) -> dict[str, Any]:
    """Append a short progress note to a backlog task's `## Progress` section.

    Required params: slug, task_id, sid, text
    Returns: {ok: true, task_id, line_appended}

    The task md is updated atomically (tmp + rename). The verbatim and
    context sections are preserved exactly. Text is sanitised (newlines
    collapsed to spaces); text over the task_body cap errors instead of
    silently truncating.
    """
    extra = set(params) - _TASK_PROGRESS_ALLOWED
    if extra:
        raise ActionError(f"task_progress_add got unexpected params: {sorted(extra)}")
    missing = _TASK_PROGRESS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"task_progress_add missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    task_id = params["task_id"]
    sid = params["sid"]
    text = params["text"]

    if not isinstance(text, str) or not text.strip():
        raise ActionError("task_progress_add: empty text")
    if cfg.projects.get(slug) is None:
        raise ActionError(f"task_progress_add: unknown project slug {slug!r}")

    backlog_dir: Path = cfg.data_dir / slug / "backlog"
    from bot_squad_worker import frontmatter as _fm
    try:
        # T-0231: resolve by id: frontmatter (strict) — a note must never land
        # on the wrong ticket because a stale filename happened to sort first.
        path = _fm.resolve_id_file(backlog_dir, task_id, strict=True)
    except _fm.AmbiguousIdError as e:
        raise ActionError(f"task_progress_add: {e}") from e
    if path is None:
        raise ActionError(f"task_progress_add: task not found: {task_id}")

    from datetime import datetime, timezone
    import re as _re
    from bot_squad_worker.task_body import append_progress, _sanitize_progress_text
    from bot_squad_worker.mdlock import task_lock, atomic_write

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # T-0373: lock the whole read→modify→write (and unique tmp via atomic_write)
    # so concurrent progress/comment adds (worker AND api) never lose each other.
    with task_lock(path):
        text_raw = path.read_text()
        fm_match = _re.match(r"\A---\n(.*?)\n---\n(.*)", text_raw, _re.DOTALL)
        if not fm_match:
            raise ActionError(f"task_progress_add: no frontmatter in {path}")
        fm_block = fm_match.group(1)
        body = fm_match.group(2).lstrip("\n")

        try:
            new_body = append_progress(body, ts, sid, text)
        except ValueError as e:
            raise ActionError(f"task_progress_add: {e}") from e

        # Update `updated:` in place (or append) without parsing YAML — a
        # line-based frontmatter patch.
        fm_lines = fm_block.splitlines()
        has_updated = False
        for i, ln in enumerate(fm_lines):
            if ln.lstrip().startswith("updated:"):
                fm_lines[i] = f"updated: {ts}"
                has_updated = True
                break
        if not has_updated:
            fm_lines.append(f"updated: {ts}")
        new_fm = "\n".join(fm_lines)

        atomic_write(path, f"---\n{new_fm}\n---\n\n{new_body}")

    line = f"- {ts} · {sid} · {_sanitize_progress_text(text)}"
    return {"ok": True, "task_id": task_id, "line_appended": line}


_TASK_DIGEST_ALLOWED = {"slug"}


def _action_task_digest(params: dict[str, Any]) -> dict[str, Any]:
    """Compose the short on-demand backlog digest (T-0589 direction #1b).

    The ATTENDANT's answer to a TG-thread "что по задачам": status counts +
    P1/P2 headlines with T-ids, built deterministically in
    ``task_chat.compose_digest`` so every ask yields the same short shape
    (conscious single-message cap per the T-0610 slim rule — the digest is
    conversational content the thread relay carries verbatim, so shortness is
    enforced at composition, not by the page cut). Read-only.

    Required params: slug. Returns ``{ok, text, counts}``.
    """
    extra = set(params) - _TASK_DIGEST_ALLOWED
    if extra:
        raise ActionError(f"task_digest got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("task_digest missing required param: slug")
    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"task_digest: unknown project slug {slug!r}")
    from bot_squad_worker import task_chat as _task_chat

    return {"ok": True, **_task_chat.compose_digest(cfg, slug)}


_ASSIGNMENT_WRITE_RESULT_REQUIRED = {"slug", "assignment_id", "content", "sid"}
# T-0464: optional ``kind`` selects the assignment implementer (task default,
# routine for routine-spawned sessions) — the ONE write-result seam serves both.
_ASSIGNMENT_WRITE_RESULT_ALLOWED = _ASSIGNMENT_WRITE_RESULT_REQUIRED | {"kind"}


def _action_assignment_write_result(params: dict[str, Any]) -> dict[str, Any]:
    """Write a session's RESULT back into its assignment artifact (T-0463, F1.1-d).

    The write-result primitive of the assignment interface — persists the
    work-product into an in-system artifact so it survives outside the disposable
    Claude jsonl. Backed by the ONE reusable ``Artifact`` seam (T-0467/T-0473
    extend it). Serves BOTH assignment kinds: ``kind=task`` (default) and
    ``kind=routine`` (T-0464, routine-spawned sessions).

    Required params: slug, assignment_id, content, sid
    Optional params: kind ("task" | "routine", default "task")
    Returns: {ok, assignment_id, kind, artifact_path, bytes_written}
    """
    extra = set(params) - _ASSIGNMENT_WRITE_RESULT_ALLOWED
    if extra:
        raise ActionError(f"assignment_write_result got unexpected params: {sorted(extra)}")
    missing = _ASSIGNMENT_WRITE_RESULT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"assignment_write_result missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    assignment_id = params["assignment_id"]
    content = params["content"]
    sid = params["sid"]
    kind = str(params.get("kind", "task") or "task").strip().lower()

    if not isinstance(content, str) or not content.strip():
        raise ActionError("assignment_write_result: empty content")
    if cfg.projects.get(slug) is None:
        raise ActionError(f"assignment_write_result: unknown project slug {slug!r}")

    from bot_squad_worker.assignment import for_routine, for_task

    if kind == "routine":
        assignment = for_routine(cfg.data_dir, slug, assignment_id)
    elif kind == "task":
        assignment = for_task(cfg.data_dir, slug, assignment_id)
    else:
        raise ActionError(f"assignment_write_result: unknown kind {kind!r} (task|routine)")
    try:
        art = assignment.write_result(content, sid=sid)
    except ValueError as e:
        raise ActionError(f"assignment_write_result: {e}") from e

    body = art.read()
    return {
        "ok": True,
        "assignment_id": assignment_id,
        "kind": assignment.kind,
        "artifact_path": str(art.path),
        "bytes_written": len(body.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# T-0464 / M1-F1.1: Routines — declare + list (the firing tick lives in
# routines.routine_tick, wired into scheduler.py). A Routine is the SECOND thing
# implementing the assignment interface: a declared rule (instruction + a
# schedule trigger) that spawns a session per due tick.
# ---------------------------------------------------------------------------

_ROUTINE_DECLARE_REQUIRED = {"slug", "instruction"}
_ROUTINE_DECLARE_ALLOWED = _ROUTINE_DECLARE_REQUIRED | {
    "title", "trigger", "provenance",
    # per-trigger spec — routines.declare enforces exactly-one: a cron string
    # for "schedule", the D-0048 §3.1 mapping (on_breach/on_recover inside)
    # for "monitor" (T-0604).
    "schedule", "monitor",
}


def _action_routine_declare(params: dict[str, Any]) -> dict[str, Any]:
    """Declare + persist a Routine in the project store (T-0464 / T-0604).

    Required params: slug, instruction
    Trigger spec: schedule (5-field cron) for trigger "schedule" (default);
                  monitor (mapping) for trigger "monitor"
    Optional params: title, provenance
    Returns: {ok, id, file_path, next_run_at}
    """
    extra = set(params) - _ROUTINE_DECLARE_ALLOWED
    if extra:
        raise ActionError(f"routine_declare got unexpected params: {sorted(extra)}")
    missing = _ROUTINE_DECLARE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"routine_declare missing required params: {sorted(missing)}")

    from bot_squad_worker import routines as _routines

    cfg = _get_config()
    try:
        return _routines.declare(
            cfg, params["slug"],
            instruction=params["instruction"],
            schedule=params.get("schedule"),
            title=params.get("title"),
            trigger=params.get("trigger", "schedule") or "schedule",
            monitor=params.get("monitor"),
            provenance=params.get("provenance"),
        )
    except _routines.RoutineError as e:
        raise ActionError(f"routine_declare: {e}") from e


_ROUTINE_LIST_REQUIRED = {"slug"}
_ROUTINE_LIST_ALLOWED = _ROUTINE_LIST_REQUIRED


def _action_routine_list(params: dict[str, Any]) -> dict[str, Any]:
    """List declared Routines for a project (T-0464). Returns {ok, routines:[...]}."""
    extra = set(params) - _ROUTINE_LIST_ALLOWED
    if extra:
        raise ActionError(f"routine_list got unexpected params: {sorted(extra)}")
    missing = _ROUTINE_LIST_REQUIRED - set(params)
    if missing:
        raise ActionError(f"routine_list missing required params: {sorted(missing)}")

    from bot_squad_worker import routines as _routines

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"routine_list: unknown project slug {slug!r}")
    return {"ok": True, "routines": _routines.list_routines(cfg, slug)}


_ROUTINE_MUTE_REQUIRED = {"slug", "rid", "duration_s"}
_ROUTINE_MUTE_ALLOWED = _ROUTINE_MUTE_REQUIRED | {"reason"}


def _action_routine_mute(params: dict[str, Any]) -> dict[str, Any]:
    """Mute a monitor routine for a duration (T-0604, linza mute semantics):
    it keeps probing, but never fires until the mute expires. duration_s == 0
    clears a standing mute; a nonzero mute requires a reason.

    Required params: slug, rid, duration_s
    Optional params: reason (mandatory when duration_s > 0)
    Returns: {ok, id, muted_until[, reason]}
    """
    extra = set(params) - _ROUTINE_MUTE_ALLOWED
    if extra:
        raise ActionError(f"routine_mute got unexpected params: {sorted(extra)}")
    missing = _ROUTINE_MUTE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"routine_mute missing required params: {sorted(missing)}")

    from bot_squad_worker import routines as _routines

    cfg = _get_config()
    try:
        return _routines.mute(
            cfg, params["slug"], params["rid"],
            duration_s=params["duration_s"],
            reason=params.get("reason"),
        )
    except _routines.RoutineError as e:
        raise ActionError(f"routine_mute: {e}") from e


_COMPACT_WRITE_STATE_REQUIRED = {"slug", "sid", "content"}
_COMPACT_WRITE_STATE_ALLOWED = _COMPACT_WRITE_STATE_REQUIRED


def _action_compact_write_state(params: dict[str, Any]) -> dict[str, Any]:
    """Write a session's full forward-state into its ROLE artifact (T-0467, F1.4).

    The "write everything down" half of the universal compact: on context-full /
    timeout the session is asked to dump its complete forward-state so a fresh
    incarnation can boot from it. Role-agnostic — unlike ``assignment_write_result``
    (which needs an assignment_id), this resolves the session's role + task from
    its session md so a *task-less* role (e.g. the operator) can save too. Backed
    by the SAME reusable ``Artifact`` seam (no second store).

    A dev's role artifact IS its T-0463 result sidecar; an operator's is the
    state-doc (``artifacts/operator-state.md``, schema = T-0473).

    Required params: slug, sid, content
    Returns: {ok, role, assignment_id, artifact_path, bytes_written}
    """
    extra = set(params) - _COMPACT_WRITE_STATE_ALLOWED
    if extra:
        raise ActionError(f"compact_write_state got unexpected params: {sorted(extra)}")
    missing = _COMPACT_WRITE_STATE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"compact_write_state missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    sid = params["sid"]
    content = params["content"]

    if not isinstance(content, str) or not content.strip():
        raise ActionError("compact_write_state: empty content")
    if cfg.projects.get(slug) is None:
        raise ActionError(f"compact_write_state: unknown project slug {slug!r}")

    from bot_squad_worker import sessions as _sessions
    from bot_squad_worker.assignment import compose_result_body, role_artifact

    meta = _sessions._read_session_metadata(
        _sessions._session_file(cfg.data_dir, slug, sid))
    if meta is None:
        raise ActionError(f"compact_write_state: no session md for sid {sid!r}")

    task_id = meta.get("task_id")
    role = meta.get("role") or _sessions._derive_role(
        meta.get("window"), task_id, meta.get("initiative"))

    art = role_artifact(cfg.data_dir, slug, role=role, sid=sid, task_id=task_id)
    if art is None:
        raise ActionError(
            f"compact_write_state: no role artifact for sid {sid!r} (role {role!r})")

    task_bound = bool(task_id) and task_id != "~"
    assignment_id = task_id if task_bound else (role or sid)
    kind = "task" if task_bound else (role or "compact")
    art.write(compose_result_body(assignment_id, kind, content, sid=sid))

    body = art.read()
    return {
        "ok": True,
        "role": role,
        "assignment_id": assignment_id,
        "artifact_path": str(art.path),
        "bytes_written": len(body.encode("utf-8")),
    }


_OPERATOR_STATE_DOC_REQUIRED = {"slug"}
_OPERATOR_STATE_DOC_ALLOWED = _OPERATOR_STATE_DOC_REQUIRED


def _action_operator_state_doc(params: dict[str, Any]) -> dict[str, Any]:
    """Read the operator's state-doc — the read-only transparency primitive
    (T-0473, M2-F2.1; M11-T4 consumes it later).

    The operator's role artifact is a FUTURE-FOCUSED project-management state
    document at the well-known path ``artifacts/operator-state.md`` (written via
    ``compact_write_state``). This action only READS it (never writes) and always
    returns the fillable schema template, so a fresh operator can boot from the
    doc or seed it from the scaffold.

    Required params: slug
    Returns: {ok, path, exists, content, template}
    """
    extra = set(params) - _OPERATOR_STATE_DOC_ALLOWED
    if extra:
        raise ActionError(f"operator_state_doc got unexpected params: {sorted(extra)}")
    missing = _OPERATOR_STATE_DOC_REQUIRED - set(params)
    if missing:
        raise ActionError(f"operator_state_doc missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"operator_state_doc: unknown project slug {slug!r}")

    from bot_squad_worker.assignment import (
        _ARTIFACTS_SUBDIR, OPERATOR_STATE_ARTIFACT, Artifact,
        operator_state_template)

    art = Artifact(cfg.data_dir / slug / _ARTIFACTS_SUBDIR / OPERATOR_STATE_ARTIFACT)
    return {
        "ok": True,
        "path": str(art.path),
        "exists": art.exists(),
        "content": art.read(),
        "template": operator_state_template(slug),
    }


# ---------------------------------------------------------------------------
# T-0522 (M2 follow-up to T-0474): user-facing operator re-drive pause toggle.
# Thin coordinator-side wrappers over the EXISTING operator_redrive helpers
# (pause/resume/is_paused) — the flag + its semantics live in that module
# (T-0474, TL-owned); these only expose the toggle to `bsq operator ...`.
# Mirrors the pause_deploys/resume_deploys pair.
# ---------------------------------------------------------------------------

_OPERATOR_PAUSE_REQUIRED = {"slug"}
_OPERATOR_PAUSE_ALLOWED = _OPERATOR_PAUSE_REQUIRED | {"reason", "requested_by"}


def _action_operator_pause(params: dict[str, Any]) -> dict[str, Any]:
    """Pause the operator re-drive for a project — the user-facing toggle that
    stops the 60s re-drive tick from respawning the operator (T-0474).

    Wraps ``operator_redrive.pause`` (presence of ``operator_paused.flag`` =
    paused); idempotent. Required params: slug. Optional: reason, requested_by.
    Returns: {ok, paused: <meta>, was_already_paused: bool}.
    """
    extra = set(params) - _OPERATOR_PAUSE_ALLOWED
    if extra:
        raise ActionError(f"operator_pause got unexpected params: {sorted(extra)}")
    missing = _OPERATOR_PAUSE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"operator_pause missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"operator_pause: unknown project slug {slug!r}")

    from bot_squad_worker import operator_redrive as _ord
    was_already_paused = _ord.is_paused(cfg, slug)
    meta = _ord.pause(
        cfg, slug,
        by=params.get("requested_by") or "user",
        reason=params.get("reason") or "",
    )
    return {"ok": True, "paused": meta, "was_already_paused": was_already_paused}


_OPERATOR_RESUME_REQUIRED = {"slug"}
_OPERATOR_RESUME_ALLOWED = _OPERATOR_RESUME_REQUIRED | {"requested_by"}


def _action_operator_resume(params: dict[str, Any]) -> dict[str, Any]:
    """Resume a paused operator re-drive. No-op (idempotent) if not paused.

    Wraps ``operator_redrive.resume``. Required params: slug. Optional:
    requested_by. Returns: {ok, was_paused: bool}.
    """
    extra = set(params) - _OPERATOR_RESUME_ALLOWED
    if extra:
        raise ActionError(f"operator_resume got unexpected params: {sorted(extra)}")
    missing = _OPERATOR_RESUME_REQUIRED - set(params)
    if missing:
        raise ActionError(f"operator_resume missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"operator_resume: unknown project slug {slug!r}")

    from bot_squad_worker import operator_redrive as _ord
    was_paused = _ord.resume(cfg, slug)
    return {"ok": True, "was_paused": was_paused}


_OPERATOR_STATUS_REQUIRED = {"slug"}
_OPERATOR_STATUS_ALLOWED = _OPERATOR_STATUS_REQUIRED


def _action_operator_status(params: dict[str, Any]) -> dict[str, Any]:
    """Report the operator re-drive state for a project: paused-vs-driving.

    Composed READ-only from the PUBLIC operator_redrive helpers (``is_paused`` +
    ``count_pending_backlog``) and the one-operator detector
    (``dispatch.live_operator_sids``) — no write, no edit to the T-0474 module.

    ``state`` is the one-word steer:
      * ``paused``             — user paused; re-drive is off.
      * ``driving``            — an operator is currently live.
      * ``pending-redrive``    — backlog has work but no live operator (between
                                 re-drives / waiting on spawn capacity).
      * ``idle-empty-backlog`` — nothing to clear; the only idle state.

    Required params: slug. Returns: {ok, paused, state, live_operators,
    pending_backlog}.
    """
    extra = set(params) - _OPERATOR_STATUS_ALLOWED
    if extra:
        raise ActionError(f"operator_status got unexpected params: {sorted(extra)}")
    missing = _OPERATOR_STATUS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"operator_status missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"operator_status: unknown project slug {slug!r}")

    from bot_squad_worker import operator_redrive as _ord
    from bot_squad_worker import dispatch as _dispatch

    paused = _ord.is_paused(cfg, slug)
    live = _dispatch.live_operator_sids(cfg, slug)
    pending = _ord.count_pending_backlog(cfg, slug)

    if paused:
        state = "paused"
    elif live:
        state = "driving"
    elif pending > 0:
        state = "pending-redrive"
    else:
        state = "idle-empty-backlog"

    return {
        "ok": True,
        "paused": paused,
        "state": state,
        "live_operators": live,
        "pending_backlog": pending,
    }


_PICKUP_QUEUE_REQUIRED = {"slug"}
_PICKUP_QUEUE_ALLOWED = _PICKUP_QUEUE_REQUIRED | {"band"}


def _action_pickup_queue(params: dict[str, Any]) -> dict[str, Any]:
    """The banded pickup queue for a project — what is takeable, what needs
    triage, and what is excluded and why (T-0783a).

    Read-only scan of the shared backlog + session mds, composed from the PUBLIC
    ``pickup`` helpers. Answers the question ``operator_status``'s
    ``pending_backlog`` count cannot: not "is there work" but "WHICH work is
    takeable right now" — the gap that left a reopened P1 sitting until the
    stakeholder dispatched it by hand.

    Required params: slug. Optional: band (``pickup``/``triage``/``excluded`` —
    return only that band; the full result is large on a mature board, and
    ``excluded`` is ~730 rows of closed tickets). Returns the
    :func:`pickup.pickup_queue` result plus ``brief`` (the operator-facing
    rendering, so the CLI and the re-drive show the SAME text).
    """
    extra = set(params) - _PICKUP_QUEUE_ALLOWED
    if extra:
        raise ActionError(f"pickup_queue got unexpected params: {sorted(extra)}")
    missing = _PICKUP_QUEUE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"pickup_queue missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"pickup_queue: unknown project slug {slug!r}")

    from bot_squad_worker import pickup as _pickup

    band = str(params.get("band") or "").strip().lower()
    bands = (_pickup.BAND_PICKUP, _pickup.BAND_TRIAGE, _pickup.BAND_EXCLUDED)
    if band and band not in bands:
        raise ActionError(f"pickup_queue: band must be one of {list(bands)}, got {band!r}")

    out = _pickup.pickup_queue(cfg, slug)
    out["brief"] = _pickup.pickup_brief(out)
    if band:
        for other in bands:
            if other != band:
                out.pop(other, None)
    return out


# ---------------------------------------------------------------------------
# T-0630 (T-0620 seam): fleet-default `claude --model` (~/.claude/settings.json
# `model` key of the worker linux user). Thin wrappers over fleet_model.py —
# the API container has no filesystem access to write this itself.
# ---------------------------------------------------------------------------

def _action_fleet_model_get(params: dict[str, Any]) -> dict[str, Any]:
    """Read the fleet-default model. No params. Returns: {ok, model}
    ("" if unset — the built-in claude default applies)."""
    if params:
        raise ActionError(f"fleet_model_get got unexpected params: {sorted(params)}")

    from bot_squad_worker import fleet_model
    from bot_squad_worker import sessions as _sessions
    cfg = _get_config()
    return {
        "ok": True,
        "model": fleet_model.get_model(_sessions._caps_config_dir(cfg)),
    }


_FLEET_MODEL_SET_REQUIRED = {"model"}


def _action_fleet_model_set(params: dict[str, Any]) -> dict[str, Any]:
    """Set (or, for ``model=""``, clear) the fleet-default model — atomic
    read-modify-write of ~/.claude/settings.json preserving every other key.
    Required: model (one of fleet_model.ALLOWED_MODELS). Returns: {ok, model}."""
    extra = set(params) - _FLEET_MODEL_SET_REQUIRED
    if extra:
        raise ActionError(f"fleet_model_set got unexpected params: {sorted(extra)}")
    missing = _FLEET_MODEL_SET_REQUIRED - set(params)
    if missing:
        raise ActionError(f"fleet_model_set missing required params: {sorted(missing)}")

    from bot_squad_worker import fleet_model
    from bot_squad_worker import sessions as _sessions
    cfg = _get_config()
    model = params["model"]
    try:
        fleet_model.set_model(model, _sessions._caps_config_dir(cfg))
    except ValueError as e:
        raise ActionError(str(e)) from e
    return {"ok": True, "model": model}


_TASK_NEW_REQUIRED = {"slug", "title"}
# T-0519: ``provenance`` is accepted (and required at the gate below) so a direct
# caller of this action can no longer mint a sourceless ticket. Kept out of
# _REQUIRED so the gate can raise a specific provenance error instead of the
# generic "missing required params" one.
_TASK_NEW_ALLOWED = _TASK_NEW_REQUIRED | {
    "initiative", "priority", "owner", "provenance",
    # T-0577: dedupe-vs-create gate additions. `verbatim` is signal-only (it
    # widens the similarity query so a bare, generic title still catches a
    # near-duplicate whose distinctive words live in the caller's verbatim
    # text) — it is NOT stored on the ticket; the existing task_new body stays
    # the stub placeholder, unchanged. `force` bypasses the gate outright.
    "verbatim", "force",
}
_TASK_NEW_TITLE_MAX = 240
# T-0577: dedupe gate tuning. A query with fewer than this many distinct
# meaningful tokens (task_search.tokenize) is never flagged — mirrors
# task_search's own duplicate-eligibility floor: a 1-token query has 100%
# coverage on any hit trivially, which is noise, not a real duplicate signal.
_TASK_DEDUPE_MIN_TOKENS = 2
# Cap on how many coverage-filtered candidates go into the reject message —
# applied AFTER ranking+filtering (never before: capping the ranked list
# first can crowd out a lower-scored candidate that actually clears the
# coverage threshold, see the recall-gap fix in _task_new_similar_backlog).
# Keeps a single reject message short and readable.
_TASK_DEDUPE_CANDIDATE_LIMIT = 5


def normalize_id(value: str) -> str:
    """T-0424 contract (byte-identical to ``api.app.routes_feedback.normalize_id``
    and the TS mirror in T-0425): strip EXACTLY ONE trailing literal lowercase
    ``.md``. An entity id never carries its file suffix — compare/key on the
    stem; case-sensitive (never ``.MD``); not greedy (``x.md.md`` → ``x.md``); no
    trimming. To hit a FILE, re-add the suffix: ``f"{normalize_id(x)}.md"``."""
    return value[:-3] if value.endswith(".md") else value


def _slugify_title(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return (s[:60].rstrip("-") or "task")


def _yaml_quote(s: str) -> str:
    """Return a YAML scalar that is safe even when ``s`` contains : # " etc.

    Always emits double-quoted form with JSON-style escaping, which is a
    valid subset of YAML 1.2 plain double-quoted strings.
    """
    import json as _json
    return _json.dumps(s, ensure_ascii=False)


def _atomic_write_new(path: Path, content: str) -> None:
    """O_EXCL write of a freshly-allocated entity file.

    Belt-and-braces over the allocator's flock: if the filesystem already
    has a file with this exact name we surface that rather than overwrite,
    and on any write failure we don't leave a half-written file squatting
    on the id we just allocated.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except BaseException:
        try:
            os.unlink(str(path))
        except FileNotFoundError:
            pass
        raise


def _task_new_similar_backlog(cfg: Any, slug: str, query: str) -> list[Any]:
    """T-0577 dedupe gate: rank ``slug``'s existing backlog tasks against
    ``query`` (the new ticket's title, optionally + verbatim text) via the
    ``task_search`` ranker, and return the ranked ``Candidate``s whose
    COVERAGE — the fraction of ``query``'s distinct meaningful tokens already
    found in that candidate's title+body — is at/above
    ``cfg.tasks_dedupe_threshold``.

    Read-only, best-effort: a missing/empty backlog dir or an unparsable
    ticket file just drops out of consideration (never blocks a mint on an I/O
    hiccup). Below ``_TASK_DEDUPE_MIN_TOKENS`` distinct query tokens, nothing
    is ever flagged (a 1-token query trivially "covers" 100% of any hit).
    """
    from bot_squad_worker import frontmatter as _fm
    from bot_squad_worker import task_search as _ts

    tokens = _ts.tokenize(query)
    if len(tokens) < _TASK_DEDUPE_MIN_TOKENS:
        return []

    backlog_dir: Path = cfg.data_dir / slug / "backlog"
    if not backlog_dir.exists():
        return []

    tickets: list[Any] = []
    for md in sorted(backlog_dir.glob("T-*.md")):
        try:
            raw = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = _fm.parse_or_none(raw)
        if not parsed:
            continue
        meta, body = parsed
        meta = meta or {}
        status = str(meta.get("status") or "")
        if status == "closed":
            # A closed ticket is done — re-filing a regression of it (or a
            # fresh, unrelated ask that happens to share vocabulary with an
            # old closed ticket) must not need --force. Only OPEN backlog
            # entries are live near-duplicate risk.
            continue
        stem_parts = md.stem.split("-", 2)
        fallback_id = "-".join(stem_parts[:2]) if len(stem_parts) >= 2 else md.stem
        tickets.append(_ts.Ticket(
            id=str(meta.get("id") or fallback_id).strip(),
            title=str(meta.get("title") or md.stem),
            body=body,
            status=status,
        ))
    if not tickets:
        return []

    threshold = cfg.tasks_dedupe_threshold
    out = []
    # Rank UNBOUNDED (limit=len(tickets)) so the title-weighted score never
    # crowds a body-only near-dupe out before it reaches the coverage filter,
    # THEN filter by coverage, THEN cap the listing — capping first (the old
    # behaviour) could drop the one candidate that actually clears threshold
    # in favour of five higher-scored-but-irrelevant ones (recall gap).
    for c in _ts.rank(query, tickets, limit=len(tickets)):
        coverage = len(c.matched) / len(tokens)
        if coverage >= threshold:
            out.append(c)
    return out[:_TASK_DEDUPE_CANDIDATE_LIMIT]


def _action_task_new(params: dict[str, Any]) -> dict[str, Any]:
    """Atomically allocate the next T-NNNN id and write a stub task md.

    Required params: slug, title
    Optional params: initiative, priority, owner, verbatim, force
    Returns: {ok: true, id: "T-NNNN", file_path: "<abs path>"}

    Allocation goes through the shared ``idalloc`` allocator (T-0174), which
    serialises on ``data/<slug>/_counters/task.txt`` — the SAME counter the
    API's ``POST /backlog`` uses, so a web create and an agent ``task new``
    can no longer hand out the same id (they used to lock different files).
    Crashes between alloc and write merely burn one id — fine, ids aren't
    scarce.

    T-0577 dedupe-vs-create gate: before minting, the (title [+ verbatim])
    text is ranked against the project's existing backlog via
    ``task_search.rank()``. A top match at/above ``cfg.tasks_dedupe_threshold``
    coverage raises ``ActionError`` instead of minting — carrying the similar
    task id(s)+title(s) and the retry recipe (``force: true`` /
    ``bsq task new --force``) so the reject is never silent. Pass
    ``force: true`` to bypass (the caller has already judged the ask distinct).
    """
    extra = set(params) - _TASK_NEW_ALLOWED
    if extra:
        raise ActionError(f"task_new got unexpected params: {sorted(extra)}")
    missing = _TASK_NEW_REQUIRED - set(params)
    if missing:
        raise ActionError(f"task_new missing required params: {sorted(missing)}")

    slug = params["slug"]
    title = params["title"]
    if not isinstance(title, str) or not title.strip():
        raise ActionError("task_new: empty title")
    if "\n" in title or "\r" in title:
        raise ActionError("task_new: title must be single-line")
    if len(title) > _TASK_NEW_TITLE_MAX:
        raise ActionError(f"task_new: title too long (max {_TASK_NEW_TITLE_MAX})")

    # T-0519: provenance gate. New tickets are always post-cutoff, so provenance
    # is always required here (the 95 pre-cutoff tickets stay grandfathered by
    # the lint's cutoff, untouched). Validate against the canonical grammar
    # mirror so a direct action caller gets the same gate as `bsq task new`.
    from bot_squad_worker import provenance as _prov
    prov = params.get("provenance")
    prov = str(prov).strip() if prov is not None else ""
    if not prov:
        raise ActionError(
            f"task_new requires provenance (cite the source) — allowed: {_prov.ALLOWED_HELP}"
        )
    if not _prov.provenance_valid(prov):
        raise ActionError(
            f"task_new: invalid provenance {prov!r} — allowed: {_prov.ALLOWED_HELP}"
        )

    cfg = _get_config()
    if cfg.projects.get(slug) is None:
        raise ActionError(f"task_new: unknown project slug {slug!r}")

    # T-0577: dedupe-vs-create gate. task_search.py (ranking of similar backlog
    # tasks) previously had zero ingest callers (D-0047) — every TG/voice-driven
    # task_new minted unconditionally, so a firehose-driven backlog proliferated
    # near-duplicate tickets. Rank the new ask against the existing backlog and
    # reject (rather than silently mint) when it looks like a near-duplicate.
    force = params.get("force", False)
    if not isinstance(force, bool):
        raise ActionError("task_new: force must be a boolean")
    if not force:
        verbatim = params.get("verbatim")
        dedupe_query = title
        if isinstance(verbatim, str) and verbatim.strip():
            dedupe_query = f"{title}\n{verbatim}"
        # Fail OPEN, not closed: the gate is a courtesy dedupe check, not a
        # security boundary — an unexpected error here (PermissionError from
        # glob iteration, a bug in task_search.rank, a malformed backlog
        # file) must never block a legitimate mint. Log and proceed as if
        # nothing similar was found.
        try:
            similar = _task_new_similar_backlog(cfg, slug, dedupe_query)
        except Exception:
            log.exception(
                "task_new: dedupe gate raised for slug=%r — failing OPEN (minting without a dedupe check)",
                slug,
            )
            similar = []
        if similar:
            listing = "; ".join(f"{c.id} {c.title!r}" for c in similar)
            raise ActionError(
                "task_new: rejected — looks like a near-duplicate of existing "
                f"backlog task(s): {listing}. If this is genuinely a new, "
                "distinct task, retry with force:true (bsq: "
                "`bsq task new ... --force`)."
            )

    from datetime import datetime, timezone
    from bot_squad_worker import idalloc

    backlog_dir: Path = cfg.data_dir / slug / "backlog"
    backlog_dir.mkdir(parents=True, exist_ok=True)

    new_id = idalloc.allocate_id(cfg.data_dir, slug, "task")
    file_path = backlog_dir / f"{new_id}-{_slugify_title(title)}.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fm_lines = [
        f"id: {new_id}",
        f"title: {_yaml_quote(title)}",
        "status: planned",
        f"created: {ts}",
        f"provenance: {prov}",  # T-0519: validated above; stamp at creation
    ]
    for opt_key in ("initiative", "priority", "owner"):
        if opt_key in params:
            val = params[opt_key]
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            sval = str(val)
            if opt_key == "initiative":
                # T-0424 canonicalize-on-store: persist the .md FILE form so a
                # bare stem matches the initiative file + the FE option.basename
                # (no false orphan). normalize_id strips one trailing .md, then
                # we re-add it: both 'ui-polish' and 'ui-polish.md' → 'ui-polish.md'.
                sval = f"{normalize_id(sval.strip())}.md"
            fm_lines.append(f"{opt_key}: {_yaml_quote(sval)}")

    body = (
        "## Verbatim request\n\n"
        "(filed via task_new)\n\n"
        "## DoD\n\n"
        "TBD\n"
    )
    content = "---\n" + "\n".join(fm_lines) + f"\n---\n\n{body}"
    _atomic_write_new(file_path, content)

    return {"ok": True, "id": new_id, "file_path": str(file_path)}


# ---------------------------------------------------------------------------
# T-0174: generalized entity_new actions (doc / initiative).
# All wrap the same idalloc allocator + _atomic_write_new helper as task_new,
# so every entity type gets collision-free ids from a per-type counter.
# ---------------------------------------------------------------------------

_ENTITY_TITLE_MAX = 240
_DOC_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _require_str(params: dict[str, Any], key: str, action: str) -> str:
    val = params.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ActionError(f"{action}: empty or missing {key!r}")
    if "\n" in val or "\r" in val:
        raise ActionError(f"{action}: {key!r} must be single-line")
    if len(val) > _ENTITY_TITLE_MAX:
        raise ActionError(f"{action}: {key!r} too long (max {_ENTITY_TITLE_MAX})")
    return val.strip()


def _entity_setup(params: dict[str, Any], required: set, allowed: set, action: str):
    """Shared param-guard + config/slug resolution for the entity_new actions."""
    extra = set(params) - allowed
    if extra:
        raise ActionError(f"{action} got unexpected params: {sorted(extra)}")
    missing = required - set(params)
    if missing:
        raise ActionError(f"{action} missing required params: {sorted(missing)}")
    slug = params["slug"]
    cfg = _get_config()
    if cfg.projects.get(slug) is None:
        raise ActionError(f"{action}: unknown project slug {slug!r}")
    return cfg, slug


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DOC_NEW_REQUIRED = {"slug", "category", "title"}
# T-0290 (a): `parent_doc_id` was the one thing the web create accepted and the
# agent path did not, so `bsq doc new` could not author the nested tree the UI
# renders. Optional — a doc with no parent is still a root doc.
_DOC_NEW_ALLOWED = _DOC_NEW_REQUIRED | {"parent_doc_id"}


def _action_doc_new(params: dict[str, Any]) -> dict[str, Any]:
    """Allocate the next D-NNNN id and write a stub doc md (composes T-0172).

    Required params: slug, category, title
    Optional: parent_doc_id (T-0290) — the mother artifact the new doc nests
      under. Cross-store (T-0283): any artifact ``artifact_nesting`` walks, so
      a D-NNNN doc or a feedback file stem.
    Returns: {ok, id, file_path, category, parent_doc_id}

    Storage: ``data/<slug>/docs/<category>/D-NNNN-<slug>.md``. Category must be
    a safe dir token (lowercase, ``[a-z0-9_-]``); the T-0172 docs system gives
    it semantic meaning (product/architecture/design/support/runbook, …).
    """
    from bot_squad_worker import idalloc

    cfg, slug = _entity_setup(params, _DOC_NEW_REQUIRED, _DOC_NEW_ALLOWED, "doc_new")
    category = _require_str(params, "category", "doc_new")
    if not _DOC_CATEGORY_RE.match(category):
        raise ActionError(
            f"doc_new: invalid category {category!r} (expect lowercase [a-z0-9_-])"
        )
    title = _require_str(params, "title", "doc_new")

    # T-0290 (a): the parent must already EXIST, and may live in any store —
    # the same rule routes_docs.create_doc applies, decided by the same
    # cross-store resolver (artifact_nesting is a declared mirror pair, so the
    # two paths cannot drift into disagreeing about what a valid parent is).
    #
    # That existence check IS the self-parent/cycle rejection: the child's id
    # is allocated BELOW, after the check, so a doc being created cannot name
    # itself (its id does not exist yet) and cannot close a loop (every
    # ancestor of an existing parent predates it).
    parent_doc_id = params.get("parent_doc_id")
    parent_doc_id = (str(parent_doc_id).strip() or None) if parent_doc_id is not None else None
    if parent_doc_id is not None:
        from bot_squad_worker import artifact_nesting

        if artifact_nesting.find_artifact(cfg.data_dir / slug, parent_doc_id) is None:
            raise ActionError(f"doc_new: parent artifact not found: {parent_doc_id!r}")

    docs_dir = cfg.data_dir / slug / "docs" / category
    docs_dir.mkdir(parents=True, exist_ok=True)
    new_id = idalloc.allocate_id(cfg.data_dir, slug, "doc")
    file_path = docs_dir / f"{new_id}-{_slugify_title(title)}.md"

    fm_lines = [
        f"id: {new_id}",
        f"title: {_yaml_quote(title)}",
        f"category: {category}",
        "status: draft",
        f"created: {_now_iso()}",
        "related_tickets: []",
    ]
    if parent_doc_id is not None:
        fm_lines.append(f"parent_doc_id: {parent_doc_id}")
    fm = "\n".join(fm_lines)
    content = f"---\n{fm}\n---\n\n# {title}\n\n(filed via doc_new — T-0172 docs system)\n"
    _atomic_write_new(file_path, content)
    return {"ok": True, "id": new_id, "file_path": str(file_path), "category": category,
            "parent_doc_id": parent_doc_id}


_INITIATIVE_NEW_REQUIRED = {"slug", "name"}
_INITIATIVE_NEW_ALLOWED = _INITIATIVE_NEW_REQUIRED | {"provenance", "persistent"}


def _action_initiative_new(params: dict[str, Any]) -> dict[str, Any]:
    """Allocate the next T-NNNN id and write a stub ``kind: initiative`` task.

    Required params: slug, name
    Optional: persistent (bool) — T-0354: standing responsibility vs the
      default one-shot; sets ``initiative_kind: persistent`` on the stub.
    Returns: {ok, id, kind, file_path}

    Storage: ``data/<slug>/backlog/T-NNNN-<slug>.md`` (T-0480 3b-1 — an
    initiative IS a task now, not a separate ``vision/initiatives/`` entity).
    """
    from bot_squad_worker import idalloc

    cfg, slug = _entity_setup(
        params, _INITIATIVE_NEW_REQUIRED, _INITIATIVE_NEW_ALLOWED, "initiative_new"
    )
    name = _require_str(params, "name", "initiative_new")

    # T-0480 3b-1: an initiative IS a task marked kind:initiative — mint a
    # kind:initiative TASK in the backlog, not a legacy vision/initiatives file.
    # Provenance: an initiative is stakeholder-directed; accept an explicit
    # token, else stamp stakeholder:<today> (post-cutoff task → lint needs one).
    prov = params.get("provenance")
    prov = str(prov).strip() if prov is not None else ""
    if prov:
        from bot_squad_worker import provenance as _prov
        if not _prov.provenance_valid(prov):
            raise ActionError(
                f"initiative_new: invalid provenance {prov!r} — allowed: {_prov.ALLOWED_HELP}"
            )

    ts = _now_iso()
    if not prov:
        prov = f"stakeholder:{ts[:10]}"

    backlog_dir = cfg.data_dir / slug / "backlog"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    new_id = idalloc.allocate_id(cfg.data_dir, slug, "task")
    stem = _slugify_title(name)
    file_path = backlog_dir / f"{new_id}-{stem}.md"

    fm_lines = [
        f"id: {new_id}",
        f"title: {_yaml_quote(name)}",
        "status: open",
        "kind: initiative",
        "priority: 0",
        f"created: {ts}",
        f"provenance: {prov}",
        f"aka: [{stem}]",
    ]
    persistent = bool(params.get("persistent"))
    if persistent:
        # T-0354: omitted entirely for the one-shot (default) case, so the
        # stub for the common path stays byte-identical to before this.
        fm_lines.append("initiative_kind: persistent")
    fm = "\n".join(fm_lines)
    content = f"---\n{fm}\n---\n\n# {name}\n\n(filed via initiative_new)\n"
    _atomic_write_new(file_path, content)
    result = {"ok": True, "id": new_id, "kind": "initiative", "file_path": str(file_path)}
    if persistent:
        result["initiative_kind"] = "persistent"
    return result


_PEER_INBOX_WAIT_REQUIRED = {"slug", "sid", "timeout"}
_PEER_INBOX_WAIT_ALLOWED = _PEER_INBOX_WAIT_REQUIRED


def _action_peer_inbox_wait(params: dict[str, Any]) -> dict[str, Any]:
    """Long-poll until inbox grows past the seen offset, or timeout.

    Required params: slug, sid, timeout (seconds, capped at 7200 / 2h — T-0091)
    Returns: {ok: true, ready: bool, elapsed_sec: float}
    """
    extra = set(params) - _PEER_INBOX_WAIT_ALLOWED
    if extra:
        raise ActionError(f"peer_inbox_wait got unexpected params: {sorted(extra)}")
    missing = _PEER_INBOX_WAIT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_inbox_wait missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import intersession as _is
    return _is.inbox_wait(cfg, params["slug"], params["sid"], params["timeout"])


# ---------------------------------------------------------------------------
# T-0498 (M6/F6.2): synchronous inter-session channel — request -> ack ->
# enter -> live 2-way send, closing on exit/timeout. A thin handshake layer on
# top of the verified async substrate (D-0037); each action maps to one
# sync_channel function. SyncError is surfaced as an ActionError so the CLI
# gets a clean 4xx instead of a 500.
# ---------------------------------------------------------------------------

def _sync_call(fn, *args, **kwargs) -> dict[str, Any]:
    from bot_squad_worker import sync_channel as _sc
    try:
        return fn(_sc, *args, **kwargs)
    except _sc.SyncError as e:
        raise ActionError(str(e)) from e


_SYNC_REQUEST_REQUIRED = {"slug", "from_sid", "to"}
_SYNC_REQUEST_ALLOWED = _SYNC_REQUEST_REQUIRED | {"reason", "ttl"}


def _action_sync_request(params: dict[str, Any]) -> dict[str, Any]:
    """Request a synchronous channel with another session.

    Required params: slug, from_sid, to
    Optional params: reason, ttl (idle seconds; default 3600)
    Returns: {ok, channel_id, status, requester, responder, notify}
    """
    extra = set(params) - _SYNC_REQUEST_ALLOWED
    if extra:
        raise ActionError(f"sync_request got unexpected params: {sorted(extra)}")
    missing = _SYNC_REQUEST_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sync_request missing required params: {sorted(missing)}")

    cfg = _get_config()
    kw: dict[str, Any] = {}
    if params.get("reason"):
        kw["reason"] = params["reason"]
    if params.get("ttl") is not None:
        kw["ttl"] = float(params["ttl"])
    return _sync_call(
        lambda sc: sc.request(cfg, params["slug"], params["from_sid"], params["to"], **kw)
    )


_SYNC_ACK_REQUIRED = {"slug", "sid", "channel_id"}
_SYNC_ACK_ALLOWED = _SYNC_ACK_REQUIRED


def _action_sync_ack(params: dict[str, Any]) -> dict[str, Any]:
    """Ack a pending sync-channel request (responder only).

    Required params: slug, sid, channel_id
    Returns: {ok, status, notify, ...}
    """
    extra = set(params) - _SYNC_ACK_ALLOWED
    if extra:
        raise ActionError(f"sync_ack got unexpected params: {sorted(extra)}")
    missing = _SYNC_ACK_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sync_ack missing required params: {sorted(missing)}")

    cfg = _get_config()
    return _sync_call(
        lambda sc: sc.ack(cfg, params["slug"], params["sid"], params["channel_id"])
    )


_SYNC_ENTER_REQUIRED = {"slug", "sid", "channel_id"}
_SYNC_ENTER_ALLOWED = _SYNC_ENTER_REQUIRED


def _action_sync_enter(params: dict[str, Any]) -> dict[str, Any]:
    """Enter an acked sync channel (opens once both members enter).

    Required params: slug, sid, channel_id
    Returns: {ok, status, both_in, peer, notify, ...}
    """
    extra = set(params) - _SYNC_ENTER_ALLOWED
    if extra:
        raise ActionError(f"sync_enter got unexpected params: {sorted(extra)}")
    missing = _SYNC_ENTER_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sync_enter missing required params: {sorted(missing)}")

    cfg = _get_config()
    return _sync_call(
        lambda sc: sc.enter(cfg, params["slug"], params["sid"], params["channel_id"])
    )


_SYNC_SEND_REQUIRED = {"slug", "sid", "channel_id", "text"}
_SYNC_SEND_ALLOWED = _SYNC_SEND_REQUIRED


def _action_sync_send(params: dict[str, Any]) -> dict[str, Any]:
    """Send a live message to the other member of an open sync channel.

    Required params: slug, sid, channel_id, text
    Returns: {ok, peer, line, notify, ...}
    """
    extra = set(params) - _SYNC_SEND_ALLOWED
    if extra:
        raise ActionError(f"sync_send got unexpected params: {sorted(extra)}")
    missing = _SYNC_SEND_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sync_send missing required params: {sorted(missing)}")

    cfg = _get_config()
    return _sync_call(
        lambda sc: sc.send(
            cfg, params["slug"], params["sid"], params["channel_id"], params["text"]
        )
    )


_SYNC_EXIT_REQUIRED = {"slug", "sid", "channel_id"}
_SYNC_EXIT_ALLOWED = _SYNC_EXIT_REQUIRED | {"reason"}


def _action_sync_exit(params: dict[str, Any]) -> dict[str, Any]:
    """Leave a sync channel — closes it and notifies the peer.

    Required params: slug, sid, channel_id
    Optional params: reason
    Returns: {ok, status, closed_reason, notify, ...}
    """
    extra = set(params) - _SYNC_EXIT_ALLOWED
    if extra:
        raise ActionError(f"sync_exit got unexpected params: {sorted(extra)}")
    missing = _SYNC_EXIT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sync_exit missing required params: {sorted(missing)}")

    cfg = _get_config()
    kw: dict[str, Any] = {}
    if params.get("reason"):
        kw["reason"] = params["reason"]
    return _sync_call(
        lambda sc: sc.exit_channel(
            cfg, params["slug"], params["sid"], params["channel_id"], **kw
        )
    )


_SYNC_STATUS_REQUIRED = {"slug"}
_SYNC_STATUS_ALLOWED = _SYNC_STATUS_REQUIRED | {"channel_id", "sid"}


def _action_sync_status(params: dict[str, Any]) -> dict[str, Any]:
    """Report a sync channel by id, or every channel a sid is in.

    Required params: slug
    Optional params: channel_id (one channel) or sid (list channels for a sid)
    Returns: {ok, ...channel} or {ok, channels: [...]}
    """
    extra = set(params) - _SYNC_STATUS_ALLOWED
    if extra:
        raise ActionError(f"sync_status got unexpected params: {sorted(extra)}")
    missing = _SYNC_STATUS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sync_status missing required params: {sorted(missing)}")

    cfg = _get_config()
    return _sync_call(
        lambda sc: sc.status(
            cfg, params["slug"],
            channel_id=params.get("channel_id"),
            sid=params.get("sid"),
        )
    )


# ---------------------------------------------------------------------------
# Phase 9: bind_task / bind_initiative — append to a session's extras
# ---------------------------------------------------------------------------

_BIND_TASK_REQUIRED = {"slug", "sid", "task_id"}
_BIND_TASK_ALLOWED = _BIND_TASK_REQUIRED


def _action_bind_task(params: dict[str, Any]) -> dict[str, Any]:
    """Bind another task to an already-running dev session.

    Required params: slug, sid, task_id
    Returns: {ok, sid, task_id, extras: [...full extra list...]}
    """
    extra = set(params) - _BIND_TASK_ALLOWED
    if extra:
        raise ActionError(f"bind_task got unexpected params: {sorted(extra)}")
    missing = _BIND_TASK_REQUIRED - set(params)
    if missing:
        raise ActionError(f"bind_task missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.bind_task(cfg, params["slug"], params["sid"], params["task_id"])


_DISPATCH_DECISION_REQUIRED = {"slug", "task_id"}
_DISPATCH_DECISION_ALLOWED = _DISPATCH_DECISION_REQUIRED


def _action_dispatch_decision(params: dict[str, Any]) -> dict[str, Any]:
    """T-0237 Layer-2 v1: recommend reuse-vs-spawn for an unbound task.

    Required params: slug, task_id. Returns the decision record
    ``{ok, decision: 'reuse'|'spawn', target_sid, reason, candidates: [...]}``.
    Advisory only — the operator/TL acts on the recommendation via the existing
    spawn / resume paths; this never spawns on its own (operator fork S2).
    """
    extra = set(params) - _DISPATCH_DECISION_ALLOWED
    if extra:
        raise ActionError(f"dispatch_decision got unexpected params: {sorted(extra)}")
    missing = _DISPATCH_DECISION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"dispatch_decision missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import dispatch as _dispatch
    return _dispatch.decide_dispatch(cfg, params["slug"], params["task_id"])


_PLACEMENT_DECISION_REQUIRED = {"slug", "text"}
_PLACEMENT_DECISION_ALLOWED = _PLACEMENT_DECISION_REQUIRED | {"task_id"}


def _action_placement_decision(params: dict[str, Any]) -> dict[str, Any]:
    """T-0576 (M11/F11.3): classify one inbound user request as instant-tweak
    vs long-request and decide its placement — the correct-placement guarantee
    as ONE system surface every access point (TG mail / attach-write /
    dedicated user session) consults, instead of prompt convention.

    Required params: slug, text. Optional: task_id (a long request already
    filed — the result then chains the dispatch_decision seam for that task).
    Returns ``{ok, kind: 'instant_tweak'|'long_request', route:
    'apply_live'|'file_task', target_sid, reason, signals, ...}``. Advisory
    like dispatch_decision — it never injects, files, or spawns on its own.
    """
    extra = set(params) - _PLACEMENT_DECISION_ALLOWED
    if extra:
        raise ActionError(f"placement_decision got unexpected params: {sorted(extra)}")
    missing = _PLACEMENT_DECISION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"placement_decision missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import dispatch as _dispatch
    return _dispatch.decide_placement(
        cfg, params["slug"], params["text"], task_id=params.get("task_id"))


_SET_DRIFT_PAUSED_REQUIRED = {"slug", "sid", "paused"}
_SET_DRIFT_PAUSED_ALLOWED = _SET_DRIFT_PAUSED_REQUIRED


def _action_set_drift_paused(params: dict[str, Any]) -> dict[str, Any]:
    """T-0184: pause/resume the drift-check tick for a single session.

    Required params: slug, sid, paused (bool). Returns {ok, sid, drift_paused}.
    Backs ``bsq drift off`` / ``bsq drift on`` — the per-session off-ramp for
    the drift reminder. ``tmux_only`` so a dev session can silence itself via
    the user-worker without coordinator privileges.
    """
    extra = set(params) - _SET_DRIFT_PAUSED_ALLOWED
    if extra:
        raise ActionError(f"set_drift_paused got unexpected params: {sorted(extra)}")
    missing = _SET_DRIFT_PAUSED_REQUIRED - set(params)
    if missing:
        raise ActionError(f"set_drift_paused missing required params: {sorted(missing)}")
    if not isinstance(params["paused"], bool):
        raise ActionError("set_drift_paused: 'paused' must be a boolean")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.set_drift_paused(cfg, params["slug"], params["sid"], params["paused"])


_SET_DRIVE_REQUIRED = {"slug", "sid", "on"}
_SET_DRIVE_ALLOWED = _SET_DRIVE_REQUIRED


def _action_set_drive(params: dict[str, Any]) -> dict[str, Any]:
    """T-0655: the operator's own drive=on/off toggle (``bsq drive off/on``).

    Required params: slug, sid, on (bool). Returns {ok, sid, drive}.
    Operator-role only (``sessions.set_drive`` refuses any other role).
    ``tmux_only`` — it writes only its OWN SessionMd frontmatter
    (filesystem-local), like ``set_drift_paused``.
    """
    extra = set(params) - _SET_DRIVE_ALLOWED
    if extra:
        raise ActionError(f"set_drive got unexpected params: {sorted(extra)}")
    missing = _SET_DRIVE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"set_drive missing required params: {sorted(missing)}")
    if not isinstance(params["on"], bool):
        raise ActionError("set_drive: 'on' must be a boolean")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.set_drive(cfg, params["slug"], params["sid"], params["on"])


_SET_MODEL_REQUIRED = {"slug", "sid", "model"}
_SET_MODEL_ALLOWED = _SET_MODEL_REQUIRED


def _action_set_model(params: dict[str, Any]) -> dict[str, Any]:
    """T-0678: durable PER-SESSION ``claude --model`` override (``bsq model
    set`` / ``bsq model status``), distinct from the FLEET-WIDE
    ``fleet_model_set`` (T-0630, edits the coordinator's settings.json).

    Required params: slug, sid, model (str; "" clears the override). Returns
    {ok, sid, model}. ``tmux_only`` — it writes only its OWN SessionMd
    frontmatter (filesystem-local), like ``set_drift_paused`` / ``set_drive``.
    """
    extra = set(params) - _SET_MODEL_ALLOWED
    if extra:
        raise ActionError(f"set_model got unexpected params: {sorted(extra)}")
    missing = _SET_MODEL_REQUIRED - set(params)
    if missing:
        raise ActionError(f"set_model missing required params: {sorted(missing)}")
    if not isinstance(params["model"], str):
        raise ActionError("set_model: 'model' must be a string")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.set_model(cfg, params["slug"], params["sid"], params["model"])


_IDLE_POSTPONE_REQUIRED = {"slug", "sid"}
_IDLE_POSTPONE_ALLOWED = _IDLE_POSTPONE_REQUIRED | {"seconds", "reason"}


def _action_idle_postpone(params: dict[str, Any]) -> dict[str, Any]:
    """T-0466: defer this session's next ~1h cache-window recycle (``bsq postpone``).

    Required params: slug, sid. Optional: seconds (deferral length; default = one
    full idle window), reason. Returns {ok, sid, postpone_until, seconds}.
    Backs the postpone protocol — a stale waiting session asks to be left running
    one more window, repeatable indefinitely; pass ``seconds`` to declare a
    bounded wait with a known ETA (e.g. a long build). ``tmux_only`` — it writes
    only its OWN SessionMd frontmatter (filesystem-local), like set_drift_paused.
    """
    extra = set(params) - _IDLE_POSTPONE_ALLOWED
    if extra:
        raise ActionError(f"idle_postpone got unexpected params: {sorted(extra)}")
    missing = _IDLE_POSTPONE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"idle_postpone missing required params: {sorted(missing)}")
    seconds = params.get("seconds")
    if seconds is not None and not isinstance(seconds, int):
        raise ActionError("idle_postpone: 'seconds' must be an integer")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.set_idle_postpone(cfg, params["slug"], params["sid"],
                                       seconds=seconds, reason=params.get("reason"))


_MORPH_SESSION_REQUIRED = {"slug", "sid", "role"}
_MORPH_SESSION_ALLOWED = _MORPH_SESSION_REQUIRED | {
    "task_id", "initiative", "window", "cwd", "claude_uuid",
}


def _action_morph_session(params: dict[str, Any]) -> dict[str, Any]:
    """T-0509 (M11/F11.2): morph a user session's role IN PLACE.

    A user-launched session is a USER session by default but may morph: take a
    task → ``dev``; spawn teammates → ``teamlead``; become ``operator`` iff none
    is running ("sessions are transient, system is persistent"). Stamps the
    ``role`` (+ task_id / initiative) on the session md without renaming the
    tmux window, so the peer-bus SID is untouched. ``operator`` morph is gated
    by the one-per-project singleton (T-0472/T-0523).

    Required params: slug, sid, role (``dev`` | ``teamlead`` | ``operator``).
    Optional: task_id, initiative, window, cwd, claude_uuid (the last three let
    the CLI seed an md for an unregistered, manually-launched user session).
    Returns {ok, sid, role, task_id, initiative, created}. ``tmux_only`` — it
    writes only its OWN SessionMd frontmatter (filesystem-local), like
    set_drift_paused / idle_postpone.
    """
    extra = set(params) - _MORPH_SESSION_ALLOWED
    if extra:
        raise ActionError(f"morph_session got unexpected params: {sorted(extra)}")
    missing = _MORPH_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"morph_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.morph_session(
        cfg, params["slug"], params["sid"], params["role"],
        task_id=params.get("task_id"),
        initiative=params.get("initiative"),
        window=params.get("window"),
        cwd=params.get("cwd"),
        claude_uuid=params.get("claude_uuid"),
    )


_BIND_INITIATIVE_REQUIRED = {"slug", "sid", "initiative"}
_BIND_INITIATIVE_ALLOWED = _BIND_INITIATIVE_REQUIRED


def _action_bind_initiative(params: dict[str, Any]) -> dict[str, Any]:
    """Bind another initiative to an already-running teamlead session.

    Required params: slug, sid, initiative
    Returns: {ok, sid, initiative, extras: [...full extra list...]}
    """
    extra = set(params) - _BIND_INITIATIVE_ALLOWED
    if extra:
        raise ActionError(f"bind_initiative got unexpected params: {sorted(extra)}")
    missing = _BIND_INITIATIVE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"bind_initiative missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.bind_initiative(cfg, params["slug"], params["sid"], params["initiative"])


_UNBIND_TASK_REQUIRED = {"slug", "sid", "task_id"}
_UNBIND_TASK_ALLOWED = _UNBIND_TASK_REQUIRED


def _action_unbind_task(params: dict[str, Any]) -> dict[str, Any]:
    """Remove a task binding from a dev session's extras.

    Required params: slug, sid, task_id
    Returns: {ok, sid, task_id, extras, changed}
    """
    extra = set(params) - _UNBIND_TASK_ALLOWED
    if extra:
        raise ActionError(f"unbind_task got unexpected params: {sorted(extra)}")
    missing = _UNBIND_TASK_REQUIRED - set(params)
    if missing:
        raise ActionError(f"unbind_task missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.unbind_task(cfg, params["slug"], params["sid"], params["task_id"])


_REHOME_PRIMARY_REQUIRED = {"slug", "task_id", "to_sid"}
_REHOME_PRIMARY_ALLOWED = _REHOME_PRIMARY_REQUIRED


def _action_rehome_primary(params: dict[str, Any]) -> dict[str, Any]:
    """T-0324 (H2): safely re-home a task's PRIMARY binding onto to_sid.

    The repair neither bind_task (adopt-empty / append-extras only) nor
    unbind_task (refuses to touch the primary) can do — strips the primary
    off every other claimant and stamps it on the target, under the
    .task-claim.lock flock, with bind_task's admission rules (no
    constant-team / TL / operator target, no clobbering a different primary).

    Required params: slug, task_id, to_sid
    Returns: {ok, task_id, to_sid, stripped: [sids]}
    """
    extra = set(params) - _REHOME_PRIMARY_ALLOWED
    if extra:
        raise ActionError(f"rehome_primary got unexpected params: {sorted(extra)}")
    missing = _REHOME_PRIMARY_REQUIRED - set(params)
    if missing:
        raise ActionError(f"rehome_primary missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.rehome_primary(
        cfg, params["slug"], params["task_id"], params["to_sid"])


_UNBIND_INITIATIVE_REQUIRED = {"slug", "sid", "initiative"}
_UNBIND_INITIATIVE_ALLOWED = _UNBIND_INITIATIVE_REQUIRED


def _action_unbind_initiative(params: dict[str, Any]) -> dict[str, Any]:
    """Remove an initiative binding from a teamlead session.

    Required params: slug, sid, initiative
    Returns: {ok, sid, initiative, extras: [...], changed: bool}
    """
    extra = set(params) - _UNBIND_INITIATIVE_ALLOWED
    if extra:
        raise ActionError(f"unbind_initiative got unexpected params: {sorted(extra)}")
    missing = _UNBIND_INITIATIVE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"unbind_initiative missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.unbind_initiative(cfg, params["slug"], params["sid"], params["initiative"])


_ARCHIVE_SESSION_REQUIRED = {"slug", "sid"}
_ARCHIVE_SESSION_ALLOWED = _ARCHIVE_SESSION_REQUIRED


def _action_archive_session(params: dict[str, Any]) -> dict[str, Any]:
    """Archive a suspended session (set archived: true in its md).

    Required params: slug, sid
    Returns: {ok: true, sid, archived: true}
    """
    extra = set(params) - _ARCHIVE_SESSION_ALLOWED
    if extra:
        raise ActionError(f"archive_session got unexpected params: {sorted(extra)}")
    missing = _ARCHIVE_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"archive_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.archive_session(cfg, params["slug"], params["sid"])


def _action_unarchive_session(params: dict[str, Any]) -> dict[str, Any]:
    """Remove the archived flag from a session.

    Required params: slug, sid
    Returns: {ok: true, sid, archived: false}
    """
    extra = set(params) - _ARCHIVE_SESSION_ALLOWED
    if extra:
        raise ActionError(f"unarchive_session got unexpected params: {sorted(extra)}")
    missing = _ARCHIVE_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"unarchive_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.unarchive_session(cfg, params["slug"], params["sid"])


# ---------------------------------------------------------------------------
# Binding-graph reconcilers (T-0072 / T-0073 / T-0077)
# ---------------------------------------------------------------------------

_GC_SESSIONS_REQUIRED = {"slug"}
_GC_SESSIONS_ALLOWED = _GC_SESSIONS_REQUIRED


def _action_gc_sessions(params: dict[str, Any]) -> dict[str, Any]:
    """T-0077: flip md ``status: active`` → ``suspended`` for panes that died.

    Required params: slug
    Returns: {ok, scanned, repaired, sids: [...]}
    """
    extra = set(params) - _GC_SESSIONS_ALLOWED
    if extra:
        raise ActionError(f"gc_sessions got unexpected params: {sorted(extra)}")
    missing = _GC_SESSIONS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"gc_sessions missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.gc_sessions(cfg, params["slug"])


_DEDUP_SESSIONS_REQUIRED = {"slug"}
_DEDUP_SESSIONS_ALLOWED = _DEDUP_SESSIONS_REQUIRED | {"dry_run"}


def _action_dedup_sessions(params: dict[str, Any]) -> dict[str, Any]:
    """T-0176 #5/#6: collapse duplicate SessionMds to one keeper per logical
    session. GATED — dry_run defaults to True; pass dry_run=False to apply.

    Required params: slug. Optional: dry_run (bool, default True).
    Returns: {ok, dry_run, merged_count, merges: [...]}
    """
    extra = set(params) - _DEDUP_SESSIONS_ALLOWED
    if extra:
        raise ActionError(f"dedup_sessions got unexpected params: {sorted(extra)}")
    missing = _DEDUP_SESSIONS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"dedup_sessions missing required params: {sorted(missing)}")

    dry_run = params.get("dry_run", True)
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("false", "0", "no", "off")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.dedup_sessions(cfg, params["slug"], dry_run=bool(dry_run))


_PRUNE_ORPHAN_TEAMS_REQUIRED = {"slug"}
_PRUNE_ORPHAN_TEAMS_ALLOWED = _PRUNE_ORPHAN_TEAMS_REQUIRED | {"dry_run"}


def _action_prune_orphan_teams(params: dict[str, Any]) -> dict[str, Any]:
    """T-0177: prune stale per-initiative team md files after the project regroup.
    GATED — dry_run defaults to True; pass dry_run=False to delete.

    Required params: slug. Optional: dry_run (bool, default True).
    Returns: {ok, dry_run, pruned: [...]}
    """
    extra = set(params) - _PRUNE_ORPHAN_TEAMS_ALLOWED
    if extra:
        raise ActionError(f"prune_orphan_teams got unexpected params: {sorted(extra)}")
    missing = _PRUNE_ORPHAN_TEAMS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"prune_orphan_teams missing required params: {sorted(missing)}")

    dry_run = params.get("dry_run", True)
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("false", "0", "no", "off")

    cfg = _get_config()
    from bot_squad_worker import teams as _teams
    return _teams.prune_orphan_teams(cfg, params["slug"], dry_run=bool(dry_run))


_GC_STALE_BINDINGS_REQUIRED = {"slug"}
_GC_STALE_BINDINGS_ALLOWED = _GC_STALE_BINDINGS_REQUIRED


def _action_gc_stale_bindings(params: dict[str, Any]) -> dict[str, Any]:
    """T-0073: strip stale ``task_id`` from sessions losing a duplicate-binding race.

    Required params: slug
    Returns: {ok, scanned, stripped, details: [...]}
    """
    extra = set(params) - _GC_STALE_BINDINGS_ALLOWED
    if extra:
        raise ActionError(f"gc_stale_bindings got unexpected params: {sorted(extra)}")
    missing = _GC_STALE_BINDINGS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"gc_stale_bindings missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.gc_stale_bindings(cfg, params["slug"])


_PEER_REBIND_SID_REQUIRED = {"slug", "old_sid", "new_sid"}
_PEER_REBIND_SID_ALLOWED = _PEER_REBIND_SID_REQUIRED


def _action_peer_rebind_sid(params: dict[str, Any]) -> dict[str, Any]:
    """T-0072: rename the peer-bus inbox triple from ``old_sid`` to ``new_sid``.

    Required params: slug, old_sid, new_sid
    Returns: {ok, renamed: [...], collisions: [...]}

    Invoked by ``scripts/hooks/session_start.sh`` after a ``tmux break-pane``
    rotates the live SID, and (internally) by ``sessions.resume()``.
    """
    extra = set(params) - _PEER_REBIND_SID_ALLOWED
    if extra:
        raise ActionError(f"peer_rebind_sid got unexpected params: {sorted(extra)}")
    missing = _PEER_REBIND_SID_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_rebind_sid missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"peer_rebind_sid: unknown project slug {slug!r}")

    from bot_squad_worker import intersession as _is
    return _is.rebind_sid(cfg, slug, params["old_sid"], params["new_sid"])


# ---------------------------------------------------------------------------
# Session-lifecycle + Team entity actions (T-0142 / T-0144)
# ---------------------------------------------------------------------------

_GC_DEAD_BINDINGS_REQUIRED = {"slug"}
_GC_DEAD_BINDINGS_ALLOWED = _GC_DEAD_BINDINGS_REQUIRED


def _action_gc_dead_bindings(params: dict[str, Any]) -> dict[str, Any]:
    """T-0142: clear task/initiative bindings whose target is closed or gone.

    Required params: slug
    Returns: {ok, scanned, cleared, details: [...]}
    """
    extra = set(params) - _GC_DEAD_BINDINGS_ALLOWED
    if extra:
        raise ActionError(f"gc_dead_bindings got unexpected params: {sorted(extra)}")
    missing = _GC_DEAD_BINDINGS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"gc_dead_bindings missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.gc_dead_bindings(cfg, params["slug"])


_ARCHIVE_DEAD_TEAMMATES_REQUIRED = {"slug"}
_ARCHIVE_DEAD_TEAMMATES_ALLOWED = _ARCHIVE_DEAD_TEAMMATES_REQUIRED


def _action_archive_dead_teammates(params: dict[str, Any]) -> dict[str, Any]:
    """T-0142/T-0144: auto-archive cleanly-exited / verified-done dev teammates.

    Required params: slug
    Returns: {ok, scanned, archived, sids: [...]}
    """
    extra = set(params) - _ARCHIVE_DEAD_TEAMMATES_ALLOWED
    if extra:
        raise ActionError(f"archive_dead_teammates got unexpected params: {sorted(extra)}")
    missing = _ARCHIVE_DEAD_TEAMMATES_REQUIRED - set(params)
    if missing:
        raise ActionError(f"archive_dead_teammates missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.archive_dead_teammates(cfg, params["slug"])


_RECONCILE_TEAMS_REQUIRED = {"slug"}
_RECONCILE_TEAMS_ALLOWED = _RECONCILE_TEAMS_REQUIRED


def _action_reconcile_teams(params: dict[str, Any]) -> dict[str, Any]:
    """T-0142: rebuild the tmux-session-keyed Team mds from the SessionMd registry.

    Required params: slug
    Returns: {ok, teams: [...], reconciled: N}
    """
    extra = set(params) - _RECONCILE_TEAMS_ALLOWED
    if extra:
        raise ActionError(f"reconcile_teams got unexpected params: {sorted(extra)}")
    missing = _RECONCILE_TEAMS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"reconcile_teams missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import teams as _teams
    return _teams.reconcile_teams(cfg, params["slug"])


_LIST_TEAMS_REQUIRED = {"slug"}
_LIST_TEAMS_ALLOWED = _LIST_TEAMS_REQUIRED


def _action_list_teams(params: dict[str, Any]) -> dict[str, Any]:
    """T-0142: list persisted Team mds for a project.

    Required params: slug
    Returns: {ok, teams: [<team meta>, ...]}
    """
    extra = set(params) - _LIST_TEAMS_ALLOWED
    if extra:
        raise ActionError(f"list_teams got unexpected params: {sorted(extra)}")
    missing = _LIST_TEAMS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"list_teams missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import teams as _teams
    return _teams.list_teams(cfg, params["slug"])


_ARCHIVE_TEAM_REQUIRED = {"slug", "name"}
_ARCHIVE_TEAM_ALLOWED = _ARCHIVE_TEAM_REQUIRED


def _action_archive_team(params: dict[str, Any]) -> dict[str, Any]:
    """T-0142: true Team archive — suspend live members + flag the team archived.

    Required params: slug, name (tmux session name)
    Returns: {ok, name, suspended: [...], archived: true}
    """
    extra = set(params) - _ARCHIVE_TEAM_ALLOWED
    if extra:
        raise ActionError(f"archive_team got unexpected params: {sorted(extra)}")
    missing = _ARCHIVE_TEAM_REQUIRED - set(params)
    if missing:
        raise ActionError(f"archive_team missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import teams as _teams
    return _teams.archive_team(cfg, params["slug"], params["name"])


_RESURRECT_TEAM_REQUIRED = {"slug", "name"}
_RESURRECT_TEAM_ALLOWED = _RESURRECT_TEAM_REQUIRED


def _action_resurrect_team(params: dict[str, Any]) -> dict[str, Any]:
    """T-0142: bring an archived team back — clear the flag + resume the TL.

    Required params: slug, name (tmux session name)
    Returns: {ok, name, tl: <resumed SID or None>}
    """
    extra = set(params) - _RESURRECT_TEAM_ALLOWED
    if extra:
        raise ActionError(f"resurrect_team got unexpected params: {sorted(extra)}")
    missing = _RESURRECT_TEAM_REQUIRED - set(params)
    if missing:
        raise ActionError(f"resurrect_team missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import teams as _teams
    return _teams.resurrect_team(cfg, params["slug"], params["name"])


_SYNC_SESSION_NAME_REQUIRED = {"slug", "sid", "name"}
_SYNC_SESSION_NAME_ALLOWED = _SYNC_SESSION_NAME_REQUIRED


def _action_sync_session_name(params: dict[str, Any]) -> dict[str, Any]:
    """T-0142: single-source-of-truth session rename across tmux + registry.

    Required params: slug, sid, name (new window/display name)
    Returns: {ok, sid, new_sid, name}
    """
    extra = set(params) - _SYNC_SESSION_NAME_ALLOWED
    if extra:
        raise ActionError(f"sync_session_name got unexpected params: {sorted(extra)}")
    missing = _SYNC_SESSION_NAME_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sync_session_name missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.sync_session_name(
        cfg, params["slug"], params["sid"], params["name"]
    )


# ---------------------------------------------------------------------------
# Autoupdate operator handoff actions (T-0085)
# ---------------------------------------------------------------------------

_AUTOUPDATE_RETRY_REQUIRED = {"slug"}
_AUTOUPDATE_RETRY_ALLOWED = _AUTOUPDATE_RETRY_REQUIRED


def _action_autoupdate_retry(params: dict[str, Any]) -> dict[str, Any]:
    """Re-enqueue the most-recent failed apply job.

    Required params: slug
    Returns: {ok, requeued: bool, version?: str, queue_file?: str, reason?: str}

    Idempotent — when the failed-queue is empty, returns
    ``{ok: true, requeued: false}`` without raising. The ``slug`` param is
    accepted (and validated) for consistency with peer actions even though
    the failed-queue is install-scoped, not project-scoped.
    """
    extra = set(params) - _AUTOUPDATE_RETRY_ALLOWED
    if extra:
        raise ActionError(f"autoupdate_retry got unexpected params: {sorted(extra)}")
    missing = _AUTOUPDATE_RETRY_REQUIRED - set(params)
    if missing:
        raise ActionError(f"autoupdate_retry missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autoupdate_retry: unknown project slug {slug!r}")

    from bot_squad_worker import autoupdate_apply as _apply
    try:
        return _apply.retry_last_failed(cfg)
    except Exception as e:
        raise ActionError(f"autoupdate_retry: {e}") from e


def _action_autoupdate_check_now(params: dict[str, Any]) -> dict[str, Any]:
    """T-0089: trigger the poller tick out-of-cadence.

    Takes no params. Returns {ok, scheduled: bool, next_run?: str}.
    Reschedules the registered ``autoupdate`` APScheduler job to fire on
    the next loop pass (typically <1s). Non-blocking — the actual poll
    happens on the scheduler thread; the API caller can refresh
    ``/autoupdate/status`` a moment later to see the updated
    ``last_check_at``.

    If the scheduler isn't initialised (tests, or worker not in coordinator
    mode), the action falls back to running ``autoupdate.tick`` inline so
    operators still get the documented "force a fresh check" behaviour.
    """
    if params:
        raise ActionError(f"autoupdate_check_now takes no params, got: {sorted(params)}")

    cfg = _get_config()
    from bot_squad_worker import autoupdate as _au

    if _SCHED is None:
        # No scheduler around (e.g. tests, single-shot scripts) — fall back to
        # inline tick so the action still has its documented effect.
        try:
            _au.tick(cfg)
        except Exception as e:  # noqa: BLE001 — operator-facing, surface message
            raise ActionError(f"autoupdate_check_now: tick failed: {e}") from e
        return {"ok": True, "scheduled": False, "ran_inline": True}

    from datetime import datetime, timezone
    try:
        job = _SCHED.modify_job(
            "autoupdate", next_run_time=datetime.now(timezone.utc)
        )
    except Exception as e:  # JobLookupError, scheduler not running, etc.
        raise ActionError(f"autoupdate_check_now: could not reschedule: {e}") from e

    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    return {"ok": True, "scheduled": True, "next_run": next_run}


_AUTOUPDATE_FORCE_REQUIRED = {"slug", "version"}
_AUTOUPDATE_FORCE_ALLOWED = _AUTOUPDATE_FORCE_REQUIRED


def _action_autoupdate_force(params: dict[str, Any]) -> dict[str, Any]:
    """Force-apply a specific release version (skips poller's newer-than gate).

    Required params: slug, version
    Returns: {ok: true, version, queue_file}

    The manifest entry is fetched from the mothership's
    ``/api/releases/<version>`` endpoint and enqueued for the drain loop.
    Apply itself runs out-of-band on the next tick.
    """
    extra = set(params) - _AUTOUPDATE_FORCE_ALLOWED
    if extra:
        raise ActionError(f"autoupdate_force got unexpected params: {sorted(extra)}")
    missing = _AUTOUPDATE_FORCE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"autoupdate_force missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    version = params["version"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autoupdate_force: unknown project slug {slug!r}")
    if not isinstance(version, str) or not version.strip():
        raise ActionError("autoupdate_force: empty version")

    from bot_squad_worker import autoupdate_apply as _apply
    try:
        return _apply.force_apply(cfg, version)
    except Exception as e:
        raise ActionError(f"autoupdate_force: {e}") from e


_RELOAD_PROJECTS_ALLOWED: set[str] = set()


def _action_reload_projects(params: dict[str, Any]) -> dict[str, Any]:
    """Re-read projects.toml + rebuild the in-memory project list.

    Takes no params. Returns ``{ok: true, projects: [<slug>, ...], count: N}``.

    Called by the API after ``POST /api/projects`` writes a new project
    block. The worker reads projects.toml only at startup, so without this
    nudge any slug-keyed action (peer_send role-resolution, tg_notify,
    deploy, etc.) would 404 on the new slug until a restart. The on-disk
    state is already consistent before this fires; failures here only
    leave the in-memory worker view stale (the next restart fixes it).
    """
    extra = set(params) - _RELOAD_PROJECTS_ALLOWED
    if extra:
        raise ActionError(f"reload_projects got unexpected params: {sorted(extra)}")

    cfg = _get_config()
    from bot_squad_worker.config import Config
    new_cfg = Config.load(cfg.config_dir)
    set_config(new_cfg)
    return {
        "ok": True,
        "projects": sorted(new_cfg.projects.keys()),
        "count": len(new_cfg.projects),
    }


# ADDING AN ACTION? Three edits, and the third is the one people miss (T-0799):
#   1. this registry;
#   2. ``ACTION_MODES`` below — its parity with this dict is asserted by
#      ``worker/tests/test_action_modes.py`` ("set(ACTION_MODES) ==
#      set(ACTION_REGISTRY)"), so a missing mode fails there;
#   3. the CLOSED allowlist in ``worker/tests/test_actions.py``
#      (``test_registry_lists_only_allowed_actions``) — a hand-maintained set,
#      so a new action fails it BY CONSTRUCTION.
# Nothing here used to point at #3, and running only the tests near your own
# emitter passes while the shared suite goes red for every other session. That
# is how T-0799 and T-0783a each red-ed it within one hour; the note is here
# rather than in a doc because this dict is where you are standing when it bites.
ACTION_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "noop": _action_noop,
    "tg_verify_login": _action_tg_verify_login,
    "tg_notify": _action_tg_notify,
    # T-0247: MAX (max.ru) DM channel — mirrors tg_notify.
    "max_notify": _action_max_notify,
    # T-0610: temporary page-channel switch (TG-primary default / MAX reserve).
    "page_channel": _action_page_channel,
    "tg_stall_clear": _action_tg_stall_clear,
    # T-0639: runtime (chat_id,thread_id)->project topic-binding surface.
    "tg_topic_bind": _action_tg_topic_bind,
    "tg_topic_unbind": _action_tg_topic_unbind,
    "tg_topic_list": _action_tg_topic_list,
    # T-0799: per-message-TYPE destination map (configurable chat_id per type,
    # defaulting to today's DM behaviour).
    "msg_route_list": _action_msg_route_list,
    "msg_route_set": _action_msg_route_set,
    "msg_route_clear": _action_msg_route_clear,
    # T-0662: human-readable label -> session SID aliases.
    "session_alias_set": _action_session_alias_set,
    "session_alias_remove": _action_session_alias_remove,
    "session_alias_resolve": _action_session_alias_resolve,
    "session_alias_list": _action_session_alias_list,
    # T-0660: create-and-bind a forum topic in one step + rename General.
    "tg_topic_create": _action_tg_topic_create,
    "tg_topic_rename_general": _action_tg_topic_rename_general,
    "tg_topic_rename": _action_tg_topic_rename,
    "tg_topic_close_for_ticket": _action_tg_topic_close_for_ticket,
    # T-0386: per-project forum-topic lifecycle (create-on-project / GC-on-archive).
    "provision_project_topics": _action_provision_project_topics,
    "gc_project_topics": _action_gc_project_topics,
    "deploy": _action_deploy,
    "clone_status": _action_clone_status,
    "pull_master": _action_pull_master,
    "pause_deploys": _action_pause_deploys,
    "resume_deploys": _action_resume_deploys,
    "list_sessions": _action_list_sessions,
    # T-0210: read persisted resource telemetry (context/memory/quota).
    "telemetry_get": _action_telemetry_get,
    "pause_session": _action_pause_session,
    "suspend_session": _action_suspend_session,
    "resume_session": _action_resume_session,
    "spawn_session": _action_spawn_session,
    # T-0478 (M2/F2.4): ensure a user-conversation session attends a user.
    "ensure_user_conversation": _action_ensure_user_conversation,
    "scheduler_state": _action_scheduler_state,
    # T-0759: read-only liveness of the outbound log (ok/idle/decayed/blind).
    "outbound_liveness": _action_outbound_liveness,
    "inject_input": _action_inject_input,
    # T-0770: the BLOCK sibling — a multi-line payload as ONE composer
    # submission (inject_input submits one line at a time, which splits an
    # envelope into N turns).
    "inject_prompt": _action_inject_prompt,
    # T-0469 (M1/F1.6): multiplexed queue-backed input (coalesce + caption +
    # defer-on-busy). Agents write via `bsq send-input`, not raw send-keys.
    "send_input": _action_send_input,
    # T-0153: autopilot — prompt-driven, time-boxed autonomous runs per target.
    "autopilot_start": _action_autopilot_start,
    "autopilot_stop": _action_autopilot_stop,
    "autopilot_status": _action_autopilot_status,
    "peer_send": _action_peer_send,
    "peer_inbox_read": _action_peer_inbox_read,
    "peer_inbox_wait": _action_peer_inbox_wait,
    # T-0498 (M6/F6.2): synchronous inter-session channel handshake + live send.
    "sync_request": _action_sync_request,
    "sync_ack": _action_sync_ack,
    "sync_enter": _action_sync_enter,
    "sync_send": _action_sync_send,
    "sync_exit": _action_sync_exit,
    "sync_status": _action_sync_status,
    "task_progress_add": _action_task_progress_add,
    # T-0589: on-demand short backlog digest for the TG conversation surface.
    "task_digest": _action_task_digest,
    # T-0463: assignment-interface write-result primitive (F1.1-d).
    "assignment_write_result": _action_assignment_write_result,
    # T-0464: Routines — declare + list (firing tick = routines.routine_tick).
    "routine_declare": _action_routine_declare,
    "routine_list": _action_routine_list,
    # T-0604 (D-0048 slice 2): mute a monitor routine — probes, never fires.
    "routine_mute": _action_routine_mute,
    # T-0467: universal-compact "write everything down" — role-agnostic save of
    # a session's forward-state into its role artifact (F1.4).
    "compact_write_state": _action_compact_write_state,
    # T-0473: read-only operator state-doc transparency primitive (M2-F2.1).
    "operator_state_doc": _action_operator_state_doc,
    # T-0522: user-facing operator re-drive pause toggle (wraps T-0474 helpers).
    "operator_pause": _action_operator_pause,
    "operator_resume": _action_operator_resume,
    "operator_status": _action_operator_status,
    # T-0783a: which board tickets are takeable / need triage / are excluded.
    "pickup_queue": _action_pickup_queue,
    # T-0630: fleet-default `claude --model` (~/.claude/settings.json).
    "fleet_model_get": _action_fleet_model_get,
    "fleet_model_set": _action_fleet_model_set,
    # T-0042: atomic T-NNNN allocator (flock-protected).
    "task_new": _action_task_new,
    "doc_new": _action_doc_new,
    "initiative_new": _action_initiative_new,
    "bind_task": _action_bind_task,
    "bind_initiative": _action_bind_initiative,
    # T-0237 Layer-2: operator-invoked reuse-vs-spawn dispatch recommendation.
    "dispatch_decision": _action_dispatch_decision,
    # T-0576 (M11/F11.3): instant-tweak vs long-request placement guarantee.
    "placement_decision": _action_placement_decision,
    # T-0184: per-session drift-check off-ramp (bsq drift on/off).
    "set_drift_paused": _action_set_drift_paused,
    # T-0655: operator's own drive=on/off continuity toggle (bsq drive on/off).
    "set_drive": _action_set_drive,
    # T-0678: per-session `claude --model` override (bsq model set/status).
    "set_model": _action_set_model,
    # T-0466: per-session cache-window recycle postpone (bsq postpone).
    "idle_postpone": _action_idle_postpone,
    # T-0509 (M11/F11.2): user-session role morph (user→dev/teamlead/operator).
    "morph_session": _action_morph_session,
    "unbind_task": _action_unbind_task,
    # T-0324 (H2): safe primary re-home — the repair bind/unbind can't do.
    "rehome_primary": _action_rehome_primary,
    "unbind_initiative": _action_unbind_initiative,
    "archive_session": _action_archive_session,
    "unarchive_session": _action_unarchive_session,
    # T-0072/0073/0077: binding-graph reconcilers + peer-bus SID rotation.
    "gc_sessions": _action_gc_sessions,
    "dedup_sessions": _action_dedup_sessions,
    "prune_orphan_teams": _action_prune_orphan_teams,
    "gc_stale_bindings": _action_gc_stale_bindings,
    "peer_rebind_sid": _action_peer_rebind_sid,
    # T-0142/0144: session-lifecycle reconcilers + Team entity + rename sync.
    "gc_dead_bindings": _action_gc_dead_bindings,
    "archive_dead_teammates": _action_archive_dead_teammates,
    "reconcile_teams": _action_reconcile_teams,
    "list_teams": _action_list_teams,
    "archive_team": _action_archive_team,
    "resurrect_team": _action_resurrect_team,
    "sync_session_name": _action_sync_session_name,
    # T-0085: autoupdate operator handoff levers.
    "autoupdate_retry": _action_autoupdate_retry,
    "autoupdate_force": _action_autoupdate_force,
    # T-0089: trigger an out-of-cadence poller tick from the consumer UI.
    "autoupdate_check_now": _action_autoupdate_check_now,
    # T-0054: nudge the worker after POST /api/projects so the in-memory
    # project list picks up the new slug without a worker restart.
    "reload_projects": _action_reload_projects,
}


# Mode tags: which worker role is allowed to invoke each action.
# coordinator_only — needs scheduler / coordinator-only state (TG client,
#   deploy queue, peer inbox, autopilot watchdog).
# tmux_only — purely tmux/filesystem ops local to a Linux user.
# both — universally safe (proof-of-life).
ACTION_MODES: dict[str, str] = {
    "noop": "both",
    "tg_verify_login": "coordinator_only",
    "tg_notify": "coordinator_only",
    "max_notify": "coordinator_only",
    "page_channel": "coordinator_only",
    "tg_stall_clear": "coordinator_only",
    # T-0639: the binding store is read by the coordinator's TG listener
    # (single writer, mirrors the tg_stall/tg_topics ops above it).
    "tg_topic_bind": "coordinator_only",
    "tg_topic_unbind": "coordinator_only",
    "tg_topic_list": "coordinator_only",
    # T-0799: the route store is read on the coordinator's own send path (every
    # automated pager resolves through it) — single writer, coordinator-only
    # like the binding/topic stores above it.
    "msg_route_list": "coordinator_only",
    "msg_route_set": "coordinator_only",
    "msg_route_clear": "coordinator_only",
    # T-0662: the alias store is GLOBAL (data/_worker/session_aliases.json,
    # not per-project) — single writer, coordinator-only like the bindings
    # store above it.
    "session_alias_set": "coordinator_only",
    "session_alias_remove": "coordinator_only",
    "session_alias_resolve": "coordinator_only",
    "session_alias_list": "coordinator_only",
    # T-0660: both call the coordinator's TG client (createForumTopic /
    # editGeneralForumTopic) — coordinator-only like the rest of the TG ops.
    "tg_topic_create": "coordinator_only",
    "tg_topic_rename_general": "coordinator_only",
    # T-0669/T-0676 item 1: editForumTopic, same coordinator TG client.
    "tg_topic_rename": "coordinator_only",
    "tg_topic_close_for_ticket": "coordinator_only",
    # T-0386: use the coordinator TG client + project config (single writer of
    # the per-project topic map) — coordinator-only like the rest of the TG ops.
    "provision_project_topics": "coordinator_only",
    "gc_project_topics": "coordinator_only",
    "deploy": "coordinator_only",
    # T-0296: both run on-host git against the project clones (coordinator-side,
    # like deploy) — a tmux-only user-worker has neither the repos nor the right
    # to read clone health or fast-forward the prod clone. pull_master is further
    # admin-gated at the API edge.
    "clone_status": "coordinator_only",
    "pull_master": "coordinator_only",
    "pause_deploys": "coordinator_only",
    "resume_deploys": "coordinator_only",
    "list_sessions": "tmux_only",
    # T-0237: pure local fs read (session mds + telemetry records), no tmux.
    "dispatch_decision": "tmux_only",
    # T-0576: same read-only profile as dispatch_decision (session mds + a
    # tolerant live-pane scan for the operator target) — no coordinator state.
    "placement_decision": "tmux_only",
    # telemetry_get reads the SHARED install data dir (all users' sampled
    # records land there) → a single coordinator read, not a per-user fan-out.
    "telemetry_get": "coordinator_only",
    "pause_session": "tmux_only",
    "suspend_session": "tmux_only",
    "resume_session": "tmux_only",
    "spawn_session": "tmux_only",
    "ensure_user_conversation": "tmux_only",
    "scheduler_state": "coordinator_only",
    # T-0759: reads the SHARED install data dir (one outbound spool + witness
    # files for the whole install), so a single coordinator read like
    # telemetry_get — a per-user fan-out would answer the same question N times.
    "outbound_liveness": "coordinator_only",
    "inject_input": "tmux_only",
    # T-0770: same per-user tmux view as inject_input (pane lookup + paste into
    # a LOCAL pane) → tmux_only for the same reason.
    "inject_prompt": "tmux_only",
    # T-0469: enqueues + delivers to a LOCAL pane (list_panes/compute_sid), same
    # per-user tmux view as inject_input → tmux_only.
    "send_input": "tmux_only",
    # T-0153: autopilot reads/writes coordinator state (peer bus, spawn, tg,
    # scheduler-coupled watchdog), so coordinator-only like the rest.
    "autopilot_start": "coordinator_only",
    "autopilot_stop": "coordinator_only",
    "autopilot_status": "coordinator_only",
    "peer_send": "coordinator_only",
    "peer_inbox_read": "coordinator_only",
    "peer_inbox_wait": "coordinator_only",
    # T-0498: writes the shared install data dir (_chat/sync) + reuses
    # intersession.send for delivery — single coordinator writer, like the peer
    # actions. Sessions reach it via `bsq sync` (the coordinator socket); the
    # live-pane terminal delivery is the CLI's job (the tmux_only inject_input).
    "sync_request": "coordinator_only",
    "sync_ack": "coordinator_only",
    "sync_enter": "coordinator_only",
    "sync_send": "coordinator_only",
    "sync_exit": "coordinator_only",
    "sync_status": "coordinator_only",
    "task_progress_add": "coordinator_only",
    # T-0589: read-only scan of the shared install data dir (backlog/) — a
    # single coordinator read, like telemetry_get. Sessions reach it via
    # `bsq task digest` (the coordinator socket).
    "task_digest": "coordinator_only",
    # T-0463: writes the shared install data dir (artifacts/) — single
    # coordinator writer, like task_progress_add. Dev sessions reach it via the
    # API / coordinator socket.
    "assignment_write_result": "coordinator_only",
    # T-0464: writes/reads the shared install data dir (routines/) + allocates a
    # shared counter id — single coordinator writer, like task_new. Sessions reach
    # it via `bsq routine ...` (the coordinator socket).
    "routine_declare": "coordinator_only",
    "routine_list": "coordinator_only",
    "routine_mute": "coordinator_only",
    # T-0467: writes the shared install data dir (artifacts/) + reads session md
    # — single coordinator writer, like assignment_write_result. Sessions reach
    # it via `bsq compact-save` (the coordinator socket).
    "compact_write_state": "coordinator_only",
    # T-0473: reads the shared install data dir (artifacts/operator-state.md) —
    # single coordinator reader, like compact_write_state. The operator reaches
    # it via `bsq operator-state`.
    "operator_state_doc": "coordinator_only",
    # T-0522: toggle/read the per-project operator re-drive pause flag under the
    # shared install data dir (_worker/operator_redrive/) + read the live-operator
    # detector — coordinator-only, like the rest of the project-state ops.
    "operator_pause": "coordinator_only",
    "operator_resume": "coordinator_only",
    "operator_status": "coordinator_only",
    # T-0783a: read-only scan of the shared install data dir (backlog/ +
    # sessions/) — a single coordinator read, like task_digest. Sessions reach it
    # via `bsq pickup` (the coordinator socket).
    "pickup_queue": "coordinator_only",
    # T-0630: edits the worker linux user's OWN ~/.claude/settings.json — a
    # single coordinator-owned file, like the rest of the project-state ops.
    "fleet_model_get": "coordinator_only",
    "fleet_model_set": "coordinator_only",
    "task_new": "coordinator_only",
    "doc_new": "coordinator_only",
    "initiative_new": "coordinator_only",
    "bind_task": "coordinator_only",
    "bind_initiative": "coordinator_only",
    # T-0184: a dev session silences its OWN drift checks via the user-worker —
    # it writes only its own SessionMd frontmatter (filesystem-local), so
    # tmux_only (no coordinator privilege required).
    "set_drift_paused": "tmux_only",
    # T-0655: the operator toggles its OWN drive continuity flag — writes
    # only its own SessionMd frontmatter (filesystem-local), tmux_only like
    # set_drift_paused.
    "set_drive": "tmux_only",
    # T-0678: a session sets its OWN per-session model override — writes only
    # its own SessionMd frontmatter (filesystem-local), tmux_only like
    # set_drive/set_drift_paused.
    "set_model": "tmux_only",
    # T-0466: a session postpones its OWN cache-window recycle by stamping its
    # own SessionMd frontmatter (filesystem-local) — tmux_only, like the drift
    # off-ramp; no coordinator privilege required.
    "idle_postpone": "tmux_only",
    # T-0509: a user session morphs its OWN role by stamping its own SessionMd
    # frontmatter (filesystem-local) — tmux_only, like idle_postpone. The
    # operator-singleton guard reads mds + a tmux pane scan, both user-local.
    "morph_session": "tmux_only",
    "unbind_task": "coordinator_only",
    # T-0324 (H2): walks every SessionMd + the backlog under the claim flock —
    # single-writer semantics, same as the other binding mutators/reconcilers.
    "rehome_primary": "coordinator_only",
    "unbind_initiative": "coordinator_only",
    "archive_session": "coordinator_only",
    "unarchive_session": "coordinator_only",
    # T-0072/0073/0077: reconcilers walk SessionMd + live tmux; coordinator-only
    # because the scheduler tick and migration semantics expect a single writer
    # per host. (Filesystem & tmux are user-local; multi-user multi-host
    # coordination is out of scope for this bundle.)
    "gc_sessions": "coordinator_only",
    "dedup_sessions": "coordinator_only",
    "prune_orphan_teams": "coordinator_only",
    "gc_stale_bindings": "coordinator_only",
    "peer_rebind_sid": "coordinator_only",
    # T-0142/0144: reconcilers + Team entity walk SessionMd + live tmux; the
    # scheduler tick is the single writer, so coordinator-only. sync_session_name
    # mutates tmux + the registry (tmux-local) but is coordinator-gated for the
    # same single-writer reason the other reconcilers are.
    "gc_dead_bindings": "coordinator_only",
    "archive_dead_teammates": "coordinator_only",
    "reconcile_teams": "coordinator_only",
    "list_teams": "coordinator_only",
    "archive_team": "coordinator_only",
    "resurrect_team": "coordinator_only",
    "sync_session_name": "coordinator_only",
    # T-0085: autoupdate handoff is install-scoped (coordinator).
    "autoupdate_retry": "coordinator_only",
    "autoupdate_force": "coordinator_only",
    # T-0089: scheduler-coupled (modifies the autoupdate job's next_run).
    "autoupdate_check_now": "coordinator_only",
    # T-0054: rebuilds the global Config (touches module-level state).
    "reload_projects": "coordinator_only",
}


def _mode_allows(mode: str, tag: str) -> bool:
    if tag == "both":
        return True
    if mode == "coordinator":
        return True  # coordinator also serves its own user — all tags allowed
    # user-worker: only tmux_only + both
    return tag == "tmux_only"


def dispatch(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Look up `name` in the allowlist; reject if missing or mode-disallowed; invoke."""
    handler = ACTION_REGISTRY.get(name)
    if handler is None:
        raise ActionError(f"unknown action: {name!r}")
    tag = ACTION_MODES.get(name, "coordinator_only")
    mode = get_mode()
    if not _mode_allows(mode, tag):
        raise ActionError(f"action {name!r} not available in {mode} mode")
    return handler(params or {})
