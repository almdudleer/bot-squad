"""T-0696 — stall-sweep: auto-answer known blocking TUI prompts in live panes.

Manual walkthrough (T-0158) done FIRST against real tmux panes — see
data/bot-squad/scenarios/T-0696-stuck-interactive-prompts-e-g-fable-5-usage-credits-gate-in.md.
These tests cover the same behaviour with the tmux/reporting seams stubbed
(no real tmux, no real ticket files) so the suite runs anywhere.
"""
from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import stall_sweep as SS


def _make_cfg(tmp_path: Path, *, projects: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        data_dir=tmp_path / "data",
        projects=projects if projects is not None else {"bot-squad": object()},
    )


SID = "S-almdudleer-fable-spawn-p1"
SLUG = "bot-squad"

# Verbatim shape of the observed stall (T-0696 progress notes, 2026-07-26).
FABLE5_GATE_TEXT = (
    "Fable 5 now uses usage credits for extended access.\n"
    "You don't have usage credits yet.\n"
    "  1. Set up usage credits\n"
    "  2. Switch to Sonnet 5 and continue\n"
)

# Verbatim promo banner (already known to false-positive detector.py's
# limit-marker scan, T-0571) — mentions "usage credits" on a perfectly
# healthy pane and must NEVER trip this sweep.
FABLE5_PROMO_BANNER = (
    " ◎ Until July 7, you can use up to 50% of your plan's weekly usage "
    "limit on Fable 5. If you hit your limit, you can continue on Fable 5 "
    "with usage credits. Fable 5 draws down usage faster than Opus 4.8."
)


# ---------------------------------------------------------------------------
# Signature matching
# ---------------------------------------------------------------------------

def test_fable5_signature_matches_verbatim_gate():
    sig = SS._match_signature(FABLE5_GATE_TEXT)
    assert sig is not None
    assert sig.name == "fable5_usage_credits_gate"
    assert sig.keys == ("2", "Enter")


def test_fable5_signature_ignores_promo_banner():
    """T-0571-style false positive: the promo banner mentions 'usage credits'
    but is not the blocking gate — must not match."""
    assert SS._match_signature(FABLE5_PROMO_BANNER) is None
    assert SS._match_signature("some output\n" + FABLE5_PROMO_BANNER + "\n❯ ") is None


def test_fable5_signature_ignores_bare_usage_credit_mention():
    assert SS._match_signature("this session mentions usage credits in passing") is None


def test_fable5_signature_requires_both_option_lines():
    assert SS._match_signature("Fable 5 now uses usage credits.\n1. Set up usage credits\n") is None
    assert SS._match_signature("2. Switch to Sonnet 5 and continue\nusage credit\n") is None


def test_match_signature_none_for_empty_or_unrelated_text():
    assert SS._match_signature("") is None
    assert SS._match_signature("Running tests, all green\n❯ ") is None


def test_no_signature_ever_sends_the_billing_option():
    """T-0707: option 1 on the fable5 gate is a BILLING action (set up usage
    credits) — no auto-answer signature may ever send it. This guards every
    CURRENT and FUTURE entry in SIGNATURES, not just the fable5 one, so a
    later signature addition can't accidentally wire up a billing keypress."""
    for sig in SS.SIGNATURES:
        assert "1" not in sig.keys, (
            f"{sig.name} sends '1' -- must never auto-trigger a billing action"
        )


def test_match_signature_swallows_a_raising_signature(monkeypatch):
    bad = SS.StallSignature(
        name="broken", match=lambda buf: (_ for _ in ()).throw(RuntimeError("boom")),
        keys=("x",), note="n/a",
    )
    monkeypatch.setattr(SS, "SIGNATURES", (bad,) + SS.SIGNATURES)
    # Must not raise, and the real signature after it still gets a chance.
    assert SS._match_signature(FABLE5_GATE_TEXT).name == "fable5_usage_credits_gate"


# ---------------------------------------------------------------------------
# Enable / kill switch
# ---------------------------------------------------------------------------

def test_stall_sweep_enabled_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_STALL_SWEEP", raising=False)
    assert SS.stall_sweep_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_STALL_SWEEP", "0")
    assert SS.stall_sweep_enabled() is False
    monkeypatch.setenv("BOT_SQUAD_STALL_SWEEP", "1")
    assert SS.stall_sweep_enabled() is True


# ---------------------------------------------------------------------------
# check_session — the per-session decision + action
# ---------------------------------------------------------------------------

@pytest.fixture
def seams(monkeypatch):
    """Stub tmux + reporting so no real pane/ticket/TG is touched."""
    calls = {"sent_keys": [], "ticket_notes": [], "escalations": []}
    state = {"buf": FABLE5_GATE_TEXT}

    monkeypatch.setattr(SS, "_capture_pane", lambda pane_id: state["buf"])
    monkeypatch.setattr(
        SS, "_send_keys",
        lambda data_dir, sid, pane_id, keys: calls["sent_keys"].append((sid, pane_id, keys)),
    )
    monkeypatch.setattr(
        SS, "_ticket_note",
        lambda cfg, slug, task_id, text: calls["ticket_notes"].append((task_id, text)) or True,
    )
    from bot_squad_worker import tg_stall as TG
    monkeypatch.setattr(
        TG, "mark_blocked",
        # T-0977: the sweep now stamps `origin="stall_sweep"` and passes no
        # `blocked_on` — this marker is "stuck on a TUI modal", which no peer
        # reply resolves, so nothing may peer-clear it.
        lambda cfg, slug, sid, text, *, origin="declared", blocked_on=None: (
            calls["escalations"].append((sid, text, origin, blocked_on))
        ),
    )
    return {"calls": calls, "state": state}


def test_answers_on_first_match_and_writes_marker(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    acted = SS.check_session(cfg, SLUG, SID, row, "%1", time.time())
    assert acted is True
    assert seams["calls"]["sent_keys"] == [(SID, "%1", ("2", "Enter"))]

    marker = json.loads(SS._marker_path(cfg, SLUG, SID).read_text())
    assert marker["signature"] == "fable5_usage_credits_gate"
    assert marker["attempts"] == 1
    assert marker["escalated"] is False


def test_ticket_note_written_when_task_id_present(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": "T-0696"}
    SS.check_session(cfg, SLUG, SID, row, "%1", time.time())
    notes = seams["calls"]["ticket_notes"]
    assert len(notes) == 1
    task_id, text = notes[0]
    assert task_id == "T-0696"
    assert SID in text and "usage-credits" in text


def test_no_ticket_note_when_no_task_id(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    SS.check_session(cfg, SLUG, SID, row, "%1", time.time())
    assert seams["calls"]["ticket_notes"] == []


def test_no_match_clears_a_stale_marker(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    now = time.time()
    SS.check_session(cfg, SLUG, SID, row, "%1", now)  # creates a marker
    assert SS._marker_path(cfg, SLUG, SID).exists()

    seams["state"]["buf"] = "Switched to Sonnet 5. Continuing...\n❯ "
    acted = SS.check_session(cfg, SLUG, SID, row, "%1", now + 100)
    assert acted is False
    assert not SS._marker_path(cfg, SLUG, SID).exists()


def test_within_cooldown_does_not_resend(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    now = time.time()
    SS.check_session(cfg, SLUG, SID, row, "%1", now)
    assert len(seams["calls"]["sent_keys"]) == 1

    # Still matching, well within the cooldown window.
    acted = SS.check_session(cfg, SLUG, SID, row, "%1", now + 1)
    assert acted is False
    assert len(seams["calls"]["sent_keys"]) == 1  # not resent


def test_retries_after_cooldown_elapses(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    now = time.time()
    SS.check_session(cfg, SLUG, SID, row, "%1", now)
    acted = SS.check_session(cfg, SLUG, SID, row, "%1", now + SS._RETRY_COOLDOWN_SEC + 1)
    assert acted is True
    assert len(seams["calls"]["sent_keys"]) == 2
    marker = json.loads(SS._marker_path(cfg, SLUG, SID).read_text())
    assert marker["attempts"] == 2
    # Only the FIRST attempt reports "answered" (no repeat ticket note per retry).
    assert len(seams["calls"]["ticket_notes"]) == 0  # task_id is None here


def test_first_seen_preserved_across_retries(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    now = time.time()
    SS.check_session(cfg, SLUG, SID, row, "%1", now)
    first_seen = json.loads(SS._marker_path(cfg, SLUG, SID).read_text())["first_seen"]
    SS.check_session(cfg, SLUG, SID, row, "%1", now + SS._RETRY_COOLDOWN_SEC + 1)
    marker = json.loads(SS._marker_path(cfg, SLUG, SID).read_text())
    assert marker["first_seen"] == first_seen


def test_escalates_after_max_attempts_and_stops_sending(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": "T-0696"}
    now = time.time()
    step = SS._RETRY_COOLDOWN_SEC + 1
    for i in range(SS._MAX_ATTEMPTS):
        acted = SS.check_session(cfg, SLUG, SID, row, "%1", now + i * step)
        assert acted is True
    assert len(seams["calls"]["sent_keys"]) == SS._MAX_ATTEMPTS

    # One more pass past the cooldown: still stuck -> escalate instead of
    # sending another keystroke.
    acted = SS.check_session(cfg, SLUG, SID, row, "%1", now + SS._MAX_ATTEMPTS * step)
    assert acted is False
    assert len(seams["calls"]["sent_keys"]) == SS._MAX_ATTEMPTS  # not resent
    assert len(seams["calls"]["escalations"]) == 1
    esc_sid, esc_text, esc_origin, esc_blocked_on = seams["calls"]["escalations"][0]
    assert esc_origin == "stall_sweep"
    assert esc_blocked_on is None
    assert esc_sid == SID
    assert "auto-answer did not clear it" in esc_text

    marker = json.loads(SS._marker_path(cfg, SLUG, SID).read_text())
    assert marker["escalated"] is True


def test_escalated_marker_is_never_retried_or_re_escalated(tmp_path, seams):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    now = time.time()
    step = SS._RETRY_COOLDOWN_SEC + 1
    for i in range(SS._MAX_ATTEMPTS + 1):
        SS.check_session(cfg, SLUG, SID, row, "%1", now + i * step)
    assert len(seams["calls"]["escalations"]) == 1

    # Further passes, still matching: no more keys, no repeat escalation.
    acted = SS.check_session(cfg, SLUG, SID, row, "%1", now + (SS._MAX_ATTEMPTS + 5) * step)
    assert acted is False
    assert len(seams["calls"]["sent_keys"]) == SS._MAX_ATTEMPTS
    assert len(seams["calls"]["escalations"]) == 1


def test_send_keys_failure_is_not_recorded_as_an_attempt(tmp_path, seams, monkeypatch):
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}

    def _boom(data_dir, sid, pane_id, keys):
        raise RuntimeError("tmux is on fire")
    monkeypatch.setattr(SS, "_send_keys", _boom)

    acted = SS.check_session(cfg, SLUG, SID, row, "%1", time.time())
    assert acted is False
    assert not SS._marker_path(cfg, SLUG, SID).exists()


def test_different_signature_resets_attempts(tmp_path, seams):
    """A marker for one signature must not gatekeep a differently-shaped
    stall on the same sid (defensive: attempts reset, first_seen resets)."""
    cfg = _make_cfg(tmp_path)
    row = {"task_id": None}
    now = time.time()
    SS.check_session(cfg, SLUG, SID, row, "%1", now)
    other = SS.StallSignature(name="other_prompt", match=lambda b: "OTHER" in b,
                              keys=("y", "Enter"), note="answered other prompt")
    seams["state"]["buf"] = "OTHER PROMPT\n"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SS, "SIGNATURES", (other,))
        acted = SS.check_session(cfg, SLUG, SID, row, "%1", now + 1)
    assert acted is True
    marker = json.loads(SS._marker_path(cfg, SLUG, SID).read_text())
    assert marker["signature"] == "other_prompt"
    assert marker["attempts"] == 1


# ---------------------------------------------------------------------------
# tick() — scheduler entry, iterates live sessions
# ---------------------------------------------------------------------------

def test_tick_disabled_is_a_noop(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_STALL_SWEEP", "0")
    from bot_squad_worker import sessions as S
    calls = []
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: calls.append(slug) or [])
    SS.tick(cfg)
    assert calls == []


def test_tick_checks_only_active_sessions_with_a_live_pane(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.delenv("BOT_SQUAD_STALL_SWEEP", raising=False)
    from bot_squad_worker import sessions as S

    rows = [
        {"sid": "S-live-p1", "status": "active", "linux_user": "u", "task_id": None},
        {"sid": "S-suspended-p2", "status": "suspended", "linux_user": "u", "task_id": None},
        {"sid": "S-no-pane-p3", "status": "active", "linux_user": "u", "task_id": None},
        {"sid": "S-other-user-p4", "status": "active", "linux_user": "someone-else", "task_id": None},
    ]
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: rows)
    monkeypatch.setattr(S, "live_pane_map", lambda: {"S-live-p1": "%1"})
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")

    checked = []
    monkeypatch.setattr(
        SS, "check_session",
        lambda cfg, slug, sid, row, pane_id, now: checked.append((sid, pane_id)),
    )
    SS.tick(cfg)
    assert checked == [("S-live-p1", "%1")]


def test_tick_swallows_per_session_errors(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.delenv("BOT_SQUAD_STALL_SWEEP", raising=False)
    from bot_squad_worker import sessions as S

    rows = [
        {"sid": "S-bad-p1", "status": "active", "linux_user": "u", "task_id": None},
        {"sid": "S-good-p2", "status": "active", "linux_user": "u", "task_id": None},
    ]
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: rows)
    monkeypatch.setattr(S, "live_pane_map", lambda: {"S-bad-p1": "%1", "S-good-p2": "%2"})
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")

    checked = []

    def _check(cfg, slug, sid, row, pane_id, now):
        if sid == "S-bad-p1":
            raise RuntimeError("boom")
        checked.append(sid)
    monkeypatch.setattr(SS, "check_session", _check)

    SS.tick(cfg)  # must not raise
    assert checked == ["S-good-p2"]


def test_tick_swallows_list_sessions_error_per_project(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, projects={"p1": object(), "p2": object()})
    monkeypatch.delenv("BOT_SQUAD_STALL_SWEEP", raising=False)
    from bot_squad_worker import sessions as S
    monkeypatch.setattr(S, "live_pane_map", lambda: {})
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")

    def _list_sessions(cfg, slug):
        if slug == "p1":
            raise RuntimeError("boom")
        return []
    monkeypatch.setattr(S, "list_sessions", _list_sessions)

    SS.tick(cfg)  # must not raise despite p1 failing
