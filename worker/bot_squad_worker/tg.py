"""Outbound Telegram client with debounce and SID-prefix support.

Usage::

    client = TgClient(cfg)
    sent = client.send(chat_id="404580642", text="hello", sid="S-test-p5", user="alexey")

Debounce: the same (chat_id, sid, text) combination is silently dropped for
``cooldown_sec`` seconds (default 60).  State lives as empty files in
``data/_worker/tg_debounce/``, keyed by a SHA-256 of the three components.

Empty token: if ``cfg.tg_bot_token`` is empty, ``send`` returns ``False``
without raising.  This keeps test fixtures (which set bot_token="TESTBOT:TOKEN"
or "") working without network access.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# Telegram Bot API base URL.
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"
# Generic method endpoint (createForumTopic / closeForumTopic / …).
_TG_METHOD = "https://api.telegram.org/bot{token}/{method}"


class TgClient:
    """Outbound TG sender bound to a single bot token + data dir."""

    def __init__(self, cfg: "Config", cooldown_sec: int = 60) -> None:
        self._token: str = cfg.tg_bot_token
        self._debounce_dir: Path = cfg.data_dir / "_worker" / "tg_debounce"
        self._cooldown: int = cooldown_sec
        self._quiet_start_utc: int = getattr(cfg, "tg_quiet_hours_start_utc", 17)
        self._quiet_end_utc: int = getattr(cfg, "tg_quiet_hours_end_utc", 5)
        # T-0194: per-installation TG egress proxy (socks5/http/https). Empty →
        # direct. Routed only here so non-TG worker egress stays un-proxied.
        self._proxy: str = getattr(cfg, "tg_proxy_url", "") or ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(
        self,
        *,
        chat_id: str,
        text: str,
        sid: str = "",
        user: str = "",
        urgent: bool = False,
        topic_id: int | None = None,
    ) -> bool:
        """Send ``text`` to ``chat_id``, prefixed by SID if given.

        Returns True if the message was sent, False if suppressed (empty
        token, debounce, or quiet hours).  Raises on network/API errors.

        ``urgent=True`` bypasses quiet hours (use for hard failures the
        stakeholder explicitly asked to be paged on; not for routine
        "needs your input" pings).

        ``topic_id`` (T-0156): when ``chat_id`` is a forum-enabled group,
        delivers into the given forum thread via ``message_thread_id``.
        ``None`` posts to the group's general feed (or a normal DM).
        """
        if not self._token:
            log.debug("tg.send: no bot token configured — skipping")
            return False

        if not urgent and _in_quiet_hours(self._quiet_start_utc, self._quiet_end_utc):
            log.info("tg.send: dropped (quiet hours — user is asleep)")
            return False

        full_text = _prefix(text, sid=sid, user=user)

        if self._debounced(chat_id=chat_id, sid=sid, text=text):
            log.debug("tg.send: debounced (same payload within %ds)", self._cooldown)
            return False

        self._post(chat_id=chat_id, text=full_text, topic_id=topic_id)
        self._record(chat_id=chat_id, sid=sid, text=text)
        return True

    # ------------------------------------------------------------------
    # Forum-topic CRUD (T-0386 / INI-04) — per-project topic provisioning.
    # ------------------------------------------------------------------

    def create_forum_topic(self, *, chat_id: str, name: str) -> int:
        """Create a forum topic in ``chat_id`` (a forum-enabled supergroup).

        Returns the new ``message_thread_id``. Raises if no bot token is
        configured (provisioning is explicit — it must not silently no-op) or
        on any API error.
        """
        if not self._token:
            raise RuntimeError("tg.create_forum_topic: no bot token configured")
        data = self._call("createForumTopic", {"chat_id": chat_id, "name": name})
        return int(data["result"]["message_thread_id"])

    def close_forum_topic(self, *, chat_id: str, thread_id: int) -> None:
        """Close (archive) a forum topic — the topic-level GC primitive."""
        if not self._token:
            raise RuntimeError("tg.close_forum_topic: no bot token configured")
        self._call(
            "closeForumTopic",
            {"chat_id": chat_id, "message_thread_id": int(thread_id)},
        )

    def _call(self, method: str, payload: dict) -> dict:
        """POST to an arbitrary Bot API method, honoring the egress proxy."""
        import httpx  # lazy import — not available in all envs

        url = _TG_METHOD.format(token=self._token, method=method)
        extra = {"proxy": self._proxy} if self._proxy else {}
        resp = httpx.post(url, json=payload, timeout=10, **extra)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error ({method}): {data}")
        return data

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _debounce_path(self, chat_id: str, sid: str, text: str) -> Path:
        key = f"{chat_id}\x00{sid}\x00{text}"
        h = hashlib.sha256(key.encode()).hexdigest()
        return self._debounce_dir / h

    def _debounced(self, *, chat_id: str, sid: str, text: str) -> bool:
        p = self._debounce_path(chat_id, sid, text)
        if not p.exists():
            return False
        age = time.time() - p.stat().st_mtime
        return age < self._cooldown

    def _record(self, *, chat_id: str, sid: str, text: str) -> None:
        self._debounce_dir.mkdir(parents=True, exist_ok=True)
        p = self._debounce_path(chat_id, sid, text)
        p.touch()

    def _post(self, *, chat_id: str, text: str, topic_id: int | None = None) -> None:
        import httpx  # lazy import — not available in all envs

        url = _TG_API.format(token=self._token)
        payload: dict = {"chat_id": chat_id, "text": text}
        if topic_id is not None:
            payload["message_thread_id"] = topic_id
        # T-0194: pass proxy= only when configured, so the no-proxy call shape
        # (and httpx trust_env) is unchanged.
        extra = {"proxy": self._proxy} if self._proxy else {}
        resp = httpx.post(
            url,
            json=payload,
            timeout=10,
            **extra,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        log.info("tg.send: sent to chat %s (text len=%d)", chat_id, len(text))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _prefix(text: str, *, sid: str, user: str) -> str:
    """Add [<sid> @ <user>] or [<sid>] prefix when relevant."""
    if sid and user:
        return f"[{sid} @ {user}] {text}"
    if sid:
        return f"[{sid}] {text}"
    return text


# Quiet hours: never ping the user when they're asleep. Stakeholder is in
# UTC+5 (Tashkent). Default 17:00–05:00 UTC ≈ 22:00–10:00 local — wide enough
# to cover both early bedtime and late wake-up. Admin can override via
# system_settings.toml. Urgent=True bypasses.


def _in_quiet_hours(start_utc: int = 17, end_utc: int = 5) -> bool:
    """True if the current UTC hour falls in the stakeholder's sleep window.

    Disabled when env var ``BOT_SQUAD_DISABLE_QUIET_HOURS`` is set — used by
    the test suite, which exercises send paths without time-dependent
    skips.
    """
    import os
    if os.environ.get("BOT_SQUAD_DISABLE_QUIET_HOURS"):
        return False
    from datetime import datetime, timezone
    h = datetime.now(timezone.utc).hour
    if start_utc < end_utc:
        return start_utc <= h < end_utc
    # Wraps midnight: e.g. 17 -> 5 means 17..23 or 0..4
    return h >= start_utc or h < end_utc
