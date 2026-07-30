"""User-controlled execution pace (T-0482) — Process Paradigm M3 / F3.4.

> "The system generally is required to implement all given tasks, but the order
> and pace of implementation is controlled by user. This control might consist
> of different, modes, strategies, constraints, toggles, it doesn't matter here."
> — SOURCE-VERBATIM Part A.

This is the **config / storage / toggle** half of that control (TL-B ruling
``option-b-clean``): the SSOT for a small, user-settable pace-control config the
operator consumes. The *operator-dispatch-HONORS-the-controls* half is T-0475
(Cluster A) — it imports the read helpers here; it does not duplicate storage.

Four controls, deliberately minimal (the mechanism is "my call, cost LOW" per
the stakeholder — extend the existing caps, don't build a new engine):

* **max_in_progress** — a ceiling on how many tasks the system keeps in the
  ``in_progress`` board state at once (0 = unlimited; back-compat for a fresh /
  legacy project). Distinct from the ``[caps].max_parallel_sessions`` *process*
  cap (live claude panes) — this paces the *board*, so the user can throttle
  WIP without touching concurrency.
* **per-initiative weight + priority** — steer the *order*: which initiative the
  operator drains first / how much it favours. Stored keyed by the initiative's
  ``.md`` basename (the same form tasks carry in their ``initiative:``
  frontmatter — one canonical id form, per the .md-vs-stem normalization rule).
* **pause** — two granularities: a **global** operator pause (delegates to the
  existing :mod:`operator_redrive` flag so there is exactly ONE "operator is
  paused" SSOT) and a **per-initiative** pause (new; T-0475 skips that
  initiative's tasks while set).
* **drive modes** (T-0828 / D-0069) — the ``drive`` block: a standing,
  per-project SCOPE (which ticket statuses are in play) x STOPPING CONDITION
  (when to stop), plus an ``on_stop`` attribute. See "Drive modes" below.

Storage: one per-project JSON at ``data/<slug>/_worker/pace/pace.json`` (mirrors
the ``_worker/<feature>/`` layout :mod:`operator_redrive` uses). The global pause
is NOT stored here — it lives in operator_redrive's flag and is merged in on read
so a consumer gets one unified view.

No new worker socket action and no edit to ``actions.py`` (single-owner / closed
allowlist): the user surface is the file-based ``bsq pace`` verb, which writes
this same JSON (and the operator_redrive flag for pause) directly.

Drive modes (T-0828, design D-0069)
-----------------------------------
> "нужно более чёткое понимание для меня, какой режим драйва щас стоит, я просил
> закончить всё что в опен, но видимо это не интерпретировалось как переключить
> режим драйва" — the stakeholder, 2026-07-30T07:54:16Z.

**«щас стоит» = *is currently set*.** That is a STANDING per-project setting, not
a per-message interpretation (``dispatch.classify_drive_mode``) and not a
per-session lifecycle bit (``bsq drive on/off``). D-0069 enumerates all four
objects the word "drive" already names and rules that the standing one is THIS
module — it is the per-project pace policy T-0482 already shipped, and modes are
the settings it has never had. Hence: no new store, no new module, no new CLI
noun.

Two axes plus one attribute, all closed sets (see the constants below):

* ``scope`` — which ticket statuses are in play. A NARROWING filter over
  ``pickup.PICKUP_STATUSES``; the default ``all`` is exactly that set, i.e.
  today's behaviour.
* ``stop_when`` — ``scope_exhausted`` (default; today's implicit rule) or
  ``spend_quota`` («Потратить квоту»).
* ``on_stop`` — ``nothing`` (default) or ``alert`` (T-0800's stall message).

Two properties this module is responsible for, both of them defects he already
reported if they are got wrong:

* **Absent = today's behaviour.** A ``pace.json`` with no ``drive`` key reads as
  the defaults, so shipping this changed nothing until he set it (the
  no-op-by-construction property T-0799 shipped with).
* **Reject, never coerce.** An out-of-set value raises :class:`DriveModeError`
  on the write path and is reported as ``invalid`` (never silently swapped for
  the default) on the read path. A settings surface that quietly ignores what
  you set it to reproduces the exact defect he reported.

``source_text`` is not decoration and not a log: it is the visibility half. It
stores the words that set the mode so every surface can print *"scope:
open_reopened — set 2026-07-30 14:52 from «закончить всё что в опен»"*, which
answers "did my instruction land" directly instead of leaving him to infer it
from behaviour.

⚠ **THE TWIN.** ``api/app/routes_transparency.py`` mirrors this file's on-disk
layout in a second implementation (the api cannot import worker code — separate
deployable packages). Its ``_normalized_drive`` must return the SAME keys as
:func:`_normalized_drive` here or the UI shows a project with no drive mode set.
``api/tests/test_pace_drive_mirror.py`` goes red when one grows a field the
other lacks; ``lint.yml`` runs it on every push.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Defaults applied on read — an absent / fresh config is uncapped + running, so a
# legacy project that never set a pace control behaves exactly as before.
_DEFAULT_WEIGHT = 1.0
_DEFAULT_PRIORITY = 0

# ---------------------------------------------------------------------------
# Drive modes (T-0828 / D-0069) — the closed sets
# ---------------------------------------------------------------------------
# CLOSED sets, in the stakeholder's own bullet order. A value outside them is an
# error, never a fallback: see DriveModeError. The api mirrors these three
# tuples verbatim in routes_transparency.py — test_pace_drive_mirror.py pins it.

#: AXIS A — which ticket statuses are in play. A narrowing filter over
#: ``pickup.PICKUP_STATUSES``; ``all`` IS that set, i.e. today's behaviour.
DRIVE_SCOPES = ("open_reopened", "in_progress", "all")

#: AXIS B — when the drive stops.
DRIVE_STOP_WHEN = ("scope_exhausted", "spend_quota")

#: The attribute on axis B — what happens AT the stop (T-0800 is ``alert``).
DRIVE_ON_STOP = ("nothing", "alert")

#: Absent block == these values == today's behaviour (D-0069 "The record").
DRIVE_DEFAULTS = {
    "scope": "all",
    "stop_when": "scope_exhausted",
    "on_stop": "nothing",
}

#: field -> allowed values, for validation + for what the surfaces print.
DRIVE_CHOICES = {
    "scope": DRIVE_SCOPES,
    "stop_when": DRIVE_STOP_WHEN,
    "on_stop": DRIVE_ON_STOP,
}

#: The provenance fields, stored verbatim and never validated against a set.
DRIVE_PROVENANCE_FIELDS = ("set_by", "set_at", "source_text")


class DriveModeError(ValueError):
    """An out-of-set drive-mode value. NAMED, and raised rather than coerced.

    D-0069's read of his complaint: a settings surface that quietly ignores what
    you set it to is the defect. So ``--scope opne`` fails loudly instead of
    leaving ``all`` in place and reporting success.
    """


def validate_drive_value(field: str, value: Any) -> str:
    """Return ``value`` iff it is in ``field``'s closed set; else raise.

    :raises DriveModeError: naming the field, the rejected value and the full
        allowed set — the message is what the CLI prints, so it has to be
        actionable on its own.
    """
    choices = DRIVE_CHOICES.get(field)
    if choices is None:
        raise DriveModeError(
            f"unknown drive field {field!r} (known: {', '.join(sorted(DRIVE_CHOICES))})"
        )
    sval = str(value).strip()
    if sval not in choices:
        raise DriveModeError(
            f"invalid drive {field}: {value!r} — must be one of: {', '.join(choices)}"
        )
    return sval


def _utc_now_iso() -> str:
    """``set_at`` stamp: second-resolution UTC ISO-8601 with a ``Z``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _state_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "pace"


def _config_path(cfg: Any, slug: str) -> Path:
    return _state_dir(cfg, slug) / "pace.json"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_initiative(name: str) -> str:
    """Canonical initiative key = the ``.md`` basename form, matching the
    ``initiative:`` field tasks carry in frontmatter.

    Defeats the recurring .md-vs-stem ambiguity (a bare ``process-paradigm`` and
    ``process-paradigm.md`` must address the SAME initiative). Strips any path
    and a single trailing ``.md``, then re-appends ``.md`` exactly once.
    """
    base = Path(str(name).strip()).name
    if base.lower().endswith(".md"):
        base = base[:-3]
    return f"{base}.md" if base else ""


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1", "on")


# ---------------------------------------------------------------------------
# Raw load / save
# ---------------------------------------------------------------------------

def _load_raw(cfg: Any, slug: str) -> dict:
    """The persisted pace.json as-is ({} if absent/unreadable). Does NOT include
    the global pause (that lives in operator_redrive) — see :func:`read_config`."""
    p = _config_path(cfg, slug)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(cfg: Any, slug: str, data: dict) -> None:
    d = _state_dir(cfg, slug)
    d.mkdir(parents=True, exist_ok=True)
    p = _config_path(cfg, slug)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)


def _normalized_initiatives(raw: dict) -> dict[str, dict]:
    """The ``initiatives`` map with defaults applied + keys canonicalised.

    Later duplicate keys (e.g. a stem and its ``.md`` form) win — they collapse
    onto one canonical entry, so the config can never carry two views of one
    initiative.
    """
    out: dict[str, dict] = {}
    src = raw.get("initiatives")
    if not isinstance(src, dict):
        return out
    for name, body in src.items():
        key = normalize_initiative(name)
        if not key:
            continue
        body = body if isinstance(body, dict) else {}
        out[key] = {
            "weight": _coerce_float(body.get("weight"), _DEFAULT_WEIGHT),
            "priority": _coerce_int(body.get("priority"), _DEFAULT_PRIORITY),
            "paused": _coerce_bool(body.get("paused")),
        }
    return out


def _normalized_drive(raw: dict) -> dict:
    """The ``drive`` block with defaults applied — T-0828 / D-0069 "The record".

    ⚠ **MIRRORED** by ``api/app/routes_transparency.py:_normalized_drive``; the
    two must return the same KEYS for the same input or the UI shows a project
    with no drive mode set. ``api/tests/test_pace_drive_mirror.py`` is the pin.

    Shape::

        {
          "scope": str, "stop_when": str, "on_stop": str,   # effective values
          "set_by": str|None, "set_at": str|None, "source_text": str|None,
          "configured": bool,        # a `drive` block exists on disk at all
          "invalid": {field: raw},   # stored values NOT in the closed set
        }

    Two decisions worth stating, because both are the difference between this
    being the visibility half and being another silent surface:

    * ``configured`` distinguishes "never set" from "explicitly set to the
      default". Both read ``scope: all``, and he must be able to tell them apart
      — that IS the question he asked («какой режим драйва щас стоит»).
    * ``invalid`` carries the rejected value rather than dropping it. A
      hand-edited or future-version ``pace.json`` must not crash a read (the
      worker tick and the UI both call this), but neither may it report the
      default as if that were what was set — an absent value that reads
      identically to a real one is the silent-None failure D-0069 calls the most
      dangerous line in the design. The effective value falls back to the
      default AND every surface prints the ``invalid`` entry.
    """
    out: dict = dict(DRIVE_DEFAULTS)
    out.update({"set_by": None, "set_at": None, "source_text": None})
    out["configured"] = False
    out["invalid"] = {}

    src = raw.get("drive")
    if not isinstance(src, dict):
        return out
    out["configured"] = True

    for field in DRIVE_CHOICES:
        if field not in src or src[field] is None:
            continue
        try:
            out[field] = validate_drive_value(field, src[field])
        except DriveModeError:
            out["invalid"][field] = src[field]  # effective value stays the default

    for field in DRIVE_PROVENANCE_FIELDS:
        v = src.get(field)
        out[field] = str(v) if v is not None else None
    return out


# ---------------------------------------------------------------------------
# Public read API (the SSOT the T-0475 consumer imports)
# ---------------------------------------------------------------------------

def read_config(cfg: Any, slug: str) -> dict:
    """The full, normalised pace-control view for ``slug``::

        {
          "max_in_progress": int,    # 0 = unlimited
          "paused": bool,            # GLOBAL operator pause (operator_redrive SSOT)
          "initiatives": {           # canonical .md keys; defaults applied
            "<name>.md": {"weight": float, "priority": int, "paused": bool},
            ...
          },
          "drive": {...},            # T-0828; see _normalized_drive
        }

    Safe on a fresh project (returns the all-defaults view). The global ``paused``
    is read from :mod:`operator_redrive` so there is one pause SSOT, not two.
    """
    raw = _load_raw(cfg, slug)
    return {
        "max_in_progress": max(0, _coerce_int(raw.get("max_in_progress"), 0)),
        "paused": _global_paused(cfg, slug),
        "initiatives": _normalized_initiatives(raw),
        "drive": _normalized_drive(raw),
    }


def max_in_progress(cfg: Any, slug: str) -> int:
    """The max-in-progress ceiling (0 = unlimited). Convenience for the consumer."""
    return read_config(cfg, slug)["max_in_progress"]


def initiative_pace(cfg: Any, slug: str, name: str) -> dict:
    """The ``{weight, priority, paused}`` for one initiative, defaults applied for
    an unconfigured one. Convenience for the consumer's per-task ordering."""
    key = normalize_initiative(name)
    return read_config(cfg, slug)["initiatives"].get(
        key, {"weight": _DEFAULT_WEIGHT, "priority": _DEFAULT_PRIORITY, "paused": False}
    )


def read_drive(cfg: Any, slug: str) -> dict:
    """The normalised ``drive`` block (T-0828) — see :func:`_normalized_drive`.

    This is the entry point the L2/L3/L4 lanes read: it does NOT touch
    :mod:`operator_redrive` (unlike :func:`read_config`, which merges the global
    pause), so it is safe to call from inside the pickup path with no import
    cycle and no extra stat. Never raises: an unreadable / invalid config yields
    the defaults plus a populated ``invalid`` map.
    """
    return _normalized_drive(_load_raw(cfg, slug))


# ---------------------------------------------------------------------------
# Global pause — delegate to operator_redrive (single SSOT)
# ---------------------------------------------------------------------------

def _global_paused(cfg: Any, slug: str) -> bool:
    """True iff the operator re-drive is user-paused. Imported lazily so pace.py
    has no import-time coupling to operator_redrive."""
    from bot_squad_worker import operator_redrive as _ord
    return _ord.is_paused(cfg, slug)


def pause(cfg: Any, slug: str, *, by: str = "user", reason: str = "") -> dict:
    """Set the GLOBAL operator pause (delegates to operator_redrive)."""
    from bot_squad_worker import operator_redrive as _ord
    return _ord.pause(cfg, slug, by=by, reason=reason)


def resume(cfg: Any, slug: str) -> bool:
    """Clear the GLOBAL operator pause (delegates to operator_redrive)."""
    from bot_squad_worker import operator_redrive as _ord
    return _ord.resume(cfg, slug)


# ---------------------------------------------------------------------------
# Public write API (used by the `bsq pace` verb; also test-callable)
# ---------------------------------------------------------------------------

def set_max_in_progress(cfg: Any, slug: str, n: int) -> int:
    """Set the max-in-progress ceiling (clamped to >= 0; 0 = unlimited). Returns
    the stored value."""
    n = max(0, _coerce_int(n, 0))
    raw = _load_raw(cfg, slug)
    raw["max_in_progress"] = n
    _save_raw(cfg, slug, raw)
    log.info("pace[%s]: max_in_progress = %d", slug, n)
    return n


def set_initiative(
    cfg: Any,
    slug: str,
    name: str,
    *,
    weight: Optional[float] = None,
    priority: Optional[int] = None,
    paused: Optional[bool] = None,
) -> dict:
    """Create / update one initiative's pace entry (partial — only the passed
    fields change; unset fields keep their current / default value). Returns the
    resulting normalised entry."""
    key = normalize_initiative(name)
    if not key:
        raise ValueError("initiative name must be non-empty")
    raw = _load_raw(cfg, slug)
    inits = _normalized_initiatives(raw)  # canonicalises existing keys too
    entry = inits.get(
        key, {"weight": _DEFAULT_WEIGHT, "priority": _DEFAULT_PRIORITY, "paused": False}
    )
    if weight is not None:
        entry["weight"] = _coerce_float(weight, _DEFAULT_WEIGHT)
    if priority is not None:
        entry["priority"] = _coerce_int(priority, _DEFAULT_PRIORITY)
    if paused is not None:
        entry["paused"] = bool(paused)
    inits[key] = entry
    raw["initiatives"] = inits
    _save_raw(cfg, slug, raw)
    log.info("pace[%s]: initiative %s = %s", slug, key, entry)
    return entry


def clear_initiative(cfg: Any, slug: str, name: str) -> bool:
    """Drop one initiative's pace entry (revert it to defaults). Returns True iff
    an entry was actually removed."""
    key = normalize_initiative(name)
    raw = _load_raw(cfg, slug)
    inits = _normalized_initiatives(raw)
    if key not in inits:
        return False
    del inits[key]
    raw["initiatives"] = inits
    _save_raw(cfg, slug, raw)
    log.info("pace[%s]: cleared initiative %s", slug, key)
    return True


# ---------------------------------------------------------------------------
# Drive modes — write API (T-0828 / D-0069)
# ---------------------------------------------------------------------------

def set_drive(
    cfg: Any,
    slug: str,
    *,
    scope: Optional[str] = None,
    stop_when: Optional[str] = None,
    on_stop: Optional[str] = None,
    set_by: str = "user",
    source_text: Optional[str] = None,
) -> dict:
    """Set the standing drive mode. Partial — only the passed axes change.

    Records ``set_by`` / ``set_at`` always, and ``source_text`` when given: the
    words that set it, verbatim, so ``bsq pace show`` and the UI can answer "did
    my instruction land" rather than making him infer it from behaviour.

    **Validates before writing anything.** Every passed value is checked first,
    so a call with one good and one bad axis leaves the stored config untouched
    instead of half-applying — a partially-applied settings write is worse than
    a rejected one, because the surface then reports a state nobody asked for.

    :raises DriveModeError: on an out-of-set value (never coerced to a default).
    :raises ValueError: if nothing was passed to set.
    """
    passed = {"scope": scope, "stop_when": stop_when, "on_stop": on_stop}
    given = {k: v for k, v in passed.items() if v is not None}
    if not given and source_text is None:
        raise ValueError("nothing to set — pass at least one of scope/stop_when/on_stop")

    # Validate ALL of them before touching the store (see docstring).
    validated = {k: validate_drive_value(k, v) for k, v in given.items()}

    raw = _load_raw(cfg, slug)
    block = raw.get("drive")
    block = dict(block) if isinstance(block, dict) else {}
    block.update(validated)
    block["set_by"] = str(set_by or "user")
    block["set_at"] = _utc_now_iso()
    if source_text is not None:
        block["source_text"] = str(source_text)
    raw["drive"] = block
    _save_raw(cfg, slug, raw)
    log.info("pace[%s]: drive = %s (by %s)", slug, validated, block["set_by"])
    return _normalized_drive(raw)


def clear_drive(cfg: Any, slug: str) -> bool:
    """Drop the whole ``drive`` block — back to the defaults, i.e. back to the
    behaviour a project that never set a mode has.

    Returns True iff a block was actually removed. This is the "clearing back to
    default must be possible" half: without it the only way out of a mode is to
    set the default explicitly, which reads as a deliberate choice on every
    surface (``configured: true``) and so cannot express "I have no standing
    mode".
    """
    raw = _load_raw(cfg, slug)
    if "drive" not in raw:
        return False
    del raw["drive"]
    _save_raw(cfg, slug, raw)
    log.info("pace[%s]: cleared drive mode (back to defaults)", slug)
    return True
