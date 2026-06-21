"""T-0433 P1 — REAL faster-whisper decode test (the transcription live-verify).

Skipped in CI / any env without the [voice] extra (``pytest.importorskip``); runs
only where ``scripts/install/provision-voice.sh`` has installed faster-whisper +
the model. It proves the REAL backend loads + decodes a real audio file end to
end (not the stubbed seam), so a provisioned env fails LOUDLY here instead of
silently at the first voice note. Uses the 'tiny' model for speed; a faint-tone
fixture transcribes to (near-)empty text, so we assert the result SHAPE, not the
words.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("faster_whisper", reason="[voice] extra not installed (run provision-voice.sh)")

from bot_squad_worker import transcribe as T  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "voice_sample.wav"


def test_real_faster_whisper_decodes_fixture():
    assert _FIXTURE.exists(), "voice_sample.wav fixture missing"
    res = T.transcribe(_FIXTURE, engine="faster-whisper", model="tiny", lang_hint=None)
    # Real backend ran end to end: a result dict with the contract keys.
    assert isinstance(res, dict)
    assert "text" in res and isinstance(res["text"], str)
    assert str(res.get("engine", "")).startswith("faster-whisper")
    # 'lang' is detected by the model (present, may be any language for a tone).
    assert "lang" in res
