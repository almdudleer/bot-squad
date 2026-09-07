"""What is in a session's composer, and for how long (T-0954).

The lifecycle used to ask one question of a pane — "is there text in the
composer?" — and treat any answer but "no" as "the human is typing, defer".
Measured 2026-09-03 on the live install, that gate deferred
``routine-handler-p528`` 1554 times in 26 hours and the watchrobot operator
continuously from 08-31 20:02 to 09-03 09:22: text nobody was editing sat in
those composers for days, so the compact never ran and every one of those
sessions went cache-cold. The stakeholder's ruling that day:

    «и он должен ждать, если я что-то пишу в окне. Если там просто застрял
    текст, который уже 10 минут там лежит один и тот же, он должен подписывать,
    что кажется, это stale текст в окне. Но если это что-то, что я прямо сейчас
    или в последние 10 минут менял, то он должен просто ждать в очереди, пока я
    закончу и отправлю»

So the question this module answers is not "is it empty" but **"has it moved"**.
The composer's CONTENT is remembered between ticks; a session whose composer
still holds the same bytes it held ten minutes ago is `stale`, and the
lifecycle proceeds over it (saying so). A composer that changed inside the
window is `typing` and is waited on, however long that takes — his text is
never the thing that loses.

Three states, one clock:

``empty``       nothing in the composer — act freely.
``typing``      content changed within :data:`STALE_AFTER_SEC` — WAIT.
``stale``       same content for :data:`STALE_AFTER_SEC` — proceed, and label it.
``dialog``      a permission/choice prompt owns the ❯ — WAIT, but never call it
                typing (nobody is composing, and clearing it would answer it).
``generating``  the pane is mid-turn — WAIT; its own requests refresh the cache.

``dialog`` earns its own state because the old gate could not see it. Probed on
Claude Code v2.1.259, a permission prompt renders

    Do you want to proceed?
    ❯ 1. Yes
      2. Yes, and don't ask again for: mail
      3. No

— a ``❯`` with a non-empty line after it, i.e. `composer_ready` says "ready" and
`user_is_typing` says "he is typing", and the session defers forever with a log
line blaming a human who is not there. It is a WAIT (only a human answers a
permission prompt) but it must be a *named* one, because "waiting on a dialog
since 09:41" is actionable and "human-typed text is sitting in the composer" is
not.

The state file is per (project, session) under ``data/<slug>/_worker/composer/``
— not the session md, which is rewritten by every lifecycle phase and is read
by the SessionStart hook; a per-tick observation has no business there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The composer rune Claude Code renders in front of the input line. Same
#: constant as ``input_mux._PROMPT_RUNE``; duplicated rather than imported so
#: this module has no import edge into the delivery path (which imports the
#: gate that imports this).
PROMPT_RUNE = "❯"

#: His number, verbatim: «уже 10 минут там лежит один и тот же». Overridable so
#: an install can retune it, never derived from anything else.
STALE_AFTER_SEC = 600


def stale_after_sec() -> int:
    """The unchanged-content window, in seconds (10 minutes by default)."""
    raw = os.environ.get("BOT_SQUAD_COMPOSER_STALE_SEC")
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        log.warning("composer_watch: ignoring bad BOT_SQUAD_COMPOSER_STALE_SEC "
                    "%r — using %d", raw, STALE_AFTER_SEC)
    return STALE_AFTER_SEC


STATE_EMPTY = "empty"
STATE_TYPING = "typing"
STATE_STALE = "stale"
STATE_DIALOG = "dialog"
STATE_GENERATING = "generating"

#: States in which the lifecycle may act on the pane.
ACTIONABLE = (STATE_EMPTY, STATE_STALE)

# A permission/choice prompt: the ❯ line is a numbered option ("1. Yes"), and
# the box above it asks. Both halves are required — a human CAN type "1. yes"
# into a composer, and a session's own output can contain the question — so the
# two together are the signal, and a false NEGATIVE here is safe (it degrades to
# `typing`, i.e. we wait, which is what a dialog wants anyway).
_DIALOG_OPTION_RE = re.compile(r"^\d+\.\s+\S")
_DIALOG_ASK_RE = re.compile(
    r"do you want to proceed|do you want to|apply this edit|"
    r"esc to cancel|Tab to amend",
    re.IGNORECASE,
)
#: Claude Code's mid-generation marker (same string ``autocompact`` keys on).
_GENERATING_MARK = "esc to interrupt"

# An EMPTY composer is not blank: Claude Code renders a dim placeholder in it.
# Measured live 2026-09-03 — this is what a pane shows immediately after C-u:
#
#     ❯ Try "fix lint errors"
#
# and a busy session with a queue shows `Press up to edit queued messages`.
# Reading either as "he typed something" cost real drafts: the delivery swap
# concluded its C-u had failed, skipped the restore, and left three unsent
# messages on disk instead of in their panes. Anchored whole-line so a human
# who genuinely types `Try "x"` is still respected — his line would have to be
# the placeholder byte for byte.
_PLACEHOLDER_RE = re.compile(
    r'^(?:Try\s+"[^"]*"|Press up to edit queued messages'
    r'|Ask anything|Try a command|<no suggestion>)$'
)

#: The character Claude Code puts between the ❯ rune and the composer
#: content. Measured on live pane %513 on 2026-09-04: it is U+00A0 NO-BREAK
#: SPACE, not an ASCII space.
_NBSP = "\u00a0"
_COMPOSER_SEPARATORS = (" ", _NBSP)

#: The two columns the composer inserts before his text. Row 1 spends them on
#: the rune + :data:`_NBSP`; every following row on a literal two-space inset.
#: They are the same two columns, which is why a wrapped row and a row he
#: started himself are indistinguishable by indent alone (T-0978).
_CONT_INDENT = "  "

#: Columns the composer box spends on chrome, MEASURED rather than assumed.
#: On a real Claude Code composer (v2.1.263) on 2026-09-07 a row the renderer
#: had filled to the brim held ``pane_width - 4`` characters at three different
#: pane widths — 96 at 100 columns, 133 at 137, 224 at 228. Two of the four are
#: the left inset above; the other two are a right margin a capture can never
#: show, because tmux strips trailing blanks off every row it hands back.
COMPOSER_MARGIN_COLS = 4

#: The rule that opens and closes the composer box: a run of U+2500 starting in
#: column 1. The opening rule may carry a title (``──── operator ────``); the
#: closing one is plain. His own rows can never be mistaken for it — row 1
#: starts with the rune and every later row with :data:`_CONT_INDENT`.
_BORDER_RE = re.compile("^\u2500{8,}")

#: Claude Code collapses a large paste to this, instead of rendering it. The
#: composer then holds a 18-character LABEL standing in for content the capture
#: never shows. Measured 2026-09-07: a 3689-char payload rendered as
#: ``❯ [Pasted text #1]`` with ``paste again to expand`` under the box. Reading
#: it as a draft and typing it back would replace whatever he pasted with the
#: literal string — so it is neither "empty" (that would let a delivery submit
#: his paste) nor a readable draft. It is the canonical UNREADABLE composer.
_PASTED_LABEL_RE = re.compile(r"^\[Pasted text #\d+\]$")

#: Never swap a draft taller than this many visual rows, whatever the pane
#: geometry says. The composer SCROLLS once it hits its height cap, and a
#: scrolled box drops rows off the TOP with no marker of any kind — the rune
#: simply sits on the first row that is still visible. Measured caps
#: (v2.1.263, 2026-09-07): 5 content rows in a 20-row pane, 10 in 30, 18 in 47.
#: :func:`composer_max_rows` models that cap and is the primary guard; this is
#: the belt for the case where that fit is wrong LOW on a tall pane, which is
#: the direction that would cost his text. Set above the tallest draft actually
#: observed — his live pane %640 held a 537-character draft over 6 rows on
#: 2026-09-07 — and well under the 18-row cap the production pane measures at,
#: so the cost of the bound is a legacy delivery on a draft taller than any he
#: has yet written.
MAX_SWAPPABLE_ROWS = 8


def composer_max_rows(pane_height: int) -> int:
    """How tall the composer box can grow before it starts SCROLLING.

    Fitted to the three measurements in :data:`MAX_SWAPPABLE_ROWS` and used
    only as a REFUSAL threshold, never as a promise: a block that reaches it
    may have older rows scrolled off the top, and is declared unreadable. If
    the fit is wrong high, we refuse a readable draft (one legacy delivery); it
    is paired with :data:`MAX_SWAPPABLE_ROWS` so that a fit that is wrong LOW
    on a tall pane — the direction that would cost his text — is still bounded.
    """
    try:
        return max(1, int(pane_height) // 2 - 5)
    except Exception:  # noqa: BLE001
        return 1


class ComposerBlock:
    """The WHOLE composer box, and whether the read can be trusted (T-0978).

    ``rows``      the box's content rows, inset stripped, top to bottom.
    ``text``      the draft, reconstructed. BEST-EFFORT unless ``complete``.
    ``complete``  the whole box was read AND every row break was resolved, so
                  ``text`` is what he actually typed, character for character.
    ``reason``    why not, when ``complete`` is False — for the log.

    ``complete`` is the only thing a caller that DELETES his text may branch
    on. ``text`` on its own is a reading, not a proof: the reason this class
    exists is that the previous reader returned the first visual row and the
    guard above it then measured that row instead of the draft.
    """

    __slots__ = ("rows", "text", "complete", "reason")

    def __init__(self, rows, text, complete, reason=""):
        self.rows = tuple(rows)
        self.text = text
        self.complete = bool(complete)
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return (f"ComposerBlock(rows={len(self.rows)}, chars={len(self.text)}, "
                f"complete={self.complete}, reason={self.reason!r})")


def _rune_row(buf: str) -> tuple[int, str] | None:
    """(index, content) of the LIVE composer row, or None when there is none.

    The live composer is the LAST ``❯`` line; earlier runes are scrollback.
    The content has exactly one separating space removed, so the value
    round-trips through :func:`input_mux` when a draft has to be restored —
    ``strip()`` would silently eat leading whitespace he typed.
    """
    found: tuple[int, str] | None = None
    for i, line in enumerate(buf.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith(PROMPT_RUNE):
            found = (i, stripped[len(PROMPT_RUNE):])
    if found is None:
        return None
    idx, live = found
    # T-0962: strip the ONE chrome separator, in either form. Matching only
    # the ASCII space meant the NBSP the live renderer actually emits was read
    # as the first character of his draft, typed back verbatim by the delivery
    # restore, and re-read one NBSP longer on the next tick. Drafts on disk
    # grew by exactly one NBSP per delivery (operator p513: 59 -> 87 bytes over
    # 21 deliveries, monotonic) until the padding pushed his real sentence
    # across the pane and the payload rode along unsent.
    if live[:1] in _COMPOSER_SEPARATORS:
        live = live[1:]
    # Any FURTHER leading NBSP is padding this loop already injected, so drain
    # it and let a poisoned composer heal in one delivery instead of twenty. A
    # keyboard does not produce U+00A0 (a pasted one is the only way, and the
    # cost there is invisible leading padding); his leading ASCII spaces are
    # his and still survive intact.
    live = live.lstrip(_NBSP)
    return idx, live.rstrip()


def composer_block(buf: str, width: int = 0,
                   height: int = 0) -> ComposerBlock | None:
    """Read the WHOLE composer box, not just the row carrying the rune.

    This is the fix T-0978 names. The old reader took the rune row and stopped,
    so a draft that wrapped onto following rows was captured as its first row —
    and the guard above it then measured that already-truncated capture against
    the pane width, which is a check the truncation itself makes pass. Measured
    live on his own pane %638 (2026-09-06): two saved drafts of exactly 220
    characters, both ending mid-word, on a 228-column pane.

    ``width``/``height`` are the pane's, from tmux, and both are OPTIONAL.
    ``width`` is cross-checked against the closing rule's own length — two
    independent instruments on the same geometry — and a disagreement means the
    frame was caught mid-resize, i.e. unreadable. Passing 0 asks the rule
    alone, which is enough to bound the box and classify the row breaks; what
    is then lost is only the cross-check. ``height`` has no substitute: without
    it a box sitting at its scroll cap cannot be recognised, so a caller that
    is about to DELETE his text must pass both.

    Returns None only when the pane shows no composer at all.
    """
    if not buf:
        return None
    head = _rune_row(buf)
    if head is None:
        return None
    idx, first = head
    lines = buf.splitlines()

    if _PASTED_LABEL_RE.match(first.strip()):
        # Real content the renderer refuses to show. NOT empty — calling it
        # empty tells a delivery there is nothing to protect, and its Enter
        # would then submit his paste.
        return ComposerBlock((first,), first, False,
                             "the composer holds a collapsed paste label")

    if _PLACEHOLDER_RE.match(first.strip()):
        return ComposerBlock((), "", True, "")   # the box is EMPTY

    # The closing rule bounds the box. Without it we cannot know where his text
    # ends, so the read degrades to the pre-T-0978 first-row value and says so.
    close = None
    for j in range(idx + 1, len(lines)):
        if _BORDER_RE.match(lines[j]):
            close = j
            break
    if close is None:
        return ComposerBlock((first,), first, False,
                             "no closing rule under the composer")

    rows = [first]
    for line in lines[idx + 1:close]:
        if not line.strip():
            rows.append("")                      # a blank row he typed
        elif line.startswith(_CONT_INDENT):
            rows.append(line[len(_CONT_INDENT):].rstrip())
        else:
            return ComposerBlock(tuple(rows), "\n".join(rows), False,
                                 "a row inside the box is not composer text")

    def _unreadable(reason: str) -> ComposerBlock:
        return ComposerBlock(tuple(rows), "\n".join(rows), False, reason)

    if len(rows) > MAX_SWAPPABLE_ROWS:
        return _unreadable(f"{len(rows)} rows is past the readable bound")
    if height and len(rows) >= composer_max_rows(height):
        return _unreadable("the box is at its height cap and may be scrolled")

    # Two independent instruments on the same geometry: what tmux says the pane
    # is, and how long the rule the renderer just drew actually is. They agree
    # on a settled frame; a disagreement means the frame was captured mid-resize
    # and nothing measured in columns can be trusted in it.
    border_width = len(lines[close])
    if width and width != border_width:
        return _unreadable(f"pane width {width} disagrees with the rule's "
                           f"{border_width} — a mid-resize frame")
    content_width = (width or border_width) - COMPOSER_MARGIN_COLS
    if content_width <= 0:
        return _unreadable("the pane is too narrow to have a composer")
    over = [len(r) for r in rows if len(r) > content_width]
    if over:
        # The renderer cannot put more than `content_width` characters on a
        # row, so a longer one means the model of the box is wrong here — and a
        # wrong model is exactly what read one row and called it the draft.
        return _unreadable(f"a row is {max(over)} chars in a {content_width}-"
                           f"char box — the geometry model is wrong here")

    if len(rows) == 1:
        # Provably the whole draft: the rune row is the only row in the box,
        # and it fits inside it.
        return ComposerBlock((first,), first, True, "")

    joined, reason = _join_rows(rows, content_width)
    if reason:
        return _unreadable(reason)
    return ComposerBlock(tuple(rows), joined, True, "")


def _join_rows(rows: list[str], content_width: int) -> tuple[str, str]:
    """Put the box's rows back together, or say why they cannot be.

    Claude Code wraps greedily on spaces, and the space it breaks on is
    CONSUMED — it appears on neither row. So a break is one of three things and
    the geometry says which:

    * the next row's first word could not have fitted    -> a wrap, join " "
    * it could have                                      -> he pressed a
                                                            newline, join "\n"
    * the row is filled to the brim (``content_width``)  -> UNRESOLVABLE: a
      word may have been cut in half (join "") or a space may have landed
      exactly on the boundary (join " "), and nothing in the capture separates
      those. Refuse rather than pick.

    Verified against real captures: a 577-character draft over 3 rows and a
    563-character one, both reproduced byte-for-byte by this rule.
    """
    out = [rows[0]]
    for prev, nxt in zip(rows, rows[1:]):
        if len(prev) > content_width:
            return "", (f"a row is {len(prev)} chars in a {content_width}-char "
                        f"box — the geometry model is wrong here")
        if len(prev) == content_width:
            return "", ("a row is filled to the brim — a wrap and a newline "
                        "are indistinguishable there")
        word = nxt.split(" ", 1)[0]
        if nxt and len(prev) + 1 + len(word) > content_width:
            out.append(" ")                      # the wrap ate exactly one
        else:
            out.append("\n")                     # it would have fitted
        out.append(nxt)
    return "".join(out), ""


def composer_text(buf: str) -> str | None:
    """The live composer's content, or None when the pane shows no composer.

    BEST EFFORT, and deliberately so: it returns everything the box shows when
    the box can be bounded, and falls back to the rune row alone when it cannot
    (no closing rule — the pre-T-0978 value, so every reader that only asks "is
    there anything in there" keeps reading exactly what it read before).

    It is the right surface for "has this composer changed" and "did it clear".
    It is the WRONG surface for anything that then DELETES his text: use
    :func:`composer_block` and branch on ``complete``. That distinction is the
    whole of T-0978 — the swap measured this value, and this value had already
    been cut down to one visual row by the reader that produced it.
    """
    block = composer_block(buf)
    if block is None:
        return None
    return block.text


def looks_generating(buf: str) -> bool:
    """Is this pane mid-turn — producing output, with no composer to read?

    :func:`observe` has always branched on this; it was private, so the two
    delivery-confirmation readers in ``input_mux`` could not ask. They called
    :func:`composer_text` instead, which returns ``None`` here, and read that
    absence as "the composer cleared" — i.e. as proof of submission. Exposing
    the predicate is what lets a confirmation say "I cannot see" instead of
    "it went".
    """
    return bool(buf) and _GENERATING_MARK in buf.lower()


def looks_like_dialog(buf: str, live: str | None = None) -> bool:
    """True when the ``❯`` belongs to a permission/choice prompt, not a composer.

    Asks about the RUNE ROW specifically (``1. Yes``), not the whole box: the
    other rows of a choice prompt are its other options, and folding them into
    one string would only make this harder to match. ``live`` is still honoured
    for callers that already hold the row.
    """
    if not buf:
        return False
    if live is None:
        head = _rune_row(buf)
        live = head[1] if head else None
    if not live or not _DIALOG_OPTION_RE.match(live.strip()):
        return False
    return bool(_DIALOG_ASK_RE.search(buf))


def state_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "_worker" / "composer"


def state_path(cfg: Any, slug: str, sid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(sid))
    return state_dir(cfg, slug) / f"{safe}.json"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/corrupt record is "never seen"
        return {}


def _save(path: Path, rec: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — an observation is never worth an error
        log.debug("composer_watch: could not persist %s", path, exc_info=True)


def forget(cfg: Any, slug: str, sid: str) -> None:
    """Drop a session's record (it submitted, or it is gone)."""
    try:
        state_path(cfg, slug, sid).unlink()
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001
        log.debug("composer_watch: could not forget %s", sid, exc_info=True)


def observe(cfg: Any, slug: str, sid: str, buf: str,
            now: float | None = None) -> dict:
    """Classify ``buf`` and remember its content. THE entry point.

    Returns ``{state, text, unchanged_for, since}`` where ``unchanged_for`` is
    seconds the current content has been sitting there (0.0 for an empty or
    freshly-changed composer) and ``since`` is the ISO stamp it first appeared.

    Called from the gates that are about to act, not from a background poller:
    the clock that matters is "how long has this text been in the way", so it
    starts the first tick something wanted to act and found the composer
    occupied. A session nothing is trying to do anything with is never sampled,
    and its first sample when the time comes reads `typing` — deliberately, so
    the ten minutes are ten minutes of BLOCKING, not ten minutes of existing.
    """
    now = time.time() if now is None else now
    path = state_path(cfg, slug, sid)

    if buf and _GENERATING_MARK in buf.lower():
        # Mid-turn: not a composer observation at all. Leave any remembered
        # draft alone — a turn does not mean he stopped composing the next one.
        return {"state": STATE_GENERATING, "text": None,
                "unchanged_for": 0.0, "since": None}

    live = composer_text(buf)
    if live is None:
        return {"state": STATE_GENERATING, "text": None,
                "unchanged_for": 0.0, "since": None}

    if not live.strip():
        forget(cfg, slug, sid)
        return {"state": STATE_EMPTY, "text": "", "unchanged_for": 0.0,
                "since": None}

    if looks_like_dialog(buf, live):
        # Remember it under its own kind so a dialog that has been up for an
        # hour is reportable, but never let it age into `stale`: proceeding
        # over a dialog would mean typing into a permission prompt.
        rec = _load(path)
        digest = _digest(live)
        if rec.get("hash") != digest or rec.get("kind") != STATE_DIALOG:
            rec = {"hash": digest, "kind": STATE_DIALOG, "first_seen": now,
                   "text": live}
        rec["last_seen"] = now
        _save(path, rec)
        return {"state": STATE_DIALOG, "text": live,
                "unchanged_for": max(0.0, now - float(rec.get("first_seen") or now)),
                "since": _iso(rec.get("first_seen"))}

    rec = _load(path)
    digest = _digest(live)
    first_sighting = rec.get("hash") != digest or rec.get("kind") != STATE_TYPING
    if first_sighting:
        # Changed (or first ever seen) — the clock restarts. This is the branch
        # that makes "he is editing" self-evident: every keystroke moves it.
        rec = {"hash": digest, "kind": STATE_TYPING, "first_seen": now,
               "text": live}
    rec["last_seen"] = now
    _save(path, rec)

    unchanged_for = max(0.0, now - float(rec.get("first_seen") or now))
    state = STATE_STALE if unchanged_for >= stale_after_sec() else STATE_TYPING
    return {"state": state, "text": live, "unchanged_for": unchanged_for,
            "since": _iso(rec.get("first_seen")),
            # A FIRST sighting cannot tell "he is typing right now" from "this
            # has been sitting here for two days" — the clock has to start
            # somewhere, and on a restart it starts for everyone at once.
            # Measured cost of ignoring that: four sessions were warned «ты
            # печатаешь» within seconds of a worker restart, at panes nobody had
            # touched in days — the exact «но не на stale сессию» he ruled out.
            # So callers that SPEAK (rather than merely wait) require a second
            # sighting, which only a pane someone is really working in produces.
            "first_sighting": first_sighting}


def _iso(epoch: Any) -> str | None:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch)))
    except Exception:  # noqa: BLE001
        return None


def describe(obs: dict) -> str:
    """One human line for a log or a warning — what is in the way, and since when."""
    state = obs.get("state")
    if state == STATE_EMPTY:
        return "composer is empty"
    if state == STATE_GENERATING:
        return "pane is mid-generation"
    mins = int((obs.get("unchanged_for") or 0) // 60)
    text = (obs.get("text") or "").strip()
    shown = text if len(text) <= 60 else text[:57] + "…"
    if state == STATE_DIALOG:
        return f"a permission/choice dialog has been open {mins} min ({shown!r})"
    if state == STATE_STALE:
        return (f"the same text has been sitting in the composer {mins} min — "
                f"this looks like STALE text, not live typing ({shown!r})")
    return f"he is typing — the composer changed less than {mins + 1} min ago"
