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
import json
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

@dataclass
class FireEvent:
    """An event-worthy transition surfaced by :meth:`Trigger.poll`.

    ``kind`` is one of ``fire`` (attach AI / count as a firing), ``recover``
    (a previously-FIRED breach cleared) or ``monitor_broken`` (the probe
    itself has failed ``MONITOR_ERROR_BOUND`` times in a row — the monitor
    can't see, which is a fact worth surfacing but NOT a breach).
    """

    kind: str
    value: Any = None
    threshold: Any = None
    judge: Optional[str] = None
    breach_first_seen: Optional[str] = None


class Trigger(abc.ABC):
    """A rule for WHEN a routine fires. Schedule + monitor today; events later."""

    #: short type tag persisted on the routine ("schedule", "monitor", ...)
    type: str = "trigger"

    @abc.abstractmethod
    def next_fire(self, after: datetime, *, inclusive: bool) -> Optional[datetime]:
        """The next time this trigger fires relative to ``after``.

        ``inclusive=True`` may return ``after`` itself if it matches (used at
        declare time to seed the first ``next_run_at``); ``inclusive=False``
        returns a time STRICTLY after ``after`` (used after a fire so the same
        due-tick never re-fires). ``None`` means "never again" — a monitor
        trigger always returns ``None`` (its fires are state-driven, not
        time-scheduled).
        """

    @abc.abstractmethod
    def poll(self, now: datetime, ctx: dict) -> Optional[FireEvent]:
        """Uniform firing seam (D-0048 §2): evaluate this trigger against
        ``ctx`` and return an event-worthy transition, or ``None``.

        For a schedule, ``ctx`` carries ``next_run_at`` and poll is the
        due-check. For a monitor, ``ctx`` carries the sidecar ``state`` dict
        (mutated in place; the caller persists it) and the latest ``probe``
        result. Event triggers slot into this same seam later with zero
        downstream rework.
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

    def poll(self, now: datetime, ctx: dict) -> Optional[FireEvent]:
        """Due-check as a poll: fire when ``ctx['next_run_at']`` is reached."""
        nxt = _parse_iso(ctx.get("next_run_at"))
        if nxt is None:
            return None
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return FireEvent(kind="fire") if now >= nxt else None


# --- Monitor trigger (D-0048): code probe -> judge -> persist/cooldown ------

#: probe stdout is hard-capped at this many chars — a chatty probe can't blow
#: up sidecar state or the judge (D-0048 §4 "output cap 8KB").
MONITOR_OUTPUT_CAP = 8192

#: consecutive probe errors before the monitor self-alerts as broken, ONCE
#: (D-0048 §3.2 — a monitor that can't see is a fact worth surfacing, but it
#: must not fire the AI process with garbage).
MONITOR_ERROR_BOUND = 10

#: monitor value stored in sidecar state / events is clipped to this length
#: (only regex judges carry text values; numeric judges store the number).
_MONITOR_VALUE_CLIP = 500

_MONITOR_NUMERIC_JUDGES = frozenset({"numeric_gt", "numeric_lt", "numeric_ne"})
_MONITOR_JUDGES = _MONITOR_NUMERIC_JUDGES | {"nonzero_exit", "regex_match"}
_MONITOR_SPEC_KEYS = frozenset({
    "probe", "cmd", "interval_s", "timeout_s", "judge", "threshold",
    "persist_s", "cooldown_s", "on_breach", "on_recover",
    "url", "expect_status", "latency_budget_ms",
})
_HTTP_ONLY_KEYS = frozenset({"url", "expect_status", "latency_budget_ms"})


@dataclass
class ProbeResult:
    """Outcome of one probe subprocess run.

    ``ok=False`` means the probe did not run to completion (timeout / exec
    error, reason in ``error``) — never a breach, always the error path.
    """

    ok: bool
    exit_code: int
    output: str
    error: str = ""


def _spec_int(spec: dict, key: str, *, default: Optional[int] = None,
              minimum: int = 0) -> int:
    raw = spec.get(key, default)
    if raw is None:
        raise RoutineError(f"monitor spec: {key} is mandatory")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise RoutineError(f"monitor spec: {key} must be an integer, got {raw!r}")
    if val < minimum:
        raise RoutineError(f"monitor spec: {key} must be >= {minimum}, got {val}")
    return val


class MonitorTrigger(Trigger):
    """A code-watched metric threshold (D-0048 §2): probe → judge → persist/
    cooldown state machine. Zero AI, zero tokens until a confirmed breach.

    ``spec`` is the ``monitor:`` frontmatter block (§3.1). Validation happens
    HERE so a bad probe/judge/threshold combination raises at declare time,
    before any write. The normalized spec is exposed as ``self.spec`` (what
    :func:`declare` persists).
    """

    type = "monitor"

    def __init__(self, spec: Any):
        if not isinstance(spec, dict):
            raise RoutineError(
                f"monitor spec must be a mapping, got {type(spec).__name__}")
        unknown = set(spec) - _MONITOR_SPEC_KEYS
        if unknown:
            raise RoutineError(
                f"monitor spec: unknown key(s) {sorted(unknown)} "
                f"(supported: {sorted(_MONITOR_SPEC_KEYS)})")

        probe = str(spec.get("probe") or "shell").strip().lower()
        if probe not in ("shell", "http"):
            raise RoutineError(
                f"monitor spec: unsupported probe {probe!r} "
                "(supported: 'shell', 'http')")

        interval_s = _spec_int(spec, "interval_s", default=30, minimum=5)
        timeout_s = _spec_int(spec, "timeout_s", minimum=1)  # mandatory
        persist_s = _spec_int(spec, "persist_s", default=0, minimum=0)
        cooldown_s = _spec_int(spec, "cooldown_s", default=0, minimum=0)

        if probe == "http":
            judge, threshold, http_fields = self._validate_http(spec)
        else:
            http_only = set(spec) & _HTTP_ONLY_KEYS
            if http_only:
                raise RoutineError(
                    f"monitor spec: {sorted(http_only)} are http-probe keys; "
                    "a shell probe takes cmd + judge/threshold")
            http_fields = {}
            cmd = str(spec.get("cmd") or "").strip()
            if not cmd:
                raise RoutineError("monitor spec: empty cmd")

            judge = str(spec.get("judge") or "").strip().lower()
            if judge not in _MONITOR_JUDGES:
                raise RoutineError(
                    f"monitor spec: unknown judge {judge!r} "
                    f"(supported: {sorted(_MONITOR_JUDGES)})")
            threshold = spec.get("threshold")
            if judge in _MONITOR_NUMERIC_JUDGES:
                try:
                    float(threshold)
                except (TypeError, ValueError):
                    raise RoutineError(
                        f"monitor spec: judge {judge} needs a numeric threshold, "
                        f"got {threshold!r}")
            elif judge == "regex_match":
                if not threshold or not str(threshold).strip():
                    raise RoutineError(
                        "monitor spec: judge regex_match needs a regex threshold")
                try:
                    re.compile(str(threshold))
                except re.error as e:
                    raise RoutineError(
                        f"monitor spec: invalid regex threshold {threshold!r}: {e}")

        on_breach = str(spec.get("on_breach") or "spawn").strip().lower()
        if on_breach not in ("spawn", "notify"):
            raise RoutineError(
                f"monitor spec: unsupported on_breach {on_breach!r} "
                "(supported: 'spawn' = attach AI, 'notify' = code-only TG alert)")
        on_recover = str(spec.get("on_recover") or "").strip().lower()
        if on_recover not in ("", "notify"):
            raise RoutineError(
                f"monitor spec: unsupported on_recover {on_recover!r} "
                "(supported: 'notify' or omit)")

        self.spec: dict = {
            "probe": probe,
            "interval_s": interval_s,
            "timeout_s": timeout_s,
            "judge": judge,
            "persist_s": persist_s,
            "cooldown_s": cooldown_s,
            "on_breach": on_breach,
            **http_fields,
        }
        if probe == "shell":
            self.spec["cmd"] = cmd
        if threshold is not None:
            self.spec["threshold"] = threshold
        if on_recover:
            self.spec["on_recover"] = on_recover

    @staticmethod
    def _validate_http(spec: dict) -> tuple[str, str, dict]:
        """Validate the http-probe keys (D-0048 §3.1: url + expect_status /
        latency budget). An http probe judges ITSELF — the breach condition is
        derived from expect_status/latency_budget_ms, so ``judge`` is the
        internal ``http`` and ``threshold`` is the derived expectation string
        (both re-accepted on reload so the persisted normalized spec round-trips
        through this validation).

        Returns ``(judge, threshold, http_fields)``.
        """
        if spec.get("cmd"):
            raise RoutineError(
                "monitor spec: http probe takes url, not cmd")
        url = str(spec.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise RoutineError(
                f"monitor spec: http probe needs an http(s):// url, got {url!r}")
        expect_status = _spec_int(spec, "expect_status", default=200, minimum=100)
        if expect_status > 599:
            raise RoutineError(
                f"monitor spec: expect_status must be a 100..599 HTTP status, "
                f"got {expect_status}")
        latency_ms: Optional[int] = None
        if spec.get("latency_budget_ms") is not None:
            latency_ms = _spec_int(spec, "latency_budget_ms", minimum=1)

        parts = [f"status=={expect_status}"]
        if latency_ms is not None:
            parts.append(f"latency<={latency_ms}ms")
        threshold = " & ".join(parts)

        judge = str(spec.get("judge") or "http").strip().lower()
        if judge != "http":
            raise RoutineError(
                "monitor spec: an http probe judges itself via "
                "expect_status/latency_budget_ms — don't set judge")
        declared = spec.get("threshold")
        if declared is not None and str(declared) != threshold:
            raise RoutineError(
                "monitor spec: an http probe derives its threshold from "
                f"expect_status/latency_budget_ms ({threshold!r}) — don't set it")

        http_fields: dict = {"url": url, "expect_status": expect_status}
        if latency_ms is not None:
            http_fields["latency_budget_ms"] = latency_ms
        return judge, threshold, http_fields

    def next_fire(self, after: datetime, *, inclusive: bool) -> Optional[datetime]:
        return None  # state-driven, never time-scheduled

    def poll(self, now: datetime, ctx: dict) -> Optional[FireEvent]:
        """Feed one probe result through the judge state machine (D-0048 §4).

        Mutates ``ctx['state']`` (the sidecar dict; caller persists). Fire
        stamping (``fired`` / ``last_fired_at``) is deliberately the CALLER's
        job, done only after a successful attach — a spawn deferred under
        capacity backpressure leaves cooldown unstamped so the next tick
        retries (§5.1).
        """
        st: dict = ctx["state"]
        probe: ProbeResult = ctx["probe"]
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        st["last_probe_at"] = _iso(now)

        verdict, value = evaluate_probe(self.spec, probe)
        if verdict == "error":
            # Probe errors are NOT breaches — and not recoveries either: a
            # blind probe leaves the breach state exactly as it was.
            errors = int(st.get("consecutive_errors") or 0) + 1
            st["consecutive_errors"] = errors
            if errors == MONITOR_ERROR_BOUND:
                return FireEvent(kind="monitor_broken", value=value,
                                 threshold=self.spec.get("threshold"),
                                 judge=self.spec["judge"],
                                 breach_first_seen=st.get("breach_first_seen"))
            return None
        st["consecutive_errors"] = 0
        st["last_value"] = value

        if verdict == "breach":
            if not st.get("breach_first_seen"):
                st["breach_first_seen"] = _iso(now)
            first = _parse_iso(st["breach_first_seen"])
            if (now - first).total_seconds() < self.spec["persist_s"]:
                return None  # anti-flap: breach must hold persist_s first
            last_fired = _parse_iso(st.get("last_fired_at"))
            if last_fired is not None and (
                    (now - last_fired).total_seconds() < self.spec["cooldown_s"]):
                return None  # cooldown gaps fires — spans recovery, anti-flap
            return FireEvent(kind="fire", value=value,
                             threshold=self.spec.get("threshold"),
                             judge=self.spec["judge"],
                             breach_first_seen=st["breach_first_seen"])

        # judge passes: recovery. A ✅ is only event-worthy if the breach
        # actually FIRED (linza rule); an unfired sighting clears silently.
        event = None
        if st.get("fired"):
            event = FireEvent(kind="recover", value=value,
                              threshold=self.spec.get("threshold"),
                              judge=self.spec["judge"],
                              breach_first_seen=st.get("breach_first_seen"))
        st["breach_first_seen"] = None
        st["fired"] = False
        return event


def evaluate_probe(spec: dict, probe: ProbeResult) -> tuple[str, Any]:
    """Judge one probe result: ``("breach"|"ok"|"error", value)``.

    Only the ``nonzero_exit`` judge consumes exit codes as signal; for every
    other judge a nonzero exit means the probe itself failed and its stdout is
    untrustworthy — the error path, never a breach.
    """
    judge = spec["judge"]
    threshold = spec.get("threshold")
    if not probe.ok:
        return ("error", (probe.error or "probe failed")[:_MONITOR_VALUE_CLIP])
    if judge == "http":
        # http probes judge themselves (exit_code 0 = expectations met);
        # the value is the observation ("status=... latency_ms=..." or
        # "unreachable (...)"), not a number.
        value = probe.output.strip()[:_MONITOR_VALUE_CLIP]
        return ("breach" if probe.exit_code != 0 else "ok", value)
    if judge == "nonzero_exit":
        return ("breach" if probe.exit_code != 0 else "ok", probe.exit_code)
    if probe.exit_code != 0:
        detail = (probe.error or "").strip()[:200]
        return ("error", f"exit={probe.exit_code}" + (f": {detail}" if detail else ""))
    if judge == "regex_match":
        value = probe.output.strip()[:_MONITOR_VALUE_CLIP]
        breach = re.search(str(threshold), probe.output) is not None
        return ("breach" if breach else "ok", value)
    # numeric judges
    try:
        value = float(probe.output.strip())
    except ValueError:
        return ("error", f"non-numeric output {probe.output.strip()[:80]!r}")
    if not math.isfinite(value):
        # inf/nan parse as floats but can't be judged (int() raises, nan
        # comparisons lie) — the error path, same as non-numeric output.
        return ("error", f"non-finite output {probe.output.strip()[:80]!r}")
    if value == int(value):
        value = int(value)
    t = float(threshold)
    breach = {
        "numeric_gt": value > t,
        "numeric_lt": value < t,
        "numeric_ne": value != t,
    }[judge]
    return ("breach" if breach else "ok", value)


def make_trigger(trigger_type: str, spec: Any) -> Trigger:
    """Build a :class:`Trigger` from its declared type + spec.

    ``spec`` is the cron string for ``schedule``, the ``monitor:`` mapping for
    ``monitor``. Event triggers slot in here (one new branch + a ``Trigger``
    subclass); an unknown type is a :class:`RoutineError`.
    """
    t = (trigger_type or "").strip().lower()
    if t == "schedule":
        if not isinstance(spec, str):
            raise RoutineError("schedule trigger takes a cron string spec")
        return ScheduleTrigger(spec)
    if t == "monitor":
        return MonitorTrigger(spec)
    raise RoutineError(
        f"unknown trigger type {trigger_type!r} "
        "(supported today: 'schedule', 'monitor')"
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
    monitor: Optional[dict] = None

    def trigger(self) -> Trigger:
        if self.trigger_type == "monitor":
            return make_trigger("monitor", self.monitor)
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


def declare(cfg: Any, slug: str, *, instruction: str,
            schedule: Optional[str] = None,
            title: Optional[str] = None, trigger: str = "schedule",
            monitor: Optional[dict] = None,
            provenance: Optional[str] = None,
            now: Optional[datetime] = None) -> dict:
    """Declare + persist a new Routine. Returns ``{id, file_path, next_run_at}``.

    Validates the trigger spec (a bad cron / a bad monitor probe-judge-
    threshold combination raises :class:`RoutineError` BEFORE anything is
    written), allocates the next ``R-NNNN`` via the shared idalloc allocator,
    computes the first ``next_run_at`` (inclusive of ``now``; ``None`` for a
    monitor — its fires are state-driven), and writes the routine md
    (instruction in the body, monitor spec in the frontmatter).
    """
    if cfg.projects.get(slug) is None:
        raise RoutineError(f"unknown project slug {slug!r}")
    instruction = (instruction or "").strip()
    if not instruction:
        raise RoutineError("routine declare: empty instruction")

    trigger_t = (trigger or "schedule").strip().lower()
    schedule = (schedule or "").strip()
    if trigger_t == "monitor":
        if schedule:
            raise RoutineError(
                "routine declare: a monitor routine takes a monitor spec, not a schedule")
        if not monitor:
            raise RoutineError(
                "routine declare: trigger 'monitor' requires a monitor spec")
        trig = make_trigger("monitor", monitor)  # validates; raises before any write
    else:
        if monitor:
            raise RoutineError(
                f"routine declare: trigger {trigger_t!r} does not take a monitor spec")
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
        "created": created,
        "last_run_at": None,
        "next_run_at": _iso(next_run),
    }
    if trigger_t == "monitor":
        meta["monitor"] = trig.spec  # the normalized, validated block (§3.1)
    else:
        meta["schedule"] = schedule
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
    monitor = meta.get("monitor")
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
        monitor=monitor if isinstance(monitor, dict) else None,
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

def spawn_brief(cfg: Any, slug: str, routine: Routine,
                event: Optional[FireEvent] = None) -> str:
    """Compose the initial prompt that BINDS the spawned session to the routine
    assignment: the 4 assignment primitives (T-0463) delivered into the session —
    (a) how-to, (b) id, (c) the instruction text, (d) how to write the result.

    For a monitor firing, ``event`` adds a TRIGGER EVENT section (D-0048 §5.1)
    so the attached process starts knowing WHY it exists — probe value vs
    threshold, breach onset, fire count — with no re-probing to discover the
    state of the world.
    """
    from bot_squad_worker.assignment import for_routine

    asg = for_routine(cfg.data_dir, slug, routine.id)
    fired_by = "monitor threshold breach" if event is not None else "fired by its schedule"
    lines = [
        asg.how_to_prompt,
        "",
        f"Assignment: {asg.assignment_id} (routine — {fired_by})",
    ]
    if event is not None:
        art_path = cfg.data_dir / slug / "artifacts" / f"{routine.id}.md"
        art_note = (str(art_path) if art_path.exists()
                    else "none yet (this firing produces the first)")
        # this fire is not in events.ndjson yet (appended after a successful
        # spawn), hence the +1
        count = _fire_count_24h(cfg, slug, routine.id, now=_utcnow()) + 1
        lines += [
            "",
            "TRIGGER EVENT — the monitor firing that attached this session:",
            f"  value: {event.value} | judge: {event.judge} | threshold: {event.threshold}",
            f"  breach first seen: {event.breach_first_seen}",
            f"  fires in last 24h (incl. this one): {count}",
            f"  last result artifact: {art_note}",
        ]
    lines += [
        "",
        "INSTRUCTION:",
        routine.instruction,
        "",
        "When you have a result, write it back so it survives this session:",
        f'  bsq assignment-write-result --kind routine --id {routine.id} "<your result>"',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tick — fire due routines, exactly one session per due tick.
# ---------------------------------------------------------------------------

def _spawn_for_routine(cfg: Any, slug: str, routine: Routine,
                       event: Optional[FireEvent] = None) -> Optional[str]:
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError

    try:
        res = S.spawn(cfg, slug, "dev",
                      initial_prompt=spawn_brief(cfg, slug, routine, event),
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
        if routine is None or routine.trigger_type != "schedule":
            continue  # monitor routines belong to monitor_tick (D-0048 §4)
        if not routine.is_due(now):
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


# ---------------------------------------------------------------------------
# Monitor engine (D-0048 / T-0603) — sidecar state, fire history, 5s sweep.
# ---------------------------------------------------------------------------

def state_dir(cfg: Any, slug: str) -> Path:
    return routines_dir(cfg, slug) / "state"


def state_path(cfg: Any, slug: str, rid: str) -> Path:
    return state_dir(cfg, slug) / f"{rid}.json"


def _default_state() -> dict:
    return {
        "last_probe_at": None,
        "last_value": None,
        "breach_first_seen": None,
        "fired": False,
        "last_fired_at": None,
        "consecutive_errors": 0,
    }


def load_state(cfg: Any, slug: str, rid: str) -> dict:
    """Load a monitor's sidecar runtime state (D-0048 §3.2).

    State is DISPOSABLE: absent or corrupt state re-seeds as "no breach
    observed" — worst case one extra persist window before a fire, the safe
    direction.
    """
    st = _default_state()
    try:
        raw = json.loads(state_path(cfg, slug, rid).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            st.update(raw)
    except (OSError, ValueError):
        pass
    return st


def save_state(cfg: Any, slug: str, rid: str, state: dict) -> None:
    _atomic_write(state_path(cfg, slug, rid),
                  json.dumps(state, ensure_ascii=False, sort_keys=True))


def events_path(cfg: Any, slug: str) -> Path:
    return routines_dir(cfg, slug) / "events.ndjson"


def append_event(cfg: Any, slug: str, *, ts: str, routine: str, kind: str,
                 value: Any = None, threshold: Any = None,
                 sid: Optional[str] = None, note: Optional[str] = None) -> None:
    """Append one fire/recover/monitor_broken line (D-0048 §3.3) — the
    observability contract the UI layer (T-0587 lineage) reads later."""
    rec = {"ts": ts, "routine": routine, "kind": kind, "value": value,
           "threshold": threshold, "sid": sid, "note": note}
    path = events_path(cfg, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _fire_count_24h(cfg: Any, slug: str, rid: str, *, now: datetime) -> int:
    floor = now - timedelta(hours=24)
    count = 0
    try:
        with open(events_path(cfg, slug), encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if rec.get("routine") != rid or rec.get("kind") != "fire":
                    continue
                ts = _parse_iso(rec.get("ts"))
                if ts is not None and ts >= floor:
                    count += 1
    except OSError:
        return 0
    return count


def _kill_probe_group(proc: subprocess.Popen) -> None:
    """SIGKILL the probe's whole process group and reap without touching the
    pipes: a grandchild holding stdout would make communicate() block, but
    wait() only reaps the (dead) shell. Pipes are closed explicitly so an
    escapee that setsid'd out of the group can't leak our fds."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _run_http_probe(spec: dict) -> ProbeResult:
    """Run one http probe (D-0048 §3.1): GET ``url``, judge status + latency.

    ``exit_code`` 0 = expectations met, 1 = breach (status mismatch, latency
    over budget) — the ``http`` judge consumes it. An UNREACHABLE endpoint
    (connect error / timeout) is a BREACH observation, not a probe error: for
    a health probe "connection refused" is the paradigmatic outage, and the
    consecutive_errors path would stay silent for 10 intervals and then phrase
    it as a broken monitor. The error path is reserved for the probe machinery
    itself failing.
    """
    import httpx  # lazy import — same idiom as tg.py

    url = spec["url"]
    timeout_s = spec["timeout_s"]
    start = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout_s)
    except httpx.HTTPError as e:
        reason = f"{type(e).__name__}: {e}".strip()[:200]
        return ProbeResult(ok=True, exit_code=1,
                           output=f"unreachable ({reason})", error=reason)
    except Exception as e:  # noqa: BLE001 — a probe can never kill the sweep
        return ProbeResult(ok=False, exit_code=-1, output="",
                           error=str(e)[:200])
    latency_ms = int((time.monotonic() - start) * 1000)
    status = resp.status_code

    failures: list[str] = []
    if status != spec["expect_status"]:
        failures.append(f"status {status} != {spec['expect_status']}")
    budget = spec.get("latency_budget_ms")
    if budget is not None and latency_ms > budget:
        failures.append(f"latency {latency_ms}ms > {budget}ms")
    return ProbeResult(ok=True, exit_code=1 if failures else 0,
                       output=f"status={status} latency_ms={latency_ms}",
                       error="; ".join(failures)[:500])


def _run_probe(spec: dict) -> ProbeResult:
    """Run one probe: shell subprocess or http request, per ``spec['probe']``."""
    if spec.get("probe") == "http":
        return _run_http_probe(spec)
    return _run_shell_probe(spec)


def _run_shell_probe(spec: dict) -> ProbeResult:
    """Run one shell probe subprocess: hard timeout, output capped at 8KB.

    The probe leads its own process group (``start_new_session=True``, the
    deploy.py T-0212 idiom) so the timeout kill reaches shell grandchildren
    too. Killing only the shell would leave a backgrounded child holding the
    stdout pipe, communicate() would block unbounded, and the max_instances=1
    monitor job would wedge EVERY monitor until a worker restart.
    """
    try:
        p = subprocess.Popen(
            spec["cmd"], shell=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,  # own process group → killpg reaches children
        )
    except Exception as e:  # noqa: BLE001 — a probe can never kill the sweep
        return ProbeResult(ok=False, exit_code=-1, output="",
                           error=str(e)[:200])
    try:
        out, err = p.communicate(timeout=spec["timeout_s"])
    except subprocess.TimeoutExpired:
        _kill_probe_group(p)
        return ProbeResult(ok=False, exit_code=-1, output="",
                           error=f"timeout after {spec['timeout_s']}s")
    except Exception as e:  # noqa: BLE001
        _kill_probe_group(p)
        return ProbeResult(ok=False, exit_code=-1, output="",
                           error=str(e)[:200])
    return ProbeResult(ok=True, exit_code=p.returncode,
                       output=(out or "")[:MONITOR_OUTPUT_CAP],
                       error=(err or "")[:500])


# Registry cache: the 5s tick must not re-read every routine md. Keyed by the
# routines dir path, gated on its mtime — declare/pause/fire all rewrite an md
# via os.replace, which bumps the dir mtime. A dir changed within the last 2s
# is re-scanned unconditionally (filesystem mtime granularity guard).
_REGISTRY_CACHE: dict[str, tuple[int, list[Routine]]] = {}
_REGISTRY_QUIET_S = 2.0

#: single-flight guard — a routine whose probe is still in flight is skipped
#: (linza's flock). Keys are ``(slug, rid)``.
_INFLIGHT: set[tuple[str, str]] = set()
_INFLIGHT_LOCK = threading.Lock()

#: at most this many probe subprocesses in flight per project (D-0048 §4) —
#: over-budget due monitors just wait for the next tick, 5s later.
MONITOR_PROBE_CONCURRENCY = 8


def _monitor_routines(cfg: Any, slug: str) -> list[Routine]:
    d = routines_dir(cfg, slug)
    try:
        stat = d.stat()
    except OSError:
        return []
    key = str(d)
    cached = _REGISTRY_CACHE.get(key)
    quiet = (time.time() - stat.st_mtime) > _REGISTRY_QUIET_S
    if cached is not None and cached[0] == stat.st_mtime_ns and quiet:
        return cached[1]
    out: list[Routine] = []
    for md in sorted(d.glob("*.md")):
        r = _routine_from_md(md)
        if r is not None and r.trigger_type == "monitor":
            out.append(r)
    _REGISTRY_CACHE[key] = (stat.st_mtime_ns, out)
    return out


def _live_routine_session(cfg: Any, slug: str, rid: str) -> Optional[str]:
    """SID of a live (pane-backed) session already owning ``routine:<rid>``,
    else ``None``. Unavailable session state fails toward spawning — attaching
    AI to a confirmed breach beats suppressing it on a broken lookup."""
    from bot_squad_worker import sessions as S

    try:
        rows = S.list_sessions(cfg, slug)
    except Exception:  # noqa: BLE001
        log.exception("monitor owner-dedup: list_sessions failed for %s", slug)
        return None
    owner = f"routine:{rid}"
    for row in rows:
        if (row.get("owner") == owner
                and row.get("status") in ("active", "paused")
                and not row.get("archived")):
            return row.get("sid")
    return None


def _monitor_notify(cfg: Any, slug: str, rid: str, text: str) -> bool:
    """Code-only monitor alert to the stakeholder (D-0048 §6) — zero AI.

    Routes through the ``_send_stakeholder_dm`` SSOT (MAX-primary on this
    DPI-blocked host, TG failover; a raw TgClient here would trip the T-0394
    notify-SSOT guard). ALWAYS ``urgent=True``: the quiet-hours gate silently
    drops non-urgent sends 17-05 UTC, and a threshold breach — or a monitor
    that went blind — is by definition urgent. Returns False when delivery
    raised (callers may retry next tick); never raises.
    """
    try:
        from bot_squad_worker.actions import _send_stakeholder_dm

        project = cfg.projects.get(slug)
        chat_id = getattr(project, "tg_chat", "") if project else ""
        res = _send_stakeholder_dm(cfg, message=text, sid=f"routine:{rid}",
                                   urgent=True, tg_chat_id=chat_id)
        # T-0610: the SSOT no longer raises on an undeliverable page — it
        # returns {ok: False, channel: "none"}. Treat that as not-delivered so
        # the cooldown stays unstamped and the alert retries next tick.
        return bool(res.get("ok"))
    except Exception:  # noqa: BLE001 — an alert channel outage never kills the sweep
        log.exception("monitor %s: notify delivery failed [%s]", rid, slug)
        return False


def _notify_breach(cfg: Any, slug: str, routine: Routine, event: FireEvent,
                   now: datetime) -> bool:
    """``on_breach: notify`` — deliver the code-only breach alert (no spawn).

    Returns True when delivered — only then does the caller stamp
    fired/cooldown; a failed delivery retries next tick, exactly like a
    capacity-deferred spawn.
    """
    text = (f"🔴 monitor {routine.id} ({routine.title}) breach: "
            f"value {event.value} vs threshold {event.threshold} "
            f"(judge {event.judge}), since {event.breach_first_seen}. "
            f"on_breach=notify — no AI attached.")
    if not _monitor_notify(cfg, slug, routine.id, text):
        return False
    append_event(cfg, slug, ts=_iso(now), routine=routine.id, kind="notify",
                 value=event.value, threshold=event.threshold,
                 note="on_breach=notify: code-only alert, no AI")
    log.info("monitor breach notified: %s (value %s vs %s) [%s]",
             routine.id, event.value, event.threshold, slug)
    return True


def _handle_fire(cfg: Any, slug: str, routine: Routine, event: FireEvent,
                 state: dict, now: datetime) -> bool:
    """Attach AI for a confirmed breach: ONE process per breach (D-0048 §5.1).

    A live session already owning this routine gets a one-line nudge through
    the input mux instead of a second spawn (one-brain discipline). Returns
    True when the fire was actually delivered (spawn or nudge) — only then is
    cooldown stamped; a capacity-deferred spawn retries next tick.
    """
    live_sid = _live_routine_session(cfg, slug, routine.id)
    if live_sid:
        from bot_squad_worker import input_mux

        nudge = (f"[TRIGGER EVENT {routine.id}] monitor re-fired while you are "
                 f"attached: value {event.value} vs threshold {event.threshold} "
                 f"(judge {event.judge}), breach ongoing since "
                 f"{event.breach_first_seen}. Fold this into your current run — "
                 f"no second session was spawned.")
        input_mux.enqueue(cfg.data_dir, live_sid, nudge,
                          f"routine:{routine.id}", now=now.timestamp())
        append_event(cfg, slug, ts=_iso(now), routine=routine.id, kind="fire",
                     value=event.value, threshold=event.threshold,
                     sid=live_sid, note="owner-dedup: nudged live session")
        log.info("monitor fired: %s -> nudged live owner %s [%s]",
                 routine.id, live_sid, slug)
        return True

    sid = _spawn_for_routine(cfg, slug, routine, event=event)
    if not sid:
        return False  # deferred under backpressure — cooldown NOT stamped
    # md keeps only the slow field: the AI actually attached (§3.2)
    _write_run_times(cfg, slug, routine.id,
                     last_run_at=_iso(now), next_run_at=None)
    append_event(cfg, slug, ts=_iso(now), routine=routine.id, kind="fire",
                 value=event.value, threshold=event.threshold, sid=sid)
    log.info("monitor fired: %s -> %s (value %s vs %s) [%s]",
             routine.id, sid, event.value, event.threshold, slug)
    return True


def monitor_sweep(cfg: Any, slug: str, *, now: Optional[datetime] = None) -> dict:
    """One monitor pass for a project: probe due monitors (bounded pool),
    feed each result through its judge state machine, handle fires.

    Returns ``{checked, probed, fired: [ids]}``. One bad monitor never kills
    the sweep — same contract as every sibling tick.
    """
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    monitors = [r for r in _monitor_routines(cfg, slug) if r.status == ACTIVE]
    due: list[tuple[Routine, MonitorTrigger, dict]] = []
    claimed: list[tuple[str, str]] = []
    try:
        for r in monitors:
            try:
                trig = r.trigger()
            except RoutineError:
                log.warning("monitor %s: invalid spec on disk, skipped [%s]",
                            r.id, slug)
                continue
            st = load_state(cfg, slug, r.id)
            last = _parse_iso(st.get("last_probe_at"))
            if last is not None and (
                    (now - last).total_seconds() < trig.spec["interval_s"]):
                continue
            key = (slug, r.id)
            with _INFLIGHT_LOCK:
                if key in _INFLIGHT:
                    continue  # single-flight: probe from a prior pass still runs
                _INFLIGHT.add(key)
            claimed.append(key)
            due.append((r, trig, st))

        probes: dict[str, ProbeResult] = {}
        if due:
            workers = min(MONITOR_PROBE_CONCURRENCY, len(due))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {r.id: pool.submit(_run_probe, trig.spec)
                           for r, trig, _st in due}
            probes = {rid: fut.result() for rid, fut in futures.items()}

        fired: list[str] = []
        for r, trig, st in due:
            try:
                ev = trig.poll(now, {"state": st, "probe": probes[r.id]})
                if ev is not None:
                    if ev.kind == "fire":
                        if trig.spec.get("on_breach") == "notify":
                            delivered = _notify_breach(cfg, slug, r, ev, now)
                        else:
                            delivered = _handle_fire(cfg, slug, r, ev, st, now)
                        if delivered:
                            st["fired"] = True
                            st["last_fired_at"] = _iso(now)
                            fired.append(r.id)
                    elif ev.kind == "recover":
                        append_event(cfg, slug, ts=_iso(now), routine=r.id,
                                     kind="recover", value=ev.value,
                                     threshold=ev.threshold)
                        if trig.spec.get("on_recover") == "notify":
                            # poll emits recover ONLY for a breach that actually
                            # fired (linza rule) — best-effort: the ✅ moment
                            # doesn't recur, so a failed send is logged, not retried
                            _monitor_notify(
                                cfg, slug, r.id,
                                f"✅ monitor {r.id} ({r.title}) recovered: "
                                f"value {ev.value} back within threshold "
                                f"{ev.threshold} (breach began "
                                f"{ev.breach_first_seen}).")
                        log.info("monitor recovered: %s (value %s) [%s]",
                                 r.id, ev.value, slug)
                    elif ev.kind == "monitor_broken":
                        append_event(cfg, slug, ts=_iso(now), routine=r.id,
                                     kind="monitor_broken",
                                     threshold=ev.threshold,
                                     note=str(ev.value))
                        # once, not per-tick: poll emits this event only at
                        # exactly consecutive_errors == MONITOR_ERROR_BOUND
                        _monitor_notify(
                            cfg, slug, r.id,
                            f"⚠️ monitor {r.id} ({r.title}) is BROKEN: "
                            f"{MONITOR_ERROR_BOUND} consecutive probe errors "
                            f"(last: {ev.value}). It cannot see its metric — "
                            f"no breach/recovery alerts until the probe is fixed.")
                        log.warning(
                            "monitor %s is BROKEN: %d consecutive probe "
                            "errors (last: %s) [%s]", r.id,
                            MONITOR_ERROR_BOUND, ev.value, slug)
                save_state(cfg, slug, r.id, st)
            except Exception:  # noqa: BLE001 — one bad monitor never kills the sweep
                log.exception("monitor %s: sweep step failed [%s]", r.id, slug)
    finally:
        with _INFLIGHT_LOCK:
            for key in claimed:
                _INFLIGHT.discard(key)

    return {"checked": len(monitors), "probed": len(due), "fired": fired}


def monitor_tick(cfg: Any) -> None:
    """Scheduler entry point (D-0048 §4): the 5s monitor sweep, per project.

    Kill switch: ``BOT_SQUAD_MONITORS=0``. Wired in ``scheduler.py`` with
    ``max_instances=1`` + coalesce, same idiom as every sibling tick.
    """
    raw = os.environ.get("BOT_SQUAD_MONITORS")
    if raw is not None and raw.strip().lower() in ("0", "false", "no", "off", ""):
        return
    for slug in cfg.projects:
        try:
            monitor_sweep(cfg, slug)
        except Exception:  # noqa: BLE001
            log.exception("monitor_tick: unhandled error for project %s", slug)
