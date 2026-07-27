"""WHO is speaking — the sender tag, applied at the outbound transport (T-0758).

The failure this fixes
----------------------
On 2026-07-27 the stakeholder wrote, in his own thread::

    [bot-squad user-conversation] подставляется, а [watchrobot user-conversation]
    — нет, пишет без пометки

    надо это сделать системной фичей, а не привычкой, и чтобы оно автоматически
    подставлялось в сообщение, как sender просто

Both attendants speak into the SAME Telegram supergroup, so an untagged reply
reads as "some other project answered". Measured in the live store before
building anything: of watchrobot's 213 session-authored records, **0** carry a
sender tag (7 open with the ``[FYI — ответ не требуется]`` class marker, which
is not one); of bot-squad's 33, **4** do — and all four were written on
2026-07-27, the same day he complained. That is the shape of the problem: the
"convention" was one session's habit, hours old, and it already looked enough
like a guarantee that its absence read as a different project answering.

Why the transport, and why the SENDER rather than the destination
-----------------------------------------------------------------
The tag has to be applied where every send passes, or it decays into exactly
the habit it replaces — the same argument T-0755 made for hooking
``TgClient.send`` instead of adding a sixth caller-side append, and the same
failure mode: the one path that DID tag was a caller-side convention.

But the transport must derive the tag from the SENDING SESSION, not from where
the message is going. Measured on the live install: ``config/projects.toml``
gives bot-squad and watchrobot the SAME ``tg_chat`` (``404580642``), so a
chat-id lookup cannot name the project at all, and a project-level guess would
have confidently mislabelled every DM. ``sessions.project_of_sid`` reads
``data/<slug>/sessions/<sid>.md`` off disk — the session itself says which
project it belongs to. ``tg_bindings`` (which DOES distinguish, per forum
topic) is used only as a fallback for the slug when a SID resolves to no
project, never as the reason to tag.

Which classes get a tag, and why (T-0758 DoD c)
-----------------------------------------------
The tag answers **"who is speaking"**. It is applied when, and only when, the
send NAMES a sender, and it is deliberately never a shape match on the
message's text — keying on, say, the ``📋`` glyph would fix precisely the one
class already known about, the anti-pattern T-0746 was rebuilt around.

* **Tagged** — a send that carries an identity. A session: ``sender_sid`` (the
  raw SID of whoever composed the text), a ``sid`` display label, or a
  ``route_sid``, resolved to ``[<slug> <role>]``. Or, for something the SYSTEM
  composed, a caller-declared ``slug``, rendered ``[<slug>]`` — no session
  wrote it, so claiming a role would be inventing one.
* **Untagged** — a send that names nobody. ``tg_listener``'s interactive
  command replies, and any future path that states no identity.
* **Left as it labels itself** — nothing, once a project is known. A class
  name is combined with the project rather than replaced by it
  (``[<slug> autopilot]``), because a class alone answers *what* and not
  *which project*.

The exclusion list this module shipped with was narrower, and the correction
is worth keeping because the reasoning generalises. ``📋 <ticket> → <status>``
was originally left bare as "already identified — it names a ticket". Operator
p298 then measured it: **189 of watchrobot's 192 ticket ids also exist in
bot-squad (98%)**, so a ticket id identifies almost nothing about which
project is talking. The premise was false, not the rule.

What survives the correction is a real line, and it is about the READER, not
the message class:

    An UNPROMPTED message must name its project. A direct answer to something
    he just did need not.

``[voice_intake] ✅ got your voice note`` lands seconds after he sent that
note, in the thread he sent it to; the surrounding turn disambiguates it and a
project tag is noise. A lifecycle notice, an autopilot alert and a telemetry
alert arrive out of nowhere into a supergroup that serves both projects, with
nothing around them to say whose they are. Those now carry the project;
command replies and the voice echo stay bare.

One case names nobody ON PURPOSE: ``jobs.oauth_refresh``'s failure page picks
whichever project sorts first purely to get a chat to page into, but an OAuth
failure breaks every session on the install. Tagging it with that project
would name the wrong owner, which is worse than naming none — the same call
T-0724 made for ``autoupdate_apply``.

No double-tagging (T-0758 DoD b)
---------------------------------
A body that already OPENS with a hand-typed identity tag has it **replaced**,
not duplicated — so the manual habit can simply stop, with no flag day, and a
session that keeps typing it sees no change. Replacement rather than
"leave it if present" is deliberate: it also corrects a WRONG tag, which is
the one case that actively lies to the reader (a watchrobot session that
copies bot-squad's habit verbatim would otherwise keep announcing itself as
bot-squad, and today nothing catches that).

A leading bracket that is NOT identity-shaped (``[FYI — ответ не требуется]``,
``[synthetic E2E test …]``) is a marker naming what the message IS; it is left
alone and nothing is prepended, per the class rule above.

Byte ordering vs. the echo guard (T-0758 DoD e)
------------------------------------------------
``TgClient.send`` composes the tagged text ONCE and hands the same string to
``_post`` and to ``outbound_log.record``. That ordering is load-bearing:
``echo_guard.recent_send_match`` asks "did we send these exact bytes to this
chat", so a spool holding the UNtagged body while the wire carries the tagged
one would blind the guard against every forwarded reply. Pinned by
``test_sender_tag.py::test_spool_records_the_tagged_bytes_the_wire_carried``.

Never raises
------------
A tag is a courtesy; a delivery is not. Every entry point falls back to the
pre-T-0758 :func:`tg._prefix` rendering on any failure, so a missing session
md, an unreadable bindings file, or a malformed SID costs the tag and never
the message.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

#: A leading ``[...]`` marker on a message body. Bounded (no nested brackets,
#: no newline, 120 chars) so a body that merely CONTAINS brackets — a code
#: snippet, a link further in — can never be mistaken for a tag. The ``(?!\()``
#: is the one carve-out that matters in practice: ``[T-0758](https://…) готово``
#: opens with a bracket but is a MARKDOWN LINK, not a marker, and treating it
#: as one would silently cost that message its tag.
_LEADING_TAG_RE = re.compile(r"^\[([^\[\]\n]{1,120})\](?!\()\s*")

#: A routing SID, ``S-<user>-<window>-p<pane>``. Kept as its own predicate
#: rather than importing ``sessions._SID_SHAPE_RE`` so this module stays
#: importable from the transport without dragging the session machinery in.
#: ``\S+`` where the original writes ``.+`` — deliberately stricter, because a
#: DISPLAY label ("bot-squad user-conversation") must not be mistaken for a
#: routing key, and only the space rules it out. Pinned to agree with the
#: original on real inputs by ``test_sid_predicate_matches_sessions``.
_SID_RE = re.compile(r"^S-\S+-p\d+$")

#: The T-0724 nested form some callers still produce: ``[<slug>] <name>``.
_NESTED_RE = re.compile(r"^\[([^\[\]\s]+)\]\s+(.+)$")

#: ``tg.part_marker``'s ``(n/N)`` on a split page's part. It sits BEFORE the
#: body, so a hand-typed tag on part 1 of a long reply hides behind it — and
#: without this the tag would be prepended in front of the marker and the user
#: would get both, which is precisely the double-tagging this module forbids.
#: Matched and re-emitted verbatim so an unchanged send stays byte-identical.
_PART_MARKER_RE = re.compile(r"^\(\d+/\d+\)\s+")


def is_sid(value: Any) -> bool:
    """True when ``value`` has the shape of a routing SID."""
    return bool(_SID_RE.match(str(value or "").strip()))


def _slug_for_destination(cfg: Any, chat_id: Any, topic_id: Any) -> str:
    """The project a (chat, forum topic) is BOUND to, or ``""``.

    Fallback only — see the module docstring. It reads ``tg_bindings``, which
    is per-topic and therefore actually discriminating, and deliberately NOT
    ``projects.toml``'s ``tg_chat``: on this install both projects share one
    chat id, so a chat-level lookup would answer confidently and wrongly.
    """
    if not str(chat_id or ""):
        return ""
    try:
        from bot_squad_worker import tg_bindings

        binding = tg_bindings.resolve(cfg, chat_id, topic_id)
    except Exception:  # noqa: BLE001 — a store hiccup costs the slug, not the send
        log.exception("sender_tag: binding lookup failed for chat %s", chat_id)
        return ""
    return str((binding or {}).get("slug") or "")


def label_for_sid(cfg: Any, sid: str, *, chat_id: Any = "", topic_id: Any = None) -> str:
    """The canonical ``"<slug> <role>"`` label for a routing SID.

    Both halves degrade independently and neither guesses: with no project
    resolvable the bare SID comes back (the pre-T-0758 rendering), and with no
    role parseable the slug alone does. A T-0662 stakeholder-assigned alias
    beats the derived role, matching
    ``sessions.sid_display_label(compact=True)`` — the label he already sees
    everywhere else.
    """
    sid = str(sid or "").strip()
    if not sid:
        return ""
    try:
        from bot_squad_worker import sessions as _sessions

        slug = _sessions.project_of_sid(cfg, sid) or _slug_for_destination(
            cfg, chat_id, topic_id
        )
        role = ""
        data_dir = getattr(cfg, "data_dir", None)
        if data_dir is not None:
            role = _sessions._alias_for_sid(data_dir, sid) or ""
        role = role or _sessions._role_segment_of_sid(sid) or ""
    except Exception:  # noqa: BLE001 — never fail a send over a label
        log.exception("sender_tag: could not build a label for %s", sid)
        return sid
    if slug and role:
        return f"{slug} {role}"
    return slug or sid


def resolve_label(
    cfg: Any,
    *,
    sid: str = "",
    sender_sid: str = "",
    route_sid: str = "",
    chat_id: Any = "",
    topic_id: Any = None,
) -> str:
    """The sender label for one send, or ``""`` when the send names no sender.

    Rungs, in strict order — each consulted only when no earlier one resolved:

    1. ``sender_sid`` — the raw SID of the session that COMPOSED the text.
       Label-only by contract: unlike ``route_sid`` it has no reply-routing or
       destination effect, which is what lets a caller state authorship
       without also changing where a reply lands.
    2. ``sid`` — the caller's DISPLAY label. Already canonical for every path
       that goes through ``_send_stakeholder_dm`` with a slug on hand, so it
       wins over rung 3 to keep those sends byte-identical. Two shapes are
       normalised rather than passed through: a raw SID (resolved like rung 1)
       and the nested ``"[<slug>] <name>"`` form, which ``tg._prefix`` used to
       render as the malformed ``[[bot-squad] deploy_monitor]``.
    3. ``route_sid`` — the reply-routing SID. Defensive: today every caller
       that sets it also sets ``sid``, so this is the net under a send path
       written later that sets only the routing key.

    ``""`` means "nothing named a sender" and is the whole of the DoD (c)
    class rule — see the module docstring.
    """
    sender_sid = str(sender_sid or "").strip()
    if is_sid(sender_sid):
        return label_for_sid(cfg, sender_sid, chat_id=chat_id, topic_id=topic_id)

    sid = str(sid or "").strip()
    if sid:
        if is_sid(sid):
            return label_for_sid(cfg, sid, chat_id=chat_id, topic_id=topic_id)
        nested = _NESTED_RE.match(sid)
        if nested:
            slug, name = nested.group(1), nested.group(2).strip()
            if is_sid(name):
                return label_for_sid(cfg, name, chat_id=chat_id, topic_id=topic_id)
            return f"{slug} {name}"
        return sid

    route_sid = str(route_sid or "").strip()
    if is_sid(route_sid):
        return label_for_sid(cfg, route_sid, chat_id=chat_id, topic_id=topic_id)
    return ""


def _known_slugs(cfg: Any) -> set[str]:
    try:
        return {str(s) for s in getattr(cfg, "projects", {}) or {}}
    except Exception:  # noqa: BLE001
        return set()


def is_identity_tag(cfg: Any, inner: str) -> bool:
    """True when a leading ``[...]``'s contents are a SENDER identity.

    Identity means: a routing SID, a registered project slug, or a slug
    followed by anything (``"watchrobot user-conversation"``, and the
    ``" @ <user>"`` form ``tg._prefix`` renders). Anything else — ``[FYI —
    ответ не требуется]``, ``[voice_intake]`` — is a marker naming what the
    message IS, not who sent it, and is left untouched.
    """
    inner = str(inner or "").strip()
    if not inner:
        return False
    if is_sid(inner):
        return True
    head = inner.split(None, 1)[0]
    return head in _known_slugs(cfg)


def compose(
    cfg: Any,
    text: str,
    *,
    sid: str = "",
    user: str = "",
    sender_sid: str = "",
    route_sid: str = "",
    chat_id: Any = "",
    topic_id: Any = None,
) -> str:
    """The exact bytes to put on the wire — ``text`` with its sender tag.

    Replaces the bare ``tg._prefix(text, sid=sid, user=user)`` at both
    transports. With no resolvable sender and no hand-typed tag it returns
    ``text`` unchanged, so every send that names nobody is byte-identical to
    its pre-T-0758 shape.
    """
    from bot_squad_worker.tg import _prefix

    try:
        label = resolve_label(
            cfg, sid=sid, sender_sid=sender_sid, route_sid=route_sid,
            chat_id=chat_id, topic_id=topic_id,
        )
        body = str(text or "")
        # A split page's `(n/N)` sits in front of everything; step over it so
        # the tag check sees the actual body, then put it back where it was.
        marker = ""
        part = _PART_MARKER_RE.match(body)
        if part is not None:
            marker, body = part.group(0), body[part.end():]
        existing = _LEADING_TAG_RE.match(body)
        if existing is not None:
            if not is_identity_tag(cfg, existing.group(1)):
                # A marker naming what this message IS. Already identified —
                # a second bracket in front of it is the noise DoD (c) names.
                return marker + body
            if label:
                # The hand-typed habit this ticket absorbs: drop it so the
                # system's own tag replaces it (and corrects it if it lied).
                body = body[existing.end():]
            else:
                # Nothing better to say than what the session already typed.
                return marker + body
        return _prefix(marker + body, sid=label, user=user)
    except Exception:  # noqa: BLE001 — a tag is never worth a dropped message
        log.exception("sender_tag: compose failed; sending untagged")
        return _prefix(str(text or ""), sid=str(sid or ""), user=str(user or ""))
