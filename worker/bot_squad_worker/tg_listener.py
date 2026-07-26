"""Listen for Telegram updates and route replies into sessions."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

SID_RE = re.compile(r"\[(S-[A-Za-z0-9_-]+?-p\d+)")

# T-0682 (T-0676 item 2): TG chat/topic service-event field names — a message
# carrying any of these is a system notice (e.g. the forum_topic_created echo
# from our own createForumTopic call), not human-authored content.
_SERVICE_MESSAGE_FIELDS = (
    "forum_topic_created",
    "forum_topic_closed",
    "forum_topic_reopened",
    "forum_topic_edited",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "video_chat_scheduled",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    # T-0684 audit: this tuple was built (T-0682) around the ONE service type
    # that actually bit us (forum_topic_created), not the full TG Bot API
    # Message-object service-field surface. Any of these ALSO carries no
    # `text` and no `is_bot`/`sender_chat` marker when a REAL group member
    # triggers it via ordinary TG client UI (no special bot flow needed) — so
    # each is a live path to the exact same "empty message wakes/spawns an
    # attendant for no content" bug T-0682 fixed, just via a different
    # trigger. Added defensively; harmless if a listed field never actually
    # fires in this deployment.
    "message_auto_delete_timer_changed",
    "chat_background_set",
    "boost_added",
    "users_shared",
    "chat_shared",
    "write_access_allowed",
    "proximity_alert_triggered",
    "giveaway_created",
    "giveaway",
    "giveaway_winners",
    "giveaway_completed",
    "web_app_data",
    "passport_data",
    "connected_website",
)


def _own_bot_id(cfg) -> str:
    """The numeric TG user id of our own bot, derived from the configured bot
    token (``<bot_id>:<secret>``) — never hardcoded, so it tracks whichever
    token is actually configured (T-0682 review note: derive, don't hardcode
    the id observed via getMe). Empty string when no token is configured."""
    token = getattr(cfg, "tg_bot_token", "") or ""
    return token.split(":", 1)[0] if ":" in token else ""


def _is_ignorable_service_message(msg: dict, cfg: Any = None) -> bool:
    """T-0682: our own bot's forum-topic service messages (the
    forum_topic_created echo from a createForumTopic call) were being
    ingested as human DMs — no is_bot/service-message guard existed — which
    minted a GlobalUser for the bot itself (id confirmed via getMe) and
    spawned attendant sessions replying to empty messages (the item-2
    echo-loop). Skip: any bot-authored message (ours or another bot's, or a
    sender id matching our OWN bot's id — belt-and-suspenders in case a
    payload ever omits ``is_bot``), anonymous channel-linked posts, and TG's
    own chat/topic service events.
    """
    frm = msg.get("from") or {}
    if frm.get("is_bot"):
        return True
    own_id = _own_bot_id(cfg) if cfg is not None else ""
    if own_id and str(frm.get("id", "")) == own_id:
        return True
    if msg.get("sender_chat"):
        return True
    return any(field in msg for field in _SERVICE_MESSAGE_FIELDS)


def _proxy_kwargs(cfg) -> dict:
    """T-0194: ``{"proxy": url}`` when a per-installation TG proxy is set, else
    ``{}`` — so the no-proxy httpx call shape (and trust_env) is unchanged."""
    proxy = getattr(cfg, "tg_proxy_url", "") or ""
    return {"proxy": proxy} if proxy else {}


def _last_update_id_path(cfg) -> Path:
    return cfg.data_dir / "_worker" / "tg_last_update_id"


def _poll_health_path(cfg) -> Path:
    return cfg.data_dir / "_worker" / "tg_poll_health.json"


def _unknown_chats_path(cfg) -> Path:
    return cfg.data_dir / "_worker" / "tg_unknown_chats.json"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_poll_health(cfg, *, ok: bool, error: str = "") -> None:
    """Persist TG poll health so an operator can tell 'no mail' from an egress
    error (next-wave #13). On a successful poll stamp ``last_ok_poll_at``; on a
    network error stamp ``last_error`` + ``last_error_at`` WITHOUT clearing the
    last-good timestamp (so 'last ok 10m ago, erroring since' is visible).
    Best-effort + atomic (tmp+replace); never raises — a health-write failure
    must not break polling."""
    import json
    import os
    try:
        p = _poll_health_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(p.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        now = _now_iso()
        if ok:
            state["last_ok_poll_at"] = now
            state["last_error"] = ""
        else:
            state["last_error"] = error
            state["last_error_at"] = now
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, p)
    except OSError:
        pass


def _record_unknown_chat(cfg, chat_id: str, chat: dict) -> None:
    """T-0664: onboarding seam — a message from a non-allowlisted chat_id is
    otherwise silently dropped, leaving a freshly-added group's id undiscoverable.
    Logs a WARNING (visible with no new tooling) and persists a per-chat_id
    record so it's self-service discoverable by reading one file. Mirrors
    ``_record_poll_health``'s atomic (tmp+replace) + best-effort-never-raises
    shape: a discovery-write failure must not break the allowlist skip."""
    title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    chat_type = str(chat.get("type") or "")
    log.warning(
        "tg_listener: message from unrecognized chat_id=%s (title=%r type=%r) — "
        "not allowlisted; add it to a project's tg_chat (or bind a topic) to admit it",
        chat_id, title, chat_type,
    )
    import json
    import os
    try:
        p = _unknown_chats_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(p.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        now = _now_iso()
        entry = state.get(chat_id) or {"first_seen_at": now, "count": 0}
        entry["title"] = title
        entry["type"] = chat_type
        entry["last_seen_at"] = now
        entry["count"] = entry.get("count", 0) + 1
        state[chat_id] = entry
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, p)
    except OSError:
        pass


def _unbound_topics_path(cfg) -> Path:
    return cfg.data_dir / "_worker" / "tg_unbound_topics.json"


def _record_unbound_topic(
    cfg, chat_id: str, thread_id: Any, chat: dict, *, is_general_feed: bool = False,
) -> None:
    """T-0693: a message arrived on a forum topic of a chat that already
    participates in per-topic routing (D-0055 §2-3 — it has at least one
    OTHER bound topic), but THIS ``(chat_id, thread_id)`` has no tg_bindings
    entry. Before this, ``handle_update`` silently fell back to the chat's
    STATIC default project (``_slug_for_chat``) for such a message — the live
    incident this closes: a stakeholder's watchrobot question, typed into a
    freshly-created but not-yet-bound topic, was mis-slugged into bot-squad's
    conversation store with zero error signal, discovered only because he
    noticed his question went unanswered.

    ``is_general_feed=True`` (T-0700): the message arrived on the chat's
    General feed (``thread_id=None``) rather than a named topic — there is no
    TG "topic" here, so the warning/log copy says "General"/"no topic"
    instead, but the discovery-record shape and hold behavior are identical.

    Logs a WARNING (grep-able) and persists a per-(chat_id, thread_id)
    discovery record so a freshly created/renamed topic is self-service
    discoverable (bind it via ``bsq topic bind``) instead of silently
    mis-slugging every message on it until someone notices by accident.
    Mirrors ``_record_unknown_chat``'s shape/best-effort/atomic-write
    contract."""
    title = chat.get("title") or chat.get("username") or ""
    if is_general_feed:
        log.warning(
            "tg_listener: UNBOUND GENERAL (no topic) chat_id=%s (title=%r) — "
            "this chat's bound topics span more than one project, but its "
            "General feed has no binding of its own; message HELD UNROUTED "
            "(not mis-slugged to the chat's static default project). Bind "
            "it: bsq topic bind %s None <slug>",
            chat_id, title, chat_id,
        )
    else:
        log.warning(
            "tg_listener: UNBOUND TOPIC chat_id=%s thread_id=%s (title=%r) — this "
            "chat already routes other topics via tg_bindings, but this one has "
            "no binding; message HELD UNROUTED (not mis-slugged to the chat's "
            "static default project). Bind it: bsq topic bind %s %s <slug>",
            chat_id, thread_id, title, chat_id, thread_id,
        )
    import json
    import os
    try:
        p = _unbound_topics_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(p.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        now = _now_iso()
        key = f"{chat_id}:{thread_id}"
        entry = state.get(key) or {"first_seen_at": now, "count": 0}
        entry["chat_id"] = chat_id
        entry["thread_id"] = thread_id
        entry["title"] = title
        entry["last_seen_at"] = now
        entry["count"] = entry.get("count", 0) + 1
        state[key] = entry
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, p)
    except OSError:
        pass


def _read_last_update_id(cfg) -> int:
    p = _last_update_id_path(cfg)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return 0


def _write_last_update_id(cfg, update_id: int) -> None:
    p = _last_update_id_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(update_id))


def poll_updates(cfg, last_update_id: int, timeout: int = 25) -> list[dict]:
    """Long-poll TG getUpdates. Returns empty on no-token / network errors."""
    if not cfg.tg_bot_token:
        return []
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/getUpdates"
    params = {
        "offset": last_update_id + 1,
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    # T-0194: route inbound long-poll through the per-installation TG proxy when
    # set (mirrors tg.py egress). Only passed when configured.
    extra = _proxy_kwargs(cfg)
    try:
        r = httpx.get(url, params=params, timeout=timeout + 5, **extra)
        r.raise_for_status()
        result = r.json().get("result", [])
        _record_poll_health(cfg, ok=True)
        return result
    except (httpx.HTTPError, ValueError) as e:
        # next-wave #13: don't swallow the error SILENTLY — record it so the
        # operator can distinguish a DPI/egress failure from a genuine no-mail
        # poll. Still returns [] so one bad poll never poisons the offset.
        _record_poll_health(cfg, ok=False, error=repr(e))
        return []


def extract_reply_target(message: dict) -> Optional[tuple[str, str]]:
    """Extract (sid, text) if this is a reply to a worker notification."""
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return None
    quoted = reply_to.get("text") or ""
    m = SID_RE.match(quoted)
    if not m:
        return None
    return (m.group(1), message.get("text", "").strip())


def extract_slash_command(message: dict) -> Optional[tuple[str, str]]:
    """Extract (cmd, args_str) if this is a /sessions, /say, /help, /project,
    /state or /pin-session command."""
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    cmd = parts[0].lstrip("/").split("@")[0]   # strip @botname if present
    args = parts[1] if len(parts) > 1 else ""
    # T-0677: `/pin_session` is an accepted alias for `/pin-session` — the
    # stakeholder's verbatim spells it with a dash, but TG's own command
    # registry/autocomplete only accepts [a-z0-9_], so the underscore form is
    # what a user typing via autocomplete would get. Both normalize to the
    # dashed name the picker buttons emit.
    if cmd == "pin_session":
        cmd = "pin-session"
    # T-0655 (Addendum 1): /state — drive on/off, quota target, lifecycle state.
    if cmd not in {"sessions", "say", "help", "project", "state", "pin-session"}:
        return None
    return (cmd, args)


def _api_base_url() -> str:
    """Base URL of the mothership API the worker links TG senders against.

    T-0527: read a DEDICATED ``WORKER_API_BASE_URL`` first, falling back to
    ``MOTHERSHIP_BASE_URL``. T-0488 originally overloaded ``MOTHERSHIP_BASE_URL``
    for the worker's localhost API target, but that collides with the API
    container's OWN ``MOTHERSHIP_BASE_URL`` (its public mothership self-URL) in
    the shared ``.env`` — and on this host the worker's ``.env`` load wins over
    the systemd ``Environment=`` override regardless of textual order, so the
    override never took effect (worker kept calling the public URL). A dedicated
    var set ONLY in ``.env`` (``WORKER_API_BASE_URL=http://127.0.0.1:8099``) has
    no such conflict. Empty when neither is set → linkage is a no-op."""
    return (
        os.environ.get("WORKER_API_BASE_URL")
        or os.environ.get("MOTHERSHIP_BASE_URL")
        or ""
    ).rstrip("/")


def _worker_api_token() -> str:
    """T-0488: the shared-secret Bearer the worker presents to the token-gated
    linkage endpoint. From ``WORKER_API_TOKEN``; empty when unset → no-op."""
    return (os.environ.get("WORKER_API_TOKEN") or "").strip()


def _sender_display_name(frm: dict) -> str:
    """Human label for a TG sender: ``first last`` if present, else username."""
    name = " ".join(p for p in (frm.get("first_name"), frm.get("last_name")) if p).strip()
    return name or str(frm.get("username") or "")


def resolve_or_link_sender(cfg, msg: dict, slug: str = "") -> Optional[dict]:
    """T-0488: recognize the inbound TG sender as a cross-server mothership
    GlobalUser, linking it on first contact.

    The API owns the GlobalUser registry (single-writer-per-store); the worker
    only READS ``_mothership`` elsewhere and never writes it. So this calls the
    token-gated ``POST /api/m/tg/resolve-or-link`` endpoint (httpx, mirroring the
    autoupdate worker->API precedent) — NOT through the TG egress proxy, this is
    a local-API call, not Telegram traffic.

    Best-effort + env-gated: returns ``None`` (no-op, no HTTP) when there's no
    sender id, or when the API base URL / worker token aren't configured — so a
    misconfigured or pre-secret deploy never blocks inbound routing. On success
    returns the resolved identity ``{global_user_id, created, slug}`` for the
    downstream user-conversation seam (anchored on ``(slug, global_user_id)``)."""
    frm = msg.get("from") or {}
    tg_user_id = str(frm.get("id") or "").strip()
    if not tg_user_id:
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    url = f"{base}/api/m/worker/tg/resolve-or-link"  # T-0529: /worker prefix (localhost-only)
    try:
        r = httpx.post(
            url,
            json={
                "tg_user_id": tg_user_id,
                "display_name": _sender_display_name(frm),
                "slug": slug,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("global_user_id"):
        return None
    return {
        "global_user_id": data["global_user_id"],
        "created": bool(data.get("created", False)),
        "slug": slug,
    }


def _msg_attachments(msg: dict) -> list[dict]:
    """Light attachment descriptors for the conversation record. We keep just
    enough to know an attachment was present (type + file_id) — the binary lives
    in TG, not the thread. Voice is the live case (T-0386); photos/documents are
    recorded generically so the thread isn't silently lossy."""
    out: list[dict] = []
    voice = msg.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        out.append({"type": "voice", "file_id": voice["file_id"]})
    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        out.append({"type": "document", "file_id": doc["file_id"]})
    if msg.get("photo"):
        out.append({"type": "photo"})
    return out


def append_conversation(
    cfg, slug: str, global_user_id: str, msg: dict, *, thread_id: Any = None,
    general_feed: bool = False,
) -> Optional[bool]:
    """T-0489: record one inbound TG user message to the per-(project, user)
    conversation history store — the durable thread "we can always look up"
    (voice-04), the continuity substrate across session recycles.

    The API owns the store (single-writer); the worker POSTs to the token-gated
    append endpoint over the same worker->API path T-0488 established (httpx,
    base=MOTHERSHIP_BASE_URL, Bearer=WORKER_API_TOKEN) — NOT through the TG
    egress proxy (this is a local-API call, not Telegram traffic).

    ``thread_id`` (T-0676 items 3/6): the bound forum topic this message
    arrived in, when any — isolates the record into that topic's OWN thread
    (see ``conversation_store.conv_path``) instead of the project's mixed
    history. Omitted from the request body when ``None`` (DM / non-topic
    message), so an existing caller's request is byte-identical to before.

    ``general_feed`` (T-0693 Finding B): marks this append as arriving via an
    explicit ``tg_bindings`` General-feed binding rather than a genuine
    DM/non-topic message — both have ``thread_id=None``, otherwise
    indistinguishable. Omitted from the request body when ``False`` (every
    pre-T-0693 caller).

    Best-effort + env-gated: returns ``None`` (no-op, no HTTP) when there's no
    ``global_user_id``, or the API base / worker token aren't configured — so a
    record failure NEVER blocks inbound routing. Returns ``True`` on a recorded
    append."""
    gid = str(global_user_id or "").strip()
    if not gid or not str(slug or ""):
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    url = f"{base}/api/m/worker/conversations/{slug}/{gid}/messages"  # T-0529: /worker prefix
    payload = {
        "author": "user",
        "text": msg.get("text") or "",
        "attachments": _msg_attachments(msg),
        "timestamp": _msg_ts(msg),
    }
    if thread_id is not None and str(thread_id).strip() != "":
        payload["thread_id"] = thread_id
    if general_feed:
        payload["general_feed"] = True
    try:
        r = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return True


_FYI_PREFIX = "[FYI — ответ не требуется]"


def append_conversation_fyi(cfg, slug: str, global_user_id: str, *, author: str, text: str) -> Optional[bool]:
    """T-0660: record a PASSIVE, non-actionable append into the project's
    (slug, global_user_id) attendant thread — for either of the two
    session<->stakeholder direct-contact directions the task-topic model adds
    (D-0055 'Topic model evolution' mechanics #3):
      - a dev/TL/orchestrator session wrote directly to the stakeholder
        (``author="session:<sid>"``), or
      - the stakeholder replied directly to a session, bypassing the
        attendant (``author="user"``).

    Marked ``fyi=True`` on the API append (T-0660) so the endpoint records it
    for context but suppresses the side effects a normal append of that
    ``author`` would trigger: a ``session:``-authored append normally
    RELAYS back to the user's TG (would echo the "no reply needed" note
    right back to them — wrong); a ``user``-authored append normally WAKES
    the attendant (this isn't its own inbox item to act on). ``text`` is
    prefixed with an unambiguous marker so a reading session never mistakes
    this for something requiring a reply.

    Same best-effort/env-gated contract as ``append_conversation``: a no-op
    when unconfigured or on failure, never blocks the caller."""
    gid = str(global_user_id or "").strip()
    if not gid or not str(slug or ""):
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    url = f"{base}/api/m/worker/conversations/{slug}/{gid}/messages"  # T-0529: /worker prefix
    try:
        r = httpx.post(
            url,
            json={
                "author": author,
                "text": f"{_FYI_PREFIX} {text}",
                "fyi": True,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return True


# ---- T-0492: hardwired project routing --------------------------------------
# A user pins a CURRENT project (a button / ``/project <slug>``); subsequent
# unquoted messages sticky-route to it; the bot ASKS which project when unset
# (voice-04: "the bot should be hardwired to ask the user which project he's
# talking to"). The pin is owned by the API (single-writer = pins_store); the
# worker reads/sets it through the token-gated routing endpoints, env-gated +
# best-effort so a routing-store outage never crashes inbound handling.


def _routing_url(base: str, global_user_id: str) -> str:
    return f"{base}/api/m/worker/routing/{global_user_id}/current-project"  # T-0529: /worker prefix


def get_current_project(cfg, global_user_id: str) -> Optional[str]:
    """The user's pinned current-project slug, or ``None`` when unset / the
    routing store is unreachable / linkage isn't configured (best-effort)."""
    gid = str(global_user_id or "").strip()
    if not gid:
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    try:
        r = httpx.get(
            _routing_url(base, gid),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict):
        slug = data.get("slug")
        return str(slug) if slug else None
    return None


def set_current_project(cfg, global_user_id: str, slug: str) -> Optional[bool]:
    """Pin (or switch) the user's current project via the API. Returns ``True``
    on success, ``None`` on a no-op / failure (best-effort + env-gated)."""
    gid = str(global_user_id or "").strip()
    if not gid or not str(slug or ""):
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    try:
        r = httpx.post(
            _routing_url(base, gid),
            json={"slug": slug},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return True


def _ask_which_project(cfg, chat_id: str, *, thread_id: Any = None) -> None:
    """Hardwired 'which project?' prompt — a reply-keyboard listing every
    project. Each button sends ``/project <slug>`` as a NORMAL message, so the
    selection arrives under ``allowed_updates:["message"]`` (no poll-contract
    change; mirrors the voice-intake constraint). Best-effort."""
    if not cfg.tg_bot_token:
        return
    keyboard = [[{"text": f"/project {slug}"}] for slug in cfg.projects]
    reply_markup = {
        "keyboard": keyboard,
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    # T-0513: route through the channel abstraction (was a raw httpx sendMessage
    # that bypassed get_channel). Interactive group reply → urgent=True (don't
    # quiet-hours-drop the picker a user just triggered), sid="" (no prefix),
    # debounce=False (offer the picker every time it's needed). Best-effort: a
    # messenger outage must not break inbound routing (channel.send raises).
    _channel_notify(cfg, chat_id, "Which project are you talking to? Pick one:",
                    reply_markup=reply_markup, thread_id=thread_id)


def _handle_project(cfg, chat_id: str, gid: str, args: str, *, thread_id: Any = None) -> dict:
    """``/project [slug]`` — pin/switch the current project, or (no arg / unknown
    slug) re-offer the picker."""
    if not gid:
        _notify(cfg, chat_id, "Couldn't identify you yet — try again in a moment.",
                thread_id=thread_id)
        return {"ok": False, "action": "project_no_identity"}
    slug = args.strip()
    if not slug:
        _ask_which_project(cfg, chat_id, thread_id=thread_id)
        return {"ok": True, "action": "ask_project"}
    if slug not in cfg.projects:
        _notify(cfg, chat_id, f"Unknown project: {slug}", thread_id=thread_id)
        _ask_which_project(cfg, chat_id, thread_id=thread_id)
        return {"ok": False, "action": "project_unknown", "slug": slug}
    ok = set_current_project(cfg, gid, slug)
    if not ok:
        _notify(cfg, chat_id, f"Couldn't switch to {slug} right now — try again.",
                thread_id=thread_id)
        return {"ok": False, "action": "project_set_failed", "slug": slug}
    _notify(cfg, chat_id, f"You're on project {slug}. Messages now go there.",
            thread_id=thread_id)
    return {"ok": True, "action": "project_set", "slug": slug}


def _ensure_user_conversation(
    cfg, slug: str, gid: str, message_ref: str, *, thread_id: Any = None,
) -> Optional[dict]:
    """T-0485 (M4 firehose intake): route an unquoted dump to a (continued-or-
    spawned) user-conversation session via the ``ensure_user_conversation``
    worker action (T-0478). The action is idempotent + single-attendant per
    ``(slug, gid)`` (keyed on the live tmux pane), so a burst of messages routes
    to the EXISTING session rather than fanning out into N spawns; a brand-new
    user gets a user-conversation role session that records the verbatim
    request->task and notifies the operator.

    ``message_ref`` is a POINTER to the just-appended T-0489 store record (its
    timestamp = its key in the ``(slug, gid)`` thread), surfaced in the session's
    boot prompt — the session reads the full dump from the store SSOT, not a raw
    blob passed inline.

    Best-effort: the message is already durable in the conversation store
    (T-0489), so a spawn/pane hiccup must NEVER fail inbound routing — we swallow
    and return ``None``. T-0570: a swallowed failure still must not leave the
    USER in silence — we surface the failure kind via the return value
    (``{"ok": False, "parked": True}`` for a backoff/saturation spawn refusal,
    ``None`` for anything else) so ``_handle_unquoted`` can tell the chat the
    message is parked rather than dropping into dead air."""
    from bot_squad_worker import actions as A
    params = {
        "slug": slug,
        "global_user_id": gid,
        "message_ref": message_ref,
    }
    if thread_id is not None and str(thread_id).strip() != "":
        params["thread_id"] = thread_id
    try:
        return A.dispatch("ensure_user_conversation", params)
    except A.ActionError as e:
        if "backoff" in str(e):
            return {"ok": False, "parked": True}
        return None
    except Exception:  # noqa: BLE001 — best-effort; never break inbound routing
        return None


# ---- T-0494: misattribution guard (detector retained, prompt removed T-0666) -
# A pinned user may write a message that EXPLICITLY targets a DIFFERENT project
# than the pinned one ("in bot-squad I see ...", voice-02). T-0494 originally
# offered a reroute-confirm prompt before committing the slug; the stakeholder
# found that prompt disruptive ("отключи, мешаются") and T-0666 removes it
# outright — a cross-project mention now just routes to the project-of-record
# like any other unquoted message, no confirm. ``_detect_cross_project_target``
# is KEPT (not orphaned): D-0055 §2 reuses it as the per-message classifier for
# the gated Slice 2 (T-0640) per-message dynamic routing.

_CROSS_PROJECT_CUE = r"(?:in|on|for|about|regarding|project)"


def _slug_mention_re(slug: str):
    """A regex matching an EXPLICIT mention of ``slug`` as a routing target: a
    targeting cue word (in/on/for/about/regarding/project) immediately followed
    by the slug, tolerating the slug's dashes typed as spaces ("bot-squad" ~
    "bot squad", voice-02)."""
    body = r"[\s-]+".join(re.escape(p) for p in slug.split("-"))
    return re.compile(rf"\b{_CROSS_PROJECT_CUE}\s+{body}\b", re.IGNORECASE)


def _detect_cross_project_target(cfg, text: str, current: str) -> Optional[str]:
    """Return a registered project slug (≠ ``current``) the message EXPLICITLY
    targets, else ``None``. Conservative (cue-prefixed mention) to avoid
    false-positive reroutes on incidental mentions — the deep intent analysis
    stays in the session.

    Retained for D-0055 §2 / T-0640's per-message routing classifier (see
    module comment above) — not currently wired to any prompt (T-0666)."""
    t = text or ""
    for slug in cfg.projects:
        if slug == current:
            continue
        if _slug_mention_re(slug).search(t):
            return slug
    return None


def _warn_if_general_feed_collides(cfg, slug: str, chat_id: str) -> None:
    """T-0693 Finding B: a General-feed ``tg_bindings`` entry (``thread_id=
    None``) and one or more per-topic bindings on the SAME project ``slug``
    would both accrue durable-store/locus records that are only distinguished
    by the additive ``general_feed`` marker (see ``append_conversation`` /
    ``conversation_locus.set_locus``), not by isolated storage keys — a
    General-feed message and a plain DM for that slug still land in the SAME
    file. Not exercised in production today (every live binding's General
    entry and per-topic entries are on DIFFERENT slugs) — this makes the
    coexistence loud instead of silent if it ever does happen. Best-effort
    scan of the runtime binding map; never raises."""
    from bot_squad_worker import tg_bindings
    try:
        for key, rec in tg_bindings.load(cfg).items():
            bound_chat, _, thread_part = key.partition(":")
            if bound_chat == str(chat_id) and thread_part and rec.get("slug") == slug:
                log.warning(
                    "tg_listener: slug=%r has BOTH a General-feed binding and a "
                    "per-topic binding (chat_id=%s thread=%s) — General-feed and "
                    "per-topic messages for this slug are only distinguished by "
                    "a marker (T-0693 Finding B), not isolated storage keys",
                    slug, bound_chat, thread_part,
                )
                return
    except Exception:  # noqa: BLE001 — best-effort diagnostic, never break routing
        pass


# ---- T-0677: per-topic direct mode (`/pin-session`) -------------------------
# Stakeholder, verbatim (2026-07-25): "it should be like /pin-session, replying
# with buttons to pick the session to pin, and on choice, the message about
# this should get pinned in the topic."
#
# The ROUTING this toggles already exists (T-0660): a `tg_bindings` entry
# carrying a ``session_id`` makes `_handle_topic_bound` inject straight into
# that session; ``session_id=None`` routes through the project's
# user-conversation attendant, which stays the DEFAULT. So this command flips
# ONE field of an existing binding — it is not a second routing path.
#
# The picker is a REPLY-KEYBOARD (each button sends ``/pin-session <sid>`` as a
# NORMAL message), exactly like `_ask_which_project`: the poller runs
# ``allowed_updates:["message"]`` and deliberately avoids inline
# callback_query buttons, so an inline picker would need a poll-contract change
# (operator p241, 2026-07-26 — explicitly not in scope for this ticket).

#: Args that mean "back to the attendant" (turn direct mode off).
_PIN_SESSION_OFF = {"off", "none", "attendant", "-"}


def _pinnable_sessions(cfg, slug: str) -> list[dict]:
    """Sessions of ``slug`` that `/pin-session` may target — the ones an
    inbound topic message could actually be injected into.

    Excludes SUSPENDED/archived rows: a binding pointing at one would send
    every message in the topic into `_handle_reply`'s "session not active —
    message dropped" branch, i.e. a topic that silently eats the stakeholder's
    input. Paused-but-live panes are kept (the pane still receives input)."""
    from bot_squad_worker import sessions as S
    try:
        rows = S.list_sessions(cfg, slug)
    except Exception:  # noqa: BLE001 — unknown slug / tmux hiccup; offer nothing
        return []
    return [
        r for r in rows
        if str(r.get("status") or "") != "suspended" and not r.get("archived")
    ]


def _pin_session_candidates(cfg, slug: str, binding: dict) -> list[dict]:
    """`_pinnable_sessions` ordered most-relevant-first: the session already
    pinned, then the one working THIS topic's ticket (a T-0660 per-task topic
    binds a ``ticket_id``), then the rest — so the obvious choice is the first
    button rather than somewhere down a list of look-alike SIDs."""
    current = binding.get("session_id")
    ticket_id = binding.get("ticket_id")

    def rank(row: dict) -> tuple[int, str]:
        sid = str(row.get("sid") or "")
        if current and sid == current:
            return (0, sid)
        if ticket_id and row.get("task_id") == ticket_id:
            return (1, sid)
        return (2, sid)

    return sorted(_pinnable_sessions(cfg, slug), key=rank)


def _resolve_pin_target(cfg, slug: str, arg: str) -> Optional[str]:
    """Resolve a `/pin-session` argument to a live SID of ``slug``.

    Accepts a full SID (what the picker buttons send) or a T-0662 session
    alias. Returns ``None`` when it names nothing pinnable — the caller
    refuses and re-offers the picker rather than binding the topic to a
    session that can't receive anything."""
    from bot_squad_worker import session_aliases
    candidate = arg.strip()
    if not candidate.lower().startswith("s-"):
        resolved = session_aliases.resolve_alias(cfg.data_dir, candidate)
        if not resolved:
            return None
        candidate = resolved
    live = {str(r.get("sid") or "") for r in _pinnable_sessions(cfg, slug)}
    return candidate if candidate in live else None


def _describe_session(row: dict) -> str:
    """One picker line: the SID plus just enough to tell look-alikes apart."""
    bits = [str(row.get("sid") or "")]
    role = str(row.get("role") or "")
    if role:
        bits.append(role)
    task_id = str(row.get("task_id") or "")
    if task_id:
        bits.append(task_id)
    bits.append(str(row.get("status") or ""))
    return "  ·  ".join(b for b in bits if b)


def _ask_which_session(cfg, chat_id: str, slug: str, binding: dict, *,
                       thread_id: Any = None) -> dict:
    """The picker: a reply-keyboard of `/pin-session <sid>` buttons (plus an
    ``off`` button back to attendant routing). Best-effort, like every other
    interactive reply here."""
    rows = _pin_session_candidates(cfg, slug, binding)
    if not rows:
        _notify(cfg, chat_id,
                f"No live sessions in {slug} to pin right now — messages in "
                "this topic keep going through the attendant.",
                thread_id=thread_id)
        return {"ok": False, "action": "pin_session_no_candidates", "slug": slug}
    current = binding.get("session_id")
    lines = [f"Which session should this topic talk to directly? (project {slug})"]
    lines.append(
        f"Now: {current} (direct)" if current
        else "Now: the user-conversation attendant (default)"
    )
    lines.append("")
    lines += [_describe_session(r) for r in rows]
    keyboard = [[{"text": f"/pin-session {r['sid']}"}] for r in rows]
    keyboard.append([{"text": "/pin-session off"}])
    _channel_notify(
        cfg, chat_id, "\n".join(lines),
        reply_markup={
            "keyboard": keyboard,
            "one_time_keyboard": True,
            "resize_keyboard": True,
        },
        thread_id=thread_id,
    )
    return {"ok": True, "action": "pin_session_ask", "slug": slug,
            "count": len(rows)}


def _unpin_previous(cfg, chat_id: str, binding: dict) -> None:
    """Drop the topic's previous direct-mode pin, if any.

    A stale pin is worse than no pin: it is the topic's VISIBLE claim about
    where messages go, so it must not survive a re-point or an ``off``.
    Best-effort — a failed unpin never blocks the routing change."""
    old = binding.get("pinned_message_id")
    if not old:
        return
    try:
        from bot_squad_worker.actions import _get_tg_client
        _get_tg_client(cfg).unpin_message(chat_id=chat_id, message_id=int(old))
    except Exception as e:  # noqa: BLE001
        log.warning("tg_listener: could not unpin previous /pin-session marker "
                    "(chat=%s message_id=%s): %s", chat_id, old, e)


def _handle_pin_session(cfg, chat_id: str, args: str, *,
                        thread_id: Any = None, binding: Optional[dict] = None) -> dict:
    """``/pin-session [<sid>|<alias>|off]`` — the per-topic direct-mode toggle.

    Bare: reply with the session picker. With a session: point this topic's
    binding at it, then send a confirmation into the topic and PIN it (the
    stakeholder's "the message about this should get pinned in the topic" — the
    topic's own visible marker of where it routes). With ``off``: back to the
    attendant, unpinning the marker.
    """
    from bot_squad_worker import tg_bindings
    if binding is None:
        binding = tg_bindings.resolve(cfg, chat_id, thread_id)
    if not binding:
        # No binding = no project = no candidate set, and inventing one is the
        # exact silent mis-slugging T-0693 removed. Refuse, and say how to fix.
        _notify(cfg, chat_id,
                "/pin-session works in a topic that's bound to a project — "
                "this one isn't bound yet. Ask an admin to run `bsq topic bind` "
                "for it first.",
                thread_id=thread_id)
        return {"ok": False, "action": "pin_session_unbound"}
    slug = binding["slug"]
    arg = args.strip()
    if not arg:
        return _ask_which_session(cfg, chat_id, slug, binding, thread_id=thread_id)

    if arg.lower() in _PIN_SESSION_OFF:
        _unpin_previous(cfg, chat_id, binding)
        tg_bindings.set_direct_session(cfg, chat_id, thread_id, None)
        _notify(cfg, chat_id,
                f"Direct mode off. Messages in this topic go back through the "
                f"{slug} attendant (the default).",
                thread_id=thread_id)
        return {"ok": True, "action": "pin_session_cleared", "slug": slug}

    sid = _resolve_pin_target(cfg, slug, arg)
    if not sid:
        _notify(cfg, chat_id,
                f"No live session {arg!r} in {slug} — nothing pinned. Pick one:",
                thread_id=thread_id)
        _ask_which_session(cfg, chat_id, slug, binding, thread_id=thread_id)
        return {"ok": False, "action": "pin_session_unknown", "slug": slug,
                "arg": arg}

    # Routing first, marker second: the pin is a visible label for a change
    # that must hold even if TG refuses the pin (see below).
    _unpin_previous(cfg, chat_id, binding)
    tg_bindings.set_direct_session(cfg, chat_id, thread_id, sid)
    text = (
        f"📌 Direct mode: this topic now talks straight to {sid} ({slug}).\n"
        "Messages here are injected into that session instead of going through "
        "the user-conversation attendant. /pin-session off returns to the "
        "attendant."
    )
    pin = _send_and_pin(cfg, chat_id, text, thread_id=thread_id)
    if pin.get("message_id"):
        tg_bindings.set_direct_session(
            cfg, chat_id, thread_id, sid,
            pinned_message_id=int(pin["message_id"]),
        )
    if pin.get("sent") and not pin.get("pinned"):
        # The routing IS live; only the marker is missing. Say which, and why —
        # the usual cause is the bot missing the can_pin_messages admin right.
        _notify(cfg, chat_id,
                "(Direct mode is on, but I couldn't pin the message — I likely "
                "need the 'Pin messages' admin right in this group.)",
                thread_id=thread_id)
    return {"ok": True, "action": "pin_session_set", "slug": slug, "sid": sid,
            "pinned": bool(pin.get("pinned"))}


def _send_and_pin(cfg, chat_id: str, message: str, *, thread_id: Any = None) -> dict:
    """Send ``message`` into the topic and pin it (T-0677).

    Goes straight to the TG client rather than through `_channel_notify`:
    pinning is a Telegram-specific affordance with no counterpart on the other
    channels (MAX has none), and the caller needs the message_id back. A
    transport failure is swallowed like every other outbound reply here — the
    binding change has already been persisted."""
    if not cfg.tg_bot_token:
        return {"sent": False, "message_id": None, "pinned": False, "pin_error": ""}
    try:
        from bot_squad_worker.actions import _get_tg_client
        return _get_tg_client(cfg).send_and_pin(
            chat_id=chat_id, text=message, topic_id=thread_id,
        )
    except Exception as e:  # noqa: BLE001 — best-effort; never break routing
        log.warning("tg_listener: /pin-session confirmation to %s dropped: %s",
                    chat_id, e)
        return {"sent": False, "message_id": None, "pinned": False,
                "pin_error": str(e)}


def _handle_topic_bound(cfg, chat_id: str, gid: str, binding: dict, msg: dict) -> dict:
    """T-0639/T-0660: an unquoted message arriving in a BOUND forum topic.

    The topic binding IS the routing signal (D-0055 §2 step 1) — the
    strongest, zero-ambiguity one there is — so this bypasses the sticky-pin
    resolution `_handle_unquoted` does for the DM firehose entirely.
    Unrecognized senders keep the same skip as the rest of the listener (no
    identity to anchor on).

    T-0660: a binding carrying a ``session_id`` is a per-TASK topic (not a
    project/General one) — it routes straight to that ORIGINATING session by
    id (reusing the same inject_input path an explicit ``[<sid>]`` reply
    uses), never through the project's user-conversation attendant, and does
    NOT update the conversation locus (T-0667) — a task-topic message must
    never redirect the project's own attendant-reply relay into the task
    topic. A plain project/General binding (no session_id) keeps the T-0639
    behavior: durable append (T-0489) + ensure-session (T-0485) + locus.

    The stakeholder replying directly to a session is ALSO recorded as a
    passive FYI append into the project's OWN (slug, gid) attendant thread
    (T-0660 mechanic #3) — so the attendant keeps full context of what was
    said without treating it as its own actionable inbox item (wake
    suppressed by the ``fyi`` marker, see ``append_conversation_fyi``)."""
    if not gid:
        return {"ok": True, "action": "skip", "reason": "not a reply or command"}
    slug = binding["slug"]
    session_id = binding.get("session_id")
    if session_id:
        text = msg.get("text") or ""
        result = _handle_reply(cfg, chat_id, session_id, text)
        result["action"] = f"task_topic_{result.get('action', 'inject')}"
        result["slug"] = slug
        append_conversation_fyi(
            cfg, slug, gid, author="user",
            text=f"Пользователь ответил сессии {session_id} напрямую: {text}",
        )
        return result
    # T-0676 items 3/6: isolate this bound topic's record + attendant-read
    # from the rest of the project's (mixed) history — see append_conversation
    # / _ensure_user_conversation / conversation_locus docstrings.
    bound_thread_id = msg.get("message_thread_id")
    # T-0693 Finding B: reaching this function AT ALL means `binding` resolved
    # (tg_bindings.resolve found an entry) — so `bound_thread_id is None` here
    # means the MATCHED binding is an explicit General-feed one (thread_id=
    # None, bound ON PURPOSE), not "no thread info available" the way it would
    # mean for a genuine DM in `_handle_unquoted`. Mark it so the two stay
    # distinguishable in the durable record/locus (see their docstrings), and
    # surface the one coexistence risk that isn't just cosmetic (a General-feed
    # binding sharing a slug with per-topic bindings) loudly instead of never.
    is_general_feed = bound_thread_id is None
    if is_general_feed:
        _warn_if_general_feed_collides(cfg, slug, chat_id)
    append_conversation(cfg, slug, gid, msg, thread_id=bound_thread_id, general_feed=is_general_feed)
    # T-0667: remember where this landed so an OUTGOING reply follows the
    # same chat/topic instead of falling back to the project's static DM.
    from bot_squad_worker import conversation_locus
    conversation_locus.set_locus(cfg, slug, gid, chat_id, bound_thread_id, general_feed=is_general_feed)
    message_ref = _msg_ts(msg)
    ensured = _ensure_user_conversation(cfg, slug, gid, message_ref, thread_id=bound_thread_id)
    if isinstance(ensured, dict) and ensured.get("parked"):
        # T-0570 parity: a spawn refused under backoff/saturation must still
        # tell the user, not go silent (see _handle_unquoted's identical case).
        # T-0676 item 3: land it back in the SAME topic the message arrived
        # on, not the chat's general feed.
        _channel_notify(
            cfg, chat_id,
            "Принял и записал. Сейчас все воркеры заняты — займусь, как только "
            "освободится слот (обычно пара минут).",
            thread_id=msg.get("message_thread_id"),
        )
        return {"ok": True, "action": "route_parked", "slug": slug}
    return {"ok": True, "action": "route_bound_topic", "slug": slug}


def _handle_unquoted(cfg, chat_id: str, chat_slug: str, gid: str, msg: dict) -> dict:
    """An unquoted (non-reply, non-command) message — the firehose dump path.

    Resolves the PROJECT-OF-RECORD (the pinned project is the routing authority,
    T-0492; the chat slug is incidental) and lands BOTH the durable record
    (T-0489) and the attending user-conversation session (T-0485/T-0478) on that
    SAME project, so the dump isn't lost to a session reading a different thread.
    Asks which project when unpinned. Unrecognized senders keep the pre-T-0492
    skip (no identity to anchor routing on). A message in a BOUND forum topic
    never reaches here — `handle_update` routes it via `_handle_topic_bound`
    instead (T-0639: the topic binding outranks this pin-based resolution)."""
    if not gid:
        return {"ok": True, "action": "skip", "reason": "not a reply or command"}
    sticky = get_current_project(cfg, gid)
    if not sticky:
        # Unpinned: hardwired ask (voice-04). Record under the chat's project so
        # the message isn't lost while we wait for the pin.
        append_conversation(cfg, chat_slug, gid, msg)
        _ask_which_project(cfg, chat_id, thread_id=msg.get("message_thread_id"))
        return {"ok": True, "action": "ask_project"}

    # The pinned project is the project-of-record.
    por = sticky

    # T-0666: a message explicitly mentioning a different project (T-0494's
    # _detect_cross_project_target) no longer intercepts routing with a
    # confirm prompt — it just routes to the project-of-record below, same as
    # any other unquoted message.

    # T-0489 + T-0485: record the dump under the project-of-record, then hand it
    # to the (continued-or-spawned) user-conversation session on the SAME
    # project. message_ref points at that just-appended store record (its
    # timestamp keys it in the (por, gid) thread).
    #
    # T-0693 Finding B (case "thread_id dropped/omitted by mistake"): the
    # SAME real thread_id must reach append_conversation, conversation_locus,
    # AND _ensure_user_conversation below — not just the locus call. Before
    # this fix, append_conversation's call here omitted it while the locus
    # call two lines down did not, so the durable record landed in the bare
    # per-user file while the locus claimed a real thread — exactly the
    # append/locus mismatch the live T-0693 incident needed a manual store
    # backfill to untangle. (The only way this function still sees a
    # non-None thread_id at all is a legacy chat with native TG topics that
    # has never used tg_bindings — any ALREADY topic-routed chat's unbound
    # topics are now held earlier in handle_update, never reaching here.)
    bound_thread_id = msg.get("message_thread_id")
    append_conversation(cfg, por, gid, msg, thread_id=bound_thread_id)
    # T-0667: remember where this landed so an OUTGOING reply follows the
    # same chat/topic instead of falling back to the project's static DM.
    from bot_squad_worker import conversation_locus
    conversation_locus.set_locus(cfg, por, gid, chat_id, bound_thread_id)
    message_ref = _msg_ts(msg)
    ensured = _ensure_user_conversation(cfg, por, gid, message_ref, thread_id=bound_thread_id)
    if isinstance(ensured, dict) and ensured.get("parked"):
        # T-0570: spawn refused under backoff/saturation. The message IS durably
        # recorded and the spawn retries on ramp-up — but the user must hear
        # that, not silence. Debounce stays ON so a burst during saturation
        # yields one notice per cooldown, not one per message.
        # T-0676 item 3: preserve the originating thread (a legacy per-project
        # supergroup can still carry topics even without a T-0639 binding).
        _channel_notify(
            cfg, chat_id,
            "Принял и записал. Сейчас все воркеры заняты — займусь, как только "
            "освободится слот (обычно пара минут).",
            thread_id=msg.get("message_thread_id"),
        )
        return {"ok": True, "action": "route_parked", "slug": por}
    return {"ok": True, "action": "route", "slug": por}


def _voice_reject_text(cfg, reason: str, out: dict) -> str:
    """T-0586: user-facing refusal text naming the real cause + remedy. The
    pre-fix generic "не удалось распознать — попробуйте ещё раз" was a lie for
    ``too_long`` (a retry of the same note can never pass the cap)."""
    if reason == "too_long":
        cap = int(getattr(cfg, "voice_max_duration_sec", 300) or 0)
        dur = int(out.get("duration") or 0)
        return (
            f"⚠️ Голосовое ({dur // 60}:{dur % 60:02d}) длиннее лимита "
            f"{cap // 60} мин — оно НЕ обработано и содержимое не сохранилось. "
            "Отправь его частями покороче или напиши текстом."
        )
    if reason == "too_big":
        dur = int(out.get("duration") or 0)
        return (
            f"⚠️ Голосовое ({dur // 60}:{dur % 60:02d}) больше 20МБ — Telegram не "
            "отдаёт ботам такие файлы, повторная отправка НЕ поможет. "
            "Надиктуй частями (до ~15 мин каждая — заведомо проходит)."
        )
    if reason == "download_failed":
        return "⚠️ Не удалось скачать аудио из Telegram — отправь голосовое ещё раз."
    if reason == "transcription_timeout":
        return (
            "⚠️ Распознавание не уложилось в лимит времени — попробуй ещё раз "
            "или отправь запись покороче."
        )
    return (
        "⚠️ Не удалось распознать голосовое сообщение — попробуйте ещё раз "
        "или напишите текстом."
    )


def _record_voice_rejection(cfg, chat_slug: str, gid: str, msg: dict, reason: str, out: dict) -> None:
    """T-0586 no-drop: a refused voice note must still leave a record in the
    conversation thread — duration + reason + the voice attachment descriptor
    (file_id) — so the attendant can see the drop and follow up, and the
    file_id makes late recovery from TG possible at all. Without this the
    thread is blind to the refusal (the 2026-07-05 376s incident: 6 minutes of
    stakeholder direction gone with only a TG error toast). Best-effort, same
    as append_conversation itself."""
    marker = dict(msg)
    dur = int(out.get("duration") or (msg.get("voice") or {}).get("duration") or 0)
    marker["text"] = (
        f"[голосовое {dur} сек НЕ обработано: {reason} — содержимое не транскрибировано]"
    )
    por = get_current_project(cfg, gid) or chat_slug
    append_conversation(cfg, por, gid, marker)


def _handle_private_voice(cfg, chat_id: str, chat_slug: str, gid: str, msg: dict) -> dict:
    """T-0569: a DM voice note. Transcribes (voice_intake.transcribe_only —
    NOT process_voice, so it never becomes a feedback artifact), echoes the
    transcript back (undebounced), then hands the transcript to the SAME
    routing as an unquoted text message (sticky-project pin +
    ensure_user_conversation, via ``_handle_unquoted``) so it lands in the
    per-(project, user) conversation thread with the voice attachment
    descriptor preserved (``msg`` still carries ``msg["voice"]``; only its
    ``text`` is overridden to the transcript before handing off — so
    ``_handle_unquoted``'s own ``append_conversation`` records the transcript,
    not the empty raw-voice text, and there is no double-append).

    A transcription failure (cap exceeded / download / decode) is reported back
    to the user with the ACTUAL reason (T-0586: an over-cap refusal telling the
    user "не удалось распознать — попробуйте ещё раз" sent them retrying a note
    that would never pass), and the refusal leaves a thread record carrying
    duration + reason + the voice attachment (file_id) — so the attendant sees
    the drop and late recovery stays possible. Nothing is routed (no
    transcript to route)."""
    from bot_squad_worker import voice_intake as _vi
    out = _vi.transcribe_only(cfg, chat_slug, msg)
    if not out.get("ok"):
        reason = out.get("reason", "transcription_failed")
        _notify(cfg, chat_id, _voice_reject_text(cfg, reason, out))
        _record_voice_rejection(cfg, chat_slug, gid, msg, reason, out)
        return {"ok": False, "action": "voice_private_failed", "reason": reason}

    transcript = out["transcript"]
    _echo_transcript(cfg, chat_id, transcript)

    transcript_msg = dict(msg)
    transcript_msg["text"] = transcript
    result = _handle_unquoted(cfg, chat_id, chat_slug, gid, transcript_msg)
    result["action"] = f"voice_private_{result.get('action', '')}"
    result["transcript"] = transcript
    return result


def handle_update(cfg, update: dict) -> dict:
    """Dispatch one update. Returns a small audit dict."""
    msg = update.get("message")
    if not msg:
        return {"ok": True, "action": "skip", "reason": "no message"}

    # T-0682: bot/service-authored messages never carry human intent — bail
    # out before any allowlisting or identity-linking work (that linking is
    # exactly what minted a GlobalUser for our own bot in the echo-loop bug).
    if _is_ignorable_service_message(msg, cfg):
        return {"ok": True, "action": "skip", "reason": "bot or service message"}

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    thread_id = msg.get("message_thread_id")

    from bot_squad_worker import tg_bindings

    # T-0639: Allowlist: registered tg_chat values across projects, PLUS any
    # chat_id with a runtime topic binding (the stakeholder's forum supergroup
    # is not any project's static tg_chat, so it must be admitted separately).
    allowed_chats = {str(p.tg_chat) for p in cfg.projects.values()} - {"0"}
    allowed_chats |= tg_bindings.bound_chat_ids(cfg)
    if chat_id not in allowed_chats:
        _record_unknown_chat(cfg, chat_id, chat)
        return {"ok": True, "action": "skip", "reason": f"chat {chat_id} not allowlisted"}

    # T-0639: the topic binding is a stronger, zero-ambiguity signal than the
    # static tg_chat map (D-0055 §2 step 1) — consulted FIRST, falling back to
    # the legacy `_slug_for_chat` so existing per-project chats are unchanged.
    binding = tg_bindings.resolve(cfg, chat_id, thread_id)
    is_general_feed = thread_id is None
    # T-0700: the General feed (thread_id=None, no binding) has the exact same
    # failure mode as an unbound topic below — but ONLY in a chat whose bound
    # topics span MORE THAN ONE distinct project slug (a genuinely
    # multi-project shared forum, the live incident's topology). A chat that
    # hosts exactly one project's topics is already functionally correct via
    # the static-default fallback further down, so gating this unconditionally
    # would add hold+prompt friction to every single-project chat's General
    # tab for zero benefit (operator policy call, T-0700 progress notes).
    general_feed_multi_project = (
        is_general_feed and len(tg_bindings.bound_topic_slugs(cfg, chat_id)) > 1
    )
    if binding is None and chat_id in tg_bindings.bound_chat_ids(cfg) and (
        thread_id is not None or general_feed_multi_project
    ):
        # T-0693 (+ T-0700 for the General-feed branch above): this chat
        # already participates in per-topic routing (it has at least one
        # OTHER bound topic), but THIS (chat_id, thread_id) was never bound.
        # The OLD unconditional fallback below (`... or _slug_for_chat(...)`)
        # would silently impersonate the chat's static default project for
        # it — exactly the live incident (a stakeholder's watchrobot question
        # in a freshly-created, not-yet-bound topic got mis-slugged into
        # bot-squad's conversation store with zero error signal). Loud +
        # held: log a grep-able warning, tell the sender, and hold the
        # message unrouted rather than guessing a project for it. A chat that
        # has NEVER used per-topic binding at all (not in bound_chat_ids)
        # keeps the exact pre-T-0693 fallback below, unaffected.
        _record_unbound_topic(cfg, chat_id, thread_id, chat, is_general_feed=is_general_feed)
        if is_general_feed:
            _channel_notify(
                cfg, chat_id,
                "Это форум с несколькими проектами, а General (без темы) не "
                "привязан ни к одному из них, поэтому сообщение не "
                "маршрутизировано. Ответьте в теме нужного проекта или "
                "попросите админа привязать General командой `bsq topic bind`.",
                thread_id=thread_id,
            )
        else:
            _channel_notify(
                cfg, chat_id,
                "Эта тема ещё не привязана ни к одному проекту, поэтому "
                "сообщение не маршрутизировано. Попросите админа выполнить "
                "`bsq topic bind` для этой темы.",
                thread_id=thread_id,
            )
        return {
            "ok": True, "action": "unbound_topic_held",
            "reason": f"chat {chat_id} thread {thread_id} has no binding",
        }
    chat_slug = (binding.get("slug") if binding else None) or _slug_for_chat(cfg, chat_id)

    # T-0488: recognize the TG sender as a cross-server GlobalUser (link on first
    # contact). Best-effort + env-gated, so inbound routing below is never
    # blocked by linkage. Surfaced on the audit dict as (slug, global_user_id)
    # for the downstream user-conversation seam.
    identity = resolve_or_link_sender(cfg, msg, chat_slug)
    gid = identity.get("global_user_id") if identity else ""

    # T-0634: affirm free-form steering on FIRST contact — a stakeholder must
    # never have to guess whether typing a request/feedback/steering comment
    # here "just works". Fires once per sender (gated on the linkage layer's
    # own created=True, so a recycled/resumed session never re-sends it).
    # Best-effort via _channel_notify (same as every other reply here) — a
    # send failure must not block inbound routing.
    if identity and identity.get("created"):
        _channel_notify(
            cfg, chat_id,
            "👋 First message from you here. Quick note: you don't need a "
            "command for this — any request, feedback, or steering comment "
            "typed directly in this chat is recorded and routed to the "
            "project. /help lists the extra commands, but plain text is the "
            "normal way to talk to it.",
        )

    slash = extract_slash_command(msg)
    reply = extract_reply_target(msg) if not slash else None
    # T-0386 Phase 2 / T-0569: a voice message. Flag-off-safe: gated on
    # [voice].enabled (default off) so deploying the voice code is a no-op
    # until the 1-time stakeholder TG setup flips it on. T-0569 splits the
    # enabled case by chat type: a GROUP/topic voice note keeps the original
    # transcribe->feedback-artifact behavior (process_voice); a PRIVATE (DM)
    # voice note instead routes through the same path as typed text (the
    # conversation store + ensure_user_conversation, voice-04 continuity) — a
    # DM is a conversation with the bot, not feedback.
    chat_type = str(chat.get("type") or "")
    voice_present = bool(not slash and not reply and msg.get("voice") and getattr(cfg, "voice_enabled", False))
    is_voice_group = voice_present and chat_type != "private"
    is_voice_private = voice_present and chat_type == "private"

    if slash or reply or is_voice_group:
        # T-0489: record reply/group-voice under the chat's project — the slug
        # is incidental for these (they don't sticky-route). The unquoted
        # firehose path (below, including private voice) resolves the
        # project-of-record itself and records there, so its dump and attending
        # session land on the SAME project (TL-D, T-0492).
        #
        # T-0659: do NOT append SLASH commands. /project, /state, /sessions,
        # /say, /help are pure control/routing, not project-directed content.
        # Appending a bare command into the statically-mapped (_slug_for_chat)
        # project's store — the "incidental" slug the pin design says NOT to
        # trust — spuriously wakes THAT project's user-conversation attendant
        # (the append endpoint auto-wakes on any user-authored append, T-0631),
        # which sees a contextless "/project" and replies with a confused
        # clarification it can't act on (the slash-routing context lives only
        # here, never in the store). The stakeholder hit this every time he
        # used /project to switch.
        if gid and not slash:
            append_conversation(cfg, chat_slug, gid, msg)
        if slash:
            cmd, args = slash
            # T-0492: /project pins/switches the user's current project (needs
            # the sender identity, which _handle_slash doesn't carry).
            # T-0676 item 3: thread_id (computed above) so the command's reply
            # lands back in the topic it was typed in, not the general feed.
            if cmd == "project":
                result = _handle_project(cfg, chat_id, gid, args, thread_id=thread_id)
            elif cmd == "pin-session":
                # T-0677: needs the TOPIC context (chat_id + thread_id + the
                # binding already resolved above) — it toggles that binding's
                # direct-mode session_id, so it can't live in the
                # identity-less _handle_slash.
                result = _handle_pin_session(
                    cfg, chat_id, args, thread_id=thread_id, binding=binding,
                )
            else:
                result = _handle_slash(cfg, chat_id, cmd, args, thread_id=thread_id)
        elif reply:
            result = _handle_reply(cfg, chat_id, *reply, thread_id=thread_id)
        else:  # group/topic voice
            from bot_squad_worker import voice_intake as _vi
            r = _vi.process_voice(cfg, chat_slug, msg, ts=_msg_ts(msg))
            result = {"ok": r.get("ok", True), "action": "voice", "slug": chat_slug, "result": r}
    elif is_voice_private:
        # T-0569: DM voice — transcribe, echo it back, then route the
        # transcript through the same unquoted-message path as text. This owns
        # its own record (like the unquoted path below), so it does NOT also
        # hit the append_conversation call above (no double-append).
        result = _handle_private_voice(cfg, chat_id, chat_slug, gid, msg)
    elif binding:
        # T-0639: an unquoted message in a BOUND forum topic — the binding IS
        # the routing signal (D-0055 §2 step 1), stronger than the sticky pin,
        # so this bypasses `_handle_unquoted`'s pin-based resolution entirely
        # and routes straight to the bound project via the same durable path.
        result = _handle_topic_bound(cfg, chat_id, gid, binding, msg)
    else:
        # T-0485/T-0494: the unquoted firehose path owns its own record (under
        # the project-of-record) so the dump and the user-conversation session
        # land on the same project.
        result = _handle_unquoted(cfg, chat_id, chat_slug, gid, msg)

    if identity and identity.get("global_user_id"):
        result.setdefault("global_user_id", identity["global_user_id"])
    return result


def _slug_for_chat(cfg, chat_id: str) -> str:
    """Resolve the project slug whose tg_chat == chat_id (allowlist already passed)."""
    for slug, p in cfg.projects.items():
        if str(getattr(p, "tg_chat", "")) == str(chat_id):
            return slug
    return ""


def _msg_ts(msg: dict) -> str:
    """ISO-8601 UTC timestamp for a message — from its TG `date`, else now."""
    from datetime import datetime, timezone
    d = msg.get("date")
    when = (
        datetime.fromtimestamp(d, tz=timezone.utc)
        if isinstance(d, (int, float))
        else datetime.now(timezone.utc)
    )
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _handle_reply(cfg, chat_id: str, sid: str, text: str, *, thread_id: Any = None) -> dict:
    """Find the pane by SID and inject the text."""
    from bot_squad_worker import actions as A
    try:
        result = A.dispatch("inject_input", {"sid": sid, "text": text})
        # T-0155: the stakeholder answered via TG — the agent is no longer
        # blocked on him; cancel any pending stall escalation.
        _clear_stall(cfg, chat_id, sid, thread_id=thread_id)
        return {"ok": True, "action": "inject", "sid": sid, "result": result}
    except A.ActionError as e:
        _notify(cfg, chat_id, f"❌ session {sid} not active — message dropped",
                thread_id=thread_id)
        return {"ok": False, "action": "inject_failed", "sid": sid, "error": str(e)}


def _clear_stall(cfg, chat_id: str, sid: str, *, thread_id: Any = None) -> None:
    """Clear ``sid``'s stall marker in whichever project owns ``chat_id``.

    T-0684 audit: the marker lives at ``data/<slug>/_worker/tg_stall/<sid>.json``
    — slug-scoped — so clearing it requires resolving ``chat_id`` back to a
    slug. The original lookup only checked a project's STATIC ``tg_chat``, but
    ``handle_update``'s own allowlist comment is explicit that a T-0639 bound
    forum topic's ``chat_id`` is "not any project's static tg_chat" — so a
    stall-escalation reply arriving via a bound topic (the same reply path
    ``_handle_reply`` handles) could never clear its marker, and the
    stakeholder would get a redundant "still blocked" re-ping after
    ``tg_stall_minutes`` even though they'd already answered. Also resolve via
    ``tg_bindings`` (the same runtime (chat_id, thread_id)->slug map
    ``handle_update`` consults) so a topic-bound reply clears correctly too."""
    try:
        from bot_squad_worker import tg_bindings, tg_stall as _tg_stall
        slugs = {
            slug for slug, p in cfg.projects.items()
            if str(getattr(p, "tg_chat", "")) == str(chat_id)
        }
        binding = tg_bindings.resolve(cfg, chat_id, thread_id)
        if binding and binding.get("slug"):
            slugs.add(binding["slug"])
        for slug in slugs:
            _tg_stall.clear_blocked(cfg, slug, sid)
    except Exception:  # noqa: BLE001
        pass


def _handle_slash(cfg, chat_id: str, cmd: str, args: str, *, thread_id: Any = None) -> dict:
    """Implement /sessions, /say, /help."""
    from bot_squad_worker import actions as A, sessions as S
    if cmd == "sessions":
        # List all sessions across all registered projects
        rows: list[str] = []
        for slug in cfg.projects:
            for row in S.list_sessions(cfg, slug):
                rows.append(f"  {row['sid']}  win={row['window']}  status={row['status']}")
        body = "Active sessions:\n" + ("\n".join(rows) if rows else "  (none)")
        _notify(cfg, chat_id, body, thread_id=thread_id)
        return {"ok": True, "action": "sessions", "count": len(rows)}

    if cmd == "say":
        # /say <sid> <text>
        parts = args.split(None, 1)
        if len(parts) < 2:
            _notify(cfg, chat_id, "Usage: /say <sid> <text>", thread_id=thread_id)
            return {"ok": False, "action": "say_usage"}
        sid, text = parts[0], parts[1]
        try:
            result = A.dispatch("inject_input", {"sid": sid, "text": text})
            return {"ok": True, "action": "say", "sid": sid, "result": result}
        except A.ActionError as e:
            _notify(cfg, chat_id, f"❌ /say failed: {e}", thread_id=thread_id)
            return {"ok": False, "action": "say_failed", "error": str(e)}

    if cmd == "help":
        _notify(cfg, chat_id,
                "You can just type any request, feedback, or steering "
                "comment directly — no command needed, it's recorded and "
                "routed to the project.\n"
                "Reply to a notification to inject text into the session.\n"
                "/sessions — list active sessions\n"
                "/say <sid> <text> — direct inject without reply-quoting\n"
                "/state — drive on/off, quota target, core lifecycle state\n"
                "/pin-session — (in a bound topic) pick a session to talk to "
                "directly; /pin-session off returns to the attendant",
                thread_id=thread_id)
        return {"ok": True, "action": "help"}

    if cmd == "state":
        # T-0655 (Addendum 1): "нужна команда в чате типа /state, которая
        # будет показывать состояние проекта по основным вещам, особенно
        # drive on, таргет и т.п." — gather the live signals, then _notify
        # (the /sessions pattern), not a static-text dump (the /help pattern).
        from bot_squad_worker import dispatch as _dispatch
        from bot_squad_worker import operator_redrive as _ord

        slug = _slug_for_chat(cfg, chat_id)
        if not slug:
            _notify(cfg, chat_id, "❌ /state: this chat isn't linked to a registered project",
                    thread_id=thread_id)
            return {"ok": False, "action": "state_no_project"}

        live_ops = _dispatch.live_operator_sids(cfg, slug)
        op_sid = live_ops[0] if live_ops else None
        drive = "n/a (no live operator)"
        if op_sid:
            sessions_dir = cfg.data_dir / slug / "sessions"
            md_path = S._find_session_md(sessions_dir, op_sid, None)
            meta = S._read_session_metadata(md_path) if md_path else None
            drive = str((meta or {}).get("drive") or "on")

        pacing = _ord.pacing_status(cfg, slug)
        target = pacing["weekly_target_pct"]
        target_str = f"{target:g}%" if target is not None else "(not set)"
        spend_str = (f"{pacing['spend_pct']:.1f}%"
                     if pacing["spend_pct"] is not None else "unknown")
        active = len(S.list_sessions(cfg, slug))

        body = "\n".join([
            f"Project: {slug}",
            f"Operator: {op_sid or '(none live)'}  drive={drive}",
            f"Weekly quota target: {target_str}  "
            f"(spend so far: {spend_str}, pacing: {pacing['recommendation']})",
            f"Active sessions: {active}",
        ])
        _notify(cfg, chat_id, body, thread_id=thread_id)
        return {"ok": True, "action": "state", "slug": slug, "drive": drive,
                "operator": op_sid}

    return {"ok": False, "action": "unknown_cmd"}


def _notify(cfg, chat_id: str, message: str, *, thread_id: Any = None) -> None:
    """Lightweight outbound command reply -- no SID prefix, no debounce.

    T-0513: routes through the channel abstraction (``channels.get_channel``)
    instead of a raw httpx ``sendMessage`` that bypassed it. The egress proxy,
    token gate, and quiet-hours policy now live in one place (tg.py via the
    channel), not duplicated here.

    ``thread_id`` (T-0676 item 3): the forum-topic thread the triggering
    message arrived on, when known — forwarded straight through so an
    acknowledgment/confirmation lands back in THAT topic instead of the
    chat's general feed. ``None`` (DM, General, or a caller with no msg in
    scope) preserves the exact pre-fix behavior.
    """
    _channel_notify(cfg, chat_id, message, thread_id=thread_id)


def _channel_notify(
    cfg, chat_id: str, message: str, *,
    reply_markup: dict | None = None, thread_id: Any = None,
) -> None:
    """Send an interactive group reply via the channel abstraction (T-0513).

    These are replies to a user actively messaging the bot in its project
    group, so: ``urgent=True`` (never quiet-hours-drop a reply the user just
    asked for), ``sid=""`` (no ``[SID]`` prefix), ``debounce=False`` (echo every
    time, not once per 60s). Best-effort — ``channel.send`` raises on a
    transport/API error and an outage must not break inbound command handling.

    ``thread_id`` (T-0676 item 3 — misrouted reply): before this, every
    synchronous reply/ack sent through this helper dropped the originating
    message's ``message_thread_id`` entirely, so a message typed in a bound
    forum topic got its acknowledgment delivered to the chat's GENERAL feed
    instead of back into that topic — a plain, zero-race misroute (distinct
    from the T-0667 conversation-locus staleness this ticket also covers).
    Passed straight to ``channel.send``'s ``topic_id`` when given.
    """
    if not cfg.tg_bot_token:
        return
    from bot_squad_worker import channels as _channels

    extra: dict[str, Any] = {"debounce": False}
    if reply_markup is not None:
        extra["reply_markup"] = reply_markup
    if thread_id is not None:
        extra["topic_id"] = thread_id
    try:
        _channels.get_channel(cfg, project=_slug_for_chat(cfg, chat_id)).send(
            message, chat_id=chat_id, sid="", urgent=True, **extra
        )
    except Exception as e:  # noqa: BLE001 — best-effort; never break inbound routing
        # T-0586: best-effort must not mean invisible — the 2026-07-05 13.5-min
        # 🎙-echo vanished here (transcript > TG's 4096-char message cap → API
        # 400 → bare pass) and the drop was undiagnosable from the journal.
        log.warning("tg_listener: channel notify to %s dropped: %s", chat_id, e)


# TG rejects messages over 4096 chars (API 400). Echoes of long voice
# transcripts must be split, not silently lost — cut at the cap with headroom
# for the 🎙 prefix + part markers.
_TG_MSG_CAP = 4096
_ECHO_CHUNK = 3900


def _echo_transcript(cfg, chat_id: str, transcript: str) -> None:
    """T-0586: 🎙-echo that survives transcripts longer than one TG message.
    Single send for the common case; a long transcript goes out as numbered
    parts so the user still sees the full recognition."""
    prefix = "\U0001f399 Распознал так: "
    if len(prefix) + len(transcript) + 2 <= _TG_MSG_CAP:
        _channel_notify(cfg, chat_id, f"{prefix}«{transcript}»")
        return
    chunks = [transcript[i:i + _ECHO_CHUNK] for i in range(0, len(transcript), _ECHO_CHUNK)]
    total = len(chunks)
    for n, chunk in enumerate(chunks, 1):
        _channel_notify(cfg, chat_id, f"{prefix}({n}/{total}) «{chunk}»")


def tick(cfg) -> dict:
    """One poll-and-process cycle. Called by the APScheduler job."""
    last_id = _read_last_update_id(cfg)
    updates = poll_updates(cfg, last_id, timeout=25)
    handled = 0
    max_id = last_id
    for update in updates:
        uid = update.get("update_id", 0)
        max_id = max(max_id, uid)
        try:
            handle_update(cfg, update)
            handled += 1
        except Exception:
            # T-0684 audit: this update_id is still folded into max_id above and
            # will be persisted below, so TG's getUpdates offset moves past it
            # regardless — deliberate (a permanently-raising payload must not
            # head-of-line-block every later update, see
            # test_tick_bad_update_does_not_poison_offset). But that means an
            # uncaught exception here is a SILENT, PERMANENT message loss: TG
            # never redelivers an acknowledged offset. Before this fix nothing
            # logged it at all, so a genuine drop was indistinguishable from
            # ordinary attendant latency (exactly the ambiguity the stakeholder
            # hit in the T-0676 retest) — log it so it's at least diagnosable.
            log.exception(
                "tg_listener: handle_update raised on update_id=%s — message "
                "dropped (offset still advances past it)",
                update.get("update_id"),
            )
            continue
    if max_id > last_id:
        _write_last_update_id(cfg, max_id)
    return {"ok": True, "polled": len(updates), "handled": handled, "max_update_id": max_id}
