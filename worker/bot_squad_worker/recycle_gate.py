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

T-0655 operator drive=on: a SECOND, narrower exception in the same shape as
T-0617 — an operator-role session with ``drive`` on (the default; see
:func:`operator_drive_on`) never rides the terminate-and-remember flow either,
but for a different reason than the human's own sessions: the STAKEHOLDER
verbatim ask is that a `drive=on` operator must not be silently recycled
purely because its cache is about to expire while idle — the system should
keep it alive with a keep-alive nudge instead, and only the OPERATOR ITSELF
(by setting ``drive=off``) may permit a recycle. ``idle_timeout`` checks this
directly (like :func:`user_session_exempt`, not folded into
:func:`recycle_allowed`) and, for a drive=on operator, sends a "continue"
nudge in place of the compact/terminate machinery. See ``idle_timeout``'s
module docstring for the full state machine.

T-0864 pane-scoping the attach check: :func:`is_attached` used to ask
``tmux list-clients -t <pane_id>``, and **tmux resolves a pane id to its
enclosing SESSION, not the pane**. Every session of one project shares ONE
tmux session, so a single human attached anywhere in a project reported
"attached" for EVERY pane in it and froze autocompact/idle_timeout/recovery
fleet-wide, whatever the context ceiling was set to. A per-pane check was
intended; a per-session check shipped. The fix compares the target's tmux
WINDOW against the window each attached client is actually viewing (see
:func:`is_attached`). Same ticket: this gate and ``autocompact.composer_ready``
now emit a debounced INFO line when they defer a tick — neither logged anything
on skip, which is why the 158-minute ``S-almdudleer-operator-p50`` overshoot
could not be attributed past "one of these two gates".
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RECYCLE_PROJECTS = ("bot-squad", "watchrobot")

# Skip-log debounce: at most one INFO line per KEY within this window. All
# three ticks run on a ~60s cadence and call the gate once PER SESSION, so
# without this a busy allowlisted-out project would log once per session per
# tick. A window comfortably under 60s guarantees at most one line per tick per
# key regardless of how many sessions/rows are checked within it.
#
# Keys are namespaced by gate (``project:``/``attached:``/``composer:``) so the
# three skip reasons debounce independently: an attach skip on one session must
# never suppress the allowlist line for its project, or the log stops answering
# "which gate held this session up" — the T-0864 question.
_SKIP_LOG_WINDOW_SEC = 30.0
# Keyed by sid for the per-session gates, so the dict grows with sessions seen
# over a worker's lifetime rather than staying slug-bounded. Prune on write once
# it passes this size; entries outside the debounce window carry no information.
_SKIP_LOG_MAX_KEYS = 512
_last_skip_log: dict[str, float] = {}

# T-0926 (stakeholder, 2026-08-28): is_attached() is a single stateless tmux
# poll — "which window is each client's #{window_id} RIGHT NOW". Reproduced
# live twice within the hour it was filed: idle_timeout's compact-and-stay
# sent a bare /compact into the stakeholder's own user-conversation pane
# (13:24:41), and autocompact armed a full handoff on the watchrobot operator
# pane (13:57:46) — both while he was actively working the pane, and both
# with "a human client is viewing" skip lines logged on the ticks immediately
# before and after but NOT on the firing tick itself. One negative sample from
# tmux (a momentary resize/redraw/client-switch blip, or however his actual
# viewing method reports to tmux) was enough to fire. Grace: remember the last
# tick each target read attached, and keep treating it as attached for
# _ATTACH_GRACE_SEC after that even if the current sample reads negative — a
# real detach stays negative past the grace window, a blip doesn't survive
# long enough to matter. In-memory only (per worker process, not persisted):
# a worker restart re-learns attachment on the very next tick, which is fine
# since the grace exists to bridge a single missed sample, not an outage.
_ATTACH_GRACE_SEC = 180.0
_last_attached_true_at: dict[str, float] = {}


def should_log_skip(key: str, now: float | None = None) -> bool:
    """True at most once per ``key`` per :data:`_SKIP_LOG_WINDOW_SEC`, stamping
    the key as logged. The shared debounce behind every gate's skip line
    (``project_allowed``, :func:`is_attached`, ``autocompact.composer_ready``).

    ``now`` is the tick's clock when the caller has one (every recycle tick
    does); ``None`` falls back to wall-clock for the few callers that don't.
    """
    ts = time.time() if now is None else float(now)
    last = _last_skip_log.get(key)
    if last is not None and (ts - last) < _SKIP_LOG_WINDOW_SEC:
        return False
    if len(_last_skip_log) >= _SKIP_LOG_MAX_KEYS:
        cutoff = ts - _SKIP_LOG_WINDOW_SEC
        for k, v in list(_last_skip_log.items()):
            if v < cutoff:
                del _last_skip_log[k]
    _last_skip_log[key] = ts
    return True


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
    if should_log_skip(f"project:{slug}", now):
        log.info("recycle: skipping non-allowlisted project %s", slug)
    return False


def role_exempt(role: str | None) -> bool:
    """True for the human's own live chat — never auto-recycled (T-0564)."""
    return (role or "") == "user-conversation"


# T-0616: the hand-launch convention — a `user-session` window segment
# (`user-session`, `user-session-2`, `gu_x-user-session`). Segment-anchored so
# `user-sessions` / `user-feedback` (a constant-team window) do NOT match.
#
# T-0964 adds `universal_bsq_session`, the root session's own name. It already
# derives role `user-conversation`, so the role arm below exempts it — but this
# window arm exists precisely as an INDEPENDENT belt for a caller that has a
# window and no derived role, and leaving the root session out of the belt
# would be the T-0564 hole reopened under a new name.
_USER_SESSION_WINDOW_RE = re.compile(
    r"(?:^|[-_])user[-_]session(?:$|[-_])"
    r"|^universal[-_]bsq[-_]session(?:[-_]|$)",
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


def operator_drive_on(role: str | None = None, meta: dict | None = None) -> bool:
    """T-0655: True for an operator-role session whose ``drive`` is on.

    ``drive`` is a session-md field DISTINCT from ``bsq pace pause`` (a
    project-wide dispatch gate — stop admitting new work) and from
    :func:`user_session_exempt` (the human's own live sessions, exempted
    forever). ``drive`` governs whether THIS operator session itself stays
    alive across its own ~1h cache-expiry-while-idle window, or is allowed to
    recycle. Only ever meaningful for ``role == "operator"`` — every other
    role always returns False here, unaffected by this predicate.

    Absent/unset defaults to ON, matching the documented "operator runs
    non-stop while work is on" behaviour (clarification-01) — an operator
    never has to opt in to staying alive; it has to explicitly opt OUT
    (``bsq drive off``) once it judges further unsupervised work unsafe or
    unavailable. Any value other than the literal ``off`` (case-insensitive)
    reads as on, so a truthy/garbage stamp fails safe toward "keep it alive"
    rather than toward "recycle it".
    """
    if (role or "") != "operator":
        return False
    val = str((meta or {}).get("drive") or "on").strip().lower()
    return val != "off"


def _tmux(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run a read-only tmux query. ``None`` means the invocation itself failed
    (timeout / no binary) — the caller must fail CLOSED on that, never confuse
    it with a clean "no clients" answer."""
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def is_attached(tmux_target: str | None, *, sid: str | None = None,
                now: float | None = None) -> bool:
    """True iff a human tmux client is currently LOOKING AT ``tmux_target``
    (a pane id, or a session/window name).

    T-0864 — what this used to get wrong. The old implementation ran
    ``tmux list-clients -t <pane_id>``, and **tmux resolves any target down to
    its enclosing SESSION**: a client attached to window ``w1`` is returned for
    a pane in ``w2`` of the same tmux session. Measured 2026-08-11 on a
    throwaway server (``tmux -L``): one client viewing ``w1``,
    ``list-clients -t <pane in w2>`` printed it. Since every session of one
    project shares ONE tmux session (``tmux_session: "watchrobot"``), that made
    one attached human freeze autocompact, idle_timeout AND recovery for the
    whole project fleet.

    So the check is done the other way round: resolve the TARGET's window id,
    then ask each attached client which window IT is displaying
    (``list-clients -F '#{window_id}'`` resolves per client, verified on the
    same throwaway server by switching the client between windows). Attached =
    some client's current window is the target's window.

    WINDOW-level, deliberately, not pane-level: a split window shows all of its
    panes at once, so a human in that window really is watching every pane in
    it. Live layout is one pane per window per session anyway, where the two
    coincide.

    - No target (nothing to check, e.g. a dead-pane session) → False. The
      caller's own pane-liveness gate already handles the "nothing there"
      case; this function only answers "is a HUMAN watching".
    - tmux invocation error (timeout/OSError) → True. Fail CLOSED: treat as
      attached (skip) rather than risk killing a session a human is looking
      at because of a transient tmux hiccup.
    - Target doesn't exist — non-zero exit, or the clean-but-EMPTY output tmux
      3.x gives for an unknown pane id → False. A pane that isn't there cannot
      have anyone watching it, and failing closed here would wedge exactly the
      recovery path that exists to clean dead panes up.
    - No tmux server / no clients → False.

    Logs one debounced INFO line naming the session and the window a client is
    holding, so a stuck session is a log lookup (T-0864 DoD 3).

    T-0926 grace: a "not viewing right now" reading (the ``target_window not
    in viewing`` case below) is held against the last tick this same target
    read attached for :data:`_ATTACH_GRACE_SEC` — see that constant's comment.
    Every OTHER False path (no target, target gone, no server) returns
    immediately with no grace: those mean there is nothing to be attached TO,
    not "attached a moment ago."
    """
    if not tmux_target:
        return False
    ts = time.time() if now is None else float(now)
    key = f"attached:{sid or tmux_target}"
    tgt = _tmux(["display-message", "-p", "-t", str(tmux_target),
                 "-F", "#{window_id}"])
    if tgt is None:
        log.warning("recycle: tmux display-message failed for %s — treating as "
                    "attached (fail-closed)", tmux_target)
        _last_attached_true_at[key] = ts
        return True
    if tgt.returncode != 0:
        return False
    target_window = tgt.stdout.strip()
    if not target_window:
        return False  # unknown target: tmux 3.x exits 0 and prints nothing
    clients = _tmux(["list-clients", "-F", "#{window_id}"])
    if clients is None:
        log.warning("recycle: tmux list-clients failed for %s — treating as "
                    "attached (fail-closed)", tmux_target)
        _last_attached_true_at[key] = ts
        return True
    if clients.returncode != 0:
        return False  # no server → no client possible
    viewing = {ln.strip() for ln in clients.stdout.splitlines() if ln.strip()}
    if target_window not in viewing:
        last_true = _last_attached_true_at.get(key)
        if last_true is not None and (ts - last_true) < _ATTACH_GRACE_SEC:
            return True  # T-0926: one flickered sample, not a real detach
        return False
    _last_attached_true_at[key] = ts
    if should_log_skip(key, now):
        log.info("recycle: skipping %s — a human client is viewing its tmux "
                 "window %s (target %s)", sid or "<unknown sid>",
                 target_window, tmux_target)
    return True


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
    if is_attached(tmux_target, now=now):
        return False
    return True


# --- T-0949: which recycler is already driving this session ----------------
#
# THREE state machines can drive one pane, and none of them could see the
# others: the ceiling recycler kept its in-flight state in the TELEMETRY record
# (``rec['compact']``), idle_timeout in the session md
# (``idle_recycle_phase`` / ``compact_stay_phase``), graceful_exit in
# ``exit_handoff_phase``. Each injects a PROMPT into the pane when it arms, so
# two machines arming in the same 60s window put two contradictory asks in
# front of one session ("this SAME session continues" vs "this incarnation is
# ending"), and each injection resets the jsonl idle clock the OTHER machine
# reads.
#
# The rule this encodes: a machine may always drive ITS OWN in-flight sequence
# to completion (``own``), and may never START a new one while another
# machine's is in flight. Every in-flight state is bounded by its own timeout,
# so deferring can never wedge — the blocker always clears itself.

MACHINE_IDLE_RECYCLE = "idle_timeout"      # idle_recycle_phase (handoff+exit)
MACHINE_COMPACT_STAY = "compact_stay"      # compact_stay_phase (compact-in-place)
MACHINE_EXIT_HANDOFF = "graceful_exit"     # exit_handoff_phase (pre-exit write)
MACHINE_CEILING = "ceiling"                # autocompact rec['compact'] (telemetry)

# md field -> machine name. The ceiling's marker is NOT here: it lives in the
# telemetry record, so callers pass it in as ``ceiling_phase``.
_MACHINE_MD_FIELDS = (
    ("idle_recycle_phase", MACHINE_IDLE_RECYCLE),
    ("compact_stay_phase", MACHINE_COMPACT_STAY),
    ("exit_handoff_phase", MACHINE_EXIT_HANDOFF),
)


def inflight_machines(meta: dict | None = None, *,
                      ceiling_phase: str = "") -> tuple[str, ...]:
    """Every recycle state machine currently mid-sequence on this session.

    Pure. ``meta`` is the session md's frontmatter; ``ceiling_phase`` is
    ``autocompact.ceiling_phase(...)`` (the telemetry record's
    ``compact.phase``), which the md cannot see.
    """
    m = meta or {}
    out = []
    for field, machine in _MACHINE_MD_FIELDS:
        v = m.get(field)
        if v and v != "~":
            out.append(machine)
    if ceiling_phase and ceiling_phase != "~":
        out.append(MACHINE_CEILING)
    return tuple(out)


def other_recycler(meta: dict | None = None, *, own: tuple[str, ...] = (),
                   ceiling_phase: str = "") -> str | None:
    """Name the OTHER machine mid-sequence on this session, or None.

    ``own`` lists the machines the CALLER drives — its own in-flight state
    never blocks it, or a machine could never finalize what it armed. Call
    this only in front of a decision to START a new sequence (an injection, a
    ``/compact``, a terminate); never in front of a finalize.
    """
    for machine in inflight_machines(meta, ceiling_phase=ceiling_phase):
        if machine not in own:
            return machine
    return None
