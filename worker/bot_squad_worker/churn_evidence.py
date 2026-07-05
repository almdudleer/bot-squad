"""T-0613 — session-churn evidence collector + gate verdicts + churn metrics.

The stakeholder's T-0612 §0 gate: session churn (compact-spam / "new
incarnation" respawns / runaway spawn) must PROVABLY stop before bot-squad
touches watchrobot. This module turns the EXISTING lifecycle surfaces (no new
emitters) into that proof — or into evidence-cited FAIL verdicts:

  * ``<data>/<slug>/_worker/lifecycle/<sid>.json`` — the engine events
    :mod:`lifecycle_events` records (``session_timeout`` = a recycle armed,
    ``session_recycled`` = terminate-and-remember finalized);
  * ``<data>/<slug>/sessions/*.md`` — the registry transitions (``started_at``
    = spawn, ``suspended_at`` + ``suspend_source`` = exit/terminate).

:func:`collect` merges both into ONE chronological churn log; :func:`verdicts`
judges the stakeholder's exact gate semantics over it:

  (a) **compact-once-in-place** — a recycle episode arms ONCE (one
      ``session_timeout`` ≈ one ``/compact`` send when over the token
      threshold) and finalizes. Two arms in one episode = the double-compact
      he explicitly banned ("второй раз поверх этого компакт уже не надо").
  (b) **no auto-incarnation respawn** — a recycle/crash-terminated window must
      NOT respawn by itself; the ONLY sanctioned recycle→spawn chain is the
      operator re-drive (``owner: operator-redrive``, T-0474). A redispatch
      after a ``graceful-exit`` (work done, operator re-staffed the lane) is
      need-driven, not an incarnation respawn.
  (c) **no runaway spawn / token leak** — spawn bursts stay bounded
      (:data:`RUNAWAY_SPAWNS_PER_HOUR` per sliding hour) and no session sits
      in a re-compact loop (repeated arms with NO finalize — a ``/compact``
      per idle window forever, the live p8 shape from the 2026-07-05 manual
      walkthrough, scenarios/T-0613).

Also the SSOT for the standing churn METRICS the T-0613 monitor routines
watch (declared via ``routines.declare(trigger="monitor")``; their shell
probes are self-contained one-liners mirroring these functions, because a
monitor probe runs inside the DEPLOYED worker which may predate this module):

  * :func:`spawn_count` — sessions started in the trailing window;
  * :func:`live_session_count` — registry mds at ``status: active``.

Read-only: this module never writes to the data dir. Every reader fails soft
(a corrupt doc/md is skipped) so one bad file can't break the evidence sweep.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: verdict (c) bound: more spawns than this in ANY sliding hour = runaway.
#: Sized above the observed healthy peak (10 task-dispatch spawns/h during the
#: stakeholder's 2026-07-05 morning ticket wave, every one task-bound and
#: graceful-exited) — while the pathological churn this guards against (the
#: 2026-06-29 recovery loop) burned p-numbers per MINUTE. The standing monitor
#: uses the same figure.
RUNAWAY_SPAWNS_PER_HOUR = 12

#: a new timeout-arm this long after the previous one is a NEW recycle episode
#: (the session idled through another full cache window), not a double-arm of
#: the same attempt — arms of ONE stuck attempt re-fire on the ~60s tick.
EPISODE_ARM_GAP_S = 3600

#: verdict (b): a spawn this soon after a recycle/crash-terminate of the SAME
#: window is treated as that window's next incarnation and needs attribution.
RESPAWN_ATTRIBUTION_WINDOW_S = 900

#: suspend sources that mean "the lifecycle engine terminated this session"
#: (vs. a graceful self-exit / manual close). A spawn chained onto one of
#: these must be operator-redrive-owned to be sanctioned.
_ENGINE_TERMINATE_SOURCES = frozenset({"idle_timeout", "autocompact", "recovery"})


def _parse_iso_epoch(v: Any) -> Optional[float]:
    """``2026-07-05T02:19:22Z`` (the registry/event stamp shape) → epoch,
    None for absent / ``~`` / unparseable."""
    if not v or v == "~":
        return None
    try:
        return datetime.strptime(str(v).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _window_of_sid(sid: str) -> str:
    """SID → window segment (``S-<user>-<window>-p<N>``) — the same derivation
    the session_start hook uses, for events that carry no md window."""
    base = sid.rsplit("-p", 1)[0] if "-p" in sid else sid
    parts = base.split("-", 2)
    return parts[-1] if len(parts) == 3 else base


# ---------------------------------------------------------------------------
# Collection — one chronological churn log from the existing surfaces
# ---------------------------------------------------------------------------

def _iter_session_mds(cfg: Any, slug: str):
    """Yield ``(md_meta: dict)`` per parseable ``sessions/*.md``. Fails soft."""
    from bot_squad_worker import frontmatter as _fm

    d = cfg.data_dir / slug / "sessions"
    if not d.exists():
        return
    try:
        files = sorted(d.glob("*.md"))
    except OSError:
        return
    for f in files:
        try:
            parsed = _fm.parse_or_none(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed:
            continue
        meta = parsed[0] or {}
        if meta.get("sid"):
            yield meta


def _md_transitions(cfg: Any, slug: str) -> list[dict]:
    """Registry transitions: ``session_spawned`` (started_at) and
    ``session_suspended`` (suspended_at + suspend_source) per session md."""
    out: list[dict] = []
    for meta in _iter_session_mds(cfg, slug):
        sid = str(meta.get("sid"))
        window = str(meta.get("window") or _window_of_sid(sid))
        owner = str(meta.get("owner") or "") or None
        spawned = _parse_iso_epoch(meta.get("started_at"))
        if spawned is not None:
            out.append({"kind": "session_spawned", "sid": sid, "epoch": spawned,
                        "at": _iso(spawned), "window": window, "owner": owner,
                        "task_id": meta.get("task_id") or None})
        suspended = _parse_iso_epoch(meta.get("suspended_at"))
        if suspended is not None:
            out.append({"kind": "session_suspended", "sid": sid,
                        "epoch": suspended, "at": _iso(suspended),
                        "window": window, "owner": owner,
                        "source": str(meta.get("suspend_source") or "") or None})
    return out


def _lifecycle_events(cfg: Any, slug: str) -> list[dict]:
    """Engine events from ``_worker/lifecycle/*.json`` histories, normalized to
    the merged-log shape (kind/sid/epoch/at + the event's own fields)."""
    out: list[dict] = []
    d = cfg.data_dir / slug / "_worker" / "lifecycle"
    if not d.exists():
        return out
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        sid = str(doc.get("sid") or f.stem)
        for entry in (doc.get("history") or []):
            if not isinstance(entry, dict):
                continue
            kind = entry.get("event")
            epoch = entry.get("epoch")
            if epoch is None:
                epoch = _parse_iso_epoch(entry.get("at"))
            if not kind or epoch is None:
                continue
            ev = {k: v for k, v in entry.items() if k not in ("event", "epoch")}
            ev.update({"kind": str(kind), "sid": sid, "epoch": float(epoch),
                       "at": entry.get("at") or _iso(float(epoch)),
                       "window": _window_of_sid(sid)})
            out.append(ev)
    return out


def collect(cfg: Any, slug: str, *, since: float, until: float) -> list[dict]:
    """The chronological churn log for ``slug`` within ``[since, until]``:
    every spawn / suspend / timeout-arm / recycle, sorted by epoch (ties break
    by sid then kind so the order is deterministic)."""
    events = [e for e in (_md_transitions(cfg, slug) + _lifecycle_events(cfg, slug))
              if since <= e["epoch"] <= until]
    events.sort(key=lambda e: (e["epoch"], e["sid"], e["kind"]))
    return events


# ---------------------------------------------------------------------------
# Verdicts — the stakeholder's exact gate semantics (T-0612 §0)
# ---------------------------------------------------------------------------

def _episodes(events: list[dict]) -> dict[str, list[dict]]:
    """Group timeout-arms into recycle EPISODES per SID: consecutive
    ``session_timeout`` events until a ``session_recycled`` closes the episode.
    Returns ``{sid: [{arms: [event, ...], recycled: event|None}, ...]}`` — a
    trailing episode with ``recycled=None`` is dangling (armed, no finalize)."""
    per_sid: dict[str, list[dict]] = {}
    open_ep: dict[str, dict] = {}
    for e in events:
        sid = e["sid"]
        if e["kind"] == "session_timeout":
            ep = open_ep.get(sid)
            if ep and e["epoch"] - ep["arms"][-1]["epoch"] > EPISODE_ARM_GAP_S:
                # a fresh idle window crossed since the last arm — close the
                # stale attempt as dangling and start a new episode
                per_sid.setdefault(sid, []).append(open_ep.pop(sid))
                ep = None
            if ep is None:
                ep = open_ep.setdefault(sid, {"arms": [], "recycled": None})
            ep["arms"].append(e)
        elif e["kind"] == "session_recycled":
            ep = open_ep.pop(sid, {"arms": [], "recycled": None})
            ep["recycled"] = e
            per_sid.setdefault(sid, []).append(ep)
    for sid, ep in open_ep.items():
        per_sid.setdefault(sid, []).append(ep)
    return per_sid


def _verdict_a(episodes: dict[str, list[dict]]) -> dict:
    """(a) compact-once-in-place: one arm per recycle episode."""
    violations: list[str] = []
    for sid, eps in episodes.items():
        for ep in eps:
            if len(ep["arms"]) > 1:
                arms = ", ".join(a["at"] for a in ep["arms"])
                closed = (f"recycled {ep['recycled']['at']}" if ep["recycled"]
                          else "NO finalize")
                violations.append(
                    f"{sid}: {len(ep['arms'])} timeout arms in one episode "
                    f"({arms}; {closed}) — each over-threshold arm sends "
                    f"/compact, so this is a double-compact")
    return {"pass": not violations, "violations": violations}


def _verdict_b(events: list[dict]) -> dict:
    """(b) no auto-incarnation respawn: a spawn within
    :data:`RESPAWN_ATTRIBUTION_WINDOW_S` of an engine-terminate of the same
    window is sanctioned ONLY when operator-redrive-owned."""
    violations: list[str] = []
    sanctioned: list[str] = []
    # engine-terminate moments per window (recycled events + engine suspends)
    terminated: list[tuple[float, str, str]] = []  # (epoch, window, desc)
    for e in events:
        if e["kind"] == "session_recycled":
            terminated.append((e["epoch"], e["window"],
                               f"{e['sid']} recycled {e['at']}"))
        elif (e["kind"] == "session_suspended"
              and (e.get("source") or "") in _ENGINE_TERMINATE_SOURCES):
            terminated.append((e["epoch"], e["window"],
                               f"{e['sid']} suspended ({e['source']}) {e['at']}"))
    for e in events:
        if e["kind"] != "session_spawned":
            continue
        chained = [d for (t, w, d) in terminated
                   if w == e["window"] and e["sid"] not in d
                   and 0 <= e["epoch"] - t <= RESPAWN_ATTRIBUTION_WINDOW_S]
        if not chained:
            continue
        desc = (f"{e['sid']} spawned {e['at']} after " + "; ".join(chained))
        if (e.get("owner") or "") == "operator-redrive":
            sanctioned.append(f"{desc} — operator-redrive (T-0474, sanctioned)")
        else:
            violations.append(
                f"{desc} — owner={e.get('owner') or '~'}: unsanctioned "
                f"incarnation respawn")
    return {"pass": not violations, "violations": violations,
            "sanctioned": sanctioned}


def _verdict_c(events: list[dict], episodes: dict[str, list[dict]]) -> dict:
    """(c) no runaway spawn / token leak: bounded spawn bursts + no
    re-compact loop (>=2 arms with no finalize on one SID)."""
    violations: list[str] = []
    spawns = sorted(e["epoch"] for e in events if e["kind"] == "session_spawned")
    worst, start_i = 0, 0
    for i, t in enumerate(spawns):
        while t - spawns[start_i] > 3600:
            start_i += 1
        worst = max(worst, i - start_i + 1)
    if worst > RUNAWAY_SPAWNS_PER_HOUR:
        violations.append(
            f"{worst} spawns inside one sliding hour "
            f"(bound {RUNAWAY_SPAWNS_PER_HOUR}) — runaway spawn")
    for sid, eps in episodes.items():
        dangling_arms = [a for ep in eps if ep["recycled"] is None
                         for a in ep["arms"]]
        if len(dangling_arms) >= 2:
            arms = ", ".join(a["at"] for a in dangling_arms)
            violations.append(
                f"{sid}: {len(dangling_arms)} timeout arms with no finalize "
                f"({arms}) — re-compact loop, a /compact per idle window = "
                f"token leak")
    return {"pass": not violations, "violations": violations,
            "max_hourly_spawns": worst, "total_spawns": len(spawns)}


def verdicts(events: list[dict], *, now: float) -> dict:
    """The T-0612 §0 gate verdict table over a collected churn log."""
    eps = _episodes(events)
    return {"a": _verdict_a(eps), "b": _verdict_b(events),
            "c": _verdict_c(events, eps)}


# ---------------------------------------------------------------------------
# Standing churn metrics (SSOT the T-0613 monitor probes mirror)
# ---------------------------------------------------------------------------

def spawn_count(cfg: Any, slug: str, *, window_s: int = 3600,
                now: Optional[float] = None) -> int:
    """Sessions whose ``started_at`` falls inside the trailing ``window_s``."""
    import time as _time
    now_e = _time.time() if now is None else now
    n = 0
    for meta in _iter_session_mds(cfg, slug):
        t = _parse_iso_epoch(meta.get("started_at"))
        if t is not None and 0 <= now_e - t <= window_s:
            n += 1
    return n


def live_session_count(cfg: Any, slug: str) -> int:
    """Registry mds currently at ``status: active``."""
    return sum(1 for meta in _iter_session_mds(cfg, slug)
               if str(meta.get("status") or "").strip() == "active")


# ---------------------------------------------------------------------------
# Report rendering + CLI
# ---------------------------------------------------------------------------

_VERDICT_TITLES = (
    ("a", "(a) compact-once-in-place"),
    ("b", "(b) no-auto-incarnation-respawn"),
    ("c", "(c) no-runaway-spawn/token-leak"),
)


def render_md(slug: str, events: list[dict], v: dict, *, since: float,
              until: float) -> str:
    """Markdown churn report: chronological log + evidence-cited verdict table."""
    lines = [
        f"# Session-churn evidence — {slug}",
        f"window: {_iso(since)} .. {_iso(until)} · events: {len(events)}",
        "",
        "## Chronological churn log",
        "",
        "| at (UTC) | sid | event | detail |",
        "|---|---|---|---|",
    ]
    for e in events:
        detail = ", ".join(
            f"{k}={e[k]}" for k in ("reason", "cause", "compacted", "source",
                                    "owner", "task_id")
            if e.get(k) not in (None, ""))
        lines.append(f"| {e['at']} | {e['sid']} | {e['kind']} | {detail} |")
    lines += ["", "## Gate verdicts (T-0612 §0)", "",
              "| gate semantic | verdict | evidence |", "|---|---|---|"]
    for key, title in _VERDICT_TITLES:
        vd = v[key]
        verdict = "PASS" if vd["pass"] else "FAIL"
        cites = vd["violations"] or vd.get("sanctioned") or ["no violations"]
        lines.append(f"| {title} | {verdict} | {'<br>'.join(cites)} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m bot_squad_worker.churn_evidence --project <slug> [...]``"""
    import time as _time
    from bot_squad_worker.config import Config

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="/home/www/bot-squad/config")
    ap.add_argument("--project", required=True)
    ap.add_argument("--since", help="ISO Z timestamp (default: 24h ago)")
    ap.add_argument("--until", help="ISO Z timestamp (default: now)")
    ap.add_argument("--json", action="store_true", help="raw events + verdicts")
    ap.add_argument("--metric", choices=("spawn_rate_1h", "live_sessions"),
                    help="print ONE number (monitor-probe mode) and exit")
    args = ap.parse_args(argv)

    cfg = Config.load(Path(args.config))
    if args.metric:
        if args.metric == "spawn_rate_1h":
            print(spawn_count(cfg, args.project, window_s=3600))
        else:
            print(live_session_count(cfg, args.project))
        return 0

    now = _time.time()
    until = _parse_iso_epoch(args.until) or now
    since = _parse_iso_epoch(args.since) or (until - 86400)
    events = collect(cfg, args.project, since=since, until=until)
    v = verdicts(events, now=now)
    if args.json:
        print(json.dumps({"events": events, "verdicts": v}, indent=1))
    else:
        print(render_md(args.project, events, v, since=since, until=until))
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via tests on main()
    raise SystemExit(main())
