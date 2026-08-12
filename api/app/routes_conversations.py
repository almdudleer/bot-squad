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

T-0769: both READ surfaces stamp every page with ``inbound_capture`` — whether
an inbound user message would appear in the thread just read, and where it goes
when it would not. A per-task topic routes the user's words straight to its
pinned session and records only OUR side, which reads as a one-sided
conversation rather than an empty one; two sessions took that for message loss
and escalated it to the stakeholder. See :func:`_inbound_capture`.
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


def _threadless(thread_id: Any) -> bool:
    """Whether ``thread_id`` names no topic — the single spelling of that test,
    used by both the locus lookup and the resolver so they cannot disagree
    about what "no thread" means (``None``, ``""`` and ``"  "`` all count)."""
    return thread_id is None or str(thread_id).strip() == ""


def _effective_thread_id(body_value: Any, query_value: str) -> Any:
    """T-0606: the ONE ``thread_id`` an append acts on, from its two spellings.

    ``append_message`` took its thread from the body only and declared no query
    parameters, so ``?thread_id=11`` was accepted and discarded — leaving the
    relay to guess a destination from the collapsed locus key. Both spellings
    now mean the same thing, and the two ways of getting this wrong are loud:

    * a value that is not an integer names no Telegram ``message_thread_id``
      that can exist, so it is rejected at the edge rather than carried into
      the store and the locus key;
    * body and query disagreeing is not a preference to resolve — picking
      either one silently is exactly the class of defect this ticket is about.

    Raises ``HTTPException(400)`` for both. Returns the body value untouched
    when no query value is given, so every pre-T-0606 caller is unaffected.
    """
    q = (query_value or "").strip()
    if not q:
        return body_value
    try:
        parsed: Any = int(q)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"thread_id must be an integer, got {q!r}",
        )
    if _threadless(body_value):
        return parsed
    if str(body_value).strip() != str(parsed):
        raise HTTPException(
            status_code=400,
            detail=(
                f"thread_id given twice and they disagree: body {body_value!r} "
                f"vs query {q!r} — which topic this message belongs to cannot "
                f"be guessed"
            ),
        )
    return body_value


def _load_locus_map(request: Request) -> dict:
    """The worker-owned locus map as it is on disk, or ``{}``.

    Best-effort by contract: this sits inside the relay path, which NEVER
    raises and NEVER blocks the append, so a missing or corrupt file is "no
    locus recorded" rather than an error.
    """
    cfg = request.app.state.api_config
    path = cfg.data_dir / "_worker" / "conversation_locus.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("routes_conversations: could not parse %s: %s", path, e)
        return {}
    return raw if isinstance(raw, dict) else {}


def _has_thread_scoped_locus(locus_map: dict, slug: str, global_user_id: str) -> bool:
    """Whether this ``(slug, gid)`` has ANY per-topic locus entry — i.e. the
    user has written in a bound forum topic since T-0676 moved records onto the
    thread-scoped key.

    T-0606: this is the discriminator that tells a genuinely thread-less
    conversation apart from one whose thread was LOST on the way here. For a
    user with no such entry the collapsed key is the only record there is and
    keeps deciding (the whole pre-T-0676 world); for a user who lives in topics
    it is stale by construction, because every message they have sent since has
    been written somewhere else.
    """
    prefix = f"{slug}:{global_user_id}:"
    return any(
        str(k).startswith(prefix) and isinstance(v, dict) and v.get("chat_id")
        for k, v in locus_map.items()
    )


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
    rec = _load_locus_map(request).get(_locus_key(slug, global_user_id, thread_id))
    if not isinstance(rec, dict) or not rec.get("chat_id"):
        return None
    return {"chat_id": rec["chat_id"], "thread_id": rec.get("thread_id")}


def _load_topic_bindings(request: Request) -> dict | None:
    """The worker-owned topic-binding map, or ``None`` when it could not be
    read (T-0740 read path; ``None`` split out by T-0769).

    Read-only lookup of ``data/_worker/tg_bindings.json``
    (``bot_squad_worker.tg_bindings``) — the same direct-read-of-a-shared-file
    pattern as :func:`_read_conversation_locus`, and the same best-effort
    contract: a missing/corrupt file never raises.

    ``None`` vs ``{}`` is load-bearing and is why this is a separate function.
    Callers that only want a chat_id can collapse both to "no binding"
    (:func:`_read_topic_binding_chat` does, unchanged). :func:`_inbound_capture`
    may NOT: "I read the map and this thread is not session-routed" and "I could
    not read the map at all" produce the same empty lookup, and reporting the
    second as the first states a specific falsehood exactly when the instrument
    is broken (T-0740 / T-0759's explicit-UNKNOWN rule).
    """
    cfg = request.app.state.api_config
    path = cfg.data_dir / "_worker" / "tg_bindings.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("routes_conversations: could not parse %s: %s", path, e)
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _find_binding(
    bindings: dict, thread_id: Any, slug: str | None = None,
) -> tuple[str, dict] | None:
    """The ``(chat_id, record)`` bound to ``thread_id``, or ``None``. Key
    derivation mirrors ``tg_bindings._key``: ``"<chat_id>:<thread_id>"``.

    ``slug`` filters the search; ``None`` matches any project. A thread id is
    only unique WITHIN a chat, so two chats can legitimately carry the same
    number — which is why the filter belongs in the scan and not in a check on
    its result. :func:`_read_topic_binding_chat` passes a slug (a thread bound
    elsewhere is not this project's relay destination, and must not shadow one
    that is); :func:`_inbound_capture` searches this project first and only
    then anywhere, because "you asked the wrong project" is one of the ways an
    empty page gets misread as loss.
    """
    for key, rec in bindings.items():
        if not isinstance(rec, dict) or not rec.get("slug"):
            continue
        if slug is not None and rec.get("slug") != slug:
            continue
        chat_id, _, bound_thread = str(key).rpartition(":")
        if chat_id and bound_thread == str(thread_id):
            return chat_id, rec
    return None


def _read_topic_binding_chat(request: Request, slug: str, thread_id: Any) -> str:
    """T-0740: the chat_id that ``thread_id`` is a forum topic OF, for ``slug``.

    A thread id is only meaningful INSIDE its chat, so relaying into a topic
    requires both halves. The locus supplies both when it has an entry; this
    supplies the chat half from the binding when it does not (see
    :func:`_resolve_relay_target` rung 2 — the "session speaks first in a
    freshly-created topic" case). ``slug`` is checked, not assumed: a thread
    bound to a DIFFERENT project must never be used as this project's
    destination.

    Unchanged contract: ``""`` for "no usable binding", whatever the reason
    (missing thread_id, unreadable store, foreign project, no match).
    """
    if thread_id is None or str(thread_id).strip() == "":
        return ""
    bindings = _load_topic_bindings(request)
    if bindings is None:
        return ""
    found = _find_binding(bindings, thread_id, slug=slug)
    return found[0] if found else ""


# --- T-0769: what this read DOES and DOES NOT contain -----------------------
#
# ``scope`` vocabulary. One value per way an inbound TG message can (not) reach
# the thread being read. Every value is a statement the server can prove from
# the binding store; there is no default, and no value means "probably fine".
SCOPE_PROJECT = "project"                # not a topic read at all
SCOPE_TOPIC = "topic"                    # bound topic, inbound recorded HERE
SCOPE_SESSION_ROUTED = "session_routed"  # bound topic, inbound goes to a session
SCOPE_UNBOUND = "unbound"                # no binding anywhere for this thread
SCOPE_OTHER_PROJECT = "other_project"    # bound, but to a different slug
SCOPE_UNKNOWN = "unknown"                # the binding store could not be read


def _inbound_capture(
    request: Request, slug: str, thread_id: Any,
) -> dict:
    """Say, on the read itself, whether an INBOUND user message would appear in
    it — and when it would not, say where it goes instead (T-0769).

    THE DEFECT THIS EXISTS FOR. A forum topic whose binding carries a
    ``session_id`` is a T-0660 per-TASK topic: ``tg_listener._handle_topic_bound``
    routes the user's message STRAIGHT to that session and deliberately does not
    append it into the topic's own store (T-0667 — a task-topic message must
    never redirect the project's attendant-reply relay). Our OUTBOUND posts into
    that topic ARE stored. So the read comes back showing our side and not his:
    not an empty result inviting "is my query wrong?", but a ONE-SIDED
    conversation, which reads as "his messages were lost". On 2026-07-28 two
    independent sessions read exactly that off topics 220 and 517, concluded the
    inbound wiring was structurally broken, and escalated message loss to the
    stakeholder — one of them then posted an apology into his topic for a fault
    that had not occurred. T-0770 makes it worse rather than better: it makes
    the session ANSWER into the topic, so the same read now accumulates a
    growing stream of our answers with none of his questions.

    The routing is correct and is NOT what changes. What was missing is that the
    response never SAID any of this, so a correct answer and a broken one are the
    same bytes. This is the T-0772 shape one layer down: the server emits a scope
    marker from the return site that knows the truth, and an UNKNOWN degrades to
    the vaguer TRUE statement instead of a specific false one.

    The prose is part of the fix, not decoration. The ticket's test is whether a
    reader who has never read ``_handle_topic_bound`` can tell a healthy
    session-bound topic from a genuinely dead one, so ``explain`` has to carry
    the reasoning to a reader with no worker source in front of them; the enum
    alone would just be a new thing to look up.

    ``inbound_recorded_in`` is built from ``request.url.path`` rather than a
    hardcoded route, so it is correct for whichever surface is being read (the
    session-auth ``/api/m/conversations/...`` and the worker-token
    ``/api/m/worker/conversations/...`` differ, and both readers exist) and
    cannot drift if a mount prefix moves.
    """
    project_read = f"GET {request.url.path} (no thread_id)"
    tid = "" if thread_id is None else str(thread_id).strip()
    out: dict[str, Any] = {
        "scope": SCOPE_PROJECT,
        "thread_id": tid,
        "records_inbound_here": True,
        "bound_session_id": "",
        "bound_ticket_id": "",
        "inbound_recorded_in": "",
        "explain": (
            "This is the project-level thread for this user: their DMs and "
            "general-feed messages land here, plus passive fyi copies "
            "(author 'system:direct-reply') of anything they wrote straight to "
            "a session in a task topic. A bound forum topic's own messages are "
            "NOT here — read that topic with ?thread_id=<id>."
        ),
    }
    if not tid:
        return out

    bindings = _load_topic_bindings(request)
    if bindings is None:
        out.update(
            scope=SCOPE_UNKNOWN,
            records_inbound_here=None,
            explain=(
                f"The topic-binding store could not be read, so whether inbound "
                f"messages for thread {tid} are recorded here or routed straight "
                f"to a session (T-0660 per-task topic) is UNKNOWN. Do not read an "
                f"empty or one-sided page as message loss on this evidence — it is "
                f"unexplained, not explained. Check "
                f"data/_worker/tg_bindings.json on the install."
            ),
        )
        return out

    # This project first: a thread id is unique only inside its chat, so a
    # same-numbered topic in someone else's chat must never shadow ours and
    # turn a healthy read into a wrong-project explanation.
    found = _find_binding(bindings, tid, slug=slug)
    elsewhere = None if found else _find_binding(bindings, tid)
    if found is None and elsewhere is None:
        out.update(
            scope=SCOPE_UNBOUND,
            records_inbound_here=False,
            explain=(
                f"No project is bound to thread {tid} in any chat, so nothing "
                f"routes into it and this thread is expected to be empty. A "
                f"message sent to an unbound topic is refused by the listener "
                f"(it asks for a binding) rather than captured anywhere. If you "
                f"expected records here, the binding is what is missing — not "
                f"the messages."
            ),
        )
        return out

    if found is None:
        out.update(
            scope=SCOPE_OTHER_PROJECT,
            records_inbound_here=False,
            explain=(
                f"Thread {tid} is a bound forum topic of a DIFFERENT project, "
                f"and this read is scoped to {slug!r} — so it will always be "
                f"empty here no matter what was said in that topic. This is a "
                f"wrong-project query, not missing data. Re-read it under the "
                f"slug that owns it."
            ),
        )
        return out

    _chat_id, rec = found
    session_id = str(rec.get("session_id") or "")
    ticket_id = str(rec.get("ticket_id") or "")
    out["bound_ticket_id"] = ticket_id
    if not session_id:
        out.update(
            scope=SCOPE_TOPIC,
            records_inbound_here=True,
            explain=(
                f"Thread {tid} is bound to {slug!r} with no session pinned, so "
                f"inbound messages from this user ARE appended here (author "
                f"'user'). An empty or inbound-free page for this topic is a "
                f"real absence — nothing is recording them elsewhere."
            ),
        )
        return out

    out.update(
        scope=SCOPE_SESSION_ROUTED,
        records_inbound_here=False,
        bound_session_id=session_id,
        inbound_recorded_in=project_read,
        explain=(
            f"Thread {tid} is a per-task topic pinned to session {session_id}"
            + (f" (ticket {ticket_id})" if ticket_id else "")
            + ". Messages the user sends here are delivered STRAIGHT to that "
            "session and are deliberately never appended to this topic's own "
            "record (T-0660/T-0667), while our outbound posts into the topic "
            "ARE recorded. So this page showing only our side is the DESIGNED "
            "state and is NOT evidence that the user's messages were lost. "
            f"Their words are recorded as passive fyi entries (author "
            f"'system:direct-reply') in the project thread: {project_read}. "
            "To make this topic capture inbound like an ordinary one, unpin the "
            "session ('/pin-session off')."
        ),
    )
    return out


def _resolve_relay_target(
    request: Request, slug: str, global_user_id: str, thread_id: Any = None,
) -> tuple[str, int | None]:
    """The ``(chat_id, topic_id)`` half of :func:`_resolve_relay_destination` —
    kept as the resolver's public shape because a caller that only needs the
    destination should not have to unpack a refusal it is going to ignore."""
    chat_id, topic_id, _refusal = _resolve_relay_destination(
        request, slug, global_user_id, thread_id)
    return chat_id, topic_id


def _resolve_relay_destination(
    request: Request, slug: str, global_user_id: str, thread_id: Any = None,
) -> tuple[str, int | None, dict]:
    """T-0569 / T-0667: resolve the ``(chat_id, topic_id)`` to relay a session
    reply to, plus a REFUSAL naming why when nothing may be resolved.

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
    relay" (``relayed: false``), never an error. The third element is a
    ``{"reason", "detail"}`` dict whenever the chat id is empty, so a caller
    can say WHY nothing was sent instead of failing mutely.

    T-0606 — the collapsed locus key is no longer a silent fallback
    --------------------------------------------------------------
    Rung 1 keys on ``thread_id``, and before T-0676 there was only ONE key per
    ``(slug, gid)`` — the collapsed ``slug:gid``, holding whatever topic that
    user last wrote in. T-0676 moved every new record onto the thread-scoped
    key without rewriting or expiring the old one, so a collapsed entry FREEZES
    on the topic that happened to be current the day the user's traffic moved.

    Any path that lost ``thread_id`` then resolved that frozen entry and
    delivered into a topic the request never named — and reported
    ``relayed: true``, because it did deliver, somewhere. That ran for three
    weeks on the stakeholder's only channel: replies into a topic closed on
    2026-08-07 while he watched a live one. **Success and miss were
    indistinguishable, which is the property that let it survive**, not the bad
    JSON value; hand-editing the entry would only re-arm the trap for the next
    user who writes in a second topic.

    So for a thread-less relay, exactly one of three things is true:

    * the collapsed record names NO topic (``thread_id`` null) — a genuine DM
      or an explicit General-feed binding (T-0693). "No topic" is a real
      answer, not a lost one: deliver, as before.
    * this user has thread-scoped locus entries — they converse in topics, and
      a reply that names none of them cannot be placed. **Refuse.** Not the
      collapsed key's stale topic, and not the DM either: an answer in the
      wrong place reads as an answer to a question he did not ask.
    * neither — the entire pre-T-0676 world, where the collapsed key is the
      only record in existence and no newer signal contradicts it. Unchanged.

    A relay that DOES name its thread is untouched by all of this, which is
    what keeps the ``thread_id``-in-the-body path (the live workaround) working
    exactly as it does today.

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
    if locus and not (_threadless(thread_id) and not _threadless(locus.get("thread_id"))):
        # Every case but one: the locus answers. The exception is the T-0606
        # trap — a THREAD-LESS request served by a collapsed record that names
        # a topic, i.e. a destination the caller never asked for. That falls to
        # the ambiguity check below rather than being delivered.
        return locus["chat_id"], locus.get("thread_id"), {}
    if _threadless(thread_id) and _has_thread_scoped_locus(
        _load_locus_map(request), slug, global_user_id,
    ):
        log.warning(
            "routes_conversations: refusing a thread-less relay for %s/%s — the "
            "user has per-topic loci, so no destination can be inferred (T-0606)",
            slug, global_user_id,
        )
        return "", None, {
            "reason": "thread_undetermined",
            "detail": (
                "this user's conversation is bound to forum topics and this "
                "relay named none, so its destination is unknown — pass "
                "thread_id (body or ?thread_id=) to deliver it"
            ),
        }
    if locus:
        # Pre-T-0676 only: a collapsed record naming a topic, for a user with
        # no thread-scoped entry to contradict it. The sole record in
        # existence, and the behaviour every such user has had since T-0667.
        return locus["chat_id"], locus.get("thread_id"), {}
    bound_chat = _read_topic_binding_chat(request, slug, thread_id)
    if bound_chat:
        try:
            return bound_chat, int(thread_id), {}
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
        return user.tg_user_id.strip(), None, {}
    project = cfg.project(slug)
    if project is not None and (project.tg_chat or "").strip():
        return project.tg_chat.strip(), getattr(project, "tg_topic_id", None), {}
    return "", None, {
        "reason": "no_destination",
        "detail": (
            f"no locus, topic binding, linked Telegram account or configured "
            f"tg_chat resolves a destination for {slug}/{global_user_id}"
        ),
    }


async def _relay_to_telegram(
    request: Request, slug: str, global_user_id: str, text: str, thread_id: Any = None,
    sender_sid: str = "",
) -> tuple[bool, dict, dict]:
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

    ``sender_sid`` (T-0758): the session whose writeback this is, taken from
    the append's own ``author`` (``session:<sid>``) — the ONLY place the
    author is known, since this relay spells out an explicit ``chat_id`` and
    passes no ``sid``/``slug`` at all. That is exactly why the stakeholder saw
    ``[bot-squad user-conversation]`` on one project and nothing on the other:
    the tag was a typing habit, and this path had no identity to tag with. The
    tag itself is still applied at the TRANSPORT (``sender_tag.compose``) —
    what is added here is the fact of WHO wrote it, which no other layer has.
    It is deliberately not passed as ``sid``: that would also enter
    ``tg_notify``'s destination ladder and pin ``tg_reply_map``, re-routing
    the user's next quoted reply down the direct-to-session path instead of
    the attendant's — a change this ticket has no business making.
    """
    chat_id, topic_id, refusal = _resolve_relay_destination(
        request, slug, global_user_id, thread_id)
    if not chat_id:
        # T-0606: the third element is why. An unrelayed reply that says
        # nothing is the shape this ticket exists to remove — the caller
        # holding the thread is the only party that can supply it.
        return False, {}, refusal
    client = request.app.state.worker_router.coordinator()
    params: dict = {
        "chat_id": chat_id, "message": text, "urgent": True, "debounce": False,
        # T-0755: the append that triggered this relay ALREADY put this exact
        # text in this exact thread (that is what a session writeback is), so
        # letting the transport record it too would show the reader the same
        # message twice. The delivery is unaffected — only the second record is.
        "record_outbound": False,
    }
    if sender_sid:
        params["sender_sid"] = sender_sid
    if topic_id is not None:
        params["topic_id"] = topic_id
    try:
        result = await client.call_action("tg_notify", params)
    except WorkerError as e:
        return False, {}, {"reason": "transport_error", "detail": str(e)}
    except Exception:  # noqa: BLE001 — best-effort; must never fail the append
        return False, {}, {"reason": "transport_error", "detail": "relay raised"}
    relayed = bool(result.get("ok")) and bool(result.get("sent", True))
    # T-0761: the destination Telegram itself reported, so `relayed` stops being
    # a confirmation that cannot fail. It was true in BOTH outcomes — a reply
    # that fell back to the private DM instead of the resolved topic still
    # returned true — which is why T-0740 ran unnoticed for as long as it did.
    delivery = result.get("delivery")
    refusal = {} if relayed else {
        "reason": "transport_declined",
        "detail": str(result.get("error") or result.get("reason") or
                      "the transport accepted the call but did not send"),
    }
    return relayed, delivery if isinstance(delivery, dict) else {}, refusal


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

    ``ensure_user_conversation`` is a tmux_only action.  For an attached
    GlobalUser it must run on that account's per-user worker, not on the
    coordinator: the latter has a different tmux server, credentials and agent
    provider.  An unavailable attached-user worker fails closed (the durable
    append remains) instead of spawning under the coordinator.  Unattached
    identities retain the legacy coordinator route.

    Never raises: a spawn/pane hiccup, or the worker being briefly unreachable,
    must never fail the append (the message is already durable in the store).
    Mirrors tg_listener's own ``_ensure_user_conversation`` backoff detection so
    a saturation refusal is still distinguishable (``parked: True``) from any
    other failure."""
    # The `and global_user_id` guard: most accounts carry NO
    # ``attached_to_global_user``, so an empty gid compares equal to every
    # unattached entry and this would pick whichever one auth.toml lists first
    # — a routing decision made by file order. UNREACHABLE from HTTP (an empty
    # path segment 404s here — measured, not assumed), so this is symmetry with
    # the worker's `_linux_user_for_global_user`, where the same input IS
    # reachable because identity resolution hands back "" on failure. Kept so
    # the two halves cannot drift into disagreeing about the same input.
    linux_user = next(
        (
            meta.linux_user
            for meta in request.app.state.auth_config.user_meta.values()
            if global_user_id and meta.attached_to_global_user == global_user_id
        ),
        "",
    )
    try:
        client = (
            request.app.state.worker_router.for_user_strict(linux_user)
            if linux_user
            else request.app.state.worker_router.coordinator()
        )
    except WorkerError as e:
        # Same posture as the worker half: fail closed, but never silently.
        # The append above is already durable, so what is lost here is the
        # WAKE — nobody is attending the message the user just sent.
        log.error(
            "attendant NOT woken for %s/%s: no worker for %s (%s)",
            slug, global_user_id, linux_user, e,
        )
        return {"ok": False, "user_worker_unavailable": True}
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
async def append_message(
    slug: str, global_user_id: str, request: Request, payload: dict,
    thread_id_query: str = Query(default="", alias="thread_id"),
) -> dict:
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

    T-0606: ``thread_id`` may equally be given as the ``?thread_id=`` QUERY
    parameter. This endpoint declared no query parameters at all, so a caller
    that spelled it that way got a 200 and had it dropped on the floor — the
    thread then resolved off the collapsed locus key and the reply was
    delivered into a topic nobody named (see ``_resolve_relay_destination``).
    A silently-ignored routing parameter is the same defect as a silently-wrong
    destination, so the two spellings are now equivalent, a non-integer value
    is a 400, and giving BOTH with different values is a 400 rather than a coin
    toss over where the message lands.

    ``relay_error`` (T-0606): present only when a relay was attempted and did
    not deliver — ``{reason, detail}``, where ``reason`` is
    ``thread_undetermined`` (this user converses in topics and the append named
    none — pass ``thread_id``), ``no_destination`` (nothing resolves a chat at
    all), ``transport_error`` or ``transport_declined``. Absent means either
    "delivered" or "no relay was due"; it is never a substitute for
    ``relayed``, which stays the single answer to "did it go".

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
    caller.

    ``reply_to`` (T-0780): the message this one REPLIED to, as
    ``{text, message_id?, author?, author_name?, fragment?}`` — a nested object
    and never text folded into ``text``, because the quoted words are somebody
    else's (see ``conversation_store.append`` and
    ``bot_squad_worker.reply_quote``). Optional; a non-dict value is a 400, and
    unknown keys inside it are dropped. Absence means "not stated", NEVER "this
    was not a reply" — every record written before T-0780 lost the quote at
    ingestion."""
    _authenticate_worker(request)
    if "text" not in payload:
        raise HTTPException(status_code=400, detail="text required")
    fyi = bool(payload.get("fyi", False))
    transport_managed_ensure = payload.get("ensure_attendant") is False
    thread_id = _effective_thread_id(payload.get("thread_id"), thread_id_query)
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
            reply_to=payload.get("reply_to"),
        )
    except ValueError as e:
        # An unsafe slug / global_user_id segment, an author outside the
        # T-0755 vocabulary, an unknown direction, or a non-dict `reply_to`.
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
    relayed_to: dict = {}
    relay_error: dict = {}
    if author.startswith("session:") and text and not fyi and not delivered:
        try:
            relayed, relayed_to, relay_error = await _relay_to_telegram(
                request, slug, global_user_id, text, thread_id,
                # T-0758: `author` is `session:<sid>` here by the branch
                # condition above, so the SID is the part after the colon.
                sender_sid=author.split(":", 1)[1].strip(),
            )
        except Exception:  # noqa: BLE001 — the append already succeeded; never fail it
            relayed = False
            relayed_to = {}
            relay_error = {"reason": "transport_error", "detail": "relay raised"}

    out = dict(record)
    # T-0755: the RESPONSE always states the effective direction, even when the
    # stored line omits it as derivable. A GET already normalizes it on read, so
    # a POST that didn't would make the two surfaces disagree and push the
    # derivation rule onto every HTTP consumer.
    out.setdefault("direction", CS.default_direction(author))
    out.setdefault("delivered", False)
    out["relayed"] = relayed
    # T-0761: WHERE it went, when the transport could say. Empty when nothing
    # was relayed, or when the worker predates this field — an absent key means
    # "not stated", never "delivered nowhere". `thread_known` inside it is what
    # separates "Telegram named a topic" from "Telegram did not say".
    if relayed_to:
        out["relayed_to"] = relayed_to
    # T-0606: a relay that did NOT deliver says why, in the same response that
    # says it didn't. `relayed: false` alone is a fact with no owner — the
    # caller holds the thread this path could not infer, and is the only party
    # that can act on the answer.
    if relay_error and not relayed:
        out["relay_error"] = relay_error
    if author == "user":
        if fyi:
            out["ensured"] = {"ok": True, "skipped": "fyi"}
        elif transport_managed_ensure:
            # Telegram's listener records first, then performs its own
            # identity-aware ensure. ONE writer for the wake: the transport
            # that already resolved the sender owns it, so the two ensures
            # cannot race to spawn or double-nudge the same (slug, gid).
            # (Historically the second one also landed on the coordinator; the
            # per-user routing above fixes that independently, so what this
            # flag is for now is the single-writer property, not the account.)
            out["ensured"] = {"ok": True, "skipped": "transport_managed"}
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
    offset: int | None = Query(default=None, ge=0),
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
    before this change.

    ``offset`` omitted (T-0850): the page returned is the most recent
    ``limit`` records, not the oldest — a freshly-spawned attendant's boot
    read hits this route with no query string at all (see
    ``actions._user_conversation_boot_prompt``), and the oldest slice of a
    long thread is stale context, not "no context yet". Pass ``offset=0``
    explicitly to walk the thread forward from its start instead.

    ``inbound_capture`` (T-0769): carried here for the same reason as on the
    session-auth read — see :func:`_inbound_capture`. The user-conversation
    ATTENDANT reads through THIS surface (it holds the worker token, not a JWT)
    and T-0676's boot prompt points it at a topic-scoped read, so it is exactly
    a reader that can be handed a one-sided topic."""
    _authenticate_worker(request)
    tid = thread_id or None
    try:
        if q:
            page = CS.search(_data_dir(request), slug, global_user_id, q, limit=limit, offset=offset, thread_id=tid)
        else:
            page = CS.list_messages(_data_dir(request), slug, global_user_id, limit=limit, offset=offset, thread_id=tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    page["inbound_capture"] = _inbound_capture(request, slug, tid)
    return page


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
    offset: int | None = Query(default=None, ge=0),
    q: str = Query(default=""),
    thread_id: str = Query(default=""),
) -> dict:
    """Paginated thread lookup. With ``q`` set, returns only records whose text
    contains it (case-insensitive); otherwise the full chronological thread.

    ``offset`` omitted (T-0850): returns the most recent ``limit`` records
    (the tail), not the oldest — see ``conversation_store._paginate``. Pass
    ``offset=0`` explicitly for forward pagination from the start.

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
    exactly as before this change.

    ``inbound_capture`` (T-0769): every page also states whether an INBOUND
    message from this user would appear in it, and where it goes when it would
    not. Two sessions read message loss off a correct answer here and escalated
    it to the stakeholder as urgent; the records were right and the response
    simply never said what it was. See :func:`_inbound_capture` — the marker is
    additive (the ``total``/``limit``/``offset``/``messages`` keys are byte-for-byte
    unchanged), and it is emitted on the search path too, since grepping a
    session-routed topic for the user's own words is the read MOST likely to be
    mistaken for loss."""
    tid = thread_id or None
    try:
        if q:
            page = CS.search(_data_dir(request), slug, global_user_id, q, limit=limit, offset=offset, thread_id=tid)
        else:
            page = CS.list_messages(_data_dir(request), slug, global_user_id, limit=limit, offset=offset, thread_id=tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    page["inbound_capture"] = _inbound_capture(request, slug, tid)
    return page
