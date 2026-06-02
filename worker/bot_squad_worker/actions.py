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


_TG_NOTIFY_ALLOWED = {"slug", "chat_id", "message", "sid", "user", "urgent", "topic_id"}


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

    # --- resolve chat_id (+ project-bound forum topic, T-0156) ---
    # An explicit topic_id param wins; otherwise, when the chat is resolved
    # from a project, inherit that project's tg_topic_id so group bindings
    # land in the right forum thread without the caller spelling it out.
    chat_id: str | None = params.get("chat_id") or None
    topic_id: int | None = _coerce_topic_id(params.get("topic_id"))
    if not chat_id:
        slug: str = params.get("slug") or ""
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
    tg = _get_tg_client(cfg)
    sent = tg.send(
        chat_id=chat_id,
        text=params["message"],
        sid=params.get("sid", ""),
        user=params.get("user", ""),
        urgent=bool(params.get("urgent", False)),
        topic_id=topic_id,
    )
    return {"ok": True, "sent": sent}


def _coerce_topic_id(raw: Any) -> int | None:
    """Normalise a topic_id param to int|None. Empty/None → None."""
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ActionError(f"tg_notify: topic_id must be an integer, got {raw!r}")


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
_RESUME_SESSION_ALLOWED = _RESUME_SESSION_REQUIRED | {"initial_prompt"}


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
    )


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
    result = _is.send(
        cfg, params["slug"], params["from_sid"], params["to"], params["text"],
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


_TASK_NEW_REQUIRED = {"slug", "title"}
_TASK_NEW_ALLOWED = _TASK_NEW_REQUIRED | {"initiative", "priority", "owner"}
_TASK_NEW_TITLE_MAX = 240


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


def _action_task_new(params: dict[str, Any]) -> dict[str, Any]:
    """Atomically allocate the next T-NNNN id and write a stub task md.

    Required params: slug, title
    Optional params: initiative, priority, owner
    Returns: {ok: true, id: "T-NNNN", file_path: "<abs path>"}

    Allocation goes through the shared ``idalloc`` allocator (T-0174), which
    serialises on ``data/<slug>/_counters/task.txt`` — the SAME counter the
    API's ``POST /backlog`` uses, so a web create and an agent ``task new``
    can no longer hand out the same id (they used to lock different files).
    Crashes between alloc and write merely burn one id — fine, ids aren't
    scarce.
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

    cfg = _get_config()
    if cfg.projects.get(slug) is None:
        raise ActionError(f"task_new: unknown project slug {slug!r}")

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
    ]
    for opt_key in ("initiative", "priority", "owner"):
        if opt_key in params:
            val = params[opt_key]
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            fm_lines.append(f"{opt_key}: {_yaml_quote(str(val))}")

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
    """Allocate the next F-NNNN id and write a stub user-flow md (composes T-0173).

    Required params: slug, uc_id, title
    Returns: {ok, id, file_path, uc_id}

    Storage: ``data/<slug>/use_cases/<uc-id>/flows/F-NNNN-<slug>.md``. The
    parent use case must exist (either ``<uc-id>.md`` or a ``<uc-id>/`` dir).
    The flow counter is per-project (one F-NNNN sequence across all UCs).
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
_INITIATIVE_NEW_ALLOWED = _INITIATIVE_NEW_REQUIRED


def _action_initiative_new(params: dict[str, Any]) -> dict[str, Any]:
    """Allocate the next INI-NN id and write a stub initiative md.

    Required params: slug, name
    Returns: {ok, id, file_path}

    Storage: ``data/<slug>/vision/initiatives/INI-NN-<slug>.md``. Legacy
    slug-named initiatives are non-numeric and untouched; new ones get an
    INI-NN id while keeping a human ``name``.
    """
    from bot_squad_worker import idalloc

    cfg, slug = _entity_setup(
        params, _INITIATIVE_NEW_REQUIRED, _INITIATIVE_NEW_ALLOWED, "initiative_new"
    )
    name = _require_str(params, "name", "initiative_new")

    init_dir = cfg.data_dir / slug / "vision" / "initiatives"
    init_dir.mkdir(parents=True, exist_ok=True)
    new_id = idalloc.allocate_id(cfg.data_dir, slug, "initiative")
    file_path = init_dir / f"{new_id}-{_slugify_title(name)}.md"

    fm = "\n".join([
        f"id: {new_id}",
        f"name: {_yaml_quote(name)}",
        "status: open",
        f"created: {_now_iso()}",
    ])
    content = f"---\n{fm}\n---\n\n# {name}\n\n(filed via initiative_new)\n"
    _atomic_write_new(file_path, content)
    return {"ok": True, "id": new_id, "file_path": str(file_path)}


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
    "tg_stall_clear": _action_tg_stall_clear,
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
    # T-0153: autopilot — prompt-driven, time-boxed autonomous runs per target.
    "autopilot_start": _action_autopilot_start,
    "autopilot_stop": _action_autopilot_stop,
    "autopilot_status": _action_autopilot_status,
    "peer_send": _action_peer_send,
    "peer_inbox_read": _action_peer_inbox_read,
    "peer_inbox_wait": _action_peer_inbox_wait,
    "task_progress_add": _action_task_progress_add,
    # T-0042: atomic T-NNNN allocator (flock-protected).
    "task_new": _action_task_new,
    "doc_new": _action_doc_new,
    "uc_new": _action_uc_new,
    "flow_new": _action_flow_new,
    "initiative_new": _action_initiative_new,
    "bind_task": _action_bind_task,
    "bind_initiative": _action_bind_initiative,
    "unbind_task": _action_unbind_task,
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
#   deploy queue, peer inbox, autonomous orchestrator).
# tmux_only — purely tmux/filesystem ops local to a Linux user.
# both — universally safe (proof-of-life).
ACTION_MODES: dict[str, str] = {
    "noop": "both",
    "tg_verify_login": "coordinator_only",
    "tg_notify": "coordinator_only",
    "tg_stall_clear": "coordinator_only",
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
    # T-0153: autopilot reads/writes coordinator state (peer bus, spawn, tg,
    # scheduler-coupled watchdog), so coordinator-only like the rest.
    "autopilot_start": "coordinator_only",
    "autopilot_stop": "coordinator_only",
    "autopilot_status": "coordinator_only",
    "peer_send": "coordinator_only",
    "peer_inbox_read": "coordinator_only",
    "peer_inbox_wait": "coordinator_only",
    "task_progress_add": "coordinator_only",
    "task_new": "coordinator_only",
    "doc_new": "coordinator_only",
    "uc_new": "coordinator_only",
    "flow_new": "coordinator_only",
    "initiative_new": "coordinator_only",
    "bind_task": "coordinator_only",
    "bind_initiative": "coordinator_only",
    "unbind_task": "coordinator_only",
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
