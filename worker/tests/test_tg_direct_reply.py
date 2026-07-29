"""T-0770 — the direct-mode provenance envelope and the answer it owes.

Every test here maps to a scenario walked through by hand FIRST (T-0158), on a
copy of the live config + bindings; see
``data/bot-squad/scenarios/T-0770-*.md`` for the plain-English versions and the
HEAD reproduction that is their control.

The risk of this change is OVER-reacting — nagging a session that did answer,
or accusing one when the log cannot see. Several tests below therefore pin
behaviour that must NOT happen, and they are the point.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot_squad_worker import tg_direct_reply as TDR

CHAT = "-100376"
TOPIC = 517
SID = "S-alice-dev-p9"
GID = "gu_1"


def _cfg(tmp_path: Path):
    data = tmp_path / "data"
    (data / "_worker").mkdir(parents=True)
    return types.SimpleNamespace(data_dir=data, projects={})


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(**kw) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(**kw))


def _spool(cfg, *, author, thread_id=TOPIC, chat_id=CHAT, when=None, text="ok"):
    """Append one outbound-spool record, in the shape ``outbound_log.record``
    writes. Written directly rather than through the transport: this is the
    OTHER side's artefact, and a test that produced it with the same code it
    checks would prove nothing about the read."""
    from bot_squad_worker import outbound_log
    d = outbound_log.spool_dir(cfg.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"timestamp": when or _iso(datetime.now(timezone.utc)), "direction": "out",
           "author": author, "channel": "tg", "chat_id": str(chat_id),
           "thread_id": thread_id, "text": text}
    with (d / "spool.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _witnesses(cfg):
    """Make ``outbound_liveness`` able to reach a verdict at all.

    Without a witness source it correctly answers BLIND (it cannot tell "no
    sends" from "not recorded"), and every ledger test would then pass for the
    wrong reason — the exact false-green the T-0740 positive-control rule is
    about. ``test_a_ledger_with_no_witness_source_is_blind`` pins that BLIND
    directly, so this fixture is never hiding it."""
    (cfg.data_dir / "_worker" / "tg_debounce").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "_worker" / "tg_debounce" / "marker").write_text("x")


@pytest.fixture(autouse=True)
def _fast_knobs(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_TG_ANSWER_OWED_GRACE_SEC", "60")
    monkeypatch.setenv("BOT_SQUAD_TG_ANSWER_OWED_COOLDOWN_SEC", "60")
    monkeypatch.setenv("BOT_SQUAD_TG_ANSWER_OWED_MAX_ATTEMPTS", "2")
    # T-0774: the DROPS-zeroing this fixture used to do inline has moved to
    # conftest's autouse `_isolate_outbound_drops`, which covers every worker
    # test rather than this file. Its reasoning was right on both counts and is
    # preserved there: a non-zero `outbound_log.DROPS` makes
    # `outbound_liveness.check` report DECAYED, which this module reads as BLIND
    # (see `answered_since`) — turning 13 tests here green-for-the-wrong-reason
    # in a full run — and ZEROING is required rather than restoring, because a
    # count leaked by an earlier module is already in place by the time any
    # fixture here runs.
    yield


class _Doubles:
    """Capture the tick's side effects; nothing leaves the process."""

    def __init__(self, monkeypatch, *, flush=None):
        import bot_squad_worker.actions as A
        import bot_squad_worker.input_mux as IM
        import bot_squad_worker.tg_listener as TL

        self.sent: list = []
        self.queued: list = []
        self.posts: list = []
        self.woke: list = []
        monkeypatch.setattr(A, "dispatch",
                            lambda n, p: self.sent.append((n, p)) or {"ok": True})
        monkeypatch.setattr(IM, "enqueue",
                            lambda dd, sid, text, author:
                            self.queued.append((sid, author, text)) or 1)
        monkeypatch.setattr(IM, "flush", flush or (
            lambda dd, sid, **kw: {"delivered": 1, "deferred": False, "reason": None}))
        monkeypatch.setattr(TL, "_post_conversation",
                            lambda c, s, g, payload:
                            self.posts.append((s, payload)) or True)
        monkeypatch.setattr(TL, "_ensure_user_conversation",
                            lambda c, s, g, ref, **kw:
                            self.woke.append((s, g)) or {"ok": True})


def _owe(cfg, *, at=None, **kw):
    return TDR.record_owed(
        cfg, sid=kw.pop("sid", SID), chat_id=CHAT, thread_id=TOPIC,
        text="прием-прием", slug="watchrobot", gid=GID, ticket_id="T-0314",
        at=at or _ago(minutes=10), **kw)


# ---------------------------------------------------------------------------
# The envelope — S1/S2/S3
# ---------------------------------------------------------------------------

def test_envelope_says_a_human_wrote_over_telegram():
    """S1, the reproduction inverted. At HEAD the session received exactly
    'прием-прием' and nothing else — no sender, no channel, no destination."""
    env = TDR.compose_envelope(
        text="прием-прием", chat_id=CHAT, thread_id=TOPIC, sid=SID,
        slug="watchrobot", ticket_id="T-0314", sender="Alexey")
    assert "TELEGRAM" in env
    assert "Alexey" in env and "a human" in env
    assert str(CHAT) in env and f"topic {TOPIC}" in env
    assert "watchrobot" in env and "T-0314" in env


def test_envelope_carries_his_words_verbatim_and_fenced():
    env = TDR.compose_envelope(text="прием-прием", chat_id=CHAT, thread_id=TOPIC,
                               sid=SID)
    body = env.split("--- 8< ---")[1].split("--- >8 ---")[0]
    assert body.strip() == "прием-прием"


def test_envelope_keeps_a_multiline_message_intact():
    """S2. His real message was four lines; every one of them must survive."""
    text = "прием-прием\n11\nНу и что тут?\n@bot_squad_bot как дела тут"
    env = TDR.compose_envelope(text=text, chat_id=CHAT, thread_id=TOPIC, sid=SID)
    body = env.split("--- 8< ---")[1].split("--- >8 ---")[0]
    assert body.strip() == text


def test_envelope_states_that_answering_in_its_own_turn_is_invisible():
    """The mechanism of the defect, said out loud — a session that answers in
    its own turn believes it has replied."""
    env = TDR.compose_envelope(text="hi", chat_id=CHAT, thread_id=TOPIC, sid=SID)
    assert "HE CANNOT SEE THIS SESSION" in env


def test_envelope_names_the_exact_destination_not_a_lookup():
    """S3, and the sharpest measured edge. `bsq tg ping` / `--ticket` resolve
    to whichever binding names the session FIRST: p70 held 220 and 517, so an
    obedient session would have answered in 220 while he wrote in 517."""
    env = TDR.compose_envelope(text="hi", chat_id=CHAT, thread_id=TOPIC, sid=SID)
    assert f'bsq topic say --chat {CHAT} --topic {TOPIC} "<your answer>"' in env
    # …and it must warn against the resolving forms rather than silently omit
    # them: a session that already knows `bsq tg ping` will otherwise reach for it.
    assert "bsq tg ping" in env and "wrong topic" in env


def test_reply_command_is_the_one_place_the_destination_is_rendered():
    """Envelope and reminder must not drift apart — a session told two
    different commands would pick one, and only one of them stays correct."""
    env = TDR.compose_envelope(text="hi", chat_id=CHAT, thread_id=TOPIC, sid=SID)
    rem = TDR.compose_reminder(
        {"chat_id": CHAT, "thread_id": TOPIC, "text": "hi"},
        attempt=1, attempts_left=1)
    cmd = TDR.reply_command(CHAT, TOPIC)
    assert cmd in env and cmd in rem


def test_reminder_quotes_him_back_capped():
    long = "x" * 900
    rem = TDR.compose_reminder({"chat_id": CHAT, "thread_id": TOPIC, "text": long},
                               attempt=1, attempts_left=1)
    assert "…" in rem and len(rem) < 1200


def test_reminder_says_what_happens_when_it_runs_out():
    last = TDR.compose_reminder({"chat_id": CHAT, "thread_id": TOPIC, "text": "hi"},
                                attempt=2, attempts_left=0)
    assert "last reminder" in last and "attendant" in last


# ---------------------------------------------------------------------------
# The LIGHT envelope (T-0773) — provenance without a debt
# ---------------------------------------------------------------------------

def test_light_envelope_says_a_human_wrote_over_telegram():
    env = TDR.compose_light_envelope(text="да", chat_id=CHAT, thread_id=TOPIC,
                                     sid=SID, sender="Alexey")
    assert "TELEGRAM" in env and "a human" in env and "Alexey" in env
    assert f"chat {CHAT}, forum topic {TOPIC}" in env
    assert SID in env


def test_light_envelope_keeps_a_multiline_message_intact():
    text = "line1\nline2\nline3"
    env = TDR.compose_light_envelope(text=text, chat_id=CHAT, thread_id=TOPIC, sid=SID)
    body = env.split("--- 8< ---")[1].split("--- >8 ---")[0]
    assert body.strip() == text


@pytest.mark.parametrize("origin", ["reply", "say"])
def test_light_envelope_never_claims_an_answer_is_owed(origin):
    """★ The operator ruling on T-0773, pinned at the source. Both of these
    strings are instructions to post back into his thread; the full envelope
    carries them and this one must not, on either origin. A session told an
    answer is owed acknowledges — which is the ticket's harm inverted."""
    env = TDR.compose_light_envelope(text="да", chat_id=CHAT, thread_id=TOPIC,
                                     sid=SID, origin=origin)
    assert "AN ANSWER IS OWED" not in env
    assert "Send it now" not in env
    assert "remind you" not in env


def test_the_reply_origin_offers_a_route_and_the_say_origin_does_not():
    """`/say` is one-way by construction and the ruling explicitly did NOT
    decide whether it should ever carry a reply back ("a different feature,
    nobody has asked for it, and this ticket must not grow it"). So it names no
    route at all, while a `[<sid>]` reply — where he IS reachable, in the thread
    the bot just wrote in — gets one, conditional on having something to say."""
    reply = TDR.compose_light_envelope(text="да", chat_id=CHAT, thread_id=TOPIC,
                                       sid=SID, origin="reply")
    say = TDR.compose_light_envelope(text="сделай", chat_id=CHAT, thread_id=TOPIC,
                                     sid=SID, origin="say")
    assert TDR.reply_command(CHAT, TOPIC) in reply
    assert "bsq topic say" not in say and "bsq tg ping" not in say
    assert "one-way" in say


def test_answer_route_never_hands_a_dm_a_topic_command():
    """`bsq topic say` refuses --chat without --topic, so `--topic None` would
    die on arrival — a correct-looking prompt string that fails one step later
    (the T-0775 class of defect)."""
    assert TDR.answer_route(CHAT, TOPIC) == TDR.reply_command(CHAT, TOPIC)
    assert TDR.answer_route(CHAT, None) == 'bsq tg ping "<your answer>"'
    assert "--topic" not in TDR.answer_route(CHAT, None)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def test_record_owed_writes_one_entry_keyed_by_chat_topic_and_sid(tmp_path):
    cfg = _cfg(tmp_path)
    _owe(cfg)
    mapping = TDR.load(cfg)
    assert list(mapping) == [f"{CHAT}:{TOPIC}:{SID}"]
    entry = mapping[f"{CHAT}:{TOPIC}:{SID}"]
    assert entry["text"] == "прием-прием" and entry["attempts"] == 0


def test_two_topics_of_the_same_session_are_two_separate_debts(tmp_path):
    """p70 held 220 AND 517 — an answer in one is not an answer in the other,
    which is also why the key is not the sid."""
    cfg = _cfg(tmp_path)
    _owe(cfg)
    TDR.record_owed(cfg, sid=SID, chat_id=CHAT, thread_id=220, text="и тут")
    assert len(TDR.load(cfg)) == 2


def test_a_new_message_replaces_the_debt_and_resets_its_attempts(tmp_path):
    cfg = _cfg(tmp_path)
    _owe(cfg)
    mapping = TDR.load(cfg)
    mapping[f"{CHAT}:{TOPIC}:{SID}"]["attempts"] = 2
    TDR._save(cfg, mapping)
    TDR.record_owed(cfg, sid=SID, chat_id=CHAT, thread_id=TOPIC, text="ну?")
    entry = TDR.load(cfg)[f"{CHAT}:{TOPIC}:{SID}"]
    assert entry["attempts"] == 0 and entry["text"] == "ну?"


def test_a_general_feed_binding_owes_nothing(tmp_path):
    """thread_id None is the project's General room, not a topic bound to one
    session — there is no per-topic answer to measure."""
    cfg = _cfg(tmp_path)
    assert TDR.record_owed(cfg, sid=SID, chat_id=CHAT, thread_id=None, text="hi") is None
    assert TDR.load(cfg) == {}


def test_kill_switch_stops_the_ledger(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_TG_ANSWER_OWED", "0")
    assert _owe(cfg) is None
    assert TDR.tick(cfg)["disabled"] is True


def test_an_unreadable_ledger_reads_as_empty_not_as_a_crash(tmp_path):
    cfg = _cfg(tmp_path)
    TDR.owed_path(cfg).write_text("{not json", encoding="utf-8")
    assert TDR.load(cfg) == {}


# ---------------------------------------------------------------------------
# answered_since — the measurement
# ---------------------------------------------------------------------------

def test_a_session_send_into_that_topic_is_an_answer(tmp_path):
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author=f"session:{SID}")
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.ANSWERED


def test_a_peer_session_answering_in_that_topic_also_counts(tmp_path):
    """The harm is HIS silence, not which process broke it."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="session:S-almdudleer-operator-p374")
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.ANSWERED


def test_a_system_post_in_the_topic_does_not_clear_the_debt(tmp_path):
    """★ The false-clear guard. A 📋 lifecycle notice, a deploy post and this
    module's OWN escalation notice all land in topics for their own reasons.
    Counting them would silently restore the pre-T-0770 behaviour with every
    test still green."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="📋 T-0314 → totest")
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.UNANSWERED


def test_an_answer_in_a_different_topic_does_not_count(tmp_path):
    """The wrong-topic answer is exactly what he experienced as silence."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author=f"session:{SID}", thread_id=220)
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.UNANSWERED


def test_an_answer_in_a_different_chat_does_not_count(tmp_path):
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author=f"session:{SID}", chat_id="-999")
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.UNANSWERED


def test_a_send_from_before_his_message_does_not_count(tmp_path):
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author=f"session:{SID}", when=_ago(hours=2))
    # A recent record elsewhere, so the log is demonstrably CURRENT: without it
    # the newest accounted-for send is two hours behind the (just-created)
    # witness marker and `outbound_liveness` correctly reports DECAYED, which
    # this module treats as BLIND. That would pass the assertion below for
    # entirely the wrong reason.
    _spool(cfg, author="system:bot-squad", chat_id="-999", text="unrelated")
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.UNANSWERED


def test_an_empty_spool_is_blind_not_an_accusation(tmp_path):
    """★ The positive control (T-0740/T-0759). Zero records reads exactly like
    'nobody answered him', and most convincingly when the recording is broken —
    which is when this would re-drive every session on the install at once."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.BLIND


def test_a_ledger_with_no_witness_source_is_blind(tmp_path):
    """No tg_reply_map / debounce markers → nothing can tell 'no sends' from
    'not recorded'. Reported as BLIND, and this test is why the other tests'
    witness fixture is not hiding a false green."""
    cfg = _cfg(tmp_path)
    _spool(cfg, author=f"session:{SID}")
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.BLIND


def test_a_decayed_log_is_blind_too(tmp_path, monkeypatch):
    """Sends are happening and NOT being recorded — such a log cannot be used
    to prove an answer is absent."""
    cfg = _cfg(tmp_path)
    from bot_squad_worker import outbound_liveness
    monkeypatch.setattr(outbound_liveness, "check",
                        lambda c, **kw: {"state": outbound_liveness.DECAYED})
    _spool(cfg, author=f"session:{SID}")
    assert TDR.answered_since(cfg, chat_id=CHAT, thread_id=TOPIC,
                              since=_ago(minutes=10)) == TDR.BLIND


# ---------------------------------------------------------------------------
# The tick — S4/S5/S6/S7/S10
# ---------------------------------------------------------------------------

def test_inside_the_grace_period_nothing_happens(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="unrelated")
    _owe(cfg, at=_iso(datetime.now(timezone.utc)))
    d = _Doubles(monkeypatch)
    res = TDR.tick(cfg)
    assert res["reminded"] == [] and d.queued == []
    assert TDR.load(cfg)


def test_unanswered_past_the_grace_re_drives_the_session(tmp_path, monkeypatch):
    """S4 — the whole point: the silence is MEASURED, not hoped away."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="unrelated")
    _owe(cfg)
    d = _Doubles(monkeypatch)
    res = TDR.tick(cfg)
    assert res["reminded"] == [f"{CHAT}:{TOPIC}:{SID}"]
    assert len(d.queued) == 1
    sid, author, text = d.queued[0]
    assert sid == SID and author == "tg-answer-owed"
    assert TDR.reply_command(CHAT, TOPIC) in text
    assert TDR.load(cfg)[f"{CHAT}:{TOPIC}:{SID}"]["attempts"] == 1


def test_an_answered_debt_is_cleared_and_never_nagged(tmp_path, monkeypatch):
    """S5."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _owe(cfg)
    _spool(cfg, author=f"session:{SID}", text="готово, посмотри")
    d = _Doubles(monkeypatch)
    res = TDR.tick(cfg)
    assert res["cleared"] == [f"{CHAT}:{TOPIC}:{SID}"]
    assert d.queued == [] and d.sent == []
    assert TDR.load(cfg) == {}


def test_a_blind_log_burns_no_attempt_and_keeps_the_debt(tmp_path, monkeypatch):
    """S6. Degrading to silence is correct here — an accusation the log cannot
    support is worse than a late reminder — but the debt must SURVIVE so it is
    re-checked once the log can answer."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _owe(cfg)
    d = _Doubles(monkeypatch)
    res = TDR.tick(cfg)
    assert res["blind"] == [f"{CHAT}:{TOPIC}:{SID}"]
    assert res["reminded"] == [] and d.queued == []
    assert TDR.load(cfg)[f"{CHAT}:{TOPIC}:{SID}"]["attempts"] == 0


def test_the_cooldown_paces_the_reminders(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="unrelated")
    _owe(cfg)
    d = _Doubles(monkeypatch)
    TDR.tick(cfg)
    TDR.tick(cfg)
    assert len(d.queued) == 1


def test_exhausted_reminders_tell_the_topic_and_wake_the_attendant(tmp_path, monkeypatch):
    """S7 — he must not be the one who discovers nobody answered."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="unrelated")
    _owe(cfg)
    d = _Doubles(monkeypatch)
    for _ in range(3):
        TDR.tick(cfg)
        mapping = TDR.load(cfg)
        for e in mapping.values():  # open the cooldown; the clock is not the subject
            e["last_attempt_at"] = ""
        if mapping:
            TDR._save(cfg, mapping)
    assert len(d.queued) == 2                      # bounded, not forever
    assert len(d.sent) == 1
    verb, params = d.sent[0]
    assert verb == "tg_notify"
    assert params["chat_id"] == CHAT and params["topic_id"] == TOPIC
    assert SID in params["message"]
    # …and his text reaches an attendant that WILL answer, in the T-0746 shape:
    # the system's account of the situation, then HIS words as his own.
    authors = [p["author"] for _, p in d.posts]
    assert authors == ["system:answer-owed", "user"]
    assert d.posts[1][1]["text"] == "прием-прием"
    assert d.woke == [("watchrobot", GID)]
    assert TDR.load(cfg) == {}


def test_a_dead_pane_is_handed_over_immediately(tmp_path, monkeypatch):
    """S10 — reminding a corpse on a cooldown is the silence again, slower."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="unrelated")
    _owe(cfg)
    d = _Doubles(monkeypatch, flush=lambda dd, sid, **kw: {
        "delivered": 0, "deferred": True, "reason": "no_pane"})
    res = TDR.tick(cfg)
    assert res["escalated"] == [f"{CHAT}:{TOPIC}:{SID}"]
    assert d.woke == [("watchrobot", GID)]
    assert TDR.load(cfg) == {}


def test_a_busy_composer_defers_without_burning_an_attempt(tmp_path, monkeypatch):
    """The reminder is queued and WILL land; it must not consume one of the two
    chances the session gets, and a quiet summary must not read as 'nothing
    happened' while one is in flight."""
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="unrelated")
    _owe(cfg)
    d = _Doubles(monkeypatch, flush=lambda dd, sid, **kw: {
        "delivered": 0, "deferred": True, "reason": "composer_busy"})
    res = TDR.tick(cfg)
    assert res["deferred"] == [f"{CHAT}:{TOPIC}:{SID}"]
    assert len(d.queued) == 1
    assert TDR.load(cfg)[f"{CHAT}:{TOPIC}:{SID}"]["attempts"] == 0


def test_one_broken_entry_does_not_stop_the_sweep(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _witnesses(cfg)
    _spool(cfg, author="system:bot-squad", text="unrelated")
    _owe(cfg)
    mapping = TDR.load(cfg)
    # A malformed entry sorted BEFORE the real one, so a sweep that dies on it
    # never reaches the debt that matters.
    mapping["!garbage"] = {"at": "not-a-date", "chat_id": None, "thread_id": []}
    TDR._save(cfg, mapping)
    _Doubles(monkeypatch)
    res = TDR.tick(cfg)
    assert res["ok"] is True
    assert f"{CHAT}:{TOPIC}:{SID}" in res["reminded"]


def test_an_empty_ledger_is_a_cheap_no_op(tmp_path):
    cfg = _cfg(tmp_path)
    assert TDR.tick(cfg)["checked"] == 0
