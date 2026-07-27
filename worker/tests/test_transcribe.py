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


def _register_recording_backend(monkeypatch) -> dict:
    """A stand-in for a droppable-in cloud STT: it accepts the WHOLE backend
    contract as keywords and records what the dispatcher handed it."""
    seen: dict = {}

    def _fake(audio_path, *, model, lang_hint, initial_prompt=None, vad_filter=False):
        seen.update(audio=audio_path, model=model, lang_hint=lang_hint,
                    initial_prompt=initial_prompt, vad_filter=vad_filter)
        return {"text": "привет", "lang": "ru", "engine": "fake"}

    monkeypatch.setitem(T._BACKENDS, "fake", _fake)
    return seen


def test_dispatches_to_registered_backend(monkeypatch):
    seen = _register_recording_backend(monkeypatch)
    out = T.transcribe(Path("/tmp/a.oga"), engine="fake", model="small", lang_hint="ru")
    assert out == {"text": "привет", "lang": "ru", "engine": "fake"}
    assert seen == {"audio": Path("/tmp/a.oga"), "model": "small", "lang_hint": "ru",
                    # T-0747 DoD 3: a caller that says nothing gets the inert values.
                    "initial_prompt": None, "vad_filter": False}


def test_forwards_initial_prompt_and_vad_to_any_backend(monkeypatch):
    """T-0747 DoD 1: the seam stays engine-agnostic — the glossary reaches a
    NON-whisper backend too (an STT API's `prompt`/phrase-hints), so a cloud
    engine is still droppable in without touching the dispatcher."""
    seen = _register_recording_backend(monkeypatch)
    T.transcribe(Path("/tmp/a.oga"), engine="fake", model="small",
                 initial_prompt="bot-squad, tmux, sudo", vad_filter=True)
    assert seen["initial_prompt"] == "bot-squad, tmux, sudo"
    assert seen["vad_filter"] is True


def _stub_faster_whisper(monkeypatch) -> dict:
    """Install a fake ``faster_whisper`` module and record the kwargs the
    backend hands to ``WhisperModel.transcribe`` (the package is not installed
    in the test env — the REAL decode is covered by test_transcribe_realmodel
    and by the live re-measure on T-0747)."""
    import sys
    import types as _types

    calls: dict = {}

    class _Seg:
        text = " привет "

    class _Info:
        language = "ru"

    class _Model:
        def __init__(self, model, device=None, compute_type=None):
            calls["model"] = model

        def transcribe(self, path, **kw):
            calls["kwargs"] = kw
            return iter([_Seg()]), _Info()

    mod = _types.ModuleType("faster_whisper")
    mod.WhisperModel = _Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


def test_faster_whisper_omits_unset_options_entirely(monkeypatch):
    """T-0747 DoD 3 — INERT BY CONSTRUCTION. With no glossary and no VAD the
    backend does not pass those kwargs down AT ALL, so the decode call is the
    byte-identical pre-T-0747 one. Re-asserting a believed default would
    silently stop being inert when faster-whisper moves it (vad_filter did)."""
    calls = _stub_faster_whisper(monkeypatch)
    out = T.transcribe(Path("/tmp/a.oga"), engine="faster-whisper", model="small")
    assert calls["kwargs"] == {"language": None}
    assert "initial_prompt" not in calls["kwargs"]
    assert "vad_filter" not in calls["kwargs"]
    assert out["text"] == "привет"


def test_faster_whisper_empty_prompt_is_omitted(monkeypatch):
    """An empty/whitespace [voice].initial_prompt is the same as absent —
    an operator blanking the key must restore today's behaviour exactly."""
    calls = _stub_faster_whisper(monkeypatch)
    T.transcribe(Path("/tmp/a.oga"), engine="faster-whisper", model="small",
                 initial_prompt="", vad_filter=False)
    assert calls["kwargs"] == {"language": None}


def test_faster_whisper_passes_set_options(monkeypatch):
    """T-0747 DoD 1: a configured glossary + VAD actually reach the model."""
    calls = _stub_faster_whisper(monkeypatch)
    T.transcribe(Path("/tmp/a.oga"), engine="faster-whisper", model="small",
                 lang_hint="ru", initial_prompt="bot-squad, tmux", vad_filter=True)
    assert calls["kwargs"] == {"language": "ru",
                               "initial_prompt": "bot-squad, tmux",
                               "vad_filter": True}


def test_faster_whisper_missing_dep_raises_clearly():
    # faster-whisper is NOT installed in the test env → a clear, actionable error
    # (not a bare ImportError), so a misconfigured deploy is diagnosable.
    with pytest.raises(T.TranscriptionError, match="faster-whisper"):
        T.transcribe(Path("/tmp/a.oga"), engine="faster-whisper", model="small")
