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
    initial_prompt: str | None = None,
    vad_filter: bool = False,
) -> dict[str, Any]:
    """Transcribe ``audio_path`` → ``{text, lang, engine}``.

    ``lang_hint`` (e.g. "ru"/"en") may be passed when known; ``None`` lets the
    backend auto-detect (ru vs en).

    ``initial_prompt`` (T-0747) is a DOMAIN GLOSSARY hint — a short text that
    biases decoding toward the vocabulary it contains. It is deliberately
    engine-agnostic: it maps onto faster-whisper's ``initial_prompt``, the
    OpenAI STT API's ``prompt``, Google's speech contexts / phrase hints, and
    Deepgram's keywords. ``vad_filter`` is a best-effort "drop non-speech
    first" hint.

    BACKEND CONTRACT: every backend is called with ALL of ``model`` /
    ``lang_hint`` / ``initial_prompt`` / ``vad_filter`` as keywords, and a
    backend that cannot express one is free to ignore it — so a cloud STT
    stays droppable in without changing this dispatcher or any caller.

    Both new options DEFAULT INERT: ``None`` / ``False`` reproduce the
    pre-T-0747 behaviour exactly (the faster-whisper backend does not even
    pass them down in that case — see ``_faster_whisper``).
    """
    backend = _BACKENDS.get(engine)
    if backend is None:
        raise TranscriptionError(
            f"unknown transcription engine: {engine!r} "
            f"(known: {sorted(_BACKENDS)})"
        )
    return backend(
        audio_path,
        model=model,
        lang_hint=lang_hint,
        initial_prompt=initial_prompt,
        vad_filter=vad_filter,
    )


def _faster_whisper(
    audio_path: Path,
    *,
    model: str,
    lang_hint: str | None,
    initial_prompt: str | None = None,
    vad_filter: bool = False,
) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:  # pragma: no cover - env-dependent
        raise TranscriptionError(
            "faster-whisper backend selected but the package is not installed — "
            "`pip install faster-whisper` in the worker venv (a deploy prerequisite "
            "for voice intake), or set [voice].engine to a configured alternative."
        ) from e
    # Only pass an option when it is actually SET. An unset option leaves the
    # call byte-identical to the pre-T-0747 one rather than re-asserting what we
    # believe faster-whisper's default to be — its defaults have moved between
    # releases (vad_filter flipped in 1.x), so re-asserting is how an "inert"
    # default silently stops being inert on an upgrade.
    opts: dict[str, Any] = {}
    if initial_prompt:
        opts["initial_prompt"] = initial_prompt
    if vad_filter:
        opts["vad_filter"] = True
    try:
        m = WhisperModel(model, device="cpu", compute_type="int8")
        segments, info = m.transcribe(str(audio_path), language=lang_hint, **opts)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:  # pragma: no cover - exercised live
        raise TranscriptionError(f"faster-whisper transcription failed: {e}") from e
    return {"text": text, "lang": getattr(info, "language", None), "engine": f"faster-whisper:{model}"}


# Engine name → backend. Extend here to add a cloud STT.
_BACKENDS: dict[str, Callable[..., dict[str, Any]]] = {
    "faster-whisper": _faster_whisper,
}
