"""TG voice-message intake → feedback artifact (T-0386 / INI-04 Phase 2).

A voice note in a project's #feedback topic is downloaded, transcribed (ru/en
via the transcribe seam), and stored as a first-class feedback artifact: the
audio blob beside an enriched ``F-*.md`` whose body is the transcript. That
``F-*.md`` is the single feedback store — the operator triages it via the
promote/dismiss path (audit item 8, Fork-4, cut the inbox.log line-queue +
its user-feedback firehose).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def extract_voice(message: dict) -> dict[str, Any] | None:
    """Return the voice metadata for a voice message, or None if not a voice msg."""
    voice = message.get("voice")
    if not voice:
        return None
    frm = message.get("from") or {}
    name = (frm.get("first_name") or "").strip()
    username = frm.get("username")
    if username:
        author = f"{name} (@{username})".strip()
    else:
        author = name or "unknown"
    return {
        "file_id": voice.get("file_id"),
        "file_unique_id": voice.get("file_unique_id") or voice.get("file_id"),
        "duration": int(voice.get("duration") or 0),
        "mime_type": voice.get("mime_type") or "audio/ogg",
        "author": author,
        "author_id": frm.get("id"),
        "thread_id": message.get("message_thread_id"),
    }


def _audio_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "feedback" / "_audio"


def _feedback_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "feedback"


def write_voice_feedback(
    cfg: Any,
    slug: str,
    *,
    transcript: str,
    audio_ref: str,
    author: str,
    author_id: Any,
    duration: int,
    lang: str | None,
    engine: str,
    ts: str,
) -> Path:
    """Write the voice feedback artifact (F-*.md) + append to inbox.log.

    Returns the artifact path. The frontmatter carries the audio pointer,
    transcript metadata and provenance; the body is the transcript.
    """
    fb_dir = _feedback_dir(cfg, slug)
    fb_dir.mkdir(parents=True, exist_ok=True)

    date = ts[:10]
    digest = hashlib.sha256(f"{audio_ref}\x00{transcript}".encode()).hexdigest()[:10]
    artifact = fb_dir / f"F-{date}-voice-{digest}.md"

    lang_str = lang or "auto"
    fm = (
        "---\n"
        "source: voice\n"
        "channel: tg\n"
        f'author: "{author}"\n'
        f"author_id: {author_id}\n"
        f"submitted_at: {ts}\n"
        f"audio_ref: {audio_ref}\n"
        f"audio_duration_sec: {duration}\n"
        f"lang: {lang_str}\n"
        f"transcription_engine: {engine}\n"
        'provenance: "INI-04 voice intake"\n'
        "---\n\n"
        f"# Voice note from {author} ({duration}s, {lang_str})\n\n"
        f"{transcript}\n"
    )
    artifact.write_text(fm)

    log.info("voice_intake: wrote %s (%d chars)", artifact.name, len(transcript))
    return artifact


def _proxy_kwargs(cfg: Any) -> dict:
    """Inline mirror of tg_listener._proxy_kwargs (avoids a circular import)."""
    proxy = getattr(cfg, "tg_proxy_url", "") or ""
    return {"proxy": proxy} if proxy else {}


def download_voice(cfg: Any, file_id: str, dest_path: Path) -> Path:
    """Download a TG voice file by id → ``dest_path`` (getFile then /file fetch).

    Proxy-aware (this host DPI-blocks api.telegram.org). Raises on any HTTP error.
    """
    import httpx

    token = cfg.tg_bot_token
    extra = _proxy_kwargs(cfg)
    r = httpx.get(
        f"https://api.telegram.org/bot{token}/getFile",
        params={"file_id": file_id}, timeout=15, **extra,
    )
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    r2 = httpx.get(
        f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30, **extra,
    )
    r2.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(r2.content)
    return dest_path


def process_voice(cfg: Any, slug: str, message: dict, *, ts: str) -> dict[str, Any]:
    """Full intake: download → transcribe → artifact → confirm into #feedback.

    A transcription failure does NOT lose the note: the audio is still stored and
    an artifact is written with a failure marker, flagged for triage.
    """
    from bot_squad_worker import transcribe as _transcribe

    v = extract_voice(message)
    if not v:
        return {"ok": True, "action": "skip", "reason": "no voice"}

    dest = _audio_dir(cfg, slug) / f"{v['file_unique_id']}.oga"
    try:
        download_voice(cfg, v["file_id"], dest)
    except Exception as e:  # noqa: BLE001
        log.exception("voice_intake: download failed for %s", v["file_id"])
        return {"ok": False, "reason": "download_failed", "error": str(e)}

    audio_ref = f"feedback/_audio/{v['file_unique_id']}.oga"
    engine = getattr(cfg, "voice_engine", "faster-whisper")
    model = getattr(cfg, "voice_model", "small")

    failed = False
    try:
        res = _transcribe.transcribe(dest, engine=engine, model=model, lang_hint=None)
        transcript = (res.get("text") or "").strip()
        lang = res.get("lang")
        used_engine = res.get("engine") or engine
    except Exception as e:  # noqa: BLE001
        log.exception("voice_intake: transcription failed for %s", audio_ref)
        failed = True
        transcript = f"[transcription failed: {e}]"
        lang = None
        used_engine = engine

    artifact = write_voice_feedback(
        cfg, slug,
        transcript=transcript, audio_ref=audio_ref,
        author=v["author"], author_id=v["author_id"], duration=v["duration"],
        lang=lang, engine=used_engine, ts=ts,
    )
    _confirm(cfg, slug, v, transcript, failed)

    if failed:
        return {"ok": False, "reason": "transcription_failed", "artifact": str(artifact)}
    return {"ok": True, "artifact": str(artifact), "lang": lang}


def _confirm(cfg: Any, slug: str, v: dict, transcript: str, failed: bool) -> None:
    """Post a confirmation back into the project's #feedback topic (best-effort)."""
    try:
        from bot_squad_worker import actions as A, tg_topics
        project = cfg.projects.get(slug)
        chat_id = getattr(project, "tg_chat", "") if project else ""
        if not chat_id:
            return
        topic = tg_topics.resolve(cfg, slug, "feedback")
        if failed:
            text = (f"⚠️ got your voice note ({v['duration']}s) but transcription "
                    f"failed — audio saved & flagged for triage.")
        else:
            snippet = transcript[:140] + ("…" if len(transcript) > 140 else "")
            text = f"✅ got your voice note ({v['duration']}s): {snippet}"
        A._get_tg_client(cfg).send(chat_id=chat_id, text=text, sid="voice_intake", topic_id=topic)
    except Exception:  # noqa: BLE001
        log.exception("voice_intake: confirmation send failed")
