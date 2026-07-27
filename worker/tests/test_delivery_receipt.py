"""T-0761: where Telegram says a message LANDED, not where we asked it to go.

Every delivery artefact this system kept recorded INTENT. For an attendant
writeback there was no on-disk witness of the destination at all — the store
record keeps ``delivered`` falsy, the spool gets nothing (``record_outbound:
False``), and the HTTP response carried only ``relayed: true``, which is TRUE IN
BOTH OUTCOMES. A reply that fell back to the private DM instead of the resolved
topic reported success exactly like one that landed correctly, so during the
whole lifetime of T-0740 every misrouted message confirmed itself. A bool that
cannot be false is not a confirmation.

The tests below are organised around the two instrument rules this repo learned
the hard way today: **assert the field is present** (an absent value must
surface as an explicit UNKNOWN, never a silent ``None`` that reads identically
to "was not in a topic"), and **the property that justifies a thing existing
must be enforced, not intended**.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import outbound_log as OB
from bot_squad_worker import tg as TG


CHAT = "-1003761939853"
TOKEN = "1234567890:AAaaBBbbCCccDDddEEeeFFffGGggHHhhIIj"


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        tg_bot_token=TOKEN, max_bot_token="", data_dir=tmp_path,
        tg_quiet_hours_start_utc=0, tg_quiet_hours_end_utc=0,
        tg_proxy_url="", max_proxy_url="", max_recipient_kind="chat_id",
    )


def _msg(**over) -> dict:
    """A sendMessage response, in Telegram's real shape."""
    result = {"message_id": 777, "text": "готово"}
    result.update(over)
    return {"ok": True, "result": result}


def _spool(tmp_path: Path) -> list[dict]:
    return [json.loads(line)
            for p in sorted(OB.spool_dir(tmp_path).glob("*.jsonl"))
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The capture — Telegram's own statement of destination
# ---------------------------------------------------------------------------

def test_the_thread_telegram_reports_is_captured() -> None:
    r = TG.delivery_receipt(_msg(message_thread_id=11), chat_id=CHAT,
                            requested_thread_id=11)

    assert r["thread_id"] == 11
    assert r["thread_known"] is True
    assert r["mismatch"] is False
    assert r["message_id"] == 777


def test_a_different_thread_is_a_mismatch() -> None:
    """THE T-0740 shape, observable after the fact for the first time: we asked
    for topic 11 and Telegram says it went to 3."""
    r = TG.delivery_receipt(_msg(message_thread_id=3), chat_id=CHAT,
                            requested_thread_id=11)

    assert r["thread_id"] == 3
    assert r["thread_known"] is True
    assert r["mismatch"] is True


# ---------------------------------------------------------------------------
# EXPLICIT UNKNOWN — the non-negotiable instrument rule
# ---------------------------------------------------------------------------

def test_an_absent_thread_is_unknown_and_never_reads_as_no_topic() -> None:
    """A silent ``None`` reads identically to "was not in a topic", which would
    make this witness lie in precisely the case it exists for. Telegram never
    states a negative — a DM, a General-topic message and a non-forum group all
    simply omit the field — so "in no topic" is a conclusion we are not
    entitled to draw."""
    r = TG.delivery_receipt(_msg(), chat_id=CHAT, requested_thread_id=11)

    assert r["thread_id"] is None
    assert r["thread_known"] is False
    assert r["thread_unknown_reason"] == TG.UNKNOWN_ABSENT
    # We asked for a topic and TG did not confirm it: unconfirmed is not
    # confirmed, so this counts as a mismatch — told apart from a definite
    # misroute by thread_known.
    assert r["mismatch"] is True


def test_unknown_flavours_are_distinguished() -> None:
    """"TG did not say" has genuinely different causes and a reader debugging a
    misroute needs to know which one they are looking at."""
    assert TG.delivery_receipt(None)["thread_unknown_reason"] == TG.UNKNOWN_NO_RESPONSE
    assert TG.delivery_receipt({"ok": True})["thread_unknown_reason"] == TG.UNKNOWN_NO_RESULT
    assert TG.delivery_receipt(_msg())["thread_unknown_reason"] == TG.UNKNOWN_ABSENT


def test_a_present_but_unparseable_thread_is_unknown_not_absent() -> None:
    """A shape change must not quietly become "no topic"."""
    r = TG.delivery_receipt(_msg(message_thread_id="not-an-int"),
                            requested_thread_id=11)

    assert r["thread_known"] is False
    assert r["mismatch"] is True


def test_no_requested_topic_and_no_reported_one_is_not_a_mismatch() -> None:
    """The negative guard. A plain DM send asks for no topic and gets none —
    labelling that a mismatch would fire on the majority of sends and get the
    signal muted, which is how a real alarm dies."""
    r = TG.delivery_receipt(_msg(), chat_id="404580642")

    assert r["mismatch"] is False
    assert r["thread_known"] is False


def test_the_echoed_text_is_asserted_too() -> None:
    """Same rule, second field: a silently missing echo reads identically to
    "nothing was sent"."""
    known = TG.delivery_receipt(_msg(text="[bot-squad] 📋 T-0320 → totest"))
    assert known["text_known"] is True
    assert known["text"] == "[bot-squad] 📋 T-0320 → totest"

    absent = TG.delivery_receipt({"ok": True, "result": {"message_id": 1}})
    assert absent["text_known"] is False
    assert absent["text"] == ""
    assert absent["text_unknown_reason"] == TG.UNKNOWN_ABSENT


def test_the_receipt_never_raises_on_a_hostile_response() -> None:
    """It runs on the send path, where an observability failure must never turn
    a delivered message into a failed one."""
    for bad in (None, "", [], {"result": "nope"}, {"result": {"message_id": "x"}}):
        assert TG.delivery_receipt(bad)["thread_known"] is False


# ---------------------------------------------------------------------------
# Through the transport
# ---------------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    """Capture outbound HTTP and let each test choose the response shape."""
    state: dict = {"result": {"message_id": 777, "text": "", "message_thread_id": None}}
    calls: list[dict] = []
    import httpx

    class _Resp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    def _post(url, json=None, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "json": json})
        result = dict(state["result"])
        if result.get("text") == "":
            # Telegram echoes what we sent; default to exactly that.
            result["text"] = (json or {}).get("text", "")
        if result.get("message_thread_id") is None:
            result.pop("message_thread_id", None)
        return _Resp({"ok": True, "result": result})

    monkeypatch.setattr(httpx, "post", _post)
    return SimpleNamespace(calls=calls, state=state)


def test_a_spooled_send_records_where_it_landed(tmp_path: Path, sent) -> None:
    sent.state["result"]["message_thread_id"] = 11
    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="готово", sid="x", urgent=True, topic_id=11)

    (rec,) = _spool(tmp_path)
    assert rec["thread_id"] == 11               # INTENT — where we addressed it
    assert rec["delivered_to"]["thread_id"] == 11   # OUTCOME — where TG says it went
    assert rec["delivered_to"]["thread_known"] is True
    assert rec["delivered_to"]["mismatch"] is False


def test_intent_and_outcome_are_stored_as_two_fields(tmp_path: Path, sent) -> None:
    """Reading one as the other IS the defect, so they may never be merged into
    a single "destination"."""
    sent.state["result"]["message_thread_id"] = 3
    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="готово", sid="x", urgent=True, topic_id=11)

    (rec,) = _spool(tmp_path)
    assert rec["thread_id"] == 11
    assert rec["delivered_to"]["thread_id"] == 3
    assert rec["delivered_to"]["mismatch"] is True


def test_an_unspooled_send_gets_a_receipt_record(tmp_path: Path, sent) -> None:
    """The 📋 lifecycle notice: the sender tag is applied at the TRANSPORT, the
    store keeps the raw UNTAGGED text, and record_outbound=False means the spool
    gets nothing — so Telegram's echo is the only witness of the tagged bytes
    that reached the wire, and T-0758's 📋 half is otherwise unverifiable."""
    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="📋 T-0320 → totest", sid="bot-squad", urgent=True,
        topic_id=11, record_outbound=False)

    (rec,) = _spool(tmp_path)
    assert rec["kind"] == OB.RECEIPT_KIND
    assert rec["text"] == "[bot-squad] 📋 T-0320 → totest"   # the WIRE bytes
    assert rec["delivered_to"]["thread_known"] is False       # TG said nothing
    assert rec["delivered_to"]["requested_thread_id"] == 11


def test_a_receipt_is_never_mirrored_into_the_transcript(
        tmp_path: Path, sent, monkeypatch) -> None:
    """It is an audit of a delivery, not a message for a reader. The send was
    unspooled BECAUSE its caller already put that line in this very thread, so
    mirroring the receipt would restore the duplicate the opt-out prevents.

    WITH A POSITIVE CONTROL, because "nothing was mirrored" is exactly what a
    broken drain also reports: an ordinary record is written into the SAME
    spool in the same test, and the assertion is that the drain reaches that one
    and only that one. Without it this test would pass just as happily if the
    drain never ran at all.
    """
    seen: list[dict] = []
    monkeypatch.setattr(OB, "_mirror", lambda cfg, rec: seen.append(rec) or True)

    client = TG.TgClient(_cfg(tmp_path))
    client.send(chat_id=CHAT, text="📋 T-0320 → totest", sid="bot-squad",
                urgent=True, record_outbound=False)
    client.send(chat_id=CHAT, text="обычное сообщение", sid="bot-squad",
                urgent=True)

    out = OB.drain(_cfg(tmp_path), limit=10)

    assert [r.get("kind") for r in _spool(tmp_path)] == [OB.RECEIPT_KIND, None]
    assert [r["text"] for r in seen] == ["[bot-squad] обычное сообщение"]
    assert out["mirrored"] == 1


def test_a_send_that_never_left_reports_no_destination(tmp_path: Path, sent) -> None:
    """The negative guard: a suppressed send has no destination, and writing an
    empty receipt for it would invent one."""
    client = TG.TgClient(_cfg(tmp_path), cooldown_sec=600)
    receipt: dict = {}
    assert client.send(chat_id=CHAT, text="дубль", sid="x", urgent=True) is True
    assert client.send(chat_id=CHAT, text="дубль", sid="x", urgent=True,
                       delivery=receipt) is False

    assert receipt == {}


def test_the_out_param_gives_the_caller_the_destination(tmp_path: Path, sent) -> None:
    """What makes `relayed: true` falsifiable at the API: the relay path opts
    out of the spool, so the response is the only way the destination reaches
    the caller."""
    sent.state["result"]["message_thread_id"] = 11
    receipt: dict = {}

    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="ответ", sid="x", urgent=True, topic_id=11,
        record_outbound=False, delivery=receipt)

    assert receipt["thread_id"] == 11
    assert receipt["thread_known"] is True


# ---------------------------------------------------------------------------
# Redaction — the property that makes storing responses safe
# ---------------------------------------------------------------------------

def test_a_secret_in_the_echoed_text_comes_back_redacted(tmp_path: Path, sent) -> None:
    """A Telegram response echoes OUR OWN sent text, so it carries anything we
    redacted on the way out — unredacted. A response log that did not route
    through ``redact`` would write, in cleartext, precisely the secret the spool
    was careful to strip, and it would do so on the ONE path (the unspooled 📋
    notice) that has no redacted sibling to compare against.

    This is the enforcement of the property that justifies storing responses at
    all — the same rule as the ``.unspooled`` marker's zero bytes.
    """
    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text=f"deploy failed, token = {TOKEN}", sid="x",
        urgent=True, record_outbound=False)

    (rec,) = _spool(tmp_path)
    assert rec["kind"] == OB.RECEIPT_KIND
    assert TOKEN not in rec["text"]
    assert "‹redacted:" in rec["text"]
    assert rec["redacted"]              # kinds recorded — a silent redaction is
    assert "token" in rec["text"]       # impossible, and the record stays diagnosable


def test_ordinary_prose_survives_redaction_byte_identical(tmp_path: Path, sent) -> None:
    """The free witness has to actually witness. If redaction touched ordinary
    text, the stored copy could not testify about the sender tag — which is the
    whole reason this record is worth writing for the 📋 class."""
    body, kinds = OB.redact("[bot-squad] 📋 T-0320 → totest", cfg=_cfg(tmp_path))

    assert body == "[bot-squad] 📋 T-0320 → totest"
    assert kinds == []
