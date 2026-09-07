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
    assert result == {"ok": True, "redriven": [], "probe_broken": [],
                      "aged_out": []}
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
    assert result["redriven"] == [{"gid": GID, "sid": SID, "pings": 1, "thread": ""}]
    assert len(dispatched) == 1
    name, params = dispatched[0]
    assert name == "ensure_user_conversation"
    # T-1060: the re-drive marks itself as the SYSTEM waking an idle attendant,
    # not the user messaging it — without that marker every nudge on this
    # cadence resets the recycle clock and the attendant can never reach its
    # idle deadline (so it never takes a deliberate suspend at all).
    assert params == {"slug": slug, "global_user_id": GID,
                       "message_ref": "why no answer?",
                       "system_wake": True}

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
    assert result["redriven"] == [{"gid": GID, "sid": SID, "pings": 1, "thread": ""}]
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

    # The counted quantity is pings SENT, not the schedule position: five slots
    # came due across the two hours, three nudges were actually delivered, and
    # the operator alert has to report three.
    state = json.loads((Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
                        / "state.json").read_text())[GID]
    assert (state["slot"], state["pings"]) == (5, 3)


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

    assert result["redriven"] == [{"gid": GID, "sid": SID, "pings": 1, "thread": ""}]
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
        {"gid": GID, "sid": SID, "pings": 1, "thread": ""}]


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
    escalates — is due. Returns ``(msg_at, dispatched, nudged)``."""
    msg_at = time.time() - minutes_ago * 60
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(msg_at), "author": "user", "text": "unanswered"},
    ])
    dispatched = _stub(monkeypatch, activity="idle")
    # Hermetic: no tmux scan for the unregistered-operator branch (T-0523), and
    # no real pane injection.
    monkeypatch.setattr(S, "list_panes", lambda: [])
    nudged: list[dict] = []
    monkeypatch.setattr(A, "_action_inject_input", lambda p: nudged.append(p))
    return msg_at, dispatched, nudged


def test_escalation_resolves_to_a_live_operator_sid(cfg_slug, monkeypatch):
    """DoD: not "an alert was written" — it RESOLVES to a live operator sid and
    is readable in that session's own inbox."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)
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


def test_escalation_nudges_the_operator_pane_it_resolved_to(cfg_slug, monkeypatch):
    """Landing in the inbox FILE is delivery; the "check mail" nudge is what
    makes it read. Written-somewhere-correct-that-nobody-opened is the failure
    this ticket exists to end, so the alert signals the pane the way
    ``bsq peer send`` does (precedent: tg_stall._redirect_to_upstream)."""
    cfg, slug = cfg_slug
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)
    _live_operator(cfg, slug)

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    assert nudged == []  # nothing escalated yet — no nudge either
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))

    assert nudged == [{"sid": OPERATOR_SID, "text": "check mail"}]


def test_no_operator_no_nudge(cfg_slug, monkeypatch):
    """Nothing was delivered, so there is no pane to nudge — the escalation
    must not fabricate one out of the role keyword."""
    cfg, slug = cfg_slug
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))

    assert nudged == []


def test_escalation_reaching_nobody_is_logged_and_stays_unescalated(
        cfg_slug, monkeypatch, caplog):
    """The no-live-operator case, explicitly (precedent 33f6657). With no
    operator on duty the alert reaches nothing — that must be visible, and it
    must NOT be recorded as escalated, or one empty fan-out spends the only
    alert this message will ever get."""
    cfg, slug = cfg_slug
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)  # no operator md

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
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)

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
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)
    _live_operator(cfg, slug)

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    assert I.inbox_read(cfg, slug, OPERATOR_SID)["count"] == 0


def test_escalation_names_how_long_the_message_has_hung(cfg_slug, monkeypatch):
    """The alert reports his condition (a user message hanging, and for how
    long), not just the count of re-wake attempts that stood in for it."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)
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
    msg_at, _, nudged = _hanging_thread(cfg, slug, monkeypatch)
    _live_operator(cfg, slug, sid="S-u-operator-p0", status="suspended")

    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(0))
    UC.check_project(cfg, slug, now=msg_at + UC.ping_due_after_sec(1))

    state = json.loads((Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
                        / "state.json").read_text())
    assert state[GID]["escalated_to"] == []


# ===========================================================================
# T-0917 — the probe's INPUT. Its cadence and trigger were both correct; the
# log it read had stopped carrying answers. Each test below names the property
# whose removal it must catch, because a guard nobody proved can fail is prose
# (feedback: prove-the-guard-can-fail).
# ===========================================================================

def _write_topic(cfg, slug, gid, thread, records, *, mtime=None):
    """One per-topic log: ``<slug>/<gid>/t<N>.jsonl`` beside ``<gid>.jsonl``."""
    d = Path(cfg.data_dir) / "_mothership" / "conversations" / slug / gid
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{thread}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    if mtime is not None:
        import os
        os.utime(p, (mtime, mtime))
    return p


def test_root_log_ask_is_answered_by_a_reply_in_a_topic_log(cfg_slug, monkeypatch):
    """THE LIVE INCIDENT, reproduced to the minute (2026-08-18).

    ``_fallback_undelivered`` files an undeliverable message into the ROOT log
    THREADLESS; the attendant's reply is filed in the topic it was sent to. The
    root log therefore cannot hold its own answer, and requiring one there
    produced 23 pings and an operator escalation for a message answered 67
    seconds after it arrived.

    Property: a root-log ask is answered by a ``session:`` record in ANY log of
    the same gid. Delete the ``newest_answer`` check in ``check_project`` and
    this goes red.
    """
    cfg, slug = cfg_slug
    now = time.time()
    ask = now - 3600
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(ask - 60), "author": "system:undelivered",
         "text": "адресовано сессии S-x, она не активна"},
        {"timestamp": _iso(ask), "author": "user", "text": "задача про aqice"},
    ])
    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(ask - 12), "author": "user", "text": "что с презой?"},
        {"timestamp": _iso(ask + 67), "author": f"session:{SID}",
         "text": "Принял всё"},
    ])
    dispatched = _stub(monkeypatch)

    result = UC.check_project(cfg, slug, now=now)

    assert result["redriven"] == []
    assert dispatched == []


def test_a_hang_in_a_topic_log_is_seen_at_all(cfg_slug, monkeypatch):
    """The per-topic logs were invisible to this module — it globbed only
    ``*.jsonl`` at the top of the conversations dir, and since the forum-topic
    era that is not where the conversation is. Property: the sweep descends one
    level. Revert ``_conversation_files`` to a single ``*.jsonl`` glob and this
    goes red."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(now - 600), "author": f"session:{SID}", "text": "hi"},
        {"timestamp": _iso(now - 590), "author": "user", "text": "please answer"},
    ])
    dispatched = _stub(monkeypatch)

    result = UC.check_project(cfg, slug, now=now)

    assert result["redriven"] == [
        {"gid": GID, "sid": SID, "pings": 1, "thread": "t11"}]
    assert [n for n, _ in dispatched] == ["ensure_user_conversation"]


def test_a_topic_log_is_judged_alone_not_by_its_siblings(cfg_slug, monkeypatch):
    """The cross-log answer check is deliberately NOT symmetric. A per-topic log
    carries both halves, so a ``session:`` record in some OTHER topic — a dev
    session posting a status line into a per-task topic, which the live install
    does — must not be read as an answer to a hang in this one.

    Property: only ``thread == ""`` consults ``newest_answer``. Drop that
    condition and this goes red while
    ``test_root_log_ask_is_answered_by_a_reply_in_a_topic_log`` stays green —
    which is why both exist.
    """
    cfg, slug = cfg_slug
    now = time.time()
    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(now - 600), "author": f"session:{SID}", "text": "hi"},
        {"timestamp": _iso(now - 590), "author": "user", "text": "please answer"},
    ])
    _write_topic(cfg, slug, GID, "t1181", [
        {"timestamp": _iso(now - 60), "author": "session:S-u-chart-round3-p55",
         "text": "деплой прошёл"},
    ])
    _stub(monkeypatch)

    result = UC.check_project(cfg, slug, now=now)

    assert [r["thread"] for r in result["redriven"]] == ["t11"]


def test_root_and_topic_logs_keep_independent_ping_campaigns(cfg_slug, monkeypatch):
    """Two logs of one gid hang independently, so their state cannot share a
    key. Property: ``_conversation_files`` keys a topic ``<gid>/t<N>``. Key both
    on the bare gid and one campaign silently overwrites the other."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(now - 3000), "author": f"session:{SID}", "text": "hi"},
        {"timestamp": _iso(now - 2000), "author": "user", "text": "topic ask"},
    ])
    _write_topic(cfg, slug, GID, "t23", [
        {"timestamp": _iso(now - 3000), "author": f"session:{SID}", "text": "hi"},
        {"timestamp": _iso(now - 600), "author": "user", "text": "other ask"},
    ])
    _stub(monkeypatch)

    UC.check_project(cfg, slug, now=now)

    state = json.loads((Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
                        / "state.json").read_text())
    assert set(state) == {f"{GID}/t11", f"{GID}/t23"}
    assert state[f"{GID}/t11"]["msg_ts"] != state[f"{GID}/t23"]["msg_ts"]


# --- PROBE-BROKEN (ticket item 2) -----------------------------------------

def _probe_broken_log(cfg, slug, now, *, gid=GID):
    """A log whose answer half died a week ago while asks kept landing — the
    shape watchrobot's root log had for seven days."""
    day = 86400
    _write_thread(cfg, slug, gid, [
        {"timestamp": _iso(now - 7 * day), "author": f"session:{SID}", "text": "ok"},
        {"timestamp": _iso(now - 3 * day), "author": "user", "text": "ask one"},
        {"timestamp": _iso(now - 2 * day), "author": "user", "text": "ask two"},
        {"timestamp": _iso(now - 600), "author": "user", "text": "ask three"},
    ])


def test_probe_broken_is_a_different_verdict_and_pings_nobody(cfg_slug, monkeypatch):
    """Ticket item 2, verbatim: a predicate that cannot say "answered" must
    shout PROBE-BROKEN rather than "hanging". Property: the ``probe_broken``
    branch precedes the cadence and ``continue``s. Remove it and this log is
    reported as an ordinary hang and the attendant is nudged."""
    cfg, slug = cfg_slug
    now = time.time()
    _probe_broken_log(cfg, slug, now)
    dispatched = _stub(monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)

    result = UC.check_project(cfg, slug, now=now)

    assert result["redriven"] == []
    assert dispatched == []
    assert [d["key"] for d in result["probe_broken"]] == [GID]
    assert result["probe_broken"][0]["asks_since"] == 3


def test_probe_broken_escalation_says_it_cannot_read_the_log(cfg_slug, monkeypatch):
    """The two verdicts must not read alike: the previous one sent two people to
    wake an attendant that had already replied. Property: a distinct message via
    ``_notify_probe_broken``."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    now = time.time()
    _probe_broken_log(cfg, slug, now)
    _stub(monkeypatch)
    _live_operator(cfg, slug)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    nudged: list[dict] = []
    monkeypatch.setattr(A, "_action_inject_input", lambda p: nudged.append(p))

    UC.check_project(cfg, slug, now=now)

    msg = I.inbox_read(cfg, slug, OPERATOR_SID)["messages"][0]
    assert "PROBE-BROKEN" in msg
    assert "NOT pinging" in msg
    assert "UNANSWERED user message" not in msg
    assert [n["sid"] for n in nudged] == [OPERATOR_SID]


def test_probe_broken_escalation_retries_until_it_lands(cfg_slug, monkeypatch):
    """Same contract as the hanging escalation (T-0790): an alert that reached
    NOBODY is not recorded as done. Property: ``escalated_to`` empty ⇒ re-send
    next tick."""
    from bot_squad_worker import intersession as I

    cfg, slug = cfg_slug
    now = time.time()
    _probe_broken_log(cfg, slug, now)
    _stub(monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)

    UC.check_project(cfg, slug, now=now)          # no operator alive yet
    state = json.loads((Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
                        / "state.json").read_text())
    assert state[f"probe:{GID}"]["escalated_to"] == []

    _live_operator(cfg, slug)
    UC.check_project(cfg, slug, now=now + 60)

    assert "PROBE-BROKEN" in I.inbox_read(cfg, slug, OPERATOR_SID)["messages"][0]


def test_one_ask_after_a_stale_reply_is_a_hang_not_a_broken_probe(cfg_slug, monkeypatch):
    """The inverted error is as bad as the original: calling an ordinary hanging
    message "instrument dead" is how a real hang stops being pinged. Property:
    :func:`probe_broken`'s span clause — one ask spans zero seconds, which can
    never exceed the staleness window. (A separate minimum-ask-count constant
    used to sit beside that clause; a mutation pass showed it could never fire
    on its own, so it is gone and this test pins the clause that does.)"""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 7 * 86400), "author": f"session:{SID}", "text": "ok"},
        {"timestamp": _iso(now - 600), "author": "user", "text": "the only ask"},
    ])
    dispatched = _stub(monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)

    result = UC.check_project(cfg, slug, now=now)

    assert result["probe_broken"] == []
    assert result["redriven"] == [
        {"gid": GID, "sid": SID, "pings": 1, "thread": ""}]
    assert [n for n, _ in dispatched] == ["ensure_user_conversation"]


def test_asks_must_span_the_window_before_the_log_is_called_broken(cfg_slug, monkeypatch):
    """Two asks minutes apart after a long quiet spell is a busy morning, not a
    dead log. Property: the ``asks_span_sec > stale`` clause."""
    cfg, slug = cfg_slug
    now = time.time()
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(now - 9 * 86400), "author": f"session:{SID}", "text": "ok"},
        {"timestamp": _iso(now - 900), "author": "user", "text": "ask one"},
        {"timestamp": _iso(now - 600), "author": "user", "text": "ask two"},
    ])
    _stub(monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)

    result = UC.check_project(cfg, slug, now=now)

    assert result["probe_broken"] == []
    assert [r["pings"] for r in result["redriven"]] == [1]


def test_a_log_that_never_carried_a_reply_is_not_probe_broken(cfg_slug, monkeypatch):
    """A per-task topic is a monologue by design and a new conversation has no
    history — neither has a "stopped" to detect. Property: the
    ``if not newest: return None`` guard."""
    cfg, slug = cfg_slug
    now = time.time()
    recs = [{"timestamp": _iso(now - (5 - i) * 86400), "author": "user",
             "text": f"ask {i}"} for i in range(4)]
    _write_thread(cfg, slug, GID, recs)
    _stub(monkeypatch)

    assert UC.probe_broken(recs, now=now) is None
    assert UC.check_project(cfg, slug, now=now)["probe_broken"] == []


def test_probe_broken_clears_when_a_sibling_topic_answers(cfg_slug, monkeypatch):
    """A diagnosis that never retracts is a second stuck alarm — and there are
    THREE paths on which a log becomes readable again, each with its own
    ``state.pop(pkey)``. This one: the answer arrives in a sibling topic log.
    A mutation pass caught the other two being untested."""
    cfg, slug = cfg_slug
    now = time.time()
    _probe_broken_log(cfg, slug, now)
    _stub(monkeypatch)
    _live_operator(cfg, slug)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)
    UC.check_project(cfg, slug, now=now)
    sp = Path(cfg.data_dir) / slug / "_worker" / "uc_redrive" / "state.json"
    assert f"probe:{GID}" in json.loads(sp.read_text())

    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(now - 300), "author": f"session:{SID}", "text": "answered"},
    ])
    UC.check_project(cfg, slug, now=now + 60)

    assert f"probe:{GID}" not in json.loads(sp.read_text())


# --- the aged-out floor: the fix must not become the failure ---------------

def test_an_old_untracked_hang_opens_no_ping_campaign(cfg_slug, monkeypatch):
    """Widening the sweep uncovered eight dormant hangs on the live install, the
    oldest from 2026-07-31. Pinging and escalating each would have been this
    ticket's own failure mode delivered by its fix. Property:
    :func:`max_first_ping_age_sec` applied while ``pings == 0``."""
    cfg, slug = cfg_slug
    now = time.time()
    old = now - 12 * 86400
    _write_topic(cfg, slug, GID, "t278", [
        {"timestamp": _iso(old - 60), "author": f"session:{SID}", "text": "hi"},
        {"timestamp": _iso(old), "author": "user", "text": "ancient ask"},
    ])
    dispatched = _stub(monkeypatch)

    result = UC.check_project(cfg, slug, now=now)

    assert result["redriven"] == []
    assert dispatched == []
    assert [a["key"] for a in result["aged_out"]] == [f"{GID}/t278"]


def test_a_campaign_already_running_keeps_pinging_past_the_floor(cfg_slug, monkeypatch):
    """The floor is about opening a campaign, never about bounding one — the
    bound was the bug T-0794 removed. Property: the age test is gated on
    ``pings == 0``, so one ping already sent exempts the campaign forever."""
    cfg, slug = cfg_slug
    now = time.time()
    msg_at = now - 600
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(msg_at), "author": "user", "text": "still hanging"},
    ])
    _stub(monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)

    UC.check_project(cfg, slug, now=now)                       # ping 1, tracked
    late = msg_at + 3 * 86400                                  # well past the floor
    result = UC.check_project(cfg, slug, now=late)

    assert result["aged_out"] == []
    assert [r["pings"] for r in result["redriven"]] == [2]


def test_a_dormant_log_is_not_read_every_tick(cfg_slug, monkeypatch):
    """A log untouched for longer than the floor cannot hold a pingable message
    (its mtime is never older than its newest record), so re-reading every
    archived topic once a minute buys nothing. Property: the mtime skip in
    :func:`_conversation_files`."""
    cfg, slug = cfg_slug
    now = time.time()
    old = now - 30 * 86400
    _write_topic(cfg, slug, GID, "t23", [
        {"timestamp": _iso(old), "author": "user", "text": "archived ask"},
    ], mtime=old)
    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(now - 600), "author": f"session:{SID}", "text": "hi"},
    ])
    _stub(monkeypatch)

    keys = [c["key"] for c in UC._conversation_files(
        Path(cfg.data_dir) / "_mothership" / "conversations" / slug,
        now=now, max_age_sec=UC.max_first_ping_age_sec())]

    assert keys == [f"{GID}/t11"]
    assert UC.check_project(cfg, slug, now=now)["aged_out"] == []


def test_probe_broken_clears_when_the_log_itself_is_answered(cfg_slug, monkeypatch):
    """Second clearing path: the log's own newest record becomes a reply, so
    ``_unanswered_in`` returns ``None``. Property: the ``state.pop(pkey)`` in
    the ``rec is None`` branch."""
    cfg, slug = cfg_slug
    now = time.time()
    _probe_broken_log(cfg, slug, now)
    _stub(monkeypatch)
    _live_operator(cfg, slug)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)
    UC.check_project(cfg, slug, now=now)
    sp = Path(cfg.data_dir) / slug / "_worker" / "uc_redrive" / "state.json"
    assert f"probe:{GID}" in json.loads(sp.read_text())

    p = Path(cfg.data_dir) / "_mothership" / "conversations" / slug / f"{GID}.jsonl"
    p.write_text(p.read_text() + json.dumps(
        {"timestamp": _iso(now - 60), "author": f"session:{SID}",
         "text": "answered at last"}) + "\n")
    UC.check_project(cfg, slug, now=now + 60)

    assert f"probe:{GID}" not in json.loads(sp.read_text())


def test_probe_broken_clears_while_the_message_still_hangs(cfg_slug, monkeypatch):
    """Third clearing path, and the one that matters most: the log starts
    recording replies again but the newest message is still an unanswered ask.
    The verdict must fall back to an ordinary hang — pinged, not diagnosed.
    Property: the mid-loop ``state.pop(pkey)`` on the not-broken path."""
    cfg, slug = cfg_slug
    now = time.time()
    _probe_broken_log(cfg, slug, now)
    dispatched = _stub(monkeypatch)
    _live_operator(cfg, slug)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)
    UC.check_project(cfg, slug, now=now)
    sp = Path(cfg.data_dir) / slug / "_worker" / "uc_redrive" / "state.json"
    assert f"probe:{GID}" in json.loads(sp.read_text())
    dispatched.clear()

    p = Path(cfg.data_dir) / "_mothership" / "conversations" / slug / f"{GID}.jsonl"
    p.write_text(p.read_text() + "".join(json.dumps(r) + "\n" for r in [
        {"timestamp": _iso(now - 900), "author": f"session:{SID}", "text": "back"},
        {"timestamp": _iso(now - 800), "author": "user", "text": "a fresh ask"},
    ]))
    result = UC.check_project(cfg, slug, now=now + 60)

    assert f"probe:{GID}" not in json.loads(sp.read_text())
    assert result["probe_broken"] == []
    assert [r["pings"] for r in result["redriven"]] == [1]
    assert [n for n, _ in dispatched] == ["ensure_user_conversation"]


def test_a_legacy_state_stub_does_not_smuggle_an_ancient_hang_past_the_floor(
        cfg_slug, monkeypatch):
    """Measured on the live install while walking this fix over a copy of it:
    ``watchrobot/gu_5e3d…`` carries a state entry written under the pre-T-0794
    schema (``retries``/``last_redrive_at``, no ``pings``) whose ``msg_ts``
    still MATCHES the hanging record — so the reset branch never runs and a
    24-day-old message would have been pinged as though tracked. Property: the
    floor tests ``pings``, not whether a state entry happens to exist."""
    cfg, slug = cfg_slug
    now = time.time()
    old = now - 24 * 86400
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(old), "author": "user", "text": "ancient"},
    ])
    sp = Path(cfg.data_dir) / slug / "_worker" / "uc_redrive"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "state.json").write_text(json.dumps({GID: {
        "msg_ts": _iso(old), "retries": 3, "last_redrive_at": old}}))
    dispatched = _stub(monkeypatch)

    result = UC.check_project(cfg, slug, now=now)

    assert result["redriven"] == []
    assert dispatched == []
    assert [a["key"] for a in result["aged_out"]] == [GID]


# ===========================================================================
# T-0917 hardening (operator review hold on 4fed246). The first draft let ANY
# newer session: record in ANY sibling topic close a ROOT ask — the same false
# negative the per-topic asymmetry prevents, reintroduced through the other
# door. Measured on the live install before narrowing it: 27 distinct sids that
# are NOT the attendant post into watchrobot's t11 alone.
# ===========================================================================

def test_a_root_ask_is_not_answered_by_an_unrelated_dev_session_in_a_sibling(
        cfg_slug, monkeypatch):
    """THE NEGATIVE CONTROL the review named. A dev session posting a status
    line into a per-task topic must NOT close a genuinely unanswered root ask.

    The sid is a real one, lifted from the live store: ``chart-round3-p55``
    authors 10 records in ``watchrobot/gu_dc82…/t1181.jsonl``. Property:
    :func:`is_attendant_answer` gates the cross-log map. Widen that map back to
    every ``session:`` record and this goes red while
    ``test_root_log_ask_is_answered_by_a_reply_in_a_topic_log`` stays green —
    which is the pair that pins the rule."""
    cfg, slug = cfg_slug
    now = time.time()
    ask = now - 3600
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(ask), "author": "user", "text": "unanswered ask"},
    ])
    _write_topic(cfg, slug, GID, "t1181", [
        {"timestamp": _iso(ask + 60), "author": "session:S-u-chart-round3-p55",
         "text": "деплой прошёл"},
    ])
    dispatched = _stub(monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)

    result = UC.check_project(cfg, slug, now=now)

    assert [r["thread"] for r in result["redriven"]] == [""]
    assert [n for n, _ in dispatched] == ["ensure_user_conversation"]


def test_a_root_ask_is_not_answered_by_an_operator_post_in_a_sibling(
        cfg_slug, monkeypatch):
    """The same gate, on the class that is FAR more common and therefore the
    one that would have silenced this alarm in practice: 27 distinct
    ``S-almdudleer-operator-p*`` sids write into watchrobot's t11.

    This over-reports — the operator answering him in a topic is a real answer
    — and that is the deliberate direction. The operator is not the session
    this module re-drives, and for an alarm whose failure mode is silence, a
    spurious nudge to an idle attendant costs less than a hang nobody sees."""
    cfg, slug = cfg_slug
    now = time.time()
    ask = now - 3600
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(ask), "author": "user", "text": "unanswered ask"},
    ])
    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(ask + 120), "author": f"session:{OPERATOR_SID}",
         "text": "[🎧 operator] про режим забудьте"},
    ])
    dispatched = _stub(monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(A, "_action_inject_input", lambda p: None)

    result = UC.check_project(cfg, slug, now=now)

    assert [r["thread"] for r in result["redriven"]] == [""]
    assert [n for n, _ in dispatched] == ["ensure_user_conversation"]


def test_the_attendant_lineage_is_read_from_the_spawn_producer(cfg_slug):
    """"The attendant" must mean the same thing here as at the gate that picks
    a session to nudge, so it is derived from
    :func:`sessions.user_conversation_window` — the producer that NAMES the
    window at spawn — and matched with ``_window_from_sid``, the derivation
    ``live_user_conversation_sid`` applies. A hand-typed
    ``…-user-conversation-p…`` pattern would pass this file and silently stop
    agreeing with that gate the day the scheme changes.

    The sid here is COMPOSED from the producer rather than typed, so a rename
    moves both sides together (T-0917 review)."""
    from bot_squad_worker import sessions as SS

    win = SS.user_conversation_window(GID)
    composed = f"S-almdudleer-{win}-p151"          # a real live shape
    assert UC.attendant_window(GID) == win
    assert UC.is_attendant_answer(f"session:{composed}", win)

    # …and every non-attendant class measured in the live store is rejected.
    for other in ("S-almdudleer-operator-p373", "S-u-chart-round3-p55",
                  "S-almdudleer-portfolio-signals-p64", "S-u-dev-p9",
                  # ANOTHER gid's attendant writing into this log — measured:
                  # gu_dc82…'s p70 authors a record in watchrobot/gu_5e3d….jsonl
                  f"S-almdudleer-{SS.user_conversation_window('gu_OTHER')}-p9",
                  # ⭐ the case that separates the producer derivation from a
                  # substring match, and the one a mutation pass caught this
                  # test missing: a session NAMED AFTER this gid that is not an
                  # attendant. "the sid contains the gid" says yes; the window
                  # it actually carries says no, and the window is what
                  # live_user_conversation_sid matches on.
                  f"S-almdudleer-{GID}-redrive-debug-p7",
                  f"S-almdudleer-{GID}-user-conversation-review-p7"):
        assert not UC.is_attendant_answer(f"session:{other}", win), other
    # a non-session author is never an answer, whatever it says
    assert not UC.is_attendant_answer("system:task-lifecycle", win)
    assert not UC.is_attendant_answer("user", win)


def test_an_unnameable_gid_has_no_lineage_and_never_raises(cfg_slug, monkeypatch):
    """``gid`` comes from a FILENAME, so it need not be a legal gid at all. The
    producer rejects those; a rejection must mean "no lineage" and leave the
    sweep running, never raise inside it. Property: the ``except`` in
    :func:`attendant_window`."""
    cfg, slug = cfg_slug
    now = time.time()
    assert UC.attendant_window("-not a gid-") == ""
    assert not UC.is_attendant_answer(f"session:{SID}", "")

    conv = Path(cfg.data_dir) / "_mothership" / "conversations" / slug
    conv.mkdir(parents=True, exist_ok=True)
    (conv / "-not a gid-.jsonl").write_text(json.dumps(
        {"timestamp": _iso(now - 600), "author": "user", "text": "hi"}) + "\n")
    _stub(monkeypatch, live_sid=None)

    result = UC.check_project(cfg, slug, now=now)   # must not raise

    assert result["probe_broken"] == []


def test_the_live_p151_answer_case_still_resolves(cfg_slug, monkeypatch):
    """The incident itself, re-asserted AFTER narrowing the gate — the whole
    point of the hardening is that it must not undo the fix. Composed from the
    producer so it pins the real attendant shape, not a string I typed."""
    from bot_squad_worker import sessions as SS

    cfg, slug = cfg_slug
    now = time.time()
    ask = now - 3600
    attendant = f"S-almdudleer-{SS.user_conversation_window(GID)}-p151"
    _write_thread(cfg, slug, GID, [
        {"timestamp": _iso(ask - 60), "author": "system:undelivered",
         "text": "адресовано сессии S-x, она не активна"},
        {"timestamp": _iso(ask), "author": "user", "text": "задача про aqice"},
    ])
    _write_topic(cfg, slug, GID, "t11", [
        {"timestamp": _iso(ask + 67), "author": f"session:{attendant}",
         "text": "Принял всё"},
        # …and an operator line AFTER it, which must change nothing either way
        {"timestamp": _iso(ask + 180), "author": f"session:{OPERATOR_SID}",
         "text": "[🎧 operator] дополню"},
    ])
    dispatched = _stub(monkeypatch)

    result = UC.check_project(cfg, slug, now=now)

    assert result["redriven"] == []
    assert dispatched == []
