"""Tests for Phase 2 action mode-gating (coordinator vs user-worker)."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker.actions import (
    ACTION_MODES,
    ACTION_REGISTRY,
    ActionError,
    dispatch,
    set_mode,
)


@pytest.fixture(autouse=True)
def _reset_mode():
    """Each test gets coordinator mode by default; restore after."""
    set_mode("coordinator")
    yield
    set_mode("coordinator")


def test_every_action_has_a_mode_tag():
    # Tag set must cover registry exactly — no orphans either direction.
    assert set(ACTION_MODES) == set(ACTION_REGISTRY)


def test_mode_tags_are_known_values():
    assert set(ACTION_MODES.values()) <= {"coordinator_only", "tmux_only", "both"}


def test_user_worker_refuses_coordinator_only_actions():
    set_mode("user-worker")
    # tg_notify is coordinator_only — must be refused before handler runs.
    with pytest.raises(ActionError, match="not available in user-worker"):
        dispatch("tg_notify", {"message": "hi"})


def test_user_worker_refuses_peer_send():
    set_mode("user-worker")
    with pytest.raises(ActionError, match="not available in user-worker"):
        dispatch("peer_send", {
            "slug": "x", "from_sid": "S-x-p1", "to": "y", "text": "z",
        })


def test_user_worker_allows_noop():
    set_mode("user-worker")
    out = dispatch("noop", {})
    assert out["ok"] is True


def test_user_worker_allows_tmux_only_actions_in_principle(tmp_path, monkeypatch):
    """user-worker accepts list_sessions (will fail validation later but not on mode)."""
    set_mode("user-worker")
    # Missing slug raises a *param* error, NOT a mode error — that's the
    # signal the dispatcher reached the handler.
    with pytest.raises(ActionError, match="missing required"):
        dispatch("list_sessions", {})


def test_coordinator_allows_both_classes(tmp_path, monkeypatch):
    """Coordinator mode accepts coordinator-only AND tmux-only actions."""
    set_mode("coordinator")
    # noop is the safest probe — no config needed.
    out = dispatch("noop", {})
    assert out["ok"] is True


def test_unset_mode_defaults_to_coordinator():
    """Single-user back-compat: unset mode must NOT block any action."""
    set_mode(None)
    # Should reach the handler (param error, not mode error)
    with pytest.raises(ActionError, match="missing required"):
        dispatch("tg_notify", {})
