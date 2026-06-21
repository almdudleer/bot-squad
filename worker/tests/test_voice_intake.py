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
