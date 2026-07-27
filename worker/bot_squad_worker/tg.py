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
from typing import TYPE_CHECKING, Any

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
        self._data_dir: Path = cfg.data_dir
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
        debounce: bool = True,
        reply_markup: dict | None = None,
        route_sid: str = "",
        reply_to_message_id: int | None = None,
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

        ``debounce=False`` (T-0513): skip the same-payload cooldown — used by
        interactive command replies (``tg_listener._notify`` /
        ``_ask_which_project``) that must echo every time the user types, not
        once per 60s. ``reply_markup`` (T-0513) carries a TG keyboard/inline
        markup verbatim into the send (e.g. the project-picker keyboard);
        ``None`` sends a plain message unchanged.

        ``route_sid`` (T-0719): the RAW routing SID this message came from —
        kept separate from ``sid``, which is a DISPLAY label and since T-0676
        item 5 no longer contains the SID at all. Recorded against the returned
        ``message_id`` in ``tg_reply_map`` so a stakeholder reply resolves back
        to this session by message id, not by re-parsing the prefix. Empty (or
        a non-routing name like ``deploy_monitor``) → nothing recorded, and the
        reply falls through to the legacy regex/attendant path as before.

        ``reply_to_message_id`` (T-0725): make this send an actual Telegram
        REPLY to that inbound message, so the answer is visibly threaded to
        what it answers (the voice-note transcript that arrived detached was
        the reported symptom). ``None`` sends a standalone message exactly as
        before. This addresses only the *threading* — WHERE the message goes is
        still ``chat_id``/``topic_id``, which the caller must derive from the
        inbound message rather than from a static project default.
        """
        if not self._token:
            log.debug("tg.send: no bot token configured — skipping")
            return False

        if not urgent and _in_quiet_hours(self._quiet_start_utc, self._quiet_end_utc):
            log.info("tg.send: dropped (quiet hours — user is asleep)")
            return False

        full_text = _prefix(text, sid=sid, user=user)

        if debounce and self._debounced(chat_id=chat_id, sid=sid, text=text):
            log.debug("tg.send: debounced (same payload within %ds)", self._cooldown)
            return False

        data = self._post(
            chat_id=chat_id, text=full_text, topic_id=topic_id,
            reply_markup=reply_markup, reply_to_message_id=reply_to_message_id,
        )
        self._record_reply_route(chat_id=chat_id, data=data, route_sid=route_sid)
        if debounce:
            self._record(chat_id=chat_id, sid=sid, text=text)
        return True

    def _record_reply_route(self, *, chat_id: str, data: Any, route_sid: str) -> None:
        """T-0719: pin ``result.message_id`` -> ``route_sid`` so a reply to this
        message routes back here regardless of how the prefix renders.

        Best-effort by design: a bad/absent message_id or a store hiccup must
        never turn a delivered page into a failed send."""
        if not route_sid:
            return
        try:
            from bot_squad_worker import tg_reply_map

            message_id = ((data or {}).get("result") or {}).get("message_id")
            if message_id is None:
                return
            tg_reply_map.record(
                self._data_dir, chat_id=chat_id, message_id=message_id, sid=route_sid,
            )
        except Exception:  # noqa: BLE001 — observability only, never fail the send
            log.exception("tg.send: could not record reply route for %s", route_sid)

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

    def rename_general_forum_topic(self, *, chat_id: str, name: str) -> None:
        """Rename a forum's General topic (T-0660: ``editGeneralForumTopic``).

        General is the chat's default topic — it has no ``message_thread_id``
        of its own (``thread_id=None`` in the binding store), so it is a
        distinct Bot API call from ``create_forum_topic``/``close_forum_topic``,
        not just those with ``thread_id=None``.
        """
        if not self._token:
            raise RuntimeError("tg.rename_general_forum_topic: no bot token configured")
        self._call("editGeneralForumTopic", {"chat_id": chat_id, "name": name})

    def edit_forum_topic(self, *, chat_id: str, thread_id: int, name: str) -> None:
        """Rename a REGULAR (non-General) forum topic (T-0669/T-0676 item 1:
        ``editForumTopic``) — the counterpart to
        :meth:`rename_general_forum_topic` for a topic that has its own
        ``message_thread_id``. Lets a bad/SID-named topic (e.g. the T-0669
        phantom-SID topic name bug) be relabeled without recreating it.
        """
        if not self._token:
            raise RuntimeError("tg.edit_forum_topic: no bot token configured")
        self._call(
            "editForumTopic",
            {"chat_id": chat_id, "message_thread_id": int(thread_id), "name": name},
        )

    # ------------------------------------------------------------------
    # Pinned messages (T-0677) — the topic's visible direct-mode marker.
    # ------------------------------------------------------------------

    def send_and_pin(
        self, *, chat_id: str, text: str, topic_id: int | None = None
    ) -> dict:
        """Send ``text`` and pin the resulting message. Returns
        ``{"sent", "message_id", "pinned", "pin_error"}``.

        Used by the ``/pin-session`` confirmation (T-0677: "on choice, the
        message about this should get pinned in the topic"). Deliberately NOT
        routed through :meth:`send`: this is an interactive command reply, so
        it skips debounce/quiet-hours exactly like ``tg_listener``'s other
        command replies do — and it needs the ``message_id`` back, which
        ``send``'s bool contract doesn't carry.

        There is no ``message_thread_id`` on ``pinChatMessage`` — a forum pin
        is scoped by the message's OWN thread, so sending into ``topic_id``
        first is what makes the pin land in that topic.

        A pin failure (the bot is not an admin / lacks ``can_pin_messages``) is
        REPORTED, not raised: the confirmation itself is already delivered and
        the user must be told the marker is missing rather than see the whole
        command blow up.
        """
        if not self._token:
            log.debug("tg.send_and_pin: no bot token configured — skipping")
            return {"sent": False, "message_id": None, "pinned": False, "pin_error": ""}
        data = self._post(chat_id=chat_id, text=text, topic_id=topic_id)
        message_id = ((data or {}).get("result") or {}).get("message_id")
        if message_id is None:
            return {"sent": True, "message_id": None, "pinned": False,
                    "pin_error": "no message_id in sendMessage response"}
        try:
            self.pin_message(chat_id=chat_id, message_id=int(message_id))
        except Exception as e:  # noqa: BLE001 — reported to the user, see docstring
            log.warning("tg.send_and_pin: pin failed for chat %s: %s", chat_id, e)
            return {"sent": True, "message_id": int(message_id), "pinned": False,
                    "pin_error": str(e)}
        return {"sent": True, "message_id": int(message_id), "pinned": True,
                "pin_error": ""}

    def pin_message(self, *, chat_id: str, message_id: int) -> None:
        """Pin an existing message (``pinChatMessage``)."""
        if not self._token:
            raise RuntimeError("tg.pin_message: no bot token configured")
        self._call(
            "pinChatMessage",
            {"chat_id": chat_id, "message_id": int(message_id),
             "disable_notification": True},
        )

    def unpin_message(self, *, chat_id: str, message_id: int) -> None:
        """Unpin one specific message (``unpinChatMessage``).

        Always targets a KNOWN message_id — never ``unpinAllChatMessages``,
        which would clear pins this bot didn't place.
        """
        if not self._token:
            raise RuntimeError("tg.unpin_message: no bot token configured")
        self._call(
            "unpinChatMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
        )

    def _call(self, method: str, payload: dict) -> dict:
        """POST to an arbitrary Bot API method, honoring the egress proxy.

        Parses the JSON body BEFORE ``raise_for_status()`` (T-0660 field
        note): a documented Bot API failure (bad chat_id, missing
        ``can_manage_topics`` admin right, …) comes back as a 4xx with a
        JSON body carrying ``description`` — e.g. "Bad Request:
        CHAT_ADMIN_REQUIRED". Calling ``raise_for_status()`` first threw that
        body away, surfacing only an opaque ``httpx.HTTPStatusError`` at the
        action layer (a bare 500 with no reason). Now the description is
        always in the raised message; ``raise_for_status()`` still runs as a
        fallback for a genuinely non-JSON failure (proxy/network error)."""
        import httpx  # lazy import — not available in all envs

        url = _TG_METHOD.format(token=self._token, method=method)
        extra = {"proxy": self._proxy} if self._proxy else {}
        resp = httpx.post(url, json=payload, timeout=10, **extra)
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if not data.get("ok"):
            desc = data.get("description") or f"HTTP {resp.status_code}"
            raise RuntimeError(f"Telegram API error ({method}): {desc}")
        resp.raise_for_status()
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

    def _post(
        self,
        *,
        chat_id: str,
        text: str,
        topic_id: int | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict:
        """POST sendMessage. Returns the parsed API response (T-0677 needs the
        ``result.message_id`` to pin it); ``send`` ignores the return value, so
        its bool contract is unchanged."""
        import httpx  # lazy import — not available in all envs

        url = _TG_API.format(token=self._token)
        payload: dict = {"chat_id": chat_id, "text": text}
        if topic_id is not None:
            payload["message_thread_id"] = topic_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            # T-0725: a reply target that has since been deleted makes TG reject
            # the WHOLE send (400 "message to be replied not found"). The
            # threading is a nicety; delivering the text is the point — so
            # degrade to an unthreaded message instead of losing it.
            payload["allow_sending_without_reply"] = True
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
        return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------
# Long-message splitting (T-0721) — the ONE chunker every sender uses
# ------------------------------------------------------------------

# Telegram rejects a sendMessage over 4096 chars with an API 400. That cap is a
# transport fact, so the splitter that respects it lives with the transport and
# is shared — T-0586's 🎙-echo chunker and the stakeholder-page chunker are the
# same code, not two implementations that can drift apart (T-0714).
TG_MSG_CAP = 4096
# Default per-part budget: cap minus headroom for the ``[<sid>]`` prefix
# ``send`` prepends, the ``(n/N)`` part marker, and multi-byte slack.
TG_PART_CHUNK = 3800

# Break candidates, best first: paragraph, line, sentence, clause, word. A part
# is cut at the last one that lands past ``_MIN_PART_FRACTION`` of the budget,
# so splitting never produces a runt part just because a boundary sat early.
_BREAKS = ("\n\n", "\n", ". ", "! ", "? ", "… ", "; ", ", ", " ")
_MIN_PART_FRACTION = 0.5


def part_marker(n: int, total: int) -> str:
    """The shared numbered-part marker (T-0586's convention, now shared).

    Every multi-part send carries it so the reader sees one long message split
    across N deliveries, not N unrelated alerts — and, incidentally, so the
    parts are never byte-identical to each other (which would let ``send``'s
    same-payload debounce silently swallow a repeat chunk).
    """
    return f"({n}/{total})"


def split_for_tg(text: str, *, limit: int = TG_PART_CHUNK) -> list[str]:
    """Split ``text`` into TG-sized parts. NEVER truncates (T-0721).

    Text that already fits comes back as a single unchanged element, so the
    common short send stays byte-identical. Longer text is cut at the latest
    sentence/line boundary inside each window (see ``_BREAKS``) rather than
    mid-word; text with no boundary at all (one long unbroken token) is hard-cut
    at ``limit`` — the API cap leaves no other option.
    """
    t = text or ""
    if len(t) <= limit:
        return [t]
    floor = max(1, int(limit * _MIN_PART_FRACTION))
    parts: list[str] = []
    rest = t
    while len(rest) > limit:
        window = rest[:limit]
        cut = limit
        for sep in _BREAKS:
            i = window.rfind(sep)
            if i >= floor:
                cut = i + len(sep)
                break
        head, rest = rest[:cut].rstrip(), rest[cut:].lstrip()
        if head:
            parts.append(head)
    if rest:
        parts.append(rest)
    return parts or [t]


# The 🎙-echo's per-part budget (T-0586). Deliberately looser than
# ``TG_PART_CHUNK``: an echo carries no ``[<sid>]`` prefix worth budgeting for
# on the DM path, and callers that DO get one pass ``reserve``.
ECHO_CHUNK = 3900


def render_transcript_echo(
    transcript: str,
    *,
    prefix: str,
    quote: bool = True,
    limit: int = ECHO_CHUNK,
    reserve: int = 0,
) -> list[str]:
    """Render a voice transcript as 1..N ready-to-send TG message bodies.

    ONE decision for "the user is shown the WHOLE recognition" — shared by both
    voice paths. T-0741: the GROUP/topic ACK (``voice_intake._confirm``) still
    carried T-0386's ``transcript[:140] + "…"`` while only the DM echo had ever
    been given T-0586's chunking, so every group-topic note over 140 chars came
    back visibly cut. That was the stakeholder's recurring "обрезанное"
    complaint — distinct from T-0721 (page truncation) and T-0725 (routing) —
    and it is exactly this repo's duplicated-decision failure mode, so the
    render lives here with the splitter rather than in either caller.

    ``prefix`` leads every part; ``quote`` wraps each part in «» (the DM echo's
    convention). ``reserve`` is extra room the transport will consume that is
    not in ``prefix`` — notably ``send``'s ``[<sid>] `` label — so a transcript
    that fits only without it still gets split instead of hitting an API 400.
    """
    open_q, close_q = ("«", "»") if quote else ("", "")
    single = f"{prefix}{open_q}{transcript}{close_q}"
    if len(single) + reserve <= TG_MSG_CAP:
        return [single]
    chunks = split_for_tg(transcript, limit=limit)
    total = len(chunks)
    return [
        f"{prefix}{part_marker(n, total)} {open_q}{c}{close_q}"
        for n, c in enumerate(chunks, 1)
    ]


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

    T-0690: ``start_utc == end_utc`` means quiet hours are disabled entirely
    (zero-width window), not a 24h window. Without this check the wrap-around
    branch below (``h >= start_utc or h < end_utc``) is true for every hour
    when the two are equal, which is the opposite of "disabled".
    """
    import os
    if os.environ.get("BOT_SQUAD_DISABLE_QUIET_HOURS"):
        return False
    if start_utc == end_utc:
        return False
    from datetime import datetime, timezone
    h = datetime.now(timezone.utc).hour
    if start_utc < end_utc:
        return start_utc <= h < end_utc
    # Wraps midnight: e.g. 17 -> 5 means 17..23 or 0..4
    return h >= start_utc or h < end_utc
