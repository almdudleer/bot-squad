"""T-0630: fleet-default `claude --model` — ~/.claude/settings.json round-trip.

get_model/set_model are the ONLY thing that touches the file (the API
container can't); everything here exercises them directly against a fake
home dir via the shared `_get_user_home` monkeypatch convention (see
test_sessions.py et al.).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_squad_worker import fleet_model
from bot_squad_worker import sessions as S


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    return tmp_path


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def test_get_model_absent_file_returns_empty(_home):
    assert fleet_model.get_model() == ""


def test_set_model_creates_file_and_dir(_home):
    fleet_model.set_model("claude-sonnet-5")
    p = _settings_path(_home)
    assert p.exists()
    assert json.loads(p.read_text()) == {"model": "claude-sonnet-5"}
    assert fleet_model.get_model() == "claude-sonnet-5"


def test_set_model_preserves_unrelated_keys(_home):
    p = _settings_path(_home)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"permissions": {"allow": ["Bash"]}, "other": 1}))

    fleet_model.set_model("claude-opus-4-8")

    data = json.loads(p.read_text())
    assert data["model"] == "claude-opus-4-8"
    assert data["permissions"] == {"allow": ["Bash"]}
    assert data["other"] == 1


def test_set_model_empty_string_clears_key(_home):
    p = _settings_path(_home)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"model": "claude-fable-5", "other": 1}))

    fleet_model.set_model("")

    data = json.loads(p.read_text())
    assert "model" not in data
    assert data["other"] == 1
    assert fleet_model.get_model() == ""


def test_set_model_rejects_unknown_value(_home):
    with pytest.raises(ValueError, match="not allowed"):
        fleet_model.set_model("gpt-5")
    # No partial write on rejection.
    assert not _settings_path(_home).exists()


def test_get_model_malformed_json_returns_empty(_home):
    p = _settings_path(_home)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert fleet_model.get_model() == ""


def test_set_model_malformed_json_is_overwritten_cleanly(_home):
    p = _settings_path(_home)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")

    fleet_model.set_model("claude-sonnet-5")

    assert json.loads(p.read_text()) == {"model": "claude-sonnet-5"}


def test_get_model_non_dict_json_returns_empty(_home):
    p = _settings_path(_home)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps([1, 2, 3]))
    assert fleet_model.get_model() == ""


@pytest.mark.parametrize("model", sorted(fleet_model.ALLOWED_MODELS))
def test_all_allowed_models_accepted(_home, model):
    fleet_model.set_model(model)
    assert fleet_model.get_model() == model
