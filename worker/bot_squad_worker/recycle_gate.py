"""T-0563/T-0564 (recycle-v2) — shared gates reused by ALL THREE recycle paths:
``autocompact.maybe_compact``, ``idle_timeout.maybe_recycle``, and
``recovery.recovery_tick`` / ``recovery.boot_reconcile``.

Background (2026-06-29 incident, see T-0566): none of the three respawn/recycle
paths were project-scoped and none exempted user sessions — they churned live
watchrobot sessions incl. an actively-used user session. All three are
currently OFF via env kill-switches; this module is the SINGLE gate all three
consult before touching a session, so re-enabling any of them can't repeat the
incident.

T-0563 per-project allowlist: ``[recycle].projects`` in system_settings.toml
(or the ``BOT_SQUAD_RECYCLE_PROJECTS`` env override — comma-separated slugs,
env wins) is the allowlist. Default (both unset) = ``("bot-squad",
"watchrobot")`` — a new project is NEVER auto-recycled until an operator opts
it in explicitly. watchrobot joined the default per T-0613 (the T-0612 gate:
its sessions must ride recycle-v2 BEFORE its operator program spawns).

T-0564 user/attached exemption: independent of the allowlist, no recycle path
may ever touch (a) a ``user-conversation`` role session (the human's own live
chat), or (b) a session a human is currently ATTACHED to (a live tmux client on
its pane/session) — even in an allowlisted project. Both checks fail CLOSED (an
attach-check error is treated as attached, i.e. skip) — it is always safer to
leave a session alone for one more tick than to kill one a human might be
looking at.

T-0616 hand-launched user sessions: the stakeholder's own sessions launched
by hand (window ``user-session``, ad-hoc names) derive role ``dev``, so the
T-0564 role check alone missed them (D-0053 §4). :func:`user_session_exempt`
extends the exemption to a ``user-session`` window segment and an explicit
``recycle_exempt: true`` session-md marker.

T-0617 compact-and-stay: the one deliberate exception to "no recycle path may
ever touch" an exempt session above. ``idle_timeout`` alone (not
``autocompact``, not ``recovery``) still compacts an exempt session's context
in place before its cache window lapses, using :func:`user_session_exempt`
directly rather than :func:`recycle_allowed` — it just never terminates the
session the way the non-exempt compact-terminate-remember flow (T-0566) does.
See ``idle_timeout``'s module docstring for the full state machine.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RECYCLE_PROJECTS = ("bot-squad", "watchrobot")

# Skip-log debounce: at most one INFO line per slug within this window. All
# three ticks run on a ~60s cadence and call the gate once PER SESSION, so
# without this a busy allowlisted-out project would log once per session per
# tick. A window comfortably under 60s guarantees at most one line per tick per
# project regardless of how many sessions/rows are checked within it.
_SKIP_LOG_WINDOW_SEC = 30.0
_last_skip_log: dict[str, float] = {}


def recycle_allowlist(cfg: Any) -> tuple[str, ...]:
    """Project slugs any recycle path may touch.

    ``BOT_SQUAD_RECYCLE_PROJECTS`` (comma-separated) wins when set to a
    non-empty value; else ``cfg.recycle_projects`` (system_settings.toml
    ``[recycle].projects``); else :data:`DEFAULT_RECYCLE_PROJECTS`.
    """
    raw = os.environ.get("BOT_SQUAD_RECYCLE_PROJECTS")
    if raw and raw.strip():
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    cfg_val = getattr(cfg, "recycle_projects", None)
    if cfg_val:
        return tuple(cfg_val)
    return DEFAULT_RECYCLE_PROJECTS


def project_allowed(cfg: Any, slug: str, now: float) -> bool:
    """True iff ``slug`` is in the recycle allowlist.

    Logs one INFO line per slug per debounce window (never per-session) when
    skipping a non-allowlisted project.
    """
    if slug in recycle_allowlist(cfg):
        return True
    last = _last_skip_log.get(slug)
    if last is None or (now - last) >= _SKIP_LOG_WINDOW_SEC:
        _last_skip_log[slug] = now
        log.info("recycle: skipping non-allowlisted project %s", slug)
    return False


def role_exempt(role: str | None) -> bool:
    """True for the human's own live chat — never auto-recycled (T-0564)."""
    return (role or "") == "user-conversation"


# T-0616: the hand-launch convention — a `user-session` window segment
# (`user-session`, `user-session-2`, `gu_x-user-session`). Segment-anchored so
# `user-sessions` / `user-feedback` (a constant-team window) do NOT match.
_USER_SESSION_WINDOW_RE = re.compile(r"(?:^|[-_])user[-_]session(?:$|[-_])",
                                     re.IGNORECASE)


def user_session_exempt(role: str | None = None, window: str | None = None,
                        meta: dict | None = None) -> bool:
    """T-0616 (closes the T-0564 hole): True for ANY of the human's own
    sessions — no recycle path may terminate them, and neither
    ``autocompact`` nor ``recovery`` (both gate on :func:`recycle_allowed`,
    which folds this check in) may touch them at all. T-0617 layers one
    narrow, deliberate exception on top: ``idle_timeout``'s compact-and-stay
    path checks this signal directly (it does not call ``recycle_allowed``)
    and, for a session this exempts, still compacts its context in place
    before the cache window lapses — it just never terminates it.

    The stakeholder's hand-launched sessions (window ``user-session``, ad-hoc
    names) derive role ``dev`` (T-0175 default), so the T-0564 role check
    alone missed them: the 2026-07-04/05 evidence run (D-0053 §4) shows
    user-session-p8 riding the full idle_timeout recycle path — only the
    attach-check kept it from termination. Per the stakeholder's T-0612 §0
    verbatim, user sessions stay in the user's tmux ("пускай она остается
    там"); recycle paths never touch them. Three signals, any one exempts:

    * role ``user-conversation`` — the T-0564 check, unchanged;
    * a ``user-session`` window segment — the hand-launch convention;
    * an explicit ``recycle_exempt: true`` frontmatter field on the session
      md — the opt-out for hand-launched sessions under ad-hoc window names
      (stamp it on the md; the SessionStart hook preserves it, T-0616).
    """
    if role_exempt(role):
        return True
    if window and _USER_SESSION_WINDOW_RE.search(str(window).strip()):
        return True
    marker = (meta or {}).get("recycle_exempt")
    return str(marker or "").strip().lower() in ("true", "1", "yes")


def is_attached(tmux_target: str | None) -> bool:
    """True iff a human tmux client is attached to ``tmux_target`` (a pane id
    or session name — tmux resolves either to its enclosing session).

    - No target (nothing to check, e.g. a dead-pane session) → False. The
      caller's own pane-liveness gate already handles the "nothing there"
      case; this function only answers "is a HUMAN watching".
    - tmux invocation error (timeout/OSError) → True. Fail CLOSED: treat as
      attached (skip) rather than risk killing a session a human is looking
      at because of a transient tmux hiccup.
    - Clean non-zero exit (target doesn't exist) → False (no session, no
      client possible).
    - Clean zero exit → True iff ``tmux list-clients`` printed at least one
      client line.
    """
    if not tmux_target:
        return False
    try:
        out = subprocess.run(
            ["tmux", "list-clients", "-t", tmux_target],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("recycle: tmux list-clients failed for %s — treating as "
                    "attached (fail-closed)", tmux_target)
        return True
    if out.returncode != 0:
        return False
    return bool(out.stdout.strip())


def recycle_allowed(cfg: Any, *, slug: str, role: str | None,
                     tmux_target: str | None, now: float,
                     window: str | None = None,
                     meta: dict | None = None) -> bool:
    """The single combined gate (T-0563 + T-0564 + T-0616): True iff a recycle
    path may act on this session — its project is allowlisted, it isn't one of
    the human's own sessions (user-conversation role, ``user-session`` window,
    or ``recycle_exempt`` md marker), and no human client is attached to its
    pane. ``window``/``meta`` are optional so legacy callers stay valid, but
    every caller that has the session md SHOULD pass them — without them only
    the role signal protects a hand-launched user session."""
    if not project_allowed(cfg, slug, now):
        return False
    if user_session_exempt(role=role, window=window, meta=meta):
        return False
    if is_attached(tmux_target):
        return False
    return True
