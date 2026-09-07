"""T-0979 — the check-mail nudge is emitted per peer_send on the uncoalesced
direct lane, so N sends to one session cost N agent turns even when a single
`bsq inbox check` drains all N.

The specimen: FOURTEEN identical `check mail` lines in one session's composer
inside one turn, and `bsq inbox check` answered "(no new mail)".

Two independent reasons a nudge is waste, and the tests below keep them apart
because the fixes are different and only one of them collapses a burst:

  * unread == 0 — nothing to be told about (the post-drain nudge);
  * the read mark has not moved since a nudge that WAS submitted — they have
    already been told and one drain takes everything (the burst).

And the safety direction, which is the point of the whole file: this may never
suppress a wake that did not land. A nudge that was typed into a composer and
never submitted has told nobody, so it earns no quiet.
"""
from __future__ import annotations

import json

import pytest

from bot_squad_worker import actions as A
from bot_squad_worker import input_mux
from bot_squad_worker import intersession as I
from bot_squad_worker import sessions as S
from bot_squad_worker import boot_orientation as B


SLUG = "bot-squad"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A pane, a sid, an inbox, and a transport we can make succeed or park."""
    cfg = type("C", (), {"data_dir": tmp_path})()
    pane = S.PaneInfo(pane_id="%6", window="w", pid="123",
                      cwd=str(tmp_path), command="claude")
    sid = S.compute_sid("u", pane.window, pane.pane_id)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [pane])
    monkeypatch.setattr("bot_squad_worker.park._slug_for_sid",
                        lambda cfg_, sid_: SLUG)
    monkeypatch.setattr(A, "_provider_for_pane", lambda *a, **k: "claude")

    delivered: list[str] = []
    outcome = {"value": "cleared"}

    def _fake(data_dir, sid_, pane_id, text_, **kw):
        delivered.append(text_)
        return input_mux.DirectDelivery(1, outcome["value"])

    monkeypatch.setattr(input_mux, "deliver_direct", _fake)

    chat = tmp_path / SLUG / "_chat"
    chat.mkdir(parents=True)

    def _append(text: str) -> None:
        with (chat / f"inbox-{sid}.log").open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def _drain() -> list[str]:
        return I.inbox_read(cfg, SLUG, sid)["messages"]

    def _nudge() -> dict:
        return A._action_inject_input({"sid": sid, "text": B.MAIL_SIGNAL})

    return type("Env", (), dict(
        cfg=cfg, sid=sid, delivered=delivered, outcome=outcome,
        append=staticmethod(_append), drain=staticmethod(_drain),
        nudge=staticmethod(_nudge)))


def test_a_burst_of_sends_wakes_the_session_once(env):
    """THE SPECIMEN. Fourteen peer sends inside one of the recipient's turns;
    it has not drained, so it has already been told. One composer submission,
    not fourteen — because one `bsq inbox check` takes all fourteen."""
    for i in range(14):
        env.append(f"message {i}")
        env.nudge()

    assert len(env.delivered) == 1
    assert len(env.drain()) == 14      # ...and nothing was lost to get there


def test_a_nudge_after_the_recipient_has_drained_is_not_sent(env):
    """The guaranteed-waste case: a nudge is fire-and-forget at SEND time and
    lands at DELIVERY time, which can be after the recipient drained on its own.
    Nothing is unread, so there is nothing to be told."""
    env.append("something")
    env.drain()

    res = env.nudge()

    assert env.delivered == []
    assert res["lines_sent"] == 0
    assert "drained" in res["skipped"]


def test_new_mail_after_a_drain_wakes_it_again(env):
    """The positive control for both suppressions — without it, the two greens
    above are also satisfied by a nudge that never fires at all."""
    env.append("first")
    env.nudge()
    assert len(env.delivered) == 1

    env.drain()                         # the read mark moves
    env.append("second")
    env.nudge()

    assert len(env.delivered) == 2


def test_a_nudge_that_never_submitted_earns_no_quiet(env):
    """THE SAFETY ARM. If the first nudge was typed into a composer that never
    submitted, the recipient was NOT told. Suppressing the next one would turn
    a transport failure into a silent permanent one — measured on this fleet at
    14:11:51Z, when an operator hold sat unread in a live lane for 10m16s."""
    env.outcome["value"] = "unconfirmed"
    env.append("urgent")
    first = env.nudge()
    assert first["submitted"] is False

    env.append("still urgent")
    env.nudge()                         # same mark, nothing drained

    assert len(env.delivered) == 2, "a nudge that did not land must be retried"


def test_it_fails_open_when_the_inbox_cannot_be_measured(env, monkeypatch):
    """Any uncertainty sends. A wrong 'send' costs one turn; a wrong 'skip'
    costs a message nobody ever hears about."""
    monkeypatch.setattr(I, "unread_bytes", lambda *a, **k: None)

    env.nudge()

    assert len(env.delivered) == 1


def test_it_fails_open_when_the_slug_is_unknown(env, monkeypatch):
    monkeypatch.setattr("bot_squad_worker.park._slug_for_sid",
                        lambda cfg_, sid_: None)

    env.append("mail")
    env.nudge()

    assert len(env.delivered) == 1


def test_only_the_mail_nudge_is_ever_suppressed(env):
    """`inject_input` also carries /compact, wake nudges and handoff prompts.
    None of those are answerable by `bsq inbox check`, so none of them may be
    skipped because an inbox happens to be empty."""
    env.drain()                          # unread is 0 — the suppressing state

    A._action_inject_input({"sid": env.sid, "text": "/compact"})

    assert env.delivered == ["/compact"]


def test_the_peek_never_moves_the_read_mark_or_the_heartbeat(env, tmp_path):
    """`unread_bytes` is not a drain. If it touched either file it would
    destroy the instrument that tells 'never called' from 'called and found
    nothing' — which is how this fleet's two delivery incidents were read."""
    env.append("mail")
    chat = tmp_path / SLUG / "_chat"
    hb = chat / f"heartbeat-{env.sid}"
    hb.write_text("")
    before = (hb.stat().st_mtime_ns, (chat / f"seen-{env.sid}").exists())

    assert I.unread_bytes(env.cfg, SLUG, env.sid) > 0

    assert (hb.stat().st_mtime_ns, (chat / f"seen-{env.sid}").exists()) == before


def test_unread_bytes_says_None_not_zero_when_there_is_no_inbox(env, tmp_path):
    """Absent is not known-empty. Returning 0 here would licence a suppression
    on a session whose inbox file simply has not been created yet."""
    assert I.unread_bytes(env.cfg, SLUG, "S-nobody-p0") is None
