"""T-0630: worker action pair fleet_model_get / fleet_model_set — thin
dispatch wrappers over fleet_model.py, mirroring test_operator_pause_actions.py."""
from __future__ import annotations

import json

import pytest

import bot_squad_worker.actions as A
from bot_squad_worker import sessions as S
from bot_squad_worker.config import Config
from bot_squad_worker.actions import ActionError, dispatch as act_dispatch


@pytest.fixture(autouse=True)
def _home(tmp_path, tmp_config_dir, monkeypatch):
    """Fake user home for ``~/.claude/settings.json`` PLUS the worker Config
    both handlers now resolve their fleet state against.

    T-0861: fa4ef4f made ``fleet_model_get``/``_set`` call ``_get_config()``,
    because the provider half of the fleet choice is worker state
    (``<config_dir>/../data/_state/agent_provider.json``) and not something in
    the user's home. A fixture that faked only the home dir therefore died on
    ``worker config not initialised`` before reaching any assertion.

    ``tmp_config_dir`` is the conftest fixture and shares this test's
    ``tmp_path``, so ``cfg.data_dir`` is ``tmp_path/data`` — the provider file
    stays inside the tmp tree instead of touching the real install.
    """
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    return tmp_path


def test_fleet_model_get_defaults_to_empty(_home):
    out = act_dispatch("fleet_model_get", {})
    assert out == {"ok": True, "model": ""}


def test_fleet_model_set_then_get_round_trips(_home):
    out = act_dispatch("fleet_model_set", {"model": "claude-opus-4-8"})
    assert out == {"ok": True, "model": "claude-opus-4-8"}
    assert act_dispatch("fleet_model_get", {}) == {"ok": True, "model": "claude-opus-4-8"}


def test_fleet_model_set_preserves_other_keys_on_disk(_home):
    p = _home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))

    act_dispatch("fleet_model_set", {"model": "claude-opus-4-8"})

    data = json.loads(p.read_text())
    assert data["model"] == "claude-opus-4-8"
    assert data["permissions"] == {"allow": ["Bash"]}


def test_fleet_model_set_rejects_bad_model(_home):
    with pytest.raises(ActionError, match="not allowed"):
        act_dispatch("fleet_model_set", {"model": "gpt-5"})


def test_fleet_model_set_rejects_gated_class(_home):
    """T-0707: Fable 5 requires usage credits unavailable on this account —
    fleet_model_set must reject it loudly, not persist a fleet default that
    hangs every future spawn falling through to it."""
    with pytest.raises(ActionError, match="usage credits"):
        act_dispatch("fleet_model_set", {"model": "fable"})
    assert act_dispatch("fleet_model_get", {}) == {"ok": True, "model": ""}


def test_fleet_model_set_missing_required_param(_home):
    with pytest.raises(ActionError, match="missing required"):
        act_dispatch("fleet_model_set", {})


def test_fleet_model_set_rejects_unexpected_params(_home):
    with pytest.raises(ActionError, match="unexpected"):
        act_dispatch("fleet_model_set", {"model": "claude-sonnet-5", "bogus": 1})


def test_fleet_model_get_rejects_unexpected_params(_home):
    with pytest.raises(ActionError, match="unexpected"):
        act_dispatch("fleet_model_get", {"bogus": 1})


def test_fleet_model_set_writes_provider_state_under_the_worker_config(
    _home, tmp_config_dir,
):
    """T-0861: the handlers pass ``_caps_config_dir(cfg)`` down, so the provider
    half of the choice lands in WORKER state (``<config_dir>/../data/_state``),
    not in the user's home. Pinned because the cheap way to make the rest of
    this file green again is to hand ``fleet_model`` no config dir at all —
    which silently relocates production's provider file into ``~/.config``.
    """
    provider_path = tmp_config_dir.parent / "data" / "_state" / "agent_provider.json"

    out = act_dispatch("fleet_model_set", {"model": "sol"})

    assert out == {"ok": True, "model": "sol"}
    assert json.loads(provider_path.read_text()) == {"provider": "codex"}
    # A codex choice is the provider pseudo-model, and it does NOT write a
    # claude fleet default.
    assert act_dispatch("fleet_model_get", {}) == {"ok": True, "model": "codex"}
    assert not (_home / ".claude" / "settings.json").exists()

    act_dispatch("fleet_model_set", {"model": "claude-sonnet-5"})

    assert json.loads(provider_path.read_text()) == {"provider": "claude"}
    assert act_dispatch("fleet_model_get", {}) == {"ok": True, "model": "claude-sonnet-5"}


def test_fleet_model_clear_via_empty_string(_home):
    act_dispatch("fleet_model_set", {"model": "claude-sonnet-5"})
    act_dispatch("fleet_model_set", {"model": ""})
    assert act_dispatch("fleet_model_get", {}) == {"ok": True, "model": ""}
