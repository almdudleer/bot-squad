"""T-0755: the outbound-content log — what we actually SENT.

The gap these cover, concretely: on 2026-07-27 an operator read the conversation
store for 04:47-05:10Z, saw only ``author=user`` lines, and concluded the
stakeholder had gone unanswered for ~2h50m. He had been answered four times.
``tg_reply_map`` held the routing keys for those four sends and none of their
content, so the only provable fact was that *something* went out.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import outbound_log as OB


@pytest.fixture(autouse=True)
def _reset_drops():
    """DROPS is process-global, and two tests here bump it ON PURPOSE.

    ``test_outbound_liveness.py`` has carried this fixture since T-0759 ("a
    leaked count would make every later check read DECAYED for the wrong
    reason") but the module that does the LEAKING never had it — the two only
    coexisted because 'liveness' sorts before 'log', so the victim ran first.
    T-0770 added a second consumer of that verdict, which sorts after both, and
    it read DECAYED->BLIND for 13 tests. Restoring the counter here fixes it at
    the source rather than in each new reader."""
    before = dict(OB.DROPS)
    yield
    OB.DROPS.update(before)


SID = "S-almdudleer-gu_dc8262b6cea9098d98e04d7e-user-conversation-p5"
CHAT = "-1003761939853"


def _cfg(tmp_path: Path, **over) -> SimpleNamespace:
    base = dict(
        tg_bot_token="1234567890:AAaaBBbbCCccDDddEEeeFFffGGggHHhhIIj",
        max_bot_token="",
        data_dir=tmp_path,
        tg_quiet_hours_start_utc=0,
        tg_quiet_hours_end_utc=0,
        tg_proxy_url="",
        max_recipient_kind="chat_id",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _spool_lines(tmp_path: Path) -> list[dict]:
    out: list[dict] = []
    for p in sorted(OB.spool_dir(tmp_path).glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# The record itself — the body that tg_reply_map never kept
# ---------------------------------------------------------------------------

def test_record_keeps_the_body_and_the_join_keys(tmp_path: Path) -> None:
    """The whole point: content AND the identifiers that make it auditable."""
    rec = OB.record(
        tmp_path, channel="tg", chat_id=CHAT, text="640 — это номер тикета",
        route_sid=SID, sender_label="bot-squad attendant",
        thread_id=11, message_id=346, reply_to_message_id=344,
    )
    assert rec is not None
    assert rec["text"] == "640 — это номер тикета"
    assert rec["author"] == f"session:{SID}"
    assert rec["direction"] == "out"
    assert rec["chat_id"] == CHAT
    assert rec["thread_id"] == 11
    assert rec["message_id"] == 346
    assert rec["reply_to_message_id"] == 344
    assert rec["timestamp"].endswith("Z")
    assert _spool_lines(tmp_path) == [rec]


def test_record_omits_absent_optional_keys(tmp_path: Path) -> None:
    """A DM send answers nothing and has no thread — the record must not invent
    null joins a reader would then have to distinguish from real ones."""
    rec = OB.record(tmp_path, channel="tg", chat_id=CHAT, text="hi", route_sid=SID)
    assert "thread_id" not in rec
    assert "reply_to_message_id" not in rec
    assert "redacted" not in rec
    assert "truncated" not in rec


def test_record_never_raises_and_counts_the_drop(tmp_path: Path, monkeypatch, caplog) -> None:
    """A logging failure must never break a SEND — and never be silent (T-0586:
    a silent best-effort drop left a real 13.5-minute failure undiagnosable)."""
    before = OB.DROPS["record"]
    monkeypatch.setattr(OB, "_spool_path", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with caplog.at_level("ERROR"):
        assert OB.record(tmp_path, channel="tg", chat_id=CHAT, text="x") is None
    assert OB.DROPS["record"] == before + 1
    assert "DROPPED" in caplog.text


# ---------------------------------------------------------------------------
# The authorship convention (T-0755 owns it; T-0746 consumes it)
# ---------------------------------------------------------------------------

def test_author_for_send_names_a_real_session(tmp_path: Path) -> None:
    assert OB.author_for_send(route_sid=SID, sender_label="x") == f"session:{SID}"


def test_author_for_send_falls_back_to_system_not_to_nothing() -> None:
    """A deploy notification is as unauditable as a session reply — it must be
    recorded under a class, not dropped for lacking a session identity."""
    assert OB.author_for_send(
        route_sid="deploy_monitor", sender_label="deploy_monitor",
    ) == "system:deploy-monitor"
    assert OB.author_for_send(route_sid="", sender_label="") == "system:unattributed"


def test_system_detail_cannot_forge_another_class() -> None:
    """The detail segment is slugified, so a label containing ':' can't produce
    an author that reads as `session:` or as a bare `user`."""
    a = OB.author_for_send(route_sid="", sender_label="user:  evil")
    assert a.startswith("system:")
    assert OB.author_class(a) == "system"


@pytest.mark.parametrize("author,ok", [
    ("user", True),
    (f"session:{SID}", True),
    ("system:task-lifecycle", True),
    ("user:alexey", False),   # `user` means A HUMAN — never a qualified one
    ("session", False),       # class with no detail names nobody
    ("session:", False),
    ("bot:whatever", False),  # invented class
    ("", False),
])
def test_author_vocabulary_is_closed(author: str, ok: bool) -> None:
    assert OB.is_valid_author(author) is ok


def test_worker_and_api_agree_on_the_vocabulary() -> None:
    """The worker COMPOSES authors and the API STORES them. This repo's top bug
    class is a duplicated decision that drifts, so the two definitions are
    pinned to each other rather than merely written the same way today."""
    import importlib.util
    api_cs = Path(__file__).resolve().parents[2] / "api" / "app" / "conversation_store.py"
    spec = importlib.util.spec_from_file_location("_api_cs", api_cs)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert tuple(mod.AUTHOR_CLASSES) == tuple(OB.AUTHOR_CLASSES)
    for a in ("user", f"session:{SID}", "system:x", "user:x", "session", "bot:x", ""):
        assert mod.is_valid_author(a) == OB.is_valid_author(a), a


# ---------------------------------------------------------------------------
# Secrets — a leaked bot token in a world-readable jsonl would be worse than
# the bug this fixes
# ---------------------------------------------------------------------------

def test_redacts_this_installs_real_token(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    text = f"конфиг: {cfg.tg_bot_token} — используй его"
    clean, kinds = OB.redact(text, cfg=cfg)
    assert cfg.tg_bot_token not in clean
    assert kinds == ["live-secret"]
    assert "используй его" in clean  # only the secret goes


@pytest.mark.parametrize("text,kind", [
    ('bot_token = "9876543210:ZZzzYYyyXXxxWWwwVVvvUUuuTTttSSrrQQp"', "tg-bot-token"),
    ("Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345", "bearer"),
    ("tok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.QWxhZGRpbjpvcGVu end", "jwt"),
    ("WORKER_API_TOKEN=supersecretworkertoken123", "assigned-secret"),
    # Bare, not in an assignment — an assignment is caught first by the
    # `assigned-secret` rule, which is fine but tests a different pattern.
    ("старый ключ enc:Z0FBQUFBQm9aWFJoZGdBQUFBQUFBQQ== заменён", "enc-secret"),
])
def test_redacts_token_shapes_without_config(text: str, kind: str) -> None:
    clean, kinds = OB.redact(text, cfg=None)
    assert kind in kinds, (kind, kinds, clean)
    assert "‹redacted:" in clean


def test_assigned_secret_keeps_the_key_name() -> None:
    """Redacting must leave the message diagnosable — blanking the whole line
    would destroy the record this ticket exists to preserve."""
    clean, _ = OB.redact("WORKER_API_TOKEN=supersecretworkertoken123", cfg=None)
    assert clean.startswith("WORKER_API_TOKEN=")
    assert "supersecretworkertoken123" not in clean


def test_ordinary_prose_survives_untouched() -> None:
    """A redactor that eats normal text would quietly destroy the log."""
    text = ("Готово: T-0741 закрыт, глоссарий добавлен в [voice].initial_prompt, "
            "проверил на четырёх заметках, 23.9s -> 24.9s. Ссылка: "
            "https://staging.botsquad.dev/p/bot-squad/tasks/T-0741")
    clean, kinds = OB.redact(text, cfg=None)
    assert clean == text
    assert kinds == []


def test_short_config_values_are_not_treated_as_secrets(tmp_path: Path) -> None:
    """A 4-char 'secret' would blank ordinary words everywhere they occur."""
    cfg = _cfg(tmp_path, tg_bot_token="abc")
    clean, kinds = OB.redact("abc и ещё abc", cfg=cfg)
    assert clean == "abc и ещё abc"
    assert kinds == []


def test_redaction_is_recorded_not_silent(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rec = OB.record(tmp_path, channel="tg", chat_id=CHAT,
                    text=f"t={cfg.tg_bot_token}", route_sid=SID, cfg=cfg)
    assert rec["redacted"] == ["live-secret"]


def test_body_cap_marks_what_it_cut(tmp_path: Path) -> None:
    rec = OB.record(tmp_path, channel="tg", chat_id=CHAT, text="x" * (OB.BODY_CAP + 50),
                    route_sid=SID)
    assert len(rec["text"]) == OB.BODY_CAP
    assert rec["truncated"] == {"orig_len": OB.BODY_CAP + 50, "cap": OB.BODY_CAP}


# ---------------------------------------------------------------------------
# Resolution — addresses (chat, thread) -> identity (slug, gid, thread)
# ---------------------------------------------------------------------------

def _write_locus(tmp_path: Path, mapping: dict) -> None:
    p = tmp_path / "_worker" / "conversation_locus.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping), encoding="utf-8")


def test_resolve_conversation_reads_the_locus_backwards(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _write_locus(tmp_path, {
        "bot-squad:gu_abc": {"chat_id": CHAT, "thread_id": None, "at": "2026-07-27T04:59:46Z"},
    })
    got = OB.resolve_conversation(cfg, chat_id=CHAT, thread_id=None)
    assert got["slug"] == "bot-squad"
    assert got["gid"] == "gu_abc"


def test_resolve_conversation_will_not_cross_threads(tmp_path: Path) -> None:
    """Same chat, different topic, is a DIFFERENT conversation — filing our
    reply under a topic we never sent it to would be a new misattribution."""
    cfg = _cfg(tmp_path)
    _write_locus(tmp_path, {
        "bot-squad:gu_abc:11": {"chat_id": CHAT, "thread_id": 11, "at": "2026-07-27T04:00:00Z"},
    })
    assert OB.resolve_conversation(cfg, chat_id=CHAT, thread_id=None) is None
    assert OB.resolve_conversation(cfg, chat_id=CHAT, thread_id=99) is None
    assert OB.resolve_conversation(cfg, chat_id=CHAT, thread_id=11)["gid"] == "gu_abc"


def test_resolve_conversation_prefers_the_most_recent(tmp_path: Path) -> None:
    """Two users on one General feed is the live shape (both gids sit on
    -1003761939853 with thread_id null)."""
    cfg = _cfg(tmp_path)
    _write_locus(tmp_path, {
        "bot-squad:gu_old": {"chat_id": CHAT, "thread_id": None, "at": "2026-07-25T14:30:51Z"},
        "bot-squad:gu_new": {"chat_id": CHAT, "thread_id": None, "at": "2026-07-27T04:59:46Z"},
    })
    assert OB.resolve_conversation(cfg, chat_id=CHAT, thread_id=None)["gid"] == "gu_new"


def test_resolve_conversation_none_when_unknown(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _write_locus(tmp_path, {})
    assert OB.resolve_conversation(cfg, chat_id="999", thread_id=None) is None


# ---------------------------------------------------------------------------
# The drain — spool -> conversation store
# ---------------------------------------------------------------------------

@pytest.fixture
def drained(tmp_path: Path, monkeypatch):
    """Capture what the drain would POST to the API append endpoint."""
    posts: list[dict] = []

    class _R:
        def raise_for_status(self): pass

    import bot_squad_worker.tg_listener as tgl
    monkeypatch.setattr(tgl, "_api_base_url", lambda: "http://api.test")
    monkeypatch.setattr(tgl, "_worker_api_token", lambda: "tok")
    import httpx
    monkeypatch.setattr(httpx, "post",
                        lambda url, **kw: (posts.append({"url": url, **kw["json"]}), _R())[1])
    _write_locus(tmp_path, {
        "bot-squad:gu_abc": {"chat_id": CHAT, "thread_id": None, "at": "2026-07-27T04:59:46Z"},
    })
    return posts


def test_drain_mirrors_into_the_conversation_thread(tmp_path: Path, drained) -> None:
    cfg = _cfg(tmp_path)
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="ответ", route_sid=SID)
    out = OB.drain(cfg)
    assert out["mirrored"] == 1 and out["failed"] == 0
    (post,) = drained
    assert post["url"].endswith("/conversations/bot-squad/gu_abc/messages")
    assert post["author"] == f"session:{SID}"
    assert post["text"] == "ответ"
    assert post["direction"] == "out"
    # The gate that stops the append endpoint re-sending an already-sent message
    assert post["delivered"] is True


def test_drain_is_idempotent(tmp_path: Path, drained) -> None:
    cfg = _cfg(tmp_path)
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="ответ", route_sid=SID)
    OB.drain(cfg)
    assert OB.drain(cfg)["mirrored"] == 0
    assert len(drained) == 1


def test_drain_advances_past_unresolvable_records(tmp_path: Path, drained) -> None:
    """A deploy notification to a chat nobody converses in has nowhere to be
    mirrored. Retrying it forever would hide real failures behind a permanent
    no-op; its content is already durable in the spool."""
    cfg = _cfg(tmp_path)
    OB.record(tmp_path, channel="tg", chat_id="999", text="deploy ok", route_sid="")
    out = OB.drain(cfg)
    assert (out["mirrored"], out["unresolved"], out["failed"]) == (0, 1, 0)
    assert OB.drain(cfg)["unresolved"] == 0
    assert drained == []


def test_drain_holds_the_cursor_on_a_mirror_failure(tmp_path: Path, monkeypatch, drained) -> None:
    """An API outage must DELAY the interleave, never lose it."""
    cfg = _cfg(tmp_path)
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="ответ", route_sid=SID)
    real_mirror = OB._mirror
    OB._mirror = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down"))
    try:
        assert OB.drain(cfg)["failed"] == 1
    finally:
        # Restored directly, NOT via monkeypatch.undo() — the `drained` fixture
        # shares this test's monkeypatch instance, so undo() would also revert
        # the API stub and the retry would resolve to nothing.
        OB._mirror = real_mirror
    assert OB.drain(cfg)["mirrored"] == 1


def test_drain_skips_a_torn_line_instead_of_stalling_behind_it(tmp_path: Path, drained) -> None:
    cfg = _cfg(tmp_path)
    OB.spool_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="first", route_sid=SID)
    p = sorted(OB.spool_dir(tmp_path).glob("*.jsonl"))[0]
    with p.open("a", encoding="utf-8") as f:
        f.write('{"timestamp": "2026-0\n')
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="second", route_sid=SID)
    out = OB.drain(cfg)
    assert out["mirrored"] == 2 and out["failed"] == 0
    assert [p["text"] for p in drained] == ["first", "second"]


def test_drain_never_raises(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(OB, "_load_cursor", lambda *a: (_ for _ in ()).throw(OSError("x")))
    before = OB.DROPS["drain"]
    assert "drops" in OB.drain(cfg)
    assert OB.DROPS["drain"] == before + 1


def test_prune_deletes_only_old_spool_files(tmp_path: Path, drained) -> None:
    """Retention applies to the SPOOL (a delivery queue), never to the archive —
    deleting what we told the stakeholder is the failure this ticket fixes."""
    cfg = _cfg(tmp_path)
    d = OB.spool_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "2020-01-01.jsonl").write_text("", encoding="utf-8")
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="new", route_sid=SID)
    assert OB.drain(cfg)["pruned"] == 1
    assert not (d / "2020-01-01.jsonl").exists()
    assert len(list(d.glob("*.jsonl"))) == 1


def test_spool_health_reports_undrained_backlog(tmp_path: Path, drained) -> None:
    cfg = _cfg(tmp_path)
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="a", route_sid=SID)
    assert OB.spool_health(tmp_path)["undrained"] == 1
    OB.drain(cfg)
    assert OB.spool_health(tmp_path)["undrained"] == 0


def test_read_spool_is_the_lookup_that_needs_no_routing(tmp_path: Path) -> None:
    """"Did we answer him between 04:47 and 05:10?" must be answerable even
    when resolution failed — that question is the whole ticket."""
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="a", route_sid=SID,
              timestamp="2026-07-27T04:58:34Z")
    OB.record(tmp_path, channel="tg", chat_id="999", text="b", route_sid=SID,
              timestamp="2026-07-27T05:01:02Z")
    got = OB.read_spool(tmp_path, since="2026-07-27T04:47:00Z", chat_id=CHAT)
    assert [r["text"] for r in got] == ["a"]


# ---------------------------------------------------------------------------
# T-0759: the read has to prove itself
# ---------------------------------------------------------------------------

def test_spool_health_carries_the_proof_that_the_read_was_live(tmp_path: Path) -> None:
    """``files``/``records``/``dir``/``exists`` are the positive control.

    Measured on the live install 2026-07-27: ``read_spool(<data>)`` returned 21
    records and ``read_spool(<data>/_worker)`` returned 0 — silently, no
    exception, because ``spool_dir`` appends ``_worker/outbound`` itself. A
    monitor built on the wrong argument reports "no sends" forever, and does it
    most convincingly during a real outage. So a zero is only believable once
    these say the read reached a live store.
    """
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text="письмо", route_sid=SID)

    good = OB.spool_health(tmp_path)
    assert good["exists"] is True
    assert (good["files"], good["records"]) == (1, 1)
    assert good["last_record_ts"]

    blind = OB.spool_health(tmp_path / "_worker")
    assert blind["exists"] is False
    assert (blind["files"], blind["records"]) == (0, 0)
    assert blind["dir"].endswith("_worker/_worker/outbound")


def test_note_unspooled_is_a_timestamp_not_a_record(tmp_path: Path) -> None:
    """It exists so a liveness check can tell "we did not send" from "we sent
    and did not record". It must never become a second copy of the message —
    that is the parallel record T-0759 explicitly forbids."""
    OB.note_unspooled(tmp_path)

    marker = OB.unspooled_marker(tmp_path)
    assert marker.exists() and marker.read_bytes() == b""
    assert OB.spool_health(tmp_path)["last_unspooled_ts"]
    assert OB.spool_health(tmp_path)["records"] == 0  # not a spool line


def test_note_unspooled_never_raises(tmp_path: Path) -> None:
    """Called from the send path; a bookkeeping failure must not fail a
    delivered message."""
    OB.note_unspooled(tmp_path / "nope" / "\0bad")
