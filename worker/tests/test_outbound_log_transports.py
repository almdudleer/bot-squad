"""T-0755: every transport records what it delivered.

Hooking the TRANSPORT rather than adding a caller-side append is the whole
design: the one caller-side path that DID record (the API's session writeback)
silently fell out of use after 2026-07-18 while five other send paths never had
one. These tests pin the chokepoints so a sixth cannot appear unaudited.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import max as MX
from bot_squad_worker import outbound_log as OB
from bot_squad_worker import tg as TG


SID = "S-almdudleer-operator-p298"
CHAT = "-1003761939853"


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        tg_bot_token="1234567890:AAaaBBbbCCccDDddEEeeFFffGGggHHhhIIj",
        max_bot_token="MAXTOKEN-abcdefghijklmnop",
        data_dir=tmp_path,
        tg_quiet_hours_start_utc=0, tg_quiet_hours_end_utc=0,
        tg_proxy_url="", max_proxy_url="", max_recipient_kind="chat_id",
    )


class _Resp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound HTTP without touching the network."""
    calls: list[dict] = []
    import httpx

    def _post(url, json=None, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "json": json})
        return _Resp({"ok": True, "result": {"message_id": 777}})

    monkeypatch.setattr(httpx, "post", _post)
    return calls


def _spool(tmp_path: Path) -> list[dict]:
    return [json.loads(line)
            for p in sorted(OB.spool_dir(tmp_path).glob("*.jsonl"))
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def test_tg_send_records_what_was_delivered(tmp_path: Path, sent) -> None:
    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="ответ", sid="bot-squad operator", route_sid=SID,
        topic_id=11, reply_to_message_id=344, urgent=True, debounce=False)
    (rec,) = _spool(tmp_path)
    assert rec["author"] == f"session:{SID}"
    assert rec["chat_id"] == CHAT
    assert rec["thread_id"] == 11
    assert rec["message_id"] == 777
    assert rec["reply_to_message_id"] == 344
    assert rec["channel"] == "tg"


def test_tg_records_the_text_as_DELIVERED_including_the_prefix(tmp_path: Path, sent) -> None:
    """This is an audit of what reached the stakeholder's screen, not of what
    the caller composed — the `[<label>]` prefix is part of what he saw."""
    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="ответ", sid="bot-squad operator", urgent=True)
    assert _spool(tmp_path)[0]["text"] == "[bot-squad operator] ответ"
    assert _spool(tmp_path)[0]["text"] == sent[0]["json"]["text"]


def test_tg_records_a_non_session_sender_as_system(tmp_path: Path, sent) -> None:
    """A deploy notification is as unauditable as a session reply."""
    TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="deploy ok", sid="deploy_monitor",
        route_sid="deploy_monitor", urgent=True)
    assert _spool(tmp_path)[0]["author"] == "system:deploy-monitor"


def test_tg_suppressed_send_records_nothing(tmp_path: Path, sent) -> None:
    """Debounce/quiet-hours drops never reached the user, so recording them
    would put words in the transcript that he never saw."""
    cfg = _cfg(tmp_path)
    client = TG.TgClient(cfg)
    assert client.send(chat_id=CHAT, text="dup", sid="x", urgent=True) is True
    assert client.send(chat_id=CHAT, text="dup", sid="x", urgent=True) is False
    assert len(_spool(tmp_path)) == 1


def test_tg_send_still_succeeds_when_logging_blows_up(tmp_path: Path, sent, monkeypatch) -> None:
    """A logging failure must NEVER break a SEND — the send is the product."""
    monkeypatch.setattr(OB, "record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="важное", sid="x", urgent=True) is True
    assert sent[0]["json"]["text"] == "[x] важное"


def test_tg_record_outbound_false_suppresses_the_record_not_the_send(
        tmp_path: Path, sent) -> None:
    """For the two callers that already wrote this same text into this same
    thread (the session-writeback relay, task_chat's lifecycle notice).

    UPDATED BY T-0761, and the distinction is the point rather than a
    weakening. This used to assert the spool was EMPTY. It is no longer: an
    unspooled send now leaves a `delivery-receipt`, because for the 📋
    lifecycle notice — tag applied at the transport, untagged copy in the
    store, nothing in the spool — Telegram's response echo is the only witness
    of the bytes that reached the wire. What T-0755 actually protects is that
    the READER does not see the line twice, and that still holds exactly: the
    drain skips receipts, so no ordinary message record exists to be mirrored.
    """
    assert TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="ответ", sid="x", urgent=True,
        record_outbound=False) is True

    assert sent
    recs = _spool(tmp_path)
    assert [r.get("kind") for r in recs] == [OB.RECEIPT_KIND]
    assert not [r for r in recs if r.get("kind") != OB.RECEIPT_KIND]


def test_send_and_pin_records_too(tmp_path: Path, sent) -> None:
    """It deliberately bypasses `send` — exactly the kind of second send path
    that made the store read as an inbox."""
    TG.TgClient(_cfg(tmp_path)).send_and_pin(
        chat_id=CHAT, text="закреплено", topic_id=11)
    recs = _spool(tmp_path)
    assert [r["text"] for r in recs] == ["закреплено"]
    assert recs[0]["author"] == "system:tg-listener"


# ---------------------------------------------------------------------------
# MAX — the twin. The page-channel switch (T-0610) means a stakeholder
# conversation can be happening here, and recording only one transport is how
# this repo's duplicated-decision bugs start.
# ---------------------------------------------------------------------------

def test_max_send_records_what_was_delivered(tmp_path: Path, sent) -> None:
    MX.MaxClient(_cfg(tmp_path)).send(
        chat_id="42", text="ответ", sid="x", urgent=True)
    (rec,) = _spool(tmp_path)
    assert rec["channel"] == "max"
    assert rec["chat_id"] == "42"
    assert rec["text"] == "[x] ответ"
    assert rec["author"] == "system:x"


def test_max_send_still_succeeds_when_logging_blows_up(
        tmp_path: Path, sent, monkeypatch) -> None:
    monkeypatch.setattr(OB, "record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert MX.MaxClient(_cfg(tmp_path)).send(
        chat_id="42", text="важное", sid="x", urgent=True) is True


def test_max_record_outbound_false_suppresses_the_record_not_the_send(
        tmp_path: Path, sent) -> None:
    assert MX.MaxClient(_cfg(tmp_path)).send(
        chat_id="42", text="ответ", sid="x", urgent=True,
        record_outbound=False) is True
    assert sent and _spool(tmp_path) == []


# ---------------------------------------------------------------------------
# T-0759: the DECLARED opt-out. `record_outbound=False` used to be invisible —
# the send left a debounce witness and no trace of why it was not spooled, so a
# liveness check comparing witnesses against the spool read a healthy lifecycle
# notice as decay (measured live 2026-07-27: witness 19:48:55Z vs newest spool
# record 18:35:40Z, a 73-minute "gap" with nothing wrong).
# ---------------------------------------------------------------------------

def test_tg_opt_out_declares_itself(tmp_path: Path, sent) -> None:
    assert TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="ответ", sid="x", urgent=True,
        record_outbound=False) is True

    marker = OB.unspooled_marker(tmp_path)
    assert marker.exists()
    assert marker.read_bytes() == b""  # a timestamp, NOT a second copy of the message


def test_max_opt_out_declares_itself(tmp_path: Path, sent) -> None:
    """The twin — MAX writes its debounce marker unconditionally too, so an
    undeclared opt-out reads as decay on exactly the install's primary channel."""
    assert MX.MaxClient(_cfg(tmp_path)).send(
        chat_id="42", text="ответ", sid="x", urgent=True,
        record_outbound=False) is True

    assert OB.unspooled_marker(tmp_path).exists()
    assert OB.unspooled_marker(tmp_path).stat().st_size == 0


@pytest.mark.parametrize("transport", ["tg", "max"])
def test_the_marker_stays_contentless_on_both_transports(
        tmp_path: Path, sent, transport: str) -> None:
    """THE property that justifies this file existing at all, pinned rather than
    described (T-0761, p330's review of T-0759).

    p298's rule bans a SECOND PARALLEL RECORD — a second place where the content
    of what we told the stakeholder lives, because two copies silently diverge.
    An empty file cannot diverge from anything: it makes no claim about content,
    only about WHEN. That is the entire argument for it being inside the rule
    rather than around it, and until now it was prose. A later change adding
    `chat_id` "for debuggability" would have passed every other test and quietly
    turned it into exactly what was forbidden.

    Both transports, because the chokepoint is the thing worth pinning and
    one-path-covered-and-the-other-not is the failure that created T-0755.
    """
    if transport == "tg":
        TG.TgClient(_cfg(tmp_path)).send(
            chat_id=CHAT, text="ответ", sid="x", urgent=True, record_outbound=False)
    else:
        MX.MaxClient(_cfg(tmp_path)).send(
            chat_id="42", text="ответ", sid="x", urgent=True, record_outbound=False)

    marker = OB.unspooled_marker(tmp_path)
    assert marker.exists()
    assert marker.stat().st_size == 0
    assert marker.read_bytes() == b""


def test_a_recorded_send_declares_no_opt_out(tmp_path: Path, sent) -> None:
    """The negative guard: if every send touched the marker it would account
    for its own witness and the alarm could never fire."""
    TG.TgClient(_cfg(tmp_path)).send(chat_id=CHAT, text="ответ", sid="x", urgent=True)

    assert _spool(tmp_path)
    assert not OB.unspooled_marker(tmp_path).exists()


def test_a_marker_failure_never_breaks_the_send(tmp_path: Path, sent, monkeypatch) -> None:
    monkeypatch.setattr(OB, "note_unspooled",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert TG.TgClient(_cfg(tmp_path)).send(
        chat_id=CHAT, text="важное", sid="x", urgent=True,
        record_outbound=False) is True
