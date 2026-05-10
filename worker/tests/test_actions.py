"""Tests for worker.actions."""
from __future__ import annotations

import pytest

from bot_squad_worker.actions import ACTION_REGISTRY, ActionError, dispatch


def test_noop_returns_ok():
    result = dispatch("noop", {})
    assert result["ok"] is True
    assert "ts" in result


def test_unknown_action_raises():
    with pytest.raises(ActionError) as excinfo:
        dispatch("rm_rf_root", {})
    assert "unknown action" in str(excinfo.value)


def test_registry_lists_only_allowed_actions():
    # The whole point of the registry: closed allowlist. v1 has only noop.
    assert set(ACTION_REGISTRY.keys()) == {"noop"}


def test_noop_rejects_extra_params():
    # Typed contract — noop takes no params; extras are an error.
    with pytest.raises(ActionError) as excinfo:
        dispatch("noop", {"unexpected": 1})
    assert "unexpected" in str(excinfo.value)
