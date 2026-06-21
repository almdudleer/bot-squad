"""ru/en speech-to-text seam (T-0386 / INI-04 Phase 2).

A thin dispatcher over pluggable transcription backends. The default is
self-hosted ``faster-whisper`` (multilingual — ru+en native, no per-minute
cloud cost, no extra egress dependency on a DPI-restricted host). The engine is
config-swappable (``[voice].engine`` in system_settings.toml) so a cloud STT
can be dropped in later without touching callers.

``faster_whisper`` is imported lazily inside the backend — the worker only needs
it installed when voice intake is actually used + configured, and a missing dep
surfaces as an actionable ``TranscriptionError`` rather than an import crash.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


class TranscriptionError(Exception):
    """Raised on an unknown engine, a missing backend dependency, or STT failure."""


def transcribe(
    audio_path: Path,
    *,
    engine: str = "faster-whisper",
    model: str = "small",
    lang_hint: str | None = None,
) -> dict[str, Any]:
    """Transcribe ``audio_path`` → ``{text, lang, engine}``.

    ``lang_hint`` (e.g. "ru"/"en") may be passed when known; ``None`` lets the
    backend auto-detect (ru vs en).
    """
    backend = _BACKENDS.get(engine)
    if backend is None:
        raise TranscriptionError(
            f"unknown transcription engine: {engine!r} "
            f"(known: {sorted(_BACKENDS)})"
        )
    return backend(audio_path, model=model, lang_hint=lang_hint)


def _faster_whisper(audio_path: Path, *, model: str, lang_hint: str | None) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:  # pragma: no cover - env-dependent
        raise TranscriptionError(
            "faster-whisper backend selected but the package is not installed — "
            "`pip install faster-whisper` in the worker venv (a deploy prerequisite "
            "for voice intake), or set [voice].engine to a configured alternative."
        ) from e
    try:
        m = WhisperModel(model, device="cpu", compute_type="int8")
        segments, info = m.transcribe(str(audio_path), language=lang_hint)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:  # pragma: no cover - exercised live
        raise TranscriptionError(f"faster-whisper transcription failed: {e}") from e
    return {"text": text, "lang": getattr(info, "language", None), "engine": f"faster-whisper:{model}"}


# Engine name → backend. Extend here to add a cloud STT.
_BACKENDS: dict[str, Callable[..., dict[str, Any]]] = {
    "faster-whisper": _faster_whisper,
}
