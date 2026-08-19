"""Autopilot (T-0153) — prompt-driven, time-boxed autonomous runs per target.

The stakeholder's ask (2026-06-02): turn the old "autonomous work" concept into
a first-class popover action on a **team**, a **single session**, or a whole
**project**. The operator presses *Autopilot*, fills a short dialog (duration
hours + a prompt + an early-exit condition + an optional stall threshold), and
the target TL receives that prompt "plainly" — delivered both into its live tmux
pane and into its peer inbox so a suspended TL still picks it up.

This is deliberately a **lighter, separate** mechanism from the legacy
``autonomous.py`` orchestrator (which is a backlog task-picker + DOD reviewer
state machine). Autopilot does not pick tasks or review DODs; it hands a TL a
brief, keeps it anchored with a stall watchdog, and ends on a time box or an
operator/TL early-exit.

State persists one JSON per autopilot under
``data/<slug>/_worker/autopilot/<key>.json``. A 60s scheduler tick
(``jobs.autopilot_tick``) walks every active autopilot and, per the configured
watchdog cadence, re-pings stalled targets and auto-ends expired runs (notifying
the stakeholder over Telegram).

Lifecycle::

    start  → resolve target TL (spawn one for session/project level if needed),
             deliver the brief (inbox + pane), persist state, status=running
    tick   → if now >= expires_at: status=expired + notify stakeholder
             else, every watchdog_minutes: if no progress for >= stall_minutes,
             re-deliver the brief + a "you're stalled, Xh remain" reminder
    stop   → TL/operator ends it early; status=exited (with reason) or stopped;
             notify stakeholder

The "early-exit condition" is checked by the TL itself (the worker cannot judge
an arbitrary natural-language condition); the brief instructs the TL to call
``bsq autopilot stop --reason "<what was met>"`` the moment it is satisfied.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_VALID_KINDS = {"team", "session", "project"}
_MAX_PROMPT_LEN = 4000


def _is_max_text_len() -> int:
    """The peer bus's own cap, READ from its SSOT rather than duplicated here.

    T-0827's underlying finding was four separate 4000s in four modules with no
    shared policy, so whether the next one refuses or truncates was a coin
    flip — a fifth literal in this file would be that same defect. Imported
    lazily, like every other ``intersession`` use in this module.
    """
    from bot_squad_worker.intersession import _MAX_TEXT_LEN
    return _MAX_TEXT_LEN
_MAX_LOG = 50
_DEFAULT_DURATION_HOURS = 8.0
_DEFAULT_STALL_MINUTES = 60
_DEFAULT_WATCHDOG_MINUTES = 5
_KEY_SANITISE_RE = re.compile(r"[^A-Za-z0-9_.-]")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class AutopilotState:
    slug: str
    key: str                          # file stem / handle, e.g. "session-S-..", "team-foo", "project"
    kind: str                         # team | session | project
    ref: str                          # original target ref (team name / sid / slug)
    target_sid: str                   # resolved TL/session SID receiving the brief
    prompt: str                       # the autopilot task prompt
    early_exit: str = ""              # early-exit condition (TL-evaluated)
    duration_hours: float = _DEFAULT_DURATION_HOURS
    stall_minutes: int = _DEFAULT_STALL_MINUTES
    watchdog_minutes: int = _DEFAULT_WATCHDOG_MINUTES
    enabled: bool = True
    status: str = "running"           # running | expired | exited | stopped
    exit_reason: str = ""
    created_by: str = ""
    started_at: str = ""
    expires_at: str = ""
    last_check_at: Optional[str] = None
    last_ping_at: Optional[str] = None
    pings: int = 0
    log: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: Any) -> Optional[float]:
    """Parse an ISO ts (``...Z`` or offset form) to epoch seconds, or None."""
    if not ts or ts == "~":
        return None
    s = str(ts).strip()
    try:
        if s.endswith("Z"):
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _autopilot_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "autopilot"


def _sanitise_key(key: str) -> str:
    return _KEY_SANITISE_RE.sub("_", (key or "").strip()) or "_"


def _state_path(cfg: Any, slug: str, key: str) -> Path:
    return _autopilot_dir(cfg, slug) / f"{_sanitise_key(key)}.json"


def _from_raw(raw: dict, slug: str) -> AutopilotState:
    return AutopilotState(
        slug=slug,
        key=raw.get("key", ""),
        kind=raw.get("kind", "session"),
        ref=raw.get("ref", ""),
        target_sid=raw.get("target_sid", ""),
        prompt=raw.get("prompt", ""),
        early_exit=raw.get("early_exit", ""),
        duration_hours=float(raw.get("duration_hours", _DEFAULT_DURATION_HOURS)),
        stall_minutes=int(raw.get("stall_minutes", _DEFAULT_STALL_MINUTES)),
        watchdog_minutes=int(raw.get("watchdog_minutes", _DEFAULT_WATCHDOG_MINUTES)),
        enabled=bool(raw.get("enabled", True)),
        status=raw.get("status", "running"),
        exit_reason=raw.get("exit_reason", ""),
        created_by=raw.get("created_by", ""),
        started_at=raw.get("started_at", ""),
        expires_at=raw.get("expires_at", ""),
        last_check_at=raw.get("last_check_at"),
        last_ping_at=raw.get("last_ping_at"),
        pings=int(raw.get("pings", 0)),
        log=raw.get("log", []),
    )


def load_state(cfg: Any, slug: str, key: str) -> Optional[AutopilotState]:
    p = _state_path(cfg, slug, key)
    if not p.exists():
        return None
    try:
        return _from_raw(json.loads(p.read_text()), slug)
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot: could not load %s: %s", p, e)
        return None


def list_states(cfg: Any, slug: str) -> list[AutopilotState]:
    d = _autopilot_dir(cfg, slug)
    out: list[AutopilotState] = []
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            out.append(_from_raw(json.loads(p.read_text()), slug))
        except Exception:  # noqa: BLE001
            log.warning("autopilot: skipping unparseable state %s", p)
    return out


def save_state(cfg: Any, state: AutopilotState) -> None:
    p = _state_path(cfg, state.slug, state.key)
    p.parent.mkdir(parents=True, exist_ok=True)
    if len(state.log) > _MAX_LOG:
        state.log = state.log[-_MAX_LOG:]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    tmp.replace(p)


def _log(state: AutopilotState, msg: str) -> None:
    state.log.append({"ts": _now_iso(), "msg": msg})
    if len(state.log) > _MAX_LOG:
        state.log = state.log[-_MAX_LOG:]


# ---------------------------------------------------------------------------
# Delivery + notification
# ---------------------------------------------------------------------------

def _deliver(cfg: Any, slug: str, target_sid: str, text: str) -> dict:
    """Deliver ``text`` to a target: peer inbox (persists) + live pane (tmux).

    Returns ``{"inbox": bool, "pane": bool}`` describing what landed. The inbox
    write always happens (so a suspended TL picks it up on its next read); the
    pane paste happens only when the target SID maps to a live tmux pane.
    """
    from bot_squad_worker import intersession as _is
    from bot_squad_worker import sessions as S

    result = {"inbox": False, "pane": False}
    try:
        # T-0827: read the RESULT, do not infer delivery from the absence of an
        # exception. `send` never raises — an over-cap message comes back as
        # `ok: False` with an empty `delivered_to` — so the old
        # `send(...); result["inbox"] = True` reported a delivery that had not
        # happened. That is the same "caller believes it sent" claim the ticket
        # exists to kill, one layer up from the bus.
        sent = _is.send(cfg, slug, "autopilot", target_sid, text)
        result["inbox"] = bool(sent.get("ok", True)) and bool(sent.get("delivered_to"))
        if not result["inbox"]:
            log.error(
                "autopilot: inbox delivery to %s was REFUSED: %s",
                target_sid, sent.get("error", "no recipient"),
            )
    except Exception:  # noqa: BLE001
        log.exception("autopilot: inbox delivery failed for %s", target_sid)

    try:
        user = S._get_current_user()
        panes = {S.compute_sid(user, p.window, p.pane_id): p for p in S.list_panes()}
        pane = panes.get(target_sid)
        if pane is not None:
            # T-0578: identity threads through so the paste holds the per-sid
            # mux delivery lock (never interleaves with other writers).
            S._deliver_prompt(pane.pane_id, text,
                              data_dir=cfg.data_dir, sid=target_sid)
            result["pane"] = True
    except Exception:  # noqa: BLE001
        log.exception("autopilot: pane delivery failed for %s", target_sid)
    return result


def _notify_stakeholder(cfg: Any, slug: str, text: str) -> None:
    """Best-effort stakeholder page via the _send_stakeholder_dm SSOT (P2-08).

    MAX-primary on this DPI-blocked host — a raw TG send silently dropped this
    page here — with a best-effort #team-queries group-record.
    """
    try:
        from bot_squad_worker.actions import _send_stakeholder_dm
        from bot_squad_worker import tg_topics as _tg_topics
        project = cfg.projects.get(slug)
        chat_id = getattr(project, "tg_chat", "") if project else ""
        _send_stakeholder_dm(
            cfg, message=text, sid="autopilot",
            # T-0758 follow-up: the unfixed twin of the lifecycle-notice case.
            # `[autopilot]` says WHAT is speaking but not WHICH PROJECT, and
            # autopilot is per-project — this alert is about `slug`. Same
            # unprompted-message reasoning as `task_chat._notify`; renders
            # `[<slug> autopilot]`.
            slug=slug,
            tg_chat_id=chat_id,
            tg_topic_id=_tg_topics.resolve(cfg, slug, "team_queries"),
            group_record=bool(chat_id),
            # T-0799: LOG class. Every page from here reports that an autopilot
            # RUN ENDED (stopped / exited on its condition / completed its
            # hours) — a record of what the system did, not a thing he must act
            # on. It already rides the default `urgent=False`.
            msg_type="autopilot_notice",
        )
    except Exception:  # noqa: BLE001
        log.exception("autopilot: stakeholder notify failed for %s", slug)


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def _live_rows(cfg: Any, slug: str) -> list[dict]:
    from bot_squad_worker import sessions as S
    try:
        return S.list_sessions(cfg, slug)
    except Exception:  # noqa: BLE001
        log.exception("autopilot: list_sessions failed for %s", slug)
        return []


def _pick_project_tl(cfg: Any, slug: str) -> Optional[str]:
    """Return a TL/operator SID for project-level autopilot, preferring active."""
    rows = _live_rows(cfg, slug)
    coords = [r for r in rows if r.get("role") in ("teamlead", "operator")]
    active = [r for r in coords if r.get("status") == "active"]
    pool = active or coords
    return pool[0]["sid"] if pool else None


def _is_sid_live(cfg: Any, slug: str, sid: str) -> bool:
    return any(r.get("sid") == sid and r.get("status") != "suspended"
               for r in _live_rows(cfg, slug))


def compose_brief(state: AutopilotState) -> str:
    """The autopilot brief delivered to the target TL on start + on each stall."""
    early = state.early_exit.strip() or "(none specified — run the full duration)"
    return (
        f"🛫 AUTOPILOT — you're running autonomously for ~{state.duration_hours:g}h "
        f"(until {state.expires_at}).\n\n"
        f"Your task:\n{state.prompt.strip()}\n\n"
        f"Quit autopilot early if: {early}\n\n"
        f"How this works:\n"
        f"- Drive the task autonomously. Commit and log progress regularly "
        f"(`bsq ticket note <id> <text>`) so the stall watchdog sees you're alive.\n"
        f"- If no progress is detected for ~{state.stall_minutes}min you'll get a "
        f"re-ping with this brief and the time remaining.\n"
        f"- The MOMENT the early-exit condition is met, run:\n"
        f"    bsq autopilot stop {state.key} --reason \"<what was met>\"\n"
        f"  (ends autopilot and notifies the stakeholder).\n"
        f"- Autopilot auto-ends at {state.expires_at}; the stakeholder is notified then too.\n"
        f"- Check the early-exit condition on every dispatch tick / before each new sub-task."
    )


# ---------------------------------------------------------------------------
# Progress signal (for the stall watchdog)
# ---------------------------------------------------------------------------

def _git_last_commit_at(cfg: Any, slug: str) -> Optional[float]:
    project = cfg.projects.get(slug)
    if project is None:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(project.repo_path), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return float(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _latest_progress_note_at(cfg: Any, slug: str) -> Optional[float]:
    from bot_squad_worker import drift as D
    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return None
    best: Optional[float] = None
    for md in backlog.glob("*.md"):
        t = D._ticket_last_touch(md)
        if t is not None and (best is None or t > best):
            best = t
    return best


def _progress_at(cfg: Any, slug: str, state: AutopilotState) -> float:
    """Best estimate of the target's last "progress" (commits / notes / activity).

    Floored at ``started_at`` so a fresh autopilot is never instantly "stalled"
    before the stall window elapses.
    """
    candidates: list[float] = []
    started = _parse_iso(state.started_at)
    if started is not None:
        candidates.append(started)
    last_ping = _parse_iso(state.last_ping_at)
    if last_ping is not None:
        # A re-ping resets the clock — we don't want to nag every watchdog tick;
        # the target gets stall_minutes to respond to each ping.
        candidates.append(last_ping)

    for r in _live_rows(cfg, slug):
        if r.get("sid") == state.target_sid:
            act = r.get("activity_at")
            if isinstance(act, (int, float)):
                candidates.append(float(act))
            break

    commit_at = _git_last_commit_at(cfg, slug)
    if commit_at is not None:
        candidates.append(commit_at)
    note_at = _latest_progress_note_at(cfg, slug)
    if note_at is not None:
        candidates.append(note_at)

    return max(candidates) if candidates else time.time()


# ---------------------------------------------------------------------------
# Public API: start / stop / status / tick
# ---------------------------------------------------------------------------

def _default_key(kind: str, ref: str) -> str:
    if kind == "project":
        return "project"
    return f"{kind}-{ref}"


def start(
    cfg: Any,
    slug: str,
    *,
    kind: str,
    ref: str,
    prompt: str,
    early_exit: str = "",
    duration_hours: float = _DEFAULT_DURATION_HOURS,
    stall_minutes: int = _DEFAULT_STALL_MINUTES,
    watchdog_minutes: int = _DEFAULT_WATCHDOG_MINUTES,
    created_by: str = "",
) -> dict:
    """Begin an autopilot run for a team / session / project target.

    Resolves the target TL (spawning one for session/project level when no live
    coordinator exists), delivers the brief, and persists state. Returns
    ``{ok, key, target_sid, expires_at, spawned, delivery}``.
    """
    from bot_squad_worker.actions import ActionError

    if cfg.projects.get(slug) is None:
        raise ActionError(f"autopilot.start: unknown project slug {slug!r}")
    if kind not in _VALID_KINDS:
        raise ActionError(f"autopilot.start: kind must be one of {sorted(_VALID_KINDS)}")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ActionError("autopilot.start: prompt is required")
    # T-0827 (third twin of the same bare slice): this used to be
    # `prompt = prompt[:_MAX_PROMPT_LEN]`. An autopilot prompt is a STANDING
    # BRIEF delivered to a TL that then drives a team for hours — the one
    # payload where a silently dropped tail is least visible and most costly,
    # since the recipient cannot know what the brief was supposed to say. Same
    # refusal as `task_body._sanitize_progress_text` and `intersession._sanitize`;
    # the caller is an agent/CLI that can shorten and retry.
    if len(prompt) > _MAX_PROMPT_LEN:
        raise ActionError(
            f"autopilot.start: prompt is {len(prompt)} chars, over the "
            f"{_MAX_PROMPT_LEN}-char cap — refusing to truncate (silent loss, "
            "T-0827). Shorten the brief, or put the detail on a ticket and "
            "point the prompt at it."
        )
    try:
        duration_hours = float(duration_hours)
    except (TypeError, ValueError):
        duration_hours = _DEFAULT_DURATION_HOURS
    if duration_hours <= 0:
        raise ActionError("autopilot.start: duration_hours must be > 0")
    stall_minutes = max(1, int(stall_minutes or _DEFAULT_STALL_MINUTES))
    watchdog_minutes = max(1, int(watchdog_minutes or _DEFAULT_WATCHDOG_MINUTES))

    key = _default_key(kind, ref)
    now = time.time()
    started_at = _now_iso()
    expires_at = datetime.fromtimestamp(now + duration_hours * 3600, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # ---- resolve the target SID, spawning a TL when the level needs one ----
    spawned = False
    target_sid = ""
    spawn_window: Optional[str] = None

    if kind == "session":
        target_sid = ref
        if not target_sid:
            raise ActionError("autopilot.start: session-level autopilot needs a target SID (ref)")
    elif kind == "team":
        from bot_squad_worker import teams as _teams
        team = _teams.load_team(cfg, slug, ref)
        if team is None:
            raise ActionError(f"autopilot.start: no team named {ref!r}")
        tl = team.get("tl")
        if tl and tl != "~" and _is_sid_live(cfg, slug, tl):
            target_sid = tl
        else:
            spawn_window = f"autopilot-{_sanitise_key(ref)}"[:40]
    elif kind == "project":
        tl = _pick_project_tl(cfg, slug)
        if tl:
            target_sid = tl
        else:
            spawn_window = "autopilot-tl"

    state = AutopilotState(
        slug=slug,
        key=key,
        kind=kind,
        ref=ref or slug,
        target_sid=target_sid,
        prompt=prompt,
        early_exit=(early_exit or "").strip(),
        duration_hours=duration_hours,
        stall_minutes=stall_minutes,
        watchdog_minutes=watchdog_minutes,
        enabled=True,
        status="running",
        created_by=created_by or "",
        started_at=started_at,
        expires_at=expires_at,
        last_check_at=started_at,
    )

    # T-0827: the two caps have to COMPOSE, and they did not. `compose_brief`
    # wraps the prompt in ~735 chars of boilerplate, so a prompt that is legal
    # at this module's own 4000 cap yields a 4735-char brief that the bus cap
    # then refuses — and before T-0827 it did something worse, silently slicing
    # the brief a SECOND time after `prompt[:4000]` had already sliced it once.
    # Checked HERE rather than at delivery so the caller learns it at the CLI,
    # before a TL is spawned for a brief that cannot be delivered whole. The
    # message names the overhead, since "your 3900-char prompt is too long for a
    # 4000-char cap" is otherwise unactionable.
    brief_len = len(compose_brief(state))
    if brief_len > _is_max_text_len():
        raise ActionError(
            f"autopilot.start: the composed brief is {brief_len} chars, over the "
            f"{_is_max_text_len()}-char peer-bus cap — refusing to deliver it "
            f"truncated (silent loss, T-0827). Your prompt is {len(prompt)} chars "
            f"and the brief boilerplate adds {brief_len - len(prompt)}; shorten the "
            "prompt by at least "
            f"{brief_len - _is_max_text_len()} chars, or put the detail on a ticket "
            "and point the prompt at it."
        )

    delivery = {"inbox": False, "pane": False}
    if spawn_window is not None:
        # No live TL — spawn one with the brief as its initial prompt. The
        # spawn delivers the brief into the new pane itself, so here we only
        # mirror it into the inbox (persistence) — no second pane paste.
        from bot_squad_worker import intersession as _is
        from bot_squad_worker import sessions as S
        brief = compose_brief(state)  # uses expires/key which are already set
        res = S.spawn(cfg, slug, spawn_window, initial_prompt=brief,
                      owner=created_by or None,
                      dispatched_by="autopilot")  # T-0909: attributable
        target_sid = res.get("sid", "")
        if not target_sid:
            raise ActionError("autopilot.start: spawn did not return a SID")
        state.target_sid = target_sid
        spawned = True
        try:
            # T-0827: same as `_deliver` — the inbox flag reports what the bus
            # actually did, not that the call returned.
            sent = _is.send(cfg, slug, "autopilot", target_sid, brief)
            ok = bool(sent.get("ok", True)) and bool(sent.get("delivered_to"))
            if not ok:
                log.error(
                    "autopilot: inbox mirror to spawned %s was REFUSED: %s",
                    target_sid, sent.get("error", "no recipient"),
                )
            delivery = {"inbox": ok, "pane": True}  # pane via spawn's initial_prompt
        except Exception:  # noqa: BLE001
            log.exception("autopilot: inbox mirror failed for spawned %s", target_sid)
            delivery = {"inbox": False, "pane": True}
        _log(state, f"spawned TL {target_sid} (window={spawn_window}) with brief")
    else:
        delivery = _deliver(cfg, slug, target_sid, compose_brief(state))
        _log(state, f"delivered brief to {target_sid} (inbox={delivery['inbox']}, pane={delivery['pane']})")

    save_state(cfg, state)
    log.info("autopilot[%s]: started %s → %s (%sh, stall %smin)",
             slug, key, target_sid, duration_hours, stall_minutes)
    return {
        "ok": True,
        "key": key,
        "kind": kind,
        "ref": state.ref,
        "target_sid": target_sid,
        "expires_at": expires_at,
        "spawned": spawned,
        "delivery": delivery,
    }


def _find_state(cfg: Any, slug: str, key: Optional[str], target_sid: Optional[str]) -> Optional[AutopilotState]:
    if key:
        st = load_state(cfg, slug, key)
        if st is not None:
            return st
    if target_sid:
        for st in list_states(cfg, slug):
            if st.target_sid == target_sid and st.enabled:
                return st
    return None


def stop(
    cfg: Any,
    slug: str,
    *,
    key: Optional[str] = None,
    target_sid: Optional[str] = None,
    reason: str = "",
    stopped_by: str = "",
) -> dict:
    """End an autopilot early. ``reason`` set ⟹ status=exited (early-exit met);
    no reason ⟹ status=stopped (operator cancel). Notifies the stakeholder.

    A TL can omit ``key`` and pass its own SID as ``target_sid`` to stop the
    autopilot it's running under.
    """
    from bot_squad_worker.actions import ActionError

    if cfg.projects.get(slug) is None:
        raise ActionError(f"autopilot.stop: unknown project slug {slug!r}")

    state = _find_state(cfg, slug, key, target_sid)
    if state is None:
        raise ActionError("autopilot.stop: no matching autopilot (pass key or target_sid)")

    reason = (reason or "").strip()
    state.enabled = False
    state.status = "exited" if reason else "stopped"
    state.exit_reason = reason
    _log(state, f"{state.status} by {stopped_by or 'operator'}"
                + (f": {reason}" if reason else ""))
    save_state(cfg, state)

    if reason:
        _notify_stakeholder(
            cfg, slug,
            f"🛬 Autopilot ({state.ref}) exited early — condition met: {reason}",
        )
    else:
        _notify_stakeholder(
            cfg, slug,
            f"🛬 Autopilot ({state.ref}) stopped by {stopped_by or 'operator'}.",
        )
    log.info("autopilot[%s]: stopped %s (status=%s)", slug, state.key, state.status)
    return {"ok": True, "key": state.key, "status": state.status, "exit_reason": reason}


def status(cfg: Any, slug: str) -> dict:
    """Return every autopilot state for the project (active + recently ended)."""
    from bot_squad_worker.actions import ActionError
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autopilot.status: unknown project slug {slug!r}")
    states = list_states(cfg, slug)
    out = []
    for st in states:
        d = asdict(st)
        d["log"] = st.log[-5:]
        out.append(d)
    return {"ok": True, "slug": slug, "autopilots": out}


def tick(cfg: Any, slug: str) -> dict:
    """One watchdog pass for a project: expire finished runs, re-ping stalled.

    Idempotent + side-effecting. Called by ``jobs.autopilot_tick`` every 60s;
    each autopilot is only *evaluated* every ``watchdog_minutes`` and only
    *re-pinged* when the no-progress window has reached ``stall_minutes``.
    """
    actions: list[dict] = []
    now = time.time()
    for state in list_states(cfg, slug):
        if not state.enabled or state.status != "running":
            continue

        # --- expiry ---
        expires = _parse_iso(state.expires_at)
        if expires is not None and now >= expires:
            state.status = "expired"
            state.enabled = False
            _log(state, "autopilot duration elapsed → expired")
            save_state(cfg, state)
            _notify_stakeholder(
                cfg, slug,
                f"🛬 Autopilot ({state.ref}) completed its "
                f"{state.duration_hours:g}h run (target {state.target_sid}).",
            )
            actions.append({"key": state.key, "action": "expired"})
            continue

        # --- watchdog cadence throttle ---
        last_check = _parse_iso(state.last_check_at)
        if last_check is not None and (now - last_check) < state.watchdog_minutes * 60:
            continue
        state.last_check_at = _now_iso()

        # --- stall detection ---
        last_progress = _progress_at(cfg, slug, state)
        idle_sec = now - last_progress
        if idle_sec >= state.stall_minutes * 60:
            remaining_h = max(0.0, (expires - now) / 3600.0) if expires else 0.0
            idle_min = int(idle_sec / 60)
            reminder = (
                f"⚠️ AUTOPILOT STALL — no progress detected for ~{idle_min}min "
                f"(threshold {state.stall_minutes}min). ~{remaining_h:.1f}h remain.\n"
                f"Re-anchor to your autopilot task and log progress "
                f"(`bsq ticket note`) or commit so the watchdog sees you're alive.\n\n"
                + compose_brief(state)
            )
            _deliver(cfg, slug, state.target_sid, reminder)
            state.pings += 1
            state.last_ping_at = _now_iso()
            _log(state, f"stall re-ping #{state.pings} (idle ~{idle_min}min)")
            actions.append({"key": state.key, "action": "stall_reping", "idle_min": idle_min})

        save_state(cfg, state)

    return {"ok": True, "slug": slug, "actions": actions}
