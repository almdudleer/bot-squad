"""T-0466 / M1-F1.3 — ~1h cache-window idle/waiting-session recycle + postpone.

Stakeholder (SOURCE-VERBATIM Part A): *"it should get recycled on timeout. Good
time for that is cache invalidation timeout, e.g. for claude subscription it's 1
hour. On timeout, stale waiting sessions should be asked to record their results
and exit. They should be able to postpone this until next timeout, allowed to
repeat indefinitely on each timeout. Generally they should postpone if they are
actively waiting for some long ongoing process to finish (e.g. long build),
which is expected to finish within known time boundaries."*

SIBLING of :mod:`autocompact` (T-0467), not a rival. autocompact recycles a
session when its CONTEXT crosses the ceiling; idle_timeout recycles a session
that has been *waiting* — no Claude turn means no API call, so the subscription
cache window is about to go cold. T-0856: *about to*, not *has* — the window
fires at 55 min, strictly BEFORE the 1h TTL rather than on it; see the
CACHE_TTL_SEC block below for why equality was the defect. Both end in the
SAME "record results and exit": the universal-compact handoff (write the full
forward-state to the role artifact → clear the pane → relaunch a fresh
incarnation that boots from the artifact, re-bound to the same assignment).

idle_timeout REUSES autocompact's handoff helpers for every concrete operation
(resolve-artifact / inject-prompt / artifact-mtime / suspend / relaunch /
orphan-alert) — it does NOT fork the exit. It only adds (1) a time-based TRIGGER
and (2) the postpone protocol. autocompact.py is left untouched; the in-flight
handoff + postpone STATE lives on the session md (a sessions-lifecycle concern),
so the two recyclers never share storage and can't race on each other's state.

THE IDLE CLOCK is the Claude transcript jsonl mtime (the last assistant turn ≈
the last API call ≈ the cache anchor), NOT the peer-bus heartbeat. A session
long-polling its inbox has a fresh heartbeat but a cold cache — and that IS the
"stale waiting session" the stakeholder means. Using ``_pane_activity_at``
(jsonl-only) instead of the row's heartbeat-folded ``activity_at`` is deliberate.

POSTPONE (per-window, unbounded): a session runs ``bsq postpone`` → the worker
stamps ``idle_postpone_until = now + window`` on its session md, so the tick
skips the next window. It can repeat every window, forever. ``bsq postpone --for
<seconds>`` stamps a longer deadline — the way a session declares it is waiting
on a bounded job with a known ETA (the stakeholder's "known time boundaries").

AUTO-POSTPONE: while the session is waiting on a *tracked* long bounded job the
recycle auto-defers with NO session action — killing it mid-build would throw
away the very wait it is parked on. The concrete bot-squad "build" is a deploy:
an in-flight ``_jobs/deploy/{queue,processing}/*.json`` with ``requested_by ==
sid`` auto-postpones the requester with zero registration.

T-0563/T-0564/T-0566 (recycle-v2, 2026-07-04): three changes layered on top of
the above, all gated the same:

* every recycle decision now runs through :func:`recycle_gate.recycle_allowed`
  first — a per-project allowlist (T-0563) plus a user-conversation-role /
  human-attached exemption (T-0564). Both apply even to an otherwise-due
  session.
* the FINALIZE step no longer writes a forward-state artifact and relaunches a
  fresh incarnation. Per the stakeholder's 2026-07-04 verdict (T-0566,
  verbatim): *"the autocompact loop is worse than claude's internal compact,
  so probably it should work like IF there are more than 20k tokens in context
  AND the cache is expiring soon, we call /compact, then we terminate the
  session and remember it to be --resume'd"*. NO respawn is ever triggered from
  here — a future resume (``sessions.resume``, which already prefers ``claude
  --resume <uuid>``) is a separate, human-or-automation-driven act reading
  these md fields.

T-0863 (2026-08-11) REPLACES the ``/compact``-then-terminate half of T-0566
above, on the stakeholder's own correction. Two quotes, hours apart:
*«нативный компакт через 55 минут — кринж, пустая трата токенов. Надо чтобы
писал в задачу офк, и сессия завершалась, никакого компакта»*, then *«он должен
просто обновлять контекст по задаче, и всё, никаких файлов, никаких notes, на
одну задачу один артефакт — контекст, он же на тикете в UI виден»*. The native
``/compact`` on this path was always pure waste — the pane is suspended a tick
later, so the squeezed context is paid for and then discarded — and T-0858
measured what it cost: 20 consecutive recycles that each logged success while
recording nothing a successor could read. So the flow is now: over threshold
(``[recycle].compact_min_context_tokens``, still 20000, now read as "is there
forward-state worth writing down") → inject the FINALIZE handoff
(:func:`autocompact.context_handoff_prompt` for a task-bound session, asking it
to run ``bsq ticket context <id>``; :func:`autocompact.handoff_prompt` for a
task-LESS one, which has no ticket to write onto) and stamp
``idle_recycle_phase: finalizing`` + the ARM-time ``idle_recycle_mark``; then
FINALIZE terminates once the write lands (or the bounded wait times out — never
wedge). Below threshold, or with no destination at all, terminate straight away
— never a ``/compact`` whose output nothing will ever read. The ceiling trigger
in :mod:`autocompact` takes the SAME correction, so both recyclers again share
one exit; the destination logic itself lives there
(:func:`autocompact._resolve_compact_target`) and is imported, not forked.

T-0617 COMPACT-AND-STAY (2026-07-18): the T-0566 verdict above was written for
ordinary task sessions, where terminate-and-remember is fine — a future resume
is cheap. T-0616 deliberately did NOT extend it to the human's own exempt
sessions (:func:`recycle_gate.user_session_exempt`): full exemption was the
fail-safe reading at the time, because doing the T-0566 flow's compact-then-ARM
bookkeeping on an exempt session risked the exact double-compact spam T-0616
was fixing, and the stakeholder's actual ask ("compact user sessions before
cache expiry, in place") was left as a follow-up. This is that follow-up:
:func:`_maybe_compact_and_stay` gives exempt sessions their OWN 2-phase
compact-in-place machine — same shape as :func:`_start_recycle` /
:func:`_finalize_compact` (arm → send ``/compact`` → wait for composer-ready →
finalize), but FINALIZE never calls ``sessions.suspend``: the pane, the tmux
session, the Claude process all stay exactly where the human left them. It
uses its OWN md fields (``compact_stay_phase`` / ``compact_stay_armed_at`` /
``compact_stay_last_at``) rather than reusing ``idle_recycle_phase`` /
``idle_recycle_armed_at``, so the two state machines can never collide or
mis-finalize into each other. The anti-loop guard T-0616 flagged as the risk
is ``compact_stay_last_at``: a fresh completion stamp written by FINALIZE that
blocks a new arm for a full ``idle_timeout_sec()`` window, checked
independently of the (possibly stale, post-compact) idle-age signal — so even
if a hook mis-fire or a flaky idle clock says "due" again five seconds later,
this session's own last-completed timestamp says no.

T-0649 (2026-07-18) EXTENDS this same compact-in-place machine to
``autocompact.py``'s context-CEILING trigger, for exempt sessions only —
``autocompact._maybe_compact_stay_ceiling`` arms/finalizes the identical
``compact_stay_*`` fields defined here (:func:`_finalize_compact_stay` and
:func:`compact_stay_due` are called directly from there). Worker sessions
(dev/TL/operator) are untouched: their ceiling trigger still runs the
handoff/artifact+relaunch mechanism in ``autocompact.py`` unchanged. Sharing
the SAME md fields (not forking a second pair) is what makes
``compact_stay_last_at`` an effective anti-loop guard across both triggers —
whichever fires first for a given cache window blocks the other.

T-0655 (2026-07-21) KEEP-ALIVE NUDGE for drive=on operators (stakeholder
verbatim, TG): *"надо убедиться, что оператор у нас не ресайклится через час,
чтобы только воскреситься тут же, он должен поддерживаться всегда в живых пока
drive=on ... система должна его тыкнуть, типа продолжай, но оператор может
решить что ... поставить drive=off, и только тогда заресайклиться"*. A THIRD
narrow exception, same shape as T-0617/T-0649 but a different fix for a
different session: an operator-role session with ``drive`` on (default —
:func:`recycle_gate.operator_drive_on`) never reaches the terminate-and-
remember machinery below on a plain idle-window fire. Instead
:func:`_maybe_keepalive_nudge` injects a "continue" nudge into its pane (one
per cache window — ``operator_keepalive_last_at`` is the anti-loop guard,
mirroring ``compact_stay_last_at``) and leaves the session running. Only once
the OPERATOR ITSELF stamps ``drive: off`` (via ``bsq drive off`` — see
``sessions.set_drive``) does it fall through to the SAME terminate-and-
remember path every other role already rides — at which point
:func:`_terminate_and_remember` skips stamping ``resumable``/``resume_hint``
for it (``self_terminate=True``): a drive=off operator's exit is a deliberate,
considered stop, not a stale-cache artifact worth resuming, and leaving no
resume bait is what the stakeholder's "лучше самозавершиться" ("better to just
self-terminate") preference asks for — it forecloses any future
resurrect-then-immediately-recycle churn (operator_redrive spawns a FRESH
operator if/when new backlog work actually appears, never a resume of this
one). Addendum 1's quota-utilization steering (when a hard weekly quota
target is live, the operator should NOT set drive=off just because
primary-track work ran dry — it should pull from maintenance backlog
instead) is carried entirely in the NUDGE TEXT itself
(:func:`_keepalive_nudge_text`): it is prompt-level framing for the
operator's own judgement call, not something this module can force.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from bot_squad_worker import autocompact, lifecycle_events, recycle_gate, sessions

log = logging.getLogger(__name__)

# --- the cache window, and why the recycle window is NOT equal to it (T-0856) -
#
# CACHE_TTL_SEC is a fact about Anthropic's prompt cache, not a tunable of ours:
# these sessions run on the 1h TTL (every usage record carries
# `ephemeral_1h_input_tokens`). Everything below is arithmetic against it.
#
# Until T-0856 the recycle window WAS 3600 — exactly the TTL — and because the
# tick is 60s a fire lands at 3600..3660s after the last turn, i.e. ALWAYS after
# expiry and never before it. Measured on the live install 2026-08-04 → 08-11
# (audit D-0071 §a3/b5): all 33 drive=on keep-alive wake-ups in the window had a
# 60/61/62-min gap since the previous turn and 33 of 33 were a full cache MISS —
# the wake-up turn re-wrote the whole context as cache_creation. 17.18M
# cache_creation tokens ≈ 68.7M input-equivalents = 4.2% of the week's spend for
# zero delivered work, and 23.5% of every cache-write token spent that week.
#
# The three subtractions, in the order they bite:
#
#   IDLE_TICK_SEC — the scheduler only evaluates the window every 60s, so a
#   window of W fires somewhere in [W, W+60]. Budget the whole tick, not zero.
#
#   CACHE_TURN_MARGIN_SEC — the subtle one, and the reason a 60s margin is not
#   enough. The TTL clock starts at the START of the request that last read the
#   cache, but `_idle_age` measures from the transcript jsonl's mtime, stamped
#   when that turn FINISHED. The generation time of the final assistant turn is
#   therefore already spent before our clock begins.
#
#   240s is sized against that quantity's TAIL, measured rather than guessed
#   (`data/bot-squad/artifacts/evidence/T-0856/final_turn_gap.py`, 3,415 transcripts over the 8
#   days to 2026-08-11): final-turn generation is p50 1.8s, p90 3.9s, p99 49s,
#   p99.9 94s; 17 of 3,415 exceed 60s, 3 exceed 120s, and exactly 1 exceeds 240s
#   (an 11,362s outlier, i.e. a resumed transcript, not a turn). So 60s would
#   lose the race for 0.5% of sessions and 240s for 0.03%. A session whose last
#   turn ran longer still loses it — a bounded miss, not a systematic one, which
#   is the whole difference from the 33/33 this ticket fixes.
#
#   That measurement rests on the assistant entry's `timestamp` being a
#   COMPLETION stamp; if it were stamped at request start the delta would be
#   queueing time and the sizing would be meaningless. Controlled for
#   (`data/bot-squad/artifacts/evidence/T-0856/gap_control.py`): median delta rises
#   monotonically with that turn's own output_tokens — 1.5s (<100 tok), 2.5s,
#   4.4s, 11.6s, 35.2s (>4000 tok) — which only holds if the stamp lands at the
#   end.
#
# What has to land inside the TTL is the ARM, not the exit. A cache hit
# REFRESHES the TTL ("The cache is refreshed for no additional cost each time
# the cached content is used" — Anthropic prompt-caching docs), so once the
# keep-alive nudge or the T-0863 handoff prompt makes the session issue its next
# request, that request is a hit and the clock restarts; every later request in
# the write chain refreshes it again. This is why the finalize half's
# `autocompact.DEFAULT_HANDOFF_TIMEOUT_SEC` (900s) is NOT a term here and must
# not be lowered on this account: a session still working is riding refreshed
# cache, and a session that ignores the arm issues no request at all, so its
# cache expires with nothing charged against it either way.
CACHE_TTL_SEC = 3600
IDLE_TICK_SEC = 60
CACHE_TURN_MARGIN_SEC = 240
# 55 min — the stakeholder's own stated model of this mechanism, and exactly
# CACHE_TTL_SEC - IDLE_TICK_SEC - CACHE_TURN_MARGIN_SEC. Written as a literal on
# purpose: derived from the other three it would be true by construction and the
# invariant test below could never fail, which is a check nobody can watch fail.
DEFAULT_IDLE_TIMEOUT_SEC = 3300


def idle_timeout_enabled() -> bool:
    """Master switch (default ON, mirroring :func:`autocompact.autocompact_enabled`).

    ``BOT_SQUAD_IDLE_TIMEOUT=0`` disables the time-based recycle (a deliberate
    operator kill-switch). The recycle is fully reversible — it writes the
    session's forward-state and relaunches it fresh — so ON-by-default is safe
    and matches the stakeholder's "it should get recycled on timeout".
    """
    return os.environ.get("BOT_SQUAD_IDLE_TIMEOUT", "1") != "0"


def window_fits_cache_ttl(window: int, tick: int = IDLE_TICK_SEC) -> bool:
    """True when a window of ``window`` seconds still fires INSIDE the cache TTL.

    The one predicate this module's arithmetic reduces to (T-0856):
    ``window + tick + CACHE_TURN_MARGIN_SEC <= CACHE_TTL_SEC``. Exported so the
    invariant is checked in one place — the test, the env-override guard below,
    and any future caller read the same function rather than three copies of the
    same sum drifting apart.
    """
    return window + tick + CACHE_TURN_MARGIN_SEC <= CACHE_TTL_SEC


# Values already warned about, so a per-tick-per-session call site can't turn one
# misconfigured env into a log flood. Keyed by value: a LATER, different bad
# override still gets its own line.
_warned_windows: set[int] = set()


def idle_timeout_sec() -> int:
    """The idle/waiting recycle window in seconds (55 min by default).

    Overridable via ``BOT_SQUAD_IDLE_TIMEOUT_SEC``; non-positive/garbage falls
    back to the default so a bad env can never collapse the window to zero (which
    would recycle every session on every tick).

    An override that re-crosses the cache TTL is ACCEPTED (it stays a knob — an
    operator may want it for a reason this module cannot see) but WARNED about
    once per distinct value: silently honouring it is exactly how the 3600
    default went 33/33 cache-miss for a week without anything saying so.
    """
    raw = os.environ.get("BOT_SQUAD_IDLE_TIMEOUT_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0 and not window_fits_cache_ttl(v) and v not in _warned_windows:
                _warned_windows.add(v)
                log.warning(
                    "idle_timeout: BOT_SQUAD_IDLE_TIMEOUT_SEC=%d re-crosses the "
                    "prompt-cache TTL — %d + %ds tick + %ds turn-margin > %ds, so "
                    "every keep-alive wake-up and every recycle arm lands AFTER "
                    "the cache has expired and pays a full re-write (T-0856). "
                    "Honouring it anyway; %d is the safe default.",
                    v, v, IDLE_TICK_SEC, CACHE_TURN_MARGIN_SEC, CACHE_TTL_SEC,
                    DEFAULT_IDLE_TIMEOUT_SEC,
                )
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_IDLE_TIMEOUT_SEC


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- pure decision helpers (unit-testable, no I/O) --------------------------

def idle_due(idle_age: float | None, window: int) -> bool:
    """True when the session's jsonl has been quiet for at least ``window``.

    ``idle_age`` is ``now - <jsonl mtime>`` (seconds). ``None`` (no activity
    signal — can't establish the age) is conservative: NOT due, so a session
    whose cache age we cannot measure is never recycled.
    """
    if idle_age is None:
        return False
    return idle_age >= window


def postpone_active(postpone_until: Any, now: float) -> bool:
    """True when a postpone deadline is set and still in the future."""
    ts = sessions._parse_ts_epoch(postpone_until)
    if ts is None:
        return False
    return now < ts


# --- tracked-long-job detection (auto-postpone) -----------------------------

def _inflight_deploy_for(cfg: Any, slug: str, sid: str) -> bool:
    """True when ``sid`` has a deploy queued or processing — the bot-squad
    analogue of the stakeholder's "long build". Reads the deploy job dir
    directly (queue/ + processing/) and matches ``requested_by``. Never raises:
    any read error reads as "no tracked job" so a transient fs hiccup can't wedge
    the recycle decision.
    """
    if not sid:
        return False
    base = cfg.data_dir / slug / "_jobs" / "deploy"
    for phase in ("processing", "queue"):
        d = base / phase
        if not d.exists():
            continue
        try:
            entries = sorted(d.glob("*.json"))
        except OSError:
            continue
        for f in entries:
            try:
                payload = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("requested_by") == sid:
                return True
    return False


def tracking_long_job(cfg: Any, slug: str, sid: str) -> bool:
    """True when the session is actively waiting on a tracked, time-bounded job.

    Today that is an in-flight deploy/build it requested. A session-declared
    bounded wait (``bsq postpone --for <seconds>``) rides the ``idle_postpone_until``
    stamp instead (handled by :func:`postpone_active`), so it is not re-checked
    here.
    """
    return _inflight_deploy_for(cfg, slug, sid)


# --- per-session executor ---------------------------------------------------

# In-flight compact-wait + postpone state lives as FLAT scalar md fields (never
# a nested mapping) so the line-based session_start hook reader stays happy.
# T-0616 hook contract: session_start.sh preserves these two fields ONLY on a
# source=compact fire (the /compact this recycle sent — clearing them there
# was the D-0053 double-compact root cause) and still clears them on
# startup/resume/clear, where a surviving phase stamp is definitionally stale
# and would hand the next tick a timed-out finalize against a fresh session.
# Renaming either field means updating the hook's _INFLIGHT_RECYCLE set.
_RECYCLE_FIELDS = (
    "idle_recycle_phase",
    "idle_recycle_armed_at",
    # T-0863: the ARM-time snapshot of the handoff destination (a `## Context`
    # digest, or the role artifact's mtime) — one flat string, so FINALIZE can
    # tell "the session wrote its forward-state" from "the wait timed out"
    # without a nested mapping the hook reader cannot parse.
    "idle_recycle_mark",
)


def _clear_recycle_state(meta: dict) -> None:
    for k in _RECYCLE_FIELDS:
        meta.pop(k, None)


# T-0617: compact-and-stay's OWN in-flight pair — deliberately NOT
# ``idle_recycle_phase``/``idle_recycle_armed_at`` so the terminate-flow state
# machine and the stay-in-place state machine can never collide (a session
# can never be "mid-terminate-compact" and "mid-stay-compact" at once, but
# giving them separate fields means neither's finalize can ever misread the
# other's stamp). Same hook contract as ``_RECYCLE_FIELDS``: session_start.sh
# preserves these two ONLY on a source=compact fire, still clears them on
# startup/resume/clear. ``compact_stay_last_at`` is deliberately NOT in this
# tuple — it is a completed-fact stamp (the anti-loop guard), not in-flight
# state, so it survives every hook fire like any other unmanaged field.
_COMPACT_STAY_FIELDS = (
    "compact_stay_phase",
    "compact_stay_armed_at",
)


def _clear_compact_stay_state(meta: dict) -> None:
    for k in _COMPACT_STAY_FIELDS:
        meta.pop(k, None)


def compact_stay_due(last_at: Any, now: float, window: int) -> bool:
    """T-0617 anti-loop guard: a compact-and-stay may ARM at most once per
    cache window. ``last_at`` is the ISO stamp the previous compact-and-stay
    FINALIZED at; ``None``/unparsable never blocks (first fire ever). This is
    independent of the idle-age signal on purpose — T-0616 root-caused the
    double-compact incident on a stale post-compact idle reading, so the
    guard here reads this session's OWN last-completed fact instead of
    trusting the external clock to have reset."""
    ts = sessions._parse_ts_epoch(last_at)
    if ts is None:
        return True
    return (now - ts) >= window


def compact_min_context_tokens(cfg: Any) -> int:
    """T-0566: context-token floor above which a cache-window recycle sends
    Claude's native ``/compact`` before terminating. Below it, nothing is worth
    compacting — terminate + record straight away. ``[recycle]
    .compact_min_context_tokens`` in system_settings.toml, default 20000."""
    v = getattr(cfg, "recycle_compact_min_context_tokens", None)
    try:
        return int(v) if v else 20000
    except (TypeError, ValueError):
        return 20000


def _context_tokens(cfg: Any, slug: str, sid: str) -> int:
    """T-0566: reuse telemetry's already-sampled context-token read (the same
    per-session record autocompact's ceiling trigger reads from) rather than
    re-deriving it from the transcript."""
    from bot_squad_worker import telemetry
    rec = telemetry._read_json(telemetry._record_path(cfg, slug, sid)) or {}
    try:
        return int((rec.get("context") or {}).get("tokens", 0))
    except (TypeError, ValueError):
        return 0


def maybe_recycle(cfg: Any, slug: str, row: dict, now: float, user_home: str) -> bool:
    """Recycle ``row``'s session if it has been idle/waiting past the window AND
    safe AND not postponed AND not gated by T-0563/T-0564. Returns True iff an
    action was taken this tick (compact sent, or terminate+record finalized).
    Every gate fails closed.

    Drives a tiny 2-phase machine on the session md only when there is
    forward-state worth recording (context over threshold) — START asks the
    session to write it and stamps ``idle_recycle_phase: finalizing``; FINALIZE
    waits for that write to land, then terminates + records resume state. A
    below-threshold session skips the wait entirely: START terminates + records
    in the same tick. (T-0566 shape, T-0863 content: the ask used to be
    Claude's native ``/compact``, whose output this path then discarded.)
    """
    if not idle_timeout_enabled():
        return False
    sid = row.get("sid")
    if not sid or row.get("status") != "active":
        return False

    sessions_dir = cfg.data_dir / slug / "sessions"
    md_path = sessions._find_session_md(sessions_dir, sid, row.get("claude_uuid"))
    if md_path is None:
        return False
    meta = sessions._read_session_metadata(md_path)
    if meta is None:
        return False

    role = row.get("role") or meta.get("role") or sessions._derive_role(
        meta.get("window"), meta.get("task_id"), meta.get("initiative"))
    pane = autocompact._pane_for(sid)
    window = row.get("window") or meta.get("window")

    # T-0563: never recycle a non-allowlisted project.
    if not recycle_gate.project_allowed(cfg, slug, now):
        return False
    # T-0926 follow-up: an explicit human pin beats every other signal here —
    # not just terminate, but compact-and-stay too. Checked before is_attached
    # so a pinned session takes no action even through a real (not just
    # flickered) attachment gap.
    if recycle_gate.session_pinned(meta):
        return False
    # T-0564: never touch a pane a human is currently attached to — applies
    # to every session, exempt or not.
    if recycle_gate.is_attached(pane, sid=sid, now=now):
        return False

    # T-0564/T-0616: the human's own sessions (user-conversation role,
    # hand-launched user-session window, recycle_exempt md marker) never ride
    # the terminate-and-remember flow below — T-0617 gives them a separate
    # compact-in-place-only path instead of the old full no-op exemption.
    if recycle_gate.user_session_exempt(role=role, window=window, meta=meta):
        return _maybe_compact_and_stay(cfg, slug, sid, row, meta, md_path, now, pane, user_home)

    # T-0655: a drive=on operator gets a keep-alive nudge instead of the
    # terminate-and-remember machinery below — only the operator's own
    # drive=off decision (bsq drive off) permits it to fall through to the
    # normal recycle path a few lines down.
    if recycle_gate.operator_drive_on(role=role, meta=meta):
        return _maybe_keepalive_nudge(cfg, slug, sid, row, meta, md_path, now, pane, user_home)

    # A handoff already in flight → drive its finalize half (independent of the
    # idle window; the phase field is its own guard). `compacting` is the
    # pre-T-0863 stamp — still accepted so a worker restart mid-recycle
    # converges rather than leaving the session armed forever.
    if meta.get("idle_recycle_phase") in ("finalizing", "compacting"):
        return _finalize_compact(cfg, slug, sid, meta, md_path, now, pane, role=role)

    # Otherwise decide whether to START a recycle this tick.
    idle_age = _idle_age(row, meta, user_home, now)
    if not idle_due(idle_age, idle_timeout_sec()):
        return False
    if postpone_active(meta.get("idle_postpone_until"), now):
        return False
    if tracking_long_job(cfg, slug, sid):
        # Auto-postpone: waiting on a tracked bounded job — defer (reactively, no
        # stamp) so the moment the job clears the normal window applies again.
        log.info("idle_timeout: auto-postpone %s — waiting on a tracked long job", sid)
        return False
    return _start_recycle(cfg, slug, sid, row, meta, md_path, now, pane, role=role)


def _idle_age(row: dict, meta: dict, user_home: str, now: float) -> float | None:
    """Seconds since the session went idle, or None when unknowable.

    T-0470 (M1/F1.7): PRIMARY source is the HOOK-emitted stall signal — the
    Stop-hook ``<cwd>/.claude/bsq_lifecycle/<sid>.stop`` marker stamped the
    moment the turn ended (≈ the subscription cache anchor). This is the "operate
    cohesively with the hooks" path: the timeout decision reads a signal EMITTED
    at the lifecycle transition rather than re-deriving it from a side-effect.

    FALLBACK (no hook marker yet — a pre-hook or brand-new session) is the
    legacy jsonl mtime via ``_pane_activity_at``, so nothing regresses before the
    Stop hook has fired once. Still jsonl-only (NOT the heartbeat-folded
    ``activity_at``) — see the module docstring on THE IDLE CLOCK.
    """
    cwd = str(row.get("cwd") or meta.get("cwd") or "")
    sid = row.get("sid") or meta.get("sid") or ""
    hook_age = lifecycle_events.hook_idle_age(cwd, sid, now)
    if hook_age is not None:
        return hook_age
    claude_uuid = row.get("claude_uuid") or meta.get("claude_uuid")
    at = sessions._pane_activity_at(cwd, claude_uuid, user_home)
    if at is None:
        return None
    return max(0.0, now - at)


def _start_recycle(cfg: Any, slug: str, sid: str, row: dict, meta: dict, md_path,
                   now: float, pane: str | None, *, role: str | None = None) -> bool:
    """START the cache-window recycle. Only ever acts on an idle,
    composer-ready pane — never cut mid-turn.

    T-0863: context over threshold → ASK the session to write its forward-state
    where that session's forward-state lives (a task-bound one: its ticket's
    ``## Context``, via ``bsq ticket context``; a task-less one: its role
    artifact) and stamp ``idle_recycle_phase: finalizing`` plus the ARM-time
    ``idle_recycle_mark``, finalized on a later tick by
    :func:`_finalize_compact`. Context at/below threshold, or no destination at
    all → nothing worth recording, terminate immediately.

    NO ``/compact`` is sent from this path any more, at any threshold. It used
    to be, and it was always waste: this trigger suspends the pane a tick or
    two later, so the squeezed context was paid for and then thrown away
    («кринж, пустая трата токенов»). The token cost bought nothing a successor
    could read — which is the same gap from the other side, and what T-0858
    measured.

    T-0655: ``role`` is threaded through to :func:`_terminate_and_remember` so
    a drive=off operator (the only way an operator reaches this function at
    all — see :func:`maybe_recycle`) self-terminates without resume bait."""
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now):
        return False

    # T-0470: the stall crossed the window → record the timeout lifecycle event
    # on the unified surface for operator measurement (best-effort).
    lifecycle_events.emit(cfg, slug, sid, lifecycle_events.SESSION_TIMEOUT,
                          now=now, reason="idle_window")

    tokens = _context_tokens(cfg, slug, sid)
    threshold = compact_min_context_tokens(cfg)
    if tokens <= threshold:
        # Below threshold — no forward-state worth writing down; terminate now.
        return _terminate_and_remember(cfg, slug, sid, meta, md_path, now,
                                       wrote_state=False,
                                       self_terminate=(role == "operator"))

    target = autocompact._resolve_compact_target(cfg, slug, {
        "sid": sid, "role": role or meta.get("role") or "",
        "task_id": meta.get("task_id"), "window": meta.get("window"),
    })
    if target["kind"] == "none":
        # Nowhere to write it. Terminating straight away is the whole point of
        # T-0863's correction: the old code sent Claude's native `/compact`
        # here and then suspended the pane a tick later, so the compacted
        # context was discarded seconds after being paid for — «нативный
        # компакт через 55 минут — кринж, пустая трата токенов».
        log.info("idle_timeout: no handoff destination for %s — terminating "
                 "without a compact", sid)
        return _terminate_and_remember(cfg, slug, sid, meta, md_path, now,
                                       wrote_state=False,
                                       self_terminate=(role == "operator"))

    try:
        if target["kind"] == "context":
            autocompact._inject_context_handoff(sid, target["task_id"],
                                                relaunch=False)
        else:
            autocompact._inject_handoff(sid, target["artifact_path"],
                                        target["role"], relaunch=False)
    except Exception:
        log.exception("idle_timeout: finalize inject failed for %s (will retry)",
                      sid)
        return False
    meta["idle_recycle_phase"] = "finalizing"
    meta["idle_recycle_armed_at"] = _now_iso()
    meta["idle_recycle_mark"] = autocompact.handoff_mark(target)
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: asked %s to finalize into %s (%d tokens > %d "
             "threshold) — awaiting the write", sid,
             f"{target['task_id']} ## Context" if target["kind"] == "context"
             else target["artifact_path"], tokens, threshold)
    return True


def _finalize_compact(cfg: Any, slug: str, sid: str, meta: dict, md_path, now: float,
                      pane: str | None, *, role: str | None = None) -> bool:
    """FINALIZE an in-flight handoff wait: once the session has written its
    forward-state and the pane is composer-ready again (or the bounded wait
    times out — never wedge), terminate + record.

    T-0863: the destination is RE-RESOLVED here rather than carried across
    ticks. It is a pure function of the session md (task-bound → that ticket's
    ``## Context``; task-less → the role artifact), so only the ARM-time
    snapshot needs persisting — one flat ``idle_recycle_mark`` instead of the
    four fields a carried destination would cost, on an md whose reader
    (``session_start.sh``) can only hold flat scalars.

    An md carrying the pre-T-0863 ``idle_recycle_phase: compacting`` stamp has
    no mark, so the "did it write" test passes immediately and this degrades to
    exactly the old composer-ready wait — a worker restart mid-recycle
    converges instead of wedging.
    """
    armed_at = sessions._parse_ts_epoch(meta.get("idle_recycle_armed_at")) or now
    timed_out = (now - armed_at) > autocompact.handoff_timeout_sec()

    if not pane:
        # Session already gone — nothing left to finalize; drop the stamp.
        _clear_recycle_state(meta)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return False

    target = autocompact._resolve_compact_target(cfg, slug, {
        "sid": sid, "role": role or meta.get("role") or "",
        "task_id": meta.get("task_id"), "window": meta.get("window"),
    })
    # `handoff_mark` returns "" for a destination that no longer resolves (the
    # ticket was deleted or renamed mid-handoff). That differs from the armed
    # mark, so a plain `!=` would report "it wrote its forward-state" about a
    # write that cannot have happened — a false success is worse here than the
    # timeout, because it is what a later reader trusts.
    mark = autocompact.handoff_mark(target)
    wrote_state = bool(mark) and mark != meta.get("idle_recycle_mark")
    ready = autocompact.composer_ready(autocompact._capture_pane(pane),
                                       sid=sid, now=now)

    if not timed_out and not (wrote_state and ready):
        return False  # still writing — retry next tick

    if timed_out and not wrote_state:
        log.warning("idle_timeout: %s never wrote its forward-state within the "
                    "handoff window — terminating anyway (never wedge)", sid)

    return _terminate_and_remember(cfg, slug, sid, meta, md_path, now,
                                   wrote_state=wrote_state,
                                   self_terminate=(role == "operator"))


def _terminate_and_remember(cfg: Any, slug: str, sid: str, meta: dict, md_path, now: float,
                            *, wrote_state: bool, self_terminate: bool = False) -> bool:
    """T-0566: terminate the session (``sessions.suspend`` — same graceful
    C-c/exit/kill-pane sequence autocompact uses) and stamp the resume state on
    its md: ``resumable: true``, ``recycled_at``, ``resume_hint``.
    ``claude_uuid`` is already carried by ``sessions.suspend``. NEVER
    respawns — a future resume is a separate, deliberate act (``sessions.resume``
    already prefers ``claude --resume <uuid>`` over a fresh spawn).

    T-0863 ``wrote_state``: True when the session actually wrote its
    forward-state before this terminate (ticket ``## Context`` for a task-bound
    session, role artifact for a task-less one). It replaces the old
    ``compacted`` flag, which said whether a native ``/compact`` had been sent
    — a fact about token spend that told a later reader nothing about whether
    anything survives. Carried into the resume hint and the lifecycle event so
    "recycled" and "recycled having recorded its state" stay distinguishable in
    the record; T-0858 was opened because 20 consecutive recycles read as
    successes while writing nothing.

    T-0655 ``self_terminate``: True only for an operator that reached here
    with ``drive: off`` already stamped (the only way an operator role gets
    this far — see :func:`maybe_recycle`'s drive=on gate). That is a
    deliberate, considered stop the operator made about ITS OWN continuity,
    not a stale-cache artifact — per the stakeholder's explicit preference
    ("лучше самозавершиться" / better to just self-terminate), it leaves NO
    resumable/resume_hint bait behind, so nothing can ever resurrect this
    exact incarnation into a resume-then-immediately-exit churn loop.
    ``operator_redrive`` still spawns a FRESH operator later if/when new
    backlog work actually appears — that is a distinct, deliberate act."""
    _clear_recycle_state(meta)
    role = meta.get("role") or ""
    task_id = meta.get("task_id")
    try:
        sessions.suspend(cfg, slug, sid, source="idle_timeout",
                         reason="cache-window recycle (compact-terminate-remember)")
    except Exception:
        log.exception("idle_timeout: terminate failed for %s — retry next tick", sid)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return False

    # sessions.suspend() rewrites the md wholesale — re-read then layer the
    # resume-state fields on top (it doesn't know about them).
    fresh = sessions._read_session_metadata(md_path) or meta
    fresh["recycled_at"] = _now_iso()
    if not self_terminate:
        where = (f"{task_id}'s ## Context" if task_id and task_id != "~"
                 else "its role artifact")
        fresh["resumable"] = True
        fresh["resume_hint"] = (
            f"idle cache-window recycle "
            f"({'forward-state written to ' + where if wrote_state else 'no forward-state written'})"
            f" — resume via sessions.resume to continue {task_id or role or sid}.")
    sessions._write_session_metadata(md_path, fresh, atomic=True)

    # T-0470: a cache-window recycle finalized → record it on the unified surface.
    lifecycle_events.emit(cfg, slug, sid, lifecycle_events.SESSION_RECYCLED,
                          now=now, cause="idle_timeout", wrote_state=wrote_state,
                          self_terminate=self_terminate)
    log.info("idle_timeout: recycled %s (wrote_state=%s, self_terminate=%s) — %s",
             sid, wrote_state, self_terminate,
             "no resume state (deliberate stop)" if self_terminate
             else "recorded resumable state")
    return True


# --- T-0617: compact-and-stay (exempt user sessions) ------------------------

def _maybe_compact_and_stay(cfg: Any, slug: str, sid: str, row: dict, meta: dict,
                            md_path, now: float, pane: str | None,
                            user_home: str) -> bool:
    """T-0617: the exempt-session counterpart to :func:`_start_recycle` /
    :func:`_finalize_compact` — same idle-window trigger and context-threshold
    gate, but FINALIZE never terminates. Drives its own 2-phase machine on
    ``compact_stay_phase`` so a tick that lands mid-``/compact`` just retries
    the wait instead of re-arming."""
    if meta.get("compact_stay_phase") == "compacting":
        return _finalize_compact_stay(sid, meta, md_path, now, pane)

    idle_age = _idle_age(row, meta, user_home, now)
    if not idle_due(idle_age, idle_timeout_sec()):
        return False
    if not compact_stay_due(meta.get("compact_stay_last_at"), now, idle_timeout_sec()):
        return False  # already compacted-and-stayed once this cache window
    if postpone_active(meta.get("idle_postpone_until"), now):
        return False
    if tracking_long_job(cfg, slug, sid):
        log.info("idle_timeout: compact-and-stay auto-postpone %s — waiting "
                 "on a tracked long job", sid)
        return False
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now):
        return False

    tokens = _context_tokens(cfg, slug, sid)
    threshold = compact_min_context_tokens(cfg)
    if tokens <= threshold:
        # Nothing worth compacting yet — leave compact_stay_last_at alone so
        # this is re-checked (cheaply) on every later tick, not just once per
        # window, until there's actually context worth clearing.
        return False

    try:
        autocompact._send_compact(sid)
    except Exception:
        log.exception("idle_timeout: compact-and-stay /compact send failed "
                      "for %s (will retry)", sid)
        return False
    meta["compact_stay_phase"] = "compacting"
    meta["compact_stay_armed_at"] = _now_iso()
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: compact-and-stay sent /compact to %s (%d tokens "
             "> %d threshold) — session stays, no terminate", sid, tokens,
             threshold)
    return True


def _finalize_compact_stay(sid: str, meta: dict, md_path, now: float,
                           pane: str | None) -> bool:
    """FINALIZE an in-flight compact-and-stay: once the pane is
    composer-ready again (or the bounded wait times out — never wedge),
    clear the in-flight phase and stamp ``compact_stay_last_at`` (the
    anti-loop guard for the rest of this cache window). NEVER terminates —
    that is the entire point of this path vs. :func:`_finalize_compact`."""
    armed_at = sessions._parse_ts_epoch(meta.get("compact_stay_armed_at")) or now
    timed_out = (now - armed_at) > autocompact.handoff_timeout_sec()

    if not pane:
        # Session already gone by other means — nothing left to finalize.
        _clear_compact_stay_state(meta)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return False

    ready = autocompact.composer_ready(autocompact._capture_pane(pane),
                                       sid=sid, now=now)
    if not ready and not timed_out:
        return False  # still compacting — retry next tick

    if not ready and timed_out:
        log.warning("idle_timeout: compact-and-stay /compact wait timed out "
                    "for %s — leaving the session as-is (never wedge, never "
                    "terminate)", sid)

    _clear_compact_stay_state(meta)
    meta["compact_stay_last_at"] = _now_iso()
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: compact-and-stay finalized for %s — compacted "
             "in place, session left running", sid)
    return True


# --- T-0655: keep-alive nudge (drive=on operators) --------------------------

def keepalive_due(last_at: Any, now: float, window: int) -> bool:
    """Anti-loop guard mirroring :func:`compact_stay_due`: a keep-alive nudge
    may fire at most once per cache window. ``last_at`` is the ISO stamp the
    previous nudge was sent at; ``None``/unparsable never blocks (first fire
    ever). Without this, an operator that ignores the nudge (composer text
    sitting unprocessed — no new turn, so the idle clock never resets) would
    get re-nudged every ~60s tick instead of once per window."""
    ts = sessions._parse_ts_epoch(last_at)
    if ts is None:
        return True
    return (now - ts) >= window


def _keepalive_nudge_text(cfg: Any, slug: str) -> str:
    """T-0655 Addendum 1: the nudge text itself carries the quota-utilization
    steering — a hard weekly target live means "primary work ran dry" is NOT
    by itself grounds for drive=off; the operator should pull from maintenance
    backlog first. This is prompt-level framing for the operator's own
    judgement call, not a mechanism this module can enforce."""
    from bot_squad_worker import operator_redrive

    target = operator_redrive.weekly_quota_target_pct(cfg)
    text = ("continue — your ~1h cache window is about to expire while idle; "
            "you are drive=on so the system is keeping you alive instead of "
            "recycling you. Judge for yourself whether there is genuinely "
            "more unsupervised work you can safely do right now.")
    if target is not None:
        text += (
            f" A weekly quota-utilization target ({target:g}%) is live — "
            "running out of primary-track work is NOT by itself a reason to "
            "set drive=off; pull from the maintenance backlog (tests, code "
            "quality, bug hunting, deeper UI testing) before considering it."
        )
    else:
        text += (
            " No quota-utilization target is set — if there is truly nothing "
            "left you can safely do without a human present, set drive=off "
            "yourself (`bsq drive off`) and this session will end."
        )
    return text


def _send_keepalive_nudge(sid: str, text: str) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid, "text": text})


def _maybe_keepalive_nudge(cfg: Any, slug: str, sid: str, row: dict, meta: dict,
                           md_path, now: float, pane: str | None,
                           user_home: str) -> bool:
    """T-0655: the drive=on-operator counterpart to :func:`_maybe_compact_and_stay`
    — same idle-window trigger, postpone/tracked-job gates, and composer-ready
    gate, but instead of ``/compact`` it injects a plain-text keep-alive nudge
    and NEVER terminates. ``operator_keepalive_last_at`` bounds it to at most
    once per cache window (:func:`keepalive_due`)."""
    idle_age = _idle_age(row, meta, user_home, now)
    if not idle_due(idle_age, idle_timeout_sec()):
        return False
    if not keepalive_due(meta.get("operator_keepalive_last_at"), now, idle_timeout_sec()):
        return False  # already nudged once this cache window
    if postpone_active(meta.get("idle_postpone_until"), now):
        return False
    if tracking_long_job(cfg, slug, sid):
        log.info("idle_timeout: keepalive auto-postpone %s — waiting on a "
                 "tracked long job", sid)
        return False
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now):
        return False

    try:
        _send_keepalive_nudge(sid, _keepalive_nudge_text(cfg, slug))
    except Exception:
        log.exception("idle_timeout: keepalive nudge send failed for %s "
                      "(will retry)", sid)
        return False
    meta["operator_keepalive_last_at"] = _now_iso()
    sessions._write_session_metadata(md_path, meta, atomic=True)
    lifecycle_events.emit(cfg, slug, sid, lifecycle_events.SESSION_TIMEOUT,
                          now=now, reason="idle_window_keepalive")
    log.info("idle_timeout: sent keep-alive nudge to drive=on operator %s — "
             "session stays, no recycle", sid)
    return True


# --- scheduler entry --------------------------------------------------------

def tick(cfg: Any) -> None:
    """Per-project sweep: recycle stale waiting sessions, drive in-flight handoffs.

    Sibling of the telemetry/binding_gc 60s ticks. Per-session and per-project
    errors are swallowed so one bad session/project never kills the sweep.
    """
    if not idle_timeout_enabled():
        return
    user_home = sessions._get_user_home()
    cur_user = sessions._get_current_user()
    now = time.time()
    for slug in cfg.projects:
        try:
            rows = sessions.list_sessions(cfg, slug)
        except Exception:
            log.exception("idle_timeout.tick: list_sessions failed for %s", slug)
            continue
        for row in rows:
            if row.get("status") != "active":
                continue
            # Per-user worker reads only its own ~/.claude (the idle clock lives
            # under a home we can't stat for other users).
            if (row.get("linux_user") or cur_user) != cur_user:
                continue
            try:
                maybe_recycle(cfg, slug, row, now, user_home)
            except Exception:
                log.exception("idle_timeout.tick: %s failed", row.get("sid"))
