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

def test_write_voice_feedback_creates_artifact_and_inbox_line(tmp_path):
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
    # Rides the existing triage loop: a line was appended to inbox.log.
    inbox = cfg.data_dir / "bot-squad" / "feedback" / "inbox.log"
    assert inbox.exists()
    assert "хочу тёмную тему" in inbox.read_text()


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
