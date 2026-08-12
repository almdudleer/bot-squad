"""Listen for Telegram updates and route replies into sessions."""
from __future__ import annotations

import json
import logging
import os
import pwd
import re
import tomllib
from pathlib import Path
from typing import Any, Optional

import httpx

from bot_squad_worker import reply_quote
from bot_squad_worker import tg as _tg
from bot_squad_worker import tg_direct_reply

log = logging.getLogger(__name__)

#: A raw routing SID inside the LEADING bracket of a quoted message — the
#: pre-``tg_reply_map`` resolver, kept as a fallback (see
#: :func:`extract_reply_target`). Applied with ``.match``, so the SID must sit
#: in the prefix ``tg._prefix`` renders and never merely somewhere in the body:
#: a session that QUOTES a sid mid-sentence must not become a routing target.
#:
#: The optional inner ``[<slug>] `` group is the T-0719 follow-up. ``_prefix``
#: renders its label as ``[<label>] <text>``, so when the label is itself the
#: nested ``sid_display_label`` bracket form the wire carries a DOUBLE bracket
#: — ``[[bot-squad] S-…-p1] текст``. That shape is reachable today via
#: ``sender_tag.compose``'s except-branch, which falls back to ``_prefix`` with
#: the caller's raw ``sid``, and the previous pattern could not match it: it
#: required the SID immediately after the first ``[``. Both docstrings here
#: claimed the bracket form "keeps resolving" while it silently did not — the
#: forms are now pinned as a table by
#: ``test_tg_listener.py::test_sid_re_resolves_exactly_the_wire_forms``.
SID_RE = re.compile(r"\[(?:\[[^\[\]\n]{1,60}\]\s+)?(S-[A-Za-z0-9_-]+?-p\d+)")

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


def extract_reply_target(message: dict, cfg: Any = None) -> Optional[tuple[str, str]]:
    """Extract (sid, text) if this is a reply to a worker notification.

    T-0719 — resolution order, message-id FIRST:

    1. ``tg_reply_map``: the ``(chat_id, reply_to.message_id)`` the sender
       recorded when it sent the message. Presentation-independent, so it
       survives any change to how the prefix renders. This is the path that
       must carry the common case.
    2. :data:`SID_RE` over the quoted text — the legacy path. Kept as a
       FALLBACK, not deleted: messages sent before the map existed have to
       keep resolving, as does any send whose wire prefix still carries a raw
       SID.

       What that fallback ACTUALLY covers is a table, not a sentence, and it
       is pinned by ``test_sid_re_resolves_exactly_the_wire_forms`` — an
       earlier revision of this docstring named a form (``[<slug>]
       S-...-pNNN``) that the pattern could not match and that no renderer
       emits, so the promise read as broader than the code. Resolving:
       ``[S-…-p1] …``, ``[S-…-p1 @ user] …``, and the double-bracket
       ``[[<slug>] S-…-p1] …`` ``tg._prefix`` renders when handed a nested
       label. NOT resolving, by construction: the compact ``[<slug> <role>]``
       label (no SID exists in the text to find — that IS this regression),
       and a SID anywhere but the leading bracket.

    Why the order matters: the regex was the ONLY path until now, and T-0676
    item 5's compact ``"<slug> <role>"`` label removed the raw SID from the
    text entirely — so every reply silently stopped matching and fell through
    to the attendant. Routing must not read display text to work.

    ``cfg`` is optional so the pure-parse contract (and its callers/tests)
    still holds when there's no data dir on hand; without it only step 2 runs.

    T-0786: the returned ``text`` is ``tg.msg_text`` — the reply's ``text`` OR
    its media ``caption``. This is the SAME defect the ticket is about, on the
    fifth carrier, found by re-scanning the file rather than stopping at the
    four the ticket names: replying to a session's message with a photo and
    «вот это» returned ``(sid, "")``, and `_handle_reply` then refused it as
    «нечего передавать (пустой текст)» — his words were not merely recorded
    empty here, they were never delivered at all. Only the REPLY's own words
    change; the quoted message is still read by `reply_quote`, which has always
    handled captions.
    """
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return None
    text = _tg.msg_text(message).strip()

    data_dir = getattr(cfg, "data_dir", None)
    if data_dir is not None:
        try:
            from bot_squad_worker import tg_reply_map

            chat_id = (message.get("chat") or {}).get("id")
            if chat_id is None:
                chat_id = (reply_to.get("chat") or {}).get("id")
            sid = tg_reply_map.lookup(
                data_dir, chat_id=chat_id, message_id=reply_to.get("message_id"),
            )
            if sid:
                return (sid, text)
        except Exception:  # noqa: BLE001 — never lose the regex fallback to a store hiccup
            log.exception("extract_reply_target: reply-map lookup failed")

    quoted = reply_to.get("text") or ""
    m = SID_RE.match(quoted)
    if not m:
        return None
    return (m.group(1), text)


def extract_slash_command(message: dict) -> Optional[tuple[str, str]]:
    """Extract (cmd, args_str) if this is a /sessions, /say, /help, /project,
    /state, /pin-session or /remote-control command."""
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
    # T-0300: same dash/underscore split for `/remote-control` — the
    # stakeholder's verbatim (T-0155) spells it with a dash, TG autocomplete
    # can only offer the underscore. Both normalize to the dashed name.
    if cmd == "remote_control":
        cmd = "remote-control"
    # T-0655 (Addendum 1): /state — drive on/off, quota target, lifecycle state.
    if cmd not in {"sessions", "say", "help", "project", "state", "pin-session",
                   "remote-control"}:
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


#: T-0782: why a photo descriptor can carry no handle. Named rather than
#: boolean/absent for the same reason as ``tg.UNKNOWN_*``: "there was a photo
#: and we could not keep its id" is a different fact from "there was a photo",
#: and a reader must not have to guess which one an id-less record means.
PHOTO_UNKNOWN_NOT_A_LIST = "photo-not-a-list"        # TG sent something else
PHOTO_UNKNOWN_NO_FILE_ID = "no-file-id-in-variants"  # variants, none with an id

#: T-0787: the same fact for the media types TG delivers as ONE object rather
#: than an array of variants. Same naming style, same reason — an id-less
#: record must never read as one whose id we dropped.
MEDIA_UNKNOWN_NOT_AN_OBJECT = "media-not-an-object"  # TG sent something else
MEDIA_UNKNOWN_NO_FILE_ID = "no-file-id-in-object"    # an object, no id in it

#: T-0787: every media key TG sends as a single object carrying a ``file_id``,
#: IN RECORDING ORDER. ``photo`` is deliberately absent — it arrives as an
#: ARRAY of PhotoSize variants and needs ``_largest_photo``'s choice, so it
#: keeps its own branch below.
#:
#: ``voice`` and ``document`` lead because they are the pre-T-0787 order and a
#: message carrying several media keeps its existing record byte-for-byte. The
#: five after them are the ones this ticket adds: before it, a video, a
#: sticker, an animation, a video note or an audio file produced NO descriptor
#: at all — the durable thread said nothing arrived, and an uncaptioned one
#: woke a session with the empty verbatim fence T-0785 exists to eliminate.
#:
#: A TABLE, not five more branches, on purpose. The defect this fixes IS the
#: repo's top bug class — a function whose branches were extended one media
#: type at a time until the ones nobody revisited silently dropped everything.
#: Adding a parallel mechanism for the new types would rebuild exactly that.
#:
#: NOT HERE, and not by oversight: ``location``, ``contact``, ``venue``,
#: ``poll``, ``dice`` carry NO ``file_id``. What their handle would even be is
#: a separate judgement call and it does not belong in this diff.
MEDIA_FILE_ID_KEYS = (
    "voice", "document", "video", "animation", "sticker", "video_note", "audio",
)


def _largest_photo(photo: Any) -> tuple[str, str]:
    """``(file_id, unknown_reason)`` for the LARGEST PhotoSize variant.

    TG sends ``msg["photo"]`` as an ARRAY of PhotoSize variants — the same
    image at several resolutions, thumbnail first — so keeping "the" file_id
    is a choice, not a read. **We keep the largest**, deliberately: the handle
    exists so the picture he sent can be fetched later, and a thumbnail is a
    lossy substitute that is indistinguishable from the real thing in the
    record (a silent ``photo[0]`` would look correct in every test). Nothing is
    downloaded here — we store an id, not bytes — so the usual bytes-vs-fidelity
    trade is not being paid at ingestion, and TG has already capped the
    resolution on the sending side. If some later reader wants the cheap
    variant it can ask TG for one; it cannot recover detail we chose to drop.

    "Largest" is measured, not assumed to be last: ``width * height``, then
    ``file_size``, then array order (max keeps the LAST on a tie, which is
    TG's ascending order — so a payload with no dimensions at all still
    degrades to the biggest one rather than the thumbnail).

    Every size read is coerced defensively. This runs on the inbound routing
    path — ``append_conversation``'s callers do NOT wrap it — so a junk
    ``width`` would turn "we picked the wrong variant" into "the message was
    never routed", which is a far worse failure than the one being fixed.
    """
    def _size(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    if not isinstance(photo, (list, tuple)):
        return "", PHOTO_UNKNOWN_NOT_A_LIST
    ranked = [
        (i, v) for i, v in enumerate(photo)
        if isinstance(v, dict) and v.get("file_id")
    ]
    if not ranked:
        return "", PHOTO_UNKNOWN_NO_FILE_ID
    _, best = max(
        ranked,
        key=lambda iv: (
            _size(iv[1].get("width")) * _size(iv[1].get("height")),
            _size(iv[1].get("file_size")),
            iv[0],
        ),
    )
    return str(best["file_id"]), ""


def _msg_attachments(msg: dict) -> list[dict]:
    """Light attachment descriptors for the conversation record. We keep just
    enough to know an attachment was present (type + file_id) — the binary lives
    in TG, not the thread. Voice is the live case (T-0386); photos/documents are
    recorded generically so the thread isn't silently lossy.

    T-0782: a photo used to be recorded as ``{"type": "photo"}`` — the bare
    FACT, six lines below the voice branch that keeps its handle. So the record
    truthfully said "there was a photo" while the one thing that could ever
    fetch it was thrown away at ingestion, permanently: across the whole
    conversation store all 51 attachment records were voice, because a photo
    produced a record with nothing to act on. This captures the handle. It does
    NOT make the image retrieved — there is no download path behind photos the
    way ``voice_intake`` sits behind voice — so what changes here is only that
    the data stops being destroyed on arrival.

    T-0787, the unfixed twin of that fix: this function recognised exactly
    three keys, so a ``video``, ``animation``, ``sticker``, ``video_note`` or
    ``audio`` returned ``[]`` — NO descriptor. That is worse than the missing
    field T-0782 fixed. A missing handle is lossy but honest; ``[]`` makes the
    durable thread say **nothing arrived**, which is simply false, and it makes
    T-0785's ATTACHED section never fire, so an UNCAPTIONED video sent into a
    session-bound task topic still woke the session with the empty verbatim
    fence T-0785 was written to eliminate. Measured that way on the full
    inbound path against e243dae, with a photo through the same probe as the
    control: photo produced a descriptor in the same run, video produced none.

    See ``MEDIA_FILE_ID_KEYS`` for what is covered, why it is a table rather
    than five more branches, and which types are deliberately left out.

    THE ID-LESS CASE IS UNIFORM, and that is the point of doing it here rather
    than beside the existing branches: any covered key that arrives malformed
    or without a handle KEEPS THE FACT and names why it has none
    (``MEDIA_UNKNOWN_*``), exactly as a photo does. This also reaches ``voice``
    and ``document``, whose id-less payloads used to vanish silently — the same
    defect, in the two branches that already existed. It changes nothing for
    any real message: TG always sends a ``file_id``, so a genuine voice note's
    descriptor is byte-identical to its pre-T-0787 one, and TRANSCRIPTION never
    reads this function at all (``voice_intake`` takes ``msg["voice"]``
    directly). A falsy value — absent key, ``{}`` — writes no descriptor, the
    same "absent stays absent" rule an empty ``photo`` array follows.

    Nothing here downloads anything, for the new types either. The handle is
    kept so a fetch is POSSIBLE later; a session must still say the media
    arrived and that it cannot open it (``tg_direct_reply.render_attachments``
    owns that wording and needs no change — it renders whatever type it is
    given, which is why no new mechanism was invented for these).
    """
    out: list[dict] = []
    for key in MEDIA_FILE_ID_KEYS:
        raw = msg.get(key)
        if not raw:
            continue
        if not isinstance(raw, dict):
            out.append({"type": key,
                        "file_id_unknown_reason": MEDIA_UNKNOWN_NOT_AN_OBJECT})
            continue
        file_id = str(raw.get("file_id") or "").strip()
        # The media's OWN handle, never a nested `thumbnail`/`thumb` one: a
        # thumbnail is a lossy substitute indistinguishable from the real thing
        # once stored — the same reason `_largest_photo` refuses `photo[0]`.
        out.append({"type": key, "file_id": file_id} if file_id else {
            "type": key, "file_id_unknown_reason": MEDIA_UNKNOWN_NO_FILE_ID})
    if msg.get("photo"):
        # An EMPTY array is falsy and never gets here: no photo arrived, so no
        # descriptor is written. Absent stays absent — it is not a photo whose
        # id we lost. A malformed/id-less one still records the marker (losing
        # the FACT would be a worse trade than the pre-T-0782 behaviour) and
        # says why it has no handle instead of reading as "the id was dropped".
        file_id, unknown = _largest_photo(msg["photo"])
        att = {"type": "photo", "file_id": file_id} if file_id else {
            "type": "photo", "file_id_unknown_reason": unknown}
        out.append(att)
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

    AUTHORSHIP (T-0746 item c). The author is no longer hardcoded ``"user"``.
    ``msg["from"]`` says who SENT the message, which is not the same question as
    who WROTE it, and assuming they were the same is how the live
    ``"❌ session … not active — message dropped"`` notice came to be recorded as
    the stakeholder's own words. ``echo_guard.classify_inbound`` answers the
    right question — see that module for the two rungs and why a shape-only
    validator in the store could never have caught this. Its verdict for a
    genuine, self-composed message is ``author="user"`` with no
    ``forwarded_from``, i.e. exactly this function's pre-T-0746 payload, so
    nothing changes for the overwhelming common case.

    WHAT HE WAS ANSWERING (T-0780). A TG reply used to arrive here as its own
    text and nothing else — the quoted original was read once for the SID regex
    and discarded, and the store had no slot for it, so «второй вариант» was
    recorded against a question nobody could recover. ``reply_to`` carries it as
    a SEPARATE nested field, never concatenated into ``text``: the quoted words
    are somebody else's (usually OURS), and folding them into a record authored
    ``"user"`` is precisely the T-0746 defect. See ``reply_quote``.

    HIS WORDS, WHEN HE TYPED THEM WITH A PICTURE (T-0786). ``text`` now comes
    from ``tg.msg_text``, which reads a media ``caption`` when there is no
    ``text``. This record used to store ``text: ""`` for every captioned photo,
    so the durable thread — the one "we can always look up" (voice-04) — kept
    the attachment descriptor and lost the sentence explaining what it was for.
    Nothing else about the record changes: a caption IS the sender's own words,
    so reading it keeps ``author="user"`` honest, and no placeholder is ever
    substituted when he genuinely typed nothing.

    Best-effort + env-gated: returns ``None`` (no-op, no HTTP) when there's no
    ``global_user_id``, or the API base / worker token aren't configured — so a
    record failure NEVER blocks inbound routing. Returns ``True`` on a recorded
    append."""
    from bot_squad_worker import echo_guard
    verdict = echo_guard.classify_inbound(cfg, msg)
    payload = {
        "author": verdict["author"],
        "text": _tg.msg_text(msg),
        "attachments": _msg_attachments(msg),
        "timestamp": _msg_ts(msg),
        # This listener performs the identity-aware ensure after the durable
        # append. Suppress the API's generic auto-wake or every Telegram
        # message nudges twice (and historically spawned once as coordinator).
        "ensure_attendant": False,
    }
    quote = reply_quote.extract(msg)
    if quote:
        payload["reply_to"] = quote
    if verdict["forwarded_from"]:
        payload["forwarded_from"] = verdict["forwarded_from"]
    if verdict["author"] != "user":
        # T-0755's derivation makes `system:` outbound by default (every system
        # record in this store used to be a notice we DELIVERED). This one
        # REACHED us — it came back in on the inbound channel — and the store's
        # own docstring is explicit that such a record must say so explicitly.
        payload["direction"] = "in"
    if thread_id is not None and str(thread_id).strip() != "":
        payload["thread_id"] = thread_id
    if general_feed:
        payload["general_feed"] = True
    return _post_conversation(cfg, slug, global_user_id, payload)


def _post_conversation(cfg, slug: str, global_user_id: str, payload: dict) -> Optional[bool]:
    """POST one already-built record to the API's token-gated append endpoint.

    The API owns the store (single-writer); the worker POSTs over the same
    worker->API path T-0488 established (httpx, base=MOTHERSHIP_BASE_URL,
    Bearer=WORKER_API_TOKEN) — NOT through the TG egress proxy (this is a
    local-API call, not Telegram traffic).

    Extracted by T-0746 because this ticket adds a THIRD writer with a
    different body shape (the undelivered-message fallback, which appends a
    system context line and then the user's own text). The env-gating and the
    swallow-everything contract are the load-bearing part and must not be
    re-implemented per caller: a record failure never blocks inbound routing.
    Returns ``True`` on a recorded append, ``None`` on a no-op or failure."""
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
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return True


_FYI_PREFIX = "[FYI — ответ не требуется]"


def append_conversation_fyi(cfg, slug: str, global_user_id: str, *, author: str,
                            text: str, reply_to: Optional[dict] = None,
                            thread_id: Any = None) -> Optional[bool]:
    """T-0660: record a PASSIVE, non-actionable append into the project's
    (slug, global_user_id) attendant thread — for either of the two
    session<->stakeholder direct-contact directions the task-topic model adds
    (D-0055 'Topic model evolution' mechanics #3):
      - a dev/TL/orchestrator session wrote directly to the stakeholder
        (``author="session:<sid>"``), or
      - the stakeholder replied directly to a session, bypassing the
        attendant (``author="system:direct-reply"`` — T-0746 corrected this
        from ``"user"``: the record's text is the SYSTEM's summary of what
        happened, wrapping his words, so attributing the whole line to him was
        the same lie item (c) is about, one file over).

    Marked ``fyi=True`` on the API append (T-0660) so the endpoint records it
    for context but suppresses the side effects a normal append of that
    ``author`` would trigger: a ``session:``-authored append normally
    RELAYS back to the user's TG (would echo the "no reply needed" note
    right back to them — wrong); a ``user``-authored append normally WAKES
    the attendant (this isn't its own inbox item to act on). ``text`` is
    prefixed with an unambiguous marker so a reading session never mistakes
    this for something requiring a reply.

    ``reply_to`` (T-0780): the message the user was answering, when this
    summary is about a reply. It rides as the STRUCTURED field rather than
    being appended to ``text`` — this record's text is already system prose
    wrapping his words, and stapling a second person's words onto it is the
    T-0746 shape all over again. It matters here specifically because on the
    direct-mode task-topic path this fyi line is the ONLY store record of what
    he said; without the field that path stays lossy after the fix.

    ``thread_id`` (T-0851): the bound forum topic this reply arrived in, when
    any — mirrors ``append_conversation``'s own param so a direct-reply FYI
    lands in the same isolated topic thread its actionable counterpart would
    have, instead of always falling back to the project's collapsed history.

    Same best-effort/env-gated contract as ``append_conversation``: a no-op
    when unconfigured or on failure, never blocks the caller."""
    payload = {
        "author": author,
        "text": f"{_FYI_PREFIX} {text}",
        "fyi": True,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if thread_id is not None and str(thread_id).strip() != "":
        payload["thread_id"] = thread_id
    return _post_conversation(cfg, slug, global_user_id, payload)


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
    params = {
        "slug": slug,
        "global_user_id": gid,
        "message_ref": message_ref,
    }
    if thread_id is not None and str(thread_id).strip() != "":
        params["thread_id"] = thread_id
    try:
        return _dispatch_user_conversation(cfg, gid, params)
    except _UserConversationActionError as e:
        if "backoff" in str(e):
            return {"ok": False, "parked": True}
        # Say WHICH failure this was rather than collapsing every rejection to
        # a bare None, which callers cannot tell from "no attempt was made"
        # (the explicit-UNKNOWN rule). `unavailable` is the deliberate
        # fail-closed refusal above; the user-facing courtesy line for it
        # belongs at the call site that owns the chat, and that call site is
        # consolidated by T-0640's `_route_to_project` — which sits ABOVE this
        # commit in the branch, so the wording lands there rather than being
        # written three times into code T-0640 deletes.
        log.error("user-conversation ensure refused for %s/%s: %s", slug, gid, e)
        if "unavailable" in str(e):
            return {"ok": False, "user_worker_unavailable": True}
        return None
    except Exception:  # noqa: BLE001 — best-effort; never break inbound routing
        log.exception("ensure_user_conversation failed for %s/%s", slug, gid)
        return None


class _UserConversationActionError(RuntimeError):
    """A rejected local or per-user worker action."""


def _linux_user_for_global_user(cfg: Any, global_user_id: str) -> str:
    """Return the attached account's Linux user, or ``""`` when unknown.

    Telegram polling runs only in the coordinator worker.  A user-conversation
    is nevertheless a tmux action and must execute in the attached user's
    worker; otherwise the coordinator's provider, credentials and tmux server
    leak into that user's project.
    """
    if not str(global_user_id or "").strip():
        # Identity resolution is best-effort and hands back "" when it fails,
        # and MOST accounts have no `attached_to_global_user` at all — so an
        # empty gid compares equal to every unattached entry and the match
        # below would return whichever one auth.toml happens to list first.
        # That is a coin-flip routing decision made by file order; on this
        # install it lands on the coordinator's own account and looks fine.
        # No gid means no user-worker route, so say so.
        return ""
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None:
        # Small test/embedded configs predate account attachment.  With no
        # authority file there is no user-worker route to resolve.
        return ""
    auth_path = Path(config_dir) / "auth.toml"
    try:
        raw = tomllib.loads(auth_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("user-conversation routing: failed to read auth.toml: %s", e)
        return ""
    for username, value in (raw.get("user_meta", {}) or {}).items():
        meta = value if isinstance(value, dict) else {}
        if str(meta.get("attached_to_global_user", "") or "") != global_user_id:
            continue
        linux_user = str(meta.get("linux_user", "") or username).strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", linux_user):
            return linux_user
        log.error("user-conversation routing: invalid linux_user %r", linux_user)
        return ""
    return ""


def _user_worker_timeout() -> float:
    """Client timeout for a user-worker ensure, DERIVED from the callee's own
    wait so the two cannot drift apart again.

    ``ensure_user_conversation`` may SPAWN, and `sessions.spawn` then blocks on
    `_wait_for_agent_composer_ready` for `_COMPOSER_READY_TIMEOUT_SEC` before
    tmux window creation and the agent launch are even counted. A client
    timeout at or below that number can ONLY ever time out on a cold spawn —
    and worse, the spawn on the other side has very likely SUCCEEDED, so the
    caller reports a failure for work that completed. This was hardcoded 10.0
    against a 15.0 wait, i.e. guaranteed to lose that race (found in review by
    p192; the fleet has paid for "a failure signal is not proof the work
    failed" before).

    The +25s covers window creation and agent launch on top of the composer
    wait, with margin. Falls back to the same total if the constant moves out
    of reach, since an over-long timeout costs a slow failure while an
    under-long one costs a false one.
    """
    try:
        from bot_squad_worker.sessions import _COMPOSER_READY_TIMEOUT_SEC as composer
    except Exception:  # noqa: BLE001 — a missing constant must not break routing
        composer = 15.0
    return float(composer) + 25.0


def _worker_action_over_socket(sock_path: Path, params: dict) -> dict:
    """Synchronously call ensure_user_conversation on a user-worker UDS."""
    transport = httpx.HTTPTransport(uds=str(sock_path))
    try:
        with httpx.Client(
            transport=transport, base_url="http://w", timeout=_user_worker_timeout()
        ) as client:
            response = client.post("/actions/ensure_user_conversation", json=params)
    except httpx.RequestError as e:
        raise _UserConversationActionError(
            f"user worker request failed via {sock_path}: {e}"
        ) from e
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except (ValueError, AttributeError):
            detail = response.text
        raise _UserConversationActionError(
            f"user worker rejected ensure_user_conversation: {detail}"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise _UserConversationActionError("user worker returned a non-object response")
    return body


def _dispatch_user_conversation(cfg: Any, gid: str, params: dict) -> dict:
    """Dispatch locally or to the attached user's worker without fallback."""
    from bot_squad_worker import actions as A

    target_user = _linux_user_for_global_user(cfg, gid)
    current_user = pwd.getpwuid(os.geteuid()).pw_name
    if not target_user or target_user == current_user:
        try:
            return A.dispatch("ensure_user_conversation", params)
        except A.ActionError as e:
            raise _UserConversationActionError(str(e)) from e

    sock_path = Path(cfg.data_dir) / "_sock" / f"user-{target_user}.sock"
    if not sock_path.exists():
        # Never fall back to the coordinator: doing so launches the wrong
        # provider with the wrong credentials under the wrong Linux account.
        #
        # LOUD, because this is the deliberate path, not the accident. Refusing
        # in silence is the failure this whole change exists to remove: the
        # message stays durably recorded, but nobody is woken and the user is
        # answered by nothing. Per T-0880 nothing restarts a per-user worker,
        # so "that worker is gone" is live-reachable, not hypothetical — and
        # without this line there is nothing to reconstruct it from.
        log.error(
            "user-conversation NOT delivered: no worker for %s (%s). The "
            "message is recorded but NO attendant was woken — start that "
            "user's worker.",
            target_user,
            sock_path,
        )
        raise _UserConversationActionError(
            f"user worker unavailable for {target_user}: {sock_path}"
        )
    log.info(
        "routing user conversation gid=%s to linux_user=%s via %s",
        gid,
        target_user,
        sock_path,
    )
    return _worker_action_over_socket(sock_path, params)


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


def _session_candidates(cfg, slug: str, binding: dict) -> list[dict]:
    """`_pinnable_sessions` ordered most-relevant-first: the session already
    pinned, then the one working THIS topic's ticket (a T-0660 per-task topic
    binds a ``ticket_id``), then the rest — so the obvious choice is the first
    button rather than somewhere down a list of look-alike SIDs.

    T-0300: shared with `/remote-control`, which wants the same ordering for
    the same reason. An empty ``binding`` (an unbound DM) simply ranks
    everything equal and falls through to the SID sort."""
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


def _resolve_session_arg(cfg, slug: str, arg: str) -> Optional[str]:
    """Resolve a session-naming command argument to a live SID of ``slug``.

    Accepts a full SID (what the picker buttons send) or a T-0662 session
    alias. Returns ``None`` when it names nothing pinnable — the caller
    refuses and re-offers the picker rather than binding the topic to a
    session that can't receive anything.

    Shared by `/pin-session` and (T-0300) `/remote-control`: both take the
    same "which live session of this project" argument and both must reject
    the same dead ones, so they resolve through one function."""
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
    """One picker line: the SID plus just enough to tell look-alikes apart.

    T-0300: a ``slug`` key (which `sessions.list_sessions` never sets — only
    the cross-project `/remote-control` picker adds it) is shown too, since
    that picker mixes projects and the SID alone doesn't say which."""
    bits = [str(row.get("sid") or "")]
    slug = str(row.get("slug") or "")
    if slug:
        bits.append(slug)
    role = str(row.get("role") or "")
    if role:
        bits.append(role)
    task_id = str(row.get("task_id") or "")
    if task_id:
        bits.append(task_id)
    bits.append(str(row.get("status") or ""))
    return "  ·  ".join(b for b in bits if b)


def _offer_session_picker(cfg, chat_id: str, rows: list[dict], *, command: str,
                          lines: list[str], extra_buttons: tuple[str, ...] = (),
                          thread_id: Any = None) -> None:
    """Send a reply-keyboard of ``<command> <sid>`` buttons, one per row.

    T-0300: extracted so `/pin-session` and `/remote-control` share ONE picker.
    Reply-keyboard (not inline buttons) for the T-0677 reason: the poller runs
    ``allowed_updates:["message"]``, so each button has to arrive as a normal
    message — which also means the SID text the button sends is exactly what
    the command's own argument parser accepts."""
    body = list(lines)
    body.append("")
    body += [_describe_session(r) for r in rows]
    keyboard = [[{"text": f"{command} {r['sid']}"}] for r in rows]
    for extra in extra_buttons:
        keyboard.append([{"text": extra}])
    _channel_notify(
        cfg, chat_id, "\n".join(body),
        reply_markup={
            "keyboard": keyboard,
            "one_time_keyboard": True,
            "resize_keyboard": True,
        },
        thread_id=thread_id,
    )


def _ask_which_session(cfg, chat_id: str, slug: str, binding: dict, *,
                       thread_id: Any = None) -> dict:
    """The picker: a reply-keyboard of `/pin-session <sid>` buttons (plus an
    ``off`` button back to attendant routing). Best-effort, like every other
    interactive reply here."""
    rows = _session_candidates(cfg, slug, binding)
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
    _offer_session_picker(cfg, chat_id, rows, command="/pin-session", lines=lines,
                          extra_buttons=("/pin-session off",), thread_id=thread_id)
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

    sid = _resolve_session_arg(cfg, slug, arg)
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


# ---- T-0300: `/remote-control` — continue a session in the Claude app -------
# Stakeholder, verbatim (T-0155, 2026-06-02): "...and be able to receive a
# response from my tg reply to their tmux, which might also be a
# /remote-control command for me to continue in the claude app".
#
# The handoff itself has existed since T-0155 — but only as a FOOTER that
# `tg_stall.build_escalation_text` appends when the stall watchdog pages him.
# That made it reachable only for a session the watchdog happened to escalate,
# and only in the moment it did so. This turns it into a command he can issue
# for ANY live session at any time, which is what the verbatim actually asks
# for. The URL is NOT recomposed here: `tg_stall.remote_control_line` is the
# single composer and this calls it (operator p298's explicit instruction —
# the footer and the command must never disagree about where a session is
# picked up).


def _project_order(cfg, slug: str) -> list[str]:
    """``slug`` first, then every other registered project."""
    return [slug] + [s for s in cfg.projects if s != slug]


def _resolve_remote_control_target(cfg, slug: str, arg: str) -> Optional[tuple[str, str]]:
    """Resolve ``arg`` to ``(slug, sid)`` across ALL projects, ``slug`` first.

    A SID is globally unique and `/remote-control` changes no routing, so
    scoping the lookup to the chat's project can only ever refuse a handoff the
    stakeholder may legitimately ask for. That is not hypothetical: measured on
    the live install, the project chat statically resolves to ``watchrobot``
    (`_slug_for_chat` is the "incidental" mapping this module elsewhere says
    not to trust), so a bot-squad SID typed in that very chat was refused.

    `/pin-session` deliberately keeps the single-project `_resolve_session_arg`:
    it BINDS a topic to a session, and binding a topic across projects would be
    a routing bug, not a convenience."""
    for candidate in _project_order(cfg, slug):
        sid = _resolve_session_arg(cfg, candidate, arg)
        if sid:
            return (candidate, sid)
    return None


def _remote_control_candidates(cfg, slug: str, binding: dict) -> list[dict]:
    """Picker rows across all projects, the chat's own project first.

    Each row is tagged with its ``slug`` so `_describe_session` can name the
    project — the list mixes them, and SIDs alone don't say which."""
    rows: list[dict] = []
    for s in _project_order(cfg, slug):
        # Only the chat's OWN project gets the binding-aware ranking (pinned
        # session / this topic's ticket first); it says nothing about others.
        for row in _session_candidates(cfg, s, binding if s == slug else {}):
            rows.append({**row, "slug": s})
    return rows


def _handle_remote_control(cfg, chat_id: str, args: str, *,
                           thread_id: Any = None,
                           binding: Optional[dict] = None) -> dict:
    """``/remote-control [<sid>|<alias>]`` — hand a session to the Claude app.

    Bare in a topic already pinned to a session (T-0677 direct mode): hands
    over THAT session — it is the one he is talking to, so re-asking would be
    noise. Bare anywhere else: the session picker. With a session: the handoff
    line for it.
    """
    from bot_squad_worker import tg_bindings, tg_stall as _tg_stall
    if binding is None:
        binding = tg_bindings.resolve(cfg, chat_id, thread_id)
    binding = binding or {}
    # Unlike /pin-session this does NOT require a bound topic: it changes no
    # routing, so a plain DM (where the escalation footer lands today) is a
    # first-class place to ask. Fall back to the chat's static project.
    slug = binding.get("slug") or _slug_for_chat(cfg, chat_id)
    if not slug:
        _notify(cfg, chat_id,
                "❌ /remote-control: this chat isn't linked to a registered project",
                thread_id=thread_id)
        return {"ok": False, "action": "remote_control_no_project"}

    arg = args.strip() or str(binding.get("session_id") or "")
    if not arg:
        rows = _remote_control_candidates(cfg, slug, binding)
        if not rows:
            _notify(cfg, chat_id,
                    "No live sessions to remote-control right now.",
                    thread_id=thread_id)
            return {"ok": False, "action": "remote_control_no_candidates",
                    "slug": slug}
        _offer_session_picker(
            cfg, chat_id, rows, command="/remote-control",
            lines=["Which session do you want to continue in the Claude app?"],
            thread_id=thread_id,
        )
        return {"ok": True, "action": "remote_control_ask", "slug": slug,
                "count": len(rows)}

    found = _resolve_remote_control_target(cfg, slug, arg)
    if not found:
        _notify(cfg, chat_id,
                f"No live session {arg!r}. Pick one:",
                thread_id=thread_id)
        rows = _remote_control_candidates(cfg, slug, binding)
        if rows:
            _offer_session_picker(
                cfg, chat_id, rows, command="/remote-control",
                lines=["Live sessions:"], thread_id=thread_id,
            )
        return {"ok": False, "action": "remote_control_unknown", "slug": slug,
                "arg": arg}
    slug, sid = found

    handoff = _tg_stall.remote_control_line(cfg, sid)
    if not handoff:
        # Neither affordance exists: no `tg.remote_control_url` configured for
        # this install AND no live tmux pane to attach to. Say which, rather
        # than sending a handoff line with an empty target.
        _notify(cfg, chat_id,
                f"{sid}: nothing to hand over — no remote_control_url is "
                "configured for this install and the session has no live tmux "
                "pane to attach to.",
                thread_id=thread_id)
        return {"ok": False, "action": "remote_control_unavailable",
                "slug": slug, "sid": sid}

    _notify(cfg, chat_id, f"{sid}  ({slug})\n{handoff}", thread_id=thread_id)
    return {"ok": True, "action": "remote_control", "slug": slug, "sid": sid}


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
    id, never through the project's user-conversation attendant, and does
    NOT update the conversation locus (T-0667) — a task-topic message must
    never redirect the project's own attendant-reply relay into the task
    topic. A plain project/General binding (no session_id) keeps the T-0639
    behavior: durable append (T-0489) + ensure-session (T-0485) + locus.

    T-0770: what that branch delivered was the RAW TEXT and nothing else, so
    the receiving session could not tell a human had written it, where it had
    arrived, or that an answer was owed back to Telegram rather than to its own
    turn — and he experienced silence, twice, in his own words. It now delivers
    a provenance ENVELOPE (``tg_direct_reply.compose_envelope``) as one composer
    block, and records the answer it OWES so the silence is measured rather than
    hoped away (``tg_direct_reply.tick``). Neither half touches the locus or any
    reply route: the T-0667 constraint above is the reason the provenance rides
    in the message instead.

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
        # T-0786: `tg.msg_text`, not `msg["text"]`. A photo's caption is where
        # Telegram puts the words he typed with it, and this one `text` feeds
        # ALL THREE writers below — the envelope's verbatim fence, the
        # answer-owed ledger (so the T-0770 reminder stopped quoting «» back at
        # the session), and the `system:direct-reply` FYI record whose line
        # ended at the colon with nothing after it.
        text = _tg.msg_text(msg)
        thread_id = msg.get("message_thread_id")
        # T-0770: the session gets his words INSIDE an envelope that says who
        # wrote them, where they arrived, and what command answers back into
        # that topic. Before this the payload was the bare text, and a session
        # handed bare text in its composer does the natural thing — it answers
        # in its own turn, the answer never leaves the process, and he sees
        # silence ("опять игнор меня", 2026-07-28T22:33:23Z). The envelope goes
        # as ONE block (`inject_prompt`), never one-Enter-per-line — see that
        # action's docstring for the measurement.
        # T-0780: a message in a task topic may ALSO be a reply — to the
        # session's own last post in that topic, most often — and until now the
        # quoted original was dropped on this path too. `quote` is None for the
        # ordinary un-replied message, leaving that envelope unchanged.
        # T-0785: and WHAT ARRIVED WITH IT. `text` is empty for an UNCAPTIONED
        # photo (T-0786 now reads a caption when there is one), so until then a
        # photo sent into a task topic woke the session with an envelope whose
        # verbatim fence was empty — not "he sent a photo", not a marker,
        # nothing to react to. That is still the case for a photo he sent
        # without typing anything, which is why the mention below is NOT made
        # redundant by the caption fix. The descriptor is the one T-0782
        # already writes for the durable record (type + file_id, or type + a
        # named PHOTO_UNKNOWN_* reason); this only MENTIONS it. No download
        # path exists behind a photo, so the session still cannot look at the
        # image — see `tg_direct_reply.render_attachments`, whose wording is
        # what keeps a session from claiming otherwise.
        # T-0787: and it is no longer only a photo. `_msg_attachments` knew
        # three media keys, so an uncaptioned video/sticker/animation/
        # video_note/audio produced no descriptor and this section never
        # fired — i.e. the empty-fence failure above survived here for every
        # media type except the one T-0785 was measured on. Same descriptor
        # shape, same renderer, nothing new to render.
        quote = reply_quote.extract(msg)
        envelope = tg_direct_reply.compose_envelope(
            text=text, chat_id=chat_id, thread_id=thread_id, sid=session_id,
            slug=slug, ticket_id=binding.get("ticket_id") or "",
            sender=_sender_display_name(msg.get("from") or {}),
            quote=quote, attachments=_msg_attachments(msg),
        )
        # T-0746: `gid` so an undeliverable message can fall back to the target
        # session's user-conversation instead of evaporating, and `thread_id`
        # so the outcome notice lands in the TASK TOPIC the user is looking at
        # rather than the forum's general feed (this call site was the one that
        # never passed it — the same omission T-0740 fixed on the other paths).
        result = _handle_reply(
            cfg, chat_id, session_id, text,
            thread_id=thread_id, gid=gid, block_text=envelope, quote=quote,
        )
        result["action"] = f"task_topic_{result.get('action', 'inject')}"
        result["slug"] = slug
        if result.get("ok") and result.get("fallback") is None:
            # T-0770: the envelope was DELIVERED — start the clock on the answer
            # it owes. Only on success: a message the session never received is
            # not a debt it owes, and the T-0746 fallback above has already
            # handed that case to an attendant who WILL answer.
            tg_direct_reply.record_owed(
                cfg, sid=session_id, chat_id=chat_id, thread_id=thread_id,
                text=text, slug=slug, gid=gid,
                ticket_id=binding.get("ticket_id") or "",
            )
        if result.get("fallback") is None:
            # T-0746, the same defect item (c) is about, found in this file:
            # this record's text is SYSTEM prose ("Пользователь ответил сессии
            # … напрямую:") wrapping his words, and it was stored as
            # `author="user"` — a line the stakeholder never wrote, attributed
            # to him. `system:direct-reply` is what it has always been. Side
            # effects are unchanged (an `fyi` append neither relays nor wakes
            # either way); what DOES change, correctly, is that `uc_redrive`
            # stops treating a record explicitly marked "ответ не требуется" as
            # an unanswered stakeholder message and nagging the attendant to
            # answer it.
            #
            # Skipped entirely when the message FELL BACK: this summary exists
            # to give the attendant context about text that went straight to a
            # session, and if that never happened the fallback's own records
            # are the account — repeating his words inside a "no reply needed"
            # wrapper would tell the attendant to ignore the very message it
            # was just woken for.
            append_conversation_fyi(
                cfg, slug, gid, author="system:direct-reply",
                text=f"Пользователь ответил сессии {session_id} напрямую: {text}",
                reply_to=quote,
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
    # T-0848: the T-0830 drive-scope recogniser used to run here, reading his
    # message for a scope command. Removed on his ruling — «не надо срабатывать
    # в контексте в целом на слова автоматически». Nothing on this path may
    # infer a setting from his prose; see dispatch.py's tombstone.
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
    instead (T-0639: the topic binding outranks this pin-based resolution).

    T-0883: a project's OWN dedicated group chat outranks the pin, which is
    the live bug this fixes. The pin is keyed on the global user ALONE — it
    knows nothing about which chat the message arrived in — so a stakeholder
    pinned to bot-squad had every message he typed in guestent's own group
    chat (`-1004380986138`, guestent's static `tg_chat`) recorded in
    bot-squad's store, waking bot-squad's attendant, which then answered him
    back inside guestent's chat. His words: «чат должен быть жестко привязан
    именно к проекту». A negative-id chat named by exactly one project IS
    that rigid binding (see `_dedicated_static_group_chat_slug`), so it is
    consulted BEFORE the pin and the pin is never read for such a chat —
    neither read nor written, so nothing about this path can drift with it."""
    if not gid:
        return {"ok": True, "action": "skip", "reason": "not a reply or command"}

    # A unique static SUPERGROUP chat is an explicit project boundary, unlike
    # a positive-id private chat which may be shared by several projects for
    # one person.
    por = _dedicated_static_group_chat_slug(cfg, chat_id)
    if not por:
        sticky = get_current_project(cfg, gid)
        if not sticky:
            # Unpinned: hardwired ask (voice-04). Record under the chat's project
            # so the message isn't lost while we wait for the pin.
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
    # T-0848: the second T-0830 drive-scope call site was here. Removed with the
    # other one — the recogniser is gone, not merely unwired.
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
    as append_conversation itself.

    T-0746 item (c), found by re-scanning the store on a CLASS predicate rather
    than the ❌ notice's shape (operator p298's steer): this text is entirely
    OURS — a diagnostic ABOUT a message, not a message — and it was being
    written with ``author="user"``, so the live thread holds
    ``{"author": "user", "text": "[голосовое 1540 сек НЕ обработано: too_long
    …]"}`` from 2026-07-05. Same lie as the ❌ line, different producer, and
    unlike that one it is a code path that would have kept emitting them.
    ``system:voice-rejected`` with ``direction="in"`` (T-0755 derives
    ``system:`` as outbound; this one is our note about something that reached
    us and was never sent anywhere).

    The attendant WAKE is now explicit. It used to ride on the append being
    ``author="user"`` — the endpoint auto-wakes only for that author — so
    simply correcting the attribution would have silently un-done T-0586's
    entire point (the attendant seeing the drop). The voice ATTACHMENT is
    preserved for the same reason it was added: the ``file_id`` is what makes
    late recovery from TG possible at all."""
    dur = int(out.get("duration") or (msg.get("voice") or {}).get("duration") or 0)
    por = get_current_project(cfg, gid) or chat_slug
    message_ref = _msg_ts(msg)
    _post_conversation(cfg, por, gid, {
        "author": "system:voice-rejected",
        "text": (
            f"[голосовое {dur} сек НЕ обработано: {reason} — "
            f"содержимое не транскрибировано]"
        ),
        "attachments": _msg_attachments(msg),
        "timestamp": message_ref,
        "direction": "in",
    })
    _ensure_user_conversation(cfg, por, gid, message_ref)


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
        # T-0725: the refusal is about THIS note — thread it to the note, same
        # as the transcript echo below (the group path threads every outcome
        # through _confirm; the DM path shouldn't be the odd one out).
        _notify(cfg, chat_id, _voice_reject_text(cfg, reason, out),
                reply_to_message_id=msg.get("message_id"))
        _record_voice_rejection(cfg, chat_slug, gid, msg, reason, out)
        return {"ok": False, "action": "voice_private_failed", "reason": reason}

    transcript = out["transcript"]
    # T-0725: threaded to the note itself, so the transcript reads as an answer
    # to that recording rather than a detached message.
    _echo_transcript(cfg, chat_id, transcript,
                     reply_to_message_id=msg.get("message_id"))

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

    # T-0883, second half of his ask: «И договорились что в этом чате всё будет
    # падать на воркера юзера flomaster … это должно идти от юзера flomaster, в
    # guestent, и эта сессия должна быть codex, целиком этот чат так должен быть
    # привязан». A project may trust several senders while deliberately running
    # only ONE user-conversation (guestent's `stakeholders` says so, and its
    # `groups.json` already names the owner). The conversation store is keyed
    # (slug, global_user_id), so the OWNER's gid is the whole mechanism: route
    # the sender's message through it and the message lands in the owner's
    # thread, which wakes the attendant that already exists for it —
    # flomaster's live codex session — instead of provisioning a second
    # per-sender attendant for the same project. Deliberately placed BEFORE the
    # slash/reply/voice/unquoted fork so it holds for the whole chat, not just
    # the firehose path («целиком этот чат»).
    dedicated_chat_slug = _dedicated_static_group_chat_slug(cfg, chat_id)
    if dedicated_chat_slug and gid:
        routed_gid = _dedicated_chat_conversation_gid(cfg, dedicated_chat_slug, gid)
        if routed_gid != gid:
            log.info(
                "dedicated chat %s: routing trusted sender %s through canonical "
                "conversation owner %s",
                chat_id,
                gid,
                routed_gid,
            )
            gid = routed_gid

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
    reply = extract_reply_target(msg, cfg) if not slash else None
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
        #
        # T-0740: pass `thread_id` — this branch was the one inbound path that
        # DROPPED it. `_handle_topic_bound` and `_handle_unquoted` below both
        # thread it through (T-0676 items 3/6); a reply-quote or a GROUP VOICE
        # NOTE typed in a bound topic was recorded into the project's collapsed
        # thread-less history instead, so the attendant woke unscoped and its
        # answer relayed to whatever the stale project-level locus pointed at —
        # the observed "voice note answered correctly in the topic, then the
        # actual reply landed in a different topic entirely". The thread is
        # right here in scope; it just was never handed over. Whenever
        # `thread_id` is set in this branch the chat is a forum whose binding
        # (resolved above) supplied `chat_slug`, so the record and the topic
        # agree on the project.
        #
        # T-0851: a RESOLVED REPLY (``reply`` truthy) is recorded as an FYI,
        # not a normal user-authored append. `append_conversation`'s payload
        # is `author="user"` with no `fyi` flag, which is exactly what makes
        # the append endpoint WAKE the attendant (`_ensure_attendant`) — but
        # this message is already being delivered straight to the operator/
        # dev session it replied to (below, `_handle_reply`), the same
        # "bypasses the attendant" shape `append_conversation_fyi` exists for
        # (T-0660). Without this, every reply to a session double-fires: the
        # session gets it AND the user-conversation attendant wakes on a
        # message that was never addressed to it. The quote (T-0780) still
        # carries the replied-to message's own author/author_name, so this FYI
        # line names both WHO he answered and WHAT they said, same as any
        # other record's ``reply_to``.
        if gid and not slash:
            if reply:
                append_conversation_fyi(
                    cfg, chat_slug, gid,
                    author="system:direct-reply",
                    text=f"replied to session {reply[0]}: {reply[1]}",
                    reply_to=reply_quote.extract(msg),
                    thread_id=thread_id,
                )
            else:
                append_conversation(cfg, chat_slug, gid, msg, thread_id=thread_id)
            if thread_id is not None:
                # T-0740: and remember WHERE he wrote. This branch never
                # recorded the locus either, so after a reply-quote or a voice
                # note in a topic the project's locus still named whatever
                # topic (or DM) he last used on the OTHER paths — days stale in
                # the live case. Gated on `thread_id is not None` deliberately:
                # only a bound topic makes `chat_slug` authoritative (it came
                # from the binding). For a thread-less message in this branch
                # the slug is the "incidental" static one this code is
                # explicitly told not to trust, so the locus is left alone —
                # unchanged from before this fix.
                from bot_squad_worker import conversation_locus
                conversation_locus.set_locus(cfg, chat_slug, gid, chat_id, thread_id)
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
            elif cmd == "remote-control":
                # T-0300: same reason it can't live in _handle_slash — it reads
                # the binding (its slug, and the pinned session that makes a
                # bare `/remote-control` unambiguous in a topic).
                result = _handle_remote_control(
                    cfg, chat_id, args, thread_id=thread_id, binding=binding,
                )
            else:
                result = _handle_slash(cfg, chat_id, cmd, args, thread_id=thread_id,
                                       gid=gid,
                                       sender=_sender_display_name(msg.get("from") or {}))
        elif reply:
            # T-0746: `gid` is what makes the undelivered-message fallback
            # possible at all — the store is keyed (slug, global_user_id).
            #
            # T-0773: and it goes as ONE composer submission inside a light
            # provenance envelope. Until now this path handed the session the
            # bare text over `inject_input`, whose transport sends one Enter PER
            # LINE — measured at a real pane: his 3-line answer arrived as 3
            # separate submissions, so the session began answering line 1 while
            # 2 and 3 were still landing. The envelope carries NO answer-owed
            # debt (operator ruling, see `compose_light_envelope`): here he is
            # ANSWERING a question the session asked him, and nagging it to post
            # "понял" back into his thread is this branch's harm inverted.
            #
            # T-0780: and it carries WHAT HE WAS ANSWERING. This is the path the
            # ticket is about — a reply resolved to a session is by definition
            # an answer to something that session said, and «да» delivered
            # without the question is a message the session has to guess at.
            reply_sid, reply_text = reply
            quote = reply_quote.extract(msg)
            block = ""
            if str(reply_text or "").strip():
                block = tg_direct_reply.compose_light_envelope(
                    text=reply_text, chat_id=chat_id, thread_id=thread_id,
                    sid=reply_sid, sender=_sender_display_name(msg.get("from") or {}),
                    origin="reply", quote=quote,
                )
            # Empty text keeps the OLD path deliberately: an envelope is never
            # empty, so wrapping unconditionally would turn the "нечего
            # передавать (пустой текст)" refusal into a header delivered with no
            # body — a silent success where there used to be a loud failure.
            result = _handle_reply(cfg, chat_id, reply_sid, reply_text,
                                   thread_id=thread_id, gid=gid, block_text=block,
                                   quote=quote)
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


def _dedicated_static_group_chat_slug(cfg, chat_id: str) -> str:
    """The sole project owning a negative-id static chat, else ``""``.

    Telegram private chats have positive ids and may deliberately be reused as
    one person's inbox across projects — bot-squad and watchrobot both name the
    SAME positive id (`404580642`) today, which is exactly why `_slug_for_chat`
    can't be treated as authoritative in general. Groups/supergroups have
    negative ids; when exactly one project names one, that room itself is an
    authoritative routing boundary and must not fall through to the
    per-global-user pin (T-0883).

    Returns "" — deliberately not the first match — when TWO projects name the
    same negative id, because then the room is not evidence for either of them
    and the caller's existing resolution is still the better answer.
    """
    chat = str(chat_id or "")
    if not chat.startswith("-"):
        return ""
    matches = [
        slug
        for slug, project in cfg.projects.items()
        if str(getattr(project, "tg_chat", "")) == chat
    ]
    return matches[0] if len(matches) == 1 else ""


def _dedicated_chat_conversation_gid(cfg, slug: str, sender_gid: str) -> str:
    """Canonical shared attendant identity for a dedicated project chat.

    A project may trust several senders while deliberately running only one
    low-token user-conversation; `data/<slug>/groups.json` names it in
    ``conversation_owner_global_user_id``. Returns that gid, or ``sender_gid``
    unchanged when the project declares no owner (every project but the opted-in
    one, so this is a no-op by default).

    The owner is accepted only when it is itself an explicit member of the
    project's groups: a stale or mistyped owner id would otherwise silently
    redirect one project's conversations into a gid nobody owns, creating a
    thread no session attends. Malformed/absent config falls back to the sender
    — the sender's own thread is the pre-T-0883 behaviour, so a broken file
    degrades to "as before", never to "routed somewhere unintended".
    """
    import json as _json

    groups_path = Path(cfg.data_dir) / slug / "groups.json"
    try:
        raw = _json.loads(groups_path.read_text())
    except (OSError, _json.JSONDecodeError):
        return sender_gid
    if not isinstance(raw, dict):
        return sender_gid
    owner_gid = str(raw.get("conversation_owner_global_user_id") or "").strip()
    if not owner_gid:
        return sender_gid
    from bot_squad_worker import project_groups_store

    if project_groups_store.group_for_user(Path(cfg.data_dir), slug, owner_gid) is None:
        log.error(
            "dedicated chat %s has non-member conversation owner %s — routing "
            "sender %s to their own thread instead",
            slug,
            owner_gid,
            sender_gid,
        )
        return sender_gid
    return owner_gid


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


def _handle_reply(
    cfg, chat_id: str, sid: str, text: str, *, thread_id: Any = None, gid: str = "",
    block_text: str = "", quote: Optional[dict] = None,
) -> dict:
    """Find the pane by SID and inject the text; on failure, DON'T drop it.

    ``block_text`` (T-0770): deliver THIS instead of the raw ``text``, as one
    composer submission (``inject_prompt``) rather than one submission per line
    (``inject_input``). It is how the direct-mode topic path hands a session the
    provenance envelope around his words. ``text`` is still what the FALLBACK
    stores and relays: the attendant is owed what the human actually said, not
    our wrapper around it — the T-0746 rule that system prose never enters the
    record as his words.

    T-0746 item (a). Before this, an undeliverable message evaporated: we told
    the sender "message dropped" and that was the whole of it — the text was
    gone, nothing downstream had it, and the only trace was a TG notice that
    (item c) then came back in and corrupted the record. Sessions are reaped
    routinely, so aiming a reply at one that has since gone is the NORMAL case,
    not an edge one.

    The fallback destination is the TARGET SESSION'S project (item e) — see
    ``sessions.project_of_sid`` for why that and not the arrival store. ``gid``
    is required to have one: the conversation store is keyed
    ``(slug, global_user_id)``, so an unrecognized sender has no thread to fall
    back INTO.

    ``quote`` (T-0780) rides along for the FALLBACK's sake — the injected
    ``block_text`` already carries it when there is one. An attendant picking
    up a message aimed at a session that has since been reaped is the reader
    who needs the question MOST: it holds none of the dead session's context,
    so a bare «да» in its thread is unanswerable without it.
    """
    from bot_squad_worker import actions as A
    verb, payload = (
        ("inject_prompt", block_text) if block_text else ("inject_input", text)
    )
    try:
        result = A.dispatch(verb, {"sid": sid, "text": payload})
        # T-0155: the stakeholder answered via TG — the agent is no longer
        # blocked on him; cancel any pending stall escalation.
        _clear_stall(cfg, chat_id, sid, thread_id=thread_id)
        return {"ok": True, "action": "inject", "sid": sid, "result": result}
    except A.ActionError as e:
        fb = _fallback_undelivered(
            cfg, chat_id, sid, text, gid=gid, thread_id=thread_id, error=str(e),
            quote=quote,
        )
        out = {"sid": sid, "error": str(e)}
        out.update(fb)  # carries `ok` + `action` for both outcomes
        return out


#: Marker for the store record that says WHO a fallen-back message was aimed
#: at. A separate ``system:``-authored line rather than a prefix on the user's
#: own text, because T-0746 item (c) is precisely about system prose being
#: mixed into a record that claims the stakeholder wrote it.
_UNDELIVERED_AUTHOR = "system:undelivered"


def _fallback_undelivered(
    cfg, chat_id: str, sid: str, text: str, *, gid: str = "",
    thread_id: Any = None, error: str = "", quote: Optional[dict] = None,
) -> dict:
    """Hand a message we could not deliver to ``sid`` to that project's
    user-conversation attendant, and tell the sender what happened.

    Two records, in this order, into ``(slug, gid)``:

    1. ``system:undelivered`` (``direction="in"``) naming the intended SID and
       why it could not be delivered — the context the attendant needs to
       answer usefully rather than just report a failure, and the reason item
       (a) says the fallback must "name the intended SID".
    2. The user's ORIGINAL text, ``author="user"``. Unprefixed and unedited:
       the whole point of the store is that it holds what he actually said, so
       the system's account of the situation belongs in record 1, not stapled
       onto his words. T-0780 puts the quoted original on THIS record, as the
       structured ``reply_to`` field — it belongs to his message, and it is
       still not stapled onto his text, for the same reason.

    Deliberately THREADLESS. The sender may have been writing in a forum topic
    bound to a DIFFERENT project (the live incident exactly: a watchrobot
    session addressed from a bot-squad-bound feed), and a thread id is only
    meaningful inside its own chat — reusing it here would file the message
    under a topic of the target project that does not exist. The project's main
    ``(slug, gid)`` thread is the one destination that is always correct, which
    is what makes item (e)'s "deterministic" true rather than aspirational.

    On item (b): when no fallback exists — an unrecognized sender, or a SID no
    project claims — nothing is written to the store at all and the sender is
    told plainly that the message was NOT delivered. The failure is surfaced as
    a failure; it is never smuggled into the thread as content.
    """
    from bot_squad_worker import sessions as S

    body = str(text or "").strip()
    slug = ""
    try:
        slug = S.project_of_sid(cfg, sid)
    except Exception:  # noqa: BLE001 — an unreadable data dir is "no project"
        log.exception("tg_listener: project_of_sid failed for %s", sid)
    reason = ""
    if not body:
        reason = "нечего передавать (пустой текст)"
    elif not gid:
        reason = "отправитель не опознан"
    elif not slug:
        reason = f"не найден проект сессии {sid}"
    if reason:
        _notify(
            cfg, chat_id,
            f"❌ Сессия {sid} не активна, и передать сообщение в "
            f"user-conversation не удалось: {reason}. Сообщение НЕ доставлено — "
            f"напиши его обычным сообщением в нужном топике.",
            thread_id=thread_id,
        )
        return {"ok": False, "action": "inject_failed", "fallback": "impossible",
                "reason": reason}

    _post_conversation(cfg, slug, gid, {
        "author": _UNDELIVERED_AUTHOR,
        "text": (
            f"Сообщение было адресовано сессии {sid} (проект {slug}), но она не "
            f"активна ({error or 'нет живой панели'}). Оригинальный текст — "
            f"следующим сообщением; ответь на него сам."
        ),
        # T-0755 derives `system:` as outbound; this one was never sent
        # anywhere — it is our note ABOUT an inbound message, so it must say so.
        "direction": "in",
    })
    message_ref = _now_iso()
    user_record = {
        "author": "user",
        "text": body,
        "timestamp": message_ref,
    }
    if quote:
        user_record["reply_to"] = quote
    recorded = _post_conversation(cfg, slug, gid, user_record)
    if not recorded:
        _notify(
            cfg, chat_id,
            f"❌ Сессия {sid} не активна, и записать сообщение в "
            f"user-conversation проекта «{slug}» не удалось. Сообщение НЕ "
            f"доставлено — напиши его обычным сообщением в нужном топике.",
            thread_id=thread_id,
        )
        return {"ok": False, "action": "inject_failed", "fallback": "store_failed",
                "fallback_slug": slug}

    ensured = _ensure_user_conversation(cfg, slug, gid, message_ref)
    if isinstance(ensured, dict) and ensured.get("parked"):
        _notify(
            cfg, chat_id,
            f"⚠️ Сессия {sid} не активна. Сообщение записано в "
            f"user-conversation проекта «{slug}», но все воркеры сейчас заняты — "
            f"займусь, как только освободится слот.",
            thread_id=thread_id,
        )
        return {"ok": True, "action": "inject_fallback", "fallback": "parked",
                "fallback_slug": slug}
    _notify(
        cfg, chat_id,
        f"⚠️ Сессия {sid} не активна — передал сообщение в user-conversation "
        f"проекта «{slug}».",
        thread_id=thread_id,
    )
    return {"ok": True, "action": "inject_fallback", "fallback": "routed",
            "fallback_slug": slug}


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


def _handle_slash(
    cfg, chat_id: str, cmd: str, args: str, *, thread_id: Any = None, gid: str = "",
    sender: str = "",
) -> dict:
    """Implement /sessions, /say, /help.

    ``gid`` (T-0746): only ``/say`` uses it, and only on the failure path — see
    there for why the undelivered-message fallback covers this verb too.

    ``sender`` (T-0773): the writer's DISPLAY NAME, for ``/say``'s provenance
    envelope. Deliberately just a label — this function stays identity-less in
    the sense that matters (it resolves no GlobalUser and pins nothing; that is
    why ``/project`` and ``/pin-session`` still live in ``handle_update``). A
    provenance line saying "a human, via Telegram" is the whole use."""
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
        # T-0773: same two changes as the `[<sid>]` reply path, same reasons —
        # ONE composer submission (`inject_prompt`) instead of one Enter per
        # line, wrapped in a light provenance envelope so the session knows a
        # human typed this rather than being handed an anonymous string. No
        # answer-owed debt: `/say` is one-way by construction (operator ruling
        # on T-0773 — see `tg_direct_reply.compose_light_envelope`).
        block = ""
        if text.strip():
            block = tg_direct_reply.compose_light_envelope(
                text=text, chat_id=chat_id, thread_id=thread_id, sid=sid,
                sender=sender, origin="say",
            )
        verb, payload = ("inject_prompt", block) if block else ("inject_input", text)
        try:
            result = A.dispatch(verb, {"sid": sid, "text": payload})
            return {"ok": True, "action": "say", "sid": sid, "result": result}
        except A.ActionError as e:
            # T-0746: /say is the SAME verb as a reply-quote — "a message
            # addressed to a session" — so it gets the same fallback rather
            # than being left as the unfixed twin of the bug this ticket is
            # about. T-0659's "never append a slash command" is not in tension:
            # that rule is about routinely dumping contextless control commands
            # into a project's store, and this appends only the message BODY,
            # only when delivery has already failed.
            fb = _fallback_undelivered(
                cfg, chat_id, sid, text, gid=gid, thread_id=thread_id, error=str(e),
            )
            out = dict(fb)
            out.update({
                "action": "say_fallback" if fb.get("ok") else "say_failed",
                "sid": sid,
                "error": str(e),
            })
            return out

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
                "directly; /pin-session off returns to the attendant\n"
                "/remote-control [<sid>] — continue a session in the Claude "
                "app (or attach to its tmux); bare = pick from a list",
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


def _notify(
    cfg, chat_id: str, message: str, *, thread_id: Any = None,
    reply_to_message_id: Any = None,
) -> None:
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

    ``reply_to_message_id`` (T-0725): thread the reply to the message it
    answers — see :func:`_channel_notify`.
    """
    _channel_notify(cfg, chat_id, message, thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id)


def _channel_notify(
    cfg, chat_id: str, message: str, *,
    reply_markup: dict | None = None, thread_id: Any = None,
    reply_to_message_id: Any = None,
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

    ``reply_to_message_id`` (T-0725): thread this reply to the specific inbound
    message it answers, so it reads as an answer instead of a loose message in
    the feed. Independent of ``thread_id`` — that one says which TOPIC, this one
    says which MESSAGE. ``None`` sends unthreaded, exactly as before.
    """
    if not cfg.tg_bot_token:
        return
    from bot_squad_worker import channels as _channels

    extra: dict[str, Any] = {"debounce": False}
    if reply_markup is not None:
        extra["reply_markup"] = reply_markup
    if thread_id is not None:
        extra["topic_id"] = thread_id
    if reply_to_message_id is not None:
        extra["reply_to_message_id"] = reply_to_message_id
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
# T-0721: the cap and the splitter itself now live in ``tg`` and are shared with
# the stakeholder-page path (actions._split_page) — one chunker, no drift.
# T-0741: so does the RENDER (fit-or-numbered-parts), now shared with the
# GROUP/topic ACK — see ``tg.render_transcript_echo``.
_TG_MSG_CAP = _tg.TG_MSG_CAP
_ECHO_CHUNK = _tg.ECHO_CHUNK

_ECHO_PREFIX = "\U0001f399 Распознал так: "


def _echo_transcript(
    cfg, chat_id: str, transcript: str, *, reply_to_message_id: Any = None,
) -> None:
    """T-0586: 🎙-echo that survives transcripts longer than one TG message.
    Single send for the common case; a long transcript goes out as numbered
    parts so the user still sees the full recognition.

    T-0725: ``reply_to_message_id`` threads the echo to the voice note it
    transcribes. EVERY part replies to the note (not part 1 only) — a
    multi-part echo whose tail floated free of the note is the same detachment
    complaint at a smaller scale. The DM path's ``chat_id`` was already
    correct, so this adds threading only; nothing about where it goes changes.

    T-0741: the render moved to ``tg.render_transcript_echo`` verbatim — the
    GROUP/topic path's ACK had never been given this treatment and was still
    showing 140 chars, so the two now render through one function.
    """
    for body in _tg.render_transcript_echo(transcript, prefix=_ECHO_PREFIX):
        _channel_notify(cfg, chat_id, body,
                        reply_to_message_id=reply_to_message_id)


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
