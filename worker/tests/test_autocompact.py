"""T-0333/T-0334: self-healing context compaction (closed loop).

When a session crosses the context ceiling, the SYSTEM /compacts it — but ONLY
when its pane is idle (Claude finished its turn, awaiting input) and the composer
is ready (❯), never mid-turn. The operator session is NOT exempt (T-0334).
"""
from __future__ import annotations

import types

import pytest

from bot_squad_worker import autocompact as A


# --- pure decision helpers --------------------------------------------------

def test_compact_safe_only_when_idle():
    assert A.compact_safe("idle") is True
    # never interrupt active work, a Ctrl-C'd pane, or a paneless session
    assert A.compact_safe("running") is False
    assert A.compact_safe("paused") is False
    assert A.compact_safe("suspended") is False
    assert A.compact_safe("") is False


def test_compact_due_requires_urgent_level_and_respects_cooldown():
    # below the ceiling → never auto-compact
    assert A.compact_due("none", {}, now=1000.0) is False
    assert A.compact_due("warn", {}, now=1000.0) is False
    # at the ceiling, never compacted before → due
    assert A.compact_due("urgent", {}, now=1000.0) is True
    # just compacted → cooldown suppresses a re-/compact
    fired = {"compact": 1000.0}
    assert A.compact_due("urgent", fired, now=1000.0 + 10, cooldown=600) is False
    # cooldown elapsed → due again
    assert A.compact_due("urgent", fired, now=1000.0 + 601, cooldown=600) is True


def test_composer_ready_pure():
    assert A.composer_ready("some output\n❯ \n") is True
    assert A.composer_ready("") is False
    assert A.composer_ready("just bash $ no rune") is False
    # mid-generation marker → not safe even if a stale ❯ lingers
    assert A.composer_ready("❯\n· Working… (esc to interrupt)") is False


def test_autocompact_enabled_default_on_kill_switch_off(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_AUTOCOMPACT", raising=False)
    assert A.autocompact_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_AUTOCOMPACT", "0")
    assert A.autocompact_enabled() is False


# --- executor (maybe_compact) -----------------------------------------------

@pytest.fixture
def harness(monkeypatch):
    """Stub pane resolution / capture / inject so no tmux is touched.

    These exercise the legacy Claude ``/compact`` path + the safety gates shared
    by both strategies, so we pin ``compact_mode`` to ``claude`` (the T-0467
    write-to-artifact handoff has its own suite in test_compact_handoff.py)."""
    sent: list[str] = []
    state = {"pane": "%9", "buf": "❯ ready\n"}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: sent.append(sid))
    monkeypatch.setattr(A, "autocompact_enabled", lambda: True)
    monkeypatch.setattr(A, "compact_mode", lambda: "claude")
    # T-0563/T-0564: these tests use slug "proj" with cfg=None and a fake pane
    # id — allowlist it and stub the tmux attach check so the new shared gate
    # (recycle_gate.recycle_allowed) doesn't touch real tmux or the default
    # bot-squad-only allowlist. The gate itself has its own test module.
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "proj")
    monkeypatch.setattr(A.recycle_gate, "is_attached", lambda target: False)
    return {"sent": sent, "state": state}


def _rec(sid="S-almdudleer-dev-p5", activity="idle", fired=None, role=None):
    return {"sid": sid, "activity": activity, "alert_fired_at": dict(fired or {}),
            "role": role}


def test_maybe_compact_sends_when_idle_urgent_and_composer_ready(harness):
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is True
    assert harness["sent"] == ["S-almdudleer-dev-p5"]
    assert rec["alert_fired_at"]["compact"] == 1000.0  # cooldown stamped


def test_maybe_compact_skips_when_not_idle(harness):
    rec = _rec(activity="running")
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_below_threshold(harness):
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "warn", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_when_composer_not_ready(harness):
    harness["state"]["buf"] = "· Working… (esc to interrupt)"
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_within_cooldown(harness, monkeypatch):
    monkeypatch.setattr(A, "compact_cooldown_sec", lambda: 600)
    rec = _rec(fired={"compact": 1000.0})
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0 + 5) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_when_no_live_pane(harness):
    harness["state"]["pane"] = None
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_kill_switch(harness, monkeypatch):
    monkeypatch.setattr(A, "autocompact_enabled", lambda: False)
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_operator_session_is_not_exempt(harness):
    """T-0334: the operator session is a first-class member of the loop."""
    rec = _rec(sid="S-almdudleer-bot-squad-operator-p5", activity="idle")
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is True
    assert harness["sent"] == ["S-almdudleer-bot-squad-operator-p5"]


# --- T-0563/T-0564: recycle-v2 gates ----------------------------------------

def test_non_allowlisted_project_is_never_compacted(harness, monkeypatch):
    """T-0563: the 2026-06-29 incident's fix — a non-allowlisted project (e.g.
    watchrobot) is NEVER auto-/compact-ed."""
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)  # undo harness override
    rec = _rec()
    assert A.maybe_compact(None, "watchrobot", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_user_conversation_role_is_never_compacted(harness):
    """T-0564: the human's own live chat is never auto-/compact-ed even when
    over the context ceiling."""
    rec = _rec(role="user-conversation")
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_attached_pane_is_never_compacted(harness, monkeypatch):
    """T-0564: a human client attached to the pane blocks the /compact even
    when everything else says "go"."""
    monkeypatch.setattr(A.recycle_gate, "is_attached", lambda target: True)
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []
