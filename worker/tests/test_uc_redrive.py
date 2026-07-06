"""Tests for T-0622 user-conversation intake no-drop (bot_squad_worker.uc_redrive).

Promotes the manual walkthrough in
``data/bot-squad/scenarios/T-0622-...md`` (written + walked first, per T-0158):
an unanswered thread whose attendant is still live but idle gets re-driven via
the EXISTING ``ensure_user_conversation`` action — but only once its OWN 429/
limit pressure has cleared, never while it's actually busy, and bounded by a
per-(slug, gid) cooldown + retry cap so a permanently stuck thread pages the
operator once instead of nagging forever.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bot_squad_worker import uc_redrive as UC
from bot_squad_worker import sessions as S
from bot_squad_worker import detector as D
from bot_squad_worker import actions as A
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

SID = "S-u-gu_a1b2c3-user-conversation-p9"
GID = "gu_a1b2c3"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_GRACE_SEC", "60")
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_COOLDOWN_SEC", "300")
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_MAX_RETRIES", "3")
    return cfg, slug


def _write_thread(cfg, slug, gid, records):
    conv_dir = cfg.data_dir / "_mothership" / "conversations" / slug
    conv_dir.mkdir(parents=True, exist_ok=True)
    p = conv_dir / f"{gid}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


def _stub(monkeypatch, *, sid=SID, pressured_sids=(), limit_blocked_sids=(),
          activity="idle", live_sid=SID, dispatched=None):
    dispatched = dispatched if dispatched is not None else []

    def fake_dispatch(name, params):
        dispatched.append((name, params))
        return {"ok": True, "sid": sid, "spawned": False}

    monkeypatch.setattr(D, "session_pressure", lambda c, **k: {
        "rate_limited_sids": list(pressured_sids),
        "limit_blocked_sids": list(limit_blocked_sids),
        "any": bool(pressured_sids or limit_blocked_sids),
        "sampled_at": _iso(time.time()),
    })
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [{"sid": sid, "activity": activity}])
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda c, s, g: live_sid)
    monkeypatch.setattr(A, "dispatch", fake_dispatch)
    return dispatched


def test_no_thread_is_a_noop(cfg_slug, monkeypatch):
    cfg, slug = cfg_slug
    dispatched = _stub(monkeypatch)
    result = UC.check_project(cfg, slug)
    assert result == {"ok": True, "redriven": []}
    assert dispatched == []


def test_answered_thread_never_redrives(cfg_slug, monkeypatch):
    """A thread whose newest record is the attendant's own reply is left alone."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 600), "author": "user", "text": "hi"},
        {"timestamp": _iso(now - 500), "author": f"session:{SID}", "text": "hello!"},
    ])
    dispatched = _stub(monkeypatch)
    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == []
    assert dispatched == []


def test_fresh_message_within_grace_period_not_redriven(cfg_slug, monkeypatch):
    """A message that JUST arrived is left to the normal reply flow first."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 5), "author": "user", "text": "hi"},
    ])
    dispatched = _stub(monkeypatch)
    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == []
    assert dispatched == []


def test_still_under_own_pressure_not_redriven(cfg_slug, monkeypatch):
    """The attendant's own sid is 429-flagged — never redrive into a live storm."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, pressured_sids=[SID], activity="idle")
    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == []
    assert dispatched == []


def test_limit_blocked_sid_not_redriven(cfg_slug, monkeypatch):
    """5h/usage-limit pane marker also counts as pressure — same gate."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, limit_blocked_sids=[SID], activity="idle")
    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == []
    assert dispatched == []


def test_busy_attendant_not_redriven(cfg_slug, monkeypatch):
    """Pressure cleared but the attendant is actively working — never interrupt it."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, activity="running")
    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == []
    assert dispatched == []


def test_no_live_attendant_out_of_scope(cfg_slug, monkeypatch):
    """No live attendant at all is out of scope (see module docstring) — the
    existing spawn-on-next-message flow, not this tick, covers that case."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, live_sid=None)
    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == []
    assert dispatched == []


def test_idle_after_pressure_clears_redrives_exactly_once(cfg_slug, monkeypatch):
    """DoD core: idle + unanswered + pressure clear -> exactly one re-wake this
    tick, and an immediate re-check inside the cooldown window fires no more."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")

    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == [{"gid": GID, "sid": SID, "retries": 1}]
    assert len(dispatched) == 1
    name, params = dispatched[0]
    assert name == "ensure_user_conversation"
    assert params == {"slug": slug, "global_user_id": GID,
                       "message_ref": "why no answer?"}

    # Same unanswered message, immediately again — cooldown holds.
    result2 = UC.check_project(cfg, slug, now=now + 1)
    assert result2["redriven"] == []
    assert len(dispatched) == 1  # no additional dispatch


def test_cooldown_elapsed_allows_a_second_attempt(cfg_slug, monkeypatch):
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")

    UC.check_project(cfg, slug, now=now)
    assert len(dispatched) == 1

    later = now + 301  # past the 300s cooldown set in the fixture
    result = UC.check_project(cfg, slug, now=later)
    assert result["redriven"] == [{"gid": GID, "sid": SID, "retries": 2}]
    assert len(dispatched) == 2


def test_bounded_retries_then_notifies_operator_once(cfg_slug, monkeypatch):
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "stuck"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")
    notified = []
    monkeypatch.setattr(UC, "_notify_operator_stuck",
                        lambda cfg, slug, gid, retries: notified.append(retries))

    for i in range(5):
        UC.check_project(cfg, slug, now=now + i * 301)

    assert len(dispatched) == 3  # BOT_SQUAD_UC_REDRIVE_MAX_RETRIES=3
    assert notified == [3]  # escalated exactly once, right when the bound was hit


def test_new_message_after_a_reply_resets_retry_state(cfg_slug, monkeypatch):
    """A stuck thread that finally gets answered, then goes unanswered again on
    a LATER message, starts its retry count fresh (not still-exhausted)."""
    cfg, slug = cfg_slug
    now = time.time()
    thread = _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 1200), "author": "user", "text": "first"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")
    monkeypatch.setattr(UC, "_notify_operator_stuck", lambda *a, **k: None)

    for i in range(3):
        UC.check_project(cfg, slug, now=now - 900 + i * 301)
    assert len(dispatched) == 3

    # The attendant finally replies; state clears.
    thread.write_text(thread.read_text() + json.dumps(
        {"timestamp": _iso(now - 200), "author": f"session:{SID}", "text": "sorry!"}
    ) + "\n")
    UC.check_project(cfg, slug, now=now - 100)
    assert len(dispatched) == 3  # no new dispatch — thread is answered

    # A brand new unanswered message arrives.
    thread.write_text(thread.read_text() + json.dumps(
        {"timestamp": _iso(now - 60), "author": "user", "text": "second ask"}
    ) + "\n")
    result = UC.check_project(cfg, slug, now=now + 10)
    assert result["redriven"] == [{"gid": GID, "sid": SID, "retries": 1}]
    assert len(dispatched) == 4


def test_kill_switch_disables_tick(cfg_slug, monkeypatch):
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE", "0")

    UC.uc_redrive_tick(cfg)
    assert dispatched == []


def test_tick_iterates_projects_and_contains_per_project_errors(cfg_slug, monkeypatch):
    cfg, slug = cfg_slug

    def boom(_cfg, _slug, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(UC, "check_project", boom)
    UC.uc_redrive_tick(cfg)  # must not raise
