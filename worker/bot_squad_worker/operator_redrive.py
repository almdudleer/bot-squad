"""Operator re-drive cadence (T-0474) — Process Paradigm M2 / F2.1.

What keeps the operator *continuously scheduled WITHOUT a persistent session*.
clarification-01: the operator "is kept being re-driven while work is on (and is
paused when the user pauses / stops when it stalls out of time) — NOT a
never-recycled process"; clarification-03: "when on it always has a task to clear
the backlog". The operator's state lives in its artifact (operator-state.md,
T-0473) and continuity is achieved by *re-driving* — this tick — not by keeping a
conversation alive.

A 60s scheduler tick (:func:`operator_tick`, wired in ``scheduler.py``) walks
every project and, for each:

* **re-drives** (respawns the operator with its standing task) when the backlog
  has pending work AND the user has not paused AND no operator is currently live;
* **continues** (no-op) when an operator is already live — exactly one operator
  drives a project at a time (T-0472), and a within-time-box live operator is
  left alone for the universal lifecycle (idle_timeout / autopilot) to end;
* **stops** re-driving when the user has paused (a per-project pause flag) or
  when the operator stalls out of its time/quota budget — the latter is honored
  for free by routing the respawn through the normal spawn-admission path, which
  defers under capacity / quota backpressure (T-0250 / backoff_tick). Deferring
  into a wall instead of respawning is precisely "NOT a never-recycled process".

Design notes
------------
* Continue-vs-respawn rides :func:`dispatch.live_operator_sids` — the SAME seam
  the T-0472 one-operator spawn guard uses, so the tick can never double-drive.
* The respawn brief is :func:`dispatch.operator_standing_task` — the SSOT for
  the "clear the backlog" directive (shared with the spawn-time brief) — and
  since T-0783a it carries the CONCRETE pickup queue (:mod:`pickup`) so a fresh
  operator is told WHICH tickets are takeable instead of re-deriving the board.
* Purely scheduler-internal: no new worker socket action (TL directive — keep
  actions.py single-owner). The pause flag is a flag *file* with in-module
  helpers; wiring a ``bsq operator pause/resume`` verb is a follow-up.

Kill switch: ``BOT_SQUAD_OPERATOR_REDRIVE=0`` disables the tick entirely.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Per-project minimum gap between operator respawns, even when work is pending.
# Belt-and-braces (with the live_operator_sids gate) against an overlapping tick
# or a fast-exiting operator stampeding the spawn path. Override via env in tests.
_SPAWN_COOLDOWN_SEC = int(os.environ.get("BOT_SQUAD_OPERATOR_REDRIVE_COOLDOWN", "90"))

# A board task is "pending" (keeps the operator on) unless it is terminal. closed
# is the only terminal status (task status schema: planned/open/in_progress/
# totest/reopened/closed); an archived task is off-board intent. Everything else —
# including totest awaiting close — is still backlog the operator must clear, so
# "empty backlog (nothing actionable left) is the only idle state" (clarification-03).
_TERMINAL_STATUSES = frozenset({"closed"})


def _enabled() -> bool:
    """False iff the kill switch (``BOT_SQUAD_OPERATOR_REDRIVE``) disables re-drive."""
    raw = os.environ.get("BOT_SQUAD_OPERATOR_REDRIVE")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# State + user-pause flag (per project, under _worker/operator_redrive/)
# ---------------------------------------------------------------------------

def _state_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "operator_redrive"


def _pause_flag_path(cfg: Any, slug: str) -> Path:
    """Path of the per-project user-pause flag. Presence = paused (mirrors the
    autoupdate pause-flag pattern, T-0089: a flag file, not a config edit, so the
    pause is a cheap atomic toggle)."""
    return _state_dir(cfg, slug) / "operator_paused.flag"


def is_paused(cfg: Any, slug: str) -> bool:
    """True iff the user has paused the operator re-drive for ``slug``."""
    return _pause_flag_path(cfg, slug).exists()


def pause(cfg: Any, slug: str, *, by: str = "user", reason: str = "") -> dict:
    """Set the user-pause flag — re-drive stops until :func:`resume`. Returns the
    metadata written. Idempotent (re-pausing just rewrites the marker).

    T-0929 (stakeholder, 2026-08-28): "I regularly find the sessions working
    when earlier on I've explicitly asked them to stop the auto-drive." Root
    cause — this flag only ever gated FUTURE re-drive respawns (see this
    module's own docstring: "a within-time-box live operator is left alone
    for the universal lifecycle (idle_timeout / autopilot) to end"); it never
    touched a currently-running :mod:`autopilot` (T-0153), which is a wholly
    separate mechanism with its own stop condition and zero awareness of this
    flag. A user telling the system to stop while an autopilot brief is
    active would see this flag flip and the driven session keep working
    regardless. Pause is the stakeholder's one asked-for "stop everything
    automatic" lever, so it now also ends every currently-running autopilot
    for this project (best-effort: a broken autopilot-state read must not
    block the pause itself from taking effect)."""
    d = _state_dir(cfg, slug)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"paused_by": by, "reason": reason, "paused_at": time.time()}
    p = _pause_flag_path(cfg, slug)
    tmp = p.with_suffix(".flag.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, p)
    log.info("operator_redrive: %s paused by %s — %s", slug, by, reason or "(no reason)")

    try:
        from bot_squad_worker import autopilot as _autopilot
        for state in _autopilot.list_states(cfg, slug):
            if state.enabled and state.status == "running":
                _autopilot.stop(
                    cfg, slug, key=state.key,
                    reason="", stopped_by=f"operator_redrive pause ({by})",
                )
    except Exception:  # noqa: BLE001
        log.exception("operator_redrive: %s pause — failed to also stop running autopilots", slug)

    return meta


def resume(cfg: Any, slug: str) -> bool:
    """Clear the user-pause flag. Returns True iff it was actually set."""
    p = _pause_flag_path(cfg, slug)
    if p.exists():
        p.unlink()
        log.info("operator_redrive: %s resumed", slug)
        return True
    return False


def _state_file(cfg: Any, slug: str) -> Path:
    return _state_dir(cfg, slug) / "state.json"


def _load_state(cfg: Any, slug: str) -> dict:
    p = _state_file(cfg, slug)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(cfg: Any, slug: str, state: dict) -> None:
    d = _state_dir(cfg, slug)
    d.mkdir(parents=True, exist_ok=True)
    p = _state_file(cfg, slug)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Pending-backlog signal
# ---------------------------------------------------------------------------

def count_pending_backlog(cfg: Any, slug: str) -> int:
    """Count board tasks that still need clearing — top-level ``backlog/*.md``
    whose status is not terminal (``closed``) and which are not archived. The
    ``_gc/`` archive subdir is a child dir, so a top-level glob never re-counts
    already-archived tasks. An empty result is the operator's only idle state."""
    from bot_squad_worker import frontmatter as _fm

    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return 0
    n = 0
    for md in sorted(backlog.glob("*.md")):
        try:
            parsed = _fm.parse_or_none(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed:
            continue
        meta, _body = parsed
        meta = meta or {}
        if str(meta.get("archived", "")).strip().lower() in ("true", "yes", "1", "on"):
            continue
        status = str(meta.get("status", "")).strip().lower()
        if status in _TERMINAL_STATUSES:
            continue
        n += 1
    return n


# ---------------------------------------------------------------------------
# T-0475 — operator pacing: parallelism + best-effort weekly-quota target
# ---------------------------------------------------------------------------
# clarification-03: "orchestrating the sessions according to parallelism and
# token usage constraints; ideally ... weekly quota utilization constraints/
# targets IF we can get this info."
#
# The operator is an LLM session — it paces its OWN dispatching from its brief.
# This module exposes the pacing SIGNALS it should honor; the hard parallelism
# ceiling is still enforced at spawn (_enforce_parallel_cap, T-0239) and the
# re-drive defers under that backpressure. We surface:
#   * max_in_progress  — the parallelism cap (pace.py SSOT, TL-B / T-0482)
#   * live_dev_sessions— current load, and the quantity the cap is measured
#     against since T-0966 (see `_live_dev_count`); `in_progress` (the board
#     label) is still reported beside it, but it no longer gates anything
#   * weekly_target_pct— OPTIONAL utilization target (system_settings [operator])
#   * burn_tokens_per_hr / remaining_tokens — best-effort from telemetry's quota
#     estimate (Max weekly TOTAL is NOT queryable, telemetry.py — so the % target
#     is ADVISORY, never a hard block; degrade gracefully when no signal).

#: What ``max_in_progress`` counts, published on every pacing payload (T-0966
#: DoD4). A surface prints this beside the number so "7" is never a bare integer
#: an operator has to infer the unit of.
CAP_COUNTS = "live_dev_sessions"


def _in_progress_count(cfg: Any, slug: str) -> int:
    """Board tasks currently at ``status: in_progress``. Non-archived top-level
    ``backlog/*.md`` only.

    T-0966: this is REPORTED, not gated on. It used to be the load measured
    against ``max_in_progress``, which made that cap inert — the label is a
    field a session must remember to stamp, and 7 of 8 live devs had not, so the
    cap read 1/7 with eight sessions running. See :func:`_live_dev_count` for
    what the cap is measured against now, and ``dispatch.decide_topology`` for
    the same choice made (and measured) a month earlier for the same reason."""
    from bot_squad_worker import frontmatter as _fm

    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return 0
    n = 0
    for md in sorted(backlog.glob("*.md")):
        try:
            parsed = _fm.parse_or_none(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed:
            continue
        meta = (parsed[0] or {})
        if str(meta.get("archived", "")).strip().lower() in ("true", "yes", "1", "on"):
            continue
        if str(meta.get("status", "")).strip().lower() == "in_progress":
            n += 1
    return n


def _live_dev_count(cfg: Any, slug: str) -> int:
    """Live dev sessions on ``slug`` — the load the parallelism cap gates (T-0966).

    Delegates to ``sessions.count_live_dev_sessions``, the same counter spawn
    admission enforces, so the dashboard and the refusal can never disagree
    about how many lanes are running. Best-effort: pacing must never break the
    tick, and an unreadable session dir degrades to 0 rather than raising.
    """
    try:
        from bot_squad_worker import sessions as _sessions
        return _sessions.count_live_dev_sessions(cfg, slug)
    except Exception:  # noqa: BLE001 — pacing must never break the tick
        log.exception("pacing: live dev count failed for %s", slug)
        return 0


def _system_settings_path(cfg: Any) -> Optional[Path]:
    """Best-effort locate ``system_settings.toml`` — ``cfg.config_dir`` if set,
    else the conventional install path. A set ``cfg.config_dir`` is authoritative
    even when the file doesn't exist there yet (e.g. a fresh/isolated config
    dir): callers already handle a missing file (read failure -> None), and
    falling back past an explicit config_dir would leak the conventional
    install's file into a config_dir-scoped caller — exactly the T-0697 bug
    (an 'isolated' test fixture silently reading the real production
    system_settings.toml). None if neither is usable."""
    cdir = getattr(cfg, "config_dir", None)
    if cdir:
        return Path(cdir) / "system_settings.toml"
    p = Path("/home/www/bot-squad/config/system_settings.toml")
    return p if p.exists() else None


def weekly_quota_target_pct(cfg: Any) -> Optional[float]:
    """The operator's OPTIONAL weekly quota-utilization target (percent, e.g.
    ``20.0``), read best-effort from ``system_settings.toml`` ``[operator]
    .weekly_quota_target_pct``. None when unset/unreadable — the target is
    explicitly conditional ("IF we can get this info"), so absence is normal."""
    import tomllib

    p = _system_settings_path(cfg)
    if p is None:
        return None
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    v = ((data.get("operator") or {}).get("weekly_quota_target_pct"))
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _burn_signal(cfg: Any, slug: str) -> dict:
    """Best-effort read of telemetry's quota estimate (``telemetry._quota_path``):
    ``{burn_tokens_per_hr, remaining_tokens, rate_limit_429, spend_pct}``. All-None
    (spend_pct excepted, 0 for rate_limit_429) when no signal exists yet (Max
    remaining is never authoritative — estimate only).

    ``spend_pct`` (F2.7) is the best-effort weekly-budget spend-to-date percent:
    ``100 * (budget_tokens - remaining_tokens) / budget_tokens``, derived from the
    operator-set ``[quota]`` anchor telemetry persists alongside ``remaining_tokens``
    (``telemetry._update_quota`` writes ``anchor`` into the same file). None when no
    anchor is set — there is then no total to measure spend against."""
    from bot_squad_worker import telemetry as _telemetry

    q = _telemetry._quota_path(cfg, slug)
    try:
        data = json.loads(q.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"burn_tokens_per_hr": None, "remaining_tokens": None,
                "rate_limit_429": 0, "spend_pct": None}
    rl = data.get("rate_limit_429") or {}
    anchor = data.get("anchor") or {}
    remaining = data.get("remaining_tokens")
    spend_pct = None
    try:
        budget = float(anchor.get("budget_tokens"))
        if budget > 0 and remaining is not None:
            spend_pct = max(0.0, min(100.0, 100.0 * (budget - float(remaining)) / budget))
    except (TypeError, ValueError):
        spend_pct = None
    return {
        "burn_tokens_per_hr": data.get("burn_tokens_per_hr"),
        "remaining_tokens": remaining,
        "rate_limit_429": int(rl.get("count", 0)) if isinstance(rl, dict) else 0,
        "spend_pct": spend_pct,
    }


def pace_verdict(target_pct: Optional[float], spend_pct: Optional[float]) -> Optional[str]:
    """``under`` / ``on`` / ``over`` the weekly-target pace line, or None when
    either signal is unknown (unset target, or no quota anchor to measure spend
    against — the graceful-degradation case T-0475 already established)."""
    if target_pct is None or spend_pct is None:
        return None
    if spend_pct < target_pct:
        return "under"
    if spend_pct > target_pct:
        return "over"
    return "on"


# Additive ramp-up step (board tasks) applied per pacing read while under pace,
# mirroring backoff.py's AIMD additive-increase shape. Env-tunable, no redeploy.
def _pace_ramp_step() -> int:
    try:
        v = int(os.environ.get("BOT_SQUAD_PACE_RAMP_STEP", "2"))
    except (TypeError, ValueError):
        return 2
    return v if v > 0 else 2


def pacing_status(cfg: Any, slug: str) -> dict:
    """The operator's pacing dashboard — the signals it honors when deciding how
    many sessions to run (T-0475 / F2.6, ramp-up mechanized by F2.7 / T-0579).
    Composed from the parallelism cap (pace.py SSOT), current load, the optional
    weekly target + best-effort spend-to-date, and the AIMD backoff ceiling.
    ``recommendation`` is the one-word steer for the operator brief:

      * ``throttle``  — at/over the parallelism cap OR rate-limit 429s seen:
                        stop dispatching new sessions, let in-flight drain.
                        T-0966: "at/over the cap" now compares LIVE DEV SESSIONS
                        (``live_dev_sessions``) to ``max_in_progress``; the
                        ``in_progress`` board label is reported, never gated on.
      * ``ramp``      — a weekly target is set, spend-to-date is UNDER it, and
                        there is real headroom below BOTH the board cap
                        (``max_in_progress``) and the live AIMD backoff ceiling
                        (``backoff.effective_limit``): ``target_in_progress`` names
                        the concrete number of lanes to admit toward (F2.7 — this
                        is the mechanized ramp-UP; it never exceeds either bound,
                        so an active backoff clamp or a tight board cap silently
                        suppresses it back to ``advisory``).
      * ``advisory``  — a weekly target is set but spend-to-date is at/over it, OR
                        under it with no ramp headroom, OR the weekly total isn't
                        knowable (no quota anchor): pace by judgement toward it.
      * ``ok``        — under cap, no target/pressure: dispatch freely.

    Always safe + total (never raises): every signal degrades to None/0 so the
    operator still gets a usable dashboard on a fresh project with no telemetry."""
    from bot_squad_worker import pace as _pace

    try:
        cap = _pace.max_in_progress(cfg, slug)  # 0 = unlimited
    except Exception:  # noqa: BLE001 — pacing must never break the tick
        cap = 0
    # T-0966: the cap is measured against LIVE DEV SESSIONS. The board label is
    # still read and reported (`in_progress`) so its drift stays visible, but it
    # gates nothing — see `_in_progress_count`.
    lanes = _live_dev_count(cfg, slug)
    in_prog = _in_progress_count(cfg, slug)
    at_cap = cap > 0 and lanes >= cap
    target = weekly_quota_target_pct(cfg)
    burn = _burn_signal(cfg, slug)
    verdict = pace_verdict(target, burn["spend_pct"])

    try:
        from bot_squad_worker import backoff as _backoff
        backoff_ceiling = _backoff.effective_limit(cfg)
    except Exception:  # noqa: BLE001 — an unreadable backoff state is not fatal
        backoff_ceiling = None

    ramp_to: Optional[int] = None
    if at_cap or burn["rate_limit_429"] > 0:
        rec = "throttle"
    elif verdict == "under":
        # Never overrides an explicit max_in_progress or an active backoff clamp
        # (F2.7 DoD) — both bound the candidate below, so a clamped backoff or a
        # cap already saturated by in_prog collapses this back to "advisory".
        candidate = lanes + _pace_ramp_step()
        if cap > 0:
            candidate = min(candidate, cap)
        if backoff_ceiling is not None:
            candidate = min(candidate, backoff_ceiling)
        if candidate > lanes:
            ramp_to = candidate
            rec = "ramp"
        else:
            rec = "advisory"
    elif target is not None:
        rec = "advisory"
    else:
        rec = "ok"

    return {
        "max_in_progress": cap,            # 0 = unlimited
        # T-0966: name the quantity the cap gates, on the payload, so a surface
        # cannot print a bare "7" and leave the operator to infer what it counts.
        "cap_counts": CAP_COUNTS,
        "live_dev_sessions": lanes,        # the load measured against the cap
        "in_progress": in_prog,            # board LABEL — reported, gates nothing
        "at_cap": at_cap,
        "weekly_target_pct": target,       # None = unset (optional)
        "spend_pct": burn["spend_pct"],    # None = no quota anchor to measure against
        "pace_verdict": verdict,           # under/on/over/None
        "burn_tokens_per_hr": burn["burn_tokens_per_hr"],
        "remaining_tokens": burn["remaining_tokens"],  # estimate, may be None
        "rate_limit_429": burn["rate_limit_429"],
        "target_in_progress": ramp_to,     # None unless recommendation == "ramp"
        "recommendation": rec,
    }


# ---------------------------------------------------------------------------
# Re-drive (spawn) — purely scheduler-internal
# ---------------------------------------------------------------------------

def _respawn_operator(cfg: Any, slug: str) -> Optional[str]:
    """Spawn a fresh operator driving the standing 'clear the backlog' task.

    Returns the new SID, or None when the spawn is *deferred* (capacity / quota
    backpressure — "stalls out of time") or fails. The continue-vs-respawn gate
    (live_operator_sids) is applied by the caller, so this never double-drives.
    """
    from bot_squad_worker import sessions as S
    from bot_squad_worker import dispatch as _dispatch
    from bot_squad_worker.actions import ActionError

    # T-0783a: the respawn brief carries the CONCRETE pickup queue, not just the
    # "clear the backlog" directive. This is the seam the whole ticket turns on —
    # a re-driven operator that has to re-derive which tickets are takeable from
    # 57 board mds is the operator that left a reopened P1 sitting until the
    # stakeholder chased it by hand. A failure to compute it must never block the
    # respawn (an operator with the plain directive is what we had before), so it
    # degrades to "".
    #
    # T-0829: that same brief now OPENS with the active drive SCOPE, and the
    # queue below it is already narrowed to that scope — `pickup_queue` reads the
    # standing per-project setting itself (pace.read_drive), which is why no
    # argument is threaded through here. This is the half of «какой режим драйва
    # щас стоит» that agents read: every operator incarnation is TOLD the scope it
    # is driving under, plus the in-scope triage residue, instead of inferring a
    # mode from which tickets it happened to be handed. Absent config = the
    # widest scope = the queue this line produced before the axis existed.
    try:
        from bot_squad_worker import pickup as _pickup
        brief = _pickup.pickup_brief(_pickup.pickup_queue(cfg, slug))
    except Exception:  # noqa: BLE001
        log.exception("operator_redrive: pickup brief failed for %s", slug)
        brief = ""

    try:
        # T-0678: carry forward a sticky per-session `model` override from the
        # operator incarnation this respawn replaces — a full respawn mints a
        # BRAND-NEW SID (unlike sessions.resume()'s in-place carry-forward), so
        # without this the override would silently revert to the fleet/role
        # default on every re-drive.
        model = S.last_operator_model(cfg, slug) or None
        res = S.spawn(
            cfg, slug, "operator",
            initial_prompt=_dispatch.operator_standing_task(brief),
            owner="operator-redrive",
            model=model,
            dispatched_by="operator-redrive",  # T-0909: attributable
        )
        return res.get("sid")
    except ActionError as e:
        # The parallel-session cap / quota backpressure is normal — the operator
        # has, in effect, stalled out of its time budget. Defer quietly (don't
        # ERROR-spam every 60s) so re-drive backs off instead of respawning into
        # a wall. This is what makes it "NOT a never-recycled process".
        if "capacity reached" in str(e):
            log.debug("operator_redrive: respawn deferred for %s (%s)", slug, e)
        else:
            log.exception("operator_redrive: respawn failed for %s", slug)
        return None
    except Exception:  # noqa: BLE001
        log.exception("operator_redrive: respawn failed for %s", slug)
        return None


def bud_operator(cfg: Any, slug: str, *, requested_by: str = "") -> dict:
    """T-0932: bud an OPERATOR off deliberately, at a session's own request.

    The L1→L2 rung of the budding ladder («выделение оператора ... если надо
    менеджить несколько параллельных девов»). It is the SAME spawn the 60s
    re-drive would perform — :func:`_respawn_operator`, so the operator boots
    with the same pickup brief, the same carried-forward model and the same
    capacity backpressure handling — with the same two invariants applied
    ahead of it:

      * the T-0472 singleton (a live operator means one is already driving;
        a second would double-drive the board), and
      * the spawn cooldown, shared with the re-drive through the same state
        file so the two paths cannot stampede each other.

    T-0937 adds a third: the operator SEAT. A session holding it is handing the
    wheel over, so the seat is released once the new operator exists; ANY OTHER
    holder refuses the bud, same as a live operator does.

    What it deliberately does NOT re-check is the load threshold: a session
    asking for this has read :func:`dispatch.decide_topology` (via ``bsq bud``)
    and decided — triggers suggest, sessions decide (T-0929). Returns
    ``{ok, spawned, operator, reason}``; ``spawned`` is False for every
    no-op/deferred outcome, never an exception, so the caller can report the
    reason verbatim.
    """
    from bot_squad_worker import dispatch as _dispatch
    from bot_squad_worker.actions import ActionError

    if cfg.projects.get(slug) is None:
        raise ActionError(f"bud_operator: unknown project slug {slug!r}")
    if is_paused(cfg, slug):
        return {"ok": True, "spawned": False, "operator": None,
                "reason": "operator re-drive is PAUSED for this project "
                          "(`bsq operator resume` to lift it)"}

    live = _dispatch.live_operator_sids(cfg, slug)
    if live:
        return {"ok": True, "spawned": False, "operator": live[0],
                "reason": f"operator {live[0]} is already driving this board "
                          f"— exactly one per project (T-0472); route through it"}

    # T-0937: the seat is the OTHER way this board can already have a driver. A
    # session budding an operator while it itself holds the seat is not blocked
    # — that IS the L1→L2 handover, «выделение оператора», and it is the only
    # move that legitimately ends a root's own drive — so it releases the seat
    # first, in the same call, rather than leaving a claim behind that would
    # then contradict the operator it just spawned. Somebody ELSE holding the
    # seat is refused for exactly the reason a live operator is: two dispatchers
    # double-drive the backlog.
    from bot_squad_worker import operator_seat as _seat
    seat = _seat.seat_holder(cfg, slug)
    if seat is not None and seat["sid"] != (requested_by or ""):
        return {"ok": True, "spawned": False, "operator": None,
                "reason": f"{seat['sid']} holds the operator seat (via "
                          f"{seat['kind']}) and is driving this board directly "
                          f"— route through it, or have it hand the seat over "
                          f"(`bsq operator seat release`)"}

    state = _load_state(cfg, slug)
    last_spawn = float(state.get("last_spawn_at", 0) or 0)
    if time.time() - last_spawn < _SPAWN_COOLDOWN_SEC:
        return {"ok": True, "spawned": False, "operator": None,
                "reason": f"an operator spawn fired less than "
                          f"{_SPAWN_COOLDOWN_SEC}s ago — wait for it to register"}

    sid = _respawn_operator(cfg, slug)
    if not sid:
        return {"ok": True, "spawned": False, "operator": None,
                "reason": "spawn deferred under capacity/quota backpressure — "
                          "retry when a session slot frees"}

    # The handover, completed: the seat is dropped only once the operator that
    # replaces it actually exists. Releasing before the spawn would open a
    # window in which the 60s tick could mint a SECOND operator behind this one.
    # Best-effort — a failed release must not un-spawn a live operator; the
    # claim is inert anyway once `live_operator_sids` sees the new session.
    if seat is not None:
        try:
            _seat.release(cfg, slug, seat["sid"], force=True)
        except Exception:  # noqa: BLE001
            log.exception("operator_redrive: seat release failed for %s", slug)

    state["last_spawn_at"] = time.time()
    _save_state(cfg, slug, state)
    log.info("budding[%s]: %s budded off operator %s", slug,
             requested_by or "a session", sid)
    return {"ok": True, "spawned": True, "operator": sid,
            "reason": f"budded off operator {sid} — it drives the board from "
                      f"here; you stay user-facing and talk to IT, not to the devs"}


def tick(cfg: Any, slug: str) -> dict:
    """One re-drive pass for a single project. Returns a small record dict
    ``{action, ...}`` describing what the pass decided (for tests + the journal).

    actions: ``disabled`` | ``paused`` | ``idle-empty-backlog`` | ``continue`` |
    ``seat-held`` | ``direct-tier`` | ``cooldown`` | ``deferred`` | ``respawned``.
    """
    if not _enabled():
        return {"action": "disabled"}

    # STOP re-driving when the user has paused (clarification-01). T-0929 routes
    # it through the shared gate so `automation.MECHANISMS` and its source-scan
    # test can see this mechanism — same predicate, same behaviour, one call
    # shape across all of them.
    from bot_squad_worker import automation as _automation
    if not _automation.gate(cfg, slug, "operator_redrive"):
        return {"action": "paused"}

    # "When on, it always has a task to clear the backlog"; an empty backlog
    # (nothing actionable left) is the only idle state (clarification-03).
    pending = count_pending_backlog(cfg, slug)
    if pending <= 0:
        return {"action": "idle-empty-backlog", "pending": 0}

    # Continue-vs-respawn rides the T-0472 seam: a live operator means one is
    # already driving (continue — no-op, exactly one per project); none means the
    # prior incarnation exited (or never started), so re-drive spawns a fresh one.
    from bot_squad_worker import dispatch as _dispatch
    live = _dispatch.live_operator_sids(cfg, slug)
    if live:
        return {"action": "continue", "operator": live[0], "pending": pending}

    # T-0937 — the SEAT gate. Everything below asks "does the load justify an
    # operator TIER"; this asks the prior question, "does the board already have
    # a DRIVER". It was answered by `live_operator_sids` alone, which sees only
    # sessions whose ROLE is operator — so a root user-conversation session that
    # had just set the drive mode and was actively clearing the board was
    # invisible here, and the tick minted a second dispatcher 25s behind it
    # («ты должен стать оператором одновременно с юзер-сессией»). The seat is
    # the root's claim, and it outranks the tier: while a live root wears the
    # operator hat there is nothing for a spawned operator to do but double-drive.
    #
    # Ordered BEFORE decide_topology deliberately: the seat holds even at
    # operator tier (that is the whole point — parallelism WITHOUT a separate
    # operator), and reading it here keeps the journal action distinguishable
    # from T-0855's load-based `direct-tier`. `seat_holder` never raises and
    # degrades to "vacant", i.e. to the behaviour below.
    from bot_squad_worker import operator_seat as _seat
    seat = _seat.seat_holder(cfg, slug)
    if seat is not None:
        return {"action": "seat-held", "pending": pending, "seat": seat}

    # T-0855 — the SCALING-LADDER gate, and the reason this ticket is code and
    # not a role-doc edit. Everything above says "there is pending work and no
    # operator", which used to mean "spawn one" unconditionally: a project with
    # ONE open task and a live user-conversation session got an operator back
    # within 60s, so «когда поток задач маленький, не устраивать цепочку из
    # юзер-сессия -> оператор -> дев-сессия» could not hold however the contracts
    # were worded. Now the tier is promoted only when the load justifies it
    # (dispatch.decide_topology) — and only ever suppressed while a live
    # user-conversation session exists to drive the board directly, so an
    # unattended project still gets its operator exactly as before.
    #
    # A failure here must NEVER strand the backlog: any error falls through to
    # the respawn, i.e. to pre-T-0855 behaviour.
    try:
        topo = _dispatch.decide_topology(cfg, slug)
    except Exception:  # noqa: BLE001 — a broken gate must not stop the operator
        log.exception("operator_redrive: topology gate failed for %s", slug)
        topo = None
    if topo is not None and not topo["operator_needed"]:
        return {
            "action": "direct-tier",
            "pending": pending,
            "topology": topo,
            "user_sessions": topo["attending_user_session_sids"],
        }

    # Cooldown gate (belt-and-braces against overlapping ticks / a fast-exiting
    # operator stampeding the spawn path).
    state = _load_state(cfg, slug)
    last_spawn = float(state.get("last_spawn_at", 0) or 0)
    if time.time() - last_spawn < _SPAWN_COOLDOWN_SEC:
        return {"action": "cooldown", "pending": pending}

    sid = _respawn_operator(cfg, slug)
    if not sid:
        # Spawn deferred under backpressure ("stalls out of time") — re-drive
        # backs off; a later tick retries when a slot / quota frees.
        return {"action": "deferred", "pending": pending}

    state["last_spawn_at"] = time.time()
    _save_state(cfg, slug, state)
    log.info("operator_redrive[%s]: re-drove operator %s (%d pending)", slug, sid, pending)
    return {"action": "respawned", "operator": sid, "pending": pending}


def operator_tick(cfg: Any) -> None:
    """Scheduler entry point (T-0474): one re-drive pass across every project.

    Per-project errors are caught and logged so one bad project never kills the
    sweep — same contract as the sibling lifecycle ticks. Registered on the 60s
    cadence in ``scheduler.py``.
    """
    for slug in cfg.projects:
        try:
            tick(cfg, slug)
        except Exception:  # noqa: BLE001
            log.exception("operator_tick: unhandled error for project %s", slug)
