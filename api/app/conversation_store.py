"""Per-(project, user) TG conversation history store (T-0489).

The durable record of the full Telegram user-conversation thread — kept "just
like we keep the jsonl files for the cloud sessions, which we can always look
up" (voice-04). This is the substrate for continuity across session recycles
(feeds the user-conversation seam, M5-T8/T9): a recycled or token-capped
session terminates, but the conversation it was attending lives on here so the
next session can pick it up.

Design notes
------------
* Keyed by ``(project_slug, global_user_id)``. The ``global_user_id`` is the
  cross-server mothership identity established by T-0488's resolve-or-link; the
  conversation is anchored on it (not the raw TG sender id) so recognition is
  consistent across servers. The conversation is ALSO project-scoped — voice-04
  is explicit that conversational sessions attach to ONE project (privacy: a
  user has limited project access, so a cross-project thread would leak).
* Mothership-level store (lives under ``_mothership/``), mirroring
  ``mothership_users_store.py`` — the bot's user-communication is centralized on
  the mothership (voice-04). One thread = one append-only JSONL file, one record
  per line (timestamp, author, text, attachments), echoing the Claude jsonl
  shape so the record is "always lookup-able".
* SINGLE WRITER = the API. The worker never writes this file directly; it POSTs
  to the API append endpoint (the same worker->API token-gated path T-0488
  established). So there is no dual-writer divergence. An in-process lock guards
  concurrent API appends from interleaving their lines.
* Atomic-enough: appends are O_APPEND line writes (each record < PIPE_BUF, so
  the kernel won't tear a single line); reads tolerate a torn/garbage trailing
  line by skipping it rather than failing the whole lookup.

Shape on disk (``data/_mothership/conversations/<slug>/<global_user_id>.jsonl``)::

    {"timestamp": "<iso8601>", "author": "user", "text": "...", "attachments": [], "channel": "tg"}
    {"timestamp": "<iso8601>", "author": "session:S-...", "text": "...", "attachments": [], "channel": "tg"}

``channel`` (T-0631): which inbound transport the message arrived on ("tg"
today; "mcp"/"api" for a direct append; "mail"/"max" once those transports
land, T-0490). Additive + back-compat: a record written before T-0631 has no
``channel`` key on disk; reads normalize the absent field to "tg" (every
pre-T-0631 record is TG-origin) so old threads don't silently look
channel-less.

``direction`` (T-0755): ``"in"`` (it reached us) or ``"out"`` (we sent it).
Orthogonal to ``author`` and genuinely independent of it — a ``system:`` line
can be either — which is why one field could not carry both. Before T-0755 the
transports recorded outbound sends nowhere (only ``tg_reply_map``'s
content-less ``message_id -> sid`` pin), so a reader of this file saw a run of
``author=user`` lines and read it as SILENCE; on 2026-07-27 an operator did
exactly that and escalated a three-hour non-response that had not happened.
Additive + back-compat: absent on disk reads back as ``"in"``, which is the
true direction of every record written before this change (all of them were
inbound TG messages or lifecycle notices ABOUT something already delivered).

``author`` vocabulary (T-0755 fixed it; T-0746 consumes it). Exactly three
classes, validated on write so a writer cannot invent a fourth:
``user`` (a HUMAN wrote this content — no detail segment, because a qualified
human is not a thing this store represents), ``session:<sid>`` (an agent
session authored it), ``system:<kind>`` (the system composed it — e.g.
``system:task-lifecycle``). Validation is deliberately shape-only: it cannot
tell that a SYSTEM diagnostic arriving as inbound TG text was mislabelled
``user`` (the live ``"❌ session … not active — message dropped"`` line), because
that writer believes it is recording human text. Fixing THAT is the delivery
half, T-0746 item (c).

``fyi`` (T-0660): a PASSIVE, non-actionable append — a session wrote directly
to the stakeholder, or the stakeholder replied directly to a session,
bypassing this thread's own attendant. Recorded here for context but must
never be treated as this thread's own actionable inbox item (the caller,
``routes_conversations.append_message``, suppresses the attendant-wake / TG
relay side effects a normal append of that author would otherwise trigger).
Additive + back-compat: omitted from the record entirely when False (the
overwhelming common case) — absent on disk reads back as ``False``, so no
pre-T-0660 record is affected.

A per-task topic file holds ONE SIDE ON PURPOSE (T-0769). When a topic's
binding names a ``session_id``, ``tg_listener._handle_topic_bound`` delivers the
user's message straight to that session and never appends it into
``<gid>/t<thread>.jsonl``, while our outbound posts into the topic are stored
normally. So that file legitimately reads as a monologue — our answers with none
of their questions — and their words are in the ``<gid>.jsonl`` beside it, as
``fyi`` records authored ``system:direct-reply``. Two sessions read the
monologue as message loss on 2026-07-28 and escalated it to the stakeholder as
urgent. Anything reading these files directly should check
``data/_worker/tg_bindings.json`` for the thread before concluding anything from
an absence; the HTTP reads say it outright — see
``routes_conversations._inbound_capture``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_append_lock = threading.Lock()

# A path segment (slug / global_user_id) must be a single, non-traversing name.
# Both real inputs satisfy this: project slugs are lowercase alnum+dash, and a
# global_user_id is ``gu_<hex>``. Anything with a separator or "." escape is
# rejected so a crafted value can never walk outside the conversations root.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_segment(value: str) -> str:
    """Validate a single path segment; raise ``ValueError`` on anything that
    could traverse. Returns the value unchanged when safe."""
    v = str(value or "")
    if v in ("", ".", "..") or not _SEGMENT_RE.match(v) or v.startswith("."):
        raise ValueError(f"unsafe conversation path segment: {value!r}")
    return v


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# T-0755: the closed author vocabulary (see module docstring). Mirrored by
# ``bot_squad_worker.outbound_log.is_valid_author`` — the worker composes
# authors and the API stores them, so the two must agree; ``AUTHOR_CLASSES``
# is asserted identical by an agreement test on both sides.
AUTHOR_CLASSES = ("user", "session", "system")

#: Records we sent, vs records that reached us.
DIRECTIONS = ("in", "out")


def default_direction(author: str) -> str:
    """The direction implied by ``author`` when a record doesn't state one.

    Used on WRITE (a caller that doesn't pass ``direction``) and on READ (every
    record written before T-0755, none of which carries the field). Deriving it
    rather than defaulting everything to ``"in"`` is what retro-classifies the
    existing history CORRECTLY with no migration: the 233 ``session:``-authored
    and 198 ``system:``-authored records already on disk are all things we sent,
    and rewriting the stakeholder's own file to say so would be exactly the kind
    of silent edit this ticket exists to prevent.

    ``user`` is the only class that is inbound by definition — a human wrote it,
    and we never write as him (:func:`append` refuses that combination).
    ``system:`` is outbound by default because every system record in this store
    is a notice we DELIVERED (lifecycle notices, dropped-message diagnostics); an
    inbound system record is possible in principle and must say ``direction="in"``
    explicitly.
    """
    return "in" if author_class(author) == "user" else "out"


def is_valid_author(author: str) -> bool:
    """True when ``author`` obeys the T-0755 convention.

    ``user`` takes no detail segment; ``session``/``system`` require one. A
    bare ``"session"`` or an invented class is rejected — the point of a closed
    vocabulary is that a reader can trust ``author`` to answer "did a human say
    this?" without inspecting the text.
    """
    a = str(author or "").strip()
    if a == "user":
        return True
    cls, sep, detail = a.partition(":")
    return bool(sep) and cls in ("session", "system") and bool(detail.strip())


def author_class(author: str) -> str:
    """The class half of ``author``, or ``""`` when it breaks the convention."""
    a = str(author or "").strip()
    if a == "user":
        return "user"
    return a.partition(":")[0] if is_valid_author(a) else ""


#: Fields a ``reply_to`` may carry. Anything else is dropped rather than
#: stored: this is the one write path into the store, and a nested blob a
#: caller can put arbitrary keys into is a schema that stops being one.
REPLY_TO_FIELDS = ("text", "message_id", "author", "author_name", "fragment")

#: Defensive cap on a quoted original's text. Telegram's own per-message limit
#: is 4096 characters, so for a TG-origin record this will essentially never
#: fire — it bounds a NON-TG caller (T-0631 makes this endpoint the intake seam
#: for every channel), not routine trimming. The worker applies its own,
#: much tighter cap when rendering a quote into an injected prompt
#: (``reply_quote.QUOTE_CAP``); the two numbers bound two different things and
#: are deliberately not shared.
REPLY_TO_TEXT_CAP = 4096


def _sanitize_reply_to(reply_to: Any) -> dict | None:
    """The stored form of a quoted original, or ``None`` when there is nothing
    to store. See :func:`append`'s ``reply_to`` paragraph for the design.

    Raises ``ValueError`` on a non-dict, non-``None`` value — a caller passing a
    bare string means it is about to fold somebody else's words into a record
    as if they were one blob of the sender's, which is the exact confusion the
    nested field exists to prevent. Failing loudly at the one write path is what
    makes that a guarantee rather than a convention (same reasoning as the
    T-0755 author vocabulary two functions up).
    """
    if reply_to is None:
        return None
    if not isinstance(reply_to, dict):
        raise ValueError(
            f"reply_to must be a dict of {REPLY_TO_FIELDS}, got {type(reply_to).__name__}")
    out: dict = {}
    for k in REPLY_TO_FIELDS:
        if k not in reply_to or reply_to[k] is None:
            continue
        v = reply_to[k]
        if k == "message_id":
            out[k] = v
            continue
        s = str(v)
        if k == "text" and len(s) > REPLY_TO_TEXT_CAP:
            s = s[:REPLY_TO_TEXT_CAP]
            out["truncated"] = True
        if s:
            out[k] = s
    # A quote with no text is not a quote — it is an empty slot that would read
    # on every consumer as "he replied to something blank".
    if not out.get("text") and not out.get("fragment"):
        return None
    return out


def conversations_root(data_dir: Path) -> Path:
    """Root holding every per-(project, user) conversation thread."""
    return Path(data_dir) / "_mothership" / "conversations"


def conv_path(
    data_dir: Path, slug: str, global_user_id: str, thread_id: Any = None,
) -> Path:
    """Path to one (project, user[, forum thread]) conversation thread (a
    JSONL file).

    T-0676 items 3/6: a bound forum topic's messages were colliding into the
    SAME (slug, global_user_id) file as every other topic of that project,
    collapsing per-topic context (cross-topic bleed) and making the last
    written topic win for outbound relay (misrouted replies). ``thread_id``
    (the TG ``message_thread_id`` of a bound topic) is OPTIONAL and additive:
    ``None`` (the overwhelming pre-existing case — DMs and unbound-group
    messages have no thread) resolves to the EXACT pre-T-0676 path, so every
    thread recorded before this change is unaffected. A given ``thread_id``
    resolves to its OWN nested file, isolated from the bare per-user file and
    from every other thread.
    """
    s = _safe_segment(slug)
    g = _safe_segment(global_user_id)
    if thread_id is None or thread_id == "":
        return conversations_root(data_dir) / s / f"{g}.jsonl"
    t = _safe_segment(str(thread_id))
    return conversations_root(data_dir) / s / g / f"t{t}.jsonl"


def append(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    *,
    author: str,
    text: str,
    attachments: list | None = None,
    timestamp: str | None = None,
    channel: str | None = None,
    fyi: bool = False,
    thread_id: Any = None,
    general_feed: bool = False,
    direction: str | None = None,
    delivered: bool = False,
    forwarded_from: str | None = None,
    reply_to: dict | None = None,
) -> dict:
    """Append one message record to the thread; return the stored record.

    ``author`` is free-form ("user" for inbound TG, "session:<sid>" for an
    attending session's writeback). ``attachments`` defaults to ``[]``.
    ``timestamp`` defaults to now (UTC, ISO-8601). ``channel`` (T-0631) is the
    inbound transport ("tg", "mcp", "api", ...); defaults to "tg" since every
    caller predating T-0631 is TG-origin. ``fyi`` (T-0660) marks a passive,
    non-actionable append (see module docstring); omitted from the stored
    record when False. ``thread_id`` (T-0676 items 3/6) isolates a bound forum
    topic's thread from the rest of the (slug, global_user_id) history — see
    :func:`conv_path`; omitted from the stored record when ``None``.

    ``direction`` (T-0755): ``"out"`` for something WE sent, ``"in"`` for
    something that reached us; defaults from ``author`` (see
    :func:`default_direction`) and is stored only when it overrides that.
    ``delivered`` (T-0755) marks a record of a message that is ALREADY on the
    wire, which is what stops the append endpoint from relaying it again.
    Raises ``ValueError`` on an unknown direction, on an ``author`` outside the
    closed vocabulary, or on ``author="user"`` with ``direction="out"`` — this
    is the one write path into the store, so rejecting here is what makes the
    vocabulary a guarantee rather than a convention.

    ``general_feed`` (T-0693 Finding B): ``thread_id=None`` is overloaded — a
    genuine DM/non-topic message and an explicit ``tg_bindings`` General-feed
    binding (bound ON PURPOSE with no thread) both land in the SAME bare
    ``(slug, global_user_id)`` file. This flag marks the record as the latter,
    so the two are distinguishable on read instead of silently identical;
    omitted from the stored record when ``False`` (every pre-T-0693 caller).

    ``forwarded_from`` (T-0746 item c): where this content came from when the
    SENDER did not compose it — ``"bot"`` for our own output that re-entered on
    the inbound channel, ``"user:<id>"`` / ``"chat:<id>"`` / a hidden-sender
    name for somebody else's message the sender relayed. The author vocabulary
    cannot express this on its own: a forward of another human's words is still
    ``author="user"`` (a human did write them) and would otherwise read as the
    sender's own. Set by ``tg_listener`` via ``echo_guard.classify_inbound``;
    omitted from the stored record when empty, so every record whose sender DID
    compose it stays byte-identical to its pre-T-0746 shape.

    ``reply_to`` (T-0780): the message this one was a REPLY to —
    ``{text, message_id?, author?, author_name?, fragment?}``, built by
    ``bot_squad_worker.reply_quote.extract``. A NESTED field, never folded into
    ``text``, and that is the whole design: the quoted words belong to somebody
    else (usually to us), so on a record authored ``"user"`` a concatenated
    quote would be the T-0746 lie — the stakeholder credited with our prose —
    in the one file whose job is to say what he actually said. Unknown keys are
    dropped and ``text`` is capped defensively here (see
    :data:`REPLY_TO_TEXT_CAP`) because this is the one write path into the store
    and it must not trust a caller. Omitted from the stored record when absent
    or when it carries no text.

    **An absent ``reply_to`` is NOT evidence that a record was not a reply**, and
    for that reason it is deliberately NOT defaulted on read the way
    ``forwarded_from`` and ``direction`` are. Every record written before T-0780
    dropped the quote at ingestion, so ``setdefault("reply_to", None)`` would
    assert "this was not a reply" over 1298 historical records, some of which
    were. That is the explicit-UNKNOWN rule: an absent field must not read as a
    real negative.
    """
    author = str(author)
    if not is_valid_author(author):
        raise ValueError(
            f"author {author!r} is outside the T-0755 vocabulary "
            f"({'|'.join(AUTHOR_CLASSES)}; 'user' bare, the others '<class>:<detail>')")
    dirn = str(direction or default_direction(author))
    if dirn not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    if dirn == "out" and author == "user":
        # We never send AS the stakeholder. `author="user"` means a human wrote
        # this content, so an outbound one would be us putting words in his
        # mouth in the very record used to audit what was said.
        raise ValueError("author='user' cannot be direction='out'")
    record = {
        "timestamp": timestamp or _now_iso(),
        "author": author,
        "text": "" if text is None else str(text),
        "attachments": list(attachments) if attachments else [],
        "channel": str(channel) if channel else "tg",
        "fyi": bool(fyi),
    }
    # Omitted whenever the author already implies it — which is every record
    # written today — so a new line stays BYTE-IDENTICAL to its pre-T-0755
    # shape. Written only when a caller OVERRIDES the derivation (an inbound
    # system diagnostic), which is the one case a reader could not infer.
    if dirn != default_direction(author):
        record["direction"] = dirn
    if delivered:
        # T-0755: this record is of a message ALREADY on the wire (the outbound
        # log's drain, task_chat's post-send notice) — as opposed to a session
        # writeback, which is a message to BE delivered and which this store's
        # append endpoint relays. Without the distinction, recording a delivered
        # send would make the endpoint deliver it a second time, and the relay
        # goes back out through the transport that writes these records, so it
        # would not stop at two.
        record["delivered"] = True
    if thread_id is not None and thread_id != "":
        record["thread_id"] = thread_id
    if general_feed:
        record["general_feed"] = True
    if forwarded_from:
        record["forwarded_from"] = str(forwarded_from)
    quoted = _sanitize_reply_to(reply_to)
    if quoted:
        record["reply_to"] = quoted
    p = conv_path(data_dir, slug, global_user_id, thread_id)
    line = json.dumps(record, ensure_ascii=False)
    with _append_lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return record


def _read_all(
    data_dir: Path, slug: str, global_user_id: str, thread_id: Any = None,
) -> list[dict]:
    """Read every record in append order, skipping any torn/garbage line."""
    p = conv_path(data_dir, slug, global_user_id, thread_id)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        log.warning("conversation_store: unreadable thread at %s", p)
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # A torn trailing write or hand-edited garbage line — skip it
            # rather than failing the whole lookup (the record is best-effort).
            continue
        if isinstance(rec, dict):
            rec.setdefault("channel", "tg")  # T-0631: pre-migration records are TG-origin
            rec.setdefault("fyi", False)  # T-0660: absent -> not a passive/FYI append
            rec.setdefault("general_feed", False)  # T-0693: absent -> not a General-feed binding
            # T-0755: absent -> derived from the author, which classifies every
            # pre-T-0755 record correctly without touching the file. No
            # migration is needed and none must be done: silently rewriting the
            # record of what the stakeholder said is the class of thing this
            # ticket exists to prevent.
            rec.setdefault("direction", default_direction(rec.get("author", "")))
            rec.setdefault("delivered", False)
            # T-0746: absent -> the sender composed it, which is true of every
            # record written before the guard existed EXCEPT the one line it
            # exists for. That line is deliberately left as it stands: rewriting
            # the stakeholder's own file is the failure this ticket is about.
            rec.setdefault("forwarded_from", "")
            out.append(rec)
    # T-0755: order by TIMESTAMP, not by arrival. Until this ticket the file was
    # written by one lane (inbound) so append order WAS chronological; now the
    # outbound half arrives via a drain tick that can lag its send by up to its
    # interval, which would put a reply visibly after a message it preceded —
    # in the one file whose job is to show who spoke when. Stable, so the three
    # records sharing 2026-07-27T04:57:58Z keep their arrival order among
    # themselves. A record with no timestamp (``append`` always writes one, so
    # this means a hand-edited line) inherits its predecessor's rather than
    # sorting to the front of the whole thread on an empty key.
    keys: list[str] = []
    prev = ""
    for rec in out:
        prev = str(rec.get("timestamp") or "") or prev
        keys.append(prev)
    out = [r for _, r in sorted(zip(keys, out), key=lambda kv: kv[0])]
    return out


def _paginate(records: list[dict], *, limit: int, offset: int | None) -> dict:
    total = len(records)
    limit = max(0, int(limit))
    if offset is None:
        # T-0850: no offset named -> the caller wants recent context, not the
        # start of a (possibly months-old) thread. A fresh user-conversation
        # session's boot prompt hits this exact path with no query string at
        # all, so the un-offset request must mean "the tail", not "page 0 of
        # forward pagination" — the two used to be the same slice by
        # accident, which is what silently handed a new attendant the OLDEST
        # 200 messages of a long-running thread instead of its most recent
        # ones. An explicit ``offset`` (including ``0``) is unaffected: it is
        # what forward pagination and every existing test pass, and it keeps
        # meaning "records[offset:offset+limit]" exactly as before.
        resolved_offset = max(0, total - limit) if limit else 0
        page = records[resolved_offset:resolved_offset + limit] if limit else records[:]
        return {"total": total, "limit": limit, "offset": resolved_offset, "messages": page}
    offset = max(0, int(offset))
    page = records[offset:offset + limit]
    return {"total": total, "limit": limit, "offset": offset, "messages": page}


def list_messages(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    *,
    limit: int = 200,
    offset: int | None = None,
    thread_id: Any = None,
) -> dict:
    """Return a paginated, chronological page of the thread.

    ``total`` is the full thread length (not the page size) so a caller can
    drive pagination. A missing thread yields an empty page. ``thread_id``
    (T-0676) selects a bound topic's isolated thread instead of the bare
    per-user one — see :func:`conv_path`.

    ``offset`` omitted/``None`` (T-0850) returns the most recent ``limit``
    records (the tail) rather than the oldest — the right default for a
    reader with no prior page state, like a freshly-spawned attendant's boot
    read. Pass an explicit ``offset`` (``0`` included) for forward pagination
    from the start of the thread.
    """
    records = _read_all(data_dir, slug, global_user_id, thread_id)
    return _paginate(records, limit=limit, offset=offset)


def search(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    query: str,
    *,
    limit: int = 200,
    offset: int | None = None,
    thread_id: Any = None,
) -> dict:
    """Return a paginated page of records whose ``text`` contains ``query``
    (case-insensitive). ``total`` is the full match count.

    ``offset`` omitted/``None`` (T-0850) returns the most recent ``limit``
    matches rather than the oldest — see :func:`list_messages`."""
    needle = str(query or "").lower()
    records = [r for r in _read_all(data_dir, slug, global_user_id, thread_id)
               if needle in str(r.get("text", "")).lower()]
    return _paginate(records, limit=limit, offset=offset)
