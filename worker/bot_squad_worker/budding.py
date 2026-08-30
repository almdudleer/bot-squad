"""T-0932: GRADUAL BUDDING — the system's journey across energy levels.

Stakeholder (2026-08-30, live tmux; «и сделайте наконец это постепенное
почкование мне»):

    "когда у нас одна сессия на всё, потом она может отпочковать дева а сама
    дифференцироваться чисто в юзер-сессию, если сильно много параллельных
    запросов от юзера, и может отпочковать оператора если сильно много
    отдельных девов и их оркестрации"

    "юзер-сессия-дев <-> отделение себя в юзер-сессию, а разработка
    дев-сессии или оператору ... <-> выделение оператора (превращение себя в
    юзер-сессию оператора)"

So the topology is not a fixed org chart — it is a LADDER the project climbs
under load and slides back down when the load drops:

    L0 solo   one session does everything: it talks to the user AND holds the
              task. Cheapest possible shape; the default a project starts in.
    L1 work   the work is split off: a DEV bud holds the task, the original
              narrows back to pure user-conversation ("отделение себя").
    L2 orch   the orchestration is split off too: an OPERATOR bud steers the
              devs, the original stays user-facing and now talks to the
              operator instead of to devs ("превращение себя в юзер-сессию
              оператора").

WHAT IS NEW HERE, AND WHAT IS NOT. Almost every moving part already existed;
the gap this module fills is COMPOSITION + TRIGGERS.

  * READ side of the L1→L2 rung: :func:`dispatch.decide_topology` (T-0855)
    already answers "does this project need an operator tier right now", and
    ``operator_redrive.tick`` already ACTS on it every 60s. This module does
    not re-derive that verdict or invent a second set of thresholds — it reads
    the same one, so a session and the scheduler can never disagree about
    which rung the project is on. What was missing on that rung is the
    session's own deliberate move (``bsq bud operator``) and the parent's
    morph.
  * The differentiation primitive: ``sessions.morph_session`` (T-0509). Its
    DE-differentiation half was missing — a session could morph INTO dev /
    teamlead / operator but never back down into ``user-conversation``, which
    is literally «отделение себя в юзер-сессию». T-0932 adds that role to
    ``MORPH_ROLES``; this module is its main caller.
  * The handover: ``bsq spawn <task>`` already assembles the full ticket brief,
    inherits the initiative, applies the T-0909 model rules and stamps
    ``parent_sid``. A dev bud IS that spawn — no second brief assembler.
  * The bud's end of life: T-0465 graceful exit + T-0930's handoff/compact
    lifecycle already own it. A dev bud dies when its task goes terminal; the
    ROOT never exits because a ``user-conversation`` role has no done-signal
    (:func:`graceful_exit.work_done`).

    ★ Which is exactly why budding off a dev MUST SHED the parent's task
    binding. ``work_done`` tests ``task_id`` BEFORE it falls through to the
    role, so a parent that morphed to ``user-conversation`` but kept the
    binding would be graceful-exited the moment its BUD set the task to
    ``totest`` — the root session killed by its own child's success. The shed
    is not tidiness; it is what makes "the root session never exits" true.

TRIGGERS ARE CONSERVATIVE AND ADVISORY (the ticket's item 1, and his T-0929
line that parallelism is decided by the sessions themselves): this module
never spawns, never morphs and never kills anything. It computes a verdict and
:func:`budding_check` SUGGESTS it to the session itself, which decides. The
write side is a ``bsq bud …`` verb a session runs deliberately.

Deflation is part of the same ladder, not a separate mechanism:
  * L1→L0 — with no live devs, no operator and one lone piece of queued work,
    the root is told it can just take it (``bsq bud absorb``): re-absorbing the
    work beats paying for a second process.
  * L2→L1 — NOT a session-driven move. An operator with an empty backlog exits
    by itself (T-0465) and the T-0855 gate leaves it off while the flow stays
    small, so the tier de-escalates by attrition. A session suggesting a PEER's
    death is outside its authority (role contract: never kill peer sessions on
    your own authority), so the verdict here is ``hold`` with a reason naming
    who owns it.

Kill switch: ``BOT_SQUAD_BUDDING=0`` disables the suggestion tick entirely
(the read verbs stay available — they are pure reads).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from bot_squad_worker import frontmatter as _fm

log = logging.getLogger(__name__)

# --- the ladder -------------------------------------------------------------

LEVEL_SOLO = "L0-solo"
LEVEL_WORK_SPLIT = "L1-work-split"
LEVEL_ORCH_SPLIT = "L2-orchestration-split"

# Verdicts. ``hold`` is the overwhelmingly common answer — this ladder should
# move rarely, and a suggestion that fires often is one nobody reads.
HOLD = "hold"
BUD_DEV = "bud_dev"
BUD_OPERATOR = "bud_operator"
ABSORB = "absorb"


def budding_enabled() -> bool:
    """Master switch for the SUGGESTION tick. Read verbs are never disabled."""
    return os.environ.get("BOT_SQUAD_BUDDING", "1") != "0"


def request_pressure_threshold() -> int:
    """How many OTHER queued user requests make «сильно много параллельных
    запросов от юзера» true, while this session is head-down holding a task.

    Deliberately small (2) and deliberately not 1: one queued request behind
    the one you are doing is an ordinary backlog, not pressure — the shape he
    described is the user piling requests on a session that cannot record them
    because it is busy building. Tunable via
    ``BOT_SQUAD_BUD_REQUEST_PRESSURE``; a non-positive / garbage value falls
    back to the default rather than collapsing to "bud on everything".
    """
    raw = os.environ.get("BOT_SQUAD_BUD_REQUEST_PRESSURE")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 2


def cooldown_minutes() -> int:
    """Minimum gap between two budding suggestions to the SAME session."""
    raw = os.environ.get("BOT_SQUAD_BUDDING_COOLDOWN_MINUTES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 30


# --- task states: read T-0931's SSOT, never a local copy ---------------------

# T-0931 owns the task state machine (``task_states``: TICKET_STATUSES,
# TRANSITIONS, ACTIVE_STATES, PARKED_STATES, is_parked). The budding triggers
# READ those states rather than inventing counters, and the import is HARD on
# purpose — a soft import with a local mirror would keep working while silently
# diverging from the SSOT, which is the failure mode this whole seam exists to
# avoid. If task_states is missing, budding must fail loudly, not guess.


def _state_sets() -> tuple[frozenset[str], frozenset[str]]:
    """(ACTIVE_STATES, PARKED_STATES) — T-0931's SSOT, no local copy."""
    from bot_squad_worker import task_states as _ts
    return (frozenset(_ts.ACTIVE_STATES), frozenset(_ts.PARKED_STATES))


def pressure_states() -> frozenset[str]:
    """Task statuses that count as an OUTSTANDING USER REQUEST.

    Derived from T-0931's ACTIVE_STATES by two explicit adjustments, because
    "what the drive machinery may pick up" and "what the user is still waiting
    for" are different questions:

      + ``planned``  — T-0931 parks it (nothing automatic should DRIVE a
        planned ticket), but a freshly-filed request the user made IS something
        he is waiting for. Pressure measures HIS queue, not the drive queue.
      − ``totest``   — handed over for review. From the requester's side that
        request is answered; counting it would keep the pressure high forever
        on a board that reviews slowly.

    Written as set algebra over the SSOT (not a literal list) so a state added
    by T-0931 later flows in here without a second edit.
    """
    active, _parked = _state_sets()
    return frozenset(active | {"planned"}) - {"totest"}


# --- board + roster reads ---------------------------------------------------

def _board_rows(cfg: Any, slug: str) -> list[tuple[str, str]]:
    """(task_id, status) for every non-archived top-level board ticket.

    Top-level glob only — ``_gc/`` is a child dir, so archived tickets are
    never re-counted (same contract as ``operator_redrive.count_pending_backlog``).
    """
    out: list[tuple[str, str]] = []
    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return out
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
        tid = str(meta.get("id") or "").strip()
        if not tid:
            continue
        out.append((tid, str(meta.get("status", "")).strip().lower()))
    return out


def _live_sessions(cfg: Any, slug: str) -> list[tuple[str, dict]]:
    """(sid, meta) for every LIVE session md of this project (active + paused).

    Pure md scan — the same source ``dispatch.live_role_sids`` reads, so the
    ladder and the T-0855 topology gate can never see different rosters.
    """
    from bot_squad_worker import sessions as S

    out: list[tuple[str, dict]] = []
    sess_dir = cfg.data_dir / slug / "sessions"
    if not sess_dir.exists():
        return out
    for md in sorted(sess_dir.glob("*.md")):
        meta = S._read_session_metadata(md)
        if meta is None or not S._is_live_holder(meta):
            continue
        out.append((str(meta.get("sid") or md.stem), meta))
    return out


def is_root_session(sid: str, meta: dict | None = None) -> bool:
    """True for the session at the BOTTOM of the ladder — the one that owns the
    conversation with the user and differentiates under load.

    Identified by the window embedded in its IMMUTABLE SID, not by its stored
    role: the whole point of budding is that this session's role oscillates
    (user-conversation → dev → user-conversation …), so a role-based test would
    stop recognising the root at exactly the moment it is doing the thing this
    module is about. Same identity rule ``live_user_conversation_sid`` uses
    (T-0478), so "the root" and "the attendant" are one session by construction.

    A hand-launched session that carries the ``recycle_exempt`` / user-session
    markers is NOT auto-classified as root here: it may be the human's own
    scratch pane, and suggesting it bud off a dev would be noise. It can still
    ask (``bsq bud`` answers for any session) — it just is not swept.
    """
    from bot_squad_worker import sessions as S

    window = S._window_from_sid(sid) or str((meta or {}).get("window") or "")
    return S._derive_role(window, None, None) == "user-conversation"


def observe(cfg: Any, slug: str) -> dict:
    """The ladder's current rung + the two pressures that move it. Pure read.

    Returns ``{ok, level, counts, thresholds, topology, queued_requests,
    held_task_ids, root_sids, signals}``.
    """
    from bot_squad_worker import dispatch as _dispatch
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError

    if cfg.projects.get(slug) is None:
        raise ActionError(f"observe: unknown project slug {slug!r}")

    live = _live_sessions(cfg, slug)
    roots = [sid for sid, m in live if is_root_session(sid, m)]
    # BUD devs, not every dev-role session: the root itself reads as ``dev``
    # while it holds a task (that IS the L0 shape), so counting it here would
    # report the solo session as already work-split and hide the very rung the
    # ladder starts on. ``topology.counts.live_devs`` is T-0855's own count and
    # DOES include it — both are returned, and they answer different questions:
    # "how much load is on the board" vs "how many buds exist".
    devs = [(sid, m) for sid, m in live
            if S._role_of(m) == "dev" and not is_root_session(sid, m)]

    # Every task any live session holds — the ones that are NOT queued, because
    # somebody is on them. Computed from the live roster rather than from board
    # labels: a ``status: in_progress`` left behind by a dead dev is precisely
    # the case where the label lies and the roster does not (the ★ note in
    # dispatch.py — board status is reported there, never gated on).
    held: set[str] = set()
    for _sid, m in live:
        held |= {t for t in S._full_task_set(m) if t and t != "~"}

    wanted = pressure_states()
    queued = sorted(tid for tid, st in _board_rows(cfg, slug)
                    if st in wanted and tid not in held)

    # The L1→L2 rung is not re-derived here: it IS T-0855's verdict.
    try:
        topo = _dispatch.decide_topology(cfg, slug)
    except Exception as exc:  # noqa: BLE001 — advisory; report, never sink
        topo = None
        topo_error = f"{type(exc).__name__}: {exc}"
    else:
        topo_error = None

    operators = (topo or {}).get("live_operator_sids") or []
    if operators:
        level = LEVEL_ORCH_SPLIT
    elif devs:
        level = LEVEL_WORK_SPLIT
    else:
        level = LEVEL_SOLO

    return {
        "ok": True,
        "level": level,
        "counts": {
            "bud_devs": len(devs),
            "live_operators": len(operators),
            "root_sessions": len(roots),
            "queued_requests": len(queued),
            "held_tasks": len(held),
        },
        "thresholds": {
            "request_pressure": request_pressure_threshold(),
            "pressure_states": sorted(wanted),
        },
        "topology": topo,
        "topology_error": topo_error,
        "queued_requests": queued,
        "held_task_ids": sorted(held),
        "root_sids": roots,
        "bud_dev_sids": [sid for sid, _m in devs],
        "operator_sids": list(operators),
    }


def decide_budding(cfg: Any, slug: str, sid: str | None = None) -> dict:
    """Should ``sid`` bud right now — and into what? Advisory, pure read.

    Returns ``{ok, sid, level, verdict, task_id, command, reason, observation}``
    where ``verdict`` ∈ ``hold`` | ``bud_dev`` | ``bud_operator`` | ``absorb``
    and ``command`` is the exact ``bsq`` verb that performs it (empty on hold).

    ``sid=None`` answers for the project's root session when there is exactly
    one, so the scheduler and ``bsq bud`` ask the same question.
    """
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError

    obs = observe(cfg, slug)

    live = dict(_live_sessions(cfg, slug))
    if sid is None:
        roots = obs["root_sids"]
        if len(roots) != 1:
            return _hold(sid, obs,
                         f"no single root session to answer for "
                         f"({len(roots)} user-conversation session(s) live)")
        sid = roots[0]
    meta = live.get(sid)
    if meta is None:
        raise ActionError(f"decide_budding: {sid!r} is not a live session of {slug!r}")

    role = S._role_of(meta)
    my_tasks = sorted(t for t in S._full_task_set(meta) if t and t != "~")
    topo = obs["topology"] or {}
    queued = obs["queued_requests"]
    pressure = len(queued)
    threshold = obs["thresholds"]["request_pressure"]

    # --- L1 → L2: too many devs / too much orchestration to steer by hand.
    # The trigger is T-0855's own verdict, not a second opinion: `tier ==
    # "operator"` means the load has passed the ceilings that gate direct
    # dispatch. Checked FIRST because it outranks the dev rung — a session that
    # is drowning in orchestration should not also be handed a task.
    if topo.get("tier") == "operator" and not obs["operator_sids"]:
        if is_root_session(sid, meta) or role == "user-conversation":
            return {
                "ok": True, "sid": sid, "level": obs["level"],
                "verdict": BUD_OPERATOR, "task_id": None,
                "command": "bsq bud operator",
                "reason": (
                    f"orchestration load has passed the direct-drive ceiling "
                    f"({topo.get('counts', {}).get('live_devs')} live dev(s), "
                    f"{topo.get('counts', {}).get('tasks_in_flight')} task(s) in "
                    f"flight; ceilings {topo.get('thresholds', {}).get('max_devs')}"
                    f"/{topo.get('thresholds', {}).get('max_tasks')}) and no "
                    f"operator is live — bud one off and stay user-facing. "
                    f"(The 60s re-drive would also spawn one; running it "
                    f"yourself is the deliberate, immediate form and reads the "
                    f"SAME thresholds.)"
                ),
                "observation": obs,
            }

    # --- L0 → L1: the user is queueing requests at a session that is head-down
    # holding a task. Hand the task to a bud, narrow back to the conversation.
    if my_tasks and pressure >= threshold:
        return {
            "ok": True, "sid": sid, "level": obs["level"],
            "verdict": BUD_DEV, "task_id": my_tasks[0],
            "command": f"bsq bud dev {my_tasks[0]}",
            "reason": (
                f"you are holding {my_tasks[0]} while {pressure} other user "
                f"request(s) sit queued and unheld ({', '.join(queued[:5])}"
                f"{'…' if len(queued) > 5 else ''}) — at or over the pressure "
                f"threshold of {threshold}. Bud the work off to a dev and "
                f"narrow back to the conversation, so requests get recorded "
                f"and driven in parallel instead of behind your build."
            ),
            "observation": obs,
        }

    # --- L1 → L0 deflation: nothing is running, one lone piece of queued work,
    # and this session is free. A second process for it is pure overhead.
    if (not my_tasks and not obs["bud_dev_sids"] and not obs["operator_sids"]
            and len(queued) == 1 and is_root_session(sid, meta)):
        return {
            "ok": True, "sid": sid, "level": obs["level"],
            "verdict": ABSORB, "task_id": queued[0],
            "command": f"bsq bud absorb {queued[0]}",
            "reason": (
                f"the load dropped to one queued request ({queued[0]}) with no "
                f"live dev and no operator — take it yourself instead of paying "
                f"for a second process. This is the ladder sliding back down to "
                f"L0; spawn a dev instead if you expect the user to keep talking."
            ),
            "observation": obs,
        }

    # --- hold, with the reason naming which pressure is short.
    if obs["operator_sids"] and not obs["bud_dev_sids"]:
        return _hold(sid, obs,
                     "an operator is live with no devs under it — the tier "
                     "de-escalates by attrition (its own graceful exit on an "
                     "empty backlog, T-0465), never by a peer suggesting it. "
                     "Nothing for you to do.")
    if my_tasks:
        return _hold(sid, obs,
                     f"holding {', '.join(my_tasks)} with {pressure} queued "
                     f"request(s) — under the pressure threshold of {threshold}. "
                     f"Keep building.")
    return _hold(sid, obs,
                 f"level {obs['level']}: {len(obs['bud_dev_sids'])} bud dev(s), "
                 f"{pressure} queued request(s) — nothing to differentiate.")


def _hold(sid: str | None, obs: dict, reason: str) -> dict:
    return {
        "ok": True, "sid": sid, "level": obs["level"], "verdict": HOLD,
        "task_id": None, "command": "", "reason": reason, "observation": obs,
    }


# --- the SUGGESTION tick ----------------------------------------------------

_SUGGESTION_HEADER = "🌱 BUDDING (T-0932, advisory — you decide)"


def suggestion_text(decision: dict) -> str:
    """The one advisory block a session receives. Names the fact first, then
    the exact verb — a suggestion the session has to translate into a command
    is one it will skip."""
    return (
        f"{_SUGGESTION_HEADER}: {decision['reason']}\n\n"
        f"Run `{decision['command']}` if you agree — or ignore this; the "
        f"system will not bud on its own (parallelism is decided by the "
        f"sessions, T-0929). `bsq bud` shows the full ladder read."
    )


def budding_check(cfg: Any, slug: str) -> dict:
    """One suggestion pass for a project. Idempotent + cooldown-guarded.

    Only ROOT sessions are swept, at most one suggestion per session per
    cooldown window, and never into a pane a human is watching or one that is
    pinned — a budding advisory typed into the stakeholder's own composer
    mid-turn is the exact class of automatic pane write T-0926/T-0930 were
    filed about. Every gate fails closed: on any doubt we defer to the next
    tick rather than write into a live pane.
    """
    from bot_squad_worker import autocompact as _autocompact
    from bot_squad_worker import recycle_gate as _gate
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError

    if not budding_enabled():
        return {"ok": True, "disabled": True, "suggested": []}
    if cfg.projects.get(slug) is None:
        raise ActionError(f"budding_check: unknown project slug {slug!r}")

    now = time.time()
    cooldown = cooldown_minutes() * 60
    sessions_dir = cfg.data_dir / slug / "sessions"
    suggested: list[dict] = []

    try:
        obs_roots = observe(cfg, slug)["root_sids"]
    except Exception:
        log.exception("budding_check: observe failed for %s", slug)
        return {"ok": False, "suggested": []}

    panes = {}
    if obs_roots:
        try:
            user = S._get_current_user()
            panes = {S.compute_sid(user, p.window, p.pane_id): p for p in S.list_panes()}
        except Exception:  # noqa: BLE001 — no tmux, no delivery; not an error
            log.debug("budding_check: pane scan failed for %s", slug, exc_info=True)
            panes = {}

    for sid in obs_roots:
        pane = panes.get(sid)
        if pane is None:
            continue
        md_path = S._find_session_md(sessions_dir, sid, None)
        meta = S._read_session_metadata(md_path) if md_path else None
        if meta is None:
            continue
        if str(meta.get("status", "")).strip().lower() != "active":
            continue
        # T-0926 follow-up: a pinned session takes no automatic action of ANY
        # kind, this one included.
        if _gate.session_pinned(meta):
            continue
        last = _parse_iso(meta.get("budding_suggested_at"))
        if last is not None and (now - last) < cooldown:
            continue

        try:
            decision = decide_budding(cfg, slug, sid)
        except Exception:
            log.exception("budding_check: decide failed for %s", sid)
            continue
        if decision["verdict"] == HOLD:
            continue

        # Never type into a pane a human is watching, and never mid-turn.
        try:
            if _gate.is_attached(pane.pane_id, sid=sid, now=now):
                continue
            # T-0930's gate, not bare ``composer_ready``: never write over the
            # human's half-typed text («только не надо ее компактить, когда у
            # меня текст во вводе»). A suggestion can always wait a tick.
            if not _autocompact.composer_free(
                    _autocompact._capture_pane(pane.pane_id), sid=sid, now=now):
                continue
        except Exception:  # noqa: BLE001 — fail CLOSED: skip this tick
            log.debug("budding_check: liveness gates errored for %s", sid,
                      exc_info=True)
            continue

        try:
            S._deliver_prompt(pane.pane_id, suggestion_text(decision),
                              data_dir=cfg.data_dir, sid=sid)
        except Exception:
            log.exception("budding_check: inject failed for %s", sid)
            continue
        suggested.append({"sid": sid, "verdict": decision["verdict"],
                          "task_id": decision["task_id"],
                          "level": decision["level"]})
        meta["budding_suggested_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        try:
            S._write_session_metadata(md_path, meta, atomic=True)
        except OSError:
            log.exception("budding_check: could not persist cooldown for %s", sid)

    if suggested:
        log.info("budding_check[%s]: suggested %d bud(s): %s",
                 slug, len(suggested), suggested)
    return {"ok": True, "suggested": suggested}


def _parse_iso(ts: Any) -> float | None:
    """ISO-8601 ``...Z`` → epoch seconds, or None. (Local copy of the two-line
    helper every tick module carries; importing drift's would couple the
    budding ladder to the drift enforcer for no reason.)"""
    s = str(ts or "").strip()
    if not s or s == "~":
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return None
