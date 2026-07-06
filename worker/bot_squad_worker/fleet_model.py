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

# "" clears the key (spawned claude falls back to its own built-in default).
# claude-fable-5 is a valid fleet default value here, but T-0623 deliberately
# never assigns it as a per-ROLE default — this is the explicit fleet-wide
# opt-in that role defaults intentionally don't provide.
ALLOWED_MODELS = frozenset({
    "", "claude-sonnet-5", "claude-opus-4-8", "opus[1m]", "claude-fable-5",
})


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
