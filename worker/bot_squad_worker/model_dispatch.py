"""T-0909 — the fleet's model-dispatch ledger, and the compliance number read off it.

WHY A LEDGER AND NOT THE SESSION MDs (measured, 2026-08-18/19). The first pass on
this ticket counted ``model_source: explicit`` across ``data/*/sessions/*.md`` and
read **zero**, concluding the operator had never once exercised the per-ticket
model choice T-0866 asks for. That instrument is blind: session mds are DELETED
when a session is reaped, so it only ever sees the handful of sessions alive at
the moment of the grep. The worker journal — which carries the ``spawn … model=X
(source)`` line T-0871 added — says the opposite over the same window:

    104  role=dev model=opus   (explicit)     <- an operator TYPED --model opus
     92  role=dev model=opus   (config:dev)
     35  role=operator opus    (config:operator)
      2  role=dev model=sonnet (explicit)

So the mechanism is not unused; it is used ~50x more often to PIN the premium
model than to step down to Sonnet, and one dev spawn in 198 ran Sonnet over the
eight days since T-0866 shipped the policy. The journal is the right reading and
the wrong home for it: it is rotated by systemd, is not readable without the
right privileges, and forces every consumer to parse log prose.

This module is that reading, written where it can be read cheaply and does not
rotate. Every claude spawn/resume appends ONE json line here, and
:func:`compliance` turns the tail into the number an operator (and this ticket's
verification step) is measured against.

DELIBERATELY RECORDS AUTOMATED DISPATCHES TOO (routines, autopilot, the API
spawn, the operator re-drive). They spend the same money. A compliance number
that silently excluded the callers with no human in the loop would report a
share of a corpus the operator does not control, which is the reporting failure
this ticket exists to correct — the record carries ``source`` so a reader can
separate "an agent chose this" from "a config default chose this".
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: Model CLASSES that cost more than Sonnet per token. A DELIBERATE MIRROR of
#: ``fleet_model._EXPLICIT_ID_CLASS`` / ``CLASS_ALIASES`` rather than an import,
#: because the identical table has to exist in ``scripts/cli/bsq`` (which never
#: imports the worker package) and a three-way drift is caught by a test that
#: compares all three. Fable is here on price, NOT on availability: it is the
#: TOP tier ($10/$50 per Mtok), so a spawn that reaches for it needs the same
#: stated reason an Opus spawn does. ``fleet_model.UNAVAILABLE_CLASSES`` blocks
#: it today for an unrelated reason (no purchased credits) and that gate can be
#: lifted without quietly making Fable the cheap default here.
PREMIUM_CLASSES = frozenset({"opus", "fable"})

#: The step-down classes the policy prefers (T-0866, stakeholder «надо почаще
#: юзать соннет для простых задач»).
ECONOMY_CLASSES = frozenset({"sonnet", "haiku"})

#: Keep the ledger bounded. Pruned to this many lines whenever it grows past
#: twice the cap, so the common append path never rewrites the file.
MAX_RECORDS = 5000

_FIELDS = (
    "ts", "kind", "slug", "sid", "window", "task_id", "role", "provider",
    "model", "model_class", "source", "effort", "effort_source", "reason",
    "dispatched_by",
)


def model_class(value: str | None) -> str:
    """The pricing CLASS of a model value — bare alias or pinned id.

    Substring matching on purpose: ``claude-opus-4-8``, ``opus[1m]`` and a
    future ``claude-opus-6`` all have to read as ``opus`` without this table
    being re-edited on every release. An unrecognised value returns ``""`` and
    is counted as neither premium nor economy rather than being guessed into
    one — see [[feedback_explicit_unknown_not_silent_none]]: an absent class
    must not read as a real negative.
    """
    v = (value or "").strip().lower()
    if not v:
        return ""
    for cls in ("sonnet", "opus", "fable", "haiku"):
        if cls in v:
            return cls
    return ""


def is_premium(value: str | None) -> bool:
    """True when ``value`` names a model class priced above Sonnet."""
    return model_class(value) in PREMIUM_CLASSES


def ledger_path(data_dir: Any) -> Path:
    """The fleet-wide (NOT per-project) ledger file.

    Fleet-wide because the bill is: the stakeholder's ask is about what the
    whole fleet spawns, and per-project files would let one project's drift
    hide behind another's compliance.
    """
    return Path(data_dir) / "_worker" / "model_dispatch.ndjson"


def record(
    data_dir: Any,
    *,
    kind: str,
    slug: str = "",
    sid: str = "",
    window: str = "",
    task_id: str = "",
    role: str = "",
    provider: str = "",
    model: str = "",
    source: str = "",
    effort: str = "",
    effort_source: str = "",
    reason: str = "",
    dispatched_by: str = "",
) -> None:
    """Append one dispatch record. NEVER raises into a spawn.

    A ledger write that could fail a spawn would be a cost-control feature that
    takes the fleet down; every error here is swallowed. The caller's own
    ``log.info`` line remains the redundant second copy.
    """
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "slug": slug or "",
            "sid": sid or "",
            "window": window or "",
            "task_id": task_id or "",
            "role": role or "",
            "provider": provider or "",
            "model": model or "",
            "model_class": model_class(model),
            "source": source or "",
            "effort": effort or "",
            "effort_source": effort_source or "",
            "reason": (reason or "").strip()[:500],
            "dispatched_by": dispatched_by or "",
        }
        path = ledger_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        # O_APPEND + a single write: concurrent per-user workers interleave
        # whole lines rather than corrupting one.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        _prune(path)
    except Exception:  # pragma: no cover - defensive, see docstring
        pass


def _prune(path: Path) -> None:
    """Trim to :data:`MAX_RECORDS` once the file passes twice the cap."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= MAX_RECORDS * 2:
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(lines[-MAX_RECORDS:]), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # pragma: no cover - defensive
        pass


def read(data_dir: Any, *, limit: int | None = None) -> list[dict]:
    """The ledger, oldest-first. A malformed line is skipped, not fatal."""
    path = ledger_path(data_dir)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    if limit is not None and limit > 0:
        return out[-limit:]
    return out


def compliance(
    data_dir: Any,
    *,
    limit: int = 50,
    role: str = "dev",
    kinds: Iterable[str] = ("spawn",),
    slug: str = "",
) -> dict:
    """The number the operator is measured against: what the last N dispatches ran.

    Returns ``{n, economy, premium, unknown, economy_pct, premium_reasons,
    by_source, by_producer, roles, oldest_ts, newest_ts}``. ``n == 0`` is reported as such —
    an empty ledger must not render as 100% or 0% compliance, because both read
    as a measurement when there is none.
    """
    wanted_kinds = {k for k in kinds if k}
    recs = [
        r for r in read(data_dir)
        if (not wanted_kinds or r.get("kind") in wanted_kinds)
        and (not role or r.get("role") == role)
        and (not slug or r.get("slug") == slug)
    ]
    recs = recs[-limit:] if limit and limit > 0 else recs
    economy = premium = unknown = 0
    reasons: list[dict] = []
    by_source: dict[str, int] = {}
    by_producer: dict[str, int] = {}
    for r in recs:
        cls = r.get("model_class") or model_class(r.get("model"))
        if cls in PREMIUM_CLASSES:
            premium += 1
            reasons.append({
                "ts": r.get("ts", ""),
                "task_id": r.get("task_id", ""),
                "window": r.get("window", ""),
                "model": r.get("model", ""),
                "reason": r.get("reason", ""),
                "source": r.get("source", ""),
            })
        elif cls in ECONOMY_CLASSES:
            economy += 1
        else:
            unknown += 1
        src = r.get("source") or "-"
        by_source[src] = by_source.get(src, 0) + 1
        # T-0909 follow-up: WHICH surface asked. `source` says whether a model
        # was chosen or defaulted; this says who did the asking, which is the
        # question "why is the default still winning" actually turns on — 87 of
        # 123 role-default dev spawns turned out to be relaunches, not
        # dispatches. An unlabelled producer reads as "unattributed", never as
        # one of the named ones.
        by_producer[r.get("dispatched_by") or "unattributed"] = (
            by_producer.get(r.get("dispatched_by") or "unattributed", 0) + 1)
    n = len(recs)
    return {
        "n": n,
        "economy": economy,
        "premium": premium,
        "unknown": unknown,
        # None, not 0.0, when there is nothing to divide by.
        "economy_pct": (round(100.0 * economy / n, 1) if n else None),
        "premium_reasons": reasons,
        "by_source": by_source,
        "by_producer": by_producer,
        "roles": role,
        "oldest_ts": recs[0].get("ts", "") if recs else "",
        "newest_ts": recs[-1].get("ts", "") if recs else "",
    }


def summary_line(comp: dict) -> str:
    """One line for a session-start banner / CLI footer.

    Says "no measurement" out loud when the ledger is empty rather than
    printing a percentage computed from nothing.
    """
    n = comp.get("n") or 0
    if not n:
        return "model mix: no dispatches recorded yet (ledger empty)"
    pct = comp.get("economy_pct")
    return (
        f"model mix: {comp.get('economy', 0)}/{n} of the last {n} "
        f"{comp.get('roles') or 'dev'} spawns ran a step-down model "
        f"({pct}% sonnet-class), {comp.get('premium', 0)} premium"
    )
