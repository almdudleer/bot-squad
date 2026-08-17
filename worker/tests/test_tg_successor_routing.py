"""T-0896 — an answer aimed at a RECYCLED session reaches the session that
replaced it, and when nobody replaced it the refusal is audible.

The live incident (2026-08-17). The watchrobot operator asked the stakeholder
at 15:41Z to sanction a production fix. It recycled at 16:24:49Z (autocompact);
its successor started 16:25:00Z. His answer arrived at 16:26:18Z, resolved to
the DEAD sid through ``tg_reply_map``, failed to inject, and the T-0746
fallback handed it to the project's user-conversation attendant. The successor
operator — the only party that could act on a sanction — got no message and no
notice: no inbox file, no seen, no heartbeat. A human attendant noticed and
relayed it by hand; without that hand the sanction was lost with both sides
believing the ball was with the other.

Two things this file is careful about, because the ticket is:

* Every arm that expects a REFUSAL is paired with one that expects a PASS.
  ``test_live_target_...`` is that pair: a message to a LIVE session must still
  go to it, and must NOT be copied to anything else. Without it a "fix" that
  routes everything to a successor would look green.
* The refusal arms assert the CAUSE, not just the failure. "Сессия не активна"
  was already true before this ticket; what was missing is which of the three
  ways the successor lookup can come back empty actually happened.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import bot_squad_worker.tg_listener as TL
from bot_squad_worker import actions as A
from bot_squad_worker import intersession as IS


CHAT = "-100123"
GID = "gu_dc8262b6cea9098d98e04d7e"
TEXT = "да, делай эту строку"

DEAD = "S-almdudleer-operator-p319"
HEIR = "S-almdudleer-operator-p322"
LIVE = "S-almdudleer-operator-p400"


def _write_md(data_dir: Path, slug: str, sid: str, status: str) -> None:
    d = data_dir / slug / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.md").write_text(
        f"---\nsid: {sid}\nstatus: {status}\nwindow: operator\n"
        "task_id: ~\nrole: operator\nsuspend_source: autocompact\n---\n",
        encoding="utf-8",
    )


def _cfg(tmp_path: Path, sessions: dict[str, str]):
    """``sessions`` maps sid -> status, all under 'watchrobot'."""
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True, exist_ok=True)
    for sid, status in sessions.items():
        _write_md(data_dir, "watchrobot", sid, status)
    return types.SimpleNamespace(
        tg_bot_token="8206895402:SECRET",
        data_dir=data_dir,
        projects={
            "bot-squad": types.SimpleNamespace(tg_chat=CHAT),
            "watchrobot": types.SimpleNamespace(tg_chat="0"),
        },
        tg_proxy_url="",
        voice_enabled=False,
    )


@pytest.fixture()
def spy(monkeypatch):
    """Capture every observable side effect: what the sender was told, what was
    written to the conversation store, and which SIDs were dispatched at."""
    state: dict = {"notices": [], "posts": [], "ensured": [], "dispatch": []}
    monkeypatch.setattr(
        TL, "_notify",
        lambda cfg, chat_id, text, **k: state["notices"].append(text))
    monkeypatch.setattr(
        TL, "_post_conversation",
        lambda cfg, slug, gid, payload: (state["posts"].append((slug, payload)) or True))
    monkeypatch.setattr(
        TL, "_ensure_user_conversation",
        lambda cfg, slug, gid, ref, **k: (state["ensured"].append(slug) or
                                          {"ok": True, "spawned": False}))
    return state


@pytest.fixture()
def dispatch(monkeypatch, spy):
    """``actions.dispatch`` with per-SID outcomes.

    Records every call so an arm can assert what was NOT dispatched at — the
    duplication half of DoD 3 is invisible otherwise. SIDs in ``dead`` raise
    ``ActionError`` exactly as a reaped pane does; everything else succeeds.
    """
    dead: set[str] = set()

    def _fake(verb, params):
        state = (verb, params.get("sid"), params.get("text", ""))
        spy["dispatch"].append(state)
        if params.get("sid") in dead:
            raise A.ActionError(f"{verb}: no live pane for sid {params.get('sid')!r}")
        return {"ok": True, "pane_id": "%1"}

    monkeypatch.setattr(A, "dispatch", _fake)
    return dead


def _dispatched_sids(spy, verb: str | None = None) -> list[str]:
    return [sid for v, sid, _ in spy["dispatch"] if verb is None or v == verb]


# ---------------------------------------------------------------------------
# The PASS arm — required, or the refusal arms below are vacuous
# ---------------------------------------------------------------------------

def test_live_target_gets_it_and_nothing_is_copied_to_a_successor(tmp_path, spy, dispatch):
    """DoD 3: a reply to a LIVE session reaches that session and is not
    duplicated — not to the store, not onto the bus, not to the other live
    session sharing its window."""
    cfg = _cfg(tmp_path, {LIVE: "active", HEIR: "active"})

    out = TL._handle_reply(cfg, CHAT, LIVE, TEXT, gid=GID, block_text="конверт")

    assert out["action"] == "inject" and out["ok"] is True
    assert _dispatched_sids(spy) == [LIVE]
    assert spy["posts"] == [] and spy["notices"] == []
    chat_dir = Path(cfg.data_dir) / "watchrobot" / "_chat"
    assert not list(chat_dir.glob("inbox-*")) if chat_dir.exists() else True


# ---------------------------------------------------------------------------
# The REROUTE arms — one per inbound type measured on the old route
# ---------------------------------------------------------------------------

def _inbox(cfg, sid: str) -> str:
    p = Path(cfg.data_dir) / "watchrobot" / "_chat" / f"inbox-{sid}.log"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def test_reply_to_recycled_session_lands_in_the_successor_inbox(tmp_path, spy, dispatch):
    """The incident itself: the answer reaches the seat, not an attendant."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    dispatch.add(DEAD)

    out = TL._handle_reply(cfg, CHAT, DEAD, TEXT, gid=GID, block_text="конверт",
                           quote={"text": "Нужно ваше слово"})

    assert out["action"] == "inject_successor"
    assert out["successor"] == HEIR
    body = _inbox(cfg, HEIR)
    assert TEXT in body and DEAD in body
    # ...and the store fallback did NOT also fire: one delivery, not two.
    assert spy["posts"] == []
    assert any(HEIR in n for n in spy["notices"])


def test_successor_pane_is_nudged_because_the_bus_does_not_do_it(tmp_path, spy, dispatch):
    """Measured on the stand: ``peer_send`` writes the inbox and does NOT touch
    the pane — only the ``bsq`` CLI nudges. Without this step a sanction waits
    for the successor's next mail check."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    dispatch.add(DEAD)

    out = TL._handle_reply(cfg, CHAT, DEAD, TEXT, gid=GID, block_text="конверт")

    assert out["successor_nudged"] is True
    assert ("inject_input", HEIR, "check mail") in spy["dispatch"]


def test_nudge_failure_does_not_lose_the_message(tmp_path, spy, dispatch):
    """A successor whose pane died between the lookup and the nudge still has
    the inbox line waiting — which is why the bus write goes FIRST."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    dispatch.add(DEAD)
    dispatch.add(HEIR)

    out = TL._handle_reply(cfg, CHAT, DEAD, TEXT, gid=GID, block_text="конверт")

    assert out["action"] == "inject_successor" and out["successor_nudged"] is False
    assert TEXT in _inbox(cfg, HEIR)


def test_say_to_recycled_session_is_rerouted_too(tmp_path, spy, dispatch):
    """`/say` is the same shape — "a message addressed to a session" — and was
    measured losing one silently."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    dispatch.add(DEAD)

    out = TL._handle_slash(cfg, CHAT, "say", f"{DEAD} {TEXT}", gid=GID, sender="Alexey")

    assert out["action"] == "say_successor" and out["successor"] == HEIR
    assert TEXT in _inbox(cfg, HEIR)
    assert spy["posts"] == []


def test_answer_owed_debt_follows_the_message_to_the_successor(tmp_path, spy, dispatch,
                                                               monkeypatch):
    """A session-pinned topic stores a MORTAL sid, so its messages reroute —
    and the answer-owed ledger must then chase the session that got it. Filing
    the debt against the dead sid would nag a pane that no longer exists."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    dispatch.add(DEAD)
    owed: list[str] = []
    monkeypatch.setattr(TL.tg_direct_reply, "record_owed",
                        lambda cfg, **kw: owed.append(kw["sid"]))
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda *a, **k: True)
    binding = {"slug": "watchrobot", "session_id": DEAD, "ticket_id": "T-0896"}
    msg = {"message_id": 5, "message_thread_id": 77, "text": TEXT,
           "chat": {"id": int(CHAT), "type": "supergroup"},
           "from": {"id": 404580642, "first_name": "Alexey"}}

    out = TL._handle_topic_bound(cfg, CHAT, GID, binding, msg)

    assert out["action"] == "task_topic_inject_successor"
    assert owed == [HEIR], "the debt must be the successor's, not the dead sid's"


# ---------------------------------------------------------------------------
# The REFUSAL arms — DoD 4: undelivery stops being silence, and names the cause
# ---------------------------------------------------------------------------

def test_no_live_session_in_the_window_says_so(tmp_path, spy, dispatch):
    cfg = _cfg(tmp_path, {DEAD: "suspended"})
    dispatch.add(DEAD)

    out = TL._handle_reply(cfg, CHAT, DEAD, TEXT, gid=GID, block_text="конверт")

    assert out["successor_refusal"] == "none_live"
    assert any("окно сейчас никем не занято" in n for n in spy["notices"])


def test_two_live_sessions_in_one_window_says_it_refused_to_guess(tmp_path, spy, dispatch):
    """The resolver declining to guess is CORRECT behaviour — and silently it
    is worth no more than the loss, which is the whole ticket."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active", LIVE: "active"})
    dispatch.add(DEAD)

    out = TL._handle_reply(cfg, CHAT, DEAD, TEXT, gid=GID, block_text="конверт")

    assert out["successor_refusal"] == "ambiguous"
    assert any("угадывать между ними нельзя" in n for n in spy["notices"])


def test_deleted_session_card_says_so(tmp_path, spy, dispatch):
    """Session mds are reaped, so this is not a hypothetical. It surfaces as
    ``no_project`` rather than ``no_session_md`` because ``project_of_sid``
    reads the same card and fails first — asserted here so the mapping's own
    comment stays honest."""
    cfg = _cfg(tmp_path, {HEIR: "active"})  # DEAD has no md at all
    dispatch.add(DEAD)

    out = TL._handle_reply(cfg, CHAT, DEAD, TEXT, gid=GID, block_text="конверт")

    assert out["successor_refusal"] == "no_project"
    assert out["ok"] is False, "nothing was delivered anywhere — say so"
    assert any("карточка удалена" in n for n in spy["notices"])


def test_over_cap_message_keeps_the_store_fallback(tmp_path, spy, dispatch):
    """The bus refuses over 4000 chars rather than truncating. Falling through
    to the store (which has no such limit) loses nothing; letting the cap raise
    here would lose the message."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    dispatch.add(DEAD)

    out = TL._handle_reply(cfg, CHAT, DEAD, "я" * (IS._MAX_TEXT_LEN + 1),
                           gid=GID, block_text="конверт")

    assert out["successor_refusal"] == "over_bus_cap"
    assert out["action"] == "inject_fallback"
    assert _inbox(cfg, HEIR) == ""


def test_bus_cap_is_read_from_intersession_not_copied(tmp_path, spy, dispatch,
                                                      monkeypatch):
    """A local literal would be a second copy of a number only the bus gets to
    choose, and the copy is what goes stale.

    Pinned by MOVING the bus's cap and watching the router move with it — a
    message that fits under 4000 but not under the patched cap must fall back.
    The first version of this arm grepped the source for "4000" and went red on
    the number inside its own docstring: an extractor scoped wider than the
    thing it pins reports on text you did not mean to include.
    """
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    dispatch.add(DEAD)
    monkeypatch.setattr(IS, "_MAX_TEXT_LEN", 50)

    out = TL._handle_reply(cfg, CHAT, DEAD, TEXT, gid=GID, block_text="конверт")

    assert out["successor_refusal"] == "over_bus_cap", \
        "the router is using its own copy of the cap, not the bus's"
    assert _inbox(cfg, HEIR) == ""


# ---------------------------------------------------------------------------
# The resolver itself — one implementation, two views
# ---------------------------------------------------------------------------

# Spelled as literals, not as `IS.SUCCESSOR_*`, on purpose: a parametrize
# decorator is evaluated at IMPORT, so naming the new constants here would turn
# a control run against the UNFIXED sources into one collection error instead
# of a per-arm red. The constants' own values are pinned by
# `test_reason_constants_have_the_expected_values` below.
@pytest.mark.parametrize("sessions,target,reason,sid", [
    ({DEAD: "suspended", HEIR: "active"}, DEAD, "found", HEIR),
    ({DEAD: "suspended"}, DEAD, "none_live", None),
    ({DEAD: "suspended", HEIR: "active", LIVE: "active"}, DEAD, "ambiguous", None),
    ({DEAD: "active", HEIR: "active"}, DEAD, "target_live", None),
    ({HEIR: "active"}, DEAD, "no_session_md", None),
])
def test_successor_lookup_names_every_outcome(tmp_path, sessions, target, reason, sid):
    cfg = _cfg(tmp_path, sessions)
    got = IS.successor_lookup(cfg, "watchrobot", target)
    assert (got["reason"], got["sid"]) == (reason, sid)


def test_live_successor_sid_is_the_who_view_of_the_same_lookup(tmp_path):
    """The peer bus's contract is unchanged — same answer, no second copy."""
    cfg = _cfg(tmp_path, {DEAD: "suspended", HEIR: "active"})
    assert IS.live_successor_sid(cfg, "watchrobot", DEAD) == HEIR
    assert IS.live_successor_sid(cfg, "watchrobot", DEAD) == \
        IS.successor_lookup(cfg, "watchrobot", DEAD)["sid"]


def test_reason_constants_have_the_expected_values():
    """The literals the parametrize above uses ARE the module's constants."""
    assert (IS.SUCCESSOR_FOUND, IS.SUCCESSOR_NONE_LIVE, IS.SUCCESSOR_AMBIGUOUS,
            IS.SUCCESSOR_TARGET_LIVE, IS.SUCCESSOR_NO_SESSION_MD,
            IS.SUCCESSOR_UNPARSEABLE_SID) == (
        "found", "none_live", "ambiguous", "target_live", "no_session_md",
        "unparseable_sid")


def test_every_reason_renders_a_sentence_not_a_code():
    """A reason that reaches a human as a bare code is this ticket's own defect
    in miniature."""
    reasons = {v for k, v in vars(IS).items()
               if k.startswith("SUCCESSOR_") and k != "SUCCESSOR_FOUND"}
    # Without this the arm is VACUOUS: an empty set makes the loop below pass
    # while proving nothing — measured, it is what kept this one green on the
    # unfixed control while its 17 siblings went red.
    assert len(reasons) == 5, reasons
    for reason in reasons:
        assert reason in TL._SUCCESSOR_REASON_RU, reason
        assert "не распознана" not in TL._successor_refusal(reason)
