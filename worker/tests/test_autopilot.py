"""Tests for T-0153 autopilot (bot_squad_worker.autopilot).

Mirrors the manual walkthrough in
``data/bot-squad/scenarios/T-0153-autopilot-popover.md`` (written + walked
through first, per T-0158): start → no-stall tick → stall re-ping → expiry, and
the two stop paths (early-exit with reason vs operator cancel).

The progress signal (git commit / progress notes / session activity) is mocked
to None so the stall window is governed solely by ``started_at`` / ``last_ping``
— otherwise the test repo's fresh init commit reads as "progress just now" and
nothing ever stalls.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bot_squad_worker import autopilot as ap
from bot_squad_worker import sessions as S
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

FAKE = "S-fake-tl-p99"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    # Isolate from real tmux + the test repo's commit clock + TG.
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [])
    monkeypatch.setattr(S, "_get_current_user", lambda: "tester")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(ap, "_git_last_commit_at", lambda c, s: None)
    monkeypatch.setattr(ap, "_latest_progress_note_at", lambda c, s: None)
    notes: list[str] = []
    monkeypatch.setattr(ap, "_notify_stakeholder", lambda c, s, text: notes.append(text))
    return cfg, project.slug, notes


def _inbox(cfg, slug, sid) -> Path:
    return cfg.data_dir / slug / "_chat" / f"inbox-{sid}.log"


def test_start_delivers_brief_and_persists(cfg_slug):
    cfg, slug, _notes = cfg_slug
    res = ap.start(cfg, slug, kind="session", ref=FAKE, prompt="tidy the X",
                   early_exit="X is tidy", duration_hours=1.0,
                   stall_minutes=1, watchdog_minutes=1)
    assert res["ok"] and res["target_sid"] == FAKE and res["spawned"] is False
    assert res["delivery"]["inbox"] is True
    # State persisted, expiry ~1h out.
    st = ap.load_state(cfg, slug, res["key"])
    assert st is not None and st.status == "running"
    assert ap._parse_iso(st.expires_at) - time.time() == pytest.approx(3600, abs=60)
    # Brief landed in the inbox.
    body = _inbox(cfg, slug, FAKE).read_text()
    assert "AUTOPILOT" in body and "tidy the X" in body


def test_no_stall_within_window(cfg_slug):
    cfg, slug, _notes = cfg_slug
    res = ap.start(cfg, slug, kind="session", ref=FAKE, prompt="p",
                   duration_hours=1.0, stall_minutes=30, watchdog_minutes=1)
    ap.tick(cfg, slug)
    st = ap.load_state(cfg, slug, res["key"])
    assert st.pings == 0


def test_stall_reping(cfg_slug):
    cfg, slug, _notes = cfg_slug
    res = ap.start(cfg, slug, kind="session", ref=FAKE, prompt="p",
                   duration_hours=1.0, stall_minutes=1, watchdog_minutes=1)
    key = res["key"]
    # Backdate the progress clock past the stall threshold + the watchdog gate.
    st = ap.load_state(cfg, slug, key)
    old = _iso(time.time() - 600)
    st.started_at = old
    st.last_check_at = old
    st.last_ping_at = None
    ap.save_state(cfg, st)
    before = len(_inbox(cfg, slug, FAKE).read_text().splitlines())
    out = ap.tick(cfg, slug)
    assert any(a["action"] == "stall_reping" for a in out["actions"])
    st = ap.load_state(cfg, slug, key)
    assert st.pings == 1
    after = _inbox(cfg, slug, FAKE).read_text()
    assert len(after.splitlines()) == before + 1
    assert "STALL" in after.splitlines()[-1]


def test_expiry_notifies(cfg_slug):
    cfg, slug, notes = cfg_slug
    res = ap.start(cfg, slug, kind="session", ref=FAKE, prompt="p",
                   duration_hours=1.0, stall_minutes=1, watchdog_minutes=1)
    key = res["key"]
    st = ap.load_state(cfg, slug, key)
    st.expires_at = _iso(time.time() - 60)
    ap.save_state(cfg, st)
    out = ap.tick(cfg, slug)
    assert any(a["action"] == "expired" for a in out["actions"])
    st = ap.load_state(cfg, slug, key)
    assert st.status == "expired" and st.enabled is False
    assert any("completed" in n for n in notes)


def test_stop_early_exit_vs_operator(cfg_slug):
    cfg, slug, notes = cfg_slug
    ap.start(cfg, slug, kind="session", ref="S-a-p1", prompt="p", duration_hours=1.0)
    r1 = ap.stop(cfg, slug, target_sid="S-a-p1", reason="condition met")
    assert r1["status"] == "exited" and r1["exit_reason"] == "condition met"
    assert any("exited early" in n for n in notes)

    ap.start(cfg, slug, kind="session", ref="S-b-p2", prompt="p", duration_hours=1.0)
    r2 = ap.stop(cfg, slug, target_sid="S-b-p2")
    assert r2["status"] == "stopped"


def test_stop_requires_a_selector(cfg_slug):
    from bot_squad_worker.actions import ActionError
    cfg, slug, _notes = cfg_slug
    with pytest.raises(ActionError):
        ap.stop(cfg, slug)


def test_start_rejects_bad_kind_and_empty_prompt(cfg_slug):
    from bot_squad_worker.actions import ActionError
    cfg, slug, _notes = cfg_slug
    with pytest.raises(ActionError):
        ap.start(cfg, slug, kind="bogus", ref=FAKE, prompt="p")
    with pytest.raises(ActionError):
        ap.start(cfg, slug, kind="session", ref=FAKE, prompt="   ")


def test_notify_stakeholder_routes_tg_primary(tmp_path: Path, monkeypatch):
    """P2-08 + T-0610 inversion: autopilot's stakeholder page routes through the
    _send_stakeholder_dm SSOT — ONE TG delivery into #team-queries (TG-primary
    since the 2026-07-04 proxy fix), MAX untouched, no duplicate."""
    import dataclasses
    import types
    from bot_squad_worker import actions as A, tg_topics

    project = _make_project_with_repo(tmp_path)
    cfg = dataclasses.replace(
        _make_config_with_project(tmp_path, project),
        max_default_chat_id="MAXID", max_recipient_kind="chat_id",
    )
    tg_topics.save(cfg, project.slug, {"team_queries": 777})
    max_calls: list[dict] = []
    tg_calls: list[dict] = []
    monkeypatch.setattr(A, "_MAX", types.SimpleNamespace(send=lambda **k: (max_calls.append(k) or True)))
    monkeypatch.setattr(A, "_TG", types.SimpleNamespace(send=lambda **k: (tg_calls.append(k) or True)))

    ap._notify_stakeholder(cfg, project.slug, "autopilot parked the run")

    assert len(tg_calls) == 1 and tg_calls[0]["topic_id"] == 777
    assert "autopilot parked the run" in tg_calls[0]["text"]
    assert len(max_calls) == 0  # one page = one delivery (T-0610)
