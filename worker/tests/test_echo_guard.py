"""T-0746 item (c) — our own text can never be recorded as the stakeholder's.

The live corruption these lock down: on 2026-07-27T04:57:58Z the bot-squad
conversation store recorded

    {"author": "user", "text": "❌ session S-…-p266 not active — message dropped"}

i.e. our own notice, attributed to the human, in the permanent record. Every
test here is written against the REAL notice text and the real entry shapes.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import echo_guard as EG


BOT_ID = "8206895402"
NOTICE = ("❌ session S-almdudleer-rv-pair-trading-signals-poc-review-real--p266 "
          "not active — message dropped")
HIS_OWN = ("Такую ситуацию должен подхватывать user conversation, и в целом все "
           "должны иметь доступ к истории по всем чатам и топикам")


def _cfg(tmp_path: Path, *, token: str = f"{BOT_ID}:SECRET"):
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(tg_bot_token=token, data_dir=data_dir, projects={})


def _msg(text: str, **extra) -> dict:
    msg = {"message_id": 1, "date": 1785000000, "chat": {"id": -100123, "type": "supergroup"},
           "from": {"id": 404580642, "is_bot": False}, "text": text}
    msg.update(extra)
    return msg


def _seed_send(cfg, text: str, *, chat_id="-100123"):
    """Record an outbound send the way the transport does (T-0755)."""
    from bot_squad_worker import outbound_log
    return outbound_log.record(cfg.data_dir, channel="tg", chat_id=chat_id,
                               text=text, sender_label="tg-listener", cfg=cfg)


# ---------------------------------------------------------------------------
# forward_provenance — both encodings TG has shipped
# ---------------------------------------------------------------------------

def test_provenance_empty_for_a_typed_message():
    assert EG.forward_provenance(_msg(HIS_OWN)) == ""


def test_provenance_forward_origin_user():
    msg = _msg(NOTICE, forward_origin={
        "type": "user", "date": 1, "sender_user": {"id": 777, "is_bot": True}})
    assert EG.forward_provenance(msg) == "user:777"


def test_provenance_forward_origin_hidden_keeps_the_name():
    msg = _msg(NOTICE, forward_origin={
        "type": "hidden_user", "date": 1, "sender_user_name": "Bot Squad"})
    assert EG.forward_provenance(msg) == "Bot Squad"


def test_provenance_forward_origin_chat_and_channel():
    assert EG.forward_provenance(_msg("x", forward_origin={
        "type": "chat", "date": 1, "sender_chat": {"id": -42}})) == "chat:-42"
    assert EG.forward_provenance(_msg("x", forward_origin={
        "type": "channel", "date": 1, "chat": {"id": -7}, "message_id": 3})) == "chat:-7"


def test_provenance_legacy_fields_pre_bot_api_7():
    assert EG.forward_provenance(_msg("x", forward_from={"id": 777})) == "user:777"
    assert EG.forward_provenance(_msg("x", forward_from_chat={"id": -9})) == "chat:-9"
    assert EG.forward_provenance(_msg("x", forward_sender_name="Someone")) == "Someone"
    # A forward whose only surviving marker is the date is still a forward.
    assert EG.forward_provenance(_msg("x", forward_date=1785000000)) == "hidden"


# ---------------------------------------------------------------------------
# RUNG 1 — forwarded from our own bot
# ---------------------------------------------------------------------------

def test_own_bot_forward_detected_from_the_token(tmp_path):
    cfg = _cfg(tmp_path)
    msg = _msg(NOTICE, forward_origin={
        "type": "user", "date": 1, "sender_user": {"id": int(BOT_ID), "is_bot": True}})
    assert EG.is_own_bot_forward(msg, cfg) is True


def test_another_bots_forward_is_not_ours(tmp_path):
    cfg = _cfg(tmp_path)
    msg = _msg(NOTICE, forward_origin={
        "type": "user", "date": 1, "sender_user": {"id": 12345, "is_bot": True}})
    assert EG.is_own_bot_forward(msg, cfg) is False


def test_hidden_origin_is_not_treated_as_proof(tmp_path):
    """"Someone chose not to say" must never be read as "it was us" — guessing
    there is how a guard starts eating the stakeholder's own words. Rung 2
    covers the case instead."""
    cfg = _cfg(tmp_path)
    msg = _msg(NOTICE, forward_origin={
        "type": "hidden_user", "date": 1, "sender_user_name": "Bot Squad"})
    assert EG.is_own_bot_forward(msg, cfg) is False


def test_no_token_configured_means_no_rung_one(tmp_path):
    cfg = _cfg(tmp_path, token="")
    msg = _msg(NOTICE, forward_origin={
        "type": "user", "date": 1, "sender_user": {"id": int(BOT_ID)}})
    assert EG.is_own_bot_forward(msg, cfg) is False


def test_own_bot_id_matches_tg_listeners_derivation(tmp_path):
    """The two derivations are duplicated on purpose (import cycle); they must
    not be allowed to drift about which bot is "ours"."""
    import bot_squad_worker.tg_listener as TL
    cfg = _cfg(tmp_path)
    assert EG._own_bot_id(cfg) == TL._own_bot_id(cfg) == BOT_ID


# ---------------------------------------------------------------------------
# RUNG 2 — we can prove we sent these exact bytes
# ---------------------------------------------------------------------------

def test_recent_send_match_finds_our_own_notice(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_send(cfg, NOTICE)
    assert EG.recent_send_match(cfg, chat_id="-100123", text=NOTICE) is not None


def test_recent_send_match_ignores_whitespace_drift(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_send(cfg, NOTICE)
    mangled = NOTICE.replace(" ", " ", 1) + "  \n"
    assert EG.recent_send_match(cfg, chat_id="-100123", text=mangled) is not None


def test_recent_send_match_is_scoped_to_the_same_chat(tmp_path):
    """A body we sent elsewhere arriving here is the human relaying it — his
    act, recorded as a forward, not our echo."""
    cfg = _cfg(tmp_path)
    _seed_send(cfg, NOTICE, chat_id="-100999")
    assert EG.recent_send_match(cfg, chat_id="-100123", text=NOTICE) is None


def test_recent_send_match_ignores_short_text(tmp_path):
    """We send "Принял"; so does he. Below the length floor a match is not
    evidence, and claiming it would be the same lie in the other direction."""
    cfg = _cfg(tmp_path)
    _seed_send(cfg, "Принял")
    assert EG.recent_send_match(cfg, chat_id="-100123", text="Принял") is None


def test_recent_send_match_ignores_a_stale_send(tmp_path):
    from datetime import datetime, timedelta, timezone
    from bot_squad_worker import outbound_log
    cfg = _cfg(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=EG.LOOKBACK_DAYS + 2))
    outbound_log.record(cfg.data_dir, channel="tg", chat_id="-100123", text=NOTICE,
                        sender_label="tg-listener", cfg=cfg,
                        timestamp=old.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert EG.recent_send_match(cfg, chat_id="-100123", text=NOTICE) is None


def test_recent_send_match_survives_a_broken_spool(tmp_path):
    """A lookup failure means "no evidence", never a raised exception — this
    runs on the inbound path and must not be able to break intake."""
    cfg = _cfg(tmp_path)
    spool = cfg.data_dir / "_worker" / "outbound"
    spool.mkdir(parents=True)
    (spool / "2026-07-27.jsonl").write_text("{not json\n", encoding="utf-8")
    assert EG.recent_send_match(cfg, chat_id="-100123", text=NOTICE) is None


# ---------------------------------------------------------------------------
# classify_inbound — the verdict tg_listener actually consumes
# ---------------------------------------------------------------------------

def test_classify_the_live_incident_shape(tmp_path):
    """THE regression: the exact notice, forwarded from our own bot."""
    cfg = _cfg(tmp_path)
    msg = _msg(NOTICE, forward_origin={
        "type": "user", "date": 1, "sender_user": {"id": int(BOT_ID), "is_bot": True}})
    v = EG.classify_inbound(cfg, msg)
    assert v == {"author": "system:bot-echo", "forwarded_from": "bot",
                 "reason": "forward-origin"}


def test_classify_hidden_forward_falls_through_to_the_delivery_match(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_send(cfg, NOTICE)
    msg = _msg(NOTICE, forward_origin={
        "type": "hidden_user", "date": 1, "sender_user_name": "Bot Squad"})
    v = EG.classify_inbound(cfg, msg)
    assert v["author"] == EG.ECHO_AUTHOR and v["reason"] == "outbound-match"


def test_classify_copy_paste_with_no_metadata_at_all(tmp_path):
    """No forward marks whatsoever — the case rung 1 cannot see and the one a
    text-shape matcher would have had to guess at."""
    cfg = _cfg(tmp_path)
    _seed_send(cfg, NOTICE)
    v = EG.classify_inbound(cfg, _msg(NOTICE))
    assert v["author"] == EG.ECHO_AUTHOR and v["reason"] == "outbound-match"


def test_classify_is_class_independent(tmp_path):
    """Nothing here knows about the "not active" notice. ANY body we sent gets
    the same treatment — which is what makes it a guard rather than a patch for
    the one message class we already know about."""
    cfg = _cfg(tmp_path)
    other = "📋 T-0719 → closed — REGRESSION: the compact TG label broke routing"
    _seed_send(cfg, other)
    assert EG.classify_inbound(cfg, _msg(other))["author"] == EG.ECHO_AUTHOR


def test_classify_someone_elses_forward_stays_user_authored(tmp_path):
    """A human DID write it, so `user` is still correct — but the record must
    not imply the SENDER composed it."""
    cfg = _cfg(tmp_path)
    msg = _msg("глянь что пишут", forward_origin={
        "type": "user", "date": 1, "sender_user": {"id": 999, "is_bot": False}})
    v = EG.classify_inbound(cfg, msg)
    assert v == {"author": "user", "forwarded_from": "user:999", "reason": "forwarded"}


def test_classify_his_own_words_are_untouched(tmp_path):
    """NEGATIVE GUARD — must pass before AND after the fix. The verdict for a
    genuine message is byte-identical to the pre-T-0746 hardcoded payload."""
    cfg = _cfg(tmp_path)
    _seed_send(cfg, NOTICE)
    v = EG.classify_inbound(cfg, _msg(HIS_OWN))
    assert v == {"author": "user", "forwarded_from": "", "reason": ""}


def test_classify_never_raises_on_a_malformed_message(tmp_path):
    cfg = _cfg(tmp_path)
    v = EG.classify_inbound(cfg, {"forward_origin": "not-a-dict", "text": HIS_OWN})
    assert v["author"] == "user"


def test_echo_author_obeys_the_t0755_vocabulary():
    """No fourth author class (p312's explicit instruction) — `system:<kind>`."""
    from bot_squad_worker import outbound_log
    assert outbound_log.is_valid_author(EG.ECHO_AUTHOR)
    assert outbound_log.author_class(EG.ECHO_AUTHOR) == "system"
