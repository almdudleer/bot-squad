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
from bot_squad_worker import agent_provider
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
CLAUDE_MODELS = frozenset({
    "",
    "sonnet", "opus", "fable",                       # passthrough -> latest
    "claude-sonnet-5", "claude-opus-5",
    "claude-opus-4-8", "claude-fable-5", "opus[1m]",  # explicit pins
})
CODEX_MODELS = frozenset({
    "codex",
    *agent_provider.CODEX_MODEL_ALIASES,
    *agent_provider.CODEX_MODELS,
})
ALLOWED_MODELS = CLAUDE_MODELS | CODEX_MODELS

# T-0707: which CLASS an explicit pinned id belongs to, for the
# availability gate below. Bare aliases map to themselves via CLASS_ALIASES.
_EXPLICIT_ID_CLASS = {
    "claude-sonnet-5": "sonnet",
    "claude-opus-5": "opus",
    "claude-opus-4-8": "opus",
    "opus[1m]": "opus",
    "claude-fable-5": "fable",
}

# T-0707: account-level availability gate — ORTHOGONAL to ALLOWED_MODELS
# (syntax: is this a recognized value) and CLASS_ALIASES (which classes
# passthrough to latest-in-class). This is a FACT about the account's
# billing state, not a version pin: this account has never purchased Fable
# 5 usage credits, so a fresh/resumed spawn on that class parks on Claude
# Code's own interactive "requires usage credits" gate and hangs until a
# human answers it (observed live 2026-07-26, see stall_sweep.py). Gating
# the CLASS (not a specific model id) means it doesn't regress T-0704's
# staleness fix either way — lifting this gate once credits are purchased
# is a one-line removal from this set, no id-pinning involved.
UNAVAILABLE_CLASSES = frozenset({"fable"})

# Display name for the error message only — purely cosmetic, falls back to
# the bare class name for anything not listed.
_CLASS_DISPLAY_NAME = {"fable": "Fable 5"}


def _class_of(value: str) -> str:
    """The model CLASS ``value`` belongs to (bare alias or explicit pin);
    "" for blank/unrecognized values."""
    if value in CLASS_ALIASES:
        return value
    return _EXPLICIT_ID_CLASS.get(value, "")


def is_available(value: str) -> bool:
    """False iff ``value`` names a model class gated by
    :data:`UNAVAILABLE_CLASSES` (T-0707). Distinct from membership in
    :data:`ALLOWED_MODELS`, which only checks syntax."""
    cls = _class_of(value)
    return not (cls and cls in UNAVAILABLE_CLASSES)


def check_available(value: str) -> None:
    """Raise ``ValueError`` if ``value``'s model class is account-gated
    (T-0707). Called from both :func:`resolve_model` (the spawn seam) and
    :func:`set_model` (the fleet-default seam) so neither choke point can
    hand out a class known to hang on a blocking interactive prompt.
    """
    cls = _class_of(value)
    if cls and cls in UNAVAILABLE_CLASSES:
        name = _CLASS_DISPLAY_NAME.get(cls, cls)
        raise ValueError(
            f"{name} requires usage credits, unavailable on this account "
            "-- spawn with a different --model (sonnet/opus), or set up "
            "usage credits on claude.ai first"
        )


def resolve_model(value: str, provider: str | None = None) -> str:
    """Validate a ``--model`` value; return it unchanged if allowed.

    A class alias (``sonnet``/``opus``/``fable``) or an explicit id in
    :data:`ALLOWED_MODELS` passes through unchanged — ``claude`` itself
    resolves a class alias to the latest-in-class model, so passing the BARE
    alias (not a pinned id) keeps the choice from going stale on a new release
    (T-0704). "" (or whitespace-only) passes through as "" (no override).
    Anything else raises ``ValueError`` so garbage fails loudly here rather
    than silently reaching the launch command (T-0694). A syntactically
    valid value whose CLASS is account-gated (T-0707, e.g. Fable 5's usage
    credits) also raises ``ValueError`` — see :func:`check_available`.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if v == "codex":
        if provider == "claude":
            raise ValueError("model 'codex' belongs to provider 'codex'")
        return ""
    if v in agent_provider.CODEX_MODEL_ALIASES:
        if provider == "claude":
            raise ValueError(f"model {v!r} belongs to provider 'codex'")
        return agent_provider.CODEX_MODEL_ALIASES[v]
    if v in agent_provider.CODEX_MODELS:
        if provider == "claude":
            raise ValueError(f"model {v!r} belongs to provider 'codex'")
        return v
    if provider == "codex":
        raise ValueError(
            f"model {v!r} belongs to provider 'claude'; project default is "
            "Codex (use sol/terra/luna, or explicitly select provider claude)"
        )
    if v not in ALLOWED_MODELS:
        raise ValueError(f"model not allowed: {value!r}")
    check_available(v)
    return v


def _settings_path() -> Path:
    return Path(_sessions._get_user_home()) / ".claude" / "settings.json"


def _provider_path(config_dir: Path | None = None) -> Path:
    if config_dir is not None:
        # Workers are systemd-hardened with config read-only and data writable.
        # Keep this runtime selector beside the other mutable worker state.
        return Path(config_dir).parent / "data" / "_state" / "agent_provider.json"
    return Path(_sessions._get_user_home()) / ".config" / "bot-squad" / "agent.json"


def get_provider(config_dir: Path | None = None) -> str:
    """Fleet-default agent provider. Missing/invalid state means Claude."""
    try:
        raw = json.loads(_provider_path(config_dir).read_text())
    except (OSError, ValueError):
        return "claude"
    return "codex" if isinstance(raw, dict) and raw.get("provider") == "codex" else "claude"


def get_model(config_dir: Path | None = None) -> str:
    """Current fleet choice. ``codex`` is the provider pseudo-model."""
    if get_provider(config_dir) == "codex":
        return "codex"
    try:
        raw = json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("model") or "")


def set_model(model: str, config_dir: Path | None = None) -> None:
    """Set (or clear, for ``model == ""``) the ``model`` key.

    Atomic read-modify-write (unique tmp + ``os.replace``, T-0373 convention)
    that preserves every other key already in the file. Raises ``ValueError``
    if ``model`` is not in ``ALLOWED_MODELS`` or (T-0707) its class is
    account-gated — routed through :func:`resolve_model`, the same
    validation the spawn seam uses, so this isn't a second copy of either
    check.
    """
    requested = (model or "").strip()
    provider = (
        "codex"
        if requested == "codex"
        or requested in agent_provider.CODEX_MODEL_ALIASES
        or requested in agent_provider.CODEX_MODELS
        else "claude"
    )
    model = resolve_model(requested, provider=provider)

    provider_path = _provider_path(config_dir)
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    with task_lock(provider_path):
        atomic_write(
            provider_path,
            json.dumps({"provider": provider}, indent=2)
            + "\n",
        )
    if provider == "codex":
        return

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
