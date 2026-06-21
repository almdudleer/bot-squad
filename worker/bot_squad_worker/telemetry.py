"""T-0210: resource telemetry — per-session context-token usage, memory
footprint, and a project-level quota-burndown estimate, with crossing-only
threshold alerts to the operator + each TL.

Research (full notes on the ticket) bottomed out three data sources:

* **Context usage** — the Claude transcript jsonl. The number that grows
  toward the compaction ceiling (``context_ceiling()``; default 700k, T-0210)
  is the *latest* ``type:"assistant"``
  line's ``usage.input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens`` (cache_read already carries the running prior
  context, so the last turn's input side ≈ the live window size). The
  transcript is joined to a bot-squad SID via the session-md ``claude_uuid``
  field → ``<claude_uuid>.jsonl`` (found by glob so a symlinked cwd can't
  break the path). Transcripts reach tens of MB, so we never full-read: the
  first sample tail-reads the last chunk for the current window, and every
  subsequent sample reads only the bytes appended since the last offset.

* **Quota burndown** — Max-plan remaining quota is NOT queryable anywhere on
  disk (``.credentials.json`` carries only the static plan tier;
  ``stats-cache.json`` is a stale daily aggregate). The only hard live signal
  is reactive: a 429 ``rate_limit`` ``isApiErrorMessage`` line that appears in
  a transcript *after* a session hits the cap. So we estimate burn from the
  output tokens accrued across live sessions since telemetry started, and
  project a time-to-exhaustion only when the operator has set an (optional)
  budget anchor in ``system_settings.toml [quota]``. The 429 throttle flag is
  surfaced regardless.

* **Memory usage** — per agent, the project memory dir (``memory/*.md`` +
  ``MEMORY.md``): file count + byte size + a ~bytes/4 token estimate.

All alerts are SYSTEM pings → ``urgent=True`` so the quiet-hours gate (17–05
UTC) can't silently drop them, and they fire only when a threshold is *newly
crossed* (the per-session record carries the last alert level) so the operator
isn't re-pinged every 60s.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot_squad_worker import autocompact

log = logging.getLogger(__name__)

# --- thresholds (T-0210; stakeholder 2026-06-19: 700k context ceiling) -------
# The ceiling is the 100% / "compact now" line; warn fires at CONTEXT_WARN_RATIO
# of it (0.8 preserves the historical 400k:500k ratio → 560k at the 700k default).
# Tunable per-install WITHOUT a redeploy via the BOT_SQUAD_CONTEXT_CEILING env
# (read per-call), so the threshold can be retuned with a worker env change.
DEFAULT_CONTEXT_CEILING = 700_000
CONTEXT_WARN_RATIO = 0.8
MEMORY_WARN_TOKENS = 40_000  # per-agent memory footprint warn line


def context_ceiling() -> int:
    """Context-window ceiling in tokens (100% / "compact now"). Overridable via
    ``BOT_SQUAD_CONTEXT_CEILING``; non-positive/garbage values fall back to the
    default so a bad env can never disable the alerts."""
    raw = os.environ.get("BOT_SQUAD_CONTEXT_CEILING")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_CONTEXT_CEILING


def context_urgent() -> int:
    """Tokens at/above which context is 'urgent' (= the ceiling)."""
    return context_ceiling()


def context_warn() -> int:
    """Tokens at/above which context is 'warn' (CONTEXT_WARN_RATIO of ceiling)."""
    return int(context_ceiling() * CONTEXT_WARN_RATIO)

# First-sample tail size: enough to contain at least one full assistant turn.
_TAIL_BYTES = 256 * 1024
# Rolling burn-rate window: keep this many (ts, cum) samples in _quota.json.
_BURN_SAMPLES = 60          # ~1h at the 60s cadence
_BURN_WINDOW_SECONDS = 1800  # compute burn over the last 30 min of samples

_LEVELS = {"none": 0, "warn": 1, "urgent": 2}

# --- alert debounce (T-0332) -------------------------------------------------
# A compact spawns a fresh transcript, so a session's context resets to ~0 and
# climbs back through the same threshold — a genuine "crossing" each cycle. With
# many parallel sessions compacting all day that re-fired the same alert every
# few minutes. So even on a real crossing we suppress a re-ping of the SAME
# (kind, level) within a cooldown window. Env-tunable per-install.
DEFAULT_ALERT_COOLDOWN_SEC = 3 * 3600  # one alert per session per threshold / 3h


def alert_cooldown_sec() -> int:
    """Per-(session, threshold-level) alert cooldown in seconds. Overridable via
    ``BOT_SQUAD_ALERT_COOLDOWN_SEC``; non-positive/garbage falls back to default."""
    raw = os.environ.get("BOT_SQUAD_ALERT_COOLDOWN_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_ALERT_COOLDOWN_SEC


def cooldown_ok(fired_at: dict, key: str, now: float, window: int | None = None) -> bool:
    """True when ``key`` may fire now: never fired, or the window has elapsed.

    ``key`` encodes (kind, level) e.g. ``"context:warn"`` so an escalation to a
    higher level (a different key) is NEVER gated by the lower level's timer.
    """
    if window is None:
        window = alert_cooldown_sec()
    last = fired_at.get(key)
    if last is None:
        return True
    try:
        return (now - float(last)) >= window
    except (TypeError, ValueError):
        return True


# Which (kind, level) alerts are urgent enough to bypass the quiet-hours gate.
# Context is NOT here — it no longer alerts a human at all (T-0333 auto-compacts
# instead). Memory is non-urgent (quiet hours respected); only a genuine
# human-decision (quota-EOD pacing, live 429 throttle) bypasses quiet hours.
_URGENT_ALERTS = {("quota", "urgent"), ("throttle", "urgent")}


def alert_urgent(kind: str, level: str) -> bool:
    """True when a (kind, level) alert should bypass quiet hours (urgent=True)."""
    return (kind, level) in _URGENT_ALERTS


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Transcript parsing (pure, unit-testable)
# ---------------------------------------------------------------------------

def _usage_window_tokens(usage: dict) -> int:
    """Live context-window occupancy from one assistant ``usage`` block."""
    return (
        int(usage.get("input_tokens", 0))
        + int(usage.get("cache_read_input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0))
    )


def scan_lines(lines: Iterable[str]) -> dict:
    """Scan jsonl text lines → {last_window, model, output_sum, saw_429}.

    ``last_window`` / ``model`` come from the LAST assistant line carrying a
    ``usage`` block (None when the chunk holds no such line). ``output_sum`` is
    the total ``output_tokens`` across assistant lines in the chunk (used to
    accrue burn). ``saw_429`` is True if any line is a 429 rate_limit api-error.
    """
    last_window: int | None = None
    model: str | None = None
    output_sum = 0
    saw_429 = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if d.get("error") == "rate_limit" and d.get("apiErrorStatus") == 429:
            saw_429 = True
        if d.get("type") != "assistant":
            continue
        msg = d.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        last_window = _usage_window_tokens(usage)
        if isinstance(msg, dict) and msg.get("model"):
            model = str(msg.get("model"))
        output_sum += int(usage.get("output_tokens", 0))
    return {
        "last_window": last_window,
        "model": model,
        "output_sum": output_sum,
        "saw_429": saw_429,
    }


def _read_chunk(path: Path, offset: int) -> tuple[str, int]:
    """Read complete (newline-terminated) lines from ``offset`` → EOF.

    Returns ``(text, new_offset)`` where ``new_offset`` advances only past the
    last complete line, so a half-written final line is re-read next tick
    rather than lost or parsed partial.
    """
    with path.open("rb") as fh:
        fh.seek(offset)
        raw = fh.read()
    if not raw:
        return "", offset
    cut = raw.rfind(b"\n")
    if cut == -1:
        # No complete line yet — don't advance.
        return "", offset
    complete = raw[: cut + 1]
    return complete.decode("utf-8", "replace"), offset + len(complete)


def find_transcript(home: str, claude_uuid: str) -> Path | None:
    """Locate ``<claude_uuid>.jsonl`` under a user's ~/.claude/projects.

    Globs across project dirs so a symlinked cwd (the dev clone points at the
    mgmt clone, which is how the project dir gets encoded) can't break the
    path. Returns None if no transcript exists yet.
    """
    if not claude_uuid:
        return None
    base = Path(home) / ".claude" / "projects"
    if not base.exists():
        return None
    hits = list(base.glob(f"*/{claude_uuid}.jsonl"))
    return hits[0] if hits else None


# ---------------------------------------------------------------------------
# Memory footprint (pure-ish, unit-testable)
# ---------------------------------------------------------------------------

def memory_stats(memory_dir: Path) -> dict:
    """Count + byte/token-size the agent memory files in ``memory_dir``.

    Includes ``MEMORY.md`` and every ``*.md`` under the dir. Token estimate is
    ~bytes/4 (no tiktoken dep in the worker). Missing dir → zeroes.
    """
    files = 0
    total = 0
    if memory_dir.exists():
        for md in sorted(memory_dir.glob("*.md")):
            try:
                total += md.stat().st_size
                files += 1
            except OSError:
                continue
    return {"files": files, "bytes": total, "tokens_est": total // 4}


# ---------------------------------------------------------------------------
# Alert-level helpers (pure)
# ---------------------------------------------------------------------------

def context_level(tokens: int) -> str:
    if tokens >= context_urgent():
        return "urgent"
    if tokens >= context_warn():
        return "warn"
    return "none"


def memory_level(tokens_est: int) -> str:
    return "warn" if tokens_est >= MEMORY_WARN_TOKENS else "none"


def crossed(prev: str, new: str) -> bool:
    """True when ``new`` is a strictly worse level than ``prev`` (a crossing)."""
    return _LEVELS.get(new, 0) > _LEVELS.get(prev, 0)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _telemetry_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "telemetry"


def _record_path(cfg: Any, slug: str, sid: str) -> Path:
    safe = sid.replace("/", "_")
    return _telemetry_dir(cfg, slug) / f"{safe}.json"


def _quota_path(cfg: Any, slug: str) -> Path:
    return _telemetry_dir(cfg, slug) / "_quota.json"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.rename(tmp, path)


# ---------------------------------------------------------------------------
# Quota burndown (pure)
# ---------------------------------------------------------------------------

def compute_burn(samples: list[list], window_seconds: int = _BURN_WINDOW_SECONDS) -> float | None:
    """Tokens/hour over the most recent ``window_seconds`` of (ts, cum) samples.

    Returns None when there's < 2 samples or no positive time span.
    """
    if len(samples) < 2:
        return None
    latest_ts = samples[-1][0]
    window = [s for s in samples if latest_ts - s[0] <= window_seconds]
    if len(window) < 2:
        window = samples[-2:]
    t0, c0 = window[0]
    t1, c1 = window[-1]
    dt = t1 - t0
    if dt <= 0:
        return None
    return max(0.0, (c1 - c0)) / dt * 3600.0


def project_exhaustion(anchor: dict | None, output_since_anchor: int, burn_per_hr: float | None) -> tuple[int | None, str | None]:
    """Return (remaining_tokens, projected_exhaustion_iso).

    Requires an operator-set anchor ``{budget_tokens, set_at}`` AND a positive
    burn rate. Without an anchor both are None ("anchor unset" — the UI shows
    burn rate + 429 flag regardless).
    """
    if not anchor:
        return None, None
    budget = int(anchor.get("budget_tokens", 0))
    if budget <= 0:
        return None, None
    remaining = max(0, budget - output_since_anchor)
    if not burn_per_hr or burn_per_hr <= 0:
        return remaining, None
    hours_left = remaining / burn_per_hr
    eta = datetime.now(timezone.utc).timestamp() + hours_left * 3600.0
    eta_iso = datetime.fromtimestamp(eta, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return remaining, eta_iso


def _read_anchor(cfg: Any) -> dict | None:
    """Read the optional ``[quota]`` anchor fresh from system_settings.toml.

    Read per-tick (not from the once-loaded cfg) so the operator can set/clear
    the anchor without a worker restart. Shape: ``{budget_tokens, set_at}``.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # py<3.11 fallback (worker ships 3.11+)
        return None
    path = Path(cfg.config_dir) / "system_settings.toml"
    if not path.exists():
        return None
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return None
    q = raw.get("quota") or {}
    budget = q.get("budget_tokens")
    if not budget:
        return None
    return {"budget_tokens": int(budget), "set_at": str(q.get("set_at", ""))}


def _projected_before_eod(eta_iso: str | None) -> bool:
    """True when the projected exhaustion lands before end-of-day UTC."""
    if not eta_iso:
        return False
    try:
        eta = datetime.strptime(eta_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    eod = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return eta <= eod


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def sample(cfg: Any, slug: str) -> dict:
    """Sample every LIVE session's telemetry for ``slug``; persist + alert.

    Returns a summary dict (used by tests + the action surface). Per-session
    errors are swallowed so one bad transcript never kills the sweep.
    """
    from bot_squad_worker import sessions as _sessions

    try:
        rows = _sessions.list_sessions(cfg, slug)
    except Exception:
        log.exception("telemetry.sample: list_sessions failed for %s", slug)
        return {"ok": False, "slug": slug, "sampled": 0}

    home = _sessions._get_user_home()
    cur_user = _sessions._get_current_user()
    now = time.time()

    sampled: list[dict] = []
    total_output_cum = 0
    any_429 = False
    last_429_at: str | None = None

    for row in rows:
        if row.get("status") != "active":
            continue
        sid = row.get("sid")
        claude_uuid = row.get("claude_uuid")
        if not sid or not claude_uuid:
            continue
        # Per-user worker reads its own ~/.claude only — skip other users'
        # sessions (their transcripts live under a home we can't read).
        if (row.get("linux_user") or cur_user) != cur_user:
            continue
        try:
            rec = _sample_one(cfg, slug, row, home, now)
        except Exception:
            log.exception("telemetry.sample: %s failed", sid)
            continue
        if rec is None:
            continue
        sampled.append(rec)
        total_output_cum += int(rec.get("output_tokens_cum", 0))
        if rec.get("rate_limited"):
            any_429 = True
            last_429_at = rec.get("sampled_at")

    quota = _update_quota(cfg, slug, total_output_cum, now, any_429, last_429_at)

    # Operator SIDs (role=operator) for TARGETED delivery — stakeholder
    # guardrail (2026-06-18): alerts are targeted peer_sends to the specific
    # operator SID + each specific TL SID, NEVER a broadcast (role=all), which
    # historically interrupted every live session.
    operator_sids = [r.get("sid") for r in rows
                     if r.get("role") == "operator" and r.get("sid")]

    # Alerts (crossing-only). Done after persistence so a crash mid-alert
    # doesn't re-fire next tick.
    try:
        _fire_alerts(cfg, slug, sampled, quota, operator_sids, now)
    except Exception:
        log.exception("telemetry.sample: alerting failed for %s", slug)

    return {"ok": True, "slug": slug, "sampled": len(sampled), "quota": quota}


def _sample_one(cfg: Any, slug: str, row: dict, home: str, now: float) -> dict | None:
    sid = row["sid"]
    claude_uuid = row["claude_uuid"]
    prev = _read_json(_record_path(cfg, slug, sid)) or {}
    prev_uuid = prev.get("claude_uuid")
    fresh = prev_uuid != claude_uuid  # resume/compact spawned a new transcript

    transcript = find_transcript(home, claude_uuid)
    context_tokens = int((prev.get("context") or {}).get("tokens", 0)) if not fresh else 0
    model = (prev.get("context") or {}).get("model") if not fresh else None
    output_cum = int(prev.get("output_tokens_cum", 0)) if not fresh else 0
    offset = int(prev.get("transcript_offset", 0)) if not fresh else 0
    saw_429 = False
    memory = {"files": 0, "bytes": 0, "tokens_est": 0}

    if transcript is not None:
        try:
            size = transcript.stat().st_size
        except OSError:
            size = 0
        if fresh or offset == 0 or offset > size:
            # First sample for this transcript: tail-read for the current
            # window only and start accruing burn from here (don't re-read MBs
            # of history). offset jumps to EOF.
            start = max(0, size - _TAIL_BYTES)
            text, _ = _read_chunk(transcript, start)
            scan = scan_lines(text.splitlines())
            if scan["last_window"] is not None:
                context_tokens = scan["last_window"]
                model = scan["model"] or model
            # T-0332: do NOT re-detect a 429 from the fresh tail-read — a stale
            # marker already in the tail (common after a compact spawns a new
            # transcript) would re-fire "RATE LIMITED NOW" every cycle. Only a
            # 429 seen on a later INCREMENTAL read is genuinely new.
            saw_429 = False
            offset = size
        else:
            text, offset = _read_chunk(transcript, offset)
            if text:
                scan = scan_lines(text.splitlines())
                if scan["last_window"] is not None:
                    context_tokens = scan["last_window"]
                    model = scan["model"] or model
                output_cum += scan["output_sum"]
                saw_429 = scan["saw_429"]
        memory = memory_stats(transcript.parent / "memory")

    pct = round(context_tokens / context_ceiling() * 100.0, 1)
    rec = {
        "sid": sid,
        "claude_uuid": claude_uuid,
        "linux_user": row.get("linux_user", ""),
        "role": row.get("role", ""),
        "task_id": row.get("task_id"),
        "tmux_session": row.get("tmux_session", ""),
        # T-0333: pane activity (running|idle|paused) gates the auto-compact loop
        # — only an idle pane is safe to /compact.
        "activity": row.get("activity", ""),
        "sampled_at": _now_iso(),
        "context": {
            "tokens": context_tokens,
            "pct": pct,
            "ceiling": context_ceiling(),
            "model": model,
        },
        "memory": memory,
        "output_tokens_cum": output_cum,
        "transcript_offset": offset,
        "rate_limited": saw_429,
        "last_alert": prev.get("last_alert") or {"context": "none", "memory": "none"},
        # Per-(kind:level) last-fire epochs — preserved across a compact (fresh
        # uuid) so the debounce survives the transcript rotation that re-arms the
        # crossing. (T-0332)
        "alert_fired_at": prev.get("alert_fired_at") or {},
    }
    _write_json(_record_path(cfg, slug, sid), rec)
    return rec


def _update_quota(
    cfg: Any, slug: str, total_output_cum: int, now: float,
    any_429: bool, last_429_at: str | None,
) -> dict:
    q = _read_json(_quota_path(cfg, slug)) or {}
    samples: list[list] = q.get("samples") or []
    samples.append([now, total_output_cum])
    samples = samples[-_BURN_SAMPLES:]

    burn = compute_burn(samples)
    anchor = _read_anchor(cfg)
    output_since_anchor = total_output_cum  # cum resets when telemetry started
    remaining, eta = project_exhaustion(anchor, output_since_anchor, burn)

    rl = q.get("rate_limit_429") or {"count": 0, "last_at": None}
    if any_429:
        rl = {"count": int(rl.get("count", 0)) + 1, "last_at": last_429_at}

    out = {
        "sampled_at": _now_iso(),
        "burn_tokens_per_hr": round(burn, 1) if burn is not None else None,
        "output_tokens_cum_total": total_output_cum,
        "samples": samples,
        "anchor": anchor,
        "remaining_tokens": remaining,
        "projected_exhaustion_at": eta,
        "rate_limit_429": rl,
        "throttled": bool(any_429),
        "last_alert": q.get("last_alert") or {"quota": "none", "throttle_seen": None},
    }
    _write_json(_quota_path(cfg, slug), out)
    return out


# ---------------------------------------------------------------------------
# Alerting (crossing-only, urgent=True)
# ---------------------------------------------------------------------------

def _human_tg(cfg: Any, slug: str, text: str, urgent: bool = True) -> None:
    """TG ping to the project's (single) stakeholder chat.

    This is the human channel — one bound chat, not a broadcast. ``urgent``
    bypasses the quiet-hours gate (17–05 UTC); T-0332 makes only genuinely-urgent
    crossings (compact-now / quota-EOD / 429) urgent, so warn/memory alerts are
    quiet-hours-respecting and stop spamming the stakeholder overnight.
    """
    # T-0394: page the human via the _send_stakeholder_dm SSOT — MAX-primary
    # (TG is DPI-blocked here), TG failover, + best-effort #team-queries record.
    from bot_squad_worker.actions import _send_stakeholder_dm
    from bot_squad_worker import tg_topics as _tg_topics
    project = cfg.projects.get(slug)
    chat_id = getattr(project, "tg_chat", "") if project else ""
    if not chat_id:
        return
    try:
        _send_stakeholder_dm(
            cfg, message=text, sid="telemetry", urgent=urgent,
            tg_chat_id=chat_id, tg_topic_id=_tg_topics.resolve(cfg, slug, "team_queries"),
            group_record=True,
        )
    except Exception:
        log.exception("telemetry: human page failed (non-fatal): %s", text)


def _peer_to(cfg: Any, slug: str, sid: str, text: str) -> None:
    """TARGETED peer_send to one specific SID + a best-effort pane nudge.

    Stakeholder guardrail: never a role/broadcast target — only a literal SID,
    which ``intersession.send`` delivers without cross-session fan-out.
    """
    from bot_squad_worker import intersession as _is
    try:
        _is.send(cfg, slug, "S-telemetry", sid, text)
    except Exception:
        log.exception("telemetry: peer_send to %s failed (non-fatal)", sid)
        return
    # Best-effort realtime nudge into the recipient's pane, mirroring tg_stall —
    # the inbox write is the source of truth, so a missing pane never matters.
    try:
        from bot_squad_worker.actions import _action_inject_input
        _action_inject_input({"sid": sid, "text": "check mail"})
    except Exception:
        log.debug("telemetry: inject nudge to %s skipped (no live pane)", sid)


def _all_tls(cfg: Any, slug: str) -> list[str]:
    from bot_squad_worker import teams as _teams
    tls: list[str] = []
    teams_dir = cfg.data_dir / slug / "teams"
    if not teams_dir.exists():
        return tls
    for md in sorted(teams_dir.glob("*.md")):
        meta = _teams._read_team(md)
        if meta is None:
            continue
        tl = meta.get("tl")
        if tl and tl != "~" and tl not in tls:
            tls.append(tl)
    return tls


def _fire_alerts(
    cfg: Any, slug: str, sampled: list[dict], quota: dict,
    operator_sids: list[str], now: float,
) -> None:
    # --- per-session context + memory crossings ---
    # Fire on a crossing AND only if the same (kind:level) hasn't fired within the
    # cooldown window (T-0332 debounce — a compact re-arms the crossing; we don't
    # re-ping). Urgency is level-derived so warn/memory respect quiet hours.
    for rec in sampled:
        sid = rec["sid"]
        last = rec.get("last_alert") or {"context": "none", "memory": "none"}
        fired = rec.get("alert_fired_at") or {}
        ctx_tokens = rec["context"]["tokens"]
        ctx_new = context_level(ctx_tokens)
        mem_new = memory_level(rec["memory"]["tokens_est"])
        changed = False

        # T-0333/T-0334: context-high is SELF-HEALING — the SYSTEM /compacts the
        # over-ceiling session (when idle, composer-ready) instead of pinging a
        # human about routine resource management. No human is in this loop; no
        # session (incl. the operator, T-0334) is exempt from its own close.
        rec["alert_fired_at"] = fired  # share the cooldown dict with autocompact
        if autocompact.maybe_compact(cfg, slug, rec, ctx_new, now):
            fired = rec["alert_fired_at"]
            changed = True
        if last.get("context") != ctx_new:
            last["context"] = ctx_new
            changed = True

        mem_key = f"memory:{mem_new}"
        if crossed(last.get("memory", "none"), mem_new) and cooldown_ok(fired, mem_key, now):
            # T-0387: routine resource alert — operator/TL advisory only, NEVER
            # the human (humans only for decisions; same closed-loop as T-0333).
            _alert_session(
                cfg, slug, sid, operator_sids,
                f"🧠 memory footprint high — {sid} ~{rec['memory']['tokens_est']:,} tok "
                f"across {rec['memory']['files']} files. Consider pruning.",
                urgent=alert_urgent("memory", mem_new),
                human=False,
            )
            fired[mem_key] = now
            changed = True
        if last.get("memory") != mem_new:
            last["memory"] = mem_new
            changed = True

        if changed:
            rec["last_alert"] = last
            rec["alert_fired_at"] = fired
            _write_json(_record_path(cfg, slug, sid), rec)

    # --- project quota crossings (anchor projection + 429 throttle) ---
    qlast = quota.get("last_alert") or {"quota": "none", "throttle_seen": None}
    qfired = quota.get("alert_fired_at") or {}
    qchanged = False

    eta = quota.get("projected_exhaustion_at")
    quota_new = "urgent" if _projected_before_eod(eta) else "none"
    q_key = f"quota:{quota_new}"
    if crossed(qlast.get("quota", "none"), quota_new) and cooldown_ok(qfired, q_key, now):
        burn = quota.get("burn_tokens_per_hr")
        _alert_project(
            cfg, slug, operator_sids,
            f"⏳ QUOTA — projected to exhaust at {eta} (before EOD) "
            f"at ~{burn:,.0f} tok/hr. Pace the run / pause non-critical sessions.",
            urgent=alert_urgent("quota", quota_new),
        )
        qfired[q_key] = now
        qchanged = True
    if qlast.get("quota") != quota_new:
        qlast["quota"] = quota_new
        qchanged = True

    rl = quota.get("rate_limit_429") or {}
    last_seen = qlast.get("throttle_seen")
    if rl.get("last_at") and rl.get("last_at") != last_seen and cooldown_ok(qfired, "throttle:urgent", now):
        _alert_project(
            cfg, slug, operator_sids,
            f"🚫 RATE LIMITED — a session hit a 429 at {rl.get('last_at')} "
            f"(429 count {rl.get('count')}). Quota is being throttled NOW.",
            urgent=alert_urgent("throttle", "urgent"),
        )
        qlast["throttle_seen"] = rl.get("last_at")
        qfired["throttle:urgent"] = now
        qchanged = True

    if qchanged:
        quota["last_alert"] = qlast
        quota["alert_fired_at"] = qfired
        _write_json(_quota_path(cfg, slug), quota)


def _alert_session(
    cfg: Any, slug: str, sid: str, operator_sids: list[str], text: str,
    urgent: bool = True, human: bool = True,
) -> None:
    """Alert the operator SID(s) + the session's TL about a per-session crossing.

    All delivery is targeted (specific SIDs), plus the single human TG chat.
    The affected session itself is NOT pinged (it already sees its own context
    in-pane; pinging it would be the self-interrupt the guardrail forbids).

    T-0387: pass ``human=False`` for ROUTINE resource alerts (e.g. memory
    footprint) — they reach the operator/TL SIDs as a system-internal advisory
    but must NOT ping the human (closed-loop rule: humans only for decisions,
    same doctrine as T-0333 context auto-compact). Decision alerts (quota/
    throttle) keep ``human=True``.
    """
    from bot_squad_worker import teams as _teams
    if human:
        _human_tg(cfg, slug, text, urgent=urgent)
    targets: list[str] = list(operator_sids)
    tl = _teams.tl_for_sid(cfg, slug, sid)
    if tl:
        targets.append(tl)
    for target in dict.fromkeys(targets):  # dedupe, preserve order
        if target and target != sid:
            _peer_to(cfg, slug, target, text)


def _alert_project(
    cfg: Any, slug: str, operator_sids: list[str], text: str,
    urgent: bool = True,
) -> None:
    """Alert the operator SID(s) + every TL about a project-wide quota crossing.

    Targeted to each specific SID (never role=all), plus the human TG chat.
    """
    _human_tg(cfg, slug, text, urgent=urgent)
    targets = list(operator_sids) + _all_tls(cfg, slug)
    for target in dict.fromkeys(targets):  # dedupe, preserve order
        if target:
            _peer_to(cfg, slug, target, text)


def _live_sids(cfg: Any, slug: str) -> set:
    """SIDs of genuinely-live sessions for a project (T-0265).

    ``list_sessions`` enumerates every session and derives an authoritative
    ``status`` ('active' is pane-backed via ``list_panes`` — the same T-0326 pane
    truth as ``live_pane_map``, cwd-matched to the project, NOT the empty md
    ``pane_id`` field). Keeping only status active/paused drops the bulk of
    suspended ghosts (184 of 190 on bot-squad) down to the ~6 the board shows.
    """
    from bot_squad_worker.sessions import list_sessions
    try:
        return {
            r.get("sid") for r in list_sessions(cfg, slug)
            if r.get("sid") and str(r.get("status", "")).lower() in ("active", "paused")
        }
    except Exception:
        return set()


def read_telemetry(cfg: Any, slug: str) -> dict:
    """Read the persisted telemetry for a project (for the API / UI).

    Returns ``{"sessions": [record, ...], "quota": {...}}`` from the records the
    sampler last wrote, filtered to genuinely-live sessions (``_live_sids``) so
    the UI never shows ghosts/zombies. ``_quota.json``'s noisy internal fields
    (rolling samples, alert state) are stripped from the wire.
    """
    live = _live_sids(cfg, slug)
    tdir = _telemetry_dir(cfg, slug)
    sessions: list[dict] = []
    if tdir.exists():
        for rec_file in sorted(tdir.glob("*.json")):
            if rec_file.name in ("_quota.json", "_quota.json.tmp"):
                continue
            rec = _read_json(rec_file)
            if rec and rec.get("sid") in live:
                sessions.append(rec)
    quota = _read_json(_quota_path(cfg, slug)) or {}
    quota_wire = {
        k: v for k, v in quota.items()
        if k not in ("samples", "last_alert", "alert_fired_at")
    }
    return {"sessions": sessions, "quota": quota_wire}


def tick(cfg: Any) -> None:
    """Scheduler entry — sample telemetry for every project."""
    for slug in cfg.projects:
        try:
            sample(cfg, slug)
        except Exception:
            log.exception("telemetry.tick: unhandled error for project %s", slug)
