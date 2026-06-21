"""Unit tests for bot_squad_worker.transcribe — the ru/en STT seam (T-0386 P2).

The seam dispatches to a configurable backend (default self-hosted
faster-whisper, config-swappable). Real transcription is verified live; here we
test the dispatch + error contract, not the model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import transcribe as T


def test_unknown_engine_raises():
    with pytest.raises(T.TranscriptionError, match="unknown transcription engine"):
        T.transcribe(Path("/tmp/x.oga"), engine="nope")


def test_dispatches_to_registered_backend(monkeypatch):
    seen = {}

    def _fake(audio_path, *, model, lang_hint):
        seen["audio"] = audio_path
        seen["model"] = model
        seen["lang_hint"] = lang_hint
        return {"text": "привет", "lang": "ru", "engine": "fake"}

    monkeypatch.setitem(T._BACKENDS, "fake", _fake)
    out = T.transcribe(Path("/tmp/a.oga"), engine="fake", model="small", lang_hint="ru")
    assert out == {"text": "привет", "lang": "ru", "engine": "fake"}
    assert seen == {"audio": Path("/tmp/a.oga"), "model": "small", "lang_hint": "ru"}


def test_faster_whisper_missing_dep_raises_clearly():
    # faster-whisper is NOT installed in the test env → a clear, actionable error
    # (not a bare ImportError), so a misconfigured deploy is diagnosable.
    with pytest.raises(T.TranscriptionError, match="faster-whisper"):
        T.transcribe(Path("/tmp/a.oga"), engine="faster-whisper", model="small")
