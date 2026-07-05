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
from bot_squad_worker import lifecycle_events as LE
from bot_squad_worker import sessions as S
# Captured at collection time (before any fixture monkeypatches recycle_gate)
# so the fail-closed subprocess-error test below can restore the REAL
# implementation regardless of what the `seams` fixture stubs it to.
from bot_squad_worker.recycle_gate import is_attached as _REAL_IS_ATTACHED


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
    """Stub every tmux/telemetry/suspend seam so no real session/tmux is
    touched. T-0566: the finalize path no longer writes-to-artifact +
    relaunches — it sends ``/compact`` (over threshold) then terminates
    (``sessions.suspend``) and records resume state."""
    calls = {"compact": [], "terminate": []}
    state = {"pane": "%9", "buf": "❯ ready\n", "idle_age": 5000.0,
             "tokens": 25000}  # default ABOVE the 20k threshold

    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: calls["compact"].append(sid))
    monkeypatch.setattr(IT, "_context_tokens", lambda cfg, slug, sid: state["tokens"])

    def _fake_suspend(cfg, slug, sid, source=None, reason=None):
        calls["terminate"].append(sid)
        md = S._session_file(cfg.data_dir, slug, sid)
        existing = S._read_session_metadata(md) or {}
        meta = {"sid": sid, "status": "suspended",
                "claude_uuid": existing.get("claude_uuid", "~"),
                "task_id": existing.get("task_id", "~"),
                "window": existing.get("window", "~")}
        if source:
            meta["suspend_source"] = source
            meta["suspend_reason"] = reason or source
        S._write_session_metadata(md, meta)
        return {"ok": True, "suspended": True}
    monkeypatch.setattr(S, "suspend", _fake_suspend)

    # T-0563/T-0564: default slug "bot-squad" is already allowlisted; no human
    # is attached in these tests.
    monkeypatch.setattr(IT.recycle_gate, "is_attached", lambda target: False)

    # idle clock: jsonl mtime = now - idle_age
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - state["idle_age"])
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    return {"calls": calls, "state": state}


# --- A. TIMEOUT-FIRE: T-0566 compact-terminate-remember ---------------------

def test_start_sends_compact_and_stamps_compacting_when_over_threshold(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]
    assert seams["calls"]["terminate"] == []  # not yet — awaiting compact completion
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "compacting"
    assert "idle_recycle_armed_at" in meta


def test_start_skips_compact_and_terminates_immediately_below_threshold(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["tokens"] = 5000  # below the 20k default threshold
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == []  # nothing worth compacting
    assert seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["resumable"] is True
    assert "recycled_at" in meta
    assert "no-compact" in meta["resume_hint"]


def test_not_due_when_jsonl_fresh(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["idle_age"] = 10.0  # just had a turn → cache warm
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []
    assert seams["calls"]["terminate"] == []


def test_finalize_terminates_and_records_once_compact_done(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["status"] == "suspended"
    assert meta["resumable"] is True
    assert "recycled_at" in meta
    assert "compacted" in meta["resume_hint"]
    assert "idle_recycle_phase" not in meta  # cleared


def test_finalize_waits_while_pane_not_composer_ready(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    seams["state"]["buf"] = "· Compacting… (esc to interrupt)"  # still compacting
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "compacting"  # still armed


def test_finalize_timeout_terminates_anyway(tmp_path, seams):
    """Never wedge: even if /compact never seems to finish, the bounded wait
    times out and we terminate + record regardless (T-0566 — the recycle must
    always converge to a resumable-suspended state)."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - A.handoff_timeout_sec() - 60))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": old})
    seams["state"]["buf"] = "· Compacting… (esc to interrupt)"  # never returned to ready
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["resumable"] is True


def test_finalize_drops_stamp_when_pane_already_gone(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    seams["state"]["pane"] = None  # the session already exited on its own
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "idle_recycle_phase" not in meta


def test_terminate_failure_is_retried_next_tick(tmp_path, seams, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")

    def _boom(cfg, slug, sid, **kw):
        raise RuntimeError("tmux exploded")
    monkeypatch.setattr(S, "suspend", _boom)
    seams["state"]["tokens"] = 5000  # below threshold → terminate attempted this tick
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta.get("status") == "active"  # untouched — will retry next tick


def test_arm_skips_when_pane_not_composer_ready(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["buf"] = "working… esc to interrupt\n"  # mid-turn
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []
    assert seams["calls"]["terminate"] == []


def test_kill_switch_disables_recycle(tmp_path, seams, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT", "0")
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


# --- A2. T-0616: hand-launched user sessions are never touched ---------------

def test_hand_launched_user_session_never_recycled(tmp_path, seams):
    """p8's exact shape (D-0053 §4): window ``user-session`` derives role
    ``dev``, so the T-0564 role check alone let it ride the full recycle
    path. The window signal must keep every path off it — no compact, no
    terminate, md untouched."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    md = data / "bot-squad" / "sessions" / f"{sid}.md"
    before = md.read_text()
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []
    assert seams["calls"]["terminate"] == []
    assert md.read_text() == before


def test_hand_launched_user_session_stale_phase_never_finalized(tmp_path, seams):
    """Even a stale in-flight phase stamp (a pre-fix leftover) must not route
    an exempt session into the finalize→terminate half."""
    sid = "S-almdudleer-user-session-p8"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []


def test_recycle_exempt_marker_blocks_recycle(tmp_path, seams):
    """An ad-hoc-named hand-launched session is exempted by the explicit
    ``recycle_exempt: true`` md stamp (the hook preserves it, T-0616)."""
    sid = "S-almdudleer-bot-squad-myadhoc-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="myadhoc", task_id=None,
                          extra_md={"recycle_exempt": True})
    row = _row(sid, window="myadhoc", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


# --- B. POSTPONE: per-window, repeatable ------------------------------------

def test_postpone_skips_the_recycle(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_postpone_until": future})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_expired_postpone_lets_recycle_fire_again(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_postpone_until": past})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] or seams["calls"]["terminate"]  # due again — postpone is per-window


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
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_recycle_fires_once_build_completes(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    # no deploy in flight → the normal idle window applies and the recycle arms
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] or seams["calls"]["terminate"]


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
    assert seams["calls"]["compact"] == [sid]


# --- T-0470: hook-driven idle clock + emitted lifecycle events ---------------

def test_idle_age_reads_hook_signal_over_jsonl(tmp_path, seams):
    """The timeout decision reads the HOOK Stop-marker, not the jsonl mtime."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    repo = data.parent / "repo"
    # jsonl says FRESH (10s — not due); hook Stop-marker says idle 4000s (> 1h).
    seams["state"]["idle_age"] = 10.0
    LE.touch_marker(str(repo), sid, LE.MARKER_STOP)
    import os
    anchor = time.time() - 4000
    os.utime(LE.marker_path(str(repo), sid, LE.MARKER_STOP), (anchor, anchor))
    row = _row(sid, cwd_repo=repo)
    # Despite the fresh jsonl, the hook signal drives the decision → it ARMS.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]


def test_active_marker_keeps_session_busy(tmp_path, seams):
    """A .active marker newer than .stop = turn in progress → NOT due."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    repo = data.parent / "repo"
    seams["state"]["idle_age"] = 10.0  # jsonl irrelevant once a hook signal exists
    import os
    LE.touch_marker(str(repo), sid, LE.MARKER_STOP)
    anchor = time.time() - 4000
    os.utime(LE.marker_path(str(repo), sid, LE.MARKER_STOP), (anchor, anchor))
    LE.touch_marker(str(repo), sid, LE.MARKER_ACTIVE)  # newer → busy
    row = _row(sid, cwd_repo=repo)
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_arm_emits_session_timeout_event(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    doc = LE.read_events(cfg, "bot-squad", sid)
    assert doc.get("counts", {}).get(LE.SESSION_TIMEOUT) == 1
    assert doc["last"][LE.SESSION_TIMEOUT]["reason"] == "idle_window"


def test_finalize_emits_session_recycled_event(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    doc = LE.read_events(cfg, "bot-squad", sid)
    assert doc.get("counts", {}).get(LE.SESSION_RECYCLED) == 1
    assert doc["last"][LE.SESSION_RECYCLED]["cause"] == "idle_timeout"


# --- D. T-0563/T-0564: recycle-v2 gates, exercised through maybe_recycle -----

def test_non_allowlisted_project_never_touched_by_default_allowlist(tmp_path, seams):
    """T-0563: the 2026-06-29 incident's fix — a session in a non-allowlisted
    project is NEVER touched by idle_timeout (watchrobot itself joined the
    default allowlist in T-0613, so the example is another slug)."""
    sid = "S-almdudleer-lim-finance-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    # seed the session under a DIFFERENT (non-allowlisted) project slug
    sess = data / "lim-finance" / "sessions"
    sess.mkdir(parents=True)
    S._write_session_metadata(sess / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "demo",
        "cwd": str(data.parent / "repo"), "claude_uuid": "uuid-" + sid,
        "task_id": "T-0042"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "lim-finance", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_env_override_allowlists_watchrobot(tmp_path, seams, monkeypatch):
    """T-0563: BOT_SQUAD_RECYCLE_PROJECTS opts a project in explicitly."""
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "watchrobot")
    sid = "S-almdudleer-watchrobot-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    sess = data / "watchrobot" / "sessions"
    sess.mkdir(parents=True)
    S._write_session_metadata(sess / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "demo",
        "cwd": str(data.parent / "repo"), "claude_uuid": "uuid-" + sid,
        "task_id": "T-0042"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "watchrobot", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]


def test_config_recycle_projects_fallback_allows_watchrobot(tmp_path, seams):
    """T-0563: system_settings.toml [recycle].projects extends the allowlist
    when no env override is set."""
    sid = "S-almdudleer-watchrobot-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    cfg.recycle_projects = ("bot-squad", "watchrobot")
    sess = data / "watchrobot" / "sessions"
    sess.mkdir(parents=True)
    S._write_session_metadata(sess / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "demo",
        "cwd": str(data.parent / "repo"), "claude_uuid": "uuid-" + sid,
        "task_id": "T-0042"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "watchrobot", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]


def test_user_conversation_role_never_recycled(tmp_path, seams):
    """T-0564: the human's own live chat is never auto-recycled, even when
    idle past the window and in an allowlisted project."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"role": "user-conversation"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    row["role"] = "user-conversation"
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_attached_session_never_recycled(tmp_path, seams, monkeypatch):
    """T-0564: a human tmux client attached to the pane blocks the recycle even
    in an allowlisted project with a non-exempt role."""
    monkeypatch.setattr(IT.recycle_gate, "is_attached", lambda target: True)
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_attached_check_failure_skips_recycle(tmp_path, seams, monkeypatch):
    """T-0564: a tmux list-clients error fails CLOSED (treated as attached)."""
    # exercise the REAL is_attached (undoing the seams fixture's stub) to prove
    # the fail-closed subprocess-error path, routed through the gate.
    monkeypatch.setattr(IT.recycle_gate, "is_attached", _REAL_IS_ATTACHED)
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("tmux not found")))
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []
