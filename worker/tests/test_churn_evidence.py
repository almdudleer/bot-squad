"""T-0613 — session-churn evidence collector + gate verdicts + churn metrics.

Covers the three halves of :mod:`bot_squad_worker.churn_evidence`:
  * collect() — merging _worker/lifecycle/*.json engine events with sessions/*.md
    registry transitions into one chronological churn log;
  * verdicts() — the stakeholder's T-0612 §0 gate semantics: (a) compact-once
    (one timeout-arm per recycle episode), (b) no auto-incarnation respawn
    (operator_redrive is the only sanctioned recycle→spawn chain), (c) no
    runaway spawn / token leak (spawn-rate bound + no re-compact loop);
  * spawn_count() / live_session_count() — the standing churn metrics the
    T-0613 monitors ride.

Automated AFTER the manual walkthrough (scenarios/T-0613, 2026-07-05) — the
fixture shapes mirror the REAL lifecycle docs and session mds observed live
(p8/p11/p23/p29), including the double-arm episodes the walkthrough caught.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from pathlib import Path

from bot_squad_worker import churn_evidence as C

SLUG = "test-project"


def _cfg(tmp_path: Path):
    return types.SimpleNamespace(data_dir=tmp_path / "data", projects={SLUG: {}})


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def _write_session_md(cfg, sid: str, *, window: str, status: str = "active",
                      started_at: str = "~", suspended_at: str | None = None,
                      suspend_source: str | None = None, owner: str = "~"):
    d = cfg.data_dir / SLUG / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"sid: {sid}",
        f"status: {status}",
        f"window: {window}",
        f"started_at: {started_at}",
        f"owner: {owner}",
    ]
    if suspended_at:
        lines.append(f"suspended_at: {suspended_at}")
    if suspend_source:
        lines.append(f"suspend_source: {suspend_source}")
    lines += ["---", ""]
    (d / f"{sid}.md").write_text("\n".join(lines))


def _write_lifecycle(cfg, sid: str, history: list[dict]):
    d = cfg.data_dir / SLUG / "_worker" / "lifecycle"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps({
        "sid": sid,
        "history": list(reversed(history)),  # live docs are most-recent-first
    }))


def _timeout(at: str) -> dict:
    return {"event": "session_timeout", "at": at, "epoch": _epoch(at),
            "reason": "idle_window"}


def _recycled(at: str, *, compacted: bool = True) -> dict:
    return {"event": "session_recycled", "at": at, "epoch": _epoch(at),
            "cause": "idle_timeout", "compacted": compacted}


# --- collect() ---------------------------------------------------------------

def test_collect_merges_sources_chronologically(tmp_path):
    cfg = _cfg(tmp_path)
    _write_session_md(cfg, "S-u-operator-p11", window="operator",
                      status="suspended", started_at="2026-07-04T19:47:35Z",
                      suspended_at="2026-07-04T23:23:23Z",
                      suspend_source="idle_timeout", owner="operator-redrive")
    _write_session_md(cfg, "S-u-operator-p23", window="operator",
                      started_at="2026-07-04T23:24:23Z", owner="operator-redrive")
    _write_lifecycle(cfg, "S-u-operator-p11", [
        _timeout("2026-07-04T23:19:16Z"),
        _timeout("2026-07-04T23:22:16Z"),
        _recycled("2026-07-04T23:23:16Z"),
    ])
    events = C.collect(cfg, SLUG, since=_epoch("2026-07-04T00:00:00Z"),
                       until=_epoch("2026-07-05T00:00:00Z"))
    kinds = [(e["kind"], e["sid"]) for e in events]
    assert kinds == [
        ("session_spawned", "S-u-operator-p11"),
        ("session_timeout", "S-u-operator-p11"),
        ("session_timeout", "S-u-operator-p11"),
        ("session_recycled", "S-u-operator-p11"),
        ("session_suspended", "S-u-operator-p11"),
        ("session_spawned", "S-u-operator-p23"),
    ]
    # epochs strictly non-decreasing
    epochs = [e["epoch"] for e in events]
    assert epochs == sorted(epochs)
    # md-derived events carry window + attribution fields
    spawned = events[0]
    assert spawned["window"] == "operator"
    assert spawned["owner"] == "operator-redrive"
    suspended = events[4]
    assert suspended["source"] == "idle_timeout"


def test_collect_honors_window_and_skips_unset_timestamps(tmp_path):
    cfg = _cfg(tmp_path)
    _write_session_md(cfg, "S-u-old-p1", window="old",
                      started_at="2026-07-01T00:00:00Z")
    _write_session_md(cfg, "S-u-unset-p2", window="unset")  # started_at: ~
    events = C.collect(cfg, SLUG, since=_epoch("2026-07-04T00:00:00Z"),
                       until=_epoch("2026-07-05T00:00:00Z"))
    assert events == []


def test_collect_survives_corrupt_lifecycle_doc(tmp_path):
    cfg = _cfg(tmp_path)
    d = cfg.data_dir / SLUG / "_worker" / "lifecycle"
    d.mkdir(parents=True)
    (d / "S-u-bad-p9.json").write_text("{not json")
    _write_lifecycle(cfg, "S-u-ok-p1", [_timeout("2026-07-04T23:19:16Z")])
    events = C.collect(cfg, SLUG, since=_epoch("2026-07-04T00:00:00Z"),
                       until=_epoch("2026-07-05T00:00:00Z"))
    assert [e["sid"] for e in events] == ["S-u-ok-p1"]


# --- verdict (a): compact fires once per recycle episode ---------------------

def test_verdict_a_pass_single_arm_episode(tmp_path):
    cfg = _cfg(tmp_path)
    _write_lifecycle(cfg, "S-u-w-p1", [
        _timeout("2026-07-05T01:00:00Z"),
        _recycled("2026-07-05T01:01:00Z"),
    ])
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-06T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T02:00:00Z"))
    assert v["a"]["pass"] is True
    assert v["a"]["violations"] == []


def test_verdict_a_fail_double_arm_episode(tmp_path):
    # the live p11/p23/p29 shape: TWO timeout arms (≈ two /compact sends)
    # closed by one recycle
    cfg = _cfg(tmp_path)
    _write_lifecycle(cfg, "S-u-operator-p11", [
        _timeout("2026-07-04T23:19:16Z"),
        _timeout("2026-07-04T23:22:16Z"),
        _recycled("2026-07-04T23:23:16Z"),
    ])
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-05T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T00:00:00Z"))
    assert v["a"]["pass"] is False
    assert any("S-u-operator-p11" in x and "2 timeout arms" in x
               for x in v["a"]["violations"])


def test_verdict_a_arms_a_window_apart_are_separate_episodes(tmp_path):
    # the live p8 shape: arms 7h apart are SEPARATE recycle attempts (each a
    # fresh idle window), not one double-compact episode — (a) stays clean;
    # the repetition is (c)'s re-compact-loop finding instead
    cfg = _cfg(tmp_path)
    _write_lifecycle(cfg, "S-u-user-session-p8", [
        _timeout("2026-07-04T23:15:16Z"),
        _timeout("2026-07-05T06:25:16Z"),
    ])
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-06T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T07:00:00Z"))
    assert v["a"]["pass"] is True
    assert v["c"]["pass"] is False


# --- verdict (b): no auto-incarnation respawn --------------------------------

def test_verdict_b_operator_redrive_is_sanctioned(tmp_path):
    cfg = _cfg(tmp_path)
    _write_session_md(cfg, "S-u-operator-p11", window="operator",
                      status="suspended", started_at="2026-07-04T19:47:35Z",
                      suspended_at="2026-07-04T23:23:23Z",
                      suspend_source="idle_timeout")
    _write_session_md(cfg, "S-u-operator-p23", window="operator",
                      started_at="2026-07-04T23:24:23Z", owner="operator-redrive")
    _write_lifecycle(cfg, "S-u-operator-p11", [
        _timeout("2026-07-04T23:22:16Z"), _recycled("2026-07-04T23:23:16Z")])
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-05T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T00:00:00Z"))
    assert v["b"]["pass"] is True
    assert any("operator-redrive" in s for s in v["b"]["sanctioned"])


def test_verdict_b_fail_unattributed_respawn_after_recycle(tmp_path):
    # a recycle-terminated window respawned with NO operator-redrive owner =
    # the forbidden "new incarnation"
    cfg = _cfg(tmp_path)
    _write_session_md(cfg, "S-u-mytask-p5", window="mytask", status="suspended",
                      started_at="2026-07-04T20:00:00Z",
                      suspended_at="2026-07-04T23:23:23Z",
                      suspend_source="idle_timeout")
    _write_session_md(cfg, "S-u-mytask-p6", window="mytask",
                      started_at="2026-07-04T23:25:00Z")
    _write_lifecycle(cfg, "S-u-mytask-p5", [
        _timeout("2026-07-04T23:22:16Z"), _recycled("2026-07-04T23:23:16Z")])
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-05T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T00:00:00Z"))
    assert v["b"]["pass"] is False
    assert any("S-u-mytask-p6" in x for x in v["b"]["violations"])


def test_verdict_b_graceful_exit_redispatch_not_a_violation(tmp_path):
    # p40→p41 live shape: predecessor exited gracefully (work done), operator
    # redispatched the lane — need-driven, not an auto-incarnation respawn
    cfg = _cfg(tmp_path)
    _write_session_md(cfg, "S-u-lane-p40", window="lane", status="suspended",
                      started_at="2026-07-05T11:38:56Z",
                      suspended_at="2026-07-05T12:01:50Z",
                      suspend_source="graceful-exit")
    _write_session_md(cfg, "S-u-lane-p41", window="lane",
                      started_at="2026-07-05T12:05:14Z")
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-06T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T13:00:00Z"))
    assert v["b"]["pass"] is True


# --- verdict (c): no runaway spawn / token leak -------------------------------

def test_verdict_c_pass_bounded_spawns(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(3):
        _write_session_md(cfg, f"S-u-w{i}-p{i}", window=f"w{i}",
                          started_at=f"2026-07-05T0{i}:00:00Z")
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-06T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T04:00:00Z"))
    assert v["c"]["pass"] is True


def test_verdict_c_fail_runaway_spawn_burst(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(C.RUNAWAY_SPAWNS_PER_HOUR + 1):
        _write_session_md(cfg, f"S-u-w{i}-p{i}", window=f"w{i}",
                          started_at=f"2026-07-05T01:{i:02d}:00Z")
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-06T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T02:00:00Z"))
    assert v["c"]["pass"] is False
    assert any("spawns" in x for x in v["c"]["violations"])


def test_verdict_c_fail_recompact_loop(tmp_path):
    # the live p8 shape: repeated timeout arms with NO recycle finalize —
    # a /compact per idle window forever = the token leak
    cfg = _cfg(tmp_path)
    _write_lifecycle(cfg, "S-u-user-session-p8", [
        _timeout("2026-07-04T23:15:16Z"),
        _timeout("2026-07-05T06:25:16Z"),
    ])
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-06T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T07:00:00Z"))
    assert v["c"]["pass"] is False
    assert any("S-u-user-session-p8" in x and "no finalize" in x
               for x in v["c"]["violations"])


# --- churn metrics (the monitors' SSOT) ---------------------------------------

def test_spawn_count_window(tmp_path):
    cfg = _cfg(tmp_path)
    now = _epoch("2026-07-05T12:00:00Z")
    _write_session_md(cfg, "S-u-a-p1", window="a",
                      started_at="2026-07-05T11:30:00Z")  # inside 1h
    _write_session_md(cfg, "S-u-b-p2", window="b",
                      started_at="2026-07-05T10:30:00Z")  # outside
    _write_session_md(cfg, "S-u-c-p3", window="c")        # started_at: ~
    assert C.spawn_count(cfg, SLUG, window_s=3600, now=now) == 1
    assert C.spawn_count(cfg, SLUG, window_s=7200, now=now) == 2


def test_live_session_count(tmp_path):
    cfg = _cfg(tmp_path)
    _write_session_md(cfg, "S-u-a-p1", window="a", status="active",
                      started_at="2026-07-05T11:30:00Z")
    _write_session_md(cfg, "S-u-b-p2", window="b", status="suspended",
                      started_at="2026-07-05T10:30:00Z")
    assert C.live_session_count(cfg, SLUG) == 1
    # missing sessions dir → 0, never raises
    assert C.live_session_count(cfg, "no-such-project") == 0


# --- report rendering ----------------------------------------------------------

def test_render_md_contains_log_and_verdict_table(tmp_path):
    cfg = _cfg(tmp_path)
    _write_session_md(cfg, "S-u-operator-p11", window="operator",
                      status="suspended", started_at="2026-07-04T19:47:35Z",
                      suspended_at="2026-07-04T23:23:23Z",
                      suspend_source="idle_timeout")
    _write_lifecycle(cfg, "S-u-operator-p11", [
        _timeout("2026-07-04T23:19:16Z"),
        _timeout("2026-07-04T23:22:16Z"),
        _recycled("2026-07-04T23:23:16Z"),
    ])
    events = C.collect(cfg, SLUG, since=0, until=_epoch("2026-07-05T00:00:00Z"))
    v = C.verdicts(events, now=_epoch("2026-07-05T00:00:00Z"))
    out = C.render_md(SLUG, events, v,
                      since=_epoch("2026-07-04T00:00:00Z"),
                      until=_epoch("2026-07-05T00:00:00Z"))
    assert "session_recycled" in out
    assert "| (a) compact-once-in-place | FAIL |" in out
    assert "| (b) no-auto-incarnation-respawn | PASS |" in out
