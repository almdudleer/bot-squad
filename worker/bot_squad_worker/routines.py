"""Routines — Process Paradigm M1 / F1.1 (T-0464).

Source of truth (``vision/INI-XX-process-paradigm-SOURCE-VERBATIM.md`` Part A):

  > "Routine -- a rule, spawns sessions (see below) doing work according to
  >  instruction. Spawned according to some rule, generally any incoming event the
  >  integrations allow, simplest and very distinct type of condition is schedule."

A **Routine** is a declared rule = an *instruction* + a *trigger*. When the
trigger is DUE, the system spawns ONE session to do the work per the instruction.
The routine is the SECOND thing implementing the assignment interface (T-0463):
its assignment face is :class:`bot_squad_worker.assignment.RoutineAssignment`
(read its instruction via ``read_text``, write its result into the ONE reusable
``Artifact`` seam via ``write_result``).

Two layers:

* **Trigger** — a small pluggable abstraction. The simplest + most distinct
  trigger is a **schedule** (cron-like): :class:`ScheduleTrigger`. Integration-
  event triggers slot in later by adding a ``Trigger`` subclass + a branch in
  :func:`make_trigger`; nothing else in this module assumes "schedule".
* **Entity + store** — a Routine is persisted as one md under
  ``data/<slug>/routines/<R-NNNN>-*.md`` (id from the shared ``idalloc``
  allocator), instruction in the body, trigger/schedule/status/next_run_at in the
  frontmatter.

The firing mechanism is the EXISTING scheduler tick (:func:`routine_tick`, wired
in ``scheduler.py``) — NOT a new daemon. Grounded in "simplest condition is
schedule" + reuse of scheduler.py (T-0464 DECISION; cost-of-mistake LOW). Each
:func:`tick` fires every DUE routine exactly once: after firing it advances
``next_run_at`` strictly past ``now`` so the same due-tick can't double-spawn.

Worker-only module (no api mirror) — declared/listed through the worker socket
actions ``routine_declare`` / ``routine_list`` (and ``bsq routine ...``).
"""
from __future__ import annotations

import abc
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class RoutineError(ValueError):
    """Raised for an invalid trigger / schedule / declaration."""


# ---------------------------------------------------------------------------
# Time helpers — all routine timestamps are UTC-aware ISO strings.
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Trigger layer — pluggable; schedule is the first (and simplest) type.
# ---------------------------------------------------------------------------

class Trigger(abc.ABC):
    """A rule for WHEN a routine fires. Schedule today; events later."""

    #: short type tag persisted on the routine ("schedule", ...)
    type: str = "trigger"

    @abc.abstractmethod
    def next_fire(self, after: datetime, *, inclusive: bool) -> Optional[datetime]:
        """The next time this trigger fires relative to ``after``.

        ``inclusive=True`` may return ``after`` itself if it matches (used at
        declare time to seed the first ``next_run_at``); ``inclusive=False``
        returns a time STRICTLY after ``after`` (used after a fire so the same
        due-tick never re-fires). ``None`` means "never again".
        """


class ScheduleTrigger(Trigger):
    """A cron-schedule trigger — the simplest, most distinct condition (Part A).

    ``spec`` is a standard 5-field crontab expression (``min hour dom mon dow``),
    interpreted in UTC. Built on apscheduler's :class:`CronTrigger` (already a
    worker dependency) so the firing maths is battle-tested, not hand-rolled.
    """

    type = "schedule"

    def __init__(self, spec: str):
        from apscheduler.triggers.cron import CronTrigger

        self.spec = spec
        try:
            self._cron = CronTrigger.from_crontab(spec, timezone="UTC")
        except (ValueError, KeyError) as e:
            raise RoutineError(f"invalid cron schedule {spec!r}: {e}") from e

    def next_fire(self, after: datetime, *, inclusive: bool) -> Optional[datetime]:
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        if inclusive:
            # next fire AT or after `after` (CronTrigger returns `after` if it matches)
            return self._cron.get_next_fire_time(None, after)
        # strictly after: tell CronTrigger we just fired at `after`
        return self._cron.get_next_fire_time(after, after)


def make_trigger(trigger_type: str, spec: str) -> Trigger:
    """Build a :class:`Trigger` from its declared type + spec.

    Only ``schedule`` exists today; an unknown type is a :class:`RoutineError`.
    Event triggers slot in here (one new ``elif`` + a ``Trigger`` subclass).
    """
    t = (trigger_type or "").strip().lower()
    if t == "schedule":
        return ScheduleTrigger(spec)
    raise RoutineError(
        f"unknown trigger type {trigger_type!r} (only 'schedule' is supported today)"
    )


# ---------------------------------------------------------------------------
# Entity + store
# ---------------------------------------------------------------------------

ACTIVE = "active"
PAUSED = "paused"
_VALID_STATUSES = frozenset({ACTIVE, PAUSED})


@dataclass
class Routine:
    """A declared routine, loaded from its store md."""

    id: str
    title: str
    instruction: str
    trigger_type: str
    schedule: str
    status: str
    created: Optional[str]
    last_run_at: Optional[str]
    next_run_at: Optional[str]
    file_path: Path

    def trigger(self) -> Trigger:
        return make_trigger(self.trigger_type, self.schedule)

    def is_due(self, now: datetime) -> bool:
        """True iff active and ``now`` has reached ``next_run_at``."""
        if self.status != ACTIVE:
            return False
        nxt = _parse_iso(self.next_run_at)
        if nxt is None:
            return False
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now >= nxt

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "trigger": self.trigger_type,
            "schedule": self.schedule,
            "status": self.status,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "file_path": str(self.file_path),
        }


def routines_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "routines"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "routine")[:48]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def declare(cfg: Any, slug: str, *, instruction: str, schedule: str,
            title: Optional[str] = None, trigger: str = "schedule",
            provenance: Optional[str] = None,
            now: Optional[datetime] = None) -> dict:
    """Declare + persist a new Routine. Returns ``{id, file_path, next_run_at}``.

    Validates the trigger/schedule (a bad cron raises :class:`RoutineError`
    BEFORE anything is written), allocates the next ``R-NNNN`` via the shared
    idalloc allocator, computes the first ``next_run_at`` (inclusive of ``now``),
    and writes the routine md (instruction in the body).
    """
    if cfg.projects.get(slug) is None:
        raise RoutineError(f"unknown project slug {slug!r}")
    instruction = (instruction or "").strip()
    if not instruction:
        raise RoutineError("routine declare: empty instruction")
    schedule = (schedule or "").strip()
    if not schedule:
        raise RoutineError("routine declare: empty schedule")

    trig = make_trigger(trigger, schedule)  # validates; raises before any write
    now = now or _utcnow()
    next_run = trig.next_fire(now, inclusive=True)

    from bot_squad_worker import idalloc

    rid = idalloc.allocate_id(cfg.data_dir, slug, "routine")
    title = (title or instruction.splitlines()[0]).strip()[:240]
    created = _iso(now)

    meta = {
        "id": rid,
        "title": title,
        "status": ACTIVE,
        "trigger": trig.type,
        "schedule": schedule,
        "created": created,
        "last_run_at": None,
        "next_run_at": _iso(next_run),
    }
    if provenance:
        meta["provenance"] = provenance

    from bot_squad_worker import frontmatter as fm

    body = f"## Instruction\n\n{instruction}\n"
    content = fm.dump(meta, body)
    path = routines_dir(cfg, slug) / f"{rid}-{_slugify(title)}.md"
    _atomic_write(path, content)

    log.info("routine declared: %s (%s %s) next_run=%s [%s]",
             rid, trig.type, schedule, meta["next_run_at"], slug)
    return {"ok": True, "id": rid, "file_path": str(path),
            "next_run_at": meta["next_run_at"]}


def _routine_from_md(path: Path) -> Optional[Routine]:
    from bot_squad_worker import frontmatter as fm

    try:
        parsed = fm.parse_or_none(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not parsed:
        return None
    meta, body = parsed
    meta = meta or {}
    rid = str(meta.get("id", "")).strip()
    if not rid:
        return None
    # the instruction is the body sans the "## Instruction" heading
    instruction = body
    m = re.match(r"\s*##\s+Instruction\s*\n+", body, flags=re.IGNORECASE)
    if m:
        instruction = body[m.end():]
    return Routine(
        id=rid,
        title=str(meta.get("title", "") or ""),
        instruction=instruction.strip(),
        trigger_type=str(meta.get("trigger", "schedule") or "schedule"),
        schedule=str(meta.get("schedule", "") or ""),
        status=str(meta.get("status", ACTIVE) or ACTIVE).strip().lower(),
        created=meta.get("created"),
        last_run_at=meta.get("last_run_at"),
        next_run_at=meta.get("next_run_at"),
        file_path=path,
    )


def _routine_path(cfg: Any, slug: str, rid: str) -> Optional[Path]:
    matches = sorted(routines_dir(cfg, slug).glob(f"{rid}-*.md"))
    return matches[0] if matches else None


def load(cfg: Any, slug: str, rid: str) -> Optional[Routine]:
    path = _routine_path(cfg, slug, rid)
    if path is None:
        return None
    return _routine_from_md(path)


def list_routines(cfg: Any, slug: str) -> list[dict]:
    d = routines_dir(cfg, slug)
    if not d.exists():
        return []
    out: list[dict] = []
    for md in sorted(d.glob("*.md")):
        r = _routine_from_md(md)
        if r is not None:
            out.append(r.to_summary())
    return out


def _rewrite_meta(path: Path, updates: dict) -> None:
    """Atomically apply ``updates`` to a routine md's frontmatter, body intact."""
    from bot_squad_worker import frontmatter as fm

    meta, body = fm.parse(path.read_text(encoding="utf-8"))
    meta.update(updates)
    _atomic_write(path, fm.dump(meta, body))


def _write_run_times(cfg: Any, slug: str, rid: str, *,
                     last_run_at: Optional[str], next_run_at: Optional[str]) -> None:
    path = _routine_path(cfg, slug, rid)
    if path is None:
        raise RoutineError(f"routine not found: {rid}")
    _rewrite_meta(path, {"last_run_at": last_run_at, "next_run_at": next_run_at})


def _set_status(cfg: Any, slug: str, rid: str, status: str) -> None:
    status = (status or "").strip().lower()
    if status not in _VALID_STATUSES:
        raise RoutineError(f"invalid routine status {status!r}")
    path = _routine_path(cfg, slug, rid)
    if path is None:
        raise RoutineError(f"routine not found: {rid}")
    _rewrite_meta(path, {"status": status})


# ---------------------------------------------------------------------------
# Spawn brief — the assignment manifested into the spawned session.
# ---------------------------------------------------------------------------

def spawn_brief(cfg: Any, slug: str, routine: Routine) -> str:
    """Compose the initial prompt that BINDS the spawned session to the routine
    assignment: the 4 assignment primitives (T-0463) delivered into the session —
    (a) how-to, (b) id, (c) the instruction text, (d) how to write the result."""
    from bot_squad_worker.assignment import for_routine

    asg = for_routine(cfg.data_dir, slug, routine.id)
    return "\n".join([
        asg.how_to_prompt,
        "",
        f"Assignment: {asg.assignment_id} (routine — fired by its schedule)",
        "",
        "INSTRUCTION:",
        routine.instruction,
        "",
        "When you have a result, write it back so it survives this session:",
        f'  bsq assignment-write-result --kind routine --id {routine.id} "<your result>"',
    ])


# ---------------------------------------------------------------------------
# Tick — fire due routines, exactly one session per due tick.
# ---------------------------------------------------------------------------

def _spawn_for_routine(cfg: Any, slug: str, routine: Routine) -> Optional[str]:
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError

    try:
        res = S.spawn(cfg, slug, "dev",
                      initial_prompt=spawn_brief(cfg, slug, routine),
                      owner=f"routine:{routine.id}")
        return res.get("sid")
    except ActionError as e:
        # Parallel-session cap / quota backpressure is normal — defer quietly;
        # next_run_at is NOT advanced (below), so a later tick retries.
        if "capacity reached" in str(e):
            log.debug("routine %s spawn deferred for %s (%s)", routine.id, slug, e)
        else:
            log.exception("routine %s spawn failed for %s", routine.id, slug)
        return None
    except Exception:  # noqa: BLE001 — one bad routine never kills the sweep
        log.exception("routine %s spawn failed for %s", routine.id, slug)
        return None


def tick(cfg: Any, slug: str, *, now: Optional[datetime] = None) -> dict:
    """One routine-firing pass for a single project.

    Fires every DUE routine exactly once: spawn a session bound to the routine
    assignment, then advance ``next_run_at`` strictly past ``now`` (and stamp
    ``last_run_at``) so the same due-tick can't re-spawn. A spawn deferred under
    capacity backpressure leaves ``next_run_at`` unchanged so a later tick
    retries. Returns ``{checked, spawned, fired:[ids]}``.
    """
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    fired: list[str] = []
    checked = 0
    for summary in list_routines(cfg, slug):
        checked += 1
        routine = load(cfg, slug, summary["id"])
        if routine is None or not routine.is_due(now):
            continue
        sid = _spawn_for_routine(cfg, slug, routine)
        if not sid:
            continue  # deferred — retry next tick, next_run_at untouched
        nxt = routine.trigger().next_fire(now, inclusive=False)
        _write_run_times(cfg, slug, routine.id,
                         last_run_at=_iso(now), next_run_at=_iso(nxt))
        fired.append(routine.id)
        log.info("routine fired: %s -> %s (next %s) [%s]",
                 routine.id, sid, _iso(nxt), slug)

    return {"checked": checked, "spawned": len(fired), "fired": fired}


def routine_tick(cfg: Any) -> None:
    """Scheduler entry point (T-0464): one routine-firing pass per project.

    Per-project errors are caught + logged so one bad project never kills the
    sweep — same contract as the sibling lifecycle ticks. Wired on the 60s
    cadence in ``scheduler.py``. Kill switch: ``BOT_SQUAD_ROUTINES=0``.
    """
    raw = os.environ.get("BOT_SQUAD_ROUTINES")
    if raw is not None and raw.strip().lower() in ("0", "false", "no", "off", ""):
        return
    for slug in cfg.projects:
        try:
            tick(cfg, slug)
        except Exception:  # noqa: BLE001
            log.exception("routine_tick: unhandled error for project %s", slug)
