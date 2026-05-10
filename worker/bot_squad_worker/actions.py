"""Worker action allowlist.

Every action is a typed function registered here. The dispatcher refuses
unknown action names and unexpected parameters. This is the security
boundary: even if the API is compromised, the attacker can only invoke
actions on this allowlist with their declared parameter shapes.

v1 ships only `noop` (proof-of-life). Spec #3 adds deploy/git_status/
tmux_panes/etc.
"""
from __future__ import annotations

import time
from typing import Any, Callable


class ActionError(Exception):
    """Raised when an action call is invalid (unknown name, bad params)."""


def _action_noop(params: dict[str, Any]) -> dict[str, Any]:
    """Proof-of-life: takes no params, returns {ok, ts}."""
    if params:
        raise ActionError(f"noop takes no params, got: {sorted(params)}")
    return {"ok": True, "ts": time.time()}


ACTION_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "noop": _action_noop,
}


def dispatch(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Look up `name` in the allowlist; reject if missing; invoke."""
    handler = ACTION_REGISTRY.get(name)
    if handler is None:
        raise ActionError(f"unknown action: {name!r}")
    return handler(params or {})
