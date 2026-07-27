"""TG conversation history endpoints (T-0489).

Two auth surfaces over the per-(project, user) conversation thread
(``conversation_store``):

- ``worker_router`` — ``POST /conversations/{slug}/{gid}/messages``. The worker
  (tg_listener) records each inbound TG user message here. Token-gated by the
  shared-secret ``WORKER_API_TOKEN`` (reusing T-0488's ``_authenticate_worker``,
  the established worker->API trust path); fails closed when unset.
- ``router`` — ``GET /conversations/{slug}/{gid}/messages``. Session-auth
  list/search (paginated) — the durable lookup surface an attending session uses
  to review the thread.

Mounted only on the MOTHERSHIP build (see ``main.py``): the bot's
user-communication module is centralized on the mothership (voice-04), and the
``global_user_id`` key is a mothership identity (T-0488).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import conversation_store as CS
from app import pins_store
from app.mothership_users_store import MothershipUsersStore
from app.project_authz import require_project_read
from app.routes_auth import require_auth
from app.routes_mothership import _authenticate_worker
from app.worker_client import WorkerError


log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Session-auth read surface.
router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(require_auth)],
)
# Worker-token write surface (no session cookie); token checked per-handler.
# T-0529: mounted under a dedicated /worker prefix (-> /api/m/worker/*) so the
# public traefik router can exclude all worker-token routes (option-C
# least-exposure) WITHOUT touching the session-auth /conversations read surface,
# which must stay public for the UI. (A bare /conversations prefix shared the
# path with the auth GET, so a PathPrefix exclusion couldn't separate them.)
worker_router = APIRouter(prefix="/worker", tags=["conversations-worker"])


def _data_dir(request: Request):
    return request.app.state.api_config.data_dir


def _users_store(request: Request) -> MothershipUsersStore:
    cfg = request.app.state.api_config
    return MothershipUsersStore(cfg.data_dir / "_mothership")


def _locus_key(slug: str, global_user_id: str, thread_id: Any = None) -> str:
    """Mirrors ``bot_squad_worker.conversation_locus._key`` exactly (the API
    reads the worker's on-disk file directly, so the key derivation must stay
    byte-identical). ``thread_id`` absent -> the pre-T-0676 ``slug:gid`` key;
    given -> the isolated ``slug:gid:thread_id`` key (T-0676 items 3/6)."""
    if thread_id is None or thread_id == "":
        return f"{slug}:{global_user_id}"
    return f"{slug}:{global_user_id}:{thread_id}"


def _read_conversation_locus(
    request: Request, slug: str, global_user_id: str, thread_id: Any = None,
) -> dict | None:
    """T-0667: read-only lookup of the worker-owned conversation-locus store —
    the last ``(chat_id, thread_id)`` an inbound message from ``(slug,
    global_user_id[, thread_id])`` arrived on, recorded in-process by
    ``tg_listener`` (``bot_squad_worker.conversation_locus``,
    ``_handle_topic_bound`` / ``_handle_unquoted``).

    Worker and API share the data dir but run in separate processes/envs — this
    reads the SAME on-disk file directly rather than round-tripping through the
    worker socket, mirroring the pattern ``routes_autoupdate.py`` already uses
    for the worker's autoupdate state. Best-effort: a missing/corrupt file is
    "no locus recorded", never an error.

    ``thread_id`` (T-0676 items 3/6): when given, look up THAT topic's own
    isolated locus entry rather than the project's single collapsed one —
    see :func:`_locus_key`.
    """
    cfg = request.app.state.api_config
    path = cfg.data_dir / "_worker" / "conversation_locus.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("routes_conversations: could not parse %s: %s", path, e)
        return None
    if not isinstance(raw, dict):
        return None
    rec = raw.get(_locus_key(slug, global_user_id, thread_id))
    if not isinstance(rec, dict) or not rec.get("chat_id"):
        return None
    return {"chat_id": rec["chat_id"], "thread_id": rec.get("thread_id")}


def _read_topic_binding_chat(request: Request, slug: str, thread_id: Any) -> str:
    """T-0740: the chat_id that ``thread_id`` is a forum topic OF, for ``slug``.

    Read-only lookup of the worker-owned topic-binding store
    (``bot_squad_worker.tg_bindings``, ``data/_worker/tg_bindings.json``) —
    same direct-read-of-a-shared-file pattern, and same best-effort contract,
    as :func:`_read_conversation_locus` above (a missing/corrupt file is "no
    binding", never an error). Key derivation mirrors ``tg_bindings._key``:
    ``"<chat_id>:<thread_id>"``.

    A thread id is only meaningful INSIDE its chat, so relaying into a topic
    requires both halves. The locus supplies both when it has an entry; this
    supplies the chat half from the binding when it does not (see
    :func:`_resolve_relay_target` rung 2 — the "session speaks first in a
    freshly-created topic" case). ``slug`` is checked, not assumed: a thread
    bound to a DIFFERENT project must never be used as this project's
    destination.
    """
    if thread_id is None or str(thread_id).strip() == "":
        return ""
    cfg = request.app.state.api_config
    path = cfg.data_dir / "_worker" / "tg_bindings.json"
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("routes_conversations: could not parse %s: %s", path, e)
        return ""
    if not isinstance(raw, dict):
        return ""
    for key, rec in raw.items():
        if not isinstance(rec, dict) or rec.get("slug") != slug:
            continue
        chat_id, _, bound_thread = str(key).rpartition(":")
        if chat_id and bound_thread == str(thread_id):
            return chat_id
    return ""


def _resolve_relay_target(
    request: Request, slug: str, global_user_id: str, thread_id: Any = None,
) -> tuple[str, int | None]:
    """T-0569 / T-0667: resolve the ``(chat_id, topic_id)`` to relay a session
    reply to.

    Priority (T-0667 — "one coherent dialogue", D-0055 Addendum 3):
    1. The conversation LOCUS — the ``(chat_id, thread_id)`` the user's most
       recent inbound message for THIS project (and, when ``thread_id`` is
       given, THIS bound topic specifically — T-0676 items 3/6) arrived on.
       Without this, a reply always landed in the user's DM even when they'd
       just written in a bound forum topic, splitting the conversation (the
       live gap this ticket fixes).
    2. (T-0740) The ``thread_id`` THIS reply was appended for, paired with the
       chat that topic is bound to — see :func:`_read_topic_binding_chat`.
    3. The GlobalUser's ``tg_user_id`` (a DM chat id IS the TG user id — every
       TG user has an implicit private chat with the bot at that same id) —
       the right default for a user who has never written into a bound topic.
    4. The project's configured ``tg_chat``/``tg_topic_id`` (legacy static
       fallback, e.g. when the user record predates linkage).
    ``("", None)`` when nothing resolves — the caller treats that as "can't
    relay" (``relayed: false``), never an error.

    Rung 2 is the T-0740 fix, and it is about ``thread_id`` being an INPUT to
    this function that rung 1 used only as a lookup KEY. When the user has
    never written in that topic there is no locus entry for it, so rung 1
    missed and the thread was then DISCARDED — dropping straight to rung 3,
    the user's private DM. That is the reported bug: a session opening a
    freshly-created task topic and posting the first message into it (nobody
    can have written there yet, by construction) had that message delivered
    to the DM instead. A reply that NAMES the topic it belongs to must be
    delivered there; only a reply with no thread at all may fall through to
    the DM. Rungs 1/3/4 are untouched, so a thread-less relay behaves exactly
    as before this change.
    """
    cfg = request.app.state.api_config
    locus = _read_conversation_locus(request, slug, global_user_id, thread_id)
    if locus:
        return locus["chat_id"], locus.get("thread_id")
    bound_chat = _read_topic_binding_chat(request, slug, thread_id)
    if bound_chat:
        try:
            return bound_chat, int(thread_id)
        except (TypeError, ValueError):
            # A binding key whose thread segment isn't an integer can't name a
            # real TG forum topic — fall through rather than send a bad topic_id.
            log.warning("routes_conversations: non-integer thread_id %r for %s",
                        thread_id, slug)
    try:
        user = _users_store(request).get_user(global_user_id)
    except (OSError, ValueError):
        user = None
    if user is not None and (user.tg_user_id or "").strip():
        return user.tg_user_id.strip(), None
    project = cfg.project(slug)
    if project is not None and (project.tg_chat or "").strip():
        return project.tg_chat.strip(), getattr(project, "tg_topic_id", None)
    return "", None


async def _relay_to_telegram(
    request: Request, slug: str, global_user_id: str, text: str, thread_id: Any = None,
) -> bool:
    """Best-effort writeback (T-0569): relay a session-authored conversation
    reply to the user's Telegram chat via the worker's ``tg_notify`` action, so
    the user actually SEES the reply (before this, nothing surfaced a
    session's append back to TG at all).

    NEVER raises and NEVER blocks the append that already durably recorded the
    reply — a relay failure (worker down, no resolvable chat, TG egress error)
    just means ``relayed: false`` in the response, not a 5xx on the append.

    ``urgent=True``: the user just messaged us — they're awake, so this must
    bypass the quiet-hours gate in worker ``tg.py`` that otherwise silently
    drops non-urgent sends 17:00-05:00 UTC (an interactive reply is exactly the
    opposite of a quiet-hours background notification).
    ``debounce=False``: an interactive conversation turn must always land, even
    if textually identical to a recent send (the debounce cooldown exists to
    quash repeated BACKGROUND notifications, not conversation replies).
    ``topic_id`` (T-0667): when the resolved target carries a forum thread
    (locus or static ``tg_topic_id``), the reply is delivered into THAT thread
    instead of the chat's general feed.
    """
    chat_id, topic_id = _resolve_relay_target(request, slug, global_user_id, thread_id)
    if not chat_id:
        return False
    client = request.app.state.worker_router.coordinator()
    params: dict = {
        "chat_id": chat_id, "message": text, "urgent": True, "debounce": False,
        # T-0755: the append that triggered this relay ALREADY put this exact
        # text in this exact thread (that is what a session writeback is), so
        # letting the transport record it too would show the reader the same
        # message twice. The delivery is unaffected — only the second record is.
        "record_outbound": False,
    }
    if topic_id is not None:
        params["topic_id"] = topic_id
    try:
        result = await client.call_action("tg_notify", params)
    except WorkerError:
        return False
    except Exception:  # noqa: BLE001 — best-effort; must never fail the append
        return False
    return bool(result.get("ok")) and bool(result.get("sent", True))


async def _ensure_attendant(
    request: Request, slug: str, global_user_id: str, message_ref: str,
    thread_id: Any = None,
) -> dict:
    """T-0631: the channel-agnostic half of the intake seam — best-effort wake
    of the (slug, global_user_id) user-conversation attendant for a freshly
    appended USER-authored message, via the worker's ``ensure_user_conversation``
    action (T-0478: idempotent route-to-active / resume / spawn decision).

    Before T-0631 only ``tg_listener`` triggered this (in-process, right after
    its own append call) — a user-authored message landing here via any OTHER
    caller (MCP, a direct API script, and eventually the email/MAX transports
    once they exist, T-0490) was durably recorded but woke nothing. Centralizing
    the trigger HERE means every channel that lands a user message through this
    ONE append endpoint gets the same wake, not just TG.

    ``ensure_user_conversation`` is a tmux_only action; it runs on the
    coordinator because that's also where ``tg_listener`` (and its scheduler
    tick) run (T-0119 __main__ coordinator-only scheduler), so the coordinator
    client is the right target — same one the TG relay call below uses.

    Never raises: a spawn/pane hiccup, or the worker being briefly unreachable,
    must never fail the append (the message is already durable in the store).
    Mirrors tg_listener's own ``_ensure_user_conversation`` backoff detection so
    a saturation refusal is still distinguishable (``parked: True``) from any
    other failure."""
    client = request.app.state.worker_router.coordinator()
    params: dict = {"slug": slug, "global_user_id": global_user_id, "message_ref": message_ref}
    if thread_id is not None and thread_id != "":
        params["thread_id"] = thread_id
    try:
        return await client.call_action("ensure_user_conversation", params)
    except WorkerError as e:
        if "backoff" in str(e):
            return {"ok": False, "parked": True}
        return {"ok": False}
    except Exception:  # noqa: BLE001 — best-effort; must never fail the append
        return {"ok": False}


@worker_router.post("/conversations/{slug}/{global_user_id}/messages")
async def append_message(slug: str, global_user_id: str, request: Request, payload: dict) -> dict:
    """Append one message to the (slug, global_user_id) thread. Worker-only.

    T-0631: this endpoint IS the channel-generic user-mail intake seam — every
    inbound channel (TG's ``tg_listener`` today; a direct MCP/API caller; email
    and MAX once their transports land, T-0490 — they register here, no
    transport is built by this ticket) lands its user-authored messages through
    this ONE append, and gets the same attendant-wake behavior (see
    ``_ensure_attendant``) rather than TG being special-cased.

    Body: ``{author, text, attachments?, timestamp?, channel?, fyi?,
    direction?}`` (``text`` required — empty string is allowed, but the key
    must be present). ``direction`` (T-0755) is ``"out"`` for a record of a
    message WE already delivered — written by the worker's ``outbound_log``
    drain so this file reads as a true interleaved transcript rather than an
    inbox — and ``"in"`` (default, and the meaning of every pre-T-0755 record)
    otherwise. An ``"out"`` append is never relayed (see below) and never wakes
    an attendant. ``author`` must obey the closed T-0755 vocabulary
    (``user`` | ``session:<sid>`` | ``system:<kind>``); anything else is a 400.
    ``channel`` (T-0631) names the inbound transport ("tg", "mcp", "api", ...);
    defaults to "tg" for back-compat with pre-T-0631 callers. ``fyi`` (T-0660)
    marks a PASSIVE, non-actionable append — a session wrote directly to the
    stakeholder, or the stakeholder replied directly to a session, bypassing
    this thread's own attendant (see ``tg_listener.append_conversation_fyi``,
    the sole intended caller). Returns the stored record plus:
    - ``relayed`` (T-0569): when ``author`` is a session writeback
      (``"session:<sid>"``) with non-empty text AND NOT ``fyi``, the text is
      best-effort relayed to the user's Telegram chat (see
      ``_relay_to_telegram``) — otherwise always ``False`` (a user-authored
      append is never relayed back to itself; an ``fyi`` append already went
      out via its own direct-write, so relaying it again would echo the "no
      reply needed" note straight back to the user — T-0660).
    - ``ensured`` (T-0631): present only for a user-authored append (``author
      == "user"``) — the outcome of the attendant-wake (see
      ``_ensure_attendant``), UNLESS ``fyi`` (T-0660: this isn't the
      attendant's own inbox item, just context — the wake is suppressed, not
      run); absent entirely for a session writeback (it already HAS an
      attending session, waking one would be circular).

    ``thread_id`` (T-0676 items 3/6): the bound forum topic this message
    belongs to, when any — isolates the record into that topic's OWN thread
    and scopes the relay/attendant-wake to it, instead of the project's
    mixed history. Omitted / ``None`` (DM, non-topic message — every
    pre-T-0676 caller) behaves byte-identically to before this change.

    ``forwarded_from`` (T-0746 item c): where the content came from when the
    SENDER did not compose it (``"bot"`` for our own output echoed back,
    ``"user:<id>"``/``"chat:<id>"``/a hidden-sender name for a relayed
    message) — see ``conversation_store.append`` and
    ``bot_squad_worker.echo_guard``. Optional; omitted/empty is byte-identical
    to every pre-T-0746 caller. Note this is orthogonal to ``author``: our own
    echoed text arrives as ``system:bot-echo`` with ``direction="in"``, so it
    is neither relayed (not a session writeback) nor treated as the
    stakeholder's own words — but the TG handlers still wake the attendant
    themselves, so a forward is never silently swallowed.

    ``general_feed`` (T-0693 Finding B): marks the record as arriving via an
    explicit ``tg_bindings`` General-feed binding (``thread_id=None`` bound on
    purpose) rather than a genuine DM/non-topic message — the two are
    otherwise indistinguishable once ``thread_id`` is ``None`` either way.
    Optional; defaults to ``False``, byte-identical to every pre-T-0693
    caller."""
    _authenticate_worker(request)
    if "text" not in payload:
        raise HTTPException(status_code=400, detail="text required")
    fyi = bool(payload.get("fyi", False))
    thread_id = payload.get("thread_id")
    general_feed = bool(payload.get("general_feed", False))
    direction = payload.get("direction")
    delivered = bool(payload.get("delivered", False))
    try:
        record = CS.append(
            _data_dir(request),
            slug,
            global_user_id,
            author=str(payload.get("author") or "user"),
            text=payload.get("text"),
            attachments=payload.get("attachments"),
            timestamp=payload.get("timestamp"),
            channel=payload.get("channel"),
            fyi=fyi,
            thread_id=thread_id,
            general_feed=general_feed,
            direction=direction,
            delivered=delivered,
            forwarded_from=payload.get("forwarded_from"),
        )
    except ValueError as e:
        # An unsafe slug / global_user_id segment, an author outside the
        # T-0755 vocabulary, or an unknown direction.
        raise HTTPException(status_code=400, detail=str(e))

    relayed = False
    author = str(record.get("author") or "")
    text = str(record.get("text") or "")
    # T-0755: `delivered` is what makes recording an outbound send safe here.
    # This endpoint RELAYS a session-authored append to Telegram; a delivered
    # record is one the transport ALREADY put on the wire, so relaying it would
    # send the stakeholder a second copy — and since the relay goes back out
    # through the very transport that writes these records, it would not stop at
    # two. Note this is NOT the same question as `direction`: a session
    # writeback is outbound AND still needs delivering.
    if author.startswith("session:") and text and not fyi and not delivered:
        try:
            relayed = await _relay_to_telegram(request, slug, global_user_id, text, thread_id)
        except Exception:  # noqa: BLE001 — the append already succeeded; never fail it
            relayed = False

    out = dict(record)
    # T-0755: the RESPONSE always states the effective direction, even when the
    # stored line omits it as derivable. A GET already normalizes it on read, so
    # a POST that didn't would make the two surfaces disagree and push the
    # derivation rule onto every HTTP consumer.
    out.setdefault("direction", CS.default_direction(author))
    out.setdefault("delivered", False)
    out["relayed"] = relayed
    if author == "user":
        if fyi:
            out["ensured"] = {"ok": True, "skipped": "fyi"}
        else:
            out["ensured"] = await _ensure_attendant(
                request, slug, global_user_id, str(record.get("timestamp") or ""),
                thread_id,
            )
    return out


@worker_router.get("/conversations/{slug}/{global_user_id}/messages")
def worker_list_conversation(
    slug: str,
    global_user_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
    thread_id: str = Query(default=""),
) -> dict:
    """Worker-token READ of the (slug, global_user_id) thread (T-0542).

    The worker-side counterpart to the session-auth ``list_conversation``: a
    user-conversation ATTENDANT runs in worker context (it holds the
    ``WORKER_API_TOKEN``, not a user JWT), so it cannot use the session-auth
    read surface to review its OWN thread — before this it had to reach into the
    store JSONL directly (an architectural wart). Same store call + pagination/
    search as the session read; token-gated by the shared worker secret (fails
    closed when unset), the established worker->API trust path.

    ``thread_id`` (T-0676 items 3/6): read a bound topic's OWN isolated
    thread instead of the project's mixed (slug, global_user_id) history —
    see ``conversation_store.conv_path``. Empty/omitted behaves exactly as
    before this change."""
    _authenticate_worker(request)
    tid = thread_id or None
    try:
        if q:
            return CS.search(_data_dir(request), slug, global_user_id, q, limit=limit, offset=offset, thread_id=tid)
        return CS.list_messages(_data_dir(request), slug, global_user_id, limit=limit, offset=offset, thread_id=tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---- T-0492: per-(user, server) current-project routing (worker-token) -------
# The same user-communication module owns conversation history AND the hardwired
# routing of unquoted messages to a user's pinned project (voice-04). The worker
# reads/sets the pin through these endpoints; single-writer = API (pins_store).


@worker_router.post("/routing/{global_user_id}/current-project")
def set_current_project(global_user_id: str, request: Request, payload: dict) -> dict:
    """Pin (or switch) the user's current project. Worker-only. ``slug`` must be
    a known project (validated against the registry, so a typo can't strand the
    user on a non-existent project). Returns the stored ``{slug, at}`` record."""
    _authenticate_worker(request)
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=400, detail=f"unknown project: {slug}")
    return pins_store.set_current_project(cfg.data_dir, global_user_id, slug, at=_now_iso())


@worker_router.get("/routing/{global_user_id}/current-project")
def get_current_project(global_user_id: str, request: Request) -> dict:
    """The user's current pinned project slug (``null`` when unset). Worker-only."""
    _authenticate_worker(request)
    cfg = request.app.state.api_config
    return {"slug": pins_store.get_current_project(cfg.data_dir, global_user_id)}


@router.get(
    "/{slug}/{global_user_id}/messages",
    dependencies=[Depends(require_project_read)],
)
def list_conversation(
    slug: str,
    global_user_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
    thread_id: str = Query(default=""),
) -> dict:
    """Paginated thread lookup. With ``q`` set, returns only records whose text
    contains it (case-insensitive); otherwise the full chronological thread.

    T-0493 / voice-04 privacy: the thread is per-user PRIVATE content, so the
    read is project-access-gated (``require_project_read``) — a project-limited
    user requesting another project's conversation gets 403, never the content.
    The conversation is scoped to exactly ONE project (the ``slug`` path key,
    stored under ``data/<slug>/``); this guarantees the read cannot cross slugs
    for a project-limited user. (Binding a live conversational SESSION object to
    one project is the T-0478 seam — deferred; this enforces the DATA-access
    privacy guarantee now.)

    ``thread_id`` (T-0676 items 3/6): read a bound topic's OWN isolated
    thread instead of the project's mixed history. Empty/omitted behaves
    exactly as before this change."""
    tid = thread_id or None
    try:
        if q:
            return CS.search(_data_dir(request), slug, global_user_id, q, limit=limit, offset=offset, thread_id=tid)
        return CS.list_messages(_data_dir(request), slug, global_user_id, limit=limit, offset=offset, thread_id=tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
