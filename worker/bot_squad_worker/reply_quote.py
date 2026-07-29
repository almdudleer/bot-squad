"""What he was ANSWERING — the quoted original of a Telegram reply (T-0780).

The failure this fixes
----------------------
Replying to a specific message is how a human disambiguates. He answers «да»,
«нет», «второй вариант» against a message that carries the question — and until
this module neither the session nor the durable record ever saw that message.

``tg_listener.extract_reply_target`` returns ``(sid, text)`` where ``text`` is
the REPLY's own text. ``reply_to_message["text"]`` is read once, to regex a SID
out of it as the legacy routing fallback, and then discarded. Downstream there
was nowhere for it to go either: across 1298 live conversation-store records the
key union contains ``attachments, author, channel, direction, delivered, fyi,
forwarded_from, general_feed, text, thread_id, timestamp`` and nothing else. The
information was not "sometimes unpopulated" — it was dropped at ingestion into a
schema with no slot for it.

Two decisions, argued rather than picked
----------------------------------------
**TEXT, with the message id alongside as a locator — not the id alone.** An id
is cheap and non-duplicating, and it is also unresolvable here:

* the quoted message is frequently NOT ours. He can reply to his own message, to
  another human's in a group, or to a bot message sent before any of our maps
  existed;
* nothing in this system maps a TG ``message_id`` to a message BODY.
  ``tg_reply_map`` maps ``(chat_id, message_id) -> sid``, which answers "who
  sent it", never "what did it say" — and per T-0667/T-0725 that map is
  load-bearing routing state this ticket must not extend or pin;
* the consumer is a session reading a prompt. It cannot dereference an id at
  all, and a record whose quote evaporates when the source is pruned is the
  lossy store again with extra steps.

So ``text`` is the load-bearing part and ``message_id`` rides along as a cheap
correlation key for anything that later wants to join against the reply map or
the outbound spool.

**Two caps, because they bound two different things.** :data:`QUOTE_CAP` (600)
applies at RENDER time only: a 4000-character quoted message inside every
injected envelope is its own problem, and 600 characters is well past the point
where a reader knows which question is being answered. The durable record keeps
the quote uncapped here — its natural bound is Telegram's own 4096-character
message limit — and the store applies its own defensive cap at that value
(``conversation_store.append``), because a store must not trust a client. That
cap will essentially never fire for a TG-origin record; it exists for a
non-TG caller, and this docstring says so rather than letting a future reader
mistake it for routine trimming.

The T-0746 trap, which is the one that bites
---------------------------------------------
**The quoted text is SOMEONE ELSE'S WORDS — usually ours.** If it enters the
conversation store on a record authored ``"user"``, that is exactly the defect
T-0746 existed to fix: system prose recorded as the stakeholder saying it, in
the one file whose job is to hold what he actually said. So:

* the quote is a SEPARATE nested field (``reply_to``), never concatenated into
  ``text`` — structurally distinguishable, not distinguishable by eye;
* the nested field names the quoted message's OWN author, so a reader is never
  left inferring it;
* :func:`render_block` labels the block as somebody else's words and prefixes
  every line, so the boundary survives being read by a language model too.

What this module does NOT do
----------------------------
It does not touch routing. ``extract_reply_target``'s reply-map-first ordering
is load-bearing (T-0725) and the quoted-text SID regex stays the fallback it is;
nothing here is consulted to decide WHERE a message goes. Per T-0667 the quote
never becomes the conversation locus and never writes ``tg_reply_map``.

It also does not widen into FORWARDS (T-0746's surface). A forwarded message
carries its content in its own ``text`` field and its origin is already
annotated via ``forwarded_from`` — forwards lose attribution nuance at worst,
replies lost the content entirely.
"""
from __future__ import annotations

from typing import Any, Optional

#: How much of the quoted original an injected envelope carries. See the
#: module docstring for why this is a RENDER cap and not the record's.
QUOTE_CAP = 600

#: Marker prefixed to every line of a rendered quote. Deliberately NOT the
#: ``--- 8< ---`` fence the envelopes use for the human's own words: two blocks
#: in one message, one of them his and one of them not, must not look alike.
LINE_PREFIX = "    │ "


def _author_of(quoted: dict) -> tuple[str, str]:
    """``(descriptor, display_name)`` for whoever wrote the quoted message.

    The descriptor is always ``<kind>:<id>`` — ``bot:<id>``, ``user:<id>`` or
    ``chat:<id>``. This is deliberately NOT ``echo_guard``'s ``forwarded_from``
    vocabulary and must not be read as it: that field answers "did the sender
    compose this?", where a bare ``"bot"`` means *our* bot. Here the quoted
    message is usually ours, so the interesting question is WHICH bot or human
    wrote it, and every form stays id-qualified so a reader can check rather
    than assume.

    Returns ``("", "")`` when Telegram named nobody — an anonymous admin post or
    a payload shape we do not recognise. An explicit empty is the honest answer;
    guessing "the bot" here would re-run T-0746 in miniature.
    """
    sender_chat = quoted.get("sender_chat")
    if isinstance(sender_chat, dict) and sender_chat.get("id") is not None:
        name = str(sender_chat.get("title") or sender_chat.get("username") or "")
        return (f"chat:{sender_chat['id']}", name)
    frm = quoted.get("from")
    if isinstance(frm, dict) and frm.get("id") is not None:
        kind = "bot" if frm.get("is_bot") else "user"
        name = " ".join(
            str(x) for x in (frm.get("first_name"), frm.get("last_name")) if x
        ).strip() or str(frm.get("username") or "")
        return (f"{kind}:{frm['id']}", name)
    return ("", "")


def extract(message: dict) -> Optional[dict]:
    """The quoted original of ``message``, or ``None`` when there is none.

    Pure parse — no config, no store, no network — mirroring
    ``tg_listener.extract_reply_target``'s contract so it can be exercised
    anywhere that one can.

    Returns a dict with:

    ``text``         the quoted message's text (or a media caption). UNCAPPED
                     here; see the module docstring on the two caps.
    ``message_id``   Telegram's id for the quoted message, when it gave one.
    ``author``       ``bot:<id>`` / ``user:<id>`` / ``chat:<id>``, or ``""``.
    ``author_name``  display name, best effort; omitted when empty.
    ``fragment``     present ONLY when the sender highlighted part of the
                     quoted message rather than replying to the whole of it
                     (Bot API 7.0+ ``message["quote"]``). When Telegram tells us
                     exactly which span he was answering, that span IS the
                     answer to "what was he replying to", and dropping it would
                     be a smaller copy of the very defect this module fixes.

    ``None`` — meaning "nothing to carry", never "this was not a reply" — when
    the message is not a reply at all, or when the quoted message has no text
    and no caption. That second case is what keeps a service message (a forum
    topic's ``forum_topic_created``, which some clients attach as the
    ``reply_to_message`` of a topic's own messages) from being recorded as an
    empty quote on every message in the topic.
    """
    if not isinstance(message, dict):
        return None
    quoted = message.get("reply_to_message")
    if not isinstance(quoted, dict):
        return None

    text = str(quoted.get("text") or quoted.get("caption") or "").strip()
    fragment = ""
    q = message.get("quote")
    if isinstance(q, dict):
        fragment = str(q.get("text") or "").strip()
    if not text and not fragment:
        return None

    author, author_name = _author_of(quoted)
    out: dict[str, Any] = {"text": text}
    mid = quoted.get("message_id")
    if mid is not None:
        out["message_id"] = mid
    if author:
        out["author"] = author
    if author_name:
        out["author_name"] = author_name
    if fragment:
        out["fragment"] = fragment
    return out


def _cap(text: str) -> str:
    if len(text) <= QUOTE_CAP:
        return text
    # The count is stated rather than trailing a bare ellipsis: a session
    # deciding whether to go read the full thread needs to know how much it is
    # missing, and "…" alone reads the same for 3 dropped characters and 3000.
    return text[:QUOTE_CAP] + f"… [+{len(text) - QUOTE_CAP} chars]"


def render_block(quote: Optional[dict]) -> list[str]:
    """The quoted original as envelope lines — ``[]`` when there is no quote.

    Returned as a list so a caller splices it into its own line list; an empty
    list makes a quote-less message byte-identical to its pre-T-0780 envelope,
    which is what keeps this change invisible on every path that has no quote.

    The label does the T-0746 work: it says out loud that these are NOT the
    sender's words and names whose they are, and :data:`LINE_PREFIX` carries
    that boundary down every line so it cannot be lost to a reflow or to a
    reader skimming for the fenced block.
    """
    if not quote:
        return []
    text = _cap(str(quote.get("text") or ""))
    if not text:
        return []
    who = str(quote.get("author_name") or "").strip()
    desc = str(quote.get("author") or "").strip()
    if who and desc:
        attrib = f"written by {who} ({desc})"
    elif who or desc:
        attrib = f"written by {who or desc}"
    else:
        attrib = "author not stated by Telegram"
    mid = quote.get("message_id")
    where = f", message {mid}" if mid is not None else ""

    lines = [
        f"They were REPLYING TO this message — NOT their words, {attrib}{where}:",
        *[LINE_PREFIX + ln for ln in text.split("\n")],
    ]
    fragment = str(quote.get("fragment") or "").strip()
    if fragment:
        lines += [
            "",
            "…and they highlighted THIS part of it as the bit they are answering:",
            *[LINE_PREFIX + ln for ln in _cap(fragment).split("\n")],
        ]
    return lines
