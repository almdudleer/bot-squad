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

# Reuse the TG quiet-hours helper so both channels answer "is he asleep" the
# same way (no drift between the stakeholder's notification channels). The
# sender-tag half moved to `sender_tag.compose` in T-0758 and is shared the
# same way, from one module rather than two copies of a prefix rule.
from bot_squad_worker.tg import _in_quiet_hours

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
        # T-0755: the outbound record needs the data dir to write to and the
        # cfg to redact this install's real secrets against. MAX gets the same
        # treatment as TG deliberately — the page-channel switch (T-0610) means
        # a stakeholder conversation can be happening HERE, and recording only
        # one transport is how this repo's duplicated-decision bugs start.
        self._cfg: "Config" = cfg
        self._data_dir: Path = cfg.data_dir
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
        record_outbound: bool = True,
        sender_sid: str = "",
    ) -> bool:
        """Send ``text`` to ``chat_id``, prefixed by SID if given.

        Returns True if sent, False if suppressed (empty token, debounce, or
        quiet hours). Raises on network/API errors. ``urgent=True`` bypasses
        quiet hours. ``recipient_kind`` overrides the configured default for
        this call (``"chat_id"`` vs ``"user_id"``).

        ``record_outbound`` (T-0755, default True): write this delivery to the
        outbound log — see ``tg.TgClient.send`` for why it is opt-OUT and which
        two callers pass False.

        ``sender_sid`` (T-0758): the raw SID of the session that composed the
        text, for the sender tag. The TG twin, on purpose — the stakeholder's
        ask is about knowing WHICH PROJECT answered, and that question does not
        change with the transport, so both channels apply one rule from one
        module. MAX passes no chat/topic to the resolver because it has no
        forum bindings to resolve a slug FROM; the label therefore comes from
        the sending session alone, which is where it should come from anyway
        (see ``sender_tag``).
        """
        if not self._token:
            log.debug("max.send: no bot token configured — skipping")
            return False

        if not urgent and _in_quiet_hours(self._quiet_start_utc, self._quiet_end_utc):
            log.info("max.send: dropped (quiet hours — user is asleep)")
            return False

        # T-0758: same single composition point as tg.py — the tagged string
        # is what goes on the wire AND what `_record_outbound` spools.
        from bot_squad_worker import sender_tag as _sender_tag

        full_text = _sender_tag.compose(
            self._cfg, text, sid=sid, user=user, sender_sid=sender_sid,
        )

        if self._debounced(chat_id=chat_id, sid=sid, text=text):
            log.debug("max.send: debounced (same payload within %ds)", self._cooldown)
            return False

        self._post(
            chat_id=chat_id,
            text=full_text,
            recipient_kind=recipient_kind or self._recipient_kind,
        )
        if record_outbound:
            self._record_outbound(chat_id=chat_id, text=full_text, sid=sid)
        self._record(chat_id=chat_id, sid=sid, text=text)
        return True

    def _record_outbound(self, *, chat_id: str, text: str, sid: str) -> None:
        """T-0755: record WHAT was delivered on MAX — the TG twin of
        ``tg.TgClient._record_outbound``.

        MAX has no ``route_sid`` seam (``tg_reply_map`` is TG-specific: it joins
        on a Telegram ``message_id``), so ``sid`` here is the display label and
        a MAX send is attributed as ``system:<label>`` unless that label happens
        to be a real routing SID. That is honest rather than convenient — MAX
        replies do not route back to a session today, so claiming a session
        authorship the transport cannot verify would be worse than naming the
        class.

        Never raises: a logging failure must not turn a delivered message into
        a failed send.
        """
        try:
            from bot_squad_worker import outbound_log

            outbound_log.record(
                self._data_dir,
                channel="max",
                chat_id=chat_id,
                text=text,
                route_sid=sid,
                sender_label=sid,
                cfg=self._cfg,
            )
        except Exception:  # noqa: BLE001 — observability only, never fail the send
            log.exception("max.send: could not record outbound content for %s", sid)

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
