"""T-0762 — a delivery receipt names the session that composed it.

THE MEASURED DEFECT, from two live receipts three minutes apart:

* 23:34:20Z, the attendant writeback (a SESSION composed it) — ``author:
  "system:unattributed"``, while the SAME record's ``text`` read
  ``[bot-squad user-conversation] …``. One field named the sender and the other
  said nobody knew.
* 23:37:03Z, the ``📋`` lifecycle notice (no session composed it) — ``author:
  "system:bot-squad"``, correct, because ``task_chat`` declares its slug.

So the send with a real named author was the one recorded as belonging to
nobody, while the one composed by no session was attributed properly. The cause
is two sources for one question inside one function: ``tg.send`` tagged the TEXT
from ``sender_sid`` and derived the AUTHOR from ``sid``/``route_sid``, which the
API's session-writeback relay deliberately does not pass — T-0758 established
that ``sid`` also enters ``tg_notify``'s destination ladder and pins
``tg_reply_map``, so passing it to fix an author field would silently re-route
the stakeholder's next quoted reply.

THE NEGATIVE GUARDS MATTER MORE THAN THE POSITIVE ONE HERE, because the risk of
this fix is OVER-attributing: a system sender that starts claiming a session
authorship, or an unstated one that acquires an author out of nowhere, would be
a worse record than the vague one being replaced.

Demoed against unfixed sources (HEAD with only this file dropped in): 9 RED, 4
green. The 4 that pass on BOTH are exactly the over-attribution guards — the
``📋`` notice's ``system:bot-squad``, a class sender on each transport, and a
send that names nobody staying ``unattributed``. They are the tests this change
is measured against.

Two more READ like negative guards and are not, said plainly rather than
claimed: ``test_a_non_routing_sender_sid_…`` and
``test_the_ladder_below_sender_sid_is_unchanged`` pin behaviour that is OLD
(the shape gate, and ``route_sid`` outranking ``sender_label``), but they go RED
at HEAD because ``author_for_send`` there does not accept the kwarg they pass
and has no defaults. The property is old; the test is new.

Scope, stated so a reader does not go looking for more: this is observability
only. ``echo_guard`` rung 2 matches on TEXT and CHAT, never on the author, and
the destination evidence (``delivered_to`` / ``mismatch``) is untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_squad_worker import outbound_log as OB
from bot_squad_worker import tg_reply_map
from bot_squad_worker.max import MaxClient
from bot_squad_worker.tg import TgClient

# The live bot-squad attendant — the session whose receipt was recorded as
# belonging to nobody. Its project is knowable ONLY from its on-disk session md
# (both attendants share a window name and a chat), which is why the fixture
# writes one.
ATTENDANT = "S-almdudleer-gu_dc8262b6cea9098d98e04d7e-user-conversation-p5"
CHAT = "404580642"


class _Cfg:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.tg_bot_token = "8000000000:TESTTOKEN"
        self.max_bot_token = "8000000000:TESTTOKEN"
        self.tg_proxy_url = ""
        self.max_proxy_url = ""
        self.max_recipient_kind = "chat_id"
        self.projects = {"bot-squad": object(), "watchrobot": object()}


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Cfg:
    sess = tmp_path / "bot-squad" / "sessions"
    sess.mkdir(parents=True)
    (sess / f"{ATTENDANT}.md").write_text(f"---\nsid: {ATTENDANT}\n---\n")
    monkeypatch.setenv("BOT_SQUAD_DISABLE_QUIET_HOURS", "1")
    return _Cfg(tmp_path)


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Both transports' ``_post``, with Telegram echoing the text as it does."""
    sent: list[dict] = []

    def _tg_post(self, **kw):
        sent.append(dict(kw, channel="tg"))
        return {"ok": True, "result": {"message_id": 7, "text": kw.get("text", "")}}

    def _max_post(self, **kw):
        sent.append(dict(kw, channel="max"))

    monkeypatch.setattr(TgClient, "_post", _tg_post)
    monkeypatch.setattr(MaxClient, "_post", _max_post)
    return sent


def _spool(data_dir: Path) -> list[dict]:
    return [json.loads(line)
            for p in sorted(OB.spool_dir(data_dir).glob("*.jsonl"))
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The defect — a session-composed send, recorded on the relay's exact shape
# ---------------------------------------------------------------------------

def test_a_session_composed_receipt_names_the_session(cfg, wire) -> None:
    """THE REPORTED RECORD, reproduced field for field.

    The relay's shape: an explicit ``chat_id``, a ``sender_sid``, and NEITHER
    ``sid`` NOR ``route_sid`` — the two that were previously the only inputs to
    the author. ``record_outbound=False`` because the caller already wrote this
    line into the thread, which is what routes it to the receipt path.
    """
    TgClient(cfg).send(
        chat_id=CHAT, text="This topic is here for T-0740", sender_sid=ATTENDANT,
        record_outbound=False, debounce=False,
    )

    (rec,) = _spool(cfg.data_dir)
    assert rec["kind"] == OB.RECEIPT_KIND
    assert rec["author"] == f"session:{ATTENDANT}"


def test_the_receipts_two_fields_stop_contradicting_each_other(cfg, wire) -> None:
    """The whole ticket in one assertion: the text names the sender, and the
    author field now names the SAME sender rather than nobody. Both are derived
    from ``sender_sid``, which is what makes them incapable of disagreeing."""
    TgClient(cfg).send(
        chat_id=CHAT, text="Понял, делаю.", sender_sid=ATTENDANT,
        record_outbound=False, debounce=False,
    )

    (rec,) = _spool(cfg.data_dir)
    assert rec["text"] == "[💬 bot-squad user-conversation] Понял, делаю."
    assert rec["author"] == f"session:{ATTENDANT}"


def test_the_asymmetry_the_ticket_names_is_gone(cfg, wire) -> None:
    """The two LIVE receipts, side by side in one spool.

    They stay in DIFFERENT vocabularies on purpose. A session send is
    ``session:<sid>``; a send no session composed keeps naming its project.
    Collapsing the second into a session form would trade one wrong author for
    another, so the pair — not either half — is the assertion.
    """
    client = TgClient(cfg)
    client.send(chat_id=CHAT, text="Понял, делаю.", sender_sid=ATTENDANT,
                record_outbound=False, debounce=False)
    client.send(chat_id=CHAT, text="📋 T-0740 → closed", sid="bot-squad",
                record_outbound=False, debounce=False)

    assert [r["author"] for r in _spool(cfg.data_dir)] == [
        f"session:{ATTENDANT}", "system:bot-squad",
    ]


def test_attribution_adds_no_routing_surface(cfg, wire) -> None:
    """CONSTRAINT (1), pinned rather than trusted to review.

    The forbidden fix was to pass ``sid`` on the relay path, which would also
    pin ``tg_reply_map`` and send the stakeholder's next quoted reply down the
    direct-to-session route instead of the attendant's. ``sender_sid`` is
    label-and-author only, so a fully attributed send must still leave the reply
    map empty — checked by ABSENCE OF THE FILE and by an empty load, because a
    map that was never written and one written empty are different bugs.
    """
    TgClient(cfg).send(
        chat_id=CHAT, text="Понял, делаю.", sender_sid=ATTENDANT,
        record_outbound=False, debounce=False,
    )

    (rec,) = _spool(cfg.data_dir)
    assert rec["author"] == f"session:{ATTENDANT}"          # attributed
    assert not tg_reply_map.map_path(cfg.data_dir).exists()  # and unrouted
    assert tg_reply_map.load(cfg.data_dir) == {}


def test_a_spooled_session_send_is_attributed_too(cfg, wire) -> None:
    """The twin inside ``tg.py`` itself, closed before it bites.

    No caller pairs ``sender_sid`` with ``record_outbound=True`` today, so this
    exercises a shape only a future send path will use. Wiring the receipt half
    alone would leave the same defect sitting in the ordinary record for
    whoever writes that path — which is exactly how this repo's duplicate-
    divergence bugs happen.
    """
    TgClient(cfg).send(
        chat_id=CHAT, text="готово", sender_sid=ATTENDANT, debounce=False,
    )

    (rec,) = _spool(cfg.data_dir)
    assert rec.get("kind") is None                 # an ordinary record
    assert rec["author"] == f"session:{ATTENDANT}"


# ---------------------------------------------------------------------------
# The MAX twin — same shape, on the ordinary record (MAX has no receipt path)
# ---------------------------------------------------------------------------

def test_max_records_the_composing_session_too(cfg, wire) -> None:
    """T-0758 and T-0759 both had to fix both transports, and so does this.

    ``max.send`` already TOOK ``sender_sid`` and already used it for the tag,
    while ``_record_outbound`` derived the author from ``sid`` alone — bit for
    bit the TG defect, one file over. MAX is the RESERVE channel, so it carries
    precisely the traffic TG could not deliver: the moment an audit most needs
    an author and is least likely to be watched.
    """
    MaxClient(cfg).send(chat_id=CHAT, text="Понял.", sender_sid=ATTENDANT)

    (rec,) = _spool(cfg.data_dir)
    assert rec["channel"] == "max"
    assert rec["text"] == "[💬 bot-squad user-conversation] Понял."
    assert rec["author"] == f"session:{ATTENDANT}"


# ---------------------------------------------------------------------------
# NEGATIVE GUARDS — the sends that must NOT change (green against HEAD too)
# ---------------------------------------------------------------------------

def test_a_slug_declaring_system_send_keeps_naming_its_project(cfg, wire) -> None:
    """The 📋 lifecycle notice. ``task_chat`` declares a slug and no session
    composed the line, so ``system:bot-squad`` was ALREADY right and must
    survive untouched — the regression this fix could most easily cause."""
    TgClient(cfg).send(
        chat_id=CHAT, text="📋 T-0740 → closed", sid="bot-squad",
        record_outbound=False, debounce=False,
    )

    (rec,) = _spool(cfg.data_dir)
    assert rec["author"] == "system:bot-squad"


def test_a_class_sender_keeps_naming_its_class(cfg, wire) -> None:
    """``deploy_monitor`` / ``voice_intake`` are the system speaking. Forcing
    them into a session form would name a session that does not exist."""
    TgClient(cfg).send(
        chat_id=CHAT, text="deploy paused", sid="deploy_monitor", debounce=False,
    )

    (rec,) = _spool(cfg.data_dir)
    assert rec["author"] == "system:deploy-monitor"


def test_a_max_send_that_names_no_session_keeps_its_class(cfg, wire) -> None:
    """The MAX half of the same guard — the reserve channel must not start
    inventing session authors either."""
    MaxClient(cfg).send(chat_id=CHAT, text="deploy paused", sid="deploy_monitor")

    (rec,) = _spool(cfg.data_dir)
    assert rec["author"] == "system:deploy-monitor"


def test_a_send_that_names_nobody_stays_unattributed(cfg, wire) -> None:
    """``unattributed`` is a TRUE statement when nothing named a sender, and it
    must keep being made. A fix for a missing author that starts inventing one
    where none was stated has replaced a vague record with a false one."""
    TgClient(cfg).send(chat_id=CHAT, text="hello", debounce=False)

    (rec,) = _spool(cfg.data_dir)
    assert rec["author"] == "system:unattributed"


def test_a_non_routing_sender_sid_does_not_displace_a_declared_class(cfg) -> None:
    """The gate is the SHAPE of a routing SID. Anything else falls straight
    through to the existing ladder, so a caller that puts a class name in
    ``sender_sid`` gets ``system:<class>`` rather than a forged
    ``session:deploy_monitor`` — the author vocabulary's classes are what the
    conversation store validates against, and a forged one would be accepted."""
    assert OB.author_for_send(
        sender_sid="deploy_monitor", sender_label="bot-squad",
    ) == "system:bot-squad"


def test_the_ladder_below_sender_sid_is_unchanged(cfg) -> None:
    """``route_sid`` still beats ``sender_label``, deliberately NOT copying the
    rest of ``sender_tag.resolve_label``'s order: the tag prefers the display
    label because that is what the reader sees, while an author field prefers
    the raw SID because it is the joinable key. Demoting it under a display name
    would turn every routed send into ``system:<label>``."""
    assert OB.author_for_send(
        route_sid=ATTENDANT, sender_label="bot-squad operator",
    ) == f"session:{ATTENDANT}"
    assert OB.author_for_send(sender_label="voice_intake") == "system:voice-intake"
    assert OB.author_for_send() == "system:unattributed"


def test_the_stored_author_stays_within_the_records_vocabulary(cfg, wire) -> None:
    """Whatever this derives must remain a record the store will accept — the
    author field is validated on the way into the conversation store, and a
    receipt the mirror cannot read is not an improvement on a vague one."""
    TgClient(cfg).send(
        chat_id=CHAT, text="Понял, делаю.", sender_sid=ATTENDANT,
        record_outbound=False, debounce=False,
    )

    (rec,) = _spool(cfg.data_dir)
    assert OB.is_valid_author(rec["author"])
    assert OB.author_class(rec["author"]) == "session"
