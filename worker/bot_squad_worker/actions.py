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


def _action_tg_notify(params: dict[str, Any]) -> dict[str, Any]:
    """Send a Telegram message, with optional SID prefix and debounce.

    Params (all optional except ``message``):
        message  : str  — required; the text to send
        chat_id  : str  — explicit chat; takes precedence over slug
        slug     : str  — project slug; resolved to tg_chat in projects.toml
        sid      : str  — SID prefix component  (e.g. "S-almdudleer-claude-p5")
        user     : str  — user prefix component
        debounce : bool — default True; T-0569 pass False to force delivery
                   even if the exact same payload was just sent (an interactive
                   relay reply must not be silently deduped).

    If neither ``chat_id`` nor ``slug`` is given, falls back to the first
    project's tg_chat (there is usually only one project).  Unknown slug
    raises ActionError.

    Returns {ok: true, sent: <bool>}.
    """
    extra = set(params) - _TG_NOTIFY_ALLOWED
    if extra:
        raise ActionError(f"tg_notify got unexpected params: {sorted(extra)}")
    if "message" not in params:
        raise ActionError("tg_notify missing required param: message")

    cfg = _get_config()

    # --- resolve chat_id (+ project-bound forum topic, T-0156) ---
    # An explicit topic_id param wins; otherwise, when the chat is resolved
    # from a project, inherit that project's tg_topic_id so group bindings
    # land in the right forum thread without the caller spelling it out.
    chat_id: str | None = params.get("chat_id") or None
    topic_id: int | None = _coerce_topic_id(params.get("topic_id"))
    slug: str = params.get("slug") or ""
    if not chat_id:
        if slug:
            project = cfg.projects.get(slug)
            if project is None:
                raise ActionError(f"tg_notify: unknown project slug {slug!r}")
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
    topic_class = params.get("topic") or ""
    if topic_id is None and topic_class and slug:
        from bot_squad_worker import tg_topics as _tg_topics
        topic_id = _tg_topics.resolve(cfg, slug, topic_class)

    message = params["message"]
    urgent = bool(params.get("urgent", False))
    do_slim = True
    if bool(params.get("needs_input", False)):
        from bot_squad_worker import tg_stall as _tg_stall
        session_name = params.get("tmux_session") or _resolve_tmux_session(
            cfg, params.get("slug", ""), params.get("sid", "")
        )
        # T-0610 review fix: slim the QUESTION before the escalation footer is
        # composed — the SSOT's blanket slim would cut the tmux-attach footer
        # off the end, which is the page's whole point.
        message = _tg_stall.build_escalation_text(
            cfg, params.get("sid", ""), _slim_page(message), session_name
        )
        urgent = True
        do_slim = False

    # An EXPLICIT chat_id/topic_id is a TG group/forum target (MAX has no such
    # binding), so those stay on TG. This decision is computed from the RAW
    # params (NOT the resolved topic_id / topic-class) — a topic-class must never
    # disqualify MAX-primary (T-0386 flaw-watch). Delivery itself is the
    # _send_stakeholder_dm SSOT (T-0394).
    explicit_tg_target = bool(params.get("chat_id")) or (params.get("topic_id") not in (None, ""))
    debounce = bool(params.get("debounce", True))
    return _send_stakeholder_dm(
        cfg,
        message=message,
        sid=params.get("sid", ""),
        user=params.get("user", ""),
        urgent=urgent,
        tg_chat_id=chat_id,
        tg_topic_id=topic_id,
        prefer_tg=explicit_tg_target,
        debounce=debounce,
        do_slim=do_slim,
        slug=slug,
        task_id=str(params.get("task_id") or ""),
    )


_PAGE_MODES = ("auto", "tg", "max")
# T-0610: stakeholder-facing pages are short-form — headline + refs; detail
# stays in tasks/threads ("слишком подробные сводки ... очень много подробностей").
_PAGE_SLIM_LIMIT = 400


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


def _slim_page(text: str, link: str = "") -> str:
    """T-0610: cap a page at headline size. Over-limit text is cut at a
    line/sentence boundary with an explicit continuation pointer — pages must
    be short, but never silently truncated mid-word.

    T-0635: the pointer is a real clickable ``link`` (staging web URL to the
    task/session), not the bare words "см. задачу/тред" — a plain-text
    pointer read as evasive/broken to the stakeholder. ``link`` is best-effort
    (built by ``_page_detail_link`` from whatever the call site has on hand);
    an empty link falls back to the old generic phrasing rather than emitting
    a dangling "подробнее: " with nothing after it.
    """
    t = (text or "").strip()
    if len(t) <= _PAGE_SLIM_LIMIT:
        return t
    cut = t[:_PAGE_SLIM_LIMIT]
    for sep in ("\n", ". "):
        i = cut.rfind(sep)
        if i > 100:
            cut = cut[:i]
            break
    cut = cut.rstrip(" .")
    if link:
        return f"{cut}\n… подробнее: {link}"
    return cut + "\n… (детали: см. задачу/тред)"


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
    base = staging_url.rstrip("/")
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
) -> dict[str, Any]:
    """SSOT for paging the human (T-0247 lineage, T-0394 dedupe, T-0610 inversion).

    TG is PRIMARY: the DPI-block premise behind the old MAX-primary logic died
    2026-07-04 (dead proxy removed, direct TG works). MAX is the RESERVE — it
    delivers when TG errors or no TG chat is configured (auto-failover; do NOT
    remove the MAX transport, this host has DPI history), or while the
    stakeholder's temporary 'max' page-mode is set (``page_channel`` action).

    ONE page = ONE delivery (T-0610 DoD): the MAX-ping + TG-group-record pair
    was the duplicate he complained about. ``group_record`` is now a compat
    no-op — the TG-primary delivery already lands in the group/topic the
    record used to go to.

    Pages are SHORT-FORM (``_slim_page``): headline + refs, detail in tasks.
    Slimming applies only to default-routed PAGES: an explicitly-addressed
    send (``prefer_tg``, e.g. the T-0569 conversation-relay replies to the
    stakeholder's DM) is conversational content, not a page — never truncated.
    ``do_slim=False`` lets a caller that already slimmed its question part
    (needs-input escalations, whose tmux-attach footer must survive) opt out.

    ``slug``/``task_id`` (T-0635, both optional) feed ``_page_detail_link`` so
    a truncated page's continuation pointer is a real staging-web link instead
    of the bare words "см. задачу/тред" — see that function for the fallback
    chain when a caller doesn't have them on hand.

    ``prefer_tg`` (an explicit group/forum target MAX can't honor) stays
    TG-only: no MAX fallback for group-addressed content; TG errors propagate
    to the caller as before.

    Returns ``{ok, sent, channel}``. ``channel: "none"`` (ok=False) when no
    transport could deliver — logged loudly, never a silent no-op.
    """
    del group_record  # T-0610: compat no-op — one page, one delivery
    if do_slim and not prefer_tg:
        link = _page_detail_link(
            cfg, slug=slug, tg_chat_id=tg_chat_id, sid=sid, task_id=task_id,
        )
        message = _slim_page(message, link)

    # T-0644: slug-qualified label for the [<sid>] prefix — falls back to the
    # bare sid when no slug is on hand (see sid_display_label).
    from bot_squad_worker import sessions as _sessions
    sid_label = _sessions.sid_display_label(sid, slug) if slug else sid

    def _try_tg() -> dict[str, Any] | None:
        if not tg_chat_id:
            return None
        sent = _get_tg_client(cfg).send(
            chat_id=tg_chat_id, text=message, sid=sid_label, user=user,
            urgent=urgent, topic_id=tg_topic_id, debounce=debounce,
        )
        return {"ok": True, "sent": sent, "channel": "tg"}

    def _try_max() -> dict[str, Any] | None:
        max_chat = getattr(cfg, "max_default_chat_id", "") or ""
        if not max_chat:
            return None
        sent = _get_max_client(cfg).send(
            chat_id=max_chat, text=message, sid=sid_label, user=user, urgent=urgent,
            recipient_kind=getattr(cfg, "max_recipient_kind", "chat_id"),
        )
        return {"ok": True, "sent": sent, "channel": "max"}

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
    try:
        queue_id = _deploy.enqueue(
            cfg,
            slug=slug,
            target=target,
            reason=params["reason"],
            requested_by=params["requested_by"],
            restart_worker=bool(params.get("restart_worker", False)),
        )
    except ValueError as e:
        raise ActionError(f"deploy: {e}") from e

    # T-0458: echo the to-be-built commit (origin/<deploy_branch> tip) so the
    # requester sees WHICH commit this deploy will ship up front. Best-effort —
    # "" if it can't be resolved; the authoritative sha is re-parsed post-build.
    target_sha = _deploy.resolve_target_sha(cfg, slug, target)

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
        from bot_squad_worker import sessions as _sessions
        from bot_squad_worker import tg_topics as _tg_topics
        tg = _get_tg_client(cfg)
        tg.send(
            chat_id=project.tg_chat,  # type: ignore[attr-defined]
            text=f"🟡 deploys paused for {slug} — {meta['reason']} (by {meta['paused_by']})",
            sid=_sessions.sid_display_label("deploy_monitor", slug),
            topic_id=_tg_topics.resolve(cfg, slug, "deploy_logs"),
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
        from bot_squad_worker import sessions as _sessions
        from bot_squad_worker import tg_topics as _tg_topics
        tg = _get_tg_client(cfg)
        tg.send(
            chat_id=project.tg_chat,  # type: ignore[attr-defined]
            text=f"🟢 deploys resumed for {slug} (by {who})",
            sid=_sessions.sid_display_label("deploy_monitor", slug),
            topic_id=_tg_topics.resolve(cfg, slug, "deploy_logs"),
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
    )


# ---------------------------------------------------------------------------
# ensure_user_conversation action (T-0478, M2/F2.4)
# ---------------------------------------------------------------------------

_ENSURE_UCONV_REQUIRED = {"slug", "global_user_id"}
# T-0623: optional explicit model override for the fresh-spawn path; absent
# falls through to sessions.spawn's role default (user-conversation -> claude-sonnet-5).
_ENSURE_UCONV_ALLOWED = _ENSURE_UCONV_REQUIRED | {"message_ref", "model"}


def _user_conversation_boot_prompt(
    slug: str, global_user_id: str, message_ref: str | None
) -> str:
    """The initial prompt a freshly-spawned user-conversation session boots on.

    Unlike a dev brief (task-centric, assembled by `bsq spawn`), a user-
    conversation session holds no ticket — it attends a thread. So we orient it
    explicitly: run `bsq brief` for its full role contract (now resolved to
    user-conversation.md), then read its thread + the new inbound message. The
    behavioural mandate (verbatim-into-tasks, notify-operator, unrestricted)
    lives in the role contract; this prompt points at it and supplies the
    per-session context (which user, which message)."""
    new_msg = ""
    if message_ref and str(message_ref).strip():
        new_msg = (
            "\nThe message that triggered this spawn:\n"
            f"  {str(message_ref).strip()}\n"
        )
    return f"""\
You are a USER-CONVERSATION session (system-controlled), spawned on incoming
user mail for project `{slug}`, attending the user `{global_user_id}`.

FIRST run `bsq brief` to load your full role contract (user-conversation.md) +
the product/protocol. Your mandate, in short:
  - Read this user's conversation thread (your durable memory) before replying:
    GET /api/conversations/{slug}/{global_user_id}/messages
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
{new_msg}"""


def _user_conversation_resume_prompt(
    slug: str, gid: str, message_ref: Any
) -> str:
    """Wake prompt for a RESUMED (recycled) attendant. Unlike the fresh-spawn
    boot prompt it re-orients rather than onboards — the conversation context
    rides in via ``claude --resume``."""
    new_msg = ""
    if message_ref and str(message_ref).strip():
        new_msg = (
            "\nThe message that triggered this resume:\n"
            f"  {str(message_ref).strip()}\n"
        )
    return (
        f"Your user-conversation session (user `{gid}`, project `{slug}`) was "
        "recycled and has now been RESUMED on new incoming mail. Re-read this "
        f"user's thread (GET /api/conversations/{slug}/{gid}/messages) and "
        f"respond to the new message.\n{new_msg}"
    )


def _resume_recycled_user_conversation(
    cfg: Any, slug: str, gid: str, message_ref: Any
) -> str | None:
    """T-0575: resume the newest recycled (compact-terminate-remembered)
    attendant for ``(slug, gid)`` when its remembered context fits the <50k
    budget. Returns the resumed SID, or None when there is no eligible
    candidate / the resume failed without leaving a live attendant — the
    caller then falls back to a fresh spawn."""
    from bot_squad_worker import sessions as _sessions

    want = _sessions.user_conversation_window(gid)
    cand = next(
        (r for r in _sessions.resumable_sessions(cfg, slug)
         if _sessions._window_from_sid(r["sid"]) == want),
        None,
    )
    if cand is None:
        return None
    ok, tokens = _sessions.recycled_resume_eligible(cand["claude_uuid"])
    if not ok:
        log.info(
            "ensure_user_conversation: recycled %s not resume-eligible "
            "(context=%s tokens) — fresh spawn", cand["sid"], tokens)
        return None
    try:
        res = _sessions.resume(
            cfg, slug, cand["sid"],
            initial_prompt=_user_conversation_resume_prompt(
                slug, gid, message_ref),
        )
        return res.get("sid")
    except Exception:  # noqa: BLE001 — resume failure must never fail the ensure
        log.exception(
            "ensure_user_conversation: resume of recycled %s failed", cand["sid"])
        # A LATE failure (e.g. composer-ready timeout delivering the wake
        # prompt) leaves the resumed pane LIVE — a fresh spawn then would
        # violate single-attendant. Re-check liveness: only a truly dead
        # resume falls back to the spawn path.
        try:
            return _sessions.live_user_conversation_sid(cfg, slug, gid)
        except Exception:  # noqa: BLE001
            return None


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
                     falls through to sessions.spawn's role default).
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
                try:
                    _action_inject_input({
                        "sid": existing,
                        "text": ("A new message arrived in your user-"
                                 "conversation thread — read it and respond."),
                    })
                except ActionError:
                    pass
            return {"ok": True, "sid": existing, "spawned": False}

        # T-0575: prefer RESUMING this user's recycled attendant over a fresh
        # spawn (stakeholder 2026-07-04: "<50k tokens context → resume the same
        # user's last session"). Still under the flock, so the resumed pane
        # can't race a concurrent ensure into a duplicate. Any ineligibility
        # or failure falls through to the fresh-spawn path below.
        resumed = _resume_recycled_user_conversation(cfg, slug, gid, message_ref)
        if resumed is not None:
            return {"ok": True, "sid": resumed, "spawned": False,
                    "resumed": True}

        # Spawn: no live attendant → open one in the gid-keyed window.
        result = _sessions.spawn(
            cfg,
            slug,
            window,
            _user_conversation_boot_prompt(slug, gid, message_ref),
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
    (``teamlead``/``dev``/``all``) keep using the caller's ``slug`` — those are
    inherently an in-project broadcast, not addressed at a specific SID.
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
    if to not in {"teamlead", "dev", "all"}:
        from bot_squad_worker.park import _slug_for_sid
        recipient_slug = _slug_for_sid(cfg, to)
        if recipient_slug is None:
            raise ActionError(
                f"peer_send: recipient SID {to!r} has no registered session "
                f"under any known project — refusing to deliver into sender's "
                f"project {params['slug']!r} (would silently misfile)"
            )
        delivery_slug = recipient_slug

    from bot_squad_worker import intersession as _is
    result = _is.send(
        cfg, delivery_slug, params["from_sid"], to, params["text"],
        user=params.get("user"),
    )

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
            from bot_squad_worker import sessions as _sessions
            _get_tg_client(cfg).send(
                chat_id=chat_id,
                text=params["text"],
                sid=_sessions.sid_display_label(params["from_sid"], delivery_slug),
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
    return {"ok": True, "model": fleet_model.get_model()}


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
    model = params["model"]
    try:
        fleet_model.set_model(model)
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
# T-0174: generalized entity_new actions (doc / uc / flow / initiative).
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
_DOC_NEW_ALLOWED = _DOC_NEW_REQUIRED


def _action_doc_new(params: dict[str, Any]) -> dict[str, Any]:
    """Allocate the next D-NNNN id and write a stub doc md (composes T-0172).

    Required params: slug, category, title
    Returns: {ok, id, file_path, category}

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

    docs_dir = cfg.data_dir / slug / "docs" / category
    docs_dir.mkdir(parents=True, exist_ok=True)
    new_id = idalloc.allocate_id(cfg.data_dir, slug, "doc")
    file_path = docs_dir / f"{new_id}-{_slugify_title(title)}.md"

    fm = "\n".join([
        f"id: {new_id}",
        f"title: {_yaml_quote(title)}",
        f"category: {category}",
        "status: draft",
        f"created: {_now_iso()}",
        "related_tickets: []",
    ])
    content = f"---\n{fm}\n---\n\n# {title}\n\n(filed via doc_new — T-0172 docs system)\n"
    _atomic_write_new(file_path, content)
    return {"ok": True, "id": new_id, "file_path": str(file_path), "category": category}


_UC_NEW_REQUIRED = {"slug", "title"}
_UC_NEW_ALLOWED = _UC_NEW_REQUIRED


def _action_uc_new(params: dict[str, Any]) -> dict[str, Any]:
    """Allocate the next UC-NNNN id and write a stub use-case md (composes T-0159/T-0173).

    Required params: slug, title
    Returns: {ok, id, file_path}

    Storage: ``data/<slug>/use_cases/UC-NNNN.md`` — the filename stem IS the id
    (what routes_usecases keys on). Legacy slug-named UCs (``UC-<slug>.md``) are
    non-numeric, so the scan ignores them and they keep working alongside the
    new numeric ones.
    """
    from bot_squad_worker import idalloc

    cfg, slug = _entity_setup(params, _UC_NEW_REQUIRED, _UC_NEW_ALLOWED, "uc_new")
    title = _require_str(params, "title", "uc_new")

    uc_dir = cfg.data_dir / slug / "use_cases"
    uc_dir.mkdir(parents=True, exist_ok=True)
    new_id = idalloc.allocate_id(cfg.data_dir, slug, "uc")
    file_path = uc_dir / f"{new_id}.md"

    fm = "\n".join([
        f"id: {new_id}",
        f"title: {_yaml_quote(title)}",
        "user_persona: TBD",
        "goal: TBD",
        "preconditions: TBD",
        "success_criteria: TBD",
        "related_tickets: []",
        "status: draft",
    ])
    body = (
        f"# {title}\n\n## Steps\n\n1. TBD\n\n## Feedback\n\n"
        "(filed via uc_new — attach user flows with `bsq flow new "
        f"{new_id} <title>`)\n"
    )
    content = f"---\n{fm}\n---\n\n{body}"
    _atomic_write_new(file_path, content)
    return {"ok": True, "id": new_id, "file_path": str(file_path)}


_FLOW_NEW_REQUIRED = {"slug", "uc_id", "title"}
_FLOW_NEW_ALLOWED = _FLOW_NEW_REQUIRED
_UC_ID_RE = re.compile(r"^UC-[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _action_flow_new(params: dict[str, Any]) -> dict[str, Any]:
    """Allocate the next UF-NNNN id and write a stub user-flow md (composes T-0173).

    Required params: slug, uc_id, title
    Returns: {ok, id, file_path, uc_id}

    Storage: ``data/<slug>/use_cases/<uc-id>/flows/UF-NNNN-<slug>.md``. The
    parent use case must exist (either ``<uc-id>.md`` or a ``<uc-id>/`` dir).
    The flow counter is per-project (one UF-NNNN sequence across all UCs). The
    "UF-" (user-flow) prefix is distinct from curated feedback's "F-" (T-0180).
    """
    from bot_squad_worker import idalloc

    cfg, slug = _entity_setup(params, _FLOW_NEW_REQUIRED, _FLOW_NEW_ALLOWED, "flow_new")
    uc_id = _require_str(params, "uc_id", "flow_new")
    if not _UC_ID_RE.match(uc_id):
        raise ActionError(f"flow_new: invalid uc_id {uc_id!r}")
    title = _require_str(params, "title", "flow_new")

    uc_root = cfg.data_dir / slug / "use_cases"
    if not (uc_root / f"{uc_id}.md").exists() and not (uc_root / uc_id).is_dir():
        raise ActionError(f"flow_new: unknown use case {uc_id!r}")

    flows_dir = uc_root / uc_id / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    new_id = idalloc.allocate_id(cfg.data_dir, slug, "flow")
    file_path = flows_dir / f"{new_id}-{_slugify_title(title)}.md"

    fm = "\n".join([
        f"id: {new_id}",
        f"uc_id: {uc_id}",
        f"title: {_yaml_quote(title)}",
        "status: draft",
        f"created: {_now_iso()}",
    ])
    body = (
        f"# {title}\n\n## Steps\n\n1. TBD\n\n## Mermaid\n\n"
        "```mermaid\ngraph TD\n  A[start] --> B[TBD]\n```\n\n"
        "(filed via flow_new — T-0173 user flows)\n"
    )
    content = f"---\n{fm}\n---\n\n{body}"
    _atomic_write_new(file_path, content)
    return {"ok": True, "id": new_id, "file_path": str(file_path), "uc_id": uc_id}


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


ACTION_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "noop": _action_noop,
    "tg_verify_login": _action_tg_verify_login,
    "tg_notify": _action_tg_notify,
    # T-0247: MAX (max.ru) DM channel — mirrors tg_notify.
    "max_notify": _action_max_notify,
    # T-0610: temporary page-channel switch (TG-primary default / MAX reserve).
    "page_channel": _action_page_channel,
    "tg_stall_clear": _action_tg_stall_clear,
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
    "inject_input": _action_inject_input,
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
    # T-0630: fleet-default `claude --model` (~/.claude/settings.json).
    "fleet_model_get": _action_fleet_model_get,
    "fleet_model_set": _action_fleet_model_set,
    # T-0042: atomic T-NNNN allocator (flock-protected).
    "task_new": _action_task_new,
    "doc_new": _action_doc_new,
    "uc_new": _action_uc_new,
    "flow_new": _action_flow_new,
    "initiative_new": _action_initiative_new,
    "bind_task": _action_bind_task,
    "bind_initiative": _action_bind_initiative,
    # T-0237 Layer-2: operator-invoked reuse-vs-spawn dispatch recommendation.
    "dispatch_decision": _action_dispatch_decision,
    # T-0576 (M11/F11.3): instant-tweak vs long-request placement guarantee.
    "placement_decision": _action_placement_decision,
    # T-0184: per-session drift-check off-ramp (bsq drift on/off).
    "set_drift_paused": _action_set_drift_paused,
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
    "inject_input": "tmux_only",
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
    # T-0630: edits the worker linux user's OWN ~/.claude/settings.json — a
    # single coordinator-owned file, like the rest of the project-state ops.
    "fleet_model_get": "coordinator_only",
    "fleet_model_set": "coordinator_only",
    "task_new": "coordinator_only",
    "doc_new": "coordinator_only",
    "uc_new": "coordinator_only",
    "flow_new": "coordinator_only",
    "initiative_new": "coordinator_only",
    "bind_task": "coordinator_only",
    "bind_initiative": "coordinator_only",
    # T-0184: a dev session silences its OWN drift checks via the user-worker —
    # it writes only its own SessionMd frontmatter (filesystem-local), so
    # tmux_only (no coordinator privilege required).
    "set_drift_paused": "tmux_only",
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
