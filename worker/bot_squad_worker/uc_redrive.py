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
(``data/_mothership/conversations/<slug>/<gid>.jsonl``) for one whose newest
record is still ``author: "user"`` (i.e. no session reply followed it). For
each such thread it re-drives via the EXACT mechanism a fresh inbound message
already uses — ``ensure_user_conversation`` (its live-attendant branch is a
best-effort pane nudge) — subject to:

  * a grace period (``BOT_SQUAD_UC_REDRIVE_GRACE_SEC``) so a message that just
    arrived is left to the normal reply flow first;
  * the SAME 429/5h-limit pressure signal the backoff governor consults
    (:func:`detector.session_pressure`) — never redrive INTO a live storm,
    only once THIS attendant's own pressure has cleared;
  * the live attendant being ``idle`` (T-0104 canonical activity enum,
    :func:`sessions._derive_activity`) — a genuinely in-flight reply is never
    interrupted;
  * a per-``(slug, gid)`` cooldown + bounded retry count (state persisted
    under ``_worker/uc_redrive/state.json``, mirroring
    ``operator_redrive``'s state file) — reset whenever the unanswered
    message changes, so a permanently stuck thread pages the operator once
    its retries are exhausted instead of nagging forever.

Deliberately out of scope: a thread with NO live attendant at all (a fully
dead/never-spawned pane, not merely idle) is left to the existing "next
inbound message spawns/resumes" flow — the DoD's trigger condition is
specifically an IDLE attendant, mirroring the observed incident (the pane
survived the 429; only its reply turn died).

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


def grace_sec() -> int:
    """How long an unanswered message must sit before we consider redriving —
    gives the normal reply flow a chance first."""
    try:
        return int(os.environ.get("BOT_SQUAD_UC_REDRIVE_GRACE_SEC", 120))
    except (TypeError, ValueError):
        return 120


def cooldown_sec() -> int:
    """Minimum gap between re-wake attempts for the same (slug, gid)."""
    try:
        return int(os.environ.get("BOT_SQUAD_UC_REDRIVE_COOLDOWN_SEC", 300))
    except (TypeError, ValueError):
        return 300


def retry_bound() -> int:
    try:
        v = int(os.environ.get("BOT_SQUAD_UC_REDRIVE_MAX_RETRIES", 3))
    except (TypeError, ValueError):
        return 3
    return v if v > 0 else 3


def _conversations_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / "_mothership" / "conversations" / slug


def _last_record(path: Path) -> dict | None:
    """Newest well-formed record in a conversation thread jsonl, or None.

    Tolerates a torn/garbage trailing line (mirrors ``conversation_store``'s
    own read tolerance) by skipping it and looking further back.
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
        return rec if isinstance(rec, dict) else None
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


def _notify_operator_stuck(cfg: Any, slug: str, gid: str, retries: int) -> None:
    try:
        from bot_squad_worker import intersession as _inter
        _inter.send(
            cfg, slug, to="operator",
            text=(f"⚠️ uc_redrive: user-conversation attendant for "
                  f"{gid} still unanswered after {retries} re-wake "
                  f"attempt(s) — needs a human look."),
            from_sid="S-uc-redrive",
        )
    except Exception:  # noqa: BLE001 — best-effort notify must never break the tick
        log.exception("uc_redrive: stuck-notify failed for %s/%s", slug, gid)


def check_project(cfg: Any, slug: str, *, now: float | None = None) -> dict:
    """One unanswered-message sweep for a project. Returns a summary dict.

    Idempotent + side-effecting: re-drives at most one nudge per (slug, gid)
    per cooldown window, bounded by ``retry_bound()`` total attempts per
    unanswered message.
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
        rec = _last_record(jf)
        if rec is None or str(rec.get("author")) != "user":
            # Answered (or empty) thread — nothing to re-drive. Drop any
            # stale retry-state so a LATER stuck message starts fresh.
            if gid in state:
                del state[gid]
                state_changed = True
            continue

        msg_ts = str(rec.get("timestamp") or "")
        msg_at = _parse_iso(msg_ts)
        if msg_at is None or (now - msg_at) < grace_sec():
            continue  # too soon — give the normal reply flow a chance first

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

        gid_state = state.get(gid) or {}
        if gid_state.get("msg_ts") != msg_ts:
            gid_state = {"msg_ts": msg_ts, "retries": 0, "last_redrive_at": 0}

        if gid_state["retries"] >= retry_bound():
            continue  # bounded — already escalated when the bound was first hit

        if now - float(gid_state.get("last_redrive_at") or 0) < cooldown_sec():
            continue  # cooldown not elapsed since the last re-wake attempt

        try:
            A.dispatch("ensure_user_conversation", {
                "slug": slug, "global_user_id": gid,
                "message_ref": rec.get("text"),
            })
        except Exception:
            log.exception("uc_redrive: redrive dispatch failed for %s/%s", slug, gid)
            continue

        gid_state["retries"] += 1
        gid_state["last_redrive_at"] = now
        state[gid] = gid_state
        state_changed = True
        redriven.append({"gid": gid, "sid": sid, "retries": gid_state["retries"]})
        log.warning("uc_redrive: re-woke idle attendant %s for %s/%s (retry %d)",
                    sid, slug, gid, gid_state["retries"])

        if gid_state["retries"] >= retry_bound():
            _notify_operator_stuck(cfg, slug, gid, gid_state["retries"])

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
