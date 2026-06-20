"""T-0321: owner_user derivation (per-user-scoping username) at spawn.

`owner` is overloaded (username / constant-team sentinel / TL-SID). When a caller
doesn't pass an explicit owner_user, spawn() derives the human scoping username
from owner: username→itself, constant-team→coordinator, TL-SID→the TL's human.
"""
from __future__ import annotations

import types
from pathlib import Path

from bot_squad_worker.sessions import (
    _derive_owner_user, _write_session_metadata,
)


def _cfg(tmp_path, *, coordinator="coord"):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "system_settings.toml").write_text(
        f'[admin]\ncoordinator_user = "{coordinator}"\n'
    )
    data = tmp_path / "data"
    (data / "p1" / "sessions").mkdir(parents=True)
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=data,
                                 config_dir=tmp_path / "config")


def test_username_owner_is_itself(tmp_path):
    cfg = _cfg(tmp_path)
    assert _derive_owner_user(cfg, "p1", "alice") == "alice"


def test_constant_team_maps_to_coordinator(tmp_path):
    cfg = _cfg(tmp_path, coordinator="almdudleer")
    assert _derive_owner_user(cfg, "p1", "constant-team") == "almdudleer"


def test_tl_sid_resolves_to_tl_human_via_owner_user(tmp_path):
    cfg = _cfg(tmp_path)
    _write_session_metadata(
        cfg.data_dir / "p1" / "sessions" / "S-u-TL-p9.md",
        {"sid": "S-u-TL-p9", "status": "active", "window": "TL",
         "owner": "S-u-TL-p9", "owner_user": "tlhuman"})
    assert _derive_owner_user(cfg, "p1", "S-u-TL-p9") == "tlhuman"


def test_tl_sid_falls_back_to_tl_owner_username(tmp_path):
    cfg = _cfg(tmp_path)
    _write_session_metadata(
        cfg.data_dir / "p1" / "sessions" / "S-u-TL-p9.md",
        {"sid": "S-u-TL-p9", "status": "active", "window": "TL",
         "owner": "tlhuman"})  # legacy: owner is the username, no owner_user
    assert _derive_owner_user(cfg, "p1", "S-u-TL-p9") == "tlhuman"


def test_tl_sid_unresolvable_is_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert _derive_owner_user(cfg, "p1", "S-u-ghost-p1") is None


def test_none_and_sentinel_are_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert _derive_owner_user(cfg, "p1", None) is None
    assert _derive_owner_user(cfg, "p1", "~") is None


def test_constant_team_no_coordinator_is_none(tmp_path):
    cfg = _cfg(tmp_path, coordinator="")
    assert _derive_owner_user(cfg, "p1", "constant-team") is None
