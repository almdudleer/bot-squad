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


_TG_NOTIFY_ALLOWED = {"slug", "chat_id", "message", "sid", "user", "urgent"}


def _action_tg_notify(params: dict[str, Any]) -> dict[str, Any]:
    """Send a Telegram message, with optional SID prefix and debounce.

    Params (all optional except ``message``):
        message  : str  — required; the text to send
        chat_id  : str  — explicit chat; takes precedence over slug
        slug     : str  — project slug; resolved to tg_chat in projects.toml
        sid      : str  — SID prefix component  (e.g. "S-almdudleer-claude-p5")
        user     : str  — user prefix component

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

    # --- resolve chat_id ---
    chat_id: str | None = params.get("chat_id") or None
    if not chat_id:
        slug: str = params.get("slug") or ""
        if slug:
            project = cfg.projects.get(slug)
            if project is None:
                raise ActionError(f"tg_notify: unknown project slug {slug!r}")
            chat_id = project.tg_chat
        else:
            # Fallback: first registered project's chat (single-project setups)
            if cfg.projects:
                chat_id = next(iter(cfg.projects.values())).tg_chat
            else:
                raise ActionError("tg_notify: no chat_id, no slug, and no projects configured")

    tg = _get_tg_client(cfg)
    sent = tg.send(
        chat_id=chat_id,
        text=params["message"],
        sid=params.get("sid", ""),
        user=params.get("user", ""),
        urgent=bool(params.get("urgent", False)),
    )
    return {"ok": True, "sent": sent}


_DEPLOY_REQUIRED = {"slug", "target", "reason", "requested_by"}
_DEPLOY_ALLOWED = _DEPLOY_REQUIRED


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
        )
    except ValueError as e:
        raise ActionError(f"deploy: {e}") from e

    import time as _time
    return {"ok": True, "queue_id": queue_id, "queued_at": _time.time()}


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
        tg = _get_tg_client(cfg)
        tg.send(
            chat_id=project.tg_chat,  # type: ignore[attr-defined]
            text=f"🟡 deploys paused for {slug} — {meta['reason']} (by {meta['paused_by']})",
            sid="deploy_monitor",
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
        tg = _get_tg_client(cfg)
        tg.send(
            chat_id=project.tg_chat,  # type: ignore[attr-defined]
            text=f"🟢 deploys resumed for {slug} (by {who})",
            sid="deploy_monitor",
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
_RESUME_SESSION_ALLOWED = _RESUME_SESSION_REQUIRED


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
    return _sessions.resume(cfg, params["slug"], params["sid"])


_SPAWN_SESSION_REQUIRED = {"slug", "window"}
_SPAWN_SESSION_ALLOWED = _SPAWN_SESSION_REQUIRED | {"initial_prompt", "task_id", "initiative", "owner"}


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
    return _sessions.spawn(
        cfg,
        params["slug"],
        params["window"],
        params.get("initial_prompt"),
        task_id=params.get("task_id"),
        initiative=params.get("initiative"),
        owner=params.get("owner"),
    )


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

    # tmux wraps long send-keys payloads as a bracketed-paste escape sequence;
    # an Enter chained in the same send-keys call lands inside the paste and
    # does NOT submit Claude's input box. Send text and Enter as separate
    # send-keys invocations with a brief pause, mirroring spawn_session.
    import subprocess
    import time as _time
    lines_sent = 0
    for line in text.split("\n"):
        subprocess.run(
            ["tmux", "send-keys", "-t", pane.pane_id, "--", line],
            check=False,
        )
        _time.sleep(0.4)
        subprocess.run(
            ["tmux", "send-keys", "-t", pane.pane_id, "Enter"],
            check=False,
        )
        lines_sent += 1
    return {"ok": True, "pane_id": pane.pane_id, "lines_sent": lines_sent}


# ---------------------------------------------------------------------------
# Autonomous orchestrator actions (spec #8)
# ---------------------------------------------------------------------------

_AUTO_STATUS_ALLOWED = {"slug"}


def _action_autonomous_status(params: dict[str, Any]) -> dict[str, Any]:
    """Return the current autonomous orchestrator state for a project.

    Required params: slug
    Returns: {enabled, status, current_task_id, current_pane_id, last_tick_at,
              sleep_start_hour, sleep_end_hour, tick_log (last 5)}
    """
    extra = set(params) - _AUTO_STATUS_ALLOWED
    if extra:
        raise ActionError(f"autonomous_status got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autonomous_status missing required param: slug")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autonomous_status: unknown project slug {slug!r}")

    from bot_squad_worker import autonomous as _auto
    state = _auto.load_state(cfg, slug)
    from dataclasses import asdict
    d = asdict(state)
    # Return last 5 tick log entries in status (full log via log endpoint)
    d["tick_log"] = state.tick_log[-5:]
    return {"ok": True, **d}


_AUTO_ENABLE_ALLOWED = {"slug", "sleep_start_hour", "sleep_end_hour"}


def _action_autonomous_enable(params: dict[str, Any]) -> dict[str, Any]:
    """Enable the autonomous orchestrator for a project.

    Required params: slug
    Optional params: sleep_start_hour (int, default 22), sleep_end_hour (int, default 8)
    Returns: {ok: true, slug, enabled: true}
    """
    extra = set(params) - _AUTO_ENABLE_ALLOWED
    if extra:
        raise ActionError(f"autonomous_enable got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autonomous_enable missing required param: slug")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autonomous_enable: unknown project slug {slug!r}")

    from bot_squad_worker import autonomous as _auto
    state = _auto.load_state(cfg, slug)
    state.enabled = True
    if "sleep_start_hour" in params:
        state.sleep_start_hour = int(params["sleep_start_hour"])
    if "sleep_end_hour" in params:
        state.sleep_end_hour = int(params["sleep_end_hour"])
    _auto.save_state(cfg, state)
    return {"ok": True, "slug": slug, "enabled": True}


_AUTO_DISABLE_ALLOWED = {"slug"}


def _action_autonomous_disable(params: dict[str, Any]) -> dict[str, Any]:
    """Disable the autonomous orchestrator for a project.

    Required params: slug
    Returns: {ok: true, slug, enabled: false}

    In-flight tasks complete normally; the orchestrator won't start new ones.
    """
    extra = set(params) - _AUTO_DISABLE_ALLOWED
    if extra:
        raise ActionError(f"autonomous_disable got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autonomous_disable missing required param: slug")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autonomous_disable: unknown project slug {slug!r}")

    from bot_squad_worker import autonomous as _auto
    state = _auto.load_state(cfg, slug)
    state.enabled = False
    _auto.save_state(cfg, state)
    return {"ok": True, "slug": slug, "enabled": False}


# ---------------------------------------------------------------------------
# Cross-session message bus actions (Phase 1)
# ---------------------------------------------------------------------------

_PEER_SEND_REQUIRED = {"slug", "from_sid", "to", "text"}
_PEER_SEND_ALLOWED = _PEER_SEND_REQUIRED

# T-0035 (lean Option B): peer_send replies to a UI-shaped SID are mirrored
# to that user's bound Telegram chat so a stakeholder browsing the UI still
# sees responses while the in-UI chat panel is a follow-up.
# Format: S-<username>-ui-p<N> (Sessions.tsx::handleSend builds this).
_UI_SID_RE = re.compile(r"^S-([A-Za-z0-9_-]+)-ui-p\d+$")


def _parse_ui_sid_username(sid: str) -> str | None:
    """Return the username if ``sid`` looks like a UI-originated SID, else None."""
    m = _UI_SID_RE.match(sid)
    return m.group(1) if m else None


def _lookup_user_tg_chat_id(cfg: Any, username: str) -> str:
    """Load auth.toml and return user_meta[<username>].tg_chat_id (or "" if unset/missing)."""
    auth_path = Path(cfg.config_dir) / "auth.toml"
    if not auth_path.exists():
        return ""
    try:
        raw = tomllib.loads(auth_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("peer_send tg-mirror: failed to read auth.toml: %s", e)
        return ""
    meta = raw.get("user_meta", {}).get(username, {}) or {}
    return str(meta.get("tg_chat_id", "") or "")


def _action_peer_send(params: dict[str, Any]) -> dict[str, Any]:
    """Append a message to recipient inbox(es).

    Required params: slug, from_sid, to, text
    Returns: {ok: true, delivered_to: [sid, ...]}

    T-0035: any delivered SID matching ``S-<user>-ui-p<N>`` also fires a
    ``tg.send`` to that user's bound ``tg_chat_id`` (from
    ``config/auth.toml [user_meta.<user>]``). Mirror failures are logged
    but never break the bus write — delivered_to still reflects the inbox.
    """
    extra = set(params) - _PEER_SEND_ALLOWED
    if extra:
        raise ActionError(f"peer_send got unexpected params: {sorted(extra)}")
    missing = _PEER_SEND_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_send missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import intersession as _is
    result = _is.send(cfg, params["slug"], params["from_sid"], params["to"], params["text"])

    for recipient_sid in result.get("delivered_to", []):
        username = _parse_ui_sid_username(recipient_sid)
        if not username:
            continue
        chat_id = _lookup_user_tg_chat_id(cfg, username)
        if not chat_id:
            log.debug(
                "peer_send tg-mirror: user %r has no tg_chat_id bound — skipping",
                username,
            )
            continue
        try:
            _get_tg_client(cfg).send(
                chat_id=chat_id,
                text=params["text"],
                sid=params["from_sid"],
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
    context sections are preserved exactly. Text is sanitised: newlines
    collapsed to spaces, capped at 240 chars.
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
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise ActionError(f"task_progress_add: task not found: {task_id}")
    path = matches[0]

    from datetime import datetime, timezone
    import os as _os
    import re as _re
    from bot_squad_worker.task_body import append_progress, _sanitize_progress_text

    text_raw = path.read_text()
    fm_match = _re.match(r"\A---\n(.*?)\n---\n(.*)", text_raw, _re.DOTALL)
    if not fm_match:
        raise ActionError(f"task_progress_add: no frontmatter in {path}")
    fm_block = fm_match.group(1)
    body = fm_match.group(2).lstrip("\n")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        new_body = append_progress(body, ts, sid, text)
    except ValueError as e:
        raise ActionError(f"task_progress_add: {e}") from e

    # Update `updated:` in place (or append) without parsing YAML — the same
    # line-based pattern autonomous._patch_task_file uses.
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

    content = f"---\n{new_fm}\n---\n\n{new_body}"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    _os.rename(tmp, path)

    line = f"- {ts} · {sid} · {_sanitize_progress_text(text)}"
    return {"ok": True, "task_id": task_id, "line_appended": line}


_PEER_INBOX_WAIT_REQUIRED = {"slug", "sid", "timeout"}
_PEER_INBOX_WAIT_ALLOWED = _PEER_INBOX_WAIT_REQUIRED


def _action_peer_inbox_wait(params: dict[str, Any]) -> dict[str, Any]:
    """Long-poll until inbox grows past the seen offset, or timeout.

    Required params: slug, sid, timeout (seconds, capped at 1800)
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
    "deploy": _action_deploy,
    "pause_deploys": _action_pause_deploys,
    "resume_deploys": _action_resume_deploys,
    "list_sessions": _action_list_sessions,
    "pause_session": _action_pause_session,
    "suspend_session": _action_suspend_session,
    "resume_session": _action_resume_session,
    "spawn_session": _action_spawn_session,
    "scheduler_state": _action_scheduler_state,
    "inject_input": _action_inject_input,
    "autonomous_status": _action_autonomous_status,
    "autonomous_enable": _action_autonomous_enable,
    "autonomous_disable": _action_autonomous_disable,
    "peer_send": _action_peer_send,
    "peer_inbox_read": _action_peer_inbox_read,
    "peer_inbox_wait": _action_peer_inbox_wait,
    "task_progress_add": _action_task_progress_add,
    "bind_task": _action_bind_task,
    "bind_initiative": _action_bind_initiative,
    "unbind_task": _action_unbind_task,
    "unbind_initiative": _action_unbind_initiative,
    "archive_session": _action_archive_session,
    "unarchive_session": _action_unarchive_session,
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
#   deploy queue, peer inbox, autonomous orchestrator).
# tmux_only — purely tmux/filesystem ops local to a Linux user.
# both — universally safe (proof-of-life).
ACTION_MODES: dict[str, str] = {
    "noop": "both",
    "tg_verify_login": "coordinator_only",
    "tg_notify": "coordinator_only",
    "deploy": "coordinator_only",
    "pause_deploys": "coordinator_only",
    "resume_deploys": "coordinator_only",
    "list_sessions": "tmux_only",
    "pause_session": "tmux_only",
    "suspend_session": "tmux_only",
    "resume_session": "tmux_only",
    "spawn_session": "tmux_only",
    "scheduler_state": "coordinator_only",
    "inject_input": "tmux_only",
    "autonomous_status": "coordinator_only",
    "autonomous_enable": "coordinator_only",
    "autonomous_disable": "coordinator_only",
    "peer_send": "coordinator_only",
    "peer_inbox_read": "coordinator_only",
    "peer_inbox_wait": "coordinator_only",
    "task_progress_add": "coordinator_only",
    "bind_task": "coordinator_only",
    "bind_initiative": "coordinator_only",
    "unbind_task": "coordinator_only",
    "unbind_initiative": "coordinator_only",
    "archive_session": "coordinator_only",
    "unarchive_session": "coordinator_only",
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
