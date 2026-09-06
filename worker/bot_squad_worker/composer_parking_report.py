"""T-0957 DoD 1 — detect + report composer text parked past its own bound,
independent of whether any gate happens to sample that pane.

:func:`composer_watch.observe` already classifies a pane's composer (empty /
typing / stale / dialog / generating) and ages STALE content against
:func:`composer_watch.stale_after_sec` — but per its own docstring it is
called "from the gates that are about to act, not from a background poller":
a compact-due check or a delivery attempt samples the pane it is about to
touch, and nothing else does. A session nobody is trying to act on is never
sampled, so parked text on an otherwise-quiet session's composer can sit
unreported indefinitely — the exact symptom T-0957 measured (four sessions
holding unsubmitted work instructions for days, discovered only by hand).

This module is the missing periodic sampler: it walks every live pane for a
project and reports (a WARNING log line; callers may surface further) any
composer whose content has crossed the staleness bound, whether or not a
compact or delivery attempt happens to run against it this tick.

Read-only and side-effect-free beyond logging: this reuses
:func:`composer_watch.observe`'s existing per-(project, session) state file
and never writes into a composer or clears anything. Recovering a parked
delivery (DoD 2) is a transport-seam concern (``input_mux`` /
``sessions._deliver_prompt``), not this reporter's.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


def _default_capture(pane_id: str) -> str:
    from bot_squad_worker.autocompact import _capture_pane
    return _capture_pane(pane_id)


def _default_pane_map() -> dict[str, str]:
    from bot_squad_worker import sessions
    return sessions.live_pane_map()


def _default_list_sessions(cfg: Any, slug: str) -> list[dict]:
    from bot_squad_worker import sessions
    return sessions.list_sessions(cfg, slug)


def report_parked_composers(
    cfg: Any, slug: str, *,
    now: float | None = None,
    capture: Callable[[str], str] | None = None,
    pane_map: Callable[[], dict[str, str]] | None = None,
    list_sessions: Callable[[Any, str], list[dict]] | None = None,
) -> list[dict]:
    """Sample every live pane for ``slug`` and report the ones parked STALE.

    Returns one dict per reported session — ``{sid, pane, state,
    unchanged_for, since, text}`` (the ``composer_watch.observe`` record, plus
    ``sid``/``pane``) — for a caller that wants to do more than log (e.g. a
    scheduler tick that also writes a report file). Never raises: a single
    pane's capture/observe failure is logged and skipped so it can't blank
    the whole sweep.
    """
    from bot_squad_worker import composer_watch

    now = time.time() if now is None else now
    capture = capture or _default_capture
    pane_map = pane_map or _default_pane_map
    list_sessions = list_sessions or _default_list_sessions

    try:
        rows = list_sessions(cfg, slug)
    except Exception:
        log.exception("composer_parking_report: list_sessions failed for %s", slug)
        return []

    try:
        panes = pane_map()
    except Exception:
        log.exception("composer_parking_report: pane_map failed for %s", slug)
        return []

    reported: list[dict] = []
    for row in rows:
        if row.get("status") != "active":
            continue
        sid = row.get("sid")
        pane = panes.get(sid) if sid else None
        if not sid or not pane:
            continue
        try:
            buf = capture(pane)
            obs = composer_watch.observe(cfg, slug, sid, buf, now)
        except Exception:
            log.exception("composer_parking_report: observe failed for %s", sid)
            continue
        if obs["state"] != composer_watch.STATE_STALE:
            continue
        rec = {"sid": sid, "pane": pane, **obs}
        reported.append(rec)
        log.warning("composer_parking_report: %s — %s", sid,
                    composer_watch.describe(obs))
    return reported
