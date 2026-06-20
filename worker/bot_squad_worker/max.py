"""Outbound MAX (max.ru) client — the T-0247 mirror of ``tg.TgClient``.

MAX is a Russian messenger with a Telegram-shaped Bot API. This client mirrors
``TgClient`` (debounce, SID prefix, quiet hours, per-install egress proxy) but
speaks the MAX wire shape:

    POST {base}/messages?<recipient_kind>=<id>
    Authorization: <bot_token>
    Content-Type: application/json
    {"text": "<message>"}

``recipient_kind`` is ``chat_id`` (a group/dialog id) or ``user_id`` (a direct
DM to a user) — for a stakeholder DM set ``[max].recipient_kind = "user_id"``.

Base URL: per the official docs (dev.max.ru/docs-api) the host is
``https://platform-api.max.ru`` and the bot token is passed in the
``Authorization`` header (query-param tokens are deprecated). The endpoint is
NOT 100%% verifiable from this host without a live recipient id, so the base URL
is overridable via the ``BOT_SQUAD_MAX_API_URL`` env var to make it trivial to
correct without a code change. See the T-0247 follow-ups.

Debounce/quiet-hours/prefix behaviour is identical to TG and the helpers are
reused from ``bot_squad_worker.tg`` so the two channels can never drift.

Empty token: if ``cfg.max_bot_token`` is empty, ``send`` returns ``False``
without raising — same contract as TgClient, so test fixtures need no network.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

# Reuse the TG helpers so the two channels share one SID-prefix + quiet-hours
# implementation (no drift between the stakeholder's notification channels).
from bot_squad_worker.tg import _in_quiet_hours, _prefix

if TYPE_CHECKING:
    from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# MAX Bot API send-message endpoint. Overridable via env so a wrong base URL is
# a one-line fix (no redeploy of the package) if the documented host changes.
_MAX_API_DEFAULT = "https://platform-api.max.ru/messages"
_MAX_API = os.environ.get("BOT_SQUAD_MAX_API_URL") or _MAX_API_DEFAULT


class MaxClient:
    """Outbound MAX sender bound to a single bot token + data dir."""

    def __init__(self, cfg: "Config", cooldown_sec: int = 60) -> None:
        self._token: str = getattr(cfg, "max_bot_token", "") or ""
        self._debounce_dir: Path = cfg.data_dir / "_worker" / "max_debounce"
        self._cooldown: int = cooldown_sec
        # Quiet hours are about the stakeholder's sleep window, not the channel,
        # so MAX reuses the same TG-configured window.
        self._quiet_start_utc: int = getattr(cfg, "tg_quiet_hours_start_utc", 17)
        self._quiet_end_utc: int = getattr(cfg, "tg_quiet_hours_end_utc", 5)
        # Per-install MAX egress proxy (socks5/http/https). Empty → direct.
        self._proxy: str = getattr(cfg, "max_proxy_url", "") or ""
        # "chat_id" (default) or "user_id" — how the recipient id is sent.
        self._recipient_kind: str = getattr(cfg, "max_recipient_kind", "chat_id") or "chat_id"

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
        recipient_kind: str | None = None,
    ) -> bool:
        """Send ``text`` to ``chat_id``, prefixed by SID if given.

        Returns True if sent, False if suppressed (empty token, debounce, or
        quiet hours). Raises on network/API errors. ``urgent=True`` bypasses
        quiet hours. ``recipient_kind`` overrides the configured default for
        this call (``"chat_id"`` vs ``"user_id"``).
        """
        if not self._token:
            log.debug("max.send: no bot token configured — skipping")
            return False

        if not urgent and _in_quiet_hours(self._quiet_start_utc, self._quiet_end_utc):
            log.info("max.send: dropped (quiet hours — user is asleep)")
            return False

        full_text = _prefix(text, sid=sid, user=user)

        if self._debounced(chat_id=chat_id, sid=sid, text=text):
            log.debug("max.send: debounced (same payload within %ds)", self._cooldown)
            return False

        self._post(
            chat_id=chat_id,
            text=full_text,
            recipient_kind=recipient_kind or self._recipient_kind,
        )
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

    def _post(self, *, chat_id: str, text: str, recipient_kind: str = "chat_id") -> None:
        import httpx  # lazy import — not available in all envs

        params = {recipient_kind: chat_id}
        payload: dict = {"text": text}
        headers = {"Authorization": self._token}
        # Pass proxy= only when configured, so the no-proxy call shape (and
        # httpx trust_env) is unchanged — mirrors tg.py (T-0194).
        extra = {"proxy": self._proxy} if self._proxy else {}
        resp = httpx.post(
            _MAX_API,
            params=params,
            json=payload,
            headers=headers,
            timeout=10,
            **extra,
        )
        resp.raise_for_status()
        data = resp.json()
        # MAX returns the created Message on success; an error body carries a
        # top-level "code" (e.g. {"code": "...", "message": "..."}).
        if isinstance(data, dict) and "code" in data:
            raise RuntimeError(f"MAX API error: {data}")
        log.info("max.send: sent to %s=%s (text len=%d)", recipient_kind, chat_id, len(text))
