"""T-0155: stall-watchdog — auto-escalate an agent blocked on the stakeholder.

The implicit per-idle TG flood (the old ``scripts/hooks/stop.sh`` emit) is
gone. TG is now reached two ways:

1. **Explicitly** — an agent runs ``bsq tg ping <message>`` (the
   ``tg_notify`` action). Easy to remember, fires exactly when the agent
   chooses.

2. **Auto-escalation (this module)** — the safety net for when the agent
   forgets the verb. When an agent ``peer_send``s a session whose role is
   ``operator`` (i.e. it is blocked on the stakeholder) and gets no reply,
   a *stall marker* is written. The worker's ``tick`` then fires a single
   TG ping iff ALL of:
     - the marker has aged past ``cfg.tg_stall_minutes`` (default 15), AND
     - the agent's tmux window is **not** being watched (no attached client
       has that window active — "I don't have the window open in tmux").

   Escalation is one-shot per marker (``escalated`` flag) so a long block
   never re-floods. The escalation message carries the ``[<SID>]`` prefix so
   a TG reply routes back into the pane via ``tg_listener``, plus a
   remote-control footer to resume in the Claude app.

A marker is cleared the moment the block resolves: the stakeholder replies in
tmux (``user_prompt_submit`` hook), the operator peer_sends the agent back, or
a TG reply is injected. So "no reply in tmux for 15 minutes" is exactly the
window in which a marker survives to escalation.

Marker file: ``data/<slug>/_worker/tg_stall/<sid>.json``::

    {"sid": "...", "slug": "...", "since": 1730000000.0,
     "text": "blocked: need your call on prod", "escalated": false}
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Markers older than this (escalated or not) are garbage-collected so a dead
# session's marker can't linger forever.
_MARKER_TTL_SEC = 24 * 3600


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _stall_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "_worker" / "tg_stall"


def _marker_path(cfg: Any, slug: str, sid: str) -> Path:
    # SID is filesystem-safe (S-<user>-<window>-p<N>); no separators.
    return _stall_dir(cfg, slug) / f"{sid}.json"


# ---------------------------------------------------------------------------
# Marker lifecycle
# ---------------------------------------------------------------------------

def mark_blocked(cfg: Any, slug: str, sid: str, text: str) -> None:
    """Record that ``sid`` is blocked on the stakeholder.

    Idempotent while the block stands: an existing, not-yet-escalated marker
    keeps its original ``since`` (the 15-min clock started when the agent
    *first* asked) and only refreshes the text. A previously escalated marker
    is reset to a fresh block.
    """
    if not sid:
        return
    p = _marker_path(cfg, slug, sid)
    since = time.time()
    existing = _read(p)
    if existing and not existing.get("escalated"):
        since = float(existing.get("since", since))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"sid": sid, "slug": slug, "since": since, "text": text, "escalated": False},
        indent=2,
    ))
    tmp.replace(p)
    log.info("tg_stall: marked %s blocked on operator (slug=%s)", sid, slug)


def blocked_sids(cfg: Any, slug: str) -> set[str]:
    """T-0285: the set of SIDs with an active stall marker — i.e. blocked
    waiting on the operator (they ``peer_send``-ed an operator-role session and
    haven't been replied to). Surfaced on the sessions payload as a per-session
    ``awaiting_input`` flag so the UI can glance "this one is waiting on you".

    Honors the same ``_MARKER_TTL_SEC`` the GC uses, so a stale marker left by a
    dead session doesn't show forever. Best-effort: any read error → empty set.
    """
    out: set[str] = set()
    try:
        entries = list(_stall_dir(cfg, slug).glob("*.json"))
    except OSError:
        return out
    now = time.time()
    for p in entries:
        m = _read(p)
        if not m:
            continue
        since = m.get("since")
        try:
            if since is not None and (now - float(since)) > _MARKER_TTL_SEC:
                continue
        except (TypeError, ValueError):
            pass
        sid = m.get("sid") or p.stem
        if sid:
            out.add(str(sid))
    return out


def clear_blocked(cfg: Any, slug: str, sid: str) -> bool:
    """Remove ``sid``'s stall marker. Returns True if one existed."""
    if not sid:
        return False
    p = _marker_path(cfg, slug, sid)
    try:
        p.unlink()
        log.info("tg_stall: cleared block for %s (slug=%s)", sid, slug)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("tg_stall: failed clearing marker for %s", sid)
        return False


def on_peer_send(cfg: Any, slug: str, from_sid: str, recipient_sids: list[str]) -> None:
    """React to a ``peer_send`` for the stall-watchdog (best-effort).

    Two effects, resolved from one session-registry read:
      - **mark**: if any recipient is an ``operator``-role session, ``from_sid``
        is now blocked on the stakeholder → write/refresh its marker.
      - **clear**: if the *sender* is the operator (the stakeholder replying),
        clear each recipient's marker — they just got their answer.

    Failures are swallowed so the bus write is never affected.
    """
    if not from_sid or not recipient_sids:
        return
    try:
        from bot_squad_worker import sessions as S
        rows = {r["sid"]: r for r in S.list_sessions(cfg, slug)}
    except Exception:  # noqa: BLE001
        log.exception("tg_stall: could not list sessions for operator check (slug=%s)", slug)
        return

    sender_is_operator = rows.get(from_sid, {}).get("role") == "operator"
    if sender_is_operator:
        for rsid in recipient_sids:
            clear_blocked(cfg, slug, rsid)
        return

    for rsid in recipient_sids:
        if rows.get(rsid, {}).get("role") == "operator":
            # The agent just asked the stakeholder something → it is now
            # blocked on the stakeholder until a reply clears the marker.
            mark_blocked(cfg, slug, from_sid, _last_marker_text(cfg, slug, from_sid))
            return


def _last_marker_text(cfg: Any, slug: str, sid: str) -> str:
    """Best-effort: keep an existing marker's text (peer_send doesn't carry it
    into this module). Falls back to a generic blocked note."""
    existing = _read(_marker_path(cfg, slug, sid))
    if existing and existing.get("text"):
        return str(existing["text"])
    return "is blocked waiting on your reply"


def _read(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# tmux visibility — "do I have the window open?"
# ---------------------------------------------------------------------------

def _pane_for_sid(sid: str) -> Any:
    """Return the live PaneInfo for ``sid`` or None."""
    try:
        from bot_squad_worker import sessions as S
        user = S._get_current_user()
        for pane in S.list_panes():
            if S.compute_sid(user, pane.window, pane.pane_id) == sid:
                return pane
    except Exception:  # noqa: BLE001
        log.exception("tg_stall: pane lookup failed for %s", sid)
    return None


def _window_visible(pane_id: str) -> bool:
    """True iff the stakeholder is watching this pane's window.

    "Watching" = a client is attached to the pane's tmux session AND that
    window is the active window in the session. A detached session, or the
    window sitting in the background, both count as *not* visible.
    """
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "-F",
             "#{session_attached}|#{window_active}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    parts = out.stdout.strip().split("|")
    if len(parts) != 2:
        return False
    try:
        attached = int(parts[0])
        window_active = int(parts[1])
    except ValueError:
        return False
    return attached >= 1 and window_active == 1


# ---------------------------------------------------------------------------
# T-0034: idle-notify routing by role + TL parent
# ---------------------------------------------------------------------------

def _route_idle_escalation(cfg: Any, slug: str, sid: str) -> Optional[str]:
    """Where should ``sid``'s idle/blocked escalation go? (T-0034)

    Returns a target SID to ``peer_send``, or ``None`` meaning "page the
    stakeholder via TG" (the existing behaviour). Routing follows the ticket:

      - **dev**, team ``tl`` is a teamlead    → that TL.
      - **dev**, team ``tl`` is operator/none → the operator session.
      - **teamlead** (regular dev TL)         → the operator session.
      - **operator**                          → ``None`` (TG the stakeholder).
      - **teamlead with no operator** (a prod-teamlead on the prod contour,
        which has no operator above it)       → ``None`` (TG the stakeholder).

    The fall-through to TG is exactly "no upstream session to peer_send". The
    point of T-0034: a dev going idle under a TL never pages the stakeholder —
    the TL gets the peer_send instead. Explicit ``bsq tg ping`` pages bypass
    this watchdog entirely and still reach the stakeholder.

    Best-effort: any lookup failure routes to ``None`` (TG) — the safe default
    that never silently swallows an escalation.
    """
    try:
        from bot_squad_worker import sessions as S
        rows = S.list_sessions(cfg, slug)
    except Exception:  # noqa: BLE001
        log.exception("tg_stall: idle-routing session lookup failed (slug=%s)", slug)
        return None

    by_sid = {r.get("sid"): r for r in rows}
    role = (by_sid.get(sid) or {}).get("role")
    operator_sid = next((r.get("sid") for r in rows if r.get("role") == "operator"), None)

    if role == "operator":
        # The stakeholder's own session — nothing above it. TG.
        return None

    if role == "dev":
        try:
            from bot_squad_worker import teams as T
            tl = T.tl_for_sid(cfg, slug, sid)
        except Exception:  # noqa: BLE001
            log.exception("tg_stall: team-projection lookup failed for %s", sid)
            tl = None
        if tl and (by_sid.get(tl) or {}).get("role") == "teamlead":
            return tl              # dev under a TL → the TL
        return operator_sid        # ad-hoc dev (no TL) → the operator

    # teamlead (regular dev TL) → the operator. A prod-teamlead on the prod
    # contour has no operator session, so operator_sid is None → TG. The same
    # safe fall-through covers an unknown/None role.
    return operator_sid


def _redirect_to_upstream(
    cfg: Any, slug: str, sid: str, target: str, data: dict, marker: Path,
) -> bool:
    """Deliver the idle escalation to ``target`` over the peer bus + nudge,
    instead of paging the stakeholder. Consumes the marker (one-shot).

    Calls ``intersession.send`` directly (not the ``peer_send`` action) so the
    stall-watchdog hook does not re-fire on this system-generated message.
    Then injects a ``check mail`` nudge into the target's pane, mirroring what
    ``bsq peer send`` does — best-effort; a suspended/non-tmux target just
    reads it on its next inbox check.
    """
    stall_minutes = int(getattr(cfg, "tg_stall_minutes", 15))
    text = str(data.get("text", "")).strip() or "is idle / waiting on a reply"
    body = (
        f"⏳ idle-notify: {sid} {text} (no reply for ≥{stall_minutes}m). "
        "Give them work or release them."
    )
    try:
        from bot_squad_worker import intersession as _is
        _is.send(cfg, slug, sid, target, body)
    except Exception:  # noqa: BLE001
        log.exception("tg_stall: idle redirect to %s failed for %s", target, sid)
        return False

    # Primary cross-session signal — nudge the target's pane (best-effort).
    try:
        from bot_squad_worker.actions import _action_inject_input
        _action_inject_input({"sid": target, "text": "check mail"})
    except Exception:  # noqa: BLE001
        log.debug("tg_stall: pane nudge skipped for %s (no live pane?)", target)

    data["escalated"] = True
    data["escalated_at"] = time.time()
    data["routed_to"] = target
    tmp = marker.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(marker)
    log.info("tg_stall: %s idle-redirected to %s (slug=%s) — no TG", sid, target, slug)
    return True


# ---------------------------------------------------------------------------
# Escalation message
# ---------------------------------------------------------------------------

def build_escalation_text(cfg: Any, sid: str, text: str, session_name: str) -> str:
    """Body for the escalation TG (the TgClient adds the ``[<SID>]`` prefix).

    Includes a remote-control footer: the configured URL when set, otherwise a
    ``tmux attach`` hint. Always tells the stakeholder he can just reply.
    """
    lines = [f"🔔 {text}".rstrip(), "", "↩️ Reply to this message to answer in the tmux session."]
    url = getattr(cfg, "tg_remote_control_url", "") or ""
    if url:
        try:
            url = url.format(sid=sid, session=session_name)
        except (KeyError, IndexError):
            pass
        lines.append(f"🖥 Remote-control (Claude app): {url}")
    elif session_name:
        lines.append(f"🖥 Remote-control: tmux attach -t {session_name}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Watchdog tick
# ---------------------------------------------------------------------------

def tick(cfg: Any) -> dict:
    """One escalation sweep across all projects. Returns an audit dict."""
    stall_minutes = int(getattr(cfg, "tg_stall_minutes", 15))
    audit = {"ok": True, "checked": 0, "escalated": 0, "gc": 0, "disabled": False}
    if stall_minutes <= 0:
        audit["disabled"] = True
        return audit

    now = time.time()
    threshold = stall_minutes * 60
    for slug in getattr(cfg, "projects", {}):
        d = _stall_dir(cfg, slug)
        if not d.exists():
            continue
        for marker in sorted(d.glob("*.json")):
            data = _read(marker)
            if data is None:
                continue
            audit["checked"] += 1
            since = float(data.get("since", now))
            age = now - since
            # Garbage-collect ancient markers (dead sessions).
            if age > _MARKER_TTL_SEC:
                marker.unlink(missing_ok=True)
                audit["gc"] += 1
                continue
            if data.get("escalated"):
                continue
            if age < threshold:
                continue
            try:
                if _escalate(cfg, slug, data, marker):
                    audit["escalated"] += 1
            except Exception:  # noqa: BLE001
                log.exception("tg_stall: escalation failed for %s", data.get("sid"))
    return audit


def _escalate(cfg: Any, slug: str, data: dict, marker: Path) -> bool:
    """Escalate one stale marker. Returns True if it was consumed (a TG sent or
    an in-bus redirect delivered) so the watchdog counts it once.

    T-0034: the escalation is routed by the blocked session's role + TL parent.
    A dev under a TL (and any non-operator session with an upstream) gets the
    nudge delivered to its TL / the operator over the peer bus — the
    stakeholder is NOT paged. Only an operator (or a prod-teamlead with no
    operator above it) falls through to the TG path below.
    """
    sid = data.get("sid", "")
    pane = _pane_for_sid(sid)
    if pane is None:
        # The pane is gone — the session ended; nothing to reply into. Drop
        # the marker rather than paging about a dead session.
        marker.unlink(missing_ok=True)
        log.info("tg_stall: %s pane gone — dropping marker (no escalation)", sid)
        return False

    # T-0034: redirect to the TL / operator instead of TG, when there is one.
    # No window-visible gate here — the upstream session is a different
    # recipient than the stakeholder watching the dev's pane, and the marker is
    # one-shot, so it never re-floods.
    target = _route_idle_escalation(cfg, slug, sid)
    if target:
        return _redirect_to_upstream(cfg, slug, sid, target, data, marker)

    if _window_visible(pane.pane_id):
        # Stakeholder is looking at the window — no TG needed.
        log.info("tg_stall: %s window is visible — skip escalation", sid)
        return False

    project = cfg.projects.get(slug)
    chat_id = getattr(project, "tg_chat", "") if project else ""
    if not chat_id:
        log.warning("tg_stall: %s no tg_chat for slug %s — cannot escalate", sid, slug)
        return False

    body = build_escalation_text(cfg, sid, str(data.get("text", "")), pane.session)
    # T-0394: page the human via the _send_stakeholder_dm SSOT — MAX-primary
    # (TG is DPI-blocked on this host), TG failover, + a best-effort group-record.
    # T-0386: the group target is the project's #team-queries forum topic.
    from bot_squad_worker.actions import _send_stakeholder_dm
    from bot_squad_worker import tg_topics as _tg_topics
    result = _send_stakeholder_dm(
        cfg, message=body, sid=sid, tg_chat_id=chat_id,
        tg_topic_id=_tg_topics.resolve(cfg, slug, "team_queries"),
        group_record=True,
    )
    sent = result["sent"]

    if not sent and getattr(cfg, "tg_bot_token", ""):
        # A token IS configured but the post was suppressed — almost always
        # quiet hours (the stakeholder is asleep). Leave the marker pending,
        # un-escalated, so the next tick after quiet hours delivers exactly one
        # ping when he wakes. Still flood-safe: once a post lands we flip the
        # flag below and never retry.
        log.info("tg_stall: %s escalation suppressed (quiet hours?) — will retry", sid)
        return False

    # Sent (or no token configured at all — nothing more we can do): consume the
    # marker so a long block never re-floods.
    data["escalated"] = True
    data["escalated_at"] = time.time()
    tmp = marker.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(marker)
    log.info("tg_stall: escalated %s to TG (sent=%s)", sid, sent)
    return True
