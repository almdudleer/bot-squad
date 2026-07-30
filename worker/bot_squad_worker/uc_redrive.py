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
:func:`dispatch.live_operator_sids`, and this module treats an escalation that
reached NOBODY as not-yet-escalated: it retries on each following ping slot
until a live operator sid actually receives it, rather than spending its one
alert on an empty fan-out.

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
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
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
    cfg: Any, slug: str, gid: str, *, pings: int, hanging_sec: float,
) -> list[str]:
    """Escalate one hanging message to a LIVE operator. Returns the sids it
    actually reached — ``[]`` when nobody did.

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
            text=(f"⚠️ uc_redrive: {gid} has an UNANSWERED user message — "
                  f"hanging {mins} min, {pings} re-wake ping(s) sent and the "
                  f"attendant still has not replied. Needs a human look."),
            from_sid="S-uc-redrive",
        )
    except Exception:  # noqa: BLE001 — best-effort notify must never break the tick
        log.exception("uc_redrive: stuck-notify failed for %s/%s", slug, gid)
        return []
    delivered = [str(s) for s in (out or {}).get("delivered_to") or []]
    if not delivered:
        log.warning(
            "uc_redrive: escalation for %s/%s reached NO live operator — the "
            "message has hung %d min over %d ping(s); will retry the escalation "
            "on the next ping slot", slug, gid, mins, pings,
        )
    return delivered


def check_project(cfg: Any, slug: str, *, now: float | None = None) -> dict:
    """One unanswered-message sweep for a project. Returns a summary dict.

    Idempotent + side-effecting: fires at most one nudge per (slug, gid) per
    ping slot (:func:`ping_due_after_sec`), for as long as the message hangs.
    """
    from bot_squad_worker import sessions as S
    from bot_squad_worker import detector as _detector
    from bot_squad_worker import actions as A

    now = now if now is not None else time.time()
    conv_dir = _conversations_dir(cfg, slug)
    if not conv_dir.exists():
        return {"ok": True, "redriven": []}

    pressure = _detector.session_pressure(cfg)
    pressured_sids = (set(pressure.get("rate_limited_sids", []))
                       | set(pressure.get("limit_blocked_sids", [])))

    try:
        rows = {r["sid"]: r for r in S.list_sessions(cfg, slug)}
    except Exception:
        log.exception("uc_redrive: list_sessions failed for %s", slug)
        rows = {}

    state = _load_state(cfg, slug)
    state_changed = False
    redriven: list[dict] = []

    for jf in sorted(conv_dir.glob("*.jsonl")):
        gid = jf.stem
        rec = _unanswered_user_record(jf)
        if rec is None:
            # Answered (or empty) thread — nothing to re-drive. Drop any
            # stale ping state so a LATER stuck message starts fresh.
            if gid in state:
                del state[gid]
                state_changed = True
            continue

        msg_ts = str(rec.get("timestamp") or "")
        msg_at = _parse_iso(msg_ts)
        if msg_at is None:
            continue  # undatable record — the cadence has nothing to count from

        gid_state = state.get(gid) or {}
        if gid_state.get("msg_ts") != msg_ts:
            gid_state = {"msg_ts": msg_ts, "pings": 0, "last_ping_at": 0,
                         "escalated_to": []}
        pings = int(gid_state.get("pings") or 0)
        due = pings_due_by(now - msg_at)
        if due <= pings:
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
            log.exception("uc_redrive: redrive dispatch failed for %s/%s", slug, gid)
            continue

        pings = due  # slots that came due while it was busy are spent, not owed
        gid_state["pings"] = pings
        gid_state["last_ping_at"] = now
        state[gid] = gid_state
        state_changed = True
        redriven.append({"gid": gid, "sid": sid, "pings": pings})
        log.warning("uc_redrive: re-woke idle attendant %s for %s/%s (ping %d, "
                    "unanswered %ds)", sid, slug, gid, pings, int(now - msg_at))

        # Escalate once the ramp is spent — and again on every later slot until
        # a live operator actually receives it (see _notify_operator_stuck).
        if pings >= ESCALATE_AFTER_PINGS and not gid_state.get("escalated_to"):
            gid_state["escalated_to"] = _notify_operator_stuck(
                cfg, slug, gid, pings=pings, hanging_sec=now - msg_at,
            )

    if state_changed:
        _save_state(cfg, slug, state)
    return {"ok": True, "redriven": redriven}


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
