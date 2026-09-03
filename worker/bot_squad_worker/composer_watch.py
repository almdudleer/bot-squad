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
    r'|Ask anything|Try a command)$'
)


def composer_text(buf: str) -> str | None:
    """The live composer's content, or None when the pane shows no composer.

    The live composer is the LAST ``❯`` line; earlier runes are scrollback.
    Returns the text with exactly one separating space removed, so the value
    round-trips through :func:`input_mux` when a draft has to be restored —
    ``strip()`` would silently eat leading whitespace he typed.
    """
    if not buf:
        return None
    live: str | None = None
    for line in buf.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(PROMPT_RUNE):
            live = stripped[len(PROMPT_RUNE):]
    if live is None:
        return None
    if live.startswith(" "):
        live = live[1:]
    live = live.rstrip()
    if _PLACEHOLDER_RE.match(live.strip()):
        return ""          # the box is EMPTY; that is Claude Code's own hint
    return live


def looks_like_dialog(buf: str, live: str | None = None) -> bool:
    """True when the ``❯`` belongs to a permission/choice prompt, not a composer."""
    if not buf:
        return False
    if live is None:
        live = composer_text(buf)
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
