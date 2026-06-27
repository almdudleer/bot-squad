"""T-0466 / M1-F1.3 — ~1h cache-window idle/waiting-session recycle + postpone.

Source: vision/initiatives/process-paradigm.md (SOURCE-VERBATIM Part A) — "it
should get recycled on timeout … cache invalidation timeout … 1 hour. On
timeout, stale waiting sessions should be asked to record their results and
exit. They should be able to postpone this until next timeout … indefinitely …
[and] should postpone if they are actively waiting for some long ongoing
process to finish (e.g. long build)".

The DoD wants worker coverage of three behaviours: TIMEOUT-FIRE (arm + finalize
the record-and-exit handoff), POSTPONE (per-window, repeatable), and
AUTO-POSTPONE (waiting on a tracked long bounded job).
"""
from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import autocompact as A
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import sessions as S


# --- env knobs --------------------------------------------------------------

def test_enabled_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    assert IT.idle_timeout_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT", "0")
    assert IT.idle_timeout_enabled() is False


def test_window_default_and_override(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    assert IT.idle_timeout_sec() == IT.DEFAULT_IDLE_TIMEOUT_SEC == 3600
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "120")
    assert IT.idle_timeout_sec() == 120
    # garbage / non-positive falls back to the default (never collapse to 0)
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "-5")
    assert IT.idle_timeout_sec() == 3600
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "nope")
    assert IT.idle_timeout_sec() == 3600


# --- pure decision helpers --------------------------------------------------

def test_idle_due():
    assert IT.idle_due(3601, 3600) is True
    assert IT.idle_due(3600, 3600) is True
    assert IT.idle_due(3599, 3600) is False
    # unknowable age is conservative — never due
    assert IT.idle_due(None, 3600) is False


def test_postpone_active():
    now = 1000.0
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 500))
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 500))
    assert IT.postpone_active(future, now) is True
    assert IT.postpone_active(past, now) is False
    assert IT.postpone_active(None, now) is False
    assert IT.postpone_active("~", now) is False


# --- cfg + session-md harness (mirrors test_compact_handoff) ----------------

def _make_cfg(tmp_path: Path, *, sid: str, window: str, task_id: str | None,
              extra_md: dict | None = None):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True)
    fm = {"sid": sid, "status": "active", "window": window,
          "cwd": str(repo), "claude_uuid": "uuid-" + sid}
    if task_id:
        fm["task_id"] = task_id
    if extra_md:
        fm.update(extra_md)
    S._write_session_metadata(sess / f"{sid}.md", fm)

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir)
    return patched, data_dir


def _row(sid: str, *, window="demo", task_id="T-0042", cwd_repo: Path,
         status="active"):
    return {"sid": sid, "status": status, "window": window, "task_id": task_id,
            "role": "dev", "cwd": str(cwd_repo), "claude_uuid": "uuid-" + sid,
            "linux_user": ""}


@pytest.fixture
def seams(monkeypatch):
    """Stub every tmux/spawn/artifact seam so no real session is touched."""
    calls = {"handoff": [], "suspend": [], "spawn": [], "orphan": []}
    state = {"pane": "%9", "buf": "❯ ready\n", "artifact_mtime": 100.0,
             "idle_age": 5000.0}
    monkeypatch.setattr(A, "_resolve_role_artifact",
                        lambda cfg, slug, rec: ("/art/T-0042.md", "dev", "T-0042"))
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_inject_handoff",
                        lambda sid, art, role=None: calls["handoff"].append((sid, art, role)))
    monkeypatch.setattr(A, "_artifact_mtime", lambda path: state["artifact_mtime"])
    monkeypatch.setattr(A, "_suspend_session",
                        lambda cfg, slug, sid: calls["suspend"].append(sid))
    monkeypatch.setattr(A, "_relaunch_from_artifact",
                        lambda cfg, slug, rec, art: calls["spawn"].append((rec["sid"], art)))
    monkeypatch.setattr(A, "_alert_orphaned_handoff",
                        lambda cfg, slug, sid, reason: calls["orphan"].append((sid, reason)))
    # idle clock: jsonl mtime = now - idle_age
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - state["idle_age"])
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    return {"calls": calls, "state": state}


# --- A. TIMEOUT-FIRE: arm then finalize the record-and-exit ------------------

def test_arm_injects_handoff_and_stamps_writing(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["handoff"] == [(sid, "/art/T-0042.md", "dev")]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "writing"
    assert meta["idle_recycle_artifact"] == "/art/T-0042.md"
    assert float(meta["idle_recycle_arm_mtime"]) == 100.0


def test_not_due_when_jsonl_fresh(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["idle_age"] = 10.0  # just had a turn → cache warm
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["handoff"] == []


def test_finalize_clears_and_relaunches(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "writing",
                                    "idle_recycle_armed_at": armed,
                                    "idle_recycle_arm_mtime": 100.0,
                                    "idle_recycle_artifact": "/art/T-0042.md"})
    seams["state"]["artifact_mtime"] = 150.0  # the session wrote it
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]
    assert seams["calls"]["spawn"] == [(sid, "/art/T-0042.md")]


def test_finalize_waits_until_artifact_written(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "writing",
                                    "idle_recycle_armed_at": armed,
                                    "idle_recycle_arm_mtime": 100.0,
                                    "idle_recycle_artifact": "/art/T-0042.md"})
    seams["state"]["artifact_mtime"] = 100.0  # unchanged → still waiting
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "writing"  # still armed


def test_finalize_timeout_drops_handoff(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    # armed long ago — past the handoff deadline, never wrote the artifact
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - A.handoff_timeout_sec() - 60))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "writing",
                                    "idle_recycle_armed_at": old,
                                    "idle_recycle_arm_mtime": 100.0,
                                    "idle_recycle_artifact": "/art/T-0042.md"})
    seams["state"]["artifact_mtime"] = 100.0  # never written
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "idle_recycle_phase" not in meta  # dropped → re-arm next window


def test_finalize_relaunch_failure_alerts_operator(tmp_path, seams, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "writing",
                                    "idle_recycle_armed_at": armed,
                                    "idle_recycle_arm_mtime": 100.0,
                                    "idle_recycle_artifact": "/art/T-0042.md"})
    seams["state"]["artifact_mtime"] = 150.0

    def _boom(cfg, slug, rec, art):
        raise RuntimeError("tmux exploded")
    monkeypatch.setattr(A, "_relaunch_from_artifact", _boom)
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["suspend"] == [sid]  # old pane cleared
    assert seams["calls"]["orphan"] and seams["calls"]["orphan"][0][0] == sid


def test_arm_skips_when_pane_not_composer_ready(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["buf"] = "working… esc to interrupt\n"  # mid-turn
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["handoff"] == []


def test_kill_switch_disables_recycle(tmp_path, seams, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT", "0")
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["handoff"] == []


# --- B. POSTPONE: per-window, repeatable ------------------------------------

def test_postpone_skips_the_recycle(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_postpone_until": future})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["handoff"] == []


def test_expired_postpone_lets_recycle_fire_again(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_postpone_until": past})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["handoff"]  # due again — postpone is per-window


def test_set_idle_postpone_default_one_window(tmp_path, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "3600")
    before = time.time()
    out = S.set_idle_postpone(cfg, "bot-squad", sid)
    assert out["ok"] is True and out["seconds"] == 3600
    until = S._parse_ts_epoch(out["postpone_until"])
    assert before + 3600 - 5 <= until <= before + 3600 + 5
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_postpone_until"] == out["postpone_until"]


def test_set_idle_postpone_custom_seconds_is_repeatable(tmp_path):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    out1 = S.set_idle_postpone(cfg, "bot-squad", sid, seconds=120, reason="long build")
    assert out1["seconds"] == 120
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_postpone_reason"] == "long build"
    # repeat — pushes the deadline forward again (unbounded)
    time.sleep(0.01)
    out2 = S.set_idle_postpone(cfg, "bot-squad", sid, seconds=300)
    assert S._parse_ts_epoch(out2["postpone_until"]) >= S._parse_ts_epoch(out1["postpone_until"])


def test_idle_postpone_action_dispatch(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    monkeypatch.setattr(ACT, "_get_config", lambda: cfg)
    out = ACT.dispatch("idle_postpone", {"slug": "bot-squad", "sid": sid})
    assert out["ok"] is True
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "idle_postpone_until" in meta


def test_idle_postpone_action_rejects_extra_and_bad_seconds(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, _ = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    monkeypatch.setattr(ACT, "_get_config", lambda: cfg)
    with pytest.raises(ACT.ActionError, match="unexpected"):
        ACT.dispatch("idle_postpone", {"slug": "bot-squad", "sid": sid, "bogus": 1})
    with pytest.raises(ACT.ActionError, match="integer"):
        ACT.dispatch("idle_postpone", {"slug": "bot-squad", "sid": sid, "seconds": "soon"})


def test_idle_postpone_registered_with_mode():
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY
    assert "idle_postpone" in ACTION_REGISTRY
    assert ACTION_MODES["idle_postpone"] == "tmux_only"


# --- C. AUTO-POSTPONE: waiting on a tracked long bounded job -----------------

def _enqueue_deploy(data_dir: Path, sid: str, *, phase="processing"):
    import json
    d = data_dir / "bot-squad" / "_jobs" / "deploy" / phase
    d.mkdir(parents=True, exist_ok=True)
    (d / "20260627-deploy.json").write_text(json.dumps(
        {"slug": "bot-squad", "target": "staging", "requested_by": sid}))


def test_inflight_deploy_detected(tmp_path):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    assert IT.tracking_long_job(cfg, "bot-squad", sid) is False
    _enqueue_deploy(data, sid, phase="processing")
    assert IT.tracking_long_job(cfg, "bot-squad", sid) is True
    # a deploy requested by SOMEONE ELSE does not auto-postpone us
    assert IT.tracking_long_job(cfg, "bot-squad", "S-other-p1") is False


def test_auto_postpone_skips_recycle_for_inflight_build(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    _enqueue_deploy(data, sid, phase="queue")  # build queued, not yet running
    row = _row(sid, cwd_repo=data.parent / "repo")
    # idle past the window, but waiting on a tracked build → auto-postpone
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["handoff"] == []


def test_recycle_fires_once_build_completes(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    # no deploy in flight → the normal idle window applies and the recycle arms
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["handoff"]


# --- tick: per-project sweep, non-active rows skipped ------------------------

def test_tick_recycles_active_skips_suspended(tmp_path, seams, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    active = _row(sid, cwd_repo=data.parent / "repo", status="active")
    dead = _row("S-almdudleer-bot-squad-old-p9", cwd_repo=data.parent / "repo",
                status="suspended")
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: [active, dead])
    monkeypatch.setattr(S, "_get_user_home", lambda: "/home/x")
    monkeypatch.setattr(S, "_get_current_user", lambda: "almdudleer")
    IT.tick(cfg)
    assert seams["calls"]["handoff"] == [(sid, "/art/T-0042.md", "dev")]
