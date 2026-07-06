"""T-0630: worker action pair fleet_model_get / fleet_model_set — thin
dispatch wrappers over fleet_model.py, mirroring test_operator_pause_actions.py."""
from __future__ import annotations

import json

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError, dispatch as act_dispatch


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
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

    act_dispatch("fleet_model_set", {"model": "claude-fable-5"})

    data = json.loads(p.read_text())
    assert data["model"] == "claude-fable-5"
    assert data["permissions"] == {"allow": ["Bash"]}


def test_fleet_model_set_rejects_bad_model(_home):
    with pytest.raises(ActionError, match="not allowed"):
        act_dispatch("fleet_model_set", {"model": "gpt-5"})


def test_fleet_model_set_missing_required_param(_home):
    with pytest.raises(ActionError, match="missing required"):
        act_dispatch("fleet_model_set", {})


def test_fleet_model_set_rejects_unexpected_params(_home):
    with pytest.raises(ActionError, match="unexpected"):
        act_dispatch("fleet_model_set", {"model": "claude-sonnet-5", "bogus": 1})


def test_fleet_model_get_rejects_unexpected_params(_home):
    with pytest.raises(ActionError, match="unexpected"):
        act_dispatch("fleet_model_get", {"bogus": 1})


def test_fleet_model_clear_via_empty_string(_home):
    act_dispatch("fleet_model_set", {"model": "claude-sonnet-5"})
    act_dispatch("fleet_model_set", {"model": ""})
    assert act_dispatch("fleet_model_get", {}) == {"ok": True, "model": ""}
