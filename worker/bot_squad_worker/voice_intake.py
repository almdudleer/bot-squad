"""TG voice-message intake → feedback artifact (T-0386 / INI-04 Phase 2).

A voice note in a project's #feedback topic is downloaded, transcribed (ru/en
via the transcribe seam), and stored as a first-class feedback artifact: the
audio blob beside an enriched ``F-*.md`` whose body is the transcript. That
``F-*.md`` is the single feedback store — the operator triages it via the
promote/dismiss path (audit item 8, Fork-4, cut the inbox.log line-queue +
its user-feedback firehose).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# T-0433 P2: fallback caps when cfg doesn't carry them (older config). The
# tg_listener runs process_voice synchronously in its getUpdates poll loop, so an
# over-long note is rejected pre-download and a runaway decode is time-boxed.
_DEFAULT_MAX_DURATION_SEC = 300
_DEFAULT_TRANSCRIBE_TIMEOUT_SEC = 120
# T-0611: the TG Bot API refuses getFile for files over 20MB — a hard platform
# limit, not ours (the 2026-07-05 25-min note died on it AFTER passing the
# duration gate). Reject on the update's file_size BEFORE download with an
# honest reason; small safety margin under the exact 20*1024*1024.
_BOT_API_FILE_CAP_BYTES = 20_000_000
# T-0433 P3: how long an un-triaged voice blob survives in feedback/_audio/ before
# the age backstop reaps it. A triaged (promoted/dismissed) note's audio is reaped
# immediately regardless. 0 disables the age backstop (triage-only GC).
_DEFAULT_AUDIO_RETENTION_DAYS = 30


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
        "file_size": int(voice.get("file_size") or 0),
        "mime_type": voice.get("mime_type") or "audio/ogg",
        "author": author,
        "author_id": frm.get("id"),
        "thread_id": message.get("message_thread_id"),
    }


def origin_of(message: dict) -> dict[str, Any]:
    """Where a voice note actually ARRIVED — the destination its answer belongs
    in (T-0725).

    ``{"chat_id": str, "thread_id": int|None, "message_id": int|None}``, read
    straight off the inbound update. Before this the confirmation resolved its
    destination from the project's STATIC ``tg_chat`` plus the fixed
    ``#feedback`` topic, so a note sent in any other chat/topic had its
    transcript posted somewhere else entirely — and never as a reply. The three
    fields were always on the message; they were simply not read.

    Deliberately NOT a routing *decision*: there is nothing to resolve or rank
    here (contrast T-0723's sender-identity precedence ladder in
    ``actions._action_tg_notify``, which picks a destination for a send that has
    no inbound message at all). An answer to an inbound message goes where that
    message is, full stop.
    """
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    return {
        "chat_id": "" if chat_id is None else str(chat_id),
        "thread_id": message.get("message_thread_id"),
        "message_id": message.get("message_id"),
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
        # T-0521: a voice note is a dated stakeholder/user directive, so provenance
        # is a VALID grammar token (worker.provenance / backlog_provenance lint) —
        # ``stakeholder:<submitted-date>``. The free-form "INI-04 voice intake"
        # string the lint rejects was the shape that leaked into the T-0536/0537/0538
        # backlog offenders; the user/author ref already lives in author/author_id.
        f"provenance: stakeholder:{date}\n"
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


def _transcribe_with_timeout(dest: Path, *, engine: str, model: str, timeout_sec: float) -> dict[str, Any]:
    """Run the (synchronous, CPU-bound) transcribe seam with a wall-clock ceiling.

    T-0433 P2: process_voice runs inside the tg_listener getUpdates poll loop, so
    a runaway decode would stall inbound TG for the whole install. We bound the
    CALLER: submit to a 1-worker pool and ``result(timeout)``; on timeout we stop
    waiting (``shutdown(wait=False)`` — the abandoned decode thread lingers but
    never blocks the poll loop) and raise TimeoutError, which the caller treats as
    a transcription failure (the audio is still saved + flagged). ``timeout_sec``
    <= 0 disables the ceiling. True off-loop transcription is a follow-up.
    """
    from bot_squad_worker import transcribe as _transcribe

    if timeout_sec and timeout_sec > 0:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_transcribe.transcribe, dest, engine=engine, model=model, lang_hint=None)
        try:
            return fut.result(timeout=timeout_sec)
        finally:
            ex.shutdown(wait=False)
    return _transcribe.transcribe(dest, engine=engine, model=model, lang_hint=None)


def transcribe_only(cfg: Any, slug: str, message: dict) -> dict[str, Any]:
    """Download + transcribe a voice message WITHOUT writing a feedback
    artifact or posting a #feedback confirmation (T-0569).

    A DM voice note routes through the SAME path as typed text (the
    conversation store + ``ensure_user_conversation``, voice-04 continuity) —
    it must NOT become a feedback artifact, so it can't reuse ``process_voice``
    wholesale. This sibling shares the same primitives (``extract_voice``,
    ``download_voice``, ``_transcribe_with_timeout``) and the same caps
    (``voice_max_duration_sec`` / ``voice_transcribe_timeout_sec``) as
    ``process_voice`` — the GROUP/topic path stays on ``process_voice``,
    unchanged.

    Returns ``{"ok": bool, "transcript": str, "lang": str|None, "engine": str,
    "duration": int, "reason": str|None}``. On failure ``ok`` is ``False`` and
    ``reason`` is one of ``no_voice`` / ``too_long`` / ``download_failed`` /
    ``transcription_timeout`` / ``transcription_failed`` (``transcript`` is
    empty in that case; ``error`` may carry the exception text).
    """
    v = extract_voice(message)
    if not v:
        return {"ok": False, "reason": "no_voice", "transcript": ""}

    max_dur = int(getattr(cfg, "voice_max_duration_sec", _DEFAULT_MAX_DURATION_SEC) or 0)
    if max_dur > 0 and v["duration"] > max_dur:
        log.info("voice_intake: transcribe_only rejecting over-cap note (%ds > %ds) from %s",
                  v["duration"], max_dur, v["author"])
        return {"ok": False, "reason": "too_long", "transcript": "", "duration": v["duration"]}

    if v["file_size"] > _BOT_API_FILE_CAP_BYTES:
        log.info("voice_intake: transcribe_only rejecting too-big note (%d bytes > %d, %ds) from %s"
                 " — Bot API getFile cap", v["file_size"], _BOT_API_FILE_CAP_BYTES,
                 v["duration"], v["author"])
        return {"ok": False, "reason": "too_big", "transcript": "",
                "duration": v["duration"], "file_size": v["file_size"]}

    dest = _audio_dir(cfg, slug) / f"{v['file_unique_id']}.oga"
    try:
        download_voice(cfg, v["file_id"], dest)
    except Exception as e:  # noqa: BLE001
        log.exception("voice_intake: transcribe_only download failed for %s", v["file_id"])
        return {"ok": False, "reason": "download_failed", "transcript": "",
                 "duration": v["duration"], "error": str(e)}

    engine = getattr(cfg, "voice_engine", "faster-whisper")
    model = getattr(cfg, "voice_model", "small")
    timeout_sec = float(getattr(cfg, "voice_transcribe_timeout_sec", _DEFAULT_TRANSCRIBE_TIMEOUT_SEC) or 0)
    try:
        res = _transcribe_with_timeout(dest, engine=engine, model=model, timeout_sec=timeout_sec)
    except concurrent.futures.TimeoutError:
        log.warning("voice_intake: transcribe_only timed out (>%ss) for %s", timeout_sec, dest)
        return {"ok": False, "reason": "transcription_timeout", "transcript": "", "duration": v["duration"]}
    except Exception as e:  # noqa: BLE001
        log.exception("voice_intake: transcribe_only transcription failed for %s", dest)
        return {"ok": False, "reason": "transcription_failed", "transcript": "",
                 "duration": v["duration"], "error": str(e)}

    transcript = (res.get("text") or "").strip()
    return {
        "ok": True,
        "transcript": transcript,
        "lang": res.get("lang"),
        "engine": res.get("engine") or engine,
        "duration": v["duration"],
    }


def process_voice(cfg: Any, slug: str, message: dict, *, ts: str) -> dict[str, Any]:
    """Full intake: download → transcribe → artifact → confirm into #feedback.

    A transcription failure does NOT lose the note: the audio is still stored and
    an artifact is written with a failure marker, flagged for triage.

    T-0433 P2: runs synchronously in the tg_listener poll loop, so an over-cap note
    is rejected BEFORE download (cheap pre-fetch duration check) and a runaway
    decode is time-boxed — neither blocks inbound TG.
    """
    v = extract_voice(message)
    if not v:
        return {"ok": True, "action": "skip", "reason": "no voice"}

    # T-0725: resolved ONCE here, off the inbound update, and handed to every
    # _confirm below — so no acknowledgement (success or failure) can re-derive
    # or guess its destination.
    origin = origin_of(message)

    # Cap BEFORE download/transcribe: TG carries voice.duration without a fetch, so
    # a huge note never hits the proxy/disk/decode. Confirm + bail (no artifact —
    # nothing was transcribed). 0 = no cap.
    max_dur = int(getattr(cfg, "voice_max_duration_sec", _DEFAULT_MAX_DURATION_SEC) or 0)
    if max_dur > 0 and v["duration"] > max_dur:
        log.info("voice_intake: rejecting over-cap note (%ds > %ds) from %s",
                 v["duration"], max_dur, v["author"])
        _confirm(cfg, slug, v, outcome="too_long", origin=origin)
        return {"ok": False, "reason": "too_long", "duration": v["duration"]}

    if v["file_size"] > _BOT_API_FILE_CAP_BYTES:
        log.info("voice_intake: rejecting too-big note (%d bytes > %d, %ds) from %s"
                 " — Bot API getFile cap", v["file_size"], _BOT_API_FILE_CAP_BYTES,
                 v["duration"], v["author"])
        _confirm(cfg, slug, v, outcome="too_big", origin=origin)
        return {"ok": False, "reason": "too_big", "duration": v["duration"],
                "file_size": v["file_size"]}

    dest = _audio_dir(cfg, slug) / f"{v['file_unique_id']}.oga"
    try:
        download_voice(cfg, v["file_id"], dest)
    except Exception as e:  # noqa: BLE001
        log.exception("voice_intake: download failed for %s", v["file_id"])
        # Don't silently drop the note — on this DPI host the TG file fetch can
        # fail transiently (proxy). Tell the stakeholder so they can resend.
        _confirm(cfg, slug, v, outcome="download_failed", origin=origin)
        return {"ok": False, "reason": "download_failed", "error": str(e)}

    audio_ref = f"feedback/_audio/{v['file_unique_id']}.oga"
    engine = getattr(cfg, "voice_engine", "faster-whisper")
    model = getattr(cfg, "voice_model", "small")
    timeout_sec = float(getattr(cfg, "voice_transcribe_timeout_sec", _DEFAULT_TRANSCRIBE_TIMEOUT_SEC) or 0)

    failed = timed_out = False
    try:
        res = _transcribe_with_timeout(dest, engine=engine, model=model, timeout_sec=timeout_sec)
        transcript = (res.get("text") or "").strip()
        lang = res.get("lang")
        used_engine = res.get("engine") or engine
    except concurrent.futures.TimeoutError:
        log.warning("voice_intake: transcription timed out (>%ss) for %s", timeout_sec, audio_ref)
        failed = timed_out = True
        transcript = f"[transcription timed out after {timeout_sec}s — audio saved for manual review]"
        lang = None
        used_engine = engine
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
    _confirm(
        cfg, slug, v,
        outcome="transcribe_failed" if failed else "ok",
        transcript=transcript,
        origin=origin,
    )

    if failed:
        reason = "transcription_timeout" if timed_out else "transcription_failed"
        return {"ok": False, "reason": reason, "artifact": str(artifact)}

    # T-0526: also record the transcript into the per-(slug, global_user_id)
    # conversation history store (T-0489) — not ONLY the F-*.md feedback artifact
    # — so voice joins the durable conversation thread (the continuity substrate,
    # voice-04). Reuses tg_listener's identity + append (same worker->localhost-API
    # path, T-0529 /worker prefix). Best-effort + env-gated: never blocks intake.
    try:
        from bot_squad_worker import tg_listener as _tl
        ident = _tl.resolve_or_link_sender(cfg, message, slug)
        if ident and ident.get("global_user_id"):
            conv_msg = dict(message)
            conv_msg["text"] = transcript  # voice has no text; the transcript IS the message
            _tl.append_conversation(cfg, slug, ident["global_user_id"], conv_msg)
    except Exception:  # noqa: BLE001
        log.debug("voice_intake: conversation-store append skipped (non-fatal)", exc_info=True)

    return {"ok": True, "artifact": str(artifact), "lang": lang}


def _confirm(
    cfg: Any, slug: str, v: dict, *, outcome: str, transcript: str = "",
    origin: dict[str, Any] | None = None,
) -> None:
    """Post a confirmation back where the voice note arrived (best-effort).

    ``outcome`` ∈ {"ok", "transcribe_failed", "download_failed"} — so the
    stakeholder always gets an acknowledgement, including when the TG file fetch
    fails (proxy hiccup on this DPI host) and there's nothing else to show.

    T-0725: ``origin`` (from :func:`origin_of`) carries the inbound chat, thread
    and message id, and it drives the send — the confirmation lands in the SAME
    chat and topic as the note, threaded as a real TG reply to it. It used to
    read ``project.tg_chat`` + the fixed ``#feedback`` topic instead, which put
    the transcript in a different chat from the note whenever the note wasn't
    sent in the project's default #feedback thread. The static lookup survives
    only as a fallback for a message with no chat on it at all (never a real
    TG update) — a caller passing no ``origin`` gets the old destination.
    """
    try:
        from bot_squad_worker import channels as _channels, tg_topics
        project = cfg.projects.get(slug)
        origin = origin or {}
        chat_id = str(origin.get("chat_id") or "")
        if chat_id:
            topic = origin.get("thread_id")
            reply_to = origin.get("message_id")
        else:
            chat_id = getattr(project, "tg_chat", "") if project else ""
            topic = tg_topics.resolve(cfg, slug, "feedback")
            reply_to = None
        if not chat_id:
            return
        duration = v["duration"]
        if outcome == "too_long":
            # T-0433 P2: rejected pre-download for exceeding the duration cap.
            cap = int(getattr(cfg, "voice_max_duration_sec", _DEFAULT_MAX_DURATION_SEC) or 0)
            text = (f"⚠️ voice note too long ({duration}s > {cap}s cap) — please split "
                    f"into shorter notes.")
        elif outcome == "too_big":
            # T-0611: over the Bot API 20MB getFile cap — a resend can never
            # pass; the honest remedy is splitting into shorter notes.
            text = (f"⚠️ voice note too big for Telegram's bot file limit (20MB, "
                    f"{duration}s) — resending won't help; please split into "
                    f"shorter notes (≤15 min is safe).")
        elif outcome == "download_failed":
            text = (f"⚠️ couldn't fetch your voice note ({duration}s) — the TG file "
                    f"download failed (proxy?). Please resend.")
        elif outcome == "transcribe_failed":
            text = (f"⚠️ got your voice note ({duration}s) but transcription "
                    f"failed — audio saved & flagged for triage.")
        else:
            snippet = transcript[:140] + ("…" if len(transcript) > 140 else "")
            text = f"✅ got your voice note ({duration}s): {snippet}"
        # T-0591 (F5.3): routed through the channel abstraction instead of a
        # raw TgClient — matches tg_listener._channel_notify's pattern.
        extra: dict[str, Any] = {}
        if reply_to is not None:
            extra["reply_to_message_id"] = reply_to
        _channels.get_channel(cfg, project=slug).send(
            text, chat_id=chat_id, sid="voice_intake", topic_id=topic, **extra)
    except Exception:  # noqa: BLE001
        log.exception("voice_intake: confirmation send failed")


def gc_audio(cfg: Any, slug: str) -> dict[str, Any]:
    """Retention GC for ``feedback/_audio/`` blobs (T-0433 P3).

    PRIMARY trigger: a blob whose ``F-*.md`` is promoted/dismissed (the operator
    triaged it) is reaped — the audio is only needed while the note awaits review
    (audit item-12 close-state). BACKSTOP: a blob older than
    ``voice_audio_retention_days`` (an orphan, or a never-handled note) is reaped
    so ``_audio`` can't grow unbounded. An OPEN note's audio survives until it is
    actually handled. ``retention_days <= 0`` disables the age backstop.
    """
    import time
    from bot_squad_worker import frontmatter as _fm

    audio_dir = _audio_dir(cfg, slug)
    if not audio_dir.exists():
        return {"slug": slug, "removed": 0, "removed_files": []}
    retention_days = int(getattr(cfg, "voice_audio_retention_days", _DEFAULT_AUDIO_RETENTION_DAYS) or 0)

    # Classify blobs by their referencing F-*.md: terminal (triaged → reap now) vs
    # tracked-open (an OPEN note → keep until handled, never age-reaped) vs orphan
    # (no artifact → age backstop only).
    terminal_blobs: set[str] = set()
    tracked_blobs: set[str] = set()
    fb_dir = _feedback_dir(cfg, slug)
    for md in fb_dir.glob("F-*.md"):
        try:
            parsed = _fm.parse_or_none(md.read_text())
        except OSError:
            continue
        if parsed is None:
            continue
        meta = parsed[0]
        ref = str(meta.get("audio_ref", "") or "")
        if not ref:
            continue
        name = Path(ref).name
        tracked_blobs.add(name)
        if str(meta.get("status", "") or "") in ("promoted", "dismissed"):
            terminal_blobs.add(name)

    now = time.time()
    removed: list[str] = []
    for blob in sorted(audio_dir.glob("*.oga")):
        if blob.name in terminal_blobs:
            triaged, aged = True, False
        elif blob.name in tracked_blobs:
            # An OPEN note's audio is the evidence — keep it until the operator
            # triages (promote/dismiss); the age backstop does NOT apply.
            continue
        else:
            triaged = False
            try:
                aged = retention_days > 0 and (now - blob.stat().st_mtime) > retention_days * 86400
            except OSError:
                continue
        if triaged or aged:
            try:
                blob.unlink()
                removed.append(blob.name)
            except OSError:
                log.warning("voice_intake: gc_audio could not unlink %s", blob)
    if removed:
        log.info("voice_intake: gc_audio reaped %d blob(s) in %s", len(removed), slug)
    return {"slug": slug, "removed": len(removed), "removed_files": removed}


def voice_ready(cfg: Any) -> dict[str, Any]:
    """Whether the configured STT backend is actually installed in this worker
    venv (T-0433 P1).

    The [voice] extra (faster-whisper) is NOT a core dependency and is NOT
    installed by the base worker install or synced by a deploy — so a fresh
    deploy can have voice ENABLED yet no backend, silently no-opping every note.
    This lets the worker detect that at boot. Uses find_spec (no heavy import).
    """
    import importlib.util

    engine = getattr(cfg, "voice_engine", "faster-whisper")
    if engine == "faster-whisper":
        if importlib.util.find_spec("faster_whisper") is None:
            return {"ready": False, "engine": engine,
                    "reason": "faster-whisper not installed in the worker venv — run "
                              "scripts/install/provision-voice.sh"}
        return {"ready": True, "engine": engine, "reason": "ok"}
    # Non-faster-whisper engines (e.g. a cloud STT) are the operator's to
    # provision; the lazy import in transcribe surfaces a clear error if absent.
    return {"ready": True, "engine": engine, "reason": "non-faster-whisper engine (unchecked)"}


def log_voice_readiness(cfg: Any) -> dict[str, Any]:
    """At worker startup: if voice is ENABLED but its STT backend isn't installed,
    log LOUDLY so it fails at boot, not silently at the first voice note (T-0433
    P1). A no-op when voice is disabled (the default)."""
    if not getattr(cfg, "voice_enabled", False):
        return {"ready": True, "checked": False, "reason": "voice disabled"}
    res = voice_ready(cfg)
    res["checked"] = True
    if not res["ready"]:
        log.warning(
            "voice intake is ENABLED but NOT READY: %s. Voice notes will no-op "
            "(audio saved + flagged) until the backend is provisioned.", res["reason"])
    else:
        log.info("voice intake ready (engine=%s).", res["engine"])
    return res
