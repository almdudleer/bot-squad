"""Tests for bot_squad_worker.voice_intake — TG voice → feedback artifact (T-0386 P2)."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import voice_intake as VI


def _cfg(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "bot-squad" / "_worker").mkdir(parents=True)
    proj = types.SimpleNamespace(tg_chat="-100777", tg_topic_id=None)
    return types.SimpleNamespace(
        data_dir=data_dir,
        projects={"bot-squad": proj},
        tg_bot_token="TESTBOT:TOKEN",
        tg_proxy_url="",
    )


def _voice_msg(**over):
    msg = {
        "message_id": 300,
        "chat": {"id": -100777, "type": "supergroup"},
        "from": {"id": 30719523, "first_name": "Alexey", "username": "almdudleer"},
        "voice": {"file_id": "AwACfileid", "file_unique_id": "uniq1", "duration": 7,
                  "mime_type": "audio/ogg", "file_size": 4242},
    }
    msg.update(over)
    return msg


# --- extract_voice ---------------------------------------------------------

def test_extract_voice_none_when_no_voice():
    assert VI.extract_voice({"text": "hi"}) is None


def test_extract_voice_pulls_fields_and_author():
    v = VI.extract_voice(_voice_msg())
    assert v["file_id"] == "AwACfileid"
    assert v["duration"] == 7
    assert v["author"] == "Alexey (@almdudleer)"
    assert v["author_id"] == 30719523


def test_extract_voice_author_without_username():
    v = VI.extract_voice(_voice_msg(**{"from": {"id": 1, "first_name": "Bob"}}))
    assert v["author"] == "Bob"


# --- write_voice_feedback --------------------------------------------------

def test_write_voice_feedback_creates_artifact_only(tmp_path):
    """Audit item 8 (Fork-4): the F-*.md FeedbackFile is the single store. The
    voice writer no longer ALSO appends to feedback/inbox.log — that line-queue
    + its firehose constant team are cut, so a second write would just resurrect
    the dead store."""
    cfg = _cfg(tmp_path)
    path = VI.write_voice_feedback(
        cfg, "bot-squad",
        transcript="хочу тёмную тему",
        audio_ref="feedback/_audio/uniq1.oga",
        author="Alexey (@almdudleer)", author_id=30719523,
        duration=7, lang="ru", engine="faster-whisper:small",
        ts="2026-06-21T13:00:00Z",
    )
    assert path.exists()
    body = path.read_text()
    assert "source: voice" in body
    assert "channel: tg" in body
    assert "audio_ref: feedback/_audio/uniq1.oga" in body
    assert "lang: ru" in body
    assert "transcription_engine: faster-whisper:small" in body
    assert "хочу тёмную тему" in body
    # the cut store is NOT resurrected
    inbox = cfg.data_dir / "bot-squad" / "feedback" / "inbox.log"
    assert not inbox.exists()


def test_write_voice_feedback_emits_valid_provenance(tmp_path):
    """T-0521: the voice-intake artifact must carry a provenance value that the
    canonical grammar accepts (worker.provenance == scripts/lint/backlog_provenance).
    A voice note IS a dated stakeholder/user directive, so the valid token is
    ``stakeholder:<submitted-date>`` — NOT the free-form 'INI-04 voice intake'
    string the lint rejects (it surfaced as the T-0536/0537/0538 offenders when an
    intake session mirrored that shape into a backlog task)."""
    from bot_squad_worker import provenance as P, frontmatter as _fm

    cfg = _cfg(tmp_path)
    path = VI.write_voice_feedback(
        cfg, "bot-squad",
        transcript="hello",
        audio_ref="feedback/_audio/uniq1.oga",
        author="Alexey (@almdudleer)", author_id=30719523,
        duration=7, lang="en", engine="faster-whisper:small",
        ts="2026-06-21T13:00:00Z",
    )
    meta = _fm.parse_or_none(path.read_text())[0]
    prov = str(meta["provenance"])
    assert P.provenance_valid(prov), f"voice provenance {prov!r} rejected by the grammar"
    assert prov == "stakeholder:2026-06-21"


# --- download_voice --------------------------------------------------------

def test_download_voice_getfile_then_fetch_bytes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    dest = _cfg_audio(cfg) / "uniq1.oga"
    calls = []

    class _Resp:
        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content
        def raise_for_status(self): pass
        def json(self): return self._payload

    def fake_get(url, params=None, timeout=None, **kw):
        calls.append(url)
        if "getFile" in url:
            return _Resp(payload={"ok": True, "result": {"file_path": "voice/file_7.oga"}})
        return _Resp(content=b"OGGDATA")

    monkeypatch.setattr("httpx.get", fake_get)
    out = VI.download_voice(cfg, "AwACfileid", dest)
    assert out == dest
    assert dest.read_bytes() == b"OGGDATA"
    assert any("getFile" in u for u in calls)
    assert any("/file/bot" in u and "voice/file_7.oga" in u for u in calls)


def _cfg_audio(cfg):
    from bot_squad_worker import voice_intake as _VI
    d = _VI._audio_dir(cfg, "bot-squad")
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- transcribe_only (T-0569: DM voice → conversation, not feedback) -------

def test_transcribe_only_success_no_artifact_no_confirm(tmp_path, monkeypatch):
    """Success returns the transcript; NO F-*.md artifact and NO #feedback
    confirmation is written (that stays process_voice's job for group chats)."""
    cfg = _cfg(tmp_path)
    from bot_squad_worker import voice_intake as _VI, transcribe as _T, actions as A

    def fake_download(c, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"OGG"); return dest
    monkeypatch.setattr(_VI, "download_voice", fake_download)
    monkeypatch.setattr(_T, "transcribe", lambda p, **kw: {
        "text": "включи тёмную тему", "lang": "ru", "engine": "faster-whisper:small"})
    monkeypatch.setattr(A, "_get_tg_client",
                        lambda c: (_ for _ in ()).throw(AssertionError("must not confirm into #feedback")))

    out = _VI.transcribe_only(cfg, "bot-squad", _voice_msg())
    assert out == {
        "ok": True, "transcript": "включи тёмную тему", "lang": "ru",
        "engine": "faster-whisper:small", "duration": 7,
    }
    assert (_VI._audio_dir(cfg, "bot-squad") / "uniq1.oga").exists()
    assert not list((cfg.data_dir / "bot-squad" / "feedback").glob("F-*-voice-*.md"))


def test_transcribe_only_no_voice():
    from bot_squad_worker import voice_intake as _VI
    out = _VI.transcribe_only(object(), "bot-squad", {"text": "no voice here"})
    assert out == {"ok": False, "reason": "no_voice", "transcript": ""}


def test_transcribe_only_over_cap_before_download(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.voice_max_duration_sec = 60
    from bot_squad_worker import voice_intake as _VI, transcribe as _T

    def boom_download(c, file_id, dest):
        raise AssertionError("download_voice called for an over-cap note")
    monkeypatch.setattr(_VI, "download_voice", boom_download)
    def boom_transcribe(p, **kw):
        raise AssertionError("transcribe called for an over-cap note")
    monkeypatch.setattr(_T, "transcribe", boom_transcribe)

    out = _VI.transcribe_only(cfg, "bot-squad", _voice_msg(voice={
        "file_id": "big", "file_unique_id": "big", "duration": 600, "mime_type": "audio/ogg"}))
    assert out["ok"] is False and out["reason"] == "too_long" and out["duration"] == 600
    assert not (_VI._audio_dir(cfg, "bot-squad") / "big.oga").exists()


def test_transcribe_only_download_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    from bot_squad_worker import voice_intake as _VI

    def boom_download(c, file_id, dest):
        raise RuntimeError("proxy timeout")
    monkeypatch.setattr(_VI, "download_voice", boom_download)

    out = _VI.transcribe_only(cfg, "bot-squad", _voice_msg())
    assert out["ok"] is False and out["reason"] == "download_failed"
    assert out["transcript"] == ""


def test_transcribe_only_transcription_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    from bot_squad_worker import voice_intake as _VI, transcribe as _T

    def fake_download(c, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"OGG"); return dest
    monkeypatch.setattr(_VI, "download_voice", fake_download)
    def boom(p, **kw): raise _T.TranscriptionError("no model")
    monkeypatch.setattr(_T, "transcribe", boom)

    out = _VI.transcribe_only(cfg, "bot-squad", _voice_msg())
    assert out["ok"] is False and out["reason"] == "transcription_failed"
    assert out["transcript"] == ""
    # Audio IS still saved even though no artifact is written for it.
    assert (_VI._audio_dir(cfg, "bot-squad") / "uniq1.oga").exists()


def test_transcribe_only_timeout(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.voice_transcribe_timeout_sec = 0.3
    from bot_squad_worker import voice_intake as _VI, transcribe as _T
    import time as _time

    def fake_download(c, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"OGG"); return dest
    monkeypatch.setattr(_VI, "download_voice", fake_download)
    def slow_transcribe(p, **kw):
        _time.sleep(5)
        return {"text": "never returned in time"}
    monkeypatch.setattr(_T, "transcribe", slow_transcribe)

    started = _time.monotonic()
    out = _VI.transcribe_only(cfg, "bot-squad", _voice_msg())
    elapsed = _time.monotonic() - started
    assert elapsed < 3
    assert out["ok"] is False and out["reason"] == "transcription_timeout"


# --- process_voice (orchestration) -----------------------------------------

def test_process_voice_end_to_end(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    from bot_squad_worker import voice_intake as _VI, transcribe as _T, tg_topics, actions as A

    tg_topics.save(cfg, "bot-squad", {"feedback": 9001})

    # Stub the network bits: download writes a fake file, transcribe returns text.
    def fake_download(c, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"OGG")
        return dest
    monkeypatch.setattr(_VI, "download_voice", fake_download)
    monkeypatch.setattr(_T, "transcribe", lambda p, **kw: {"text": "сделай тёмную тему", "lang": "ru", "engine": "faster-whisper:small"})

    sent = []
    class _Tg:
        def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None):
            sent.append({"chat_id": chat_id, "text": text, "topic_id": topic_id}); return True
    monkeypatch.setattr(A, "_get_tg_client", lambda c: _Tg())

    out = _VI.process_voice(cfg, "bot-squad", _voice_msg(), ts="2026-06-21T13:00:00Z")
    assert out["ok"] is True
    # Audio blob stored.
    assert (_VI._audio_dir(cfg, "bot-squad") / "uniq1.oga").exists()
    # Artifact written with the transcript.
    arts = list((cfg.data_dir / "bot-squad" / "feedback").glob("F-*-voice-*.md"))
    assert len(arts) == 1 and "сделай тёмную тему" in arts[0].read_text()
    # Confirmation posted back into the #feedback topic.
    assert sent and sent[0]["topic_id"] == 9001


def test_process_voice_records_transcript_to_conversation_store(tmp_path, monkeypatch):
    """T-0526: a successful transcript is ALSO appended to the per-(slug,
    global_user_id) conversation store (not only the F-*.md artifact), reusing
    tg_listener's identity + append (best-effort)."""
    cfg = _cfg(tmp_path)
    from bot_squad_worker import (
        voice_intake as _VI, transcribe as _T, tg_topics, actions as A,
        tg_listener as _TL,
    )
    tg_topics.save(cfg, "bot-squad", {"feedback": 9001})

    def fake_download(c, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"OGG"); return dest
    monkeypatch.setattr(_VI, "download_voice", fake_download)
    monkeypatch.setattr(_T, "transcribe", lambda p, **kw: {"text": "dark mode please", "lang": "en", "engine": "faster-whisper:small"})
    monkeypatch.setattr(A, "_get_tg_client", lambda c: types.SimpleNamespace(send=lambda **kw: True))

    # Stub identity resolution + capture the conversation-store append.
    monkeypatch.setattr(_TL, "resolve_or_link_sender",
                        lambda c, m, s: {"global_user_id": "gu_voice", "created": False, "slug": s})
    captured = {}
    monkeypatch.setattr(_TL, "append_conversation",
                        lambda c, slug, gid, msg: captured.update(slug=slug, gid=gid, text=msg.get("text")) or True)

    out = _VI.process_voice(cfg, "bot-squad", _voice_msg(), ts="2026-06-21T13:00:00Z")
    assert out["ok"] is True
    # The transcript (not the empty voice 'text') landed in the conversation store.
    assert captured == {"slug": "bot-squad", "gid": "gu_voice", "text": "dark mode please"}


def test_process_voice_download_failure_confirms_resend(tmp_path, monkeypatch):
    """Hardening: a download failure (e.g. a flaky TG proxy on this DPI-blocked
    host) must NOT silently drop the note — confirm back into #feedback so the
    stakeholder knows to resend, instead of zero acknowledgement."""
    cfg = _cfg(tmp_path)
    from bot_squad_worker import voice_intake as _VI, tg_topics, actions as A

    tg_topics.save(cfg, "bot-squad", {"feedback": 9001})

    def boom_download(c, file_id, dest):
        raise RuntimeError("proxy timeout")
    monkeypatch.setattr(_VI, "download_voice", boom_download)

    sent = []
    monkeypatch.setattr(A, "_get_tg_client", lambda c: types.SimpleNamespace(
        send=lambda **k: sent.append(k) or True))

    out = _VI.process_voice(cfg, "bot-squad", _voice_msg(), ts="2026-06-21T13:00:00Z")
    assert out["ok"] is False and out["reason"] == "download_failed"
    # Not silent: a confirm went back into #feedback asking to resend.
    assert sent and sent[0]["topic_id"] == 9001
    assert "resend" in sent[0]["text"].lower()


def test_process_voice_rejects_over_cap_before_download(tmp_path, monkeypatch):
    """T-0433 P2: a voice note longer than voice_max_duration_sec is rejected
    BEFORE any download/transcribe (TG gives voice.duration without a fetch), so a
    huge note never hits the proxy/disk/poll-loop. The stakeholder is confirmed
    ('too long, split') and nothing is transcribed or written."""
    cfg = _cfg(tmp_path)
    cfg.voice_max_duration_sec = 60
    from bot_squad_worker import voice_intake as _VI, transcribe as _T, tg_topics, actions as A

    tg_topics.save(cfg, "bot-squad", {"feedback": 9001})

    # download/transcribe must NOT be reached.
    def boom_download(c, file_id, dest):
        raise AssertionError("download_voice called for an over-cap note")
    monkeypatch.setattr(_VI, "download_voice", boom_download)
    def boom_transcribe(p, **kw):
        raise AssertionError("transcribe called for an over-cap note")
    monkeypatch.setattr(_T, "transcribe", boom_transcribe)

    sent = []
    monkeypatch.setattr(A, "_get_tg_client", lambda c: types.SimpleNamespace(
        send=lambda **k: sent.append(k) or True))

    out = _VI.process_voice(cfg, "bot-squad", _voice_msg(voice={
        "file_id": "big", "file_unique_id": "big", "duration": 600, "mime_type": "audio/ogg"}),
        ts="2026-06-21T13:00:00Z")

    assert out["ok"] is False and out["reason"] == "too_long"
    # Confirmed back into #feedback, mentioning the cap, asking to split.
    assert sent and sent[0]["topic_id"] == 9001
    assert "too long" in sent[0]["text"].lower() and "split" in sent[0]["text"].lower()
    # Nothing written: no audio blob, no artifact.
    assert not (_VI._audio_dir(cfg, "bot-squad") / "big.oga").exists()
    assert not list((cfg.data_dir / "bot-squad" / "feedback").glob("F-*-voice-*.md"))


def test_process_voice_transcribe_timeout_unblocks_and_flags(tmp_path, monkeypatch):
    """T-0433 P2: process_voice runs inside the tg_listener getUpdates poll loop,
    so a runaway transcription must NOT block it indefinitely. A decode exceeding
    voice_transcribe_timeout_sec is abandoned (the note is still saved + flagged,
    like any transcribe failure) and process_voice RETURNS promptly instead of
    waiting for the slow decode."""
    cfg = _cfg(tmp_path)
    cfg.voice_transcribe_timeout_sec = 0.3
    from bot_squad_worker import voice_intake as _VI, transcribe as _T, actions as A
    import time as _time

    def fake_download(c, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"OGG"); return dest
    monkeypatch.setattr(_VI, "download_voice", fake_download)

    def slow_transcribe(p, **kw):
        _time.sleep(5)  # far longer than the 0.3s timeout
        return {"text": "never returned in time", "lang": "ru"}
    monkeypatch.setattr(_T, "transcribe", slow_transcribe)
    monkeypatch.setattr(A, "_get_tg_client", lambda c: types.SimpleNamespace(send=lambda **k: True))

    started = _time.monotonic()
    out = _VI.process_voice(cfg, "bot-squad", _voice_msg(), ts="2026-06-21T13:00:00Z")
    elapsed = _time.monotonic() - started

    assert elapsed < 3, f"process_voice blocked {elapsed:.1f}s on a slow decode (timeout not enforced)"
    assert out["ok"] is False and out["reason"] in ("transcription_timeout", "transcription_failed")
    # The note is not lost: audio stored + an artifact written.
    assert (_VI._audio_dir(cfg, "bot-squad") / "uniq1.oga").exists()
    assert len(list((cfg.data_dir / "bot-squad" / "feedback").glob("F-*-voice-*.md"))) == 1


# --- voice_ready / readiness (P1) ------------------------------------------

def test_voice_ready_false_when_faster_whisper_missing(tmp_path, monkeypatch):
    """T-0433 P1: voice_ready reports NOT-ready when the faster-whisper backend
    isn't installed in the worker venv (the deploy-provisioning gap), so the
    worker can warn at boot instead of silently no-opping at the first note."""
    from bot_squad_worker import voice_intake as _VI
    import importlib.util
    cfg = _cfg(tmp_path)
    cfg.voice_engine = "faster-whisper"
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    res = _VI.voice_ready(cfg)
    assert res["ready"] is False
    assert "provision" in res["reason"].lower() or "faster-whisper" in res["reason"].lower()


def test_voice_ready_true_when_backend_present(tmp_path, monkeypatch):
    from bot_squad_worker import voice_intake as _VI
    import importlib.util
    cfg = _cfg(tmp_path)
    cfg.voice_engine = "faster-whisper"
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert _VI.voice_ready(cfg)["ready"] is True


def test_log_voice_readiness_noop_when_disabled(tmp_path):
    from bot_squad_worker import voice_intake as _VI
    cfg = _cfg(tmp_path)
    cfg.voice_enabled = False
    res = _VI.log_voice_readiness(cfg)
    assert res["checked"] is False


def test_log_voice_readiness_warns_when_enabled_unready(tmp_path, monkeypatch, caplog):
    """Enabled + backend missing → a LOUD warning at startup (fail loud, not silent)."""
    import logging, importlib.util
    from bot_squad_worker import voice_intake as _VI
    cfg = _cfg(tmp_path)
    cfg.voice_enabled = True
    cfg.voice_engine = "faster-whisper"
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with caplog.at_level(logging.WARNING):
        res = _VI.log_voice_readiness(cfg)
    assert res["checked"] is True and res["ready"] is False
    assert any("ENABLED" in r.message and "READY" in r.message.upper() for r in caplog.records)


# --- gc_audio (P3 retention) -----------------------------------------------

def _audio_blob(cfg, name: str, *, age_days: float = 0.0):
    import os, time
    d = _cfg_audio(cfg)
    p = d / name
    p.write_bytes(b"OGG")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


def _voice_artifact(cfg, *, uid: str, status: str | None):
    """Write an F-*-voice-*.md referencing the blob, with optional close-state."""
    fb = _cfg(cfg).data_dir if False else None  # noqa (keep linter calm)
    from bot_squad_worker import voice_intake as _VI
    fbd = _VI._feedback_dir(cfg, "bot-squad"); fbd.mkdir(parents=True, exist_ok=True)
    st = f"status: {status}\n" if status else ""
    (fbd / f"F-2026-06-21-voice-{uid}.md").write_text(
        f"---\nsource: voice\naudio_ref: feedback/_audio/{uid}.oga\n{st}---\n\n# voice\n\nbody\n"
    )


def test_gc_audio_reaps_triaged_keeps_open(tmp_path):
    """T-0433 P3: a blob whose F-*.md is promoted/dismissed (triaged) is reaped
    PROMPTLY; an un-triaged (open) note's audio SURVIVES regardless of age until
    it's actually handled (close-state is the primary trigger, audit-item-12)."""
    from bot_squad_worker import voice_intake as _VI
    cfg = _cfg(tmp_path)
    cfg.voice_audio_retention_days = 30
    _audio_blob(cfg, "prom.oga"); _voice_artifact(cfg, uid="prom", status="promoted")
    _audio_blob(cfg, "dism.oga"); _voice_artifact(cfg, uid="dism", status="dismissed")
    _audio_blob(cfg, "open.oga", age_days=99); _voice_artifact(cfg, uid="open", status=None)

    res = _VI.gc_audio(cfg, "bot-squad")

    d = _VI._audio_dir(cfg, "bot-squad")
    assert not (d / "prom.oga").exists(), "promoted note's audio should be reaped"
    assert not (d / "dism.oga").exists(), "dismissed note's audio should be reaped"
    assert (d / "open.oga").exists(), "an OPEN note's audio must survive (even old) until triaged"
    assert res["removed"] == 2


def test_gc_audio_age_backstop_reaps_old_orphan(tmp_path):
    """Backstop: a blob older than retention_days with NO (or still-open) artifact
    is reaped so _audio can't grow unbounded from abandoned/orphaned notes."""
    from bot_squad_worker import voice_intake as _VI
    cfg = _cfg(tmp_path)
    cfg.voice_audio_retention_days = 30
    _audio_blob(cfg, "orphan-old.oga", age_days=45)   # no artifact at all
    _audio_blob(cfg, "orphan-new.oga", age_days=2)    # young orphan → keep

    res = _VI.gc_audio(cfg, "bot-squad")

    d = _VI._audio_dir(cfg, "bot-squad")
    assert not (d / "orphan-old.oga").exists(), "aged orphan should be reaped (backstop)"
    assert (d / "orphan-new.oga").exists(), "young orphan should survive"
    assert res["removed"] == 1


def test_gc_audio_zero_retention_keeps_open_orphans(tmp_path):
    """retention_days=0 disables the age backstop — only triaged notes are reaped."""
    from bot_squad_worker import voice_intake as _VI
    cfg = _cfg(tmp_path)
    cfg.voice_audio_retention_days = 0
    _audio_blob(cfg, "ancient.oga", age_days=999)  # orphan, no artifact
    res = _VI.gc_audio(cfg, "bot-squad")
    assert (_VI._audio_dir(cfg, "bot-squad") / "ancient.oga").exists()
    assert res["removed"] == 0


def test_process_voice_transcription_failure_still_stores_audio(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    from bot_squad_worker import voice_intake as _VI, transcribe as _T, actions as A

    def fake_download(c, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"OGG"); return dest
    monkeypatch.setattr(_VI, "download_voice", fake_download)
    def boom(p, **kw): raise _T.TranscriptionError("no model")
    monkeypatch.setattr(_T, "transcribe", boom)
    monkeypatch.setattr(A, "_get_tg_client", lambda c: types.SimpleNamespace(send=lambda **k: True))

    out = _VI.process_voice(cfg, "bot-squad", _voice_msg(), ts="2026-06-21T13:00:00Z")
    assert out["ok"] is False and out["reason"] == "transcription_failed"
    # Audio is NOT lost.
    assert (_VI._audio_dir(cfg, "bot-squad") / "uniq1.oga").exists()
    # An artifact is still written so the note isn't silently dropped.
    arts = list((cfg.data_dir / "bot-squad" / "feedback").glob("F-*-voice-*.md"))
    assert len(arts) == 1
