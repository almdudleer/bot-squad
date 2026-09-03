"""T-0466 / M1-F1.3 — the ~1h cache-window idle/waiting-session recycle.

Stakeholder (SOURCE-VERBATIM Part A): *"it should get recycled on timeout. Good
time for that is cache invalidation timeout, e.g. for claude subscription it's 1
hour. On timeout, stale waiting sessions should be asked to record their results
and exit. They should be able to postpone this until next timeout, allowed to
repeat indefinitely on each timeout. Generally they should postpone if they are
actively waiting for some long ongoing process to finish (e.g. long build),
which is expected to finish within known time boundaries."*

The postpone half of that quote is now served WITHOUT a session-facing verb:
T-0954 (stakeholder, 2026-09-03) withdrew the manual controls — «ту тему с
пинами/manual handling сессий … надо убрать, это была ошибка» — and what he
described here, a session parked on a long build with a known ETA, is exactly
what :func:`tracking_long_job` detects by itself. The need survives; the
declaration does not.

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
and (2) the auto-postpone. autocompact.py is left untouched; the in-flight
handoff STATE lives on the session md (a sessions-lifecycle concern),
so the two recyclers never share storage and can't race on each other's state.

THE IDLE CLOCK is the Claude transcript jsonl mtime (the last assistant turn ≈
the last API call ≈ the cache anchor), NOT the peer-bus heartbeat. A session
long-polling its inbox has a fresh heartbeat but a cold cache — and that IS the
"stale waiting session" the stakeholder means. Using ``_pane_activity_at``
(jsonl-only) instead of the row's heartbeat-folded ``activity_at`` is deliberate.

POSTPONE, the manual verb, is GONE (T-0954). A session used to defer its own
window with ``bsq postpone``, and the stakeholder withdrew that whole class of
control on 2026-09-03 — «ту тему с пинами/manual handling сессий, которую мы
ввели, надо убрать, это была ошибка. Надо просто сделать нормальный процесс.» A
session hand-deferring the process that manages it is a patch over a process
that misbehaves; the auto-postpone below is the same need met by measurement
instead of by a declaration, and it is what remains.

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
``sessions.set_drive``) does it fall through to the terminate-and-remember
path. **T-0945 SUPERSEDES THE REST OF THIS PARAGRAPH.** It used to skip
stamping ``resumable``/``resume_hint`` for such an operator
(``self_terminate=True``, «лучше самозавершиться» — a deliberate stop leaves no
resume bait, so nothing can resurrect it into a resume-then-immediately-recycle
churn). The 2026-08-31 ruling gives the operator the user-conversation contract
instead — «С ролью operator — то же самое» + «и потом всегда resume» — so a
drive=off operator now runs handoff + ``/compact`` + a RESUMABLE exit like the
attendant and the flag is gone. (``operator_redrive`` still spawns a FRESH
operator when backlog work appears, and nothing auto-resumes the stamp, so the
churn it guarded against does not follow from it.) Addendum 1's quota-utilization steering (when a hard weekly quota
target is live, the operator should NOT set drive=off just because
primary-track work ran dry — it should pull from maintenance backlog
instead) is carried entirely in the NUDGE TEXT itself
(:func:`_keepalive_nudge_text`): it is prompt-level framing for the
operator's own judgement call, not something this module can force.

T-0945 RECYCLE-BY-ROLE v2 (2026-08-31) collapses every branch above into ONE
pure decision, :func:`recycle_plan`. Stakeholder verbatim, which supersedes the
T-0930 ladder wherever the two disagree: *«в зависимости от того, какие роли
держит сессия, мы ее по разному можем ресайклить … компакт это дорогая
операция, и если мы не собираемся продолжать сессию через resume или в этом же
окне вообще никогда, нужно только handoff … Важно, чтобы она не начала делать
компакт на всякий случай, надо только если есть основание что будет
продолжение»*. The system supplies the DEADLINE (55 min, unchanged) and the
CRITERIA; what the session writes into the handoff stays the session's own
done-or-not judgement — *«Это должна решать сама сессия … но система должна ей
ставить дедлайн и предоставлять четкие критерии»*.

  role / state                      plan           at the deadline
  --------------------------------  -------------  --------------------------
  human attached, or a             stay           handoff → ready → compact
  hand-launched user-session                       IN PLACE, no exit
  / ``recycle_exempt`` pane
  user-conversation                 compact_exit   handoff → /compact → exit
                                                   resumable, always resumed
                                                   («у нее всегда есть
                                                   продолжение»)
  operator, drive ON                nudge          keeps driving; never dies
                                                   on the timeout
  operator, drive OFF               compact_exit   «С ролью operator — то же
                                                   самое»
  dev / TL, a bound task alive      nudge          «продолжать только пока
                                                   какая-то из их задач жива»
  dev / TL, no bound task alive     handoff_exit   handoff → exit, NO compact
  anything else                     handoff_exit

THE COMPACT IS THE PART THAT NEEDS A REASON, and only ``compact_exit`` has
one: a resume is structurally certain for those two roles (the attendant is
resumed by ``ensure_user_conversation`` on the next inbound message, and the
operator seat travels with it — T-0943), so the compacted transcript is what
that resume loads and the spend buys something. ``handoff_exit`` spends
nothing: *«если что, можно будет начать новую из тикета, компакты делать не
надо»*. A below-threshold context skips the compact under EVERY plan — there
is nothing to squeeze — which is the mechanical form of "never на всякий
случай".

WHAT CHANGED vs T-0930, named explicitly because both rulings are days old and
they conflict:

* the user-conversation exit line moves from ~3 h to the 55 min window and now
  compacts before it exits. ``BOT_SQUAD_UC_EXIT_SEC`` survives as the knob
  (0 restores plain compact-and-stay), but its default is no longer a separate
  number — it is :func:`idle_timeout_sec`.
* a drive=off operator now exits RESUMABLE. T-0655's ``self_terminate`` (leave
  no resume bait, *«лучше самозавершиться»*) is withdrawn by *«С ролью
  operator — то же самое»* + *«потом всегда resume»*: the two roles flow into
  one another and cannot hold opposite exit contracts.
* the drive-unmet nudge covers TEAM-LEAD as well as dev, and reads ALL of a
  session's bindings (``task_id`` + ``extra_task_ids``, plus a task-less TL's
  initiative) rather than the primary alone — *«какая-то из их задач»*.
* an ATTACHED or PINNED pane no longer means "take no action at all"; it means
  compact-in-place. His pain was the EXIT, not the compact — *«моя проблема
  была с тем, что он делал handoff + exit и у меня терялся просто весь
  контекст беседы»*, *«исчезновение сессии у меня из под носа»* — and he asked
  for the compact by name: *«к ручной сессии актуальны те же правила, нужно до
  протухания кешей сделать компакт»*. T-0930 already made this exact call for
  the ceiling trigger (*«это разумная компакт логика даже когда я работаю с
  сессией»*, ``autocompact.maybe_compact``); this is the idle trigger catching
  up with it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from bot_squad_worker import autocompact, lifecycle_events, recovery, recycle_gate, sessions

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


# T-0930: the stakeholder's own cadence split — "дев, если остановился ...
# спустя 5 минут ... nudge (если он не ждет build) ... Оператор -- спустя уже
# 40 минут, т.к. оператор может ждать девов и долго." The keep-alive nudge
# used to fire on the SAME 3300s window as the terminate-and-remember trigger
# (a session too close to the cache TTL to be worth nudging sooner anyway,
# back when nudging was the ONLY alternative to recycling); now that a
# drive-unmet nudge is its own concept independent of the cache clock, it gets
# its own, shorter, independently-tunable cadence. 2400s = 40 min.
DEFAULT_OPERATOR_NUDGE_SEC = 2400


def operator_nudge_sec() -> int:
    """The drive=on-operator keep-alive nudge cadence (40 min by default) —
    DECOUPLED from :func:`idle_timeout_sec`'s cache-window recycle trigger
    (T-0930). Overridable via ``BOT_SQUAD_OPERATOR_NUDGE_SEC``; non-positive/
    garbage falls back to the default, same failure posture as
    :func:`idle_timeout_sec`. No cache-TTL cross-check here — unlike the
    recycle window, an operator nudge re-firing after the TTL is not a
    cache-miss hazard, just a later "continue"."""
    raw = os.environ.get("BOT_SQUAD_OPERATOR_NUDGE_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_OPERATOR_NUDGE_SEC


# T-0930: "дев, если остановился, а его задача не выполнена по статусу, то уже
# спустя 5 минут он должен получать nudge (если он не ждет build или что-то
# еще может его разбудить)". Much shorter than the operator's 40min — a dev
# "может ждать девов и долго" is the OPERATOR's excuse for patience, not the
# dev's.
DEFAULT_DEV_NUDGE_SEC = 300


def dev_nudge_sec() -> int:
    """The drive=unmet dev nudge cadence (5 min by default), independent of
    both :func:`idle_timeout_sec` and :func:`operator_nudge_sec` (T-0930).
    Overridable via ``BOT_SQUAD_DEV_NUDGE_SEC``; same non-positive/garbage
    fallback posture as its siblings."""
    raw = os.environ.get("BOT_SQUAD_DEV_NUDGE_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_DEV_NUDGE_SEC


# --- T-0945: the per-role recycle PLAN (the "четкие критерии") --------------
#
# Four plans, and the only one that spends a native /compact is the one where a
# resume is structurally certain. See the module docstring's table for the
# mapping and the verbatim each row comes from.
PLAN_STAY = "stay"                  # compact IN PLACE; never terminate
PLAN_NUDGE = "nudge"                # keep it alive; never terminate
PLAN_COMPACT_EXIT = "compact_exit"  # handoff -> /compact -> exit resumable
PLAN_HANDOFF_EXIT = "handoff_exit"  # handoff -> exit, no compact

# Roles whose continuation is conditional on their WORK rather than on a human
# ("логично продолжать только пока какая-то из их задач жива"). Enumerated from
# what `sessions._derive_role` can actually RETURN, not from the two names the
# stakeholder happened to say: `prod-teamlead` IS a team-lead and `qa` IS a
# task-bound worker, so leaving either out would keep recycling it at 55 min
# with live work in hand — the exact defect this ticket fixes for the TL. Zero
# live sessions hold those two today, which is precisely why a name-shaped
# reading of the rule would have missed them silently.
WORKER_ROLES = ("dev", "teamlead", "prod-teamlead", "qa")

# The subset that WAITS ON OTHER SESSIONS, and so gets the operator's patient
# 40 min cadence instead of the dev's 5 («оператор может ждать девов и долго» —
# a TL waits on its devs for the same reason).
COORDINATOR_ROLES = ("teamlead", "prod-teamlead")

# The in-flight phase stamped between the /compact a compact_exit sends and the
# terminate that follows it. Deliberately NOT the legacy "compacting" value:
# that one is the pre-T-0863 stamp for a handoff wait and is still accepted by
# :func:`_finalize_compact`, so reusing it would route a T-0945 compact-exit
# into the wrong half of the machine after a worker restart.
PHASE_COMPACT_EXIT = "compacting_exit"

# T-0954: compact-and-stay is a THREE-phase machine, not a bare /compact. The
# stakeholder's rule — «не должно происходить просто compact, должен всегда
# handoff + ready for compact и только потом compact» — makes the handoff a
# precondition of the squeeze on EVERY trigger, not only on the exiting plans.
# `handoff` is armed and waiting for the session's write; `ready` is the state
# the write lands in and is stamped before the `/compact` is sent, so "compact
# without a completed handoff" is a state the md can never show.
PHASE_STAY_HANDOFF = "handoff"
PHASE_STAY_READY = "ready"


def worker_nudge_sec(role: str | None) -> int:
    """The keep-alive cadence for a :data:`WORKER_ROLES` session with live work.

    A dev — and a qa session, task-bound the same way — gets T-0930's 5 min. A
    team-lead of either flavour (:data:`COORDINATOR_ROLES`) gets the OPERATOR's
    40 min: a TL sits waiting on its devs for exactly the reason the operator
    does («оператор может ждать девов и долго»), so the dev cadence would nudge
    it every 5 minutes while it is legitimately waiting for someone else's
    build."""
    return (operator_nudge_sec() if (role or "") in COORDINATOR_ROLES
            else dev_nudge_sec())


def bound_task_ids(row: dict | None, meta: dict | None) -> list[str]:
    """Every task this session is bound to — primary + ``extra_task_ids``.

    T-0945: «какая-то из их задач» is plural, and a bundled dev (``bsq spawn
    <ticket> --bundle …``) carries its extra bindings ONLY in
    ``extra_task_ids``. Reading the primary alone would recycle a session whose
    bundled tickets are all still open. Order-preserving and de-duplicated; the
    ``~`` unset sentinel is dropped."""
    out: list[str] = []
    def _add(v) -> None:
        t = str(v or "").strip()
        if t and t != "~" and t not in out:
            out.append(t)
    for src in (row or {}, meta or {}):
        _add(src.get("task_id"))
        extra = src.get("extra_task_ids")
        if isinstance(extra, (list, tuple)):
            for v in extra:
                _add(v)
        elif extra:
            for v in str(extra).strip("[]").split(","):
                _add(v)
    return out


def task_alive(cfg: Any, slug: str, task_id: str | None) -> bool:
    """True when ``task_id`` is neither DONE nor WAITING — the "стоит, а задача
    не выполнена" condition that keeps its session alive.

    An unreadable/absent task is NOT alive: there is nothing for a "продолжай"
    nudge to point at. Reads :func:`recovery.read_task_status` and
    ``recovery.DONE_STATUSES``/``WAITING_STATUSES`` — the same reader and the
    same sets :mod:`graceful_exit` uses — rather than a third local copy.
    A WAITING (``blocked_on_user``) task is deliberately NOT alive here: that
    is what lets its session fall through to the terminate path and be revived
    by ``wait_resume.tick`` when the block lifts, instead of being nudged at a
    stakeholder who has not answered yet (T-0930)."""
    if not task_id:
        return False
    status = recovery.read_task_status(cfg, slug, task_id)
    if not status:
        return False
    return status not in recovery.DONE_STATUSES and status not in recovery.WAITING_STATUSES


def worker_tasks_alive(cfg: Any, slug: str, task_ids: list[str], *,
                       role: str | None = None, initiative: Any = None) -> bool:
    """True when ANY of a dev/TL session's bindings is still live work.

    The task-less TL is the one shape with no binding to read: it is bound to an
    INITIATIVE, and its work is alive while that initiative still has an
    unclosed task — the same signal ``graceful_exit`` already computes for its
    done-check (:func:`graceful_exit.count_pending_initiative_tasks`), imported
    rather than re-derived so the "alive" and "done" halves cannot drift apart."""
    for tid in task_ids:
        if task_alive(cfg, slug, tid):
            return True
    if task_ids or (role or "") not in COORDINATOR_ROLES:
        return False
    init = str(initiative or "").strip()
    if not init or init == "~":
        return False
    from bot_squad_worker import graceful_exit
    try:
        return graceful_exit.count_pending_initiative_tasks(cfg, slug, init) > 0
    except Exception:
        log.exception("idle_timeout: initiative pending-count failed for %s", init)
        return False


def recycle_plan(*, role: str | None, window: str | None, meta: dict | None,
                 attached: bool, tasks_alive: bool) -> str:
    """THE per-role criterion (T-0945). Pure — no I/O, no clock — so the policy
    can be read and tested as a table rather than traced through the executor.

    Precedence, and why each step sits where it does:

    1. **attached → stay.** A human is looking at this pane. The only action
       that is ever safe here is the compact he asked for; the exit is what
       cost him «весь контекст беседы».

       T-0954 removed the second half of this test. A ``pinned`` marker used to
       force the same verdict, and the stakeholder withdrew the whole idea:
       «ту тему с пинами/manual handling сессий, которую мы ввели, надо убрать,
       это была ошибка. Надо просто сделать нормальный процесс.» A pin was a
       manual patch over a process that misbehaved — it is the process that had
       to change, and the rest of this ticket is that change.
    2. **a hand-launched user-session / ``recycle_exempt`` pane → stay.** Same
       reason, without needing a client attached this second: these are his own
       panes and they are never terminated, only compacted in place (T-0616/
       T-0617, unchanged).
    3. **user-conversation → compact_exit** (or ``stay`` when the exit is
       disabled by ``BOT_SQUAD_UC_EXIT_SEC=0``).
    4. **operator** — drive on ⇒ ``nudge`` (T-0655's rule, and the ONE exception
       the stakeholder named: «Только если drive какой-либо стоит, сессия с
       ролью оператор должна драйвиться дальше, а не умирать по таймауту»);
       drive off ⇒ the user-conversation plan, «то же самое».
    5. **dev / team-lead** — alive work ⇒ ``nudge``; nothing alive ⇒
       ``handoff_exit``.
    6. anything else ⇒ ``handoff_exit`` (the pre-T-0945 default for every
       non-exempt session, unchanged).
    """
    r = (role or "").strip()
    if attached:
        return PLAN_STAY
    if not recycle_gate.role_exempt(r) and recycle_gate.user_session_exempt(
            role=r, window=window, meta=meta):
        return PLAN_STAY
    if recycle_gate.role_exempt(r):                       # user-conversation
        return PLAN_COMPACT_EXIT if uc_exit_sec() > 0 else PLAN_STAY
    if r == "operator":
        if recycle_gate.operator_drive_on(role=r, meta=meta):
            return PLAN_NUDGE
        return PLAN_COMPACT_EXIT if uc_exit_sec() > 0 else PLAN_HANDOFF_EXIT
    if r in WORKER_ROLES:
        return PLAN_NUDGE if tasks_alive else PLAN_HANDOFF_EXIT
    return PLAN_HANDOFF_EXIT


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

    Today that is an in-flight deploy/build it requested. This is the ONLY
    deferral left: T-0954 removed the session-declared one (``bsq postpone``),
    so a wait now has to be VISIBLE to the worker to count, not merely asserted
    by the session that wants it.
    """
    return _inflight_deploy_for(cfg, slug, sid)


# --- per-session executor ---------------------------------------------------

# In-flight compact-wait state lives as FLAT scalar md fields (never
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
    # T-0945: `wrote_state` carried ACROSS the pre-exit /compact of a
    # `compact_exit` plan. The mark it was derived from is stale the moment the
    # session writes, so the fact has to be persisted rather than recomputed —
    # otherwise the terminate that follows the compact would report "no
    # forward-state written" about a write it watched land. Flat scalar
    # ("true"/"false") for the same hook-reader reason as its siblings, and in
    # `session_start.sh`'s `_INFLIGHT_RECYCLE` set for the same reason too: it
    # must survive the source=compact fire and be cleared on every other.
    "idle_recycle_wrote_state",
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
    # T-0954: the ARM-time handoff mark. In-flight like its siblings — a
    # mark that outlived its sequence would read as "it already wrote".
    "compact_stay_mark",
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
    """Act on ``row``'s session per its T-0945 :func:`recycle_plan`, once it is
    idle past the window AND safe AND not gated by T-0563.
    Returns True iff an action was taken this tick (nudge sent, compact sent, or
    terminate+record finalized). Every gate fails closed.

    T-0945: the ROLE decides which of four things "recycle" means here —
    compact-in-place and stay, a keep-alive nudge, handoff+compact+exit, or
    handoff+exit. :func:`recycle_plan` is that decision and it is pure; this
    function is only its executor.

    The two exiting plans drive a small phase machine on the session md, and
    only when there is forward-state worth recording (context over threshold):
    START asks the session to write it and stamps ``idle_recycle_phase:
    finalizing``; FINALIZE waits for that write to land, then either terminates
    (``handoff_exit``) or stamps ``compacting_exit`` and terminates a tick later
    (``compact_exit``, which spends one ``/compact`` in between). A
    below-threshold session skips both waits: START terminates + records in the
    same tick, with no compact under either plan. (T-0566 shape, T-0863 content:
    the ask used to be Claude's native ``/compact``, whose output this path then
    discarded; T-0945 brings a compact back on ONE plan, where it is loaded by a
    resume rather than thrown away.)
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

    # T-0945: ONE per-role decision, taken before any action (see
    # :func:`recycle_plan` and the module docstring's table). The gates that
    # used to short-circuit here — pinned (T-0926) and attached (T-0564) — are
    # now INPUTS to that decision rather than blanket no-ops: they downgrade the
    # session to compact-in-place instead of letting its cache expire untouched.
    attached = recycle_gate.is_attached(pane, sid=sid, now=now)
    tasks_alive = False
    if (role or "") in WORKER_ROLES:
        tasks_alive = worker_tasks_alive(
            cfg, slug, bound_task_ids(row, meta), role=role,
            initiative=(row.get("initiative") or meta.get("initiative")))
    plan = recycle_plan(role=role, window=window, meta=meta, attached=attached,
                        tasks_alive=tasks_alive)

    # An in-flight compact-and-stay finalizes first whatever the plan says now:
    # a `/compact` has already been sent and abandoning the wait would let the
    # next tick arm a second one. autocompact's CEILING trigger arms this same
    # pair (T-0649), so the in-flight state here is not always ours.
    # T-0954: he is mid-sentence and the deadline is near — say so once, rather
    # than deferring in silence for the rest of the window. Applies to every
    # plan (whatever we were about to do, we are not doing it while he types),
    # but NOT while a sequence is already in flight: a handoff or a /compact
    # that has already left needs its finalize half more than he needs a second
    # message, and preempting it here would leave the phase armed.
    if not (meta.get("compact_stay_phase") or meta.get("idle_recycle_phase")):
        if _maybe_warn_while_typing(cfg, slug, sid, row, meta, md_path, now,
                                    pane, user_home):
            return True

    stay_phase = meta.get("compact_stay_phase")
    if stay_phase == "compacting":
        return _finalize_compact_stay(sid, meta, md_path, now, pane)
    if stay_phase in (PHASE_STAY_HANDOFF, PHASE_STAY_READY):
        # T-0954: the handoff half of a compact-and-stay is in flight. Same
        # reason as the `compacting` case above — dropping the wait here
        # would let the next tick arm a second handoff on top of it.
        return _finalize_stay_handoff(cfg, slug, sid, meta, md_path, now,
                                      pane, role=role)

    if plan == PLAN_STAY:
        # T-0945: a human attached (or pinned the pane) while a terminate
        # handoff was in flight. Abandon the terminate half rather than finish
        # killing a session he is now looking at — «исчезновение сессии у меня
        # из под носа». What he already wrote stays written; only the exit is
        # dropped.
        if meta.get("idle_recycle_phase"):
            _clear_recycle_state(meta)
            sessions._write_session_metadata(md_path, meta, atomic=True)
            log.info("idle_timeout: %s became attached/pinned mid-recycle — "
                     "terminate half abandoned (compact-in-place only)", sid)
            return True
        return _maybe_compact_and_stay(cfg, slug, sid, row, meta, md_path, now,
                                       pane, user_home, role=role)

    # A handoff already in flight → drive its finalize half (independent of the
    # idle window; the phase field is its own guard). `compacting` is the
    # pre-T-0863 stamp — still accepted so a worker restart mid-recycle
    # converges rather than leaving the session armed forever.
    phase = meta.get("idle_recycle_phase")
    if phase == PHASE_COMPACT_EXIT:
        # T-0945: the /compact a compact_exit sent has been issued; all that is
        # left is to terminate once the pane comes back.
        return _finalize_compact_exit(cfg, slug, sid, meta, md_path, now, pane)
    if phase in ("finalizing", "compacting"):
        return _finalize_compact(cfg, slug, sid, meta, md_path, now, pane,
                                 role=role, plan=plan)

    if plan == PLAN_NUDGE:
        # Never dies on the timeout while its drive condition holds: the
        # operator's own `drive: on` (T-0655), or a dev/TL still holding live
        # work (T-0930, widened to TL + all bindings by T-0945).
        #
        # T-0954: a session working ALONE in its window gets the squeeze too —
        # «и если сессия сама работает в окне автономно, то тут тоже если грядет
        # таймаут, ей надо слать compact». A bare "продолжай" nudge costs a full
        # cache-write turn (T-0856 measured 33/33 misses) and does nothing about
        # a context that has grown; when there IS something to squeeze, the
        # handoff -> ready -> compact sequence keeps the session alive AND
        # smaller. Below the threshold there is nothing to compact, so the nudge
        # remains what it always was.
        # ...but only AT the window, and only if the compact actually happens:
        # the nudge cadence (dev 5 min, TL 40 min) is a different clock and must
        # keep running underneath. A compact-and-stay that declines — anti-loop
        # stamp, a composer he is typing in, nothing to hand off to — falls
        # through to the nudge rather than silencing the tick.
        if (idle_due(_idle_age(row, meta, user_home, now), idle_timeout_sec())
                and _context_tokens(cfg, slug, sid) > compact_min_context_tokens(cfg)):
            if _maybe_compact_and_stay(cfg, slug, sid, row, meta, md_path, now,
                                       pane, user_home, role=role):
                return True
        if (role or "") == "operator":
            return _maybe_keepalive_nudge(cfg, slug, sid, row, meta, md_path,
                                          now, pane, user_home)
        return _maybe_worker_nudge(cfg, slug, sid, row, meta, md_path, now,
                                   pane, user_home, role=role)

    # PLAN_COMPACT_EXIT / PLAN_HANDOFF_EXIT — decide whether to START this tick.
    # The deadline differs only by name: `uc_exit_sec()` defaults to
    # `idle_timeout_sec()` and exists so a live install can retune the exit
    # roles without moving everyone's window.
    idle_age = _idle_age(row, meta, user_home, now)
    deadline = uc_exit_sec() if plan == PLAN_COMPACT_EXIT else idle_timeout_sec()
    if not idle_due(idle_age, deadline):
        return False
    if tracking_long_job(cfg, slug, sid):
        # Auto-postpone: waiting on a tracked bounded job — defer (reactively, no
        # stamp) so the moment the job clears the normal window applies again.
        log.info("idle_timeout: auto-postpone %s — waiting on a tracked long job", sid)
        return False
    return _start_recycle(cfg, slug, sid, row, meta, md_path, now, pane,
                          role=role, plan=plan)


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
                   now: float, pane: str | None, *, role: str | None = None,
                   plan: str = PLAN_HANDOFF_EXIT) -> bool:
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

    T-0945: ``plan`` selects what happens AFTER the forward-state lands —
    :data:`PLAN_HANDOFF_EXIT` terminates directly (no compact, ever), while
    :data:`PLAN_COMPACT_EXIT` spends one native ``/compact`` first because that
    session is going to be resumed and the squeezed transcript is what the
    resume loads. Both share this one arm; only :func:`_finalize_compact`
    diverges. Below the context threshold neither compacts: there is nothing to
    squeeze, so "compact only when there is a basis" and "compact only when
    there is something to compact" agree.

    T-0655's ``self_terminate`` (a drive=off operator leaving no resume bait) is
    NOT passed any more — T-0945 gives the operator the user-conversation
    contract, «то же самое», so its exit is resumable like the attendant's."""
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now, cfg=cfg,
            slug=slug):
        return False

    # T-0470: the stall crossed the window → record the timeout lifecycle event
    # on the unified surface for operator measurement (best-effort).
    lifecycle_events.emit(cfg, slug, sid, lifecycle_events.SESSION_TIMEOUT,
                          now=now, reason="idle_window")

    tokens = _context_tokens(cfg, slug, sid)
    threshold = compact_min_context_tokens(cfg)
    if tokens <= threshold:
        # Below threshold — no forward-state worth writing down, and nothing
        # worth a /compact under either plan; terminate now.
        return _terminate_and_remember(cfg, slug, sid, meta, md_path, now,
                                       wrote_state=False)

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
        if plan == PLAN_COMPACT_EXIT:
            # T-0945: nowhere to hand off to, but this role IS coming back via
            # resume and its context is over the threshold — so the compact
            # still buys something the exit alone would throw away. Skip
            # straight to the pre-exit compact phase.
            log.info("idle_timeout: no handoff destination for %s — compacting "
                     "before the resumable exit anyway (T-0945)", sid)
            return _arm_compact_exit(sid, meta, md_path, now, pane, cfg=cfg,
                                     slug=slug,
                                     wrote_state=False)
        log.info("idle_timeout: no handoff destination for %s — terminating "
                 "without a compact", sid)
        return _terminate_and_remember(cfg, slug, sid, meta, md_path, now,
                                       wrote_state=False)

    # T-0945: tell the session which exit it is in — it changes what is worth
    # writing, and carrying the deadline + the "you decide done-or-not, say it
    # in the ticket status" criteria is the system's half of «система должна ей
    # ставить дедлайн и предоставлять четкие критерии».
    resume = (plan == PLAN_COMPACT_EXIT)
    try:
        if target["kind"] == "context":
            autocompact._inject_context_handoff(sid, target["task_id"],
                                                relaunch=False, resume=resume)
        else:
            autocompact._inject_handoff(sid, target["artifact_path"],
                                        target["role"], relaunch=False,
                                        resume=resume)
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
                      pane: str | None, *, role: str | None = None,
                      plan: str = PLAN_HANDOFF_EXIT) -> bool:
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

    if plan == PLAN_COMPACT_EXIT:
        if ready:
            # T-0945: the ONE justified compact on this path. The session is
            # about to be terminated AND resumed (`ensure_user_conversation`
            # resumes the attendant on the next inbound message), so the
            # squeeze is what that resume loads rather than something discarded
            # a tick later — the distinction T-0863 drew when it removed the
            # unconditional /compact from here.
            return _arm_compact_exit(sid, meta, md_path, now, pane, cfg=cfg,
                                     slug=slug,
                                     wrote_state=wrote_state)
        # Timed out against a busy pane. `/compact` needs the same idle,
        # composer-ready pane this finalize does, so there is no way to spend it
        # here — terminate without it rather than wedge. The resume still gets
        # the full transcript, just uncompacted.
        log.warning("idle_timeout: %s never went composer-ready within the "
                    "handoff window — exiting WITHOUT the pre-exit compact "
                    "(never wedge)", sid)

    return _terminate_and_remember(cfg, slug, sid, meta, md_path, now,
                                   wrote_state=wrote_state)


def _arm_compact_exit(sid: str, meta: dict, md_path, now: float,
                      pane: str | None, *, wrote_state: bool,
                      cfg: Any = None, slug: str | None = None) -> bool:
    """T-0945 PHASE 2 of a ``compact_exit``: send the native ``/compact``, then
    hand the terminate to :func:`_finalize_compact_exit` on a later tick.

    Split into its own phase rather than compacting-and-suspending in one tick
    because ``/compact`` is asynchronous — suspending immediately would type the
    C-c/exit sequence into a pane that is still summarizing, i.e. pay for the
    squeeze and then destroy it, which is precisely the waste T-0863 removed
    from the old code. ``wrote_state`` is persisted here because the mark it was
    derived from is stale the moment the session wrote.
    """
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now, cfg=cfg,
            slug=slug):
        return False
    try:
        autocompact._send_compact(sid)
    except Exception:
        log.exception("idle_timeout: pre-exit /compact send failed for %s "
                      "(will retry)", sid)
        return False
    meta["idle_recycle_phase"] = PHASE_COMPACT_EXIT
    meta["idle_recycle_armed_at"] = _now_iso()
    meta["idle_recycle_wrote_state"] = "true" if wrote_state else "false"
    meta.pop("idle_recycle_mark", None)
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: sent the pre-exit /compact to %s (wrote_state=%s) "
             "— terminating resumable once it lands (T-0945)", sid, wrote_state)
    return True


def _finalize_compact_exit(cfg: Any, slug: str, sid: str, meta: dict, md_path,
                           now: float, pane: str | None) -> bool:
    """T-0945 PHASE 3: the pre-exit ``/compact`` has been sent; terminate once
    the pane comes back composer-ready (or the bounded wait lapses — never
    wedge, same posture as every other finalize in this module).

    Terminating on the timeout path is safe in a way the compact-and-stay
    timeout is not: this session is ENDING either way, so a compact that never
    finished costs the squeeze, not the session — and the forward-state was
    already written in phase 1."""
    armed_at = sessions._parse_ts_epoch(meta.get("idle_recycle_armed_at")) or now
    timed_out = (now - armed_at) > autocompact.handoff_timeout_sec()

    if not pane:
        # Session already gone — nothing left to finalize; drop the stamp.
        _clear_recycle_state(meta)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return False

    ready = autocompact.composer_ready(autocompact._capture_pane(pane),
                                       sid=sid, now=now)
    if not ready and not timed_out:
        return False  # still compacting — retry next tick
    if not ready:
        log.warning("idle_timeout: pre-exit /compact for %s never came back "
                    "composer-ready within the window — terminating anyway "
                    "(never wedge)", sid)

    wrote_state = str(meta.get("idle_recycle_wrote_state", "")).strip().lower() \
        in ("true", "1", "yes")
    return _terminate_and_remember(cfg, slug, sid, meta, md_path, now,
                                   wrote_state=wrote_state, compacted=ready)


def _terminate_and_remember(cfg: Any, slug: str, sid: str, meta: dict, md_path, now: float,
                            *, wrote_state: bool, compacted: bool = False) -> bool:
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

    T-0945 WITHDRAWS T-0655's ``self_terminate``. That flag made a drive=off
    operator exit with NO ``resumable``/``resume_hint`` — a deliberate stop
    («лучше самозавершиться»), no resume bait, ``operator_redrive`` spawning a
    fresh operator instead. The stakeholder's 2026-08-31 ruling gives the
    operator the user-conversation contract instead — *«С ролью operator — то
    же самое»*, whose second half is *«и потом всегда resume»* — because the
    two roles now flow into one another (T-0943) and cannot hold opposite exit
    contracts. Every exit from this module is therefore resumable again. The
    churn it was guarding against does not follow: nothing auto-resumes an
    operator, and ``operator_redrive`` still spawns fresh when backlog work
    lands; the stamp is a marker for whoever looks, not a trigger.

    T-0945 ``compacted``: True when the pre-exit ``/compact`` actually landed
    (:func:`_finalize_compact_exit`). It says what a resume will FIND — a
    squeezed transcript or the full one — which is a different fact from
    ``wrote_state`` (what a fresh reader will find on the ticket), and both
    are things a later reader has no other way to recover."""
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
    where = (f"{task_id}'s ## Context" if task_id and task_id != "~"
             else "its role artifact")
    fresh["resumable"] = True
    fresh["resume_hint"] = (
        f"idle cache-window recycle "
        f"({'forward-state written to ' + where if wrote_state else 'no forward-state written'}"
        f"{'; context compacted before exit' if compacted else ''})"
        f" — resume via sessions.resume to continue {task_id or role or sid}.")
    if compacted:
        fresh["recycled_compacted"] = True
    # T-0930: a WAITING recycle — the bound task is blocked_on_user, so
    # this is not a generic "somebody should look at this eventually"
    # resumable, it is a specific condition the system itself can watch
    # for and act on (wait_resume.tick). Stamped in ADDITION to the
    # generic resumable/resume_hint above (for a human glancing at the
    # board), not instead of them.
    if task_id and task_id != "~":
        task_status = recovery.read_task_status(cfg, slug, task_id)
        if task_status in recovery.WAITING_STATUSES:
            fresh["wait_reason"] = task_status
            fresh["wait_task_id"] = task_id
            fresh["resume_hint"] = (
                f"WAITING on {task_id} ({task_status}) — auto-resumes via "
                f"wait_resume.tick when the block lifts; do not resume "
                f"manually unless it has actually cleared.")
    sessions._write_session_metadata(md_path, fresh, atomic=True)

    # T-0470: a cache-window recycle finalized → record it on the unified surface.
    lifecycle_events.emit(cfg, slug, sid, lifecycle_events.SESSION_RECYCLED,
                          now=now, cause="idle_timeout", wrote_state=wrote_state,
                          compacted=compacted)
    log.info("idle_timeout: recycled %s (wrote_state=%s, compacted=%s) — "
             "recorded resumable state", sid, wrote_state, compacted)
    return True


# --- T-0945: the user-conversation / operator EXIT line ----------------------
#
# T-0930 put this at a separate ~3 h number («exited in 3 hours maybe»). T-0945
# withdraws that: *«с ролью user-conversation всегда имеет смысл по таймауту 55
# мин делать handoff + compact + exit, и потом всегда resume»*. So the exit line
# IS the ordinary cache window and there is no second constant to keep in sync —
# the env knob survives only as a live-install override / kill switch.


def uc_exit_sec() -> int:
    """Idle seconds after which a ``compact_exit`` role (user-conversation, or
    a drive=off operator) terminates — :func:`idle_timeout_sec` by default, i.e.
    the same 55 min window every other plan fires on.

    ``BOT_SQUAD_UC_EXIT_SEC=0`` disables the exit for user-conversation, which
    downgrades it to plain compact-and-stay (:data:`PLAN_STAY`, the
    pre-T-0945 behaviour). The drive=off operator does NOT get the same
    downgrade — it still terminates via :data:`PLAN_HANDOFF_EXIT` (its own
    pre-T-0945 behaviour was a plain handoff exit, uncompacted), just without
    the compact step this knob would otherwise add. This is the kill switch
    for the ruling as it applies to each role; garbage falls back to the
    default."""
    raw = os.environ.get("BOT_SQUAD_UC_EXIT_SEC")
    if raw is not None and raw.strip() != "":
        try:
            v = int(raw)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
    return idle_timeout_sec()


def handoff_first_enabled() -> bool:
    """T-0954 kill switch for «всегда handoff + ready for compact и только потом
    compact». ``BOT_SQUAD_COMPACT_HANDOFF_FIRST=0`` restores the pre-T-0954 bare
    ``/compact`` on the compact-and-stay paths."""
    return os.environ.get("BOT_SQUAD_COMPACT_HANDOFF_FIRST", "1") != "0"


def typing_warnings_enabled() -> bool:
    """T-0954 kill switch for the mid-typing warnings.
    ``BOT_SQUAD_TYPING_WARNINGS=0`` goes back to deferring in silence."""
    return os.environ.get("BOT_SQUAD_TYPING_WARNINGS", "1") != "0"


# --- T-0954: warn him instead of waiting in silence ------------------------

#: How close to the cache edge the warning fires. His number: «warning: через 5
#: минут, сессия выйдет из кеша».
WARN_BEFORE_CACHE_SEC = 300


def _send_typing_warning(sid: str, text: str) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid, "text": text})


def cache_warning_due(idle_age: float | None, now_window: int) -> bool:
    """True when the prompt cache is within :data:`WARN_BEFORE_CACHE_SEC` of the
    recycle window — i.e. the point at which saying something is still useful."""
    if idle_age is None:
        return False
    return idle_age >= max(0, now_window - WARN_BEFORE_CACHE_SEC)


def _maybe_warn_while_typing(cfg: Any, slug: str, sid: str, row: dict, meta: dict,
                             md_path, now: float, pane: str | None,
                             user_home: str) -> bool:
    """T-0954: when he is mid-sentence and the deadline is coming, SAY SO.

    His alternative to waiting mutely, verbatim:

        «Либо же, если я пишу, он должен вставлять enter и warning: скоро
        закончится контекст, надо запускать handoff/compact цикл или warning:
        через 5 минут, сессия выйдет из кеша (но не на stale сессию очевидно)»

    Three things this does NOT do, each because of a word in that sentence:

    * it never fires at a `stale` composer — «но не на stale сессию очевидно».
      Nobody is typing there, so there is nobody to warn; that pane gets the
      compact instead.
    * it never submits HIS text. The Enter belongs to the warning: the delivery
      path (``input_mux``) puts the message ahead of his draft and puts the
      draft back, which is the same transport his `check mail` complaint asked
      for. Deciding it this way is recorded on T-0954.
    * it never repeats inside one cache window — one warning is information, a
      warning every 60s is noise he would learn to ignore.
    """
    from bot_squad_worker import composer_watch

    if not pane or not typing_warnings_enabled():
        return False
    if not compact_stay_due(meta.get("composer_warned_at"), now,
                            idle_timeout_sec()):
        return False

    # Decide whether there is anything to warn ABOUT before capturing the pane:
    # this runs for every active session on every 60s tick, and a tmux capture
    # per session per tick to discover "no deadline is near" is a cost with no
    # answer attached.
    idle_age = _idle_age(row, meta, user_home, now)
    tokens = _context_tokens(cfg, slug, sid)
    ceiling = compact_min_context_tokens(cfg)
    if not (cache_warning_due(idle_age, idle_timeout_sec()) or tokens > ceiling):
        return False

    obs = composer_watch.observe(cfg, slug, sid,
                                 autocompact._capture_pane(pane), now)
    if obs["state"] != composer_watch.STATE_TYPING:
        return False
    if obs.get("first_sighting"):
        # We have seen this text exactly once, so "typing" is an assumption, not
        # an observation — and on the first tick after a restart it is the wrong
        # one for every parked composer at once (measured: four sessions warned
        # in eight seconds, none of them being typed in). Say nothing until the
        # NEXT tick, which costs 60s and buys the difference between a person
        # and a leftover.
        return False

    if cache_warning_due(idle_age, idle_timeout_sec()):
        left = max(0, int(idle_timeout_sec() - (idle_age or 0)) // 60)
        text = (f"⚠ через ~{left} мин эта сессия выйдет из кеша. Допечатывай "
                f"спокойно — как только отправишь, я запущу цикл "
                f"handoff → compact; твой текст в поле ввода не трогаю.")
    elif tokens > ceiling:
        text = (f"⚠ скоро закончится контекст ({tokens} токенов) — надо "
                f"запускать handoff/compact цикл. Допечатывай, я жду; твой "
                f"текст в поле ввода не трогаю.")
    else:
        return False

    try:
        _send_typing_warning(sid, text)
    except Exception:
        log.exception("idle_timeout: typing warning send failed for %s", sid)
        return False
    meta["composer_warned_at"] = _now_iso()
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: warned %s while he is typing (%s)", sid,
             composer_watch.describe(obs))
    return True


# --- T-0617: compact-and-stay (exempt user sessions) ------------------------

def _maybe_compact_and_stay(cfg: Any, slug: str, sid: str, row: dict, meta: dict,
                            md_path, now: float, pane: str | None,
                            user_home: str, *, role: str | None = None) -> bool:
    """T-0617: the exempt-session counterpart to :func:`_start_recycle` /
    :func:`_finalize_compact` — same idle-window trigger and context-threshold
    gate, but FINALIZE never terminates.

    T-0954 turns it into the THREE-phase sequence the stakeholder ruled every
    compact must follow — **handoff -> ready-for-compact -> compact**:

    1. :data:`PHASE_STAY_HANDOFF` — ask the session to write its forward-state
       where that state lives (its ticket's ``## Context``, or its role
       artifact), exactly as :func:`_start_recycle` does for the exiting plans.
    2. :data:`PHASE_STAY_READY` — the write landed. Stamped before the
       ``/compact`` leaves, so a squeeze that skipped the handoff is a state the
       md cannot be in.
    3. ``compacting`` — the ``/compact`` is in flight;
       :func:`_finalize_compact_stay` clears it. Never terminates.

    Before T-0954 this path sent a bare ``/compact``: the session kept running
    on a summarized transcript with nothing written down, so anything the
    summary dropped was simply gone — «не должно происходить просто compact».
    """
    phase = meta.get("compact_stay_phase")
    if phase == "compacting":
        return _finalize_compact_stay(sid, meta, md_path, now, pane)
    if phase in (PHASE_STAY_HANDOFF, PHASE_STAY_READY):
        return _finalize_stay_handoff(cfg, slug, sid, meta, md_path, now, pane,
                                      role=role)

    idle_age = _idle_age(row, meta, user_home, now)
    if not idle_due(idle_age, idle_timeout_sec()):
        return False
    if not compact_stay_due(meta.get("compact_stay_last_at"), now, idle_timeout_sec()):
        return False  # already compacted-and-stayed once this cache window
    if tracking_long_job(cfg, slug, sid):
        log.info("idle_timeout: compact-and-stay auto-postpone %s — waiting "
                 "on a tracked long job", sid)
        return False
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now, cfg=cfg,
            slug=slug):
        return False

    tokens = _context_tokens(cfg, slug, sid)
    threshold = compact_min_context_tokens(cfg)
    if tokens <= threshold:
        # Nothing worth compacting yet — leave compact_stay_last_at alone so
        # this is re-checked (cheaply) on every later tick, not just once per
        # window, until there's actually context worth clearing.
        return False

    if not handoff_first_enabled():
        # Kill switch: the pre-T-0954 bare /compact, kept reachable ONLY here.
        return _send_bare_compact_stay(sid, meta, md_path, tokens, threshold)

    # T-0954 PHASE 1: the handoff, which is now a PRECONDITION of the squeeze.
    target = autocompact._resolve_compact_target(cfg, slug, {
        "sid": sid, "role": role or meta.get("role") or "",
        "task_id": meta.get("task_id"), "window": meta.get("window"),
    })
    if target["kind"] == "none":
        # No destination -> no handoff -> no compact. Deliberately NOT a bare
        # /compact: the session stays on a summarized transcript, so whatever
        # the summary drops is lost with nothing written down anywhere. Said
        # once per window (the anti-loop stamp is left alone on purpose, so the
        # moment a destination appears the normal sequence runs).
        log.warning("idle_timeout: %s is over the context threshold (%d > %d) "
                    "but has nowhere to hand off to — NOT compacting "
                    "(T-0954: never a bare compact)", sid, tokens, threshold)
        return False

    try:
        if target["kind"] == "context":
            autocompact._inject_context_handoff(sid, target["task_id"],
                                                relaunch=False, resume=True)
        else:
            autocompact._inject_handoff(sid, target["artifact_path"],
                                        target["role"], relaunch=False,
                                        resume=True)
    except Exception:
        log.exception("idle_timeout: compact-and-stay handoff inject failed "
                      "for %s (will retry)", sid)
        return False
    meta["compact_stay_phase"] = PHASE_STAY_HANDOFF
    meta["compact_stay_armed_at"] = _now_iso()
    meta["compact_stay_mark"] = autocompact.handoff_mark(target)
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: compact-and-stay asked %s to hand off into %s "
             "(%d tokens > %d threshold) — awaiting the write, then /compact",
             sid,
             f"{target['task_id']} ## Context" if target["kind"] == "context"
             else target["artifact_path"], tokens, threshold)
    return True


def _send_bare_compact_stay(sid: str, meta: dict, md_path, tokens: int,
                            threshold: int) -> bool:
    """The pre-T-0954 behaviour, reachable only via
    :func:`handoff_first_enabled`'s kill switch: ``/compact`` with no handoff."""
    try:
        autocompact._send_compact(sid)
    except Exception:
        log.exception("idle_timeout: compact-and-stay /compact send failed "
                      "for %s (will retry)", sid)
        return False
    meta["compact_stay_phase"] = "compacting"
    meta["compact_stay_armed_at"] = _now_iso()
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: compact-and-stay sent a BARE /compact to %s (%d > "
             "%d) — BOT_SQUAD_COMPACT_HANDOFF_FIRST=0", sid, tokens, threshold)
    return True


def _finalize_stay_handoff(cfg: Any, slug: str, sid: str, meta: dict, md_path,
                           now: float, pane: str | None, *,
                           role: str | None = None) -> bool:
    """T-0954 PHASES 2-3: the write landed -> ready-for-compact -> ``/compact``.

    The "did it write" test is :func:`autocompact.handoff_mark` against the
    ARM-time mark, the same one :func:`_finalize_compact` uses, so both
    sequences agree about what a completed handoff is.

    Never wedges: past :func:`autocompact.handoff_timeout_sec` the wait is
    abandoned. Abandoned means abandoned — the compact does NOT then fire on its
    own, because a squeeze whose handoff never happened is the exact thing this
    ticket removed. The window's anti-loop stamp is set so the next attempt
    starts a clean sequence rather than re-arming every tick.
    """
    armed_at = sessions._parse_ts_epoch(meta.get("compact_stay_armed_at")) or now
    timed_out = (now - armed_at) > autocompact.handoff_timeout_sec()

    if not pane:
        _clear_compact_stay_state(meta)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return False

    target = autocompact._resolve_compact_target(cfg, slug, {
        "sid": sid, "role": role or meta.get("role") or "",
        "task_id": meta.get("task_id"), "window": meta.get("window"),
    })
    mark = autocompact.handoff_mark(target)
    wrote_state = bool(mark) and mark != meta.get("compact_stay_mark")
    # `composer_free`, not `composer_ready`: he may have started typing between
    # the handoff and now, and «он должен ждать, если я что-то пишу в окне»
    # applies to the second half of the sequence exactly as much as the first.
    # The draft-preserving transport would keep his text either way — but the
    # rule is that we WAIT, not that we can safely interrupt him.
    ready = autocompact.composer_free(autocompact._capture_pane(pane),
                                      sid=sid, now=now, cfg=cfg, slug=slug)

    if not (wrote_state and ready):
        if not timed_out:
            return False  # still writing — retry next tick
        log.warning("idle_timeout: %s never completed its compact-and-stay "
                    "handoff (wrote_state=%s, composer_free=%s) — abandoning "
                    "the sequence WITHOUT compacting (T-0954)", sid,
                    wrote_state, ready)
        _clear_compact_stay_state(meta)
        meta["compact_stay_last_at"] = _now_iso()
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return True

    # PHASE 2 -> the state the /compact is allowed to leave from, stamped
    # BEFORE it is sent so the md can never show a compact with no handoff.
    meta["compact_stay_phase"] = PHASE_STAY_READY
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: %s wrote its forward-state — ready for compact", sid)

    try:
        autocompact._send_compact(sid)
    except Exception:
        log.exception("idle_timeout: compact-and-stay /compact send failed "
                      "for %s (will retry)", sid)
        return False
    meta["compact_stay_phase"] = "compacting"
    meta["compact_stay_armed_at"] = _now_iso()
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: compact-and-stay sent /compact to %s — session "
             "stays, no terminate", sid)
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
    """T-0655/T-0930: the drive=on-operator counterpart to
    :func:`_maybe_compact_and_stay` — shares the postpone/tracked-job gates
    and composer-ready gate, but fires on its OWN :func:`operator_nudge_sec`
    cadence (40 min default), decoupled from :func:`idle_timeout_sec`'s
    cache-window recycle trigger, and instead of ``/compact`` it injects a
    plain-text keep-alive nudge and NEVER terminates.
    ``operator_keepalive_last_at`` bounds it to at most once per nudge cadence
    (:func:`keepalive_due`)."""
    idle_age = _idle_age(row, meta, user_home, now)
    if not idle_due(idle_age, operator_nudge_sec()):
        return False
    if not keepalive_due(meta.get("operator_keepalive_last_at"), now, operator_nudge_sec()):
        return False  # already nudged once this cadence window
    if tracking_long_job(cfg, slug, sid):
        log.info("idle_timeout: keepalive auto-postpone %s — waiting on a "
                 "tracked long job", sid)
        return False
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now, cfg=cfg,
            slug=slug):
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


# --- T-0930/T-0945: worker drive-unmet nudge (dev 5min, TL 40min) ----------
#
# T-0945 widened this from dev-only to every :data:`WORKER_ROLES` session, and
# from the primary binding to ALL of them — «Для ролей TL и dev, если они не
# совмещены с какой-то из других, логично продолжать только пока какая-то из их
# задач жива». The alive-check itself lives in :func:`worker_tasks_alive` /
# :func:`task_alive` (pure-ish, called from :func:`recycle_plan`'s caller); what
# is left here is only the cadence and the injection.


def _worker_nudge_text(role: str | None) -> str:
    base = ("continue — продолжай. Your ~1h cache window is idling toward "
            "expiry and the system is nudging you instead of recycling you, "
            "because you still hold live work.")
    if (role or "") == "teamlead":
        return base + (
            " At least one task in your scope is still open (not to_accept/"
            "totest/closed/blocked_on_user). If you are waiting on a dev, "
            "check its state (`bsq team status`) rather than sitting; if the "
            "wait is on the stakeholder, move the ticket to blocked_on_user "
            "— that stops this nudge and lets the system handle the wait "
            "properly.")
    return base + (
        " Your bound task is not yet in to_accept/totest/closed. If you are "
        "genuinely blocked on the stakeholder, set the task to "
        "blocked_on_user instead of sitting idle (`bsq ticket update <id> "
        "blocked_on_user`) — that stops this nudge and lets the system "
        "handle the wait properly.")


def _send_dev_nudge(sid: str, text: str) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid, "text": text})


def _maybe_worker_nudge(cfg: Any, slug: str, sid: str, row: dict, meta: dict,
                        md_path, now: float, pane: str | None,
                        user_home: str, *, role: str | None = None) -> bool:
    """The dev/TL counterpart to :func:`_maybe_keepalive_nudge` — same shape
    (postpone/tracked-job/composer-ready gates, once-per-cadence anti-loop
    guard) but on :func:`worker_nudge_sec`'s role-dependent cadence, and it
    never terminates. ``dev_nudge_last_at`` bounds it to at most once per
    cadence window, mirroring :func:`keepalive_due`; the field keeps its
    T-0930 name (it is a per-session stamp, and renaming it would strand the
    guard on every live session md mid-flight)."""
    cadence = worker_nudge_sec(role)
    idle_age = _idle_age(row, meta, user_home, now)
    if not idle_due(idle_age, cadence):
        return False
    if not keepalive_due(meta.get("dev_nudge_last_at"), now, cadence):
        return False  # already nudged once this cadence window
    if tracking_long_job(cfg, slug, sid):
        # "если он не ждет build или что-то еще может его разбудить" — the
        # SAME tracked-job auto-postpone the terminate path already uses.
        log.info("idle_timeout: worker nudge auto-postpone %s — waiting on a "
                 "tracked long job", sid)
        return False
    if not pane or not autocompact.composer_free(
            autocompact._capture_pane(pane), sid=sid, now=now, cfg=cfg,
            slug=slug):
        return False

    try:
        _send_dev_nudge(sid, _worker_nudge_text(role))
    except Exception:
        log.exception("idle_timeout: worker nudge send failed for %s "
                      "(will retry)", sid)
        return False
    meta["dev_nudge_last_at"] = _now_iso()
    sessions._write_session_metadata(md_path, meta, atomic=True)
    lifecycle_events.emit(cfg, slug, sid, lifecycle_events.SESSION_TIMEOUT,
                          now=now, reason="idle_window_dev_nudge")
    log.info("idle_timeout: sent drive-unmet nudge to %s %s (cadence %ds) — "
             "session stays, no recycle", role or "worker", sid, cadence)
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
