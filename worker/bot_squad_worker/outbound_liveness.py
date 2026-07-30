"""Is the outbound log still recording? (T-0759)

The failure this detects
------------------------
T-0755 exists because a recording path did not vanish — it *fell out of use*
after 2026-07-18 and nobody noticed for over a month. A fix that can decay the
same way, undetected, has not removed that failure mode; it has reset its clock.

Since T-0746 the stake is no longer only observability. ``echo_guard`` rung 2
reads the outbound spool to answer "did we send these exact bytes to this chat
within 7 days", and that is *the only rung* that catches a copy-paste or a
hide-sender forward — the ones carrying no metadata at all. So a silent decay
does not merely leave us unable to audit: our own text starts being recorded as
the stakeholder's words again, with no failing test and nothing announcing it.

The discriminator
-----------------
"Nothing was sent" and "things were sent and not recorded" look identical from
the log alone, and conflating them is the whole defect. They are told apart by
asking a source the outbound log does not write:

* ``tg_reply_map.json`` — ``message_id -> sid``, pinned by ``tg.send`` for
  every session-routed send (T-0719).
* ``tg_debounce/`` / ``max_debounce/`` — one touched marker per delivered
  ``(chat, sid, text)``, whose mtime is the send time (T-0513).

These are WITNESSES: they are written for their own unrelated reasons (reply
routing, cooldown), by different code, into different files, and a change that
stops the outbound log recording does not stop them. That is the comparison
operator p298 ran by hand at 12:18 on 2026-07-27 to establish an empty spool was
expected rather than broken; this module is that comparison automated, and
nothing more. No new signal is invented and no second record is written —
``outbound_log.spool_health`` already exposes every quantity needed.

The correction that makes it usable
-----------------------------------
The obvious form of the comparison — "a witness newer than the newest spool
record means decay" — is WRONG, and measurably so. On the live install at the
time of writing the newest debounce witness was 19:48:55Z against a newest
spool record of 18:35:40Z. That 73-minute gap was healthy: the 19:48:55 send was
``task_chat``'s lifecycle notice, which passes ``record_outbound=False`` because
it records the same line in the same thread itself. So the transport now
DECLARES those deliberate opt-outs (``outbound_log.note_unspooled`` — an mtime,
not a record), and the comparison is against sends ACCOUNTED FOR rather than
sends spooled. Shipped without that, this module's first act on a healthy
install would have been a false alarm.

BLIND is a state, not a zero (the positive control)
---------------------------------------------------
``read_spool``/``spool_dir`` take the DATA dir and append ``_worker/outbound``
themselves, so a caller passing ``…/data/_worker`` gets
``…/data/_worker/_worker/outbound``, which does not exist — and the call returns
**zero records, no exception, well-formed**. A monitor built on that argument
would report "no sends" forever, and would do it most convincingly during a real
outage: the ticket's own failure mode, hiding inside the detector. So every
verdict carries its ``scan`` — the files and records the read actually saw — and
a zero there yields :data:`BLIND` rather than any judgement about sends. The
shape is p330's T-0740 watcher: an explicit ``BLIND`` / ``BLIND-CLEARED``
transition that says in words that zero is not evidence of absence until it
clears, rather than a quiet zero that reads as clean.

What would make this say no
---------------------------
A liveness check that cannot report "dead" is decoration. This one returns
:data:`DECAYED` when a witness proves a send that no spool line and no declared
opt-out accounts for, and :data:`BLIND` when its own read is unproven — both
reachable by forcing the inputs, which is how they are tested (``touch`` a
debounce marker, or point the check at the wrong directory) rather than by
waiting for a genuine decay.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Everything agrees: sends are witnessed AND accounted for.
OK = "ok"
#: No evidence of any send recently, from any source. NOT an error, and
#: deliberately not ``OK`` — "quiet" and "working" are different claims.
IDLE = "idle"
#: A witness proves a send that nothing accounts for. THE alarm.
DECAYED = "decayed"
#: The check's own read is not proven live, so no verdict is licensed.
BLIND = "blind"

#: How far a witness may outrun the newest accounted-for send before that
#: counts as decay. Generous on purpose: it must clear the drain cadence (30s),
#: a clock skew between an mtime and an ISO stamp, and a burst of sends
#: straddling the read — while staying far under the hours a human would take
#: to notice. Override with ``BOT_SQUAD_OUTBOUND_DECAY_LAG_S``.
DECAY_LAG_S = 900

#: Below this much evidence of recent life, the answer is :data:`IDLE` rather
#: than :data:`OK`: nothing has been sent by ANY measure inside this window, so
#: there is nothing for the recording to have got right.
ACTIVITY_WINDOW_S = 6 * 3600


def _int_env(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def decay_lag_s() -> int:
    return _int_env("BOT_SQUAD_OUTBOUND_DECAY_LAG_S", DECAY_LAG_S)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> float:
    """ISO ``…Z`` -> epoch seconds, or ``0.0``. Never raises: a torn timestamp
    must degrade to "no evidence", not to a traceback inside a health check."""
    s = str(iso or "").strip()
    if not s:
        return 0.0
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# The witnesses — evidence a send happened, from files this log does not write
# ---------------------------------------------------------------------------

def _newest_mtime(d: Path) -> tuple[int, float]:
    """``(file_count, newest_mtime)`` for a marker directory. ``(0, 0.0)`` when
    it is absent — which the caller must treat as "no witness available", never
    as "no sends happened"."""
    count = 0
    newest = 0.0
    try:
        with os.scandir(d) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                count += 1
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        return 0, 0.0
    return count, newest


def _reply_map_witness(data_dir: Path) -> dict:
    """``tg_reply_map.json`` read as evidence of sends, not as a route table.

    Read directly rather than through ``tg_reply_map.load``: this is a witness,
    and a witness that shares its reader with the thing it is checking is worth
    less. It also must not take that module's lock — a health check may never
    make a send wait.
    """
    path = data_dir / "_worker" / "tg_reply_map.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "entries": 0, "newest": 0.0}
    if not isinstance(raw, dict):
        return {"available": False, "entries": 0, "newest": 0.0}
    newest = 0.0
    for entry in raw.values():
        if isinstance(entry, dict):
            try:
                newest = max(newest, float(entry.get("ts") or 0))
            except (TypeError, ValueError):
                continue
    return {"available": True, "entries": len(raw), "newest": newest}


def witnesses(data_dir: Any) -> dict:
    """Independent evidence that sends HAPPENED.

    ``available`` per source is the positive control for the witness half: a
    missing debounce dir yields the same zero as a genuinely quiet one, and
    conflating those is how a detector reports "no sends" from an install it
    cannot see.
    """
    d = Path(data_dir)
    reply_map = _reply_map_witness(d)
    tg_files, tg_newest = _newest_mtime(d / "_worker" / "tg_debounce")
    max_files, max_newest = _newest_mtime(d / "_worker" / "max_debounce")
    newest = max(reply_map["newest"], tg_newest, max_newest)
    return {
        "reply_map": reply_map,
        "tg_debounce": {"available": (d / "_worker" / "tg_debounce").is_dir(),
                        "files": tg_files, "newest": tg_newest},
        "max_debounce": {"available": (d / "_worker" / "max_debounce").is_dir(),
                         "files": max_files, "newest": max_newest},
        "newest": newest,
        "newest_iso": _iso(newest) if newest else "",
        "any_available": bool(reply_map["available"]
                              or (d / "_worker" / "tg_debounce").is_dir()
                              or (d / "_worker" / "max_debounce").is_dir()),
        "evidence": reply_map["entries"] + tg_files + max_files,
    }


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def check(cfg: Any, *, now: float | None = None) -> dict:
    """Is the outbound log still recording what we send?

    Returns ``{state, reason, scan, witness, last_accounted_ts, last_send_ts,
    lag_s, health}``. ``state`` is one of :data:`OK` / :data:`IDLE` /
    :data:`DECAYED` / :data:`BLIND`.

    ``scan`` is reported on EVERY verdict, healthy ones included — it is what
    licenses believing the numbers (see the module docstring's positive
    control), so it must never be something a caller has to ask for separately.

    Never raises: a health check that can crash the tick it runs in is one more
    way for the silence to come back. A failure is reported as :data:`BLIND`,
    which is what it is.
    """
    from bot_squad_worker import outbound_log

    now = time.time() if now is None else float(now)
    try:
        data_dir = Path(cfg.data_dir)
        health = outbound_log.spool_health(data_dir)
        wit = witnesses(data_dir)
    except Exception:  # noqa: BLE001 — an unreadable install is BLIND, not OK
        log.exception("outbound_liveness: check failed")
        return {"state": BLIND, "reason": "check raised — see traceback",
                "scan": {}, "witness": {}, "last_accounted_ts": "",
                "last_send_ts": "", "lag_s": 0, "health": {}}

    scan = {
        "spool_dir": health.get("dir", ""),
        "spool_dir_exists": bool(health.get("exists")),
        "spool_files": int(health.get("files") or 0),
        "spool_records": int(health.get("records") or 0),
        "reply_map_entries": int(wit["reply_map"]["entries"]),
        "debounce_files": int(wit["tg_debounce"]["files"] + wit["max_debounce"]["files"]),
        "witness_sources": [k for k in ("reply_map", "tg_debounce", "max_debounce")
                            if wit[k]["available"]],
    }

    # Last send this system can ACCOUNT FOR: a spool line, or a declared
    # opt-out. The opt-out half is what keeps a healthy lifecycle notice from
    # reading as 73 minutes of decay — see note_unspooled.
    last_record = _epoch(health.get("last_record_ts", ""))
    last_optout = _epoch(health.get("last_unspooled_ts", ""))
    last_accounted = max(last_record, last_optout)
    last_send = float(wit["newest"] or 0.0)
    lag = int(last_send - last_accounted) if last_send and last_accounted else 0

    out = {
        "scan": scan,
        "witness": wit,
        "last_accounted_ts": _iso(last_accounted) if last_accounted else "",
        "last_send_ts": wit["newest_iso"],
        "lag_s": max(0, lag),
        "health": health,
    }

    # --- BLIND first: no other verdict is licensed until the read is proven.
    blind: list[str] = []
    if not health.get("exists"):
        blind.append(f"spool dir does not exist: {health.get('dir')}")
    elif not scan["spool_records"]:
        blind.append(
            f"read {scan['spool_files']} file(s) / 0 records from "
            f"{health.get('dir')} — a zero here is what a wrong data_dir "
            f"returns, so it is not evidence of absence")
    if not wit["any_available"]:
        blind.append("no witness source readable (tg_reply_map / tg_debounce / "
                     "max_debounce) — cannot tell 'no sends' from 'not recorded'")
    if blind:
        out["state"] = BLIND
        out["reason"] = "; ".join(blind)
        return out

    # --- DECAYED: a witness proves a send nothing accounts for.
    if last_send and last_send > last_accounted + decay_lag_s():
        out["state"] = DECAYED
        out["reason"] = (
            f"a send was witnessed at {wit['newest_iso']} but the newest "
            f"accounted-for send is {out['last_accounted_ts'] or 'never'} "
            f"({out['lag_s']}s behind) — sends are happening and NOT being "
            f"recorded; echo_guard rung 2 is degraded")
        return out
    drops = int((health.get("drops") or {}).get("record") or 0)
    if drops:
        out["state"] = DECAYED
        out["reason"] = (f"outbound_log.record dropped {drops} delivered "
                         f"message(s) this process lifetime")
        return out

    # --- IDLE vs OK: quiet is not the same claim as working.
    # Activity by EITHER measure counts. A recorded send with no surviving
    # witness (the debounce markers are the shorter-lived of the two) is still
    # the log doing its job, and calling that "idle" would understate it.
    latest = max(last_send, last_accounted)
    if latest and (now - latest) <= ACTIVITY_WINDOW_S:
        out["state"] = OK
        out["reason"] = (f"{scan['spool_records']} record(s) on disk; newest "
                         f"send {wit['newest_iso'] or out['last_accounted_ts']} "
                         f"is accounted for")
    else:
        out["state"] = IDLE
        out["reason"] = (
            f"no send by any measure in the last {ACTIVITY_WINDOW_S // 3600}h "
            f"(newest witness {wit['newest_iso'] or 'never'}, newest recorded "
            f"{out['last_accounted_ts'] or 'never'}) — the log is readable "
            f"({scan['spool_records']} record(s)) and there is nothing "
            f"outstanding to record")
    return out


# ---------------------------------------------------------------------------
# Announcing it — a silence that does not announce itself is the whole bug
# ---------------------------------------------------------------------------

#: Re-announce an ONGOING decay/blind no more often than this. Transitions are
#: always announced; this only bounds the nag.
REANNOUNCE_S = 12 * 3600

#: An alarm must HOLD this long before it is announced (the ``--persist`` of the
#: house monitor spec — a duration, not a sample count). Two reasons, and the
#: second is the load-bearing one: (1) it damps a single anomalous witness;
#: (2) the alarm page is ITSELF a send, so if the recording is in fact working
#: it lands in the spool and the very next check reads healthy — without a
#: persist window that is a DECAYED page followed by a ✅ a minute later, which
#: trains a reader to ignore both. A real decay does not clear, because the
#: alarm page cannot be recorded by a recorder that has stopped recording.
PERSIST_S = 600

_ALARM_STATES = (DECAYED, BLIND)


def state_path(data_dir: Any) -> Path:
    return Path(data_dir) / "_worker" / "outbound" / ".liveness.json"


def _load_state(data_dir: Any) -> dict:
    try:
        raw = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(data_dir: Any, state: dict) -> None:
    p = state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def decide_announcement(prev: dict, verdict: dict, *, now: float) -> tuple[dict, dict]:
    """Pure: what to say about this verdict, given what was last said.

    Returns ``(next_state, plan)``. ``plan`` is
    ``{announce: bool, kind: <str>, headline: <str>}`` and ``kind`` is
    ``DECAYED`` / ``BLIND`` / ``…-STILL`` / ``CLEARED`` / ``""`` — uppercase
    because these strings are read by a human in a log line and in a page, and
    this is the vocabulary p330's T-0740 watcher already established
    (``BLIND-READ`` / ``BLIND-READ-CLEARED``).

    Kept pure and separate from the tick so the state machine can be tested by
    DRIVING it, rather than by arranging an install that decays.
    """
    state = verdict.get("state") or BLIND
    was = str(prev.get("state") or "")
    since = float(prev.get("since") or 0) if state == was else now
    if not since:
        since = now
    nxt = {"state": state, "since": since,
           "announced_state": str(prev.get("announced_state") or ""),
           "announced_at": float(prev.get("announced_at") or 0)}
    quiet = {"announce": False, "kind": "", "headline": ""}

    if state in _ALARM_STATES:
        if now - since < PERSIST_S:
            # Held too briefly to be believed yet. Note it and wait — the state
            # file still records the reading, so nothing is hidden from a reader.
            return nxt, quiet
        if nxt["announced_state"] != state:
            kind = "BLIND" if state == BLIND else "DECAYED"
        elif now - nxt["announced_at"] >= REANNOUNCE_S:
            kind = ("BLIND" if state == BLIND else "DECAYED") + "-STILL"
        else:
            return nxt, quiet
        nxt["announced_state"] = state
        nxt["announced_at"] = now
        return nxt, {"announce": True, "kind": kind,
                     "headline": _headline(kind, verdict)}

    if nxt["announced_state"] in _ALARM_STATES:
        # Recovery is announced exactly once, and ONLY after an alarm actually
        # fired — an alarm that never cleared persist never needs a ✅, and the
        # first healthy tick of a fresh install must not page at all.
        nxt["announced_state"] = ""
        nxt["announced_at"] = now
        return nxt, {"announce": True, "kind": "CLEARED",
                     "headline": _headline("CLEARED", verdict)}
    return nxt, quiet


def _headline(kind: str, verdict: dict) -> str:
    """The one line a human reads.

    It always carries ``scan`` — the files and records the read actually saw —
    because that is what licenses believing any number beside it. A verdict
    without its positive control is exactly the quiet zero this module exists
    to stop being mistaken for health.
    """
    scan = verdict.get("scan") or {}
    proof = (f"scan: {scan.get('spool_files', 0)} file(s), "
             f"{scan.get('spool_records', 0)} record(s), "
             f"{scan.get('reply_map_entries', 0)} reply-map, "
             f"{scan.get('debounce_files', 0)} debounce marker(s) "
             f"in {scan.get('spool_dir') or '?'}")
    still = " (still)" if kind.endswith("-STILL") else ""
    if kind.startswith("DECAYED"):
        return f"🔴 OUTBOUND LOG DECAYED{still} — {verdict.get('reason', '')}. {proof}"
    if kind.startswith("BLIND"):
        return (f"🟠 OUTBOUND LOG BLIND{still} — {verdict.get('reason', '')}. "
                f"Zero is NOT evidence of absence until this clears. {proof}")
    return (f"✅ OUTBOUND LOG OK again — state={verdict.get('state')}: "
            f"{verdict.get('reason', '')}. {proof}")


def tick(cfg: Any, *, now: float | None = None, notify: Any = None) -> dict:
    """Run the check and announce any transition. Returns the verdict.

    ``notify`` is injected for tests; production passes ``None`` and gets the
    stakeholder-DM SSOT. The page names NO project on purpose — the outbound
    log is install-wide, and tagging it with whichever slug sorts first would
    name an owner with no more to do with it than any other (the same call
    ``jobs.oauth_refresh`` and ``autoupdate_apply`` make).
    """
    verdict = check(cfg, now=now)
    now = time.time() if now is None else float(now)
    try:
        data_dir = Path(cfg.data_dir)
        state, plan = decide_announcement(_load_state(data_dir), verdict, now=now)
        state.update(reason=verdict.get("reason", ""), at=_iso(now),
                     scan=verdict.get("scan") or {})
        # The reading is persisted whether or not it was announced: a state
        # held below the persist window is still a fact a reader may need.
        _save_state(data_dir, state)
        if plan["announce"]:
            _announce(cfg, plan, verdict, notify=notify)
        verdict["announced"] = plan
    except Exception:  # noqa: BLE001 — never kill the scheduler thread
        log.exception("outbound_liveness.tick: announcement failed")
    return verdict


def _announce(cfg: Any, plan: dict, verdict: dict, *, notify: Any = None) -> None:
    """Say it in the log AND to the human.

    The log line always happens, even if the page fails: this module's entire
    reason for existing is that a failure which only manifests as an absence
    gets missed, and an ERROR line is the one channel that cannot be suppressed
    by a broken transport. The page then rides the same transport it is
    reporting on — which is fine and worth stating plainly, because it is the
    RECORDING that decayed, not the sending; if the sending were broken the
    stakeholder would find out from the missing answers.
    """
    headline = plan["headline"]
    alarm = plan["kind"].startswith(("DECAYED", "BLIND"))
    if alarm:
        log.error("outbound_liveness %s: %s", plan["kind"], headline)
    else:
        log.warning("outbound_liveness %s: %s", plan["kind"], headline)
    try:
        if notify is None:
            from bot_squad_worker.actions import _send_stakeholder_dm as notify
        # A chat to page INTO, and nothing more — deliberately NOT `slug=`.
        # The outbound log is one install-wide file tree; `[bot-squad
        # outbound_log]` would name a project with no more to do with it than
        # any other, which is the call `jobs.oauth_refresh` and
        # `autoupdate_apply` already made. Naming nobody is honest.
        # T-0799 renamed `_slug` -> `chat_slug`: it is no longer discarded. It
        # names which project supplied the CHAT, which is the route map to read
        # — not a claim that this alarm is about that project (see below).
        chat_slug, project = next(iter((getattr(cfg, "projects", {}) or {}).items()),
                                  ("", None))
        notify(
            cfg,
            message=headline,
            sid="outbound_log",
            # An alarm is urgent because the quiet-hours gate DROPS a
            # non-urgent page rather than deferring it, and an alarm about a
            # silence that is itself silently dropped is this ticket's own bug
            # wearing a different hat. The ✅ is not urgent: losing it to quiet
            # hours costs nothing the log line and `.liveness.json` do not keep.
            urgent=alarm,
            tg_chat_id=getattr(project, "tg_chat", "") if project else "",
            # T-0799: two DIFFERENT types out of one call, on the same split the
            # `urgent=alarm` line above already makes — an alarm about the
            # messaging path being broken is URGENT class, the ✅ that it came
            # back is a LOG entry. `route_slug` (not `slug`) for the same reason
            # `slug=` is deliberately absent: this module is install-wide and
            # claims no project, but the chat it pages came from `_slug`, so
            # that is the map to read.
            msg_type="outbound_decayed" if alarm else "outbound_recovered",
            route_slug=chat_slug,
        )
    except Exception:  # noqa: BLE001 — the log line already landed
        log.exception("outbound_liveness: could not page the stakeholder")
