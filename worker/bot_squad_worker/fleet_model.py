"""T-0630 (T-0620 seam, D-0056): the fleet-default ``claude --model``.

A spawn that omits an explicit ``model`` (T-0623) and has no per-role default
(``system_settings.toml`` ``[models]``) falls through to whatever the spawned
``claude`` picks up from the worker linux user's own ``~/.claude/settings.json``
``model`` key. That key is what this module reads/writes — the stakeholder's
"change operator model when you hit limits" ask (T-0620) needs a lever for it.

The API container has no filesystem access to a linux user's home directory,
so this is worker-side only; ``api/app/routes_operator.py`` calls the two
worker actions built on ``get_model``/``set_model`` over the socket instead of
touching the file itself.
"""
from __future__ import annotations

import json
from pathlib import Path

from bot_squad_worker import sessions as _sessions
from bot_squad_worker.mdlock import atomic_write, task_lock

# T-0704: the class aliases pass STRAIGHT THROUGH to ``claude`` unchanged.
# ``claude`` (verified on CLI v2.1.220 via a live ``--model X --output-format
# json`` probe reading ``modelUsage[..].canonicalModel``) resolves each to the
# LATEST model in its class at BOTH consumption sites — the ``--model`` flag
# AND the ``settings.json`` ``model``-key read: opus -> claude-opus-5, sonnet
# -> claude-sonnet-5, fable -> claude-fable-5. Storing the BARE alias (rather
# than pinning a canonical id, as T-0694 originally did) keeps the fleet/role
# default auto-tracking latest-in-class so it never goes stale on a new
# release — the exact failure where a pinned ``opus -> claude-opus-4-8`` map
# silently missed Opus 5's 2026-07-24 launch. (T-0694's canonicalize-here step
# was a workaround for claude's alias resolution silently failing back then;
# that native resolution is now reliable, so the workaround is retired.)
CLASS_ALIASES = frozenset({"sonnet", "opus", "fable"})

# "" clears the key (spawned claude falls back to its own built-in default).
# The bare class aliases above are the staleness-proof choice. Explicit full
# ids stay allowed for anyone who deliberately wants to PIN a specific version
# (e.g. stay on claude-opus-4-8, or the 1M-context ``opus[1m]`` variant).
ALLOWED_MODELS = frozenset({
    "",
    "sonnet", "opus", "fable",                       # passthrough -> latest
    "claude-sonnet-5", "claude-opus-5",
    "claude-opus-4-8", "claude-fable-5", "opus[1m]",  # explicit pins
})


def resolve_model(value: str) -> str:
    """Validate a ``--model`` value; return it unchanged if allowed.

    A class alias (``sonnet``/``opus``/``fable``) or an explicit id in
    :data:`ALLOWED_MODELS` passes through unchanged — ``claude`` itself
    resolves a class alias to the latest-in-class model, so passing the BARE
    alias (not a pinned id) keeps the choice from going stale on a new release
    (T-0704). "" (or whitespace-only) passes through as "" (no override).
    Anything else raises ``ValueError`` so garbage fails loudly here rather
    than silently reaching the launch command (T-0694).
    """
    v = (value or "").strip()
    if not v:
        return ""
    if v not in ALLOWED_MODELS:
        raise ValueError(f"model not allowed: {value!r}")
    return v


def _settings_path() -> Path:
    return Path(_sessions._get_user_home()) / ".claude" / "settings.json"


def get_model() -> str:
    """Current fleet-default ``model`` key, "" if unset/absent/unparseable."""
    try:
        raw = json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("model") or "")


def set_model(model: str) -> None:
    """Set (or clear, for ``model == ""``) the ``model`` key.

    Atomic read-modify-write (unique tmp + ``os.replace``, T-0373 convention)
    that preserves every other key already in the file. Raises ``ValueError``
    if ``model`` is not in ``ALLOWED_MODELS``.
    """
    if model not in ALLOWED_MODELS:
        raise ValueError(f"model not allowed: {model!r}")

    path = _settings_path()
    with task_lock(path):
        try:
            raw = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if model:
            raw["model"] = model
        else:
            raw.pop("model", None)
        atomic_write(path, json.dumps(raw, indent=2) + "\n")
