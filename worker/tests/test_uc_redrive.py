"""Tests for T-0622 user-conversation intake no-drop (bot_squad_worker.uc_redrive).

Promotes the manual walkthrough in
``data/bot-squad/scenarios/T-0622-...md`` (written + walked first, per T-0158):
an unanswered thread whose attendant is still live but idle gets re-driven via
the EXISTING ``ensure_user_conversation`` action — but only once its OWN 429/
limit pressure has cleared, and never while it's actually busy.

T-0794 extends the same module with the stakeholder's cadence (~5 min, ~15 min,
then every ~30 for as long as the message hangs), his trigger (a user message
with no session reply after it — not "the newest record is a user record"), and
an escalation that has to LAND on a live operator sid instead of evaporating.
Those three groups are at the bottom of this file.
"""
from __future__ import annotations

import json
import logging
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
OPERATOR_SID = "S-u-operator-p1"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
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


def test_fresh_message_before_the_first_slot_not_redriven(cfg_slug, monkeypatch):
    """A message that JUST arrived is left to the normal reply flow first — the
    first ping slot (~5 min) is also the grace period."""
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
    tick, and an immediate re-check inside the slot fires no more."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 300), "author": "user", "text": "why no answer?"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")

    result = UC.check_project(cfg, slug, now=now)
    assert result["redriven"] == [{"gid": GID, "sid": SID, "pings": 1}]
    assert len(dispatched) == 1
    name, params = dispatched[0]
    assert name == "ensure_user_conversation"
    assert params == {"slug": slug, "global_user_id": GID,
                       "message_ref": "why no answer?"}

    # Same unanswered message, immediately again — the next slot is not due.
    result2 = UC.check_project(cfg, slug, now=now + 1)
    assert result2["redriven"] == []
    assert len(dispatched) == 1  # no additional dispatch


def test_new_message_after_a_reply_resets_ping_state(cfg_slug, monkeypatch):
    """A stuck thread that finally gets answered, then goes unanswered again on
    a LATER message, starts its cadence fresh at the ~5 min slot."""
    cfg, slug = cfg_slug
    now = time.time()
    thread = _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 3600), "author": "user", "text": "first"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")
    monkeypatch.setattr(UC, "_notify_operator_stuck", lambda *a, **k: [OPERATOR_SID])

    for offset in (-3300, -2700, -900):  # slots at +5, +15, +45 min
        UC.check_project(cfg, slug, now=now + offset)
    assert len(dispatched) == 3

    # The attendant finally replies; state clears.
    thread.write_text(thread.read_text() + json.dumps(
        {"timestamp": _iso(now - 200), "author": f"session:{SID}", "text": "sorry!"}
    ) + "\n")
    UC.check_project(cfg, slug, now=now - 100)
    assert len(dispatched) == 3  # no new dispatch — thread is answered

    # A brand new unanswered message arrives. Its FIRST slot is ~5 min out, so
    # a sweep 60s later still fires nothing…
    thread.write_text(thread.read_text() + json.dumps(
        {"timestamp": _iso(now - 60), "author": "user", "text": "second ask"}
    ) + "\n")
    assert UC.check_project(cfg, slug, now=now)["redriven"] == []
    # …and the ping that does come is ping 1 of a fresh cadence, not ping 4.
    result = UC.check_project(cfg, slug, now=now + 300)
    assert result["redriven"] == [{"gid": GID, "sid": SID, "pings": 1}]
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


# ===========================================================================
# T-0794 — the stakeholder's cadence
#
# «первый раз через минут 5 мб, второй через 15, потом каждые 30 или вроде
# того». What the module did instead: a flat 300s cooldown, three attempts, then
# permanent silence.
# ===========================================================================

def test_schedule_is_5_then_15_then_every_30_minutes():
    """His three numbers, as offsets from the unanswered message."""
    assert UC.ping_due_after_sec(0) == 5 * 60
    assert UC.ping_due_after_sec(1) == 15 * 60
    assert UC.ping_due_after_sec(2) == 45 * 60
    assert UC.ping_due_after_sec(3) == 75 * 60


def test_interval_never_grows_past_the_steady_state():
    """The gap RAMPS (5 → 10) and then stops widening at 30 minutes forever —
    an interval that kept growing would become a once-a-day check, which is the
    failure «потом каждые 30» rules out. Also: no bound. Ping 100 still exists."""
    due = [UC.ping_due_after_sec(n) for n in range(101)]
    gaps = [b - a for a, b in zip(due, due[1:])]
    assert gaps[:2] == [10 * 60, 30 * 60]
    assert set(gaps[1:]) == {30 * 60}
    assert max(gaps) == 30 * 60


def test_pings_keep_coming_past_the_old_three_attempt_bound(cfg_slug, monkeypatch):
    """The old code stopped at 3 re-wakes forever. Walk 6 hours of an idle
    attendant sitting on an unanswered message and count the pings: the ramp
    (5, 15 min) then one every 30."""
    cfg, slug = cfg_slug
    msg_at = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(msg_at), "author": "user", "text": "still waiting"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")
    monkeypatch.setattr(UC, "_notify_operator_stuck", lambda *a, **k: [OPERATOR_SID])

    fired_at = []
    for minute in range(1, 361):  # 6 hours of 60s ticks
        now = msg_at + minute * 60
        if UC.check_project(cfg, slug, now=now)["redriven"]:
            fired_at.append(minute)

    assert fired_at[:4] == [5, 15, 45, 75]
    assert len(fired_at) == 13  # 2 ramp + 11 steady in 6h — not 3, and not 360
    assert {b - a for a, b in zip(fired_at[1:], fired_at[2:])} == {30}
    assert len(dispatched) == 13


def test_slots_missed_while_busy_are_skipped_not_queued(cfg_slug, monkeypatch):
    """An attendant busy through its first hour must not eat a burst of the
    pings it missed the moment it goes idle — the cadence is a schedule, not a
    backlog it owes."""
    cfg, slug = cfg_slug
    msg_at = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(msg_at), "author": "user", "text": "hello?"},
    ])
    dispatched = _stub(monkeypatch, activity="running")
    monkeypatch.setattr(UC, "_notify_operator_stuck", lambda *a, **k: [OPERATOR_SID])

    for minute in range(1, 61):
        UC.check_project(cfg, slug, now=msg_at + minute * 60)
    assert dispatched == []  # busy the whole hour

    monkeypatch.setattr(S, "list_sessions",
                        lambda c, s: [{"sid": SID, "activity": "idle"}])
    fired_at = [m for m in range(61, 121)
                if UC.check_project(cfg, slug, now=msg_at + m * 60)["redriven"]]
    # ONE ping when it frees up — not the four slots it slept through — and
    # then straight back onto the every-30 schedule, not a replay of the ramp.
    assert fired_at == [61, 75, 105]


def test_slot_accounting_is_the_inverse_of_the_schedule():
    """``pings_due_by`` and ``ping_due_after_sec`` are one schedule read from
    two ends; a drift between them would silently double- or skip-ping."""
    for n in range(0, 50):
        due_at = UC.ping_due_after_sec(n)
        assert UC.pings_due_by(due_at) == n + 1
        assert UC.pings_due_by(due_at - 1) == n


def test_env_overrides_the_cadence(cfg_slug, monkeypatch):
    """The tuning was delegated («подумай, как лучше»), so the three numbers are
    knobs rather than literals — and a nonsense 0 is refused, not honoured."""
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_FIRST_SEC", "60")
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_SECOND_SEC", "120")
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_STEADY_SEC", "0")
    assert UC.ping_due_after_sec(0) == 60
    assert UC.ping_due_after_sec(1) == 120
    assert UC.ping_due_after_sec(2) == 120 + UC.DEFAULT_STEADY_PING_SEC

    # An inverted pair cannot un-order the schedule — pings_due_by is only the
    # inverse of ping_due_after_sec while the slots ascend.
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_FIRST_SEC", "1200")
    monkeypatch.setenv("BOT_SQUAD_UC_REDRIVE_SECOND_SEC", "600")
    assert UC.ping_due_after_sec(1) == 1200
    assert UC.pings_due_by(1200) == 2


# ===========================================================================
# T-0794 — the TRIGGER is his condition, not a proxy for it
#
# «если у него висят сообщения юзера, на которые он не ответил». The old test
# was "the newest record in the thread is author: user", which any later append
# falsifies. Measured on his own thread: 29 of 157 unanswered episodes had a
# system record land after the user's message.
# ===========================================================================

def test_system_record_after_the_user_message_does_not_hide_it(cfg_slug, monkeypatch):
    """RED PIN — a task-lifecycle notice appended after the ask made the thread
    read as answered, and the attendant was never re-woken."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 600), "author": "user", "text": "please answer"},
        {"timestamp": _iso(now - 300), "author": "system:task-lifecycle",
         "text": "📋 T-0794 → in_progress", "fyi": False},
    ])
    dispatched = _stub(monkeypatch, activity="idle")

    result = UC.check_project(cfg, slug, now=now)

    assert result["redriven"] == [{"gid": GID, "sid": SID, "pings": 1}]
    # …and it re-drives with the USER's text, not the system notice's.
    assert dispatched[0][1]["message_ref"] == "please answer"


def test_session_reply_after_the_user_message_still_ends_it(cfg_slug, monkeypatch):
    """The other half of the same predicate: a real reply, even with system
    chatter piled on top of it, means nothing hangs."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 900), "author": "user", "text": "please answer"},
        {"timestamp": _iso(now - 600), "author": f"session:{SID}", "text": "here"},
        {"timestamp": _iso(now - 300), "author": "system:task-lifecycle",
         "text": "📋 T-0794 → totest", "fyi": False},
    ])
    dispatched = _stub(monkeypatch, activity="idle")

    assert UC.check_project(cfg, slug, now=now)["redriven"] == []
    assert dispatched == []


def test_direct_mode_fyi_record_is_not_read_as_a_user_ask(cfg_slug, monkeypatch):
    """T-0770 boundary, held: a direct-mode message enters as an ``fyi`` from
    ``system:direct-reply``, and ``tg_answer_owed`` — not this tick — owns it.
    Widening the scan past the tail must not quietly annex that path."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 900), "author": "user", "text": "q"},
        {"timestamp": _iso(now - 700), "author": f"session:{SID}", "text": "a"},
        {"timestamp": _iso(now - 300), "author": "system:direct-reply",
         "text": "into the topic", "fyi": True},
    ])
    dispatched = _stub(monkeypatch, activity="idle")

    assert UC.check_project(cfg, slug, now=now)["redriven"] == []
    assert dispatched == []


def test_torn_trailing_line_does_not_hide_the_hanging_message(cfg_slug, monkeypatch):
    cfg, slug = cfg_slug
    now = time.time()
    p = _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 600), "author": "user", "text": "please answer"},
    ])
    p.write_text(p.read_text() + '{"timestamp": "2026-07-30T10:0')  # torn write
    dispatched = _stub(monkeypatch, activity="idle")

    assert UC.check_project(cfg, slug, now=now)["redriven"] == [
        {"gid": GID, "sid": SID, "pings": 1}]


# ===========================================================================
# T-0794 — DELIVERY: the half that actually failed
#
# 22 of these alerts sat undrained in an ownerless _chat/inbox-operator.log
# from 2026-07-04 to 07-27 because `operator` was not a role keyword. It is one
# now (T-0790, live at 3749ee8) — so these tests use the REAL intersession send
# and assert the alert lands in a live operator's inbox, not merely that it was
# written.
# ===========================================================================

def _live_operator(cfg, slug, sid=OPERATOR_SID, *, status="active",
                   window="operator") -> None:
    sess = Path(cfg.data_dir) / slug / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / f"{sid}.md").write_text(
        "\n".join(["---", f"sid: {sid}", f"status: {status}",
                   "task_id: ~", f"window: {window}", "---", ""])
    )


def _hanging_thread(cfg, slug, monkeypatch, *, minutes_ago=20):
    """An unanswered message old enough that the 2nd ping — the one that
    escalates — is due."""
    msg_at = time.time() - minutes_ago * 60
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(msg_at), "author": "user", "text": "unanswered"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")
    # Hermetic: no tmux scan for the unregistered-operator branch (T-0523).
    monkeypatch.setattr(S, "list_panes", lambda: [])
    return msg_at, dispatched


def test_escalation_resolves_to_a_live_operator_sid(cfg_slug, monkeypatch):
    """DoD: not "an alert was written" — it RESOLVES to a live operator sid and
    is readable in that session's own inbox."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    msg_at, _ = _hanging_thread(cfg, slug, monkeypatch)
    _live_operator(cfg, slug)

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))

    read = I.inbox_read(cfg, slug, OPERATOR_SID)
    assert read["count"] == 1
    assert GID in read["messages"][0]
    assert "UNANSWERED user message" in read["messages"][0]
    assert not (Path(cfg.data_dir) / slug / "_chat" / "inbox-operator.log").exists()

    state = json.loads((Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
                        / "state.json").read_text())
    assert state[GID]["escalated_to"] == [OPERATOR_SID]


def test_escalation_reaching_nobody_is_logged_and_stays_unescalated(
        cfg_slug, monkeypatch, caplog):
    """The no-live-operator case, explicitly (precedent 33f6657). With no
    operator on duty the alert reaches nothing — that must be visible, and it
    must NOT be recorded as escalated, or one empty fan-out spends the only
    alert this message will ever get."""
    cfg, slug = cfg_slug
    msg_at, _ = _hanging_thread(cfg, slug, monkeypatch)  # no operator session md

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    with caplog.at_level(logging.WARNING, logger="bot_squad_worker.uc_redrive"):
        UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))

    assert "reached NO live operator" in caplog.text
    assert GID in caplog.text
    state = json.loads((Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
                        / "state.json").read_text())
    assert state[GID]["escalated_to"] == []


def test_escalation_retries_on_the_next_slot_until_it_lands(cfg_slug, monkeypatch):
    """An operator comes on duty after the first escalation evaporated: the
    next ping slot re-sends, and once it lands it is not re-sent again."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    msg_at, _ = _hanging_thread(cfg, slug, monkeypatch)

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))  # nobody home

    _live_operator(cfg, slug)
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(2))
    assert I.inbox_read(cfg, slug, OPERATOR_SID)["count"] == 1

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(3))
    assert I.inbox_read(cfg, slug, OPERATOR_SID)["count"] == 0  # delivered once


def test_no_escalation_before_the_ramp_is_spent(cfg_slug, monkeypatch):
    """One ping in, the message may still just be slow — a human is told when
    the ~5 and ~15 minute nudges have both failed, not before."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    msg_at, _ = _hanging_thread(cfg, slug, monkeypatch)
    _live_operator(cfg, slug)

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    assert I.inbox_read(cfg, slug, OPERATOR_SID)["count"] == 0


def test_escalation_names_how_long_the_message_has_hung(cfg_slug, monkeypatch):
    """The alert reports his condition (a user message hanging, and for how
    long), not just the count of re-wake attempts that stood in for it."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    msg_at, _ = _hanging_thread(cfg, slug, monkeypatch)
    _live_operator(cfg, slug)

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))

    msg = I.inbox_read(cfg, slug, OPERATOR_SID)["messages"][0]
    assert "hanging 15 min" in msg
    assert "2 re-wake ping(s)" in msg


def test_a_dead_operator_session_is_not_a_delivery(cfg_slug, monkeypatch):
    """An archived/suspended operator md must not count as reached — that is
    exactly how the alert went to an owner nobody was reading."""
    cfg, slug = cfg_slug
    msg_at, _ = _hanging_thread(cfg, slug, monkeypatch)
    _live_operator(cfg, slug, sid="S-u-operator-p0", status="suspended")

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))

    state = json.loads((Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
                        / "state.json").read_text())
    assert state[GID]["escalated_to"] == []
