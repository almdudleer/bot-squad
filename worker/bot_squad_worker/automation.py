"""The automation gate (T-0929) — ONE switch, and one place that lists what it stops.

> "And there should be explicit UI state where I could easily turn them off for
> a project, just stop any automatic activity all at once. And explicitly see if
> it's happening in the first place. Now that's all very unclear there and
> uncontrollable, and there are many ways in which the system operates around
> this mechanism. But that should be the ultimate switch."
> — the stakeholder, 2026-08-28 (T-0929).

THE DEFECT THIS CLOSES
----------------------
The pause flag (:func:`operator_redrive.is_paused`) has always existed and has
always been described as "the user stopped the auto-drive". The audit for this
ticket counted, in the worker's scheduler, **thirteen ticks that make agents
work** and **three that read that flag** — ``operator_tick``, ``recovery_tick``
and ``drive_stop_tick``. The other ten kept spawning, re-pinging and injecting
after he paused. That is not "a confusing remnant": it is the literal report —
"I regularly find the sessions working when earlier on I've explicitly asked
them to stop the auto-drive" — and it is the second half of the same sentence,
"or is not covering the whole system".

So the gate is:

* **one predicate** — :func:`allowed`, which every driving tick calls FIRST;
* **one registry** — :data:`MECHANISMS`, which names every one of them;
* **one test** — ``worker/tests/test_automation_gate.py``, which reads the
  registry and asserts each named module actually calls the gate. A registry
  that is only documentation is not a control (it drifts the first time someone
  adds a tick), so the pin is a source scan, not a docstring.

WHAT THE GATE DOES **NOT** STOP, and why that is deliberate
-----------------------------------------------------------
It stops *automatic work on the project's tasks*. It does not stop bookkeeping
that only makes the system honest about itself — reaping dead sessions
(``binding_gc``), sampling telemetry, draining the outbound queue, GC-ing audio
blobs. Gating those would mean a paused project slowly fills with stale
bindings and unsent messages and then lies to him on the surface he paused it
from. "Stop any automatic activity" is about agents doing work, and every
mechanism below is classified explicitly rather than by whoever wrote it
remembering to think about it.

WHERE THE SWITCH IS THROWN FROM
-------------------------------
Four entry points, all converging on the ONE flag
``_worker/operator_redrive/operator_paused.flag``:
``pace.set_drive_state(..., "off")`` (the web card + ``bsq pace state off``),
the ``operator_pause`` action (``bsq operator pause``, ``POST
/operator/pause``), ``operator_redrive.pause()`` directly, and ``bsq pace
pause``. The last one writes the flag itself rather than calling the worker —
deliberately, because pausing must still land when the worker is sick — so it
carries its own copy of the also-stop-running-autopilots step. That copy is the
one place a future step added to :func:`operator_redrive.pause` would not
reach; keep them together.

Deploy and autoupdate keep their OWN pause flags (``deploy.is_paused``,
``autoupdate.is_paused``); they are release plumbing, not task drive, and
merging them into this switch would mean pausing the drive also froze a
hotfix.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


#: Every mechanism that can make an agent work without the user asking, and
#: whether this gate stops it. ``module`` is the file the tick's body lives in —
#: ``test_automation_gate.py`` scans exactly that file for a gate call, so a new
#: ``gated: True`` row with no call is a RED test, not a stale comment.
#:
#: ``why`` is the one-line reason, and it is the copy the visibility surfaces
#: print: "explicitly see if it's happening in the first place" needs the list to
#: be readable, not just enumerable.
MECHANISMS: tuple[dict, ...] = (
    {"key": "operator_redrive", "module": "operator_redrive.py", "gated": True,
     "why": "respawns the operator to clear the backlog"},
    {"key": "uc_redrive", "module": "uc_redrive.py", "gated": True,
     "why": "re-drives the user-conversation session on an unanswered message"},
    {"key": "autopilot", "module": "autopilot.py", "gated": True,
     "why": "re-pings a session under a standing autopilot brief (and refuses "
            "to start a new one)"},
    {"key": "recovery", "module": "recovery.py", "gated": True,
     "why": "respawns sessions that died mid-task"},
    {"key": "constant_teams", "module": "constant_teams.py", "gated": True,
     "why": "keeps a declared team staffed by spawning replacements"},
    {"key": "routines", "module": "routines.py", "gated": True,
     "why": "fires scheduled routines, which spawn sessions"},
    {"key": "routines_monitor", "module": "routines.py", "gated": True,
     "why": "probes monitors and attaches a session when one breaches"},
    {"key": "stall_sweep", "module": "stall_sweep.py", "gated": True,
     "why": "auto-answers a stuck TUI prompt to put a session back to work"},
    {"key": "drift_check", "module": "drift.py", "gated": True,
     "why": "injects a stay-on-task reminder into live panes"},
    {"key": "budding", "module": "budding.py", "gated": True,
     "why": "writes a widen-the-fleet advisory into a root session's pane"},
    {"key": "park_resume", "module": "park.py", "gated": True,
     "why": "resumes a session parked on the 5h usage limit"},
    {"key": "wait_resume", "module": "wait_resume.py", "gated": True,
     "why": "resumes a suspended session whose blocker cleared"},
    {"key": "drive_stop", "module": "drive_stop.py", "gated": True,
     "why": "evaluates the stopping condition and pages when the drive runs dry"},

    # NOT gated — see the module docstring. Each of these either ENDS work,
    # keeps the system's own picture of itself honest, or belongs to release
    # plumbing with its own switch. Gating them would make a paused project rot
    # and then misreport itself on the very surface he paused it from.
    {"key": "backoff", "module": "backoff.py", "gated": False,
     "why": "adjusts the concurrency ceiling under rate-limit pressure — "
            "spawns nothing itself"},
    {"key": "park", "module": "park.py", "gated": False,
     "why": "SUSPENDS a session stuck on the 5h limit — a stop, not a start"},
    {"key": "binding_gc", "module": "jobs.py", "gated": False,
     "why": "reaps dead sessions and stale task bindings (bookkeeping)"},
    {"key": "telemetry", "module": "telemetry.py", "gated": False,
     "why": "samples resource usage (bookkeeping)"},
    {"key": "outbound_drain", "module": "outbound_log.py", "gated": False,
     "why": "delivers already-queued outbound messages (bookkeeping)"},
    {"key": "idle_timeout", "module": "idle_timeout.py", "gated": False,
     "why": "recycles stale sessions — it ENDS work, never starts it"},
    {"key": "graceful_exit", "module": "graceful_exit.py", "gated": False,
     "why": "exits sessions whose work is done — it ENDS work, never starts it"},
    {"key": "task_lifecycle", "module": "task_gc.py", "gated": False,
     "why": "auto-pauses untouched tickets (board bookkeeping, spawns nothing)"},
    {"key": "ticket_watch", "module": "ticket_watch.py", "gated": False,
     "why": "tells bound sessions their ticket changed (notification, not work)"},
    {"key": "deploy", "module": "deploy.py", "gated": False,
     "why": "release plumbing — has its own pause (`bsq deploy pause`)"},
    {"key": "autoupdate", "module": "autoupdate.py", "gated": False,
     "why": "release plumbing — has its own pause"},
)

#: The gated subset, by key — what "the ultimate switch" actually turns off.
GATED_KEYS = tuple(m["key"] for m in MECHANISMS if m["gated"])


def allowed(cfg: Any, slug: str) -> bool:
    """True iff automatic work is permitted for ``slug`` right now.

    THE gate. One read of the pause flag — the same SSOT
    :func:`operator_redrive.is_paused` owns and ``bsq pace pause`` /
    ``pace.set_drive_state(..., "off")`` write — so there is no second bit that
    can disagree with the switch he pressed.

    **Fails OPEN.** An unreadable state must not silently freeze a project's
    entire drive: a stuck-off system is as much a lie about what is happening as
    a stuck-on one, and this ticket is about the surface telling the truth. The
    ``off`` state is a file that EXISTS, so the failure mode of a bad read is
    "we could not confirm a pause", which is not a pause.
    """
    try:
        from bot_squad_worker import operator_redrive as _ord
        return not _ord.is_paused(cfg, slug)
    except Exception:  # noqa: BLE001
        log.exception("automation: pause read failed for %s — failing open", slug)
        return True


def gate(cfg: Any, slug: str, mechanism: str) -> bool:
    """:func:`allowed`, plus a log line naming WHICH mechanism stood down.

    The call every gated tick makes. The log line is not decoration: "I
    regularly find the sessions working when earlier on I've explicitly asked
    them to stop" was un-diagnosable for months precisely because a mechanism
    that ignored the pause left no trace distinguishable from one that honored
    it. A skip is now an event with a name in it.
    """
    if allowed(cfg, slug):
        return True
    log.info("automation: %s — %s skipped (drive state is off)", slug, mechanism)
    return False


def snapshot(cfg: Any, slug: str) -> dict:
    """The "is anything automatic running right now" view, for the UI / CLI / TG.

    Shape::

        {
          "state": str,             # EFFECTIVE drive state ("off" when paused)
          "label": str,             # human copy for it
          "running": bool,          # is ANY gated mechanism permitted to act
          "paused": bool,
          "max_in_progress": int,
          "drive": {...},           # the normalised drive block
          "quota": {...},           # the caps + targets, see below
          "autopilots": [{key, kind, ref, target_sid, expires_at}, ...],
          "mechanisms": [{key, why, gated, active: bool}, ...],
        }

    ``quota`` answers "with quota caps and targets" from the same request: the
    board CAP the state implies (``max_in_progress``, 0 = unlimited) and the
    weekly-spend TARGET with the measured spend and the under/on/over verdict.
    They live in two different stores (``pace.json`` and ``system_settings.toml``
    ``[operator].weekly_quota_target_pct``), which is precisely why they belong
    on one view — needing to know that to answer "what are my caps" is the
    "unclear" half of the report. Every field is ``None`` when its signal is
    genuinely absent (no target set, or no quota anchor to measure spend
    against): an explicit unknown, never a zero standing in for one.

    ``autopilots`` is listed separately from the mechanism rows because it is the
    only one with per-target INSTANCES: "is autopilot on" is a different question
    from "which sessions are under an autopilot brief right now", and the second
    is the one that answers "why is this session working". Best-effort — an
    unreadable autopilot dir yields ``[]`` and never sinks the view.
    """
    from bot_squad_worker import pace as _pace

    auto = _pace.read_automation(cfg, slug)
    running = not auto["paused"]

    autopilots: list[dict] = []
    try:
        from bot_squad_worker import autopilot as _autopilot
        for st in _autopilot.list_states(cfg, slug):
            if st.enabled and st.status == "running":
                autopilots.append({
                    "key": st.key, "kind": st.kind, "ref": st.ref,
                    "target_sid": st.target_sid, "expires_at": st.expires_at,
                })
    except Exception:  # noqa: BLE001
        log.exception("automation: autopilot listing failed for %s", slug)

    return {
        "state": auto["state"],
        "label": auto["label"],
        "running": running,
        "paused": auto["paused"],
        "max_in_progress": auto["max_in_progress"],
        "drive": auto["drive"],
        "quota": _quota(cfg, slug, auto["max_in_progress"]),
        "autopilots": autopilots,
        "mechanisms": [
            {"key": m["key"], "why": m["why"], "gated": m["gated"],
             # A gated mechanism is ACTIVE only while the switch is released; an
             # ungated one is always active and says so, so the panel can never
             # imply the switch stopped something it does not touch.
             "active": running if m["gated"] else True}
            for m in MECHANISMS
        ],
    }


def _quota(cfg: Any, slug: str, max_in_progress: int) -> dict:
    """The caps + targets that go with the drive state — see :func:`snapshot`.

    Best-effort and never raises: this rides the same read as "is anything
    running", and an unreadable telemetry file must not take the switch's own
    status down with it. An unavailable signal is reported as ``None`` (an
    explicit unknown), never as a zero that reads like a real measurement.
    """
    out = {"max_in_progress": max_in_progress, "weekly_target_pct": None,
           "spend_pct": None, "verdict": None}
    try:
        from bot_squad_worker import operator_redrive as _ord
        out["weekly_target_pct"] = _ord.weekly_quota_target_pct(cfg)
        out["spend_pct"] = _ord._burn_signal(cfg, slug).get("spend_pct")
        out["verdict"] = _ord.pace_verdict(out["weekly_target_pct"], out["spend_pct"])
    except Exception:  # noqa: BLE001
        log.exception("automation: quota read failed for %s", slug)
    return out
