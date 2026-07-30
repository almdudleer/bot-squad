"""The chat id and topic id of a NEW-MESSAGE ping, rendered so they cannot be
missed (T-0795).

His words, 2026-07-30T07:54Z: «на пинг что новое сообщение надо подсвечивать
chat id и topic id чтобы не потерялись» — on a new-message ping, the chat id and
topic id need to be HIGHLIGHTED so they do not get lost. Said in the same breath
as the incident where a 20:09Z directive of his sat unread for seven hours
(T-0790/T-0791): the identifiers that let a human or a session tell WHERE a
message came from were present in the ping but buried, and buried is how they
get missed.

So this module exists to be the ONE rendering of that pair, imported by every
surface that pings about a new message. Five of them do today
(``tg_direct_reply.compose_envelope`` / ``compose_light_envelope`` /
``compose_reminder``, and in ``actions.py`` the attendant's pane nudge plus
``_thread_scoped_read_write_block``); the T-0770 family lesson is that this repo
fixes one branch and leaves the twin, and five copies of a format string is
exactly how the next reformat quietly drops the ids from four of them.

Why the convention is ONE LINE
------------------------------
A boxed multi-line block would be more prominent on four of the five surfaces.
It is unusable on the fifth: the attendant's nudge is delivered by
``input_mux.deliver_direct``, which sends **one Enter per line**, so every line
of a block becomes a separate composer submission. That transport is not a
detail to route around — splitting a human's multi-line message into N
submissions was itself the defect T-0773 fixed on the sibling paths, and
re-introducing it in the ping ABOUT the message would be the same bug wearing a
highlight. A single line is prominent on both transports: it stands alone in a
paste-delivered block, and it leads the sentence in a one-line nudge.

Prominence without colour
-------------------------
Every consumer is a terminal composer or a plain-text prompt: no markdown, no
ANSI, nothing that renders bold. What is left is position, case, and a marker no
other line in these envelopes uses. Each id carries its OWN ``▶`` rather than the
pair sharing one, so neither can be read as a trailing detail of the other — the
chat id being noticed and the topic id skimmed past is precisely the "не
потерялись" failure.

Absent stays absent (T-0761 / T-0782)
-------------------------------------
A plain DM has no topic id. This never emits an empty or placeholder ``TOPIC
ID`` field for that case — a field reading ``TOPIC ID: none`` is indistinguishable
from a real topic id that got dropped on the way, which is the house rule's whole
point. The absence is instead NAMED in prose on the chat segment, so a reader can
still tell "there is no topic here" apart from "the topic id went missing"
without either being a field.
"""
from __future__ import annotations

from typing import Any

#: The marker that flags an id nobody may miss. One per id — see the module
#: docstring on why the pair does not share a single mark.
MARK = "▶"

#: Between the two segments. Wide enough to read as two separate facts on one
#: line, and not punctuation — ``·`` is already the separator inside the
#: envelopes' own tag lists, and reusing it here would make the ids look like
#: another tag.
SEP = "   "

#: What a ping says when it has a chat but genuinely no topic. Prose, not a
#: field, and it does not claim "direct message": the no-topic case also covers
#: a plain (non-forum) group chat, and asserting DM there would be wrong.
NO_TOPIC_NOTE = "(no topic id — not a forum topic)"


def _clean(value: Any) -> str:
    """The id as it should be shown, or ``""`` when there isn't one.

    ``None`` and a whitespace-only value are both "absent" — the second because
    the ids reach these surfaces from JSON params and ledger entries where an
    empty string is the ordinary shape of a missing field.
    """
    if value is None:
        return ""
    return str(value).strip()


def render(*, chat_id: Any = None, thread_id: Any = None) -> str:
    """The highlighted id line for a new-message ping. ``""`` when neither id
    is known.

    Callers pass whichever ids their seam actually carries. Both is the normal
    case for a Telegram-origin ping; ``thread_id`` alone is the attendant seam
    (``ensure_user_conversation`` takes no chat — several of its callers, the
    web append and the ``uc_redrive`` re-drive among them, have no chat at all,
    so a chat field there could only be invented); neither happens on a
    topic-less attendant nudge, which then keeps its plain wording.
    """
    chat = _clean(chat_id)
    thread = _clean(thread_id)
    parts: list[str] = []
    if chat:
        parts.append(f"{MARK} CHAT ID: {chat}")
    if thread:
        parts.append(f"{MARK} TOPIC ID: {thread}")
    elif chat:
        # A chat with no thread is a real, correct state — say so, as prose.
        parts.append(NO_TOPIC_NOTE)
    return SEP.join(parts)
