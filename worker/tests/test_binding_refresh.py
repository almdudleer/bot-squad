"""Tests for the T-0142 binding-refresh + auto-archive reconcilers."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker.sessions import (
    PaneInfo,
    gc_dead_bindings,
    archive_dead_teammates,
    _write_session_metadata,
)


def _make_cfg(tmp_path: Path) -> Any:
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "backlog").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "vision" / "initiatives").mkdir(parents=True, exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{tmp_path / "repo"}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    import types
    return types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir, tg_bot_token="")


def _seed_session(cfg, sid, **fields) -> Path:
    meta = {"sid": sid, "status": "active"}
    meta.update(fields)
    p = S._session_file(cfg.data_dir, "test-project", sid)
    _write_session_metadata(p, meta)
    return p


def _seed_task(cfg, task_id, status) -> None:
    p = cfg.data_dir / "test-project" / "backlog" / f"{task_id}-thing.md"
    p.write_text(f"---\nid: {task_id}\nstatus: {status}\n---\nbody\n")


def _seed_initiative(cfg, name) -> None:
    (cfg.data_dir / "test-project" / "vision" / "initiatives" / name).write_text(
        "---\nname: x\n---\n")


@pytest.fixture(autouse=True)
def _user(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [])


# --- gc_dead_bindings ---

def test_closed_task_binding_is_cleared(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "closed")
    p = _seed_session(cfg, "S-u-dev-p1", window="dev", task_id="T-0001", status="suspended")
    res = gc_dead_bindings(cfg, "test-project")
    assert res["cleared"] == 1
    meta = S._read_session_metadata(p)
    assert meta["task_id"] is None  # ~ parses to None
    assert meta["last_task_id"] == "T-0001"


def test_missing_task_binding_is_cleared(tmp_path):
    cfg = _make_cfg(tmp_path)  # no task file at all
    p = _seed_session(cfg, "S-u-dev-p1", window="dev", task_id="T-0099")
    res = gc_dead_bindings(cfg, "test-project")
    assert res["cleared"] == 1
    assert S._read_session_metadata(p)["task_id"] is None


def test_open_task_binding_is_preserved(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    p = _seed_session(cfg, "S-u-dev-p1", window="dev", task_id="T-0001")
    res = gc_dead_bindings(cfg, "test-project")
    assert res["cleared"] == 0
    assert S._read_session_metadata(p)["task_id"] == "T-0001"


def test_totest_task_binding_is_preserved(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "totest")
    p = _seed_session(cfg, "S-u-dev-p1", window="dev", task_id="T-0001")
    gc_dead_bindings(cfg, "test-project")
    assert S._read_session_metadata(p)["task_id"] == "T-0001"


def test_missing_initiative_is_cleared(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = _seed_session(cfg, "S-u-TL-p1", window="TL", task_id="~",
                      initiative="gone.md")
    res = gc_dead_bindings(cfg, "test-project")
    assert res["cleared"] == 1
    meta = S._read_session_metadata(p)
    assert meta["initiative"] is None
    assert meta["last_initiative"] == "gone.md"


def test_present_initiative_is_preserved(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_initiative(cfg, "live.md")
    p = _seed_session(cfg, "S-u-TL-p1", window="TL", task_id="~", initiative="live.md")
    gc_dead_bindings(cfg, "test-project")
    assert S._read_session_metadata(p)["initiative"] == "live.md"


def test_extra_task_ids_pruned_of_closed(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    _seed_task(cfg, "T-0002", "closed")
    p = _seed_session(cfg, "S-u-dev-p1", window="dev", task_id="T-0001",
                      extra_task_ids=["T-0002"])
    gc_dead_bindings(cfg, "test-project")
    assert S._read_session_metadata(p)["extra_task_ids"] == []


# --- archive_dead_teammates ---

def test_exited_totest_dev_is_archived(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "totest")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001")
    # no live panes -> exited
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    meta = S._read_session_metadata(p)
    assert str(meta["archived"]).lower() == "true"
    assert meta["status"] == "suspended"
    assert meta["task_id"] is None
    assert meta["last_task_id"] == "T-0001"


def test_exited_in_progress_dev_is_not_archived(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001")
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0  # crashed-but-resumable, left for gc_sessions
    assert "archived" not in S._read_session_metadata(p)


def test_live_totest_dev_is_not_force_suspended(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "totest")
    _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001")
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%1", window="feat-dev", pid="1", cwd="/x", command="claude")],
    )
    suspended = []
    monkeypatch.setattr(S, "suspend", lambda *a, **k: suspended.append(a))
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0
    assert suspended == []  # live totest dev left alone


def test_live_closed_dev_is_suspended_and_archived(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "closed")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001")
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%1", window="feat-dev", pid="1", cwd="/x", command="claude")],
    )
    suspended = []
    monkeypatch.setattr(S, "suspend", lambda cfg, slug, sid: suspended.append(sid))
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    assert suspended == ["S-u-feat-dev-p1"]
    assert str(S._read_session_metadata(p)["archived"]).lower() == "true"


# --- T-0202: live dev whose binding was already cleared (task_id ~) ---

def _live_feat_dev_pane(monkeypatch):
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%1", window="feat-dev", pid="1", cwd="/x", command="claude")],
    )


def test_live_dev_last_task_closed_is_archived(tmp_path, monkeypatch):
    """T-0202 regression: gc_dead_bindings already stripped task_id to ~ in the
    same tick; the live idle dev must still be trimmed via last_task_id."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "closed")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="~",
                      last_task_id="T-0001")
    _live_feat_dev_pane(monkeypatch)
    suspended = []
    monkeypatch.setattr(S, "suspend", lambda cfg, slug, sid: suspended.append(sid))
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    assert suspended == ["S-u-feat-dev-p1"]
    meta = S._read_session_metadata(p)
    assert str(meta["archived"]).lower() == "true"
    assert meta["status"] == "suspended"
    assert meta["archive_reason"] == "auto-archive:live-last-closed"
    assert meta["last_task_id"] == "T-0001"  # preserved through suspend's rewrite


def test_live_dev_last_task_totest_is_not_archived(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "totest")
    _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="~",
                  last_task_id="T-0001")
    _live_feat_dev_pane(monkeypatch)
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0  # TL may still be iterating review


def test_live_dev_last_task_closed_but_pane_busy_is_not_archived(tmp_path, monkeypatch):
    """Idle guard: a pane with fresh jsonl activity is never trimmed mid-write."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "closed")
    _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="~",
                  last_task_id="T-0001", claude_uuid="u" * 8)
    _live_feat_dev_pane(monkeypatch)
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time())
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


def test_live_dev_last_task_closed_but_live_extra_task_is_not_archived(tmp_path, monkeypatch):
    """A surviving extra_task_ids entry is real remaining work (closed extras
    were already pruned by gc_dead_bindings) — never trim."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "closed")
    _seed_task(cfg, "T-0002", "in_progress")
    _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="~",
                  last_task_id="T-0001", extra_task_ids=["T-0002"])
    _live_feat_dev_pane(monkeypatch)
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


def test_same_tick_close_then_trim(tmp_path, monkeypatch):
    """End-to-end intra-tick order (the T-0202 bug): gc_dead_bindings strips the
    just-closed binding, then archive_dead_teammates trims in the SAME tick."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "closed")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001")
    _live_feat_dev_pane(monkeypatch)
    monkeypatch.setattr(S, "suspend", lambda cfg, slug, sid: None)
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    assert gc_dead_bindings(cfg, "test-project")["cleared"] == 1
    assert S._read_session_metadata(p)["task_id"] is None
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    meta = S._read_session_metadata(p)
    assert meta["archive_reason"] == "auto-archive:live-last-closed"
    assert meta["last_task_id"] == "T-0001"


# --- T-0335 item-10 / T-0288: idle-but-live dev suspend (Fork-2 Part B; T-0288
# reconciled the default to the roadmap Ch. III HARD 12h spec) ---

def _seed_idle_live_dev(cfg, monkeypatch, *, status="in_progress"):
    _seed_task(cfg, "T-0001", status)
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001",
                      claude_uuid="u" * 8)
    _live_feat_dev_pane(monkeypatch)
    return p


def test_idle_live_dev_suspended_by_default_past_12h(tmp_path, monkeypatch):
    """T-0288: no env, no [caps] override → the idle-suspend arm now defaults
    to the Ch. III HARD 12h window (was DARK/opt-in per T-0335 item-10 D2).
    An open-task dev idle for 13h is reaped."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    p = _seed_idle_live_dev(cfg, monkeypatch, status="open")
    monkeypatch.delenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", raising=False)
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time() - 13 * 3600)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    monkeypatch.setattr(S, "suspend", lambda cfg, slug, sid: None)
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    meta = S._read_session_metadata(p)
    assert meta["archive_reason"] == "auto-archive:idle-suspend"


def test_idle_live_dev_not_suspended_before_default_12h(tmp_path, monkeypatch):
    """Default window is 12h, not 0 — a dev idle for only 1h is still spared."""
    cfg = _make_cfg(tmp_path)
    import time as _time
    _seed_idle_live_dev(cfg, monkeypatch, status="open")
    monkeypatch.delenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", raising=False)
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time() - 3600)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


def test_idle_live_dev_not_suspended_when_explicitly_disabled(tmp_path, monkeypatch):
    """An operator who explicitly writes [caps].idle_suspend_sec = 0 in System
    Settings still gets a genuine OFF — explicit intent beats the 12h default."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    _seed_idle_live_dev(cfg, monkeypatch, status="open")
    monkeypatch.delenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", raising=False)
    _write_caps(cfg, idle_suspend_sec=0)
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time() - 13 * 3600)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


def test_idle_live_dev_suspended_when_knob_set(tmp_path, monkeypatch):
    """Knob>0 + pane idle past the window + not awaiting input → suspend+archive
    with reason idle-suspend; the binding is preserved as last_task_id and the
    task stays open (re-dispatchable, kill-not-resume).

    T-0426: an idle dev on an OPEN (not yet started) task is still reaped — only
    an in_progress claim is spared (see the in_progress test below)."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    p = _seed_idle_live_dev(cfg, monkeypatch, status="open")
    monkeypatch.setenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", "3600")
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time() - 7200)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    suspended = []
    monkeypatch.setattr(S, "suspend", lambda cfg, slug, sid: suspended.append(sid))
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    assert suspended == ["S-u-feat-dev-p1"]
    meta = S._read_session_metadata(p)
    assert meta["status"] == "suspended"
    assert meta["archive_reason"] == "auto-archive:idle-suspend"
    assert meta["last_task_id"] == "T-0001"
    assert meta["task_id"] is None  # "~" round-trips through the serializer as null
    # the task itself is untouched → stays open and re-dispatchable
    assert S._task_status(cfg.data_dir, "test-project", "T-0001") == "open"


def test_idle_live_in_progress_dev_is_spared(tmp_path, monkeypatch):
    """T-0426: an idle-but-live dev that OWNS an in_progress ticket is NOT
    idle-suspended — that strands the ticket (in_progress, owner='-', no
    auto-re-dispatch). in_progress is the explicit 'actively working' claim;
    only open/planned idle devs are reaped. Even with the knob on + pane idle."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    p = _seed_idle_live_dev(cfg, monkeypatch, status="in_progress")
    monkeypatch.setenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", "3600")
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time() - 7200)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0
    meta = S._read_session_metadata(p)
    assert meta.get("status") != "suspended"
    assert S._task_status(cfg.data_dir, "test-project", "T-0001") == "in_progress"


def test_idle_live_dev_spared_when_awaiting_input(tmp_path, monkeypatch):
    """A dev blocked on TG input (blocked_sids) is NOT idle-leaking — spared."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    _seed_idle_live_dev(cfg, monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", "3600")
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time() - 7200)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids",
                        lambda cfg, slug: {"S-u-feat-dev-p1"})
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


def test_idle_live_dev_spared_when_pane_busy(tmp_path, monkeypatch):
    """Fresh jsonl activity → not idle → spared even with the knob on."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    _seed_idle_live_dev(cfg, monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", "3600")
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time())
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


def test_idle_live_dev_spared_when_no_activity_signal(tmp_path, monkeypatch):
    """No transcript yet (activity None, e.g. a freshly spawned pane) → spared,
    never suspend a session whose age we cannot positively establish."""
    cfg = _make_cfg(tmp_path)
    _seed_idle_live_dev(cfg, monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", "3600")
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: None)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


def _write_caps(cfg, **caps):
    """Write a [caps] block to the cfg's system_settings.toml (the dir
    _caps_config_dir resolves — data_dir.parent/config for this test cfg)."""
    cfg_dir = cfg.data_dir.parent / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    body = "[caps]\n" + "".join(f"{k} = {v}\n" for k, v in caps.items())
    (cfg_dir / "system_settings.toml").write_text(body)


def test_idle_suspend_sec_reads_from_caps_when_env_unset(tmp_path, monkeypatch):
    """T-0408: the idle-suspend knob now lives in system_settings [caps]
    (the System Settings UI), read fresh like the other caps."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.delenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", raising=False)
    _write_caps(cfg, idle_suspend_sec=3600)
    assert S._session_idle_suspend_sec(cfg) == 3600


def test_idle_suspend_sec_missing_cap_defaults_to_12h(tmp_path, monkeypatch):
    """T-0288: an ABSENT cap (no system_settings.toml, or [caps] without the
    key) falls through to the Ch. III HARD spec default (12h), not OFF."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.delenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", raising=False)
    assert S._session_idle_suspend_sec(cfg) == S.DEFAULT_IDLE_SUSPEND_SEC == 12 * 3600
    _write_caps(cfg, max_parallel_sessions=5)  # [caps] present, key still absent
    assert S._session_idle_suspend_sec(cfg) == S.DEFAULT_IDLE_SUSPEND_SEC


def test_idle_suspend_sec_explicit_zero_cap_is_off(tmp_path, monkeypatch):
    """An EXPLICIT idle_suspend_sec = 0 is a deliberate operator override — OFF
    is honored, it does not fall back to the 12h default."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.delenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", raising=False)
    _write_caps(cfg, idle_suspend_sec=0)
    assert S._session_idle_suspend_sec(cfg) == 0.0


def test_idle_suspend_sec_positive_env_overrides_caps(tmp_path, monkeypatch):
    """A positive env var force-overrides (dev/emergency); 0/unset falls through
    to caps so a leftover dark-ship =0 can't shadow the UI setting."""
    cfg = _make_cfg(tmp_path)
    _write_caps(cfg, idle_suspend_sec=3600)
    monkeypatch.setenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", "1800")
    assert S._session_idle_suspend_sec(cfg) == 1800
    monkeypatch.setenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", "0")  # 0 → fall to caps
    assert S._session_idle_suspend_sec(cfg) == 3600


def test_idle_suspend_arm_driven_by_caps(tmp_path, monkeypatch):
    """End-to-end: knob set ONLY in [caps] (no env) → the idle-suspend arm fires
    on an idle open-task dev."""
    import time as _time
    cfg = _make_cfg(tmp_path)
    _seed_idle_live_dev(cfg, monkeypatch, status="open")
    monkeypatch.delenv("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC", raising=False)
    _write_caps(cfg, idle_suspend_sec=3600)
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: _time.time() - 7200)
    monkeypatch.setattr("bot_squad_worker.tg_stall.blocked_sids", lambda cfg, slug: set())
    monkeypatch.setattr(S, "suspend", lambda cfg, slug, sid: None)
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1


def test_tl_role_is_never_auto_archived(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_session(cfg, "S-u-feat-TL-p1", window="feat-TL", task_id="~",
                  initiative="x.md")
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0


# --- T-0211: archiving a dead dev must not kill a live same-named sibling ---

def test_archive_dead_dev_does_not_kill_live_same_named_sibling(tmp_path, monkeypatch):
    """T-0211 root cause: a dead predecessor (p87) and a LIVE different dev
    (p109) share the same window name in the same tmux session — the exact
    shape produced by respawning the same ticket into a per-initiative session
    (window name is derived from the ticket slug, so it recurs). The buggy
    "kill lingering window" block matched the pane to kill by window-name +
    tmux-session instead of by the archived session's own SID/pane_id, so
    archiving the dead p87 killed the live p109's pane. p109 then self-exited
    ~30-60s after spawn — the reported cascade.
    """
    cfg = _make_cfg(tmp_path)
    # Dead predecessor: no live pane, task-less -> rule-1 "exited-no-task".
    _seed_session(cfg, "S-u-route-dev-p87", window="route-dev",
                  task_id="~", tmux_session="proj-init")
    # Live successor: same window name + tmux session, different pane id; a
    # healthy in-progress dev that must be left completely untouched.
    _seed_task(cfg, "T-0207", "in_progress")
    live = _seed_session(cfg, "S-u-route-dev-p109", window="route-dev",
                         task_id="T-0207", tmux_session="proj-init")
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%109", window="route-dev", pid="1",
                          cwd="/x", command="claude", session="proj-init")],
    )
    runs: list = []
    monkeypatch.setattr(S, "_run", lambda args, **k: runs.append(args))
    res = archive_dead_teammates(cfg, "test-project")

    # The dead predecessor IS archived (correct, expected behaviour).
    assert "S-u-route-dev-p87" in res["sids"]
    # ...but the LIVE sibling's pane must NEVER be killed.
    kills = [a for a in runs if any("kill" in str(tok) for tok in a)]
    assert not any("%109" in a for a in kills), (
        f"live sibling pane %109 was killed by archiving its dead twin: {kills}")
    # And the live sibling's md is untouched (still active, not archived).
    lmeta = S._read_session_metadata(live)
    assert lmeta["status"] == "active"
    assert "archived" not in lmeta


# --- T-0233: aggressive stale-session GC ---
#
# Paradigm reframe: a session is a one-time run for a specific task. An exited
# (pane-gone) dev whose task is still open but which has been dead/idle past the
# staleness grace is an abandoned/crashed run — reap it (preserving the binding
# as last_task_id so the still-open task stays re-dispatchable) instead of
# letting it linger forever in the working set.

def _seed_old_ts(seconds_ago: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - seconds_ago))


def test_exited_in_progress_dev_stale_is_archived(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    # exited (no live pane), suspended long ago -> stale.
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001",
                      status="suspended", suspended_at=_seed_old_ts(48 * 3600))
    monkeypatch.setenv("BOT_SQUAD_SESSION_STALE_SEC", str(24 * 3600))
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    meta = S._read_session_metadata(p)
    assert str(meta["archived"]).lower() == "true"
    assert meta["status"] == "suspended"
    assert meta["archive_reason"] == "auto-archive:exited-stale"
    # binding freed but preserved so the still-open task is re-dispatchable.
    assert meta["task_id"] is None
    assert meta["last_task_id"] == "T-0001"
    # the task itself is untouched (still open, not closed by the reaper).
    assert S._task_status(cfg.data_dir, "test-project", "T-0001") == "in_progress"


def test_exited_in_progress_dev_fresh_is_not_archived(tmp_path, monkeypatch):
    """Within the grace window a crashed dev is still resumable -> not reaped."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001",
                      status="suspended", suspended_at=_seed_old_ts(120))
    monkeypatch.setenv("BOT_SQUAD_SESSION_STALE_SEC", str(24 * 3600))
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0
    assert "archived" not in S._read_session_metadata(p)


def test_exited_in_progress_dev_no_timestamp_is_not_archived(tmp_path, monkeypatch):
    """A session whose age cannot be determined is never stale-reaped
    (conservative: only reap a run we can positively prove is old)."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001")
    monkeypatch.setenv("BOT_SQUAD_SESSION_STALE_SEC", "1")
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0
    assert "archived" not in S._read_session_metadata(p)


def test_stale_reap_respects_env_threshold(tmp_path, monkeypatch):
    """The grace is env-tunable; a moderately-aged session is reaped only once
    the threshold drops below its age."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001",
                      status="suspended", suspended_at=_seed_old_ts(3600))
    monkeypatch.setenv("BOT_SQUAD_SESSION_STALE_SEC", str(2 * 3600))
    assert archive_dead_teammates(cfg, "test-project")["archived"] == 0
    monkeypatch.setenv("BOT_SQUAD_SESSION_STALE_SEC", str(600))
    assert archive_dead_teammates(cfg, "test-project")["archived"] == 1
    assert S._read_session_metadata(p)["archive_reason"] == "auto-archive:exited-stale"


def test_stale_reap_never_touches_tl_interface_session(tmp_path, monkeypatch):
    """An ancient operator/TL interface session is never stale-reaped, no matter
    how old (DoD: never kill an interface process)."""
    cfg = _make_cfg(tmp_path)
    p = _seed_session(cfg, "S-u-feat-TL-p1", window="feat-TL", task_id="~",
                      initiative="x.md", status="suspended",
                      suspended_at=_seed_old_ts(365 * 24 * 3600))
    _seed_initiative(cfg, "x.md")
    monkeypatch.setenv("BOT_SQUAD_SESSION_STALE_SEC", "1")
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0
    assert "archived" not in S._read_session_metadata(p)


def test_live_in_progress_dev_is_never_stale_reaped(tmp_path, monkeypatch):
    """A LIVE pane (working session) is never stale-reaped even with an ancient
    timestamp — only exited (pane-gone) runs are eligible."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "in_progress")
    p = _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001",
                      suspended_at=_seed_old_ts(365 * 24 * 3600))
    _live_feat_dev_pane(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_SESSION_STALE_SEC", "1")
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 0
    assert "archived" not in S._read_session_metadata(p)


# --- T-0288: reaper run log ("N processes reaped today") ---

def test_archive_writes_reap_log_entry(tmp_path):
    """Every sid archive_dead_teammates archives is logged; reaped_today counts
    it the same tick."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", "totest")
    _seed_session(cfg, "S-u-feat-dev-p1", window="feat-dev", task_id="T-0001")
    res = archive_dead_teammates(cfg, "test-project")
    assert res["archived"] == 1
    log_path = cfg.data_dir / "test-project" / "_worker" / "reaper"
    files = list(log_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(ln) for ln in files[0].read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["sid"] == "S-u-feat-dev-p1"
    assert lines[0]["reason"] == "exited-totest"
    assert S.reaped_today(cfg, "test-project") == 1


def test_reaped_today_excludes_yesterday(tmp_path):
    cfg = _make_cfg(tmp_path)
    now = time.time()
    yesterday = now - 26 * 3600
    S._log_reap_event(cfg, "test-project", "S-u-a-p1", "exited-stale", now=yesterday)
    S._log_reap_event(cfg, "test-project", "S-u-b-p2", "idle-suspend", now=now)
    assert S.reaped_today(cfg, "test-project", now=now) == 1


def test_reaped_today_zero_when_no_log(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert S.reaped_today(cfg, "test-project") == 0


def test_reaped_since_spans_multiple_day_files(tmp_path):
    cfg = _make_cfg(tmp_path)
    now = time.time()
    two_days_ago = now - 2 * 24 * 3600
    S._log_reap_event(cfg, "test-project", "S-u-a-p1", "exited-stale", now=two_days_ago)
    S._log_reap_event(cfg, "test-project", "S-u-b-p2", "idle-suspend", now=now)
    assert S.reaped_since(cfg, "test-project", two_days_ago, now=now) == 2
    assert S.reaped_since(cfg, "test-project", now - 3600, now=now) == 1


def test_reap_log_concurrent_appends_do_not_interleave(tmp_path):
    """Two archived sids in the same tick both land as clean, separate lines
    (the flock append must not interleave partial writes)."""
    cfg = _make_cfg(tmp_path)
    now = time.time()
    for i in range(20):
        S._log_reap_event(cfg, "test-project", f"S-u-dev-p{i}", "exited-stale", now=now)
    assert S.reaped_since(cfg, "test-project", now - 1, now=now) == 20
