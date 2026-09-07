"""T-0800 — the drive STOPPING-CONDITION axis and the stall alert (D-0069).

His ask, verbatim (2026-07-30T09:45:35Z):

    надо сделать еще в настройках драйва по проекту режим stall alert / alert me
    when done, чтобы как только всё сделано, он писал в какой-то оперативный
    канал, что ВСЁ СДЕЛАНО, ПРОВЕРЯЙ, МЫ ПРОСТАИВАЕМ

What this file is shaped by
---------------------------
**The acceptance inputs are L2's, not mine.** ``scope_with_residue`` and
``scope_truly_empty`` are imported from ``test_pickup_scope.py`` on the TL's
instruction: the alert must be accepted against the same two fixtures the scope
lane pinned, not against a re-derivation of them. That import is the mechanism —
if L2 changes what "an empty scope" means, this file moves with it instead of
quietly disagreeing.

**The obvious implementation is already pinned as a lie.**
``test_pickup_scope.test_an_empty_pickup_band_alone_would_call_a_scope_done_that_is_not``
asserts that ``not q["pickup"]`` alone answers True over a scope holding two
open tickets that need a human. So every "it fired" test here is paired with the
residue case, and the residue case asserts a DIFFERENT message rather than
silence — see the module docstring of ``drive_stop`` for why silence there
reproduces the failure the ticket exists to close.

**A negative that only asserts an absence proves little.** Every "did not send"
test drives the SAME tick that is known to send in the positive control
immediately above or below it, so a tick that is broken into permanent silence
cannot pass this file.
"""
from __future__ import annotations

import json

import pytest

from bot_squad_worker import actions as _actions
from bot_squad_worker import drive_stop
from bot_squad_worker import msg_routes
from bot_squad_worker import pace
from bot_squad_worker import pickup
from bot_squad_worker import telemetry as _telemetry

# THE ACCEPTANCE INPUTS, imported rather than rebuilt (see the docstring).
# `board` comes with them because pytest resolves fixture dependencies by name
# in the module namespace.
from tests.test_pickup_scope import (  # noqa: F401 — fixtures used by name
    NOW,
    _write_task,
    board,
    scope_truly_empty,
    scope_with_residue,
)


@pytest.fixture(autouse=True)
def _no_real_sends(monkeypatch):
    """Nothing in this file may reach a transport. Every test that cares about
    delivery installs its own recorder over this."""
    def _boom(*a, **kw):  # pragma: no cover — a failure here is the point
        raise AssertionError("a test sent without installing a recorder")
    monkeypatch.setattr(_actions, "_send_stakeholder_dm", _boom)


class _Recorder:
    """Captures ``_send_stakeholder_dm`` calls. ``sent`` is what the transport
    reports — False is how a quiet-hours DROP looks to the caller."""

    def __init__(self, sent: bool = True) -> None:
        self.calls: list[dict] = []
        self._sent = sent

    def install(self, monkeypatch):
        def _fake(cfg, **kw):
            self.calls.append(kw)
            return {"ok": True, "sent": self._sent, "channel": "tg"}
        monkeypatch.setattr(_actions, "_send_stakeholder_dm", _fake)
        return self

    @property
    def texts(self) -> list[str]:
        return [c["message"] for c in self.calls]


def _alert_on(cfg, slug, **kw):
    """Turn the mode on the way he would: through the settings surface."""
    pace.set_drive(cfg, slug, on_stop="alert", **kw)


def _tick(cfg, slug):
    """Always at the fixtures' pinned clock. L2's fixtures place their staleness
    signals relative to ``NOW``, so a tick on the wall clock would band tickets
    by how long ago 2026-05-30 was rather than by what the fixture says."""
    return drive_stop.tick(cfg, slug, now_epoch=NOW)


# ---------------------------------------------------------------------------
# DoD 1 — the mode lives in the PER-PROJECT drive settings, not a global flag
# ---------------------------------------------------------------------------

def test_the_axis_values_are_exactly_the_ones_the_per_project_config_can_store():
    """This module and ``pace`` hold two halves of one closed set: ``pace``
    validates what may be STORED, this maps it to behaviour. A value added to
    one and not the other is a setting that silently does nothing."""
    assert {drive_stop.STOP_WHEN_SCOPE, drive_stop.STOP_WHEN_QUOTA} == set(
        pace.DRIVE_STOP_WHEN)
    assert drive_stop.ON_STOP_ALERT in pace.DRIVE_ON_STOP
    assert pace.DRIVE_DEFAULTS["stop_when"] == drive_stop.STOP_WHEN_SCOPE


def test_the_mode_is_per_project_and_read_from_the_pace_record(board, monkeypatch):
    """«в настройках драйва по проекту» — the switch is one project's pace.json
    and nothing else. No env var, no global file, no second store: turning it on
    for one project leaves another untouched, which a global flag could not do."""
    cfg, slug = board
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "off"
    _alert_on(cfg, slug)
    assert _tick(cfg, slug)["action"] == "sent"

    # It came from the project's own record, and clearing that record turns it
    # back off — i.e. the setting IS the pace block, not a copy of it.
    assert json.loads(
        (cfg.data_dir / slug / "_worker" / "pace" / "pace.json").read_text()
    )["drive"]["on_stop"] == "alert"
    pace.clear_drive(cfg, slug)
    assert _tick(cfg, slug)["action"] == "off"
    assert len(rec.calls) == 1


def test_on_stop_nothing_never_even_scans_the_board(board, monkeypatch):
    """The no-op-by-construction property, asserted mechanically rather than by
    reading the code: with the default ``on_stop`` the tick returns before
    touching ``pickup_queue`` at all. A board scan per project per minute for a
    feature nobody switched on is a cost this ships without."""
    cfg, slug = board

    def _must_not_run(*a, **kw):  # pragma: no cover
        raise AssertionError("the board was scanned with on_stop=nothing")
    monkeypatch.setattr(pickup, "pickup_queue", _must_not_run)

    assert drive_stop.tick(cfg, slug) == {"action": "off"}


# ---------------------------------------------------------------------------
# DoD 2 — the trigger predicate, stated explicitly and pinned BOTH ways
# ---------------------------------------------------------------------------

def test_a_truly_empty_scope_is_the_done_state(scope_truly_empty):
    """The positive control for every negative below it. Nothing takeable AND
    nothing awaiting a human, within the CONFIGURED scope — the out-of-scope
    ``in_progress`` ticket in this fixture must not keep the drive alive."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert rec["stopped"] is True
    assert rec["reason"] == drive_stop.STOP_DONE
    assert (rec["takeable"], rec["triage_in_scope"]) == (0, 0)
    assert rec["out_of_scope"] == 1


def test_an_empty_pickup_band_with_residue_is_not_the_done_state(scope_with_residue):
    """THE failure this bundle exists to prevent. L2 pinned that the naive
    predicate calls this scope done; here the real one refuses to, and names a
    different reason instead of falling through."""
    cfg, slug = scope_with_residue
    _alert_on(cfg, slug)

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert rec["takeable"] == 0            # the naive predicate's whole input
    assert rec["triage_in_scope"] == 2     # ...and what it cannot see
    assert rec["reason"] == drive_stop.STOP_TRIAGE_BLOCKED
    assert rec["reason"] != drive_stop.STOP_DONE


def test_a_board_of_only_delivered_to_accept_work_is_not_the_done_state(board):
    """T-0951: THE second failure this bundle now prevents, alongside residue.
    A to_accept ticket is mechanically excluded and never lands in the triage
    band, so ``triage_in_scope`` alone is blind to it — before the fix this
    evaluated STOP_DONE and posted «ВСЁ СДЕЛАНО» over unaccepted deliveries."""
    cfg, slug = board
    _alert_on(cfg, slug)
    _write_task(cfg, slug, "T-0951a", status="to_accept")
    _write_task(cfg, slug, "T-0951b", status="to_accept")

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert rec["takeable"] == 0
    assert rec["triage_in_scope"] == 0     # what the old predicate alone saw
    assert rec["awaiting_acceptance"] == 2  # what it was blind to
    assert rec["reason"] == drive_stop.STOP_TRIAGE_BLOCKED
    assert rec["reason"] != drive_stop.STOP_DONE


def test_the_awaiting_acceptance_line_is_in_the_rendered_message(board):
    cfg, slug = board
    _alert_on(cfg, slug)
    _write_task(cfg, slug, "T-0951a", status="to_accept")

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)
    text = drive_stop.alert_text(rec)

    assert not text.startswith(drive_stop.HEADLINE_DONE)
    assert text.startswith(drive_stop.HEADLINE_TRIAGE_BLOCKED)
    assert "to_accept" in text
    assert "1 задача" in text


def test_the_predicate_reads_l2s_constant_not_a_copy_of_the_field_name(scope_with_residue):
    """The residue arrives under ``pickup.TRIAGE_IN_SCOPE_KEY``. Pinning the
    equality here means a rename in the lane below turns this red instead of
    silently zeroing the count that stops «ВСЁ СДЕЛАНО» being posted."""
    cfg, slug = scope_with_residue
    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert rec["triage_in_scope"] == q["drive_scope"][pickup.TRIAGE_IN_SCOPE_KEY]


def test_work_still_in_flight_is_not_a_stop(board):
    """DoD item 5, first half. One takeable ticket in scope and the drive is
    simply running — no stop, no reason, nothing to announce."""
    cfg, slug = board
    _alert_on(cfg, slug)
    _write_task(cfg, slug, "T-0001", status="open", priority="P2")

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert rec["stopped"] is False
    assert rec["reason"] is None
    assert rec["takeable"] == 1


def test_alert_text_refuses_to_render_a_stop_that_is_not_happening(board):
    """A headline with no fact under it is exactly what this ticket is trying to
    stop being sent, so rendering one is an error rather than an empty string."""
    cfg, slug = board
    _alert_on(cfg, slug)
    _write_task(cfg, slug, "T-0001", status="open")

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)
    with pytest.raises(ValueError):
        drive_stop.alert_text(rec)


def test_configured_is_reported_but_never_branched_on(board):
    """T-0828's trap #1: an absent drive block and an explicit widest scope must
    behave IDENTICALLY. ``configured`` exists so a surface can tell "never set"
    from "deliberately widest" — an alert that fired differently for the two
    would be reporting a settings artifact as a board fact."""
    cfg, slug = board
    _write_task(cfg, slug, "T-0001", status="open")

    implicit = drive_stop.evaluate(cfg, slug, now_epoch=NOW)   # no drive block
    pace.set_drive(cfg, slug, scope="all")                     # said out loud
    explicit = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert implicit["scope_configured"] is False
    assert explicit["scope_configured"] is True
    for field in ("stopped", "reason", "takeable", "triage_in_scope", "scope"):
        assert implicit[field] == explicit[field], field


# ---------------------------------------------------------------------------
# DoD 3 — the destination is the per-message-type map, never a hardcoded chat
# ---------------------------------------------------------------------------

def test_the_alert_is_a_registered_urgent_message_type():
    """T-0799's own definition of URGENT is "work is stopped, or stops soon,
    unless a human acts" — which is literally what this message says."""
    assert drive_stop.MSG_TYPE in msg_routes.TYPES
    assert msg_routes.urgency(drive_stop.MSG_TYPE) == msg_routes.URGENT
    assert drive_stop.MSG_TYPE in msg_routes.keys()


def test_the_send_names_its_type_so_the_map_can_move_it(scope_truly_empty, monkeypatch):
    """Tagging the send is what makes the destination configurable at all — an
    untagged page can never be re-routed, which is what he asked not to have."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "sent"

    assert rec.calls[0]["msg_type"] == drive_stop.MSG_TYPE


def test_a_configured_route_actually_moves_the_alert(scope_truly_empty, monkeypatch):
    """The end-to-end proof of DoD item 3, driven through the real
    ``msg_routes`` resolution rather than asserted from the call site: point the
    type at «какой-то оперативный канал» and the delivery follows it."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    sent: list[tuple] = []

    def _fake(c, **kw):
        routed = msg_routes.route(c, kw["msg_type"], slug=slug,
                                  chat_id=kw["tg_chat_id"],
                                  topic_id=kw["tg_topic_id"])
        sent.append((routed.chat_id, routed.topic_id, routed.source))
        return {"ok": True, "sent": True, "channel": "tg"}
    monkeypatch.setattr(_actions, "_send_stakeholder_dm", _fake)

    _tick(cfg, slug)
    assert sent[-1] == ("TEST_CHAT_ID", None, "default")   # today's behaviour

    msg_routes.set_route(cfg, slug, drive_stop.MSG_TYPE,
                         chat_id="-1009999", topic_id=42)
    drive_stop.rearm(cfg, slug)
    _tick(cfg, slug)
    assert sent[-1] == ("-1009999", 42, drive_stop.MSG_TYPE)


def test_a_project_with_no_chat_is_reported_not_retried_into_the_log(
        scope_truly_empty, monkeypatch):
    """No destination is a state to report, not a page to attempt every 60s."""
    import dataclasses

    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    _Recorder().install(monkeypatch)
    monkeypatch.setitem(cfg.projects, slug,
                        dataclasses.replace(cfg.projects[slug], tg_chat=""))

    assert _tick(cfg, slug)["action"] == "no-destination"


# ---------------------------------------------------------------------------
# DoD 4 — the alert says what he asked it to say (precedent: T-0795)
# ---------------------------------------------------------------------------

def test_the_done_alert_delivers_his_words_verbatim(scope_truly_empty, monkeypatch):
    """His capitals, his commas, his word order — pinned as the DELIVERED
    string, not as a constant this file also owns."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug, set_by="stakeholder", source_text="закончить всё что в опен")
    rec = _Recorder().install(monkeypatch)

    _tick(cfg, slug)

    text = rec.texts[0]
    assert text.startswith("ВСЁ СДЕЛАНО, ПРОВЕРЯЙ, МЫ ПРОСТАИВАЕМ")
    assert "ПРОВЕРЯЙ" in text and "МЫ ПРОСТАИВАЕМ" in text
    # ...and the context that makes the headline checkable rather than a slogan.
    assert slug in text
    assert "scope=open_reopened" in text
    assert "«закончить всё что в опен»" in text


def test_the_residue_alert_denies_the_done_headline_in_its_own_first_line(
        scope_with_residue, monkeypatch):
    """The judgement this lane was handed. Nothing takeable while two tickets
    need a human is idleness he must hear about — and it is NOT «ВСЁ СДЕЛАНО».
    Silence would reproduce the failure the ticket closes; the done text would
    lie. So: its own message, and one that cannot be misread at a glance in a
    notification list."""
    cfg, slug = scope_with_residue
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "sent"

    text = rec.texts[0]
    assert not text.startswith("ВСЁ СДЕЛАНО, ПРОВЕРЯЙ, МЫ ПРОСТАИВАЕМ")
    assert text.startswith("МЫ ПРОСТАИВАЕМ, НО ЭТО НЕ «ВСЁ СДЕЛАНО»")
    assert "2 задачи" in text          # the count he has to act on
    assert "ждут твоего решения" in text


@pytest.mark.parametrize("n, expected", [
    (1, "1 задача в scope ждёт твоего решения — драйв её сам не возьмёт"),
    (2, "2 задачи в scope ждут твоего решения — драйв их сам не возьмёт"),
    (5, "5 задач в scope ждут твоего решения — драйв их сам не возьмёт"),
    (11, "11 задач в scope ждут твоего решения — драйв их сам не возьмёт"),
    (21, "21 задача в scope ждёт твоего решения — драйв её сам не возьмёт"),
])
def test_the_counts_agree_with_their_verb(n, expected):
    """ONE ticket needing a decision is the commonest case there is, and «1
    задача ждут» reads as a machine talking. Caught in the manual walkthrough,
    not by a test — so it gets one now."""
    text = drive_stop.alert_text({
        "stopped": True, "reason": drive_stop.STOP_TRIAGE_BLOCKED,
        "slug": "bot-squad", "takeable": 0, "triage_in_scope": n,
        "out_of_scope": 0, "scope": "open_reopened", "on_stop": "alert",
        "invalid": {}, "stop_when": {"configured": "scope_exhausted",
                                     "effective": "scope_exhausted",
                                     "refused": False},
    })

    assert expected in text


def test_the_two_stop_messages_are_never_the_same_string(
        scope_truly_empty, scope_with_residue):
    """A regression that merged them would still deliver "a message"; this is
    what makes that visible."""
    assert drive_stop.HEADLINE_DONE != drive_stop.HEADLINE_TRIAGE_BLOCKED
    assert len(set(drive_stop.HEADLINES.values())) == len(drive_stop.HEADLINES)


def test_the_alert_states_the_mode_it_stopped_under(scope_truly_empty, monkeypatch):
    """«какой режим драйва щас стоит» reaching the alert itself: the message
    carries the scope, the stopping condition and the words that set them, so he
    can check the claim without opening a surface."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug, stop_when="scope_exhausted", set_by="stakeholder",
              source_text="закончить всё что в опен")
    rec = _Recorder().install(monkeypatch)

    _tick(cfg, slug)

    text = rec.texts[0]
    assert "stop_when=scope_exhausted" in text
    assert "on_stop=alert" in text
    assert "(stakeholder)" in text


def test_an_invalid_stored_value_shows_him_the_raw_typo(scope_truly_empty, monkeypatch):
    """T-0828's trap #2: the effective value has ALREADY fallen back, so the
    message must print the REJECTED value rather than the fallback dressed up as
    the setting."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    p = cfg.data_dir / slug / "_worker" / "pace" / "pace.json"
    raw = json.loads(p.read_text())
    raw["drive"]["stop_when"] = "spned_quota"
    p.write_text(json.dumps(raw))
    rec = _Recorder().install(monkeypatch)

    _tick(cfg, slug)

    assert "'spned_quota'" in rec.texts[0]


# ---------------------------------------------------------------------------
# DoD 5 — suppression: edge-triggered, and the negative cases
# ---------------------------------------------------------------------------

def test_work_in_flight_sends_nothing(board, monkeypatch):
    """The negative paired with a positive control in the SAME test, so a tick
    broken into permanent silence cannot pass: the alert stays quiet while a
    ticket is takeable and fires the moment it is closed."""
    cfg, slug = board
    _alert_on(cfg, slug)
    md = _write_task(cfg, slug, "T-0001", status="open", priority="P2")
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "running"
    assert rec.calls == []

    md.write_text(md.read_text().replace("status: open", "status: closed"))
    assert _tick(cfg, slug)["action"] == "sent"
    assert len(rec.calls) == 1


def test_the_alert_does_not_refire_on_every_tick(scope_truly_empty, monkeypatch):
    """The named failure: a level-triggered read of "nothing to drive" pages him
    every 60s for ever, which is what would make him mute the channel."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)

    actions = [_tick(cfg, slug)["action"] for _ in range(5)]

    assert actions == ["sent", "suppressed", "suppressed", "suppressed", "suppressed"]
    assert len(rec.calls) == 1


def test_the_latch_rearms_only_when_the_scope_becomes_non_empty_again(
        scope_truly_empty, monkeypatch):
    """The other half of edge-triggering, stated as the full cycle: fire, stay
    silent, re-arm on real work appearing, fire again when it is gone."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "sent"
    assert _tick(cfg, slug)["action"] == "suppressed"

    md = _write_task(cfg, slug, "T-0002", status="open", priority="P2")
    assert _tick(cfg, slug)["action"] == "rearmed"
    assert drive_stop.fired_reasons(cfg, slug) == []
    assert _tick(cfg, slug)["action"] == "running"

    md.write_text(md.read_text().replace("status: open", "status: closed"))
    assert _tick(cfg, slug)["action"] == "sent"
    assert len(rec.calls) == 2


def test_a_residue_episode_that_becomes_truly_done_is_real_news(
        scope_with_residue, monkeypatch):
    """The latch is keyed on the REASON, so the one transition that carries new
    information — "nothing takeable" becoming "actually finished" — is not
    swallowed by the suppression. It is still bounded: two reasons exist, so an
    idle episode costs at most two messages, never one per tick."""
    cfg, slug = scope_with_residue
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "sent"
    assert _tick(cfg, slug)["action"] == "suppressed"

    for tid in ("T-0388", "T-0612"):
        md = cfg.data_dir / slug / "backlog" / f"{tid}-x.md"
        md.write_text(md.read_text().replace("status: open", "status: closed"))

    assert _tick(cfg, slug)["action"] == "sent"
    assert _tick(cfg, slug)["action"] == "suppressed"
    assert rec.texts[0].startswith(drive_stop.HEADLINE_TRIAGE_BLOCKED)
    assert rec.texts[1].startswith(drive_stop.HEADLINE_DONE)


def test_a_send_that_did_not_land_does_not_consume_the_edge(
        scope_truly_empty, monkeypatch):
    """A quiet-hours DROP returns False from the transport. Consuming the latch
    there would lose the alert for the whole idle episode — with an edge trigger
    there is no second chance — so the tick retries and it arrives when the
    window ends instead of never."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    dropped = _Recorder(sent=False).install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "send-failed"
    assert _tick(cfg, slug)["action"] == "send-failed"
    assert drive_stop.fired_reasons(cfg, slug) == []
    assert len(dropped.calls) == 2

    landed = _Recorder(sent=True).install(monkeypatch)
    assert _tick(cfg, slug)["action"] == "sent"
    assert _tick(cfg, slug)["action"] == "suppressed"
    assert len(landed.calls) == 1


def test_a_paused_operator_is_silent_and_keeps_its_latch(scope_truly_empty, monkeypatch):
    """He caused that idleness and already knows. The latch is deliberately not
    cleared while paused, so resuming into the same idle state does not
    re-announce what was already announced."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "sent"
    pace.pause(cfg, slug, by="stakeholder", reason="ушёл")
    assert _tick(cfg, slug)["action"] == "paused"
    pace.resume(cfg, slug)
    assert _tick(cfg, slug)["action"] == "suppressed"
    assert len(rec.calls) == 1


def test_the_kill_switch_stops_the_tick_dead(scope_truly_empty, monkeypatch):
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    _Recorder().install(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIVE_STOP_ALERT", "0")

    assert drive_stop.tick(cfg, slug) == {"action": "disabled"}


def test_a_corrupt_latch_rearms_rather_than_silencing_the_channel(
        scope_truly_empty, monkeypatch):
    """Degrade toward at most one duplicate message, never toward a channel that
    has gone quiet without saying so."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)
    _tick(cfg, slug)
    drive_stop.state_path(cfg, slug).write_text("{not json")

    assert _tick(cfg, slug)["action"] == "sent"
    assert len(rec.calls) == 2


# ---------------------------------------------------------------------------
# The board it cannot see — the unknown that fires 1440 times a day
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("broken, problem", [
    (None, drive_stop.BOARD_UNREADABLE),          # the read raises
    ({}, drive_stop.BOARD_MALFORMED),             # no ok, no bands
    ({"ok": True, "pickup": [], "drive_scope": {}},
     drive_stop.BOARD_MALFORMED),                 # bands, but no residue count
    ({"ok": False, "pickup": [], "drive_scope": {"triage_in_scope": 0}},
     drive_stop.BOARD_MALFORMED),                 # the queue itself said not-ok
])
def test_a_board_it_cannot_see_is_never_reported_as_all_done(
        scope_truly_empty, monkeypatch, broken, problem):
    """An empty answer and an unanswerable question are the same shape to a
    consumer that only calls ``len()`` — and this consumer runs 1440 times a
    day. Each degradation resolves to a NAMED problem, and every stop is
    suppressed rather than defaulting to «ВСЁ СДЕЛАНО».

    Note the fixture: this is the board that WOULD legitimately fire. So these
    assert the suppression, not the absence of a trigger."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    _Recorder().install(monkeypatch)

    def _fake(*a, **kw):
        if broken is None:
            raise RuntimeError("the backlog dir went away")
        return broken
    monkeypatch.setattr(pickup, "pickup_queue", _fake)

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)
    assert rec["board_problem"] == problem
    assert rec["stopped"] is False
    assert rec["reason"] is None
    assert rec["ok"] is False

    assert _tick(cfg, slug) == {"action": "board-unknown", "problem": problem}


def test_an_unreadable_board_neither_announces_nor_rearms(
        scope_truly_empty, monkeypatch):
    """The subtler half. A blind tick must not count as "work resumed" either —
    re-arming on it would announce the SAME idle episode a second time as soon
    as the board came back."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug)
    rec = _Recorder().install(monkeypatch)

    assert _tick(cfg, slug)["action"] == "sent"
    assert drive_stop.fired_reasons(cfg, slug) == [drive_stop.STOP_DONE]

    with monkeypatch.context() as m:
        m.setattr(pickup, "pickup_queue", lambda *a, **kw: {})
        assert _tick(cfg, slug)["action"] == "board-unknown"
        assert drive_stop.fired_reasons(cfg, slug) == [drive_stop.STOP_DONE]

    assert _tick(cfg, slug)["action"] == "suppressed"
    assert len(rec.calls) == 1


def test_a_board_that_loses_the_residue_field_refuses_rather_than_guessing(
        scope_with_residue, monkeypatch):
    """The sharpest case, and the reason the shape check is not just a truthiness
    test: on THIS board the pickup band really is empty, so a consumer that
    tolerated a missing residue count would fall straight back to the naive
    predicate L2 pinned as a lie — and post «ВСЁ СДЕЛАНО» over two open
    tickets."""
    cfg, slug = scope_with_residue
    _alert_on(cfg, slug)
    _Recorder().install(monkeypatch)
    real = pickup.pickup_queue

    def _stripped(*a, **kw):
        q = real(*a, **kw)
        q["drive_scope"].pop(pickup.TRIAGE_IN_SCOPE_KEY)
        return q
    monkeypatch.setattr(pickup, "pickup_queue", _stripped)

    assert _tick(cfg, slug)["action"] == "board-unknown"


# ---------------------------------------------------------------------------
# AXIS B — «Потратить квоту», and the unknown spend that must not drive forever
# ---------------------------------------------------------------------------

def _set_quota(cfg, slug, *, remaining=None, budget=None):
    data = {"burn_tokens_per_hr": None, "remaining_tokens": remaining,
            "rate_limit_429": {"count": 0, "last_at": None}}
    if budget is not None:
        data["anchor"] = {"budget_tokens": budget, "set_at": "2026-07-14T00:00:00Z"}
    q = _telemetry._quota_path(cfg, slug)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(json.dumps(data), encoding="utf-8")


def test_spend_quota_with_no_anchor_refuses_to_be_the_active_condition(board):
    """TODAY'S REAL CASE (T-0695), not an edge: no ``_quota.json`` anchor exists,
    so the spend figure is unresolvable. It resolves to an explicit UNKNOWN that
    NAMES itself and hands the stopping condition back to ``scope_exhausted``."""
    cfg, slug = board
    _alert_on(cfg, slug, stop_when="spend_quota")

    stop = drive_stop.resolve_stop_when(cfg, slug)

    assert stop["configured"] == "spend_quota"
    assert stop["effective"] == "scope_exhausted"
    assert stop["refused"] is True
    assert stop["known"] is False
    assert stop["unknown_reason"] == drive_stop.UNKNOWN_NO_ANCHOR
    assert stop["spend_pct"] is None


def test_an_unknown_spend_never_reads_as_quota_not_spent_yet(board):
    """The single most dangerous line in D-0069, pinned. The failure is not
    "wrong number" — it is an absent value that reads identically to a real
    negative, and here that negative means DRIVE FOREVER. ``spent`` is False and
    ``known`` is False, and the two are separately readable."""
    cfg, slug = board
    _alert_on(cfg, slug, stop_when="spend_quota")
    _write_task(cfg, slug, "T-0001", status="open")

    stop = drive_stop.resolve_stop_when(cfg, slug)

    assert (stop["known"], stop["spent"]) == (False, False)
    # ...and the refusal does not SILENCE the alert: the scope condition still
    # governs, so the project is still watched rather than left to run open.
    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)
    assert rec["stopped"] is False
    assert rec["stop_when"]["effective"] == "scope_exhausted"


def test_a_refused_stopping_condition_is_stated_in_the_message(
        scope_truly_empty, monkeypatch):
    """He set ``spend_quota`` and something else decided the stop. A message
    that hid that would be the silent-fallback defect wearing a different hat."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug, stop_when="spend_quota")
    rec = _Recorder().install(monkeypatch)

    _tick(cfg, slug)

    text = rec.texts[0]
    assert "stop_when=spend_quota" in text
    assert drive_stop.UNKNOWN_NO_ANCHOR in text
    assert "scope_exhausted" in text


def test_spend_quota_with_an_anchor_stops_when_the_budget_is_consumed(board):
    """The condition working, so the refusal above is a REFUSAL and not a broken
    branch. With work still takeable, «потратить квоту» is what stops it."""
    cfg, slug = board
    _alert_on(cfg, slug, stop_when="spend_quota")
    _write_task(cfg, slug, "T-0001", status="open", priority="P2")
    _set_quota(cfg, slug, remaining=0, budget=1000)

    stop = drive_stop.resolve_stop_when(cfg, slug)
    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert (stop["effective"], stop["refused"], stop["known"]) == (
        "spend_quota", False, True)
    assert stop["spend_pct"] == 100.0 and stop["spent"] is True
    assert rec["takeable"] == 1              # work remains; the budget does not
    assert rec["reason"] == drive_stop.STOP_QUOTA_SPENT


def test_spend_quota_under_budget_does_not_stop_while_work_remains(board):
    cfg, slug = board
    _alert_on(cfg, slug, stop_when="spend_quota")
    _write_task(cfg, slug, "T-0001", status="open", priority="P2")
    _set_quota(cfg, slug, remaining=600, budget=1000)

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert rec["stop_when"]["spend_pct"] == 40.0
    assert rec["stop_when"]["spent"] is False
    assert rec["stopped"] is False


def test_spend_quota_still_stops_on_an_empty_scope(scope_truly_empty):
    """The stated departure from D-0069, which says ``spend_quota`` keeps
    driving "regardless of the scope emptying". It ADDS a terminus and never
    removes the scope one: with an empty scope there is nothing left to spend
    the budget ON, so "not stopped" would leave the system idle and silent with
    an alert configured — the same silent-idle shape the design forbids on the
    unknown-spend path, reached by a different door."""
    cfg, slug = scope_truly_empty
    _alert_on(cfg, slug, stop_when="spend_quota")
    _set_quota(cfg, slug, remaining=600, budget=1000)

    rec = drive_stop.evaluate(cfg, slug, now_epoch=NOW)

    assert rec["stop_when"]["spent"] is False   # budget not consumed
    assert rec["stopped"] is True               # ...and yet we are idling
    assert rec["reason"] == drive_stop.STOP_DONE


def test_the_terminus_is_the_budget_and_not_the_weekly_rate_target(board):
    """D-0069 Q4: ``weekly_quota_target_pct`` is a RATE control (it adjusts
    concurrency) and this is a TERMINUS (it decides when to stop). Keying the
    terminus off the rate control is the conflation he corrected — «у нас уже
    есть quota target, не спутайте». Spend well past a 20% target is nowhere
    near the budget being consumed."""
    cfg, slug = board
    _alert_on(cfg, slug, stop_when="spend_quota")
    (cfg.config_dir / "system_settings.toml").write_text(
        "[operator]\nweekly_quota_target_pct = 20.0\n", encoding="utf-8")
    _set_quota(cfg, slug, remaining=500, budget=1000)

    stop = drive_stop.resolve_stop_when(cfg, slug)

    assert stop["spend_pct"] == 50.0            # far over the 20% rate target
    assert stop["threshold_pct"] == 100.0
    assert stop["spent"] is False


def test_a_quota_read_that_blows_up_is_an_unknown_not_a_go_ahead(board, monkeypatch):
    """The other unknown. An exception in the pacing read must land in the same
    explicit-UNKNOWN state as a missing anchor, never leave ``spend_quota``
    active with a spend of "no idea"."""
    cfg, slug = board
    _alert_on(cfg, slug, stop_when="spend_quota")
    from bot_squad_worker import operator_redrive as _ord

    def _boom(*a, **kw):
        raise RuntimeError("telemetry is on fire")
    monkeypatch.setattr(_ord, "pacing_status", _boom)

    stop = drive_stop.resolve_stop_when(cfg, slug)

    assert stop["refused"] is True
    assert stop["known"] is False
    assert stop["unknown_reason"] == drive_stop.UNKNOWN_UNREADABLE
    assert stop["effective"] == "scope_exhausted"


# ---------------------------------------------------------------------------
# Wiring — the tick exists and one bad project cannot kill the sweep
# ---------------------------------------------------------------------------

def test_a_blind_tick_says_so_out_loud(board, monkeypatch, caplog):
    """The gate being right is not enough if its output is invisible. Every
    other action is correctly silent, so a suppressed stop that logged nothing
    would read exactly like a tick that found work in flight — and nobody audits
    1440 quiet no-ops a day."""
    import logging

    cfg, slug = board
    _alert_on(cfg, slug)
    _Recorder().install(monkeypatch)
    monkeypatch.setattr(pickup, "pickup_queue", lambda *a, **kw: {})

    with caplog.at_level(logging.WARNING, logger="bot_squad_worker.drive_stop"):
        drive_stop.drive_stop_tick(cfg)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a board it could not read passed by without a word"
    assert drive_stop.BOARD_MALFORMED in warnings[-1].getMessage()
    assert slug in warnings[-1].getMessage()


def test_a_normal_quiet_tick_stays_quiet(board, monkeypatch, caplog):
    """The control for the test above: a tick that legitimately found work must
    NOT log a warning, or the loud signal stops being a signal."""
    import logging

    cfg, slug = board
    _alert_on(cfg, slug)
    _write_task(cfg, slug, "T-0001", status="open", priority="P2")
    _Recorder().install(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="bot_squad_worker.drive_stop"):
        drive_stop.drive_stop_tick(cfg)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_sweep_survives_one_bad_project(board, monkeypatch):
    cfg, slug = board
    _alert_on(cfg, slug)
    seen: list[str] = []

    def _explode(c, s):
        seen.append(s)
        raise RuntimeError("boom")
    monkeypatch.setattr(drive_stop, "tick", _explode)

    drive_stop.drive_stop_tick(cfg)          # must not raise

    assert seen == [slug]


def test_the_scheduler_registers_the_tick(board):
    """A module nothing calls is a feature that does not exist. Asserted against
    a BUILT scheduler rather than against the source text, so an import that
    resolves to the wrong function still fails."""
    from bot_squad_worker import jobs
    from bot_squad_worker.scheduler import build_scheduler

    cfg, _slug = board
    sched = build_scheduler(cfg)          # builds; does not start

    job = {j.id: j for j in sched.get_jobs()}.get("drive_stop")
    assert job is not None
    assert job.func is jobs.drive_stop_tick
