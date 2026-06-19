"""T-0237 Layer-2 v1: operator-invoked reuse-vs-spawn dispatch decision.

decide_dispatch(cfg, slug, task_id) returns whether to REUSE an idle live dev
session that has useful context (same initiative + context headroom) or SPAWN a
fresh one. Pure decision — it reads session mds + telemetry records, no tmux.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker import telemetry as T
from bot_squad_worker.actions import ActionError
from bot_squad_worker.config import Config
from bot_squad_worker.dispatch import decide_dispatch
from bot_squad_worker.sessions import _write_session_metadata


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True)
    (data_dir / "test-project" / "sessions").mkdir(parents=True)
    (data_dir / "test-project" / "vision" / "initiatives").mkdir(parents=True)
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir, tg_bot_token="")


def _make_task(cfg, task_id: str, *, initiative: str = "~") -> None:
    p = cfg.data_dir / "test-project" / "backlog" / f"{task_id}-foo.md"
    p.write_text(
        f"---\nid: {task_id}\ntitle: T\nstatus: open\ninitiative: {initiative}\n---\nbody\n"
    )


def _make_session(cfg, sid, *, window, task_id="~", initiative="~",
                  status="active", archived=None) -> Path:
    meta = {
        "sid": sid, "status": status, "window": window, "cwd": "/tmp",
        "claude_uuid": "uuid-" + sid[-3:], "task_id": task_id,
        "initiative": initiative, "started_at": "2026-05-12T00:00:00Z",
    }
    if archived is not None:
        meta["archived"] = archived
    p = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    _write_session_metadata(p, meta)
    return p


def _set_context_pct(cfg, sid, pct: float) -> None:
    T._write_json(T._record_path(cfg, "test-project", sid), {"context": {"pct": pct}})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: "/home/u")
    # default: idle (no recent transcript activity)
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: None)


# --- the heuristic table ---

def test_reuse_idle_same_initiative_dev_with_headroom(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 12.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "reuse"
    assert res["target_sid"] == "S-u-d1-dev-p1"


def test_spawn_when_task_has_no_initiative(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="~")  # no initiative to match on
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"
    assert "initiative" in res["reason"]


def test_spawn_when_candidate_over_context_threshold(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 92.0)  # above the 80% warn ratio

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_spawn_when_candidate_busy(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 10.0)
    # fresh transcript activity -> busy, not idle
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: 10 ** 12)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_spawn_when_only_different_initiative(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="beta.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 10.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_interface_session_is_not_reused(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    # a TL bound to the initiative — idle, headroom — but it's an interface
    _make_session(cfg, "S-u-tl-p1", window="tl", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-tl-p1", 5.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_suspended_session_is_not_reused(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev",
                  initiative="alpha.md", status="suspended")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 10.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_tie_break_prefers_lowest_context_pct(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _make_session(cfg, "S-u-d2-dev-p2", window="d2-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 60.0)
    _set_context_pct(cfg, "S-u-d2-dev-p2", 20.0)  # more headroom -> winner

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "reuse"
    assert res["target_sid"] == "S-u-d2-dev-p2"


def test_threshold_is_env_tunable(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 50.0)
    monkeypatch.setenv("BOT_SQUAD_REUSE_MAX_CONTEXT_PCT", "40")  # 50 now over cap
    assert decide_dispatch(cfg, "test-project", "T-0009")["decision"] == "spawn"
    monkeypatch.setenv("BOT_SQUAD_REUSE_MAX_CONTEXT_PCT", "70")  # 50 now under cap
    assert decide_dispatch(cfg, "test-project", "T-0009")["decision"] == "reuse"


def test_unknown_task_raises(tmp_path):
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ActionError, match="task not found"):
        decide_dispatch(cfg, "test-project", "T-9999")


def test_missing_telemetry_record_treated_as_headroom(tmp_path):
    """A session with no telemetry record yet (brand-new) is assumed to have
    headroom (pct=0) — a fresh idle session is a fine reuse target."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    # no _set_context_pct -> no record

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "reuse"
    assert res["target_sid"] == "S-u-d1-dev-p1"
