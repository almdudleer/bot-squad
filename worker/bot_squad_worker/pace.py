"""User-controlled execution pace (T-0482) — Process Paradigm M3 / F3.4.

> "The system generally is required to implement all given tasks, but the order
> and pace of implementation is controlled by user. This control might consist
> of different, modes, strategies, constraints, toggles, it doesn't matter here."
> — SOURCE-VERBATIM Part A.

This is the **config / storage / toggle** half of that control (TL-B ruling
``option-b-clean``): the SSOT for a small, user-settable pace-control config the
operator consumes. The *operator-dispatch-HONORS-the-controls* half is T-0475
(Cluster A) — it imports the read helpers here; it does not duplicate storage.

Three controls, deliberately minimal (the mechanism is "my call, cost LOW" per
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

Storage: one per-project JSON at ``data/<slug>/_worker/pace/pace.json`` (mirrors
the ``_worker/<feature>/`` layout :mod:`operator_redrive` uses). The global pause
is NOT stored here — it lives in operator_redrive's flag and is merged in on read
so a consumer gets one unified view.

No new worker socket action and no edit to ``actions.py`` (single-owner / closed
allowlist): the user surface is the file-based ``bsq pace`` verb, which writes
this same JSON (and the operator_redrive flag for pause) directly.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Defaults applied on read — an absent / fresh config is uncapped + running, so a
# legacy project that never set a pace control behaves exactly as before.
_DEFAULT_WEIGHT = 1.0
_DEFAULT_PRIORITY = 0


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
        }

    Safe on a fresh project (returns the all-defaults view). The global ``paused``
    is read from :mod:`operator_redrive` so there is one pause SSOT, not two.
    """
    raw = _load_raw(cfg, slug)
    return {
        "max_in_progress": max(0, _coerce_int(raw.get("max_in_progress"), 0)),
        "paused": _global_paused(cfg, slug),
        "initiatives": _normalized_initiatives(raw),
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
