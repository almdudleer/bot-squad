"""T-0795: a new-message ping HIGHLIGHTS its chat id and topic id.

His ask, 2026-07-30T07:54Z: «на пинг что новое сообщение надо подсвечивать chat
id и topic id чтобы не потерялись». Said beside the incident where a directive of
his sat unread for seven hours — the ids that say WHERE a message came from were
in the ping and were missed.

What these tests are for, and it is not "the ids appear somewhere"
-----------------------------------------------------------------
Two of the assertions that already covered these surfaces read
``"thread_id 7" in text`` — true of a prose sentence with the id buried
mid-line, which is the state he is complaining about. So the pins here are on
the RENDERED STRING: the exact highlighted line, and the exact position it
occupies. A later reformat that folds the ids back into prose goes red naming
the surface.

FIVE surfaces ping about a new message, and they are pinned together on purpose
(the T-0770 family lesson — this repo fixes one branch and leaves the twin):

* ``tg_direct_reply.compose_envelope``       — the direct-mode topic ping
* ``tg_direct_reply.compose_light_envelope`` — the ``[<sid>]`` reply / ``/say`` ping
* ``tg_direct_reply.compose_reminder``       — the answer-owed re-drive
* ``actions`` attendant pane nudge           — a live attendant's new message
* ``actions._thread_scoped_read_write_block``— the same, at spawn time

The last assertion in this module is the one that keeps them together: all five
must carry the SAME rendering, byte for byte, so fixing four is not enough.

Controls that discriminate (T-0761 / T-0782, absent stays absent): a ping WITH a
topic renders both ids; a ping with NO topic renders cleanly and emits NO ``TOPIC
ID`` field — not an empty one and not a placeholder.
"""
from __future__ import annotations

import pytest

from bot_squad_worker import ping_ids
from bot_squad_worker import tg_direct_reply as D

CHAT = "-1002843452818"
TOPIC = 517
SID = "S-almdudleer-x-p70"

#: The rendering, written out by hand rather than built from ``ping_ids``
#: constants. A pin assembled from the code it pins cannot fail.
BOTH_IDS = "▶ CHAT ID: -1002843452818   ▶ TOPIC ID: 517"
CHAT_ONLY = "▶ CHAT ID: 123456789   (no topic id — not a forum topic)"
TOPIC_ONLY = "▶ TOPIC ID: 517"


# ---------------------------------------------------------------------------
# The helper — the four states its callers can be in
# ---------------------------------------------------------------------------

def test_render_both_ids_exact():
    assert ping_ids.render(chat_id=CHAT, thread_id=TOPIC) == BOTH_IDS


def test_render_topic_only_exact():
    """The attendant seam carries a topic and no chat — see ``render``'s
    docstring on why a chat field there could only be invented."""
    assert ping_ids.render(thread_id=TOPIC) == TOPIC_ONLY


def test_render_chat_without_topic_emits_no_topic_field():
    """CONTROL. A plain DM has no topic id. The absence is named as prose, and
    there is no ``TOPIC ID`` field of any kind — an empty or placeholder one
    reads identically to a real topic id that got dropped en route."""
    out = ping_ids.render(chat_id="123456789", thread_id=None)
    assert out == CHAT_ONLY
    assert "TOPIC ID" not in out


@pytest.mark.parametrize("kwargs", [
    {},
    {"chat_id": None, "thread_id": None},
    {"chat_id": "", "thread_id": ""},
    {"chat_id": "  ", "thread_id": "  "},
])
def test_render_nothing_known_is_empty(kwargs):
    """Neither id known -> no block at all, so the caller keeps its plain
    wording instead of printing a marker with nothing after it. Blank strings
    count as absent: these ids arrive from JSON params and ledger entries where
    ``""`` is the ordinary shape of a missing field."""
    assert ping_ids.render(**kwargs) == ""


def test_render_is_always_one_line():
    """The convention is single-line BECAUSE one consumer (the attendant nudge)
    is delivered by ``input_mux.deliver_direct``, one Enter per line — a block
    there arrives as N composer submissions. A newline creeping in here breaks
    that surface silently, so it is pinned at the source."""
    for kwargs in ({"chat_id": CHAT, "thread_id": TOPIC},
                   {"chat_id": CHAT}, {"thread_id": TOPIC}):
        assert "\n" not in ping_ids.render(**kwargs)


# ---------------------------------------------------------------------------
# Surface A — compose_envelope (the direct-mode ping)
# ---------------------------------------------------------------------------

def _envelope(**kw) -> str:
    params = dict(text="сообщение", chat_id=CHAT, thread_id=TOPIC, sid=SID,
                  sender="Alexey")
    params.update(kw)
    return D.compose_envelope(**params)


def test_envelope_ids_are_the_first_thing_after_the_headline():
    """Position is half of "highlighted": the ids lead the envelope instead of
    sitting in a ``Where`` row that looks like every other row."""
    lines = _envelope().splitlines()
    assert lines[1] == ""
    assert lines[2] == BOTH_IDS


def test_envelope_no_longer_buries_the_ids_in_a_where_row():
    """The pre-T-0795 shape, pinned as gone. ``Where   : chat X, forum topic Y``
    put both ids mid-line in a field indistinguishable from ``From``/``Session``
    — restoring it would satisfy "the ids are present" and lose the ask."""
    out = _envelope()
    assert "Where   :" not in out
    assert "forum topic 517" not in out


def test_envelope_tags_keep_their_own_labelled_row():
    """The slug/ticket tags used to ride the ``Where`` line. They must not be
    dropped with it, and must not dilute the id line."""
    out = _envelope(slug="bot-squad", ticket_id="T-0795")
    assert "Project : bot-squad · T-0795" in out
    assert BOTH_IDS in out


def test_envelope_without_tags_emits_no_project_row():
    """Absent stays absent on this row too."""
    assert "Project :" not in _envelope()


# ---------------------------------------------------------------------------
# Surface B — compose_light_envelope (the [<sid>] reply and /say paths)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("origin", ["reply", "say"])
def test_light_envelope_ids_lead_both_origins(origin):
    """T-0773 gave these two paths one composer. They get one id rendering too
    — the sibling-path twin is exactly what gets left unfixed here."""
    out = D.compose_light_envelope(text="да", chat_id=CHAT, thread_id=TOPIC,
                                   sid=SID, sender="Alexey", origin=origin)
    lines = out.splitlines()
    assert lines[1] == ""
    assert lines[2] == BOTH_IDS
    assert "Where   :" not in out


@pytest.mark.parametrize("origin", ["reply", "say"])
def test_light_envelope_dm_emits_no_topic_field(origin):
    """CONTROL. This is the ONE surface reached with no topic in normal
    operation (a DM reply, ``/say`` in a DM). It must read cleanly and must not
    grow an empty topic field."""
    out = D.compose_light_envelope(text="да", chat_id="123456789",
                                   thread_id=None, sid=SID, sender="Alexey",
                                   origin=origin)
    lines = out.splitlines()
    assert lines[2] == CHAT_ONLY
    assert "TOPIC ID" not in out
    # The reply affordance still has to be the DM one, not a --topic command
    # that dies on arrival (``answer_route``'s contract, unchanged here).
    assert "--topic None" not in out


# ---------------------------------------------------------------------------
# Surface C — compose_reminder (the answer-owed re-drive)
# ---------------------------------------------------------------------------

def test_reminder_ids_on_their_own_line():
    out = D.compose_reminder(
        {"chat_id": CHAT, "thread_id": TOPIC, "sid": SID, "text": "сообщение"},
        attempt=1, attempts_left=1)
    assert BOTH_IDS in out.splitlines()
    # The prose that used to carry them mid-sentence is gone.
    assert "chat -1002843452818, topic 517" not in out


# ---------------------------------------------------------------------------
# Surfaces D + E — the attendant's new-message notification (actions.py)
# ---------------------------------------------------------------------------

_ROUTE = "/api/m/worker/conversations/test-project/gu_a1b2c3/messages"

#: The nudge, in full. Pinned as ONE string because it is delivered by
#: ``inject_input`` (one Enter per line) — the whole point is that it stays a
#: single line, so a diff that wraps it is a delivery regression, not a
#: reformat.
EXPECTED_NUDGE = (
    "▶ TOPIC ID: 517 — a new message arrived in that topic of your "
    "user-conversation. Read that topic's isolated thread "
    f"(GET {_ROUTE}?thread_id=517) and reply into it "
    "(append with thread_id=517)."
)


def _nudge_text(monkeypatch, tmp_path, **params) -> str:
    """Drive the real action with a live attendant and capture what it injects."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    from tests.test_actions import _make_sessions_cfg

    _make_sessions_cfg(tmp_path, monkeypatch)
    existing = "S-u-gu_a1b2c3-user-conversation-p9"
    nudged: dict = {}
    monkeypatch.setattr(S, "live_user_conversation_sid",
                        lambda cfg, slug, gid: existing)
    monkeypatch.setattr(A, "_action_inject_input",
                        lambda p: nudged.update(p) or {"ok": True})
    base = {"slug": "test-project", "global_user_id": "gu_a1b2c3",
            "message_ref": "another message"}
    base.update(params)
    A.dispatch("ensure_user_conversation", base)
    return nudged["text"]


def test_attendant_nudge_leads_with_the_topic_id_exactly(tmp_path, monkeypatch):
    assert _nudge_text(monkeypatch, tmp_path, thread_id=TOPIC) == EXPECTED_NUDGE


def test_attendant_nudge_stays_one_line(tmp_path, monkeypatch):
    """``inject_input`` sends one Enter per line. A multi-line nudge would
    arrive as several composer submissions — the T-0773 defect, re-introduced
    in the ping about the message."""
    text = _nudge_text(monkeypatch, tmp_path, thread_id=TOPIC)
    assert "\n" not in text
    assert len(text.splitlines()) == 1


def test_attendant_nudge_without_topic_names_no_ids(tmp_path, monkeypatch):
    """CONTROL. No topic (a DM to the attendant) -> the plain wording, with no
    marker, no empty field, and no chat id invented to fill the gap."""
    text = _nudge_text(monkeypatch, tmp_path)
    assert text == ("A new message arrived in your user-conversation thread "
                    "— read it and respond.")
    assert ping_ids.MARK not in text
    assert "TOPIC ID" not in text


def test_boot_block_topic_id_on_its_own_line():
    """Surface E: the spawn-time equivalent of the nudge. Same rendering; this
    one may be multi-line (a boot prompt is pasted, not typed line by line), so
    the id gets a line to itself."""
    from bot_squad_worker.actions import _thread_scoped_read_write_block

    block = _thread_scoped_read_write_block("test-project", "gu_a1b2c3", TOPIC)
    assert TOPIC_ONLY in block.splitlines()
    # The route the attendant is handed is unchanged (T-0775).
    assert f"GET {_ROUTE}?thread_id=517" in block
    # The prose that used to carry the id mid-sentence is gone.
    assert "(thread_id 517)" not in block


def test_boot_block_without_topic_is_empty():
    """CONTROL, and the pre-existing contract: absent thread_id -> no block at
    all, so a DM attendant's boot prompt is untouched by this ticket."""
    from bot_squad_worker.actions import _thread_scoped_read_write_block

    for absent in (None, "", "   "):
        assert _thread_scoped_read_write_block("s", "g", absent) == ""


# ---------------------------------------------------------------------------
# The anti-twin pin — one convention, every surface
# ---------------------------------------------------------------------------

def test_every_surface_uses_the_identical_rendering(tmp_path, monkeypatch):
    """The assertion this module exists for. Five surfaces ping about a new
    message; fixing four of them is this repo's top bug class. Each must carry
    the SAME rendering for the ids it holds — so a convention change has to be
    made in ``ping_ids`` (one place) or this goes red.
    """
    both = [
        _envelope(),
        D.compose_light_envelope(text="да", chat_id=CHAT, thread_id=TOPIC,
                                 sid=SID, origin="reply"),
        D.compose_reminder({"chat_id": CHAT, "thread_id": TOPIC, "sid": SID,
                            "text": "x"}, attempt=1, attempts_left=1),
    ]
    for rendered in both:
        assert BOTH_IDS in rendered

    from bot_squad_worker.actions import _thread_scoped_read_write_block
    topic_only = [
        _nudge_text(monkeypatch, tmp_path, thread_id=TOPIC),
        _thread_scoped_read_write_block("test-project", "gu_a1b2c3", TOPIC),
    ]
    for rendered in topic_only:
        assert TOPIC_ONLY in rendered
