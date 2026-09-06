"""T-0622: user-conversation intake no-drop — re-drive an attendant whose reply
turn died before it answered (e.g. killed mid-turn by a Claude 429/rate-limit
storm), so a stakeholder message never sits silently unanswered.

Incident (2026-07-05 21:13-22:03Z): the inbound TG message WAS stored and its
attendant WAS woken (``ensure_user_conversation`` nudged the live pane at
21:14:28Z) — but that reply turn died on the concurrent 429 storm, and nothing
re-drove it once the storm cleared. The attendant sat live, idle, at its
composer, with the thread's newest record still ``author: "user"`` —
unanswered for ~12h until a human noticed (T-0622).

This tick (wired the same way as ``drift_check``/``operator_redrive`` — no new
scheduler surface) sweeps every project's conversation threads
(``data/_mothership/conversations/<slug>/<gid>.jsonl``) for one that holds an
unanswered user message (:func:`_unanswered_user_record`). For each such thread
it re-drives via the EXACT mechanism a fresh inbound message already uses —
``ensure_user_conversation`` (its live-attendant branch is a best-effort pane
nudge) — subject to:

  * the stakeholder's ping cadence (:func:`ping_due_after_sec`) measured from
    the unanswered message itself — the first slot doubles as the grace period
    that leaves the normal reply flow its chance;
  * the SAME 429/5h-limit pressure signal the backoff governor consults
    (:func:`detector.session_pressure`) — never redrive INTO a live storm,
    only once THIS attendant's own pressure has cleared;
  * the live attendant being ``idle`` (T-0104 canonical activity enum,
    :func:`sessions._derive_activity`) — a genuinely in-flight reply is never
    interrupted;
  * per-``(slug, gid)`` ping state (persisted under
    ``_worker/uc_redrive/state.json``, mirroring ``operator_redrive``'s state
    file) — reset whenever the unanswered message changes.

Deliberately out of scope: a thread with NO live attendant at all (a fully
dead/never-spawned pane, not merely idle) is left to the existing "next
inbound message spawns/resumes" flow — the DoD's trigger condition is
specifically an IDLE attendant, mirroring the observed incident (the pane
survived the 429; only its reply turn died).

T-0794 — his cadence, his trigger, and delivery that lands
==========================================================
The stakeholder asked for exactly this mechanism on 2026-07-30, not knowing it
existed: «система ботсквод должна пинать агента раз в N минут (первый раз
через минут 5 мб, второй через 15, потом каждые 30 или вроде того), если у него
висят сообщения юзера, на которые он не ответил». Three things changed here so
that the module he described IS the module that runs.

**The cadence** replaced a flat 5-minute cooldown bounded at 3 attempts. Ping
slots are now measured from the unanswered message: ~5 min, ~15 min, then every
~30 min for as long as it hangs (:func:`ping_due_after_sec`). The bound is gone
because the bound was the bug in the small: three nudges inside 15 minutes and
then permanent silence is how a message that outlives one bad quarter-hour goes
unanswered forever. What still bounds it in practice is the gate above — a
thread with no live idle attendant is never pinged at all, which is why the one
thread hanging on the live install (watchrobot, since 2026-07-25) draws nothing.

**The trigger** was a proxy and is now his condition. The old test was "the
NEWEST record in the thread is ``author: "user"``", which any later append
falsifies — and ``system:task-lifecycle`` notices append into these threads
constantly. Measured on his own thread (415 records): of 157 unanswered
episodes, 29 had a system record land after the user's message, so ~18% of the
time the detector went blind precisely while a message hung. The scan now walks
back past those (they are neither an ask nor an answer) and stops at the first
``session:`` reply, so "he has not answered" is read off the thread rather than
inferred from the tail. Direct-mode messages stay invisible here, unchanged and
deliberately: they arrive as ``fyi`` records from ``system:direct-reply`` and
belong to ``tg_answer_owed`` (T-0770).

**Delivery** is the half that had actually failed. This module's escalation has
been firing for weeks into ``_chat/inbox-operator.log``, an ownerless file — 22
of its alerts sat undrained there from 2026-07-04 to 07-27 because ``operator``
was not a role keyword (T-0790, since fixed). It now resolves through
:func:`dispatch.live_operator_sids`, nudges the pane it resolved to so the
alert is read rather than merely filed, and treats an escalation that reached
NOBODY as not-yet-escalated: it retries on each following ping slot until a live
operator sid actually receives it, rather than spending its one alert on an
empty fan-out.

T-0917 — the probe was reading a log the attendant had stopped writing to
=========================================================================
For a week this module escalated the same message once every ~30 minutes and
was dismissed as a false alarm twice in one hour, by two different people, each
with their own wrong explanation. Neither the cadence nor the trigger was at
fault. Its INPUT was.

Measured on the live install 2026-08-19, watchrobot's thread with the
stakeholder: the root ``gu_dc82….jsonl`` held 261 ``user`` records current to
2026-08-18 and 223 ``session:`` records whose newest was 2026-08-12 — while
``gu_dc82…/t11.jsonl``, which this module never opened, held 1023 records with
both halves current. The forum-topic era (T-0660/T-0676) moved the conversation
into per-topic logs; T-0606 (landed 2026-08-12 10:22Z) made the append honour
the ``thread_id`` the attendant supplies, which closed the last path that still
wrote a reply into the root log. The asks kept arriving there anyway, because
``tg_listener._fallback_undelivered`` files an undeliverable message THREADLESS
by design. So the root log became a place asks go in and answers never appear,
and "newest user record with no ``session:`` after it" could only ever answer
"hanging". The message it burned itself on had been answered 67 seconds later,
in ``t11``.

Three changes, and the third is the one that generalises:

* **The sweep** covers every log of a conversation, root and per-topic
  (:func:`_conversation_files`), each with its own ping campaign.
* **The answer** to a ROOT log's ask may live in any log of that gid, but only
  when THIS CONVERSATION'S ATTENDANT wrote it (:func:`is_attendant_answer`).
  The root log cannot record its own answer, so requiring one there is
  requiring the impossible; accepting any ``session:`` record instead was the
  first draft of this fix and it was wrong, caught in review. Measured on the
  live install: 27 distinct sids that are NOT the attendant post into
  watchrobot's ``t11`` alone — operator sessions, plus dev sessions like
  ``chart-round3`` and ``portfolio-signals`` — and any one of them landing
  after his message would have closed a genuinely unanswered root ask. That is
  the same false negative the per-topic asymmetry exists to prevent,
  reintroduced through the other door. Per-topic logs still carry both halves
  and are judged alone.

  Two consequences, both deliberate. An OPERATOR reply in a topic no longer
  ends a ROOT ask, so the module can over-report there — the safe direction for
  an alarm whose failure mode is silence, and the operator is not the session
  this module re-drives. And "the attendant" is decided by
  :func:`sessions.user_conversation_window` / ``_window_from_sid``, the exact
  pair ``live_user_conversation_sid`` uses, so this predicate and the gate that
  picks a session to nudge cannot drift apart.
* **A predicate that cannot say "answered" now says PROBE-BROKEN**
  (:func:`probe_broken`), not "hanging". The cheap signal is the ticket's own:
  the age of the newest ``session:`` record while asks keep landing. This is
  the guard that would have caught the defect above on 2026-08-13 instead of
  2026-08-19, and it is deliberately a different verdict with a different
  message, because the previous verdict sent two people to wake an attendant
  that had already replied.

A fourth change exists only to keep the first from doing harm: a hanging
message this module has never tracked and which is already older than
:func:`max_first_ping_age_sec` opens no campaign at all
(:data:`DEFAULT_MAX_FIRST_PING_AGE_SEC`). Widening the sweep uncovered eight
dormant hangs on the live install, the oldest from 2026-07-31; pinging and
escalating each of them would have been this ticket's own failure mode,
delivered by its fix.

Kill switch: ``BOT_SQUAD_UC_REDRIVE=0``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def enabled() -> bool:
    return os.environ.get("BOT_SQUAD_UC_REDRIVE", "1").strip() != "0"


#: His cadence, verbatim: «первый раз через минут 5 мб, второй через 15, потом
#: каждые 30 или вроде того» — first ping at ~5 min, second at ~15, then every
#: ~30. Read as offsets FROM THE UNANSWERED MESSAGE (that is how he counts:
#: «раз в N минут … если у него висят сообщения»), not from the previous ping,
#: so a thread that spent its first half-hour with a busy attendant is on the
#: steady cadence the moment the attendant frees up rather than restarting the
#: ramp. The tuning was explicitly delegated («подумай, как лучше»); these are
#: his numbers unchanged, because 5/15/30 already spans "it might just be slow"
#: → "something is wrong" → "keep it visible" and nothing measured argues with
#: them.
DEFAULT_FIRST_PING_SEC = 300
DEFAULT_SECOND_PING_SEC = 900
DEFAULT_STEADY_PING_SEC = 1800

#: The escalating phase is over once this many pings have failed to produce a
#: reply — that is when a human is told. It is the ramp length (2), so the
#: escalation rides the entry into the steady state instead of being a third
#: independent number to keep in sync.
ESCALATE_AFTER_PINGS = 2

#: T-0917 item 2 — the PROBE-BROKEN threshold. A conversation log whose newest
#: ``session:`` record is older than this WHILE asks keep landing in it has an
#: input this module can no longer read an answer from, and "he has not been
#: answered" is then a statement about the LOG, not about him. One day: far past
#: any plausible reply latency, far short of the three weeks the live instance
#: ran undetected.
DEFAULT_PROBE_STALE_SEC = 86400

#: A hanging message this module has NEVER pinged and which is ALREADY older
#: than this is history, not a reply turn that died: the docstring's whole scope
#: is an attendant that went idle mid-answer, and a day-old ask needs a human
#: decision rather than a nudge carrying context nobody holds any more. Without
#: this floor the moment T-0917 widened the sweep to per-topic logs, eight
#: dormant hangs (the oldest from 2026-07-31) would each have opened a fresh
#: ping campaign and escalated — the alarm burn this ticket exists to stop,
#: caused by its own fix.
DEFAULT_MAX_FIRST_PING_AGE_SEC = 86400


def probe_stale_sec() -> int:
    return _env_sec("BOT_SQUAD_UC_REDRIVE_PROBE_STALE_SEC", DEFAULT_PROBE_STALE_SEC)


def max_first_ping_age_sec() -> int:
    return _env_sec("BOT_SQUAD_UC_REDRIVE_MAX_FIRST_PING_AGE_SEC",
                    DEFAULT_MAX_FIRST_PING_AGE_SEC)


def _env_sec(name: str, default: int) -> int:
    """A positive-seconds env override, or ``default``. Zero and negatives are
    rejected rather than honoured — a 0 here would turn the cadence into an
    every-tick nudge loop at the attendant."""
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def first_ping_sec() -> int:
    return _env_sec("BOT_SQUAD_UC_REDRIVE_FIRST_SEC", DEFAULT_FIRST_PING_SEC)


def second_ping_sec() -> int:
    # Never earlier than the first slot: the schedule has to stay monotonic for
    # :func:`pings_due_by` to be its inverse, and an env pair that inverted the
    # two would otherwise skip straight into the steady state.
    return max(_env_sec("BOT_SQUAD_UC_REDRIVE_SECOND_SEC", DEFAULT_SECOND_PING_SEC),
               first_ping_sec())


def steady_ping_sec() -> int:
    return _env_sec("BOT_SQUAD_UC_REDRIVE_STEADY_SEC", DEFAULT_STEADY_PING_SEC)


def ping_due_after_sec(pings_sent: int) -> int:
    """Seconds after the unanswered user message at which ping number
    ``pings_sent + 1`` falls due.

    ``0 -> 300`` (5 min), ``1 -> 900`` (15 min), then +1800 per ping: 45 min,
    75 min, 105 min… The gap STOPS widening at the steady interval — this is
    not an exponential backoff that quietly becomes a once-a-day check, which
    is the failure mode "then every 30 or so" rules out.
    """
    if pings_sent <= 0:
        return first_ping_sec()
    if pings_sent == 1:
        return second_ping_sec()
    return second_ping_sec() + (pings_sent - 1) * steady_ping_sec()


def pings_due_by(hanging_sec: float) -> int:
    """How many ping slots have come due by ``hanging_sec`` after the message —
    the inverse of :func:`ping_due_after_sec`.

    Slots are CONSUMED by time, not queued: an attendant that was busy or under
    429 pressure through its first hour gets ONE ping when it frees up and then
    rejoins the every-~30 cadence, rather than absorbing the four it missed in
    a burst. The cadence is a schedule for a hanging message, not a debt owed
    to it.
    """
    first, second, steady = first_ping_sec(), second_ping_sec(), steady_ping_sec()
    if hanging_sec < first:
        return 0
    if hanging_sec < second:
        return 1
    return 2 + int((hanging_sec - second) // steady)


def _conversations_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / "_mothership" / "conversations" / slug


def _conversation_files(conv_dir: Path, *, now: float, max_age_sec: float,
                        tracked: Any = ()) -> list[dict]:
    """Every conversation log under ``conv_dir``, as
    ``{"key", "gid", "thread", "path"}`` — the ROOT ``<gid>.jsonl`` files AND
    the per-topic ``<gid>/t<N>.jsonl`` files beside them.

    T-0917: the per-topic files were invisible here, and since the forum-topic
    era (T-0660/T-0676, late July 2026) they are where the conversation
    actually happens. Measured on the live install on 2026-08-19: watchrobot's
    root ``gu_dc82….jsonl`` held its last ``session:`` record on 2026-08-12
    while ``gu_dc82…/t11.jsonl`` held 1023 records with both halves current to
    2026-08-18. Globbing only the root file meant the probe read a log the
    attendant no longer writes to.

    ``key`` is the STATE key: the bare gid for a root file, ``<gid>/t<N>`` for a
    topic. It must stay distinct per log — two topics of one gid hang
    independently and cannot share one ping campaign.

    Files untouched for longer than ``max_age_sec`` are skipped: a log's mtime
    is never older than its newest record, so a message FIRST SEEN in one is
    past :func:`max_first_ping_age_sec` and would be aged out below anyway.
    That is what keeps this sweep from re-reading every archived topic (~2 MB
    on the live install) once a minute to reach the same conclusion.

    ``tracked`` is the exemption, and it is not an optimisation detail: a
    campaign ALREADY RUNNING must keep pinging for as long as the message hangs
    (T-0794 removed the three-attempt bound precisely because a bound was the
    bug), and a hanging message in a log nothing else appends to is exactly the
    case whose mtime stops advancing. Skipping those would reinstate the bound
    by the back door, silently, after a day.
    """
    keep = set(tracked or ())
    out: list[dict] = []
    for pattern in ("*.jsonl", "*/*.jsonl"):
        for p in sorted(conv_dir.glob(pattern)):
            if p.parent == conv_dir:
                conv = {"key": p.stem, "gid": p.stem, "thread": "", "path": p}
            else:
                gid = p.parent.name
                conv = {"key": f"{gid}/{p.stem}", "gid": gid,
                        "thread": p.stem, "path": p}
            if conv["key"] not in keep:
                try:
                    if now - p.stat().st_mtime > max_age_sec:
                        continue
                except OSError:
                    continue
            out.append(conv)
    return out


def _read_records(path: Path) -> list[dict]:
    """Every well-formed record in one conversation log, in file order.

    Tolerates a torn/garbage line (mirrors ``conversation_store``'s own read
    tolerance) by skipping it — a half-written tail must not hide the records
    behind it.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _newest_session_ts(records: list[dict]) -> str:
    """The newest ``session:``-authored timestamp in ``records``, or ``""``."""
    best = ""
    for rec in records:
        if str(rec.get("author") or "").startswith("session:"):
            ts = str(rec.get("timestamp") or "")
            if ts > best:
                best = ts
    return best


def attendant_window(gid: str) -> str:
    """The tmux window every user-conversation attendant for ``gid`` carries,
    or ``""`` when ``gid`` is not a name an attendant could ever have.

    Derived from :func:`sessions.user_conversation_window` — the SAME producer
    that names the window when an attendant is spawned, and the same value
    :func:`sessions.live_user_conversation_sid` matches against to decide which
    live session is THE attendant for this conversation. Reading it from the
    producer rather than matching a hand-typed ``…-user-conversation-p…``
    pattern is what stops this test and that gate drifting apart: rename the
    scheme and both move together, or neither does.

    ``gid`` here comes from a FILENAME on disk, so it is not guaranteed to be a
    legal gid at all; the producer rejects those, and a rejection means "no
    attendant lineage exists for this name" rather than an error to raise
    inside a sweep.
    """
    from bot_squad_worker import sessions as S
    try:
        return S.user_conversation_window(gid)
    except Exception:  # noqa: BLE001 — an illegal gid has no lineage, full stop
        return ""


def attendant_windows(cfg: Any, slug: str, gid: str) -> set[str]:
    """EVERY tmux window this gid's attendant lineage has ever carried.

    T-0964 broke the one-window assumption on purpose: an attendant is no longer
    named ``<gid>-user-conversation`` but ``universal_bsq_session`` /
    ``user_session_<who>``, with the gid moved to the ``global_user_id`` md
    field. A single window string can therefore no longer decide "is this reply
    from the attendant" — and getting that wrong is not a cosmetic miss: this
    module would read every answer a post-rename attendant gave as silence and
    re-drive a conversation that was answered.

    So the lineage is the union of two readings, both of which must keep
    working while sessions from both eras are on disk:

      * the legacy window (:func:`attendant_window`), for SIDs minted before
        the rename — their md may be long gone, but their SID still spells it;
      * the ``window`` of every session md on this board whose
        ``global_user_id`` is this gid, live or suspended.

    Returns an empty set for a gid no attendant could ever have had.
    """
    from bot_squad_worker import sessions as S
    wins: set[str] = set()
    legacy = attendant_window(gid)
    if legacy:
        wins.add(legacy)
    try:
        sess_dir = Path(cfg.data_dir) / slug / "sessions"
        mds = sorted(sess_dir.glob("*.md")) if sess_dir.exists() else []
    except (AttributeError, OSError, TypeError):
        return wins
    for md in mds:
        meta = S._read_session_metadata(md)
        if not meta or S.session_global_user_id(meta) != str(gid or "").strip():
            continue
        win = str(meta.get("window") or "").strip()
        if not win:
            win = S._window_from_sid(str(meta.get("sid") or md.stem))
        if win:
            wins.add(win)
    return wins


def is_attendant_answer(author: str, attendant_win: str | set[str]) -> bool:
    """Whether ``author`` is a reply from THIS conversation's attendant lineage.

    Uses :func:`sessions._window_from_sid`, the derivation
    ``live_user_conversation_sid`` itself applies to a session md's immutable
    SID. So "the attendant" means the same thing here as it does at the gate
    that re-drives one — an alignment a regex over the author string could not
    promise.
    """
    if not attendant_win or not str(author or "").startswith("session:"):
        return False
    from bot_squad_worker import sessions as S
    wins = ({attendant_win} if isinstance(attendant_win, str)
            else set(attendant_win))
    return S._window_from_sid(str(author)[len("session:"):]) in wins


def _newest_attendant_ts(records: list[dict],
                         attendant_win: str | set[str]) -> str:
    """Newest timestamp among ``records`` authored by this gid's ATTENDANT
    lineage, or ``""``. Deliberately narrower than :func:`_newest_session_ts`:
    that one asks "does this log record answers AT ALL" (the PROBE-BROKEN
    input), this one asks "did the session we would re-drive actually reply".
    """
    best = ""
    for rec in records:
        if is_attendant_answer(rec.get("author") or "", attendant_win):
            ts = str(rec.get("timestamp") or "")
            if ts > best:
                best = ts
    return best


def probe_broken(records: list[dict], *, now: float) -> dict | None:
    """Whether this log can still tell us "answered" — T-0917 item 2.

    Returns a diagnosis dict when the log's ANSWER half has gone dead, ``None``
    when the log is readable. This is a DIFFERENT verdict from "he has not been
    answered", and keeping them apart is the whole point: on 2026-08-18 the same
    alert was raised, dismissed as a false alarm, and re-raised — twice, by two
    people, each with their own wrong explanation — because a probe whose input
    had died could only ever say "hanging". The message it fired on carried the
    stakeholder's go-ahead on the largest open move in the project, and both
    dismissals were wrong.

    The signal is the cheap one the ticket names — the age of the newest
    ``session:`` record while asks keep landing — with two guards that stop it
    firing on a healthy log:

    * a log that has NEVER carried a ``session:`` record is not broken. It is a
      one-sided feed (a per-task topic is a monologue BY DESIGN) or a brand-new
      conversation, and there is no "stopped" to detect without a "started".
    * the asks after the last answer must SPAN more than the staleness window
      themselves. A thread that was quiet for a week and got one message four
      minutes ago is genuinely unanswered, not unreadable — and calling that
      "instrument dead" would trade the false "hanging" for the same error
      inverted, which is how a real hang stops being pinged. A span of more
      than :func:`probe_stale_sec` means the log kept taking traffic for over a
      day (necessarily two asks or more) and recorded not one reply to any of
      it. This is the ONE clause that bounds the verdict; a separate
      minimum-ask-count constant used to sit beside it and a mutation pass
      showed it could never fire on its own.
    """
    newest = _newest_session_ts(records)
    at = _parse_iso(newest)
    if at is None:
        # No answer was EVER recorded here (or the newest one is undatable). A
        # per-task topic is a monologue by design and a new conversation has no
        # history — neither has a "stopped" to detect without a "started".
        return None
    stale = probe_stale_sec()
    if now - at <= stale:
        return None
    asks = sorted(
        str(r.get("timestamp") or "") for r in records
        if str(r.get("author") or "") == "user"
        and str(r.get("timestamp") or "") > newest
    )
    first = _parse_iso(asks[0]) if asks else None
    last = _parse_iso(asks[-1]) if asks else None
    if first is None or last is None or (last - first) <= stale:
        return None
    return {
        "last_session_ts": newest,
        "session_age_sec": int(now - at),
        "asks_since": len(asks),
        "asks_span_sec": int(last - first),
    }


def _unanswered_user_record(path: Path) -> dict | None:
    """The newest ``author: "user"`` record that NO session reply follows, or
    ``None`` when the thread owes nothing.

    This is the trigger, and it is his words rather than a stand-in for them:
    «если у него висят сообщения юзера, на которые он не ответил». Scanning
    backwards, the first record decides in one of three ways:

    * ``session:…`` — the attendant answered after any user message further
      back. Nothing hangs. Stop.
    * ``user`` — an ask with no reply after it. That is the hanging message.
    * anything else — TRANSPARENT, keep walking back. ``system:task-lifecycle``
      notices, delivery receipts and the like append into these threads all the
      time; they are neither an ask nor an answer, and the previous version of
      this function (return the tail record, hanging iff it was ``user``) let
      every one of them mask a real unanswered message. Measured on the
      stakeholder's own thread: 29 of 157 unanswered episodes had a system
      record land after the user's message.

    Tolerates a torn/garbage line (mirrors ``conversation_store``'s own read
    tolerance) by skipping it and looking further back.
    """
    return _unanswered_in(_read_records(path))


def _unanswered_in(records: list[dict]) -> dict | None:
    """:func:`_unanswered_user_record`'s predicate over already-read records."""
    for rec in reversed(records):
        author = str(rec.get("author") or "")
        if author == "user":
            return rec
        if author.startswith("session:"):
            return None
    return None


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return time.mktime(time.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return None


def _state_path(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "_worker" / "uc_redrive" / "state.json"


def _load_state(cfg: Any, slug: str) -> dict:
    try:
        d = json.loads(_state_path(cfg, slug).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(cfg: Any, slug: str, state: dict) -> None:
    p = _state_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    from bot_squad_worker.mdlock import atomic_write
    atomic_write(p, json.dumps(state, indent=1))


def _notify_operator_stuck(
    cfg: Any, slug: str, key: str, *, pings: int, hanging_sec: float,
) -> list[str]:
    """Escalate one hanging message to a LIVE operator. Returns the sids it
    actually reached — ``[]`` when nobody did.

    ``key`` names the conversation LOG (``<gid>`` or ``<gid>/t<N>``), not just
    the gid: since T-0917 sweeps per-topic logs, an alert that named only the
    gid would send the reader to a file the message is not in.

    The return value is the point (T-0794/T-0790). ``to="operator"`` resolves
    through :func:`dispatch.live_operator_sids`, so with no operator on duty it
    resolves to nothing and this alert evaporates. The caller keeps the message
    marked un-escalated in that case and tries again on the next ping slot; an
    escalation nobody received must not be recorded as done, which is the exact
    shape of the failure that produced 22 undrained alerts.
    """
    mins = int(max(0.0, hanging_sec) // 60)
    try:
        from bot_squad_worker import intersession as _inter
        out = _inter.send(
            cfg, slug, to="operator",
            text=(f"⚠️ uc_redrive: {key} has an UNANSWERED user message — "
                  f"hanging {mins} min, {pings} re-wake ping(s) sent and the "
                  f"attendant still has not replied. Needs a human look."),
            from_sid="S-uc-redrive",
        )
    except Exception:  # noqa: BLE001 — best-effort notify must never break the tick
        log.exception("uc_redrive: stuck-notify failed for %s/%s", slug, key)
        return []
    delivered = [str(s) for s in (out or {}).get("delivered_to") or []]
    if not delivered:
        log.warning(
            "uc_redrive: escalation for %s/%s reached NO live operator — the "
            "message has hung %d min over %d ping(s); will retry the escalation "
            "on the next ping slot", slug, key, mins, pings,
        )
        return delivered

    # Primary cross-session signal — nudge each operator pane the way `bsq peer
    # send` does (precedent: ``tg_stall._redirect_to_upstream``). Landing in the
    # inbox file is delivery; the nudge is what makes it READ, and "written
    # somewhere correct that nobody opened" is the failure this ticket is about.
    # Best-effort: an operator with no live pane reads it on its next check.
    for sid in delivered:
        try:
            from bot_squad_worker.actions import _action_inject_input
            _action_inject_input({"sid": sid, "text": "check mail"})
        except Exception:  # noqa: BLE001
            log.debug("uc_redrive: pane nudge skipped for %s (no live pane?)", sid)
    return delivered


def _notify_probe_broken(cfg: Any, slug: str, key: str, diag: dict) -> list[str]:
    """Escalate a DEAD PROBE INPUT — not a hanging message. Returns the sids
    reached, ``[]`` when nobody did (same contract as
    :func:`_notify_operator_stuck`: an escalation nobody received is not
    recorded as done).

    The wording carries the distinction to the reader, because the reader is
    who acted on it wrongly last time. "Он не отвечен" sends someone to wake an
    attendant; "я не могу прочитать ответ" sends them to the write path.
    """
    hours = int(diag.get("session_age_sec", 0) // 3600)
    days = int(diag.get("asks_span_sec", 0) // 86400)
    try:
        from bot_squad_worker import intersession as _inter
        out = _inter.send(
            cfg, slug, to="operator",
            text=(f"🚨 uc_redrive PROBE-BROKEN: {key} — I cannot tell whether "
                  f"anyone has answered. The newest session: record in this "
                  f"conversation log is {hours}h old, yet "
                  f"{diag.get('asks_since')} user message(s) have landed in it "
                  f"since, spanning {days}+ day(s). The log's ANSWER half is "
                  f"dead, so 'unanswered' here is a statement about the LOG, "
                  f"not about him. NOT pinging the attendant — this needs the "
                  f"write path looked at, not a re-wake."),
            from_sid="S-uc-redrive",
        )
    except Exception:  # noqa: BLE001 — best-effort notify must never break the tick
        log.exception("uc_redrive: probe-broken notify failed for %s/%s", slug, key)
        return []
    delivered = [str(x) for x in (out or {}).get("delivered_to") or []]
    if not delivered:
        log.error(
            "uc_redrive: PROBE-BROKEN for %s/%s reached NO live operator "
            "(newest session: record %s, %d ask(s) since) — will retry",
            slug, key, diag.get("last_session_ts"), diag.get("asks_since"))
        return delivered
    for sid in delivered:
        try:
            from bot_squad_worker.actions import _action_inject_input
            _action_inject_input({"sid": sid, "text": "check mail"})
        except Exception:  # noqa: BLE001
            log.debug("uc_redrive: pane nudge skipped for %s (no live pane?)", sid)
    return delivered


def check_project(cfg: Any, slug: str, *, now: float | None = None) -> dict:
    """One unanswered-message sweep for a project. Returns a summary dict:
    ``{ok, redriven, probe_broken, aged_out}``.

    Idempotent + side-effecting: fires at most one nudge per conversation log
    per ping slot (:func:`ping_due_after_sec`), for as long as the message
    hangs.

    T-0917 changed WHAT IS SWEPT and WHAT COUNTS AS AN ANSWER:

    * every log of a ``(slug, gid)`` conversation is swept, root and per-topic
      alike (:func:`_conversation_files`), each with its own ping campaign;
    * a ROOT log's ask is answered by an ATTENDANT record
      (:func:`is_attendant_answer`) in any log of the same gid. This asymmetry
      is not a convenience — it is the defect. The
      undelivered-message fallback (``tg_listener._fallback_undelivered``)
      writes his ask into the root log DELIBERATELY THREADLESS, while the
      attendant's answer is filed in whatever topic it was actually sent to.
      Since T-0606 landed (2026-08-12) no reply reaches the root log at all, so
      that log structurally CANNOT record its own answer, and every ask filed
      there hangs forever. Measured on the live install: 23 pings and an
      operator escalation for a message answered 67 seconds after it arrived.
      A per-topic log carries both halves and is therefore judged on its own —
      widening the cross-log check to those would let an unrelated session
      posting into some other topic mask a real hang. The AUTHOR gate closes
      the same hole in the cross-log direction; see the module docstring for
      the 27 non-attendant sids measured in one topic.
    """
    # T-0929 — THE automation gate. Every mechanism that makes an agent work
    # reads the one pause SSOT here, so "stop the auto-drive" stops all of them
    # and not just the three that used to check.
    from bot_squad_worker import automation as _automation
    if not _automation.gate(cfg, slug, "uc_redrive"):
        return {"ok": True, "paused": True, "redriven": [], "probe_broken": [],
                "aged_out": []}

    from bot_squad_worker import sessions as S
    from bot_squad_worker import detector as _detector
    from bot_squad_worker import actions as A

    now = now if now is not None else time.time()
    conv_dir = _conversations_dir(cfg, slug)
    out: dict[str, Any] = {"ok": True, "redriven": [], "probe_broken": [],
                           "aged_out": []}
    if not conv_dir.exists():
        return out

    state = _load_state(cfg, slug)
    state_changed = False

    max_age = max_first_ping_age_sec()
    logs = _conversation_files(conv_dir, now=now, max_age_sec=max_age,
                               tracked=state)
    records = {c["key"]: _read_records(c["path"]) for c in logs}

    # The newest ATTENDANT answer anywhere in each gid's conversation — see the
    # docstring for why only a ROOT log is allowed to consult it, and why the
    # author must be the attendant rather than any session at all.
    attendant_wins = {c["gid"]: attendant_windows(cfg, slug, c["gid"])
                      for c in logs}
    newest_answer: dict[str, str] = {}
    for c in logs:
        ts = _newest_attendant_ts(records[c["key"]], attendant_wins[c["gid"]])
        if ts > newest_answer.get(c["gid"], ""):
            newest_answer[c["gid"]] = ts

    pressure = _detector.session_pressure(cfg)
    pressured_sids = (set(pressure.get("rate_limited_sids", []))
                       | set(pressure.get("limit_blocked_sids", [])))

    try:
        rows = {r["sid"]: r for r in S.list_sessions(cfg, slug)}
    except Exception:
        log.exception("uc_redrive: list_sessions failed for %s", slug)
        rows = {}

    for conv in logs:
        key, gid, thread = conv["key"], conv["gid"], conv["thread"]
        # ``probe:<key>`` holds the PROBE-BROKEN diagnosis for this log. It is
        # a separate entry from the ping campaign because it is a statement
        # about the LOG, which outlives any one message — and it is dropped on
        # every path that concludes the log is readable, since a diagnosis that
        # never retracts is just a second stuck alarm.
        pkey = f"probe:{key}"

        def _forget(*keys: str) -> bool:
            """Drop these state entries; True when any existed. A helper rather
            than ``pop(a) is not None or pop(b) is not None`` because ``or``
            short-circuits — the second pop would silently not run whenever the
            first found something, which is exactly the case where both need
            dropping."""
            return any([state.pop(k, None) is not None for k in keys])

        recs = records[key]
        rec = _unanswered_in(recs)
        if rec is None:
            # Answered (or empty) log — nothing to re-drive. Drop any
            # stale ping state so a LATER stuck message starts fresh.
            state_changed = _forget(key, pkey) or state_changed
            continue

        msg_ts = str(rec.get("timestamp") or "")
        msg_at = _parse_iso(msg_ts)
        if msg_at is None:
            continue  # undatable record — the cadence has nothing to count from

        if not thread and newest_answer.get(gid, "") > msg_ts:
            # Answered in one of this gid's topic logs. The root log could
            # never have recorded that reply; see the docstring — and that is
            # also why its own PROBE-BROKEN diagnosis is dropped here: a log
            # whose answers are read from its siblings is being read fine.
            state_changed = _forget(key, pkey) or state_changed
            continue

        diag = probe_broken(recs, now=now)
        if diag is not None:
            # A DIFFERENT diagnosis, and it must not be spoken as "he is
            # unanswered" — no ping, no hanging-escalation (T-0917 item 2).
            prev = state.get(pkey) or {}
            if (prev.get("last_session_ts") != diag["last_session_ts"]
                    or not prev.get("escalated_to")):
                diag["escalated_to"] = _notify_probe_broken(cfg, slug, key, diag)
                state[pkey] = {"last_session_ts": diag["last_session_ts"],
                               "escalated_to": diag["escalated_to"],
                               "at": now}
                state_changed = True
            log.error(
                "uc_redrive: PROBE-BROKEN for %s/%s — newest session: record "
                "%s (%dh old) with %d ask(s) since; NOT reporting this as an "
                "unanswered message",
                slug, key, diag["last_session_ts"],
                diag["session_age_sec"] // 3600, diag["asks_since"])
            out["probe_broken"].append({"key": key, **diag})
            continue
        state_changed = _forget(pkey) or state_changed  # the log reads again

        gid_state = state.get(key) or {}
        if gid_state.get("msg_ts") != msg_ts:
            gid_state = {"msg_ts": msg_ts, "slot": 0, "pings": 0,
                         "last_ping_at": 0, "escalated_to": []}
            state[key] = gid_state
            state_changed = True
        if not int(gid_state.get("pings") or 0) and now - msg_at > max_age:
            # A message that is already history and on which NO campaign has
            # ever been opened. The test is ``pings == 0``, not "first
            # sighting", because a state entry can exist without a campaign
            # behind it — the live install carries one written under the
            # pre-T-0794 schema (``retries``/``last_redrive_at``, 24 days old),
            # and keying off the entry's mere presence would have let exactly
            # the message this floor exists for through. See
            # :data:`DEFAULT_MAX_FIRST_PING_AGE_SEC`. Recorded rather than
            # silently skipped, so it stays visible without being an alarm.
            if not gid_state.get("aged_out"):
                gid_state["aged_out"] = True
                state[key] = gid_state
                state_changed = True
                log.warning(
                    "uc_redrive: %s/%s has an unanswered message from %s "
                    "(%dh old) that this probe never pinged — NOT opening a "
                    "ping campaign on it", slug, key, msg_ts,
                    int(now - msg_at) // 3600)
            out["aged_out"].append({"key": key, "msg_ts": msg_ts})
            continue

        # ``slot`` is the position in the schedule (what time says is due);
        # ``pings`` is how many nudges were actually SENT. They diverge whenever
        # slots pass unusable — a message that hung five days before its
        # attendant came back is at slot 234 having been pinged once, and the
        # operator alert has to say one, not 234.
        slot = int(gid_state.get("slot") or 0)
        due = pings_due_by(now - msg_at)
        if due <= slot:
            continue  # no slot has come due since the last ping

        try:
            sid = S.live_user_conversation_sid(cfg, slug, gid)
        except Exception:
            log.exception("uc_redrive: live_user_conversation_sid failed for %s/%s",
                           slug, gid)
            continue
        if sid is None:
            continue  # no live attendant — out of scope (see module docstring)
        if sid in pressured_sids:
            continue  # still under 429/limit pressure — don't redrive into the storm

        row = rows.get(sid)
        if row is None or row.get("activity") != "idle":
            continue  # busy (or unknown) — never interrupt an in-flight reply

        try:
            A.dispatch("ensure_user_conversation", {
                "slug": slug, "global_user_id": gid,
                "message_ref": rec.get("text"),
            })
        except Exception:
            log.exception("uc_redrive: redrive dispatch failed for %s/%s", slug, key)
            continue

        gid_state["slot"] = due  # slots passed while it was busy are spent, not owed
        pings = int(gid_state.get("pings") or 0) + 1
        gid_state["pings"] = pings
        gid_state["last_ping_at"] = now
        state[key] = gid_state
        state_changed = True
        out["redriven"].append({"gid": gid, "sid": sid, "pings": pings,
                                "thread": thread})
        log.warning("uc_redrive: re-woke idle attendant %s for %s/%s (ping %d, "
                    "unanswered %ds)", sid, slug, key, pings, int(now - msg_at))

        # Escalate once the ramp is spent — and again on every later slot until
        # a live operator actually receives it (see _notify_operator_stuck).
        if pings >= ESCALATE_AFTER_PINGS and not gid_state.get("escalated_to"):
            gid_state["escalated_to"] = _notify_operator_stuck(
                cfg, slug, key, pings=pings, hanging_sec=now - msg_at,
            )

    if state_changed:
        _save_state(cfg, slug, state)
    return out


def uc_redrive_tick(cfg: Any) -> None:
    """Scheduler entry point (T-0622): one unanswered-message sweep across
    every project. Per-project errors are caught and logged so one bad
    project never kills the sweep — same contract as the sibling lifecycle
    ticks. No-op under ``BOT_SQUAD_UC_REDRIVE=0``."""
    if not enabled():
        return
    for slug in getattr(cfg, "projects", {}) or {}:
        try:
            check_project(cfg, slug)
        except Exception:  # noqa: BLE001
            log.exception("uc_redrive_tick: unhandled error for project %s", slug)
