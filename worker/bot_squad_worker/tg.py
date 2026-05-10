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


class TgClient:
    """Outbound TG sender bound to a single bot token + data dir."""

    def __init__(self, cfg: "Config", cooldown_sec: int = 60) -> None:
        self._token: str = cfg.tg_bot_token
        self._debounce_dir: Path = cfg.data_dir / "_worker" / "tg_debounce"
        self._cooldown: int = cooldown_sec

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
    ) -> bool:
        """Send ``text`` to ``chat_id``, prefixed by SID if given.

        Returns True if the message was sent, False if suppressed (empty
        token or debounce).  Raises on network/API errors.
        """
        if not self._token:
            log.debug("tg.send: no bot token configured — skipping")
            return False

        full_text = _prefix(text, sid=sid, user=user)

        if self._debounced(chat_id=chat_id, sid=sid, text=text):
            log.debug("tg.send: debounced (same payload within %ds)", self._cooldown)
            return False

        self._post(chat_id=chat_id, text=full_text)
        self._record(chat_id=chat_id, sid=sid, text=text)
        return True

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

    def _post(self, *, chat_id: str, text: str) -> None:
        import httpx  # lazy import — not available in all envs

        url = _TG_API.format(token=self._token)
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=10,
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
