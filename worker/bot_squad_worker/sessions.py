"""Session manager — list, pause, resume, spawn Claude tmux sessions.

Each function is a pure worker action callable. The worker runs as almdudleer
and has access to the user's tmux server via the default socket.

Session ID (SID) format: ``S-<user>-<window>-p<pane_id_no_pct>``
  e.g. ``S-almdudleer-spec5-smoke-p2``
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot_squad_worker import frontmatter as _frontmatter
from bot_squad_worker import mdlock as _mdlock
from bot_squad_worker import agent_provider as _agent_provider
from bot_squad_worker import boot_orientation as _boot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# T-0104 — activity-derived "running" vs "idle" threshold.
#
# A live claude pane bumps the mtime of its own jsonl transcript every time
# Claude writes (every assistant turn, every tool call). 30s is generous
# enough that a short tool pause does not flip the label, and short enough
# that an idle agent registers as `idle` on the next poll. Centralised here
# so the threshold lives in exactly one place; do not duplicate.
# ---------------------------------------------------------------------------
RUNNING_THRESHOLD_SEC = 30.0


# ---------------------------------------------------------------------------
# T-0046 — "active-at-prompt" threshold (separate from the running/idle one).
#
# An `active` pane whose jsonl has been quiet for this many seconds is
# treated as awaiting human input by the quick-status aggregator. Distinct
# from RUNNING_THRESHOLD_SEC: a multi-step tool chain can quietly run for
# 30-50s between assistant writes and we don't want every such pause to
# flip the project pill to needs-input. 60s is the conservative default.
# ---------------------------------------------------------------------------
IDLE_AT_PROMPT_SECONDS = float(os.environ.get("BOT_SQUAD_IDLE_AT_PROMPT_SECONDS") or 60)


# ---------------------------------------------------------------------------
# T-0233 — aggressive stale-session GC threshold.
#
# Paradigm reframe: each session is a one-time run for a specific task. An
# *exited* (pane-gone) dev whose task is still open but which has not done
# anything in this many seconds is an abandoned/crashed run; archive_dead_teammates
# reaps it (preserving the binding as last_task_id so the still-open task stays
# re-dispatchable) instead of letting it linger in the working set forever.
# Read per-call (not frozen at import) so it is env-tunable on a live worker;
# the default is aggressive-but-safe — a pane-gone run idle > 24h is dead.
#
# T-0288: this is a DIFFERENT concept from the roadmap Ch. III "idle session
# suspends after 12h (HARD)" spec, and its 24h default is deliberate, not a
# spec violation — it grades an *already-exited* pane's crash/abandon grace,
# where a false-positive reap is costlier (the run is gone, only the binding
# is at stake) than the LIVE-pane idle-suspend case. The Ch. III 12h figure
# governs a *live* pane going idle-too-long instead; that threshold is
# DEFAULT_IDLE_SUSPEND_SEC / _session_idle_suspend_sec() below, not this one.
# ---------------------------------------------------------------------------
DEFAULT_SESSION_STALE_SEC = 24 * 3600


def session_stale_sec() -> float:
    """Staleness grace (seconds) for the T-0233 exited-stale reaper.

    Reads ``BOT_SQUAD_SESSION_STALE_SEC`` each call (env-tunable on a live
    worker), falling back to ``DEFAULT_SESSION_STALE_SEC``.
    """
    raw = os.environ.get("BOT_SQUAD_SESSION_STALE_SEC")
    try:
        val = float(raw) if raw else DEFAULT_SESSION_STALE_SEC
    except ValueError:
        val = DEFAULT_SESSION_STALE_SEC
    return val if val > 0 else DEFAULT_SESSION_STALE_SEC


# T-0288: reconciles the idle-suspend threshold to the roadmap's Ch. III HARD
# spec ("any idle session suspends after 12h") — the default effective window
# once no explicit operator override exists, below.
DEFAULT_IDLE_SUSPEND_SEC = 12 * 3600


def _read_idle_suspend_cap(config_dir: Path | None) -> tuple[bool, float]:
    """Raw ``[caps].idle_suspend_sec`` read from ``system_settings.toml``,
    distinguishing an EXPLICIT value (including an operator's deliberate
    ``0`` = OFF) from an ABSENT key/file/dir (the caller falls through to
    ``DEFAULT_IDLE_SUSPEND_SEC``). Returns ``(explicit, value)``; a garbage
    explicit value reads the same as absent (``(False, 0.0)``), matching the
    garbage-falls-to-default handling used throughout this module.
    """
    if config_dir is None:
        return False, 0.0
    path = Path(config_dir) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return False, 0.0
    caps = raw.get("caps", {}) or {}
    if "idle_suspend_sec" not in caps:
        return False, 0.0
    try:
        v = float(caps["idle_suspend_sec"])
    except (TypeError, ValueError):
        return False, 0.0
    return True, (v if v > 0 else 0.0)


def _session_idle_suspend_sec(cfg: Any = None) -> float:
    """T-0335 item-10 / Fork-2 Part B: idle-but-live dev suspend window (seconds).

    T-0408: the knob lives in ``system_settings.toml [caps].idle_suspend_sec``
    (the System Settings UI, written by the API caps PUT, read fresh like the
    other caps).

    T-0288: reconciled to the roadmap Ch. III HARD spec — an ABSENT cap (no
    ``system_settings.toml``, or the key missing from ``[caps]``) now falls
    through to ``DEFAULT_IDLE_SUSPEND_SEC`` (12h) instead of OFF, so the
    idle-suspend arm in ``archive_dead_teammates`` is live out of the box.
    This supersedes item-10's original "ships DARK, operator opts in later"
    rollout-safety default (D2) now that the operator-facing cap UI exists
    (T-0408) and the spec calls this a HARD rule, not opt-in. An operator who
    explicitly writes ``idle_suspend_sec = 0`` still gets a genuine, honored
    OFF — only *absence* defaults to the spec value, never an explicit
    override. Suspend is reversible (the task stays open + re-dispatchable),
    so the default window is safe.

    A *positive* ``BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC`` env var force-overrides
    the cap (dev / emergency escape hatch). An unset / 0 / garbage env falls
    through to the cap (or the 12h default when the cap is itself absent).
    ``cfg=None`` (no config to read) ⟹ env-or-default (no cap file to consult).
    """
    raw = os.environ.get("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC")
    try:
        env_val = float(raw) if raw else 0.0
    except ValueError:
        env_val = 0.0
    if env_val > 0:
        return env_val
    config_dir = _caps_config_dir(cfg) if cfg is not None else None
    explicit, cap = _read_idle_suspend_cap(config_dir)
    if explicit:
        return cap
    return DEFAULT_IDLE_SUSPEND_SEC


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PaneInfo:
    pane_id: str      # e.g. %2
    window: str
    pid: str
    cwd: str
    command: str
    session: str = ""  # tmux session name (== project slug for project panes)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper so tests can monkeypatch subprocess.run."""
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def list_panes() -> list[PaneInfo]:
    """Return all tmux panes across all sessions/windows for this user.

    Returns an empty list if tmux is not running or no panes exist.
    """
    fmt = "#{pane_id}|#{window_name}|#{pane_pid}|#{pane_current_path}|#{pane_current_command}|#{session_name}"
    result = _run(["tmux", "list-panes", "-a", "-F", fmt])
    if result.returncode != 0:
        return []
    panes = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 5)
        # Tolerate legacy 5-field lines (no session_name) — session defaults to "".
        if len(parts) < 5:
            continue
        panes.append(PaneInfo(
            pane_id=parts[0],
            window=parts[1],
            pid=parts[2],
            cwd=parts[3],
            command=parts[4],
            session=parts[5] if len(parts) >= 6 else "",
        ))
    return panes


def compute_sid(user: str, window: str, pane_id: str) -> str:
    """Compute ``S-<user>-<window>-p<pane_id_no_pct>``.

    pane_id typically looks like ``%2``; we strip the ``%``.

    T-0636: this is the ROUTING key — tmux pane matching (``live_pane_map``,
    every ``compute_sid(user, pane.window, pane.pane_id) == sid`` scan in this
    module), peer-bus addressing, and session-md filenames all reproduce this
    exact shape from ``(user, window, pane_id)`` alone. It deliberately does
    NOT carry the project slug: two sessions in different projects under the
    same role/window render identical SIDs (the stakeholder's 2026-07-18
    complaint — "no way to know which project it's for"), but reshaping this
    string would ripple into every one of those matching sites across the
    worker, well beyond a display fix. See :func:`sid_display_label` for the
    human-facing label that DOES carry the slug.
    """
    pane_no_pct = pane_id.lstrip("%")
    return f"S-{user}-{window}-p{pane_no_pct}"


_SID_SHAPE_RE = re.compile(r"^S-(.+)-p\d+$")


def _role_segment_of_sid(sid: str) -> str | None:
    """T-0676 item 5: recover the ROLE from a routing SID's own shape
    (``S-<user>-<window>-p<pane>``) without a metadata load — reuses
    :func:`_derive_role`'s window-marker regexes against the user+window
    middle segment. Exact, not an approximation: ``_derive_role`` only
    matches markers against its ``window`` arg (task_id/initiative no longer
    influence the role per T-0175), and a role marker is always a SUFFIX of
    that window, which is in turn a suffix of the sid's middle segment — so
    matching the whole segment finds the same marker regardless of where the
    user/window boundary actually falls. Returns ``None`` for a
    non-SID-shaped id (e.g. the synthetic ``"deploy_monitor"`` sender) so the
    caller can fall back to the old bracket form.
    """
    m = _SID_SHAPE_RE.match(sid or "")
    if not m:
        return None
    return _derive_role(m.group(1), None, None)


def _alias_for_sid(data_dir: Any, sid: str) -> str | None:
    """T-0662 alias lookup for :func:`sid_display_label`'s ``compact`` form —
    a stakeholder-assigned nickname beats an auto-derived role label. Several
    labels may point at one sid; picks the alphabetically-first for a stable
    result. ``None`` when no alias is set (or the store is missing/corrupt —
    ``load_aliases`` already degrades to ``{}`` rather than raising)."""
    from bot_squad_worker import session_aliases as _session_aliases
    matches = sorted(
        label for label, aliased_sid in _session_aliases.load_aliases(data_dir).items()
        if aliased_sid == sid
    )
    return matches[0] if matches else None


def sid_display_label(sid: str, slug: str | None, *, compact: bool = False, data_dir: Any = None) -> str:
    """T-0636: human-facing label for ``sid`` that also names its project.

    ``compute_sid``'s ``S-<user>-<window>-p<pane>`` shape carries no project
    cue, so two sessions in different projects under the same role/window are
    visually identical wherever the raw SID is surfaced (TG pings, the web
    sessions list). This wraps it for DISPLAY ONLY — ``[<slug>] <sid>`` — so
    every caller that has a slug in hand can render a readable label without
    the underlying routing key ever changing shape (see ``compute_sid``).
    Falls back to ``sid`` unchanged when ``slug`` is empty/None so callers
    without a slug handy degrade gracefully instead of erroring.

    ``compact=True`` (T-0676 item 5 — stakeholder found ``[watchrobot]
    S-almdudleer-operator-p160`` noisy): renders ``"<slug> <role>"`` instead,
    e.g. ``"watchrobot operator"`` — the TG-facing paths (pager/notify/
    topic-say/relay/peer-send-mirror) opt into this explicitly; the web
    sessions list default is UNCHANGED (still the bracket form; out of this
    batch's scope). Falls back to the bracket form when ``sid`` doesn't parse
    as a real routing SID (e.g. the synthetic ``"deploy_monitor"`` sender —
    no role segment to derive). Pass ``data_dir`` to let a T-0662
    stakeholder-assigned alias win over the derived role (``compact`` only —
    the bracket form never substitutes an alias, unaffected either way).

    T-0724: an EMPTY/blank ``sid`` is not a session and must not be
    interpolated. Real callers pass one — ``autoupdate_apply._notify_failure``
    pages with ``sid=""`` (an apply failure is the install's, not any
    session's) — and the old code rendered ``"[bot-squad] "``, a dangling
    bracket plus trailing space, which ``tg._prefix`` then wrapped into the
    malformed ``"[[bot-squad] ] <text>"``. Degrade to a slug-ONLY label
    instead (``"[<slug>]"`` / compact ``"<slug>"``): the project is still
    named, no session is claimed, and the TG prefix reads ``"[bot-squad]
    <text>"``. Mirrors the empty-``slug`` fallback above — an absent half of
    the label drops out rather than rendering as empty punctuation.
    """
    if not slug:
        return sid
    if not (sid or "").strip():
        return slug if compact else f"[{slug}]"
    if not compact:
        return f"[{slug}] {sid}"
    if data_dir is not None:
        alias = _alias_for_sid(data_dir, sid)
        if alias:
            return f"{slug} {alias}"
    role = _role_segment_of_sid(sid)
    if role is None:
        return f"[{slug}] {sid}"
    return f"{slug} {role}"


def live_pane_map(user: str | None = None) -> dict[str, str]:
    """Canonical SID → live tmux pane_id, derived from ``list_panes()``.

    The session md ``pane_id`` field is frequently EMPTY for a live session even
    though a real tmux pane exists and the SID encodes it — so consumers must
    resolve liveness from the actual panes (the same ``compute_sid`` matching
    ``suspend()``/``gc_sessions`` use), NOT from the md field. A SID present in
    this map has a live pane; absent ⇒ no live pane (genuinely gone).
    """
    if user is None:
        user = _get_current_user()
    out: dict[str, str] = {}
    for p in list_panes():
        try:
            out[compute_sid(user, p.window, p.pane_id)] = p.pane_id
        except Exception:
            continue
    return out


def discover_claude_uuid(cwd: str, user_home: str) -> str | None:
    """Return the UUID (filename stem) of the most recent .jsonl for this cwd.

    Encodes cwd using the standard claude path-encoding:
      ``cwd.replace('/', '-')``
    Claude keeps the leading dash (e.g. ``/home/alice/repo`` →
    ``-home-alice-repo``); stripping it produces a path that doesn't exist
    on disk and causes this function to always return None for real cwds.
    Returns None if no project dir or no .jsonl files exist.
    """
    return _agent_provider.get("claude").discover_session_id(cwd, user_home)


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _proc_cmdline(pid: int) -> list[str]:
    """A process's argv, or [] when it is gone. The seam the /proc walks share
    (and the one their tests drive, so a cmdline shape can be pinned without a
    live process)."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    return cmd.split("\x00")


def _pane_claude_uuid_from_proc(
    pane_pid: str, user_home: str, children: dict[int, list[int]] | None = None,
) -> str | None:
    """T-0120: return the AUTHORITATIVE claude_uuid for a tmux pane via /proc.

    Walks /proc descendants of ``pane_pid`` for a ``claude`` process whose
    cmdline carries ``--resume <uuid>`` or ``--session-id <uuid>`` — that's
    the uuid the live session is writing to. Returns None if no matching
    descendant is found (e.g. fresh spawn whose cmdline is just ``claude
    --dangerously-skip-permissions``; that case is left for the
    discover_claude_uuid fallback). user_home is reserved for future use
    (e.g. a fd-based probe) and kept in the signature for parity with
    discover_claude_uuid.

    Disambiguates panes that share a cwd: ``discover_claude_uuid()`` returns
    the cwd's mtime-latest jsonl — the SAME uuid for every pane in the cwd —
    so the uuid-keyed md fallback would map every such pane to one md.
    Reading each pane's claude process directly gives a per-pane uuid.
    Pattern mirrors the descendant walk in scripts/hooks/session_start.sh.

    Why cmdline and not /proc/<pid>/fd: claude does NOT keep its transcript
    .jsonl open as a long-lived fd — it opens, appends, closes per write —
    so an fd scan races with each turn boundary. The cmdline is stable for
    the lifetime of the claude process.

    T-0668: ``children`` is the prebuilt ``_proc_children_map()`` (same param
    T-0416 already added to ``_pane_has_live_claude``). This function used to
    rebuild that map from a full /proc scan on EVERY call; list_sessions()
    calls it once per live pane, and list_sessions() itself is invoked by
    ~15 independent scheduler jobs a minute plus every real-time fan-out
    request — on a busy host that redundant O(panes x host-processes) scan
    kept the coordinator CPU-bound long enough to blow the fan-out httpx
    timeout. Standalone callers omit ``children`` and one is built on demand.
    """
    del user_home  # reserved; see docstring
    try:
        target = int(pane_pid)
    except (ValueError, TypeError):
        return None
    if children is None:
        children = _proc_children_map()
    queue: list[int] = [target]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        parts = _proc_cmdline(pid)
        if parts and any(p.endswith("claude") or p == "claude" for p in parts[:1]):
            for i, tok in enumerate(parts):
                if tok in ("--resume", "--session-id") and i + 1 < len(parts):
                    candidate = parts[i + 1].strip()
                    if _UUID_RE.match(candidate):
                        return candidate
        queue.extend(children.get(pid, []))
    return None


def _proc_uuid_is_resume_only(
    pane_pid: str, uuid: str, children: dict[int, list[int]] | None = None,
) -> bool:
    """T-0960: True when ``uuid`` reached us as ``--resume``, never ``--session-id``.

    The two flags are NOT interchangeable, and :func:`_pane_claude_uuid_from_proc`
    treats them as one. ``--session-id <uuid>`` FORCES the uuid, so the pane
    really does write that transcript. ``--resume <uuid>`` names the ANCESTOR
    the session was resumed FROM — Claude Code then forks a NEW transcript under
    a NEW uuid. Measured live: the operator pane carried ``--resume 29018a66…``
    while writing ``0c671a5d….jsonl``, so telemetry tailed a file nobody had
    touched in 97h, ``offset == size`` every tick, and the context reading froze
    at 410082 forever — over-reporting here, but under-reporting is the
    dangerous direction (a resumed session whose ancestor was small would never
    auto-compact).

    Walks the SAME prebuilt children map as the uuid walk, so this costs a
    handful of ``/proc/<pid>/cmdline`` reads, not the O(panes x host-processes)
    rescan T-0668 removed. The caller gates it on a uuid MISMATCH, which is rare.
    """
    try:
        target = int(pane_pid)
    except (ValueError, TypeError):
        return False
    if children is None:
        children = _proc_children_map()
    queue: list[int] = [target]
    seen: set[int] = set()
    saw_resume = False
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        parts = _proc_cmdline(pid)
        if parts and any(p.endswith("claude") or p == "claude" for p in parts[:1]):
            for i, tok in enumerate(parts):
                if tok in ("--resume", "--session-id") and i + 1 < len(parts):
                    if parts[i + 1].strip() != uuid:
                        continue
                    if tok == "--session-id":
                        return False      # forced: authoritative, keep it
                    saw_resume = True
        queue.extend(children.get(pid, []))
    return saw_resume


def _pane_agent_session_id_from_proc(
    pane_pid: str,
    provider_name: str,
    children: dict[int, list[int]] | None = None,
) -> str | None:
    """Return the provider session UUID carried by a live resume command."""
    try:
        target = int(pane_pid)
    except (ValueError, TypeError):
        return None
    if children is None:
        children = _proc_children_map()
    provider = _agent_provider.get(provider_name)
    queue: list[int] = [target]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            parts = (
                Path(f"/proc/{pid}/cmdline")
                .read_bytes()
                .decode("utf-8", "replace")
                .split("\x00")
            )
        except OSError:
            parts = []
        if parts and provider.process_matches(parts[0]):
            if provider_name == "claude":
                markers = ("--resume", "--session-id")
            else:
                markers = ("resume",)
            for i, token in enumerate(parts):
                if token in markers and i + 1 < len(parts):
                    candidate = parts[i + 1].strip()
                    if _agent_provider.is_uuid(candidate):
                        return candidate
            if provider_name == "codex":
                # Fresh Codex commands do not carry their generated session id
                # in argv, but the process keeps its exact rollout jsonl open.
                # This is pane-authoritative even when several Codex sessions
                # share one cwd (the filesystem "newest rollout" guess is not).
                try:
                    for fd in Path(f"/proc/{pid}/fd").iterdir():
                        target_path = os.readlink(fd)
                        match = re.search(
                            r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-"
                            r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
                            target_path,
                            re.IGNORECASE,
                        )
                        if match:
                            return match.group(1)
                except OSError:
                    pass
        queue.extend(children.get(pid, []))
    return None


def _pane_activity_at(
    cwd: str,
    claude_uuid: str | None,
    user_home: str,
    provider_name: str = "claude",
) -> float | None:
    """T-0104: return the per-pane activity timestamp (epoch seconds) or None.

    Reads the mtime of the pane's own jsonl transcript file
    (``~/.claude/projects/<encoded_cwd>/<uuid>.jsonl``). Claude appends to
    that file on every assistant turn / tool call, so a fresh mtime means
    the pane is actively writing.

    Per-pane granularity comes from claude_uuid — each pane has its own
    UUID and its own jsonl. We intentionally do NOT consult
    ``<cwd>/.claude/last_user_prompt_ts`` here: that file lives at cwd
    level and is bumped by every claude pane sharing the repo, so it
    can't distinguish per-pane activity in the common bot-squad setup
    where multiple panes share a single repo cwd.

    Returns None when claude_uuid is unknown or the jsonl is missing
    (e.g. brand-new pane whose first write hasn't happened yet) — the
    caller treats this as "no recent activity".
    """
    if not claude_uuid:
        return None
    jsonl_path = _agent_provider.get(provider_name).transcript_path(
        cwd, claude_uuid, user_home
    )
    if jsonl_path is None:
        return None
    try:
        return jsonl_path.stat().st_mtime
    except OSError:
        return None


def _peer_heartbeat_at(data_dir: Path, slug: str, sid: str) -> float | None:
    """T-0037: return mtime of the peer-bus heartbeat file for this SID, or None.

    intersession.inbox_read / inbox_wait touch ``data/<slug>/_chat/heartbeat-<sid>``
    each time they run (wait re-touches every ``_HEARTBEAT_INTERVAL`` seconds
    while armed). A long-idle TL whose only activity is an armed inbox_wait
    has a stale jsonl mtime but a fresh heartbeat — folding the heartbeat into
    ``activity_at`` keeps the "Last activity" column truthful for those
    sessions instead of showing "5h ago" for a session that's polling now.
    """
    hb = data_dir / slug / "_chat" / f"heartbeat-{sid}"
    try:
        return hb.stat().st_mtime
    except OSError:
        return None


def _is_active_at_prompt(
    live_status: str,
    activity_at: float | None,
    now: float,
    *,
    threshold_sec: float | None = None,
) -> bool:
    """T-0046: True iff a live `active` pane has been idle long enough that
    the human is the bottleneck (Claude finished its turn, awaiting input).

    Coarse — pure pane-mtime check, no composer-text probe. Threshold lives
    above RUNNING_THRESHOLD_SEC so normal tool-chain pauses (30-50s between
    assistant writes) don't get mistaken for "needs-input"; only persistent
    quiet on an active pane does. ``paused`` is handled separately by the
    aggregator (Ctrl-C is its own needs-input case).

    Conservative on missing data: ``activity_at is None`` returns False —
    we can't measure idle time without a write timestamp; the next poll
    will re-evaluate once a jsonl appears.
    """
    if live_status != "active":
        return False
    if activity_at is None:
        return False
    if threshold_sec is None:
        threshold_sec = IDLE_AT_PROMPT_SECONDS
    return (now - activity_at) >= threshold_sec


def _derive_activity(
    live_status: str,
    activity_at: float | None,
    now: float,
    *,
    threshold_sec: float = RUNNING_THRESHOLD_SEC,
) -> str:
    """T-0104: map (live md status, activity_at, now) → canonical activity enum.

    Enum: ``running | idle | paused | suspended``. ``suspended`` is set by
    the caller for the no-live-pane path; this helper only handles the
    live-pane derivation.

    - ``paused`` (md says Ctrl-C'd) stays ``paused`` — distinct from idle
      so the UI can offer Resume.
    - Live pane + recent jsonl mtime (< threshold_sec) → ``running``.
    - Live pane + stale or missing mtime → ``idle``.

    Activity-derived: never trusts a self-reported `status: active` in the
    md when the jsonl tells a different story.
    """
    if live_status == "paused":
        return "paused"
    if activity_at is not None and (now - activity_at) < threshold_sec:
        return "running"
    return "idle"


# ---------------------------------------------------------------------------
# T-0141 — authoritative session role.
#
# The sessions list used to infer role purely from task_id presence ("no
# task_id ⟹ teamlead"). On a project driven by agent-teams — where the lead
# break-panes each teammate into a feature-named window and binds its task_id
# only afterwards (if at all) — nearly every task-less row rendered as a
# teamlead (stakeholder note 9: "almost all the sessions … are teamleads,
# something's leaking them"). The leak is the *default*: an ambiguous,
# marker-less, task-less session fell into the teamlead bucket.
#
# Role is now positively derived. A session is a teamlead only with real
# evidence — an explicit `-TL`/`_tl`/`teamlead` window marker. The operator pane
# is its own role. The fall-through default is "dev", never "teamlead".
#
# T-0175: the earlier "initiative binding with no task ⇒ teamlead" heuristic
# still leaked — a dev that finishes its task (task_id cleared to ~) keeps its
# initiative and flipped to teamlead. Initiative/task bindings no longer change
# the role; teamlead requires an explicit window marker. Genuine worker-spawned
# TLs carry a `-TL`/`_teamlead` window, so they are unaffected.
#
# The separator-guarded `tl` match (`(^|[-_])tl$`) avoids false positives on
# words that merely end in "tl" (e.g. `some-ctl`).
# ---------------------------------------------------------------------------
_TL_WINDOW_RE = re.compile(r"(?:^|[-_])(?:tl|teamlead)$", re.IGNORECASE)
_OPERATOR_WINDOW_RE = re.compile(r"(?:^|[-_])operator$", re.IGNORECASE)
# T-0197: prod-teamlead + qa become first-class spawnable roles. The prod-TL
# marker (`prod-tl` / `prod_teamlead`) must be tested BEFORE _TL_WINDOW_RE — a
# `…-prod-tl` window also ends in `tl`, so plain-TL precedence would otherwise
# swallow it. `prod-ops-tl` does NOT match (no `prod` adjacent to the trailing
# `-tl`), so genuine dev-side prod-ops TLs stay `teamlead`.
_PROD_TL_WINDOW_RE = re.compile(r"(?:^|[-_])prod[-_](?:tl|teamlead)$", re.IGNORECASE)
_QA_WINDOW_RE = re.compile(r"(?:^|[-_])qa$", re.IGNORECASE)
# T-0478 (M2/F2.4): system-controlled user-conversation session spawned on
# incoming user mail. The window encodes the user (`<gu_id>-user-conversation`)
# but always ends in the `user-conversation` marker, so the gid prefix never
# changes the derived role. The suffix is disjoint from every other marker
# above (ends in "conversation", not tl/qa/operator), so precedence among them
# is irrelevant.
_USERCONV_WINDOW_RE = re.compile(r"(?:^|[-_])user[-_]conversation$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# T-0964 — THE NAMES THE USER SEES.
#
# Stakeholder (2026-09-06): «сейчас из конкретных жалоб
# gu_dc8262b6cea9098d98e04d7e-user-conversation -- вообще не понятно что это,
# эти айдишники юзеру не надо светить, для юзера должно быть четко
# universal_bsq_session, если budding, то уже по ролям кто есть кто,
# dev_add_ui_button, operator, user_session и т.п.»
#
# The window name is the ONE string the user reads in `tmux ls`, in the
# sessions list and in every attach hint, so it is the naming surface — not a
# separate "display name" layer that the tmux status bar would still contradict.
#
#   universal_bsq_session   the root session while it does everything (L0-solo)
#   user_session[_<who>]    the root after it budded work off (L1/L2), and any
#                           additional per-user attendant
#   dev_<feature-slug>      a dev bud                       (`bsq spawn`)
#   operator                an operator bud                 (unchanged)
#
# WHAT THIS COSTS, AND WHERE THE COST WAS PAID. The gid used to ride IN the
# window precisely so a user-conversation session was self-identifying from its
# immutable SID alone. Dropping the gid from the window means the (slug, gid)
# pair has to live somewhere else: it is now the ``global_user_id`` md field
# (see :func:`session_global_user_id`), stamped at spawn and carried through
# every md rebuild the way ``owner_user`` / ``model`` are. The legacy window
# shape is still matched, so attendants spawned before this change keep
# resolving.
#: The root/universal session — one command (`bsq start`, T-0963) puts the user
#: in front of it, and it is what a project with nothing else running IS.
UNIVERSAL_WINDOW = "universal_bsq_session"
#: The root once it has budded, and the per-user attendant shape.
USER_SESSION_WINDOW = "user_session"
#: Prefix for a dev bud's window (`dev_add_ui_button`).
DEV_WINDOW_PREFIX = "dev_"

_UNIVERSAL_WINDOW_RE = re.compile(r"^universal[-_]bsq[-_]session(?:[-_]|$)",
                                  re.IGNORECASE)
# Segment-anchored, mirroring recycle_gate's T-0616 convention regex, so
# `user-sessions` / `user-feedback` do NOT match while `user_session`,
# `user-session-2` and `user_session_flomaster` all do.
_USER_SESSION_WINDOW_RE = re.compile(r"(?:^|[-_])user[-_]session(?:$|[-_])",
                                     re.IGNORECASE)
# T-0964: an explicit `dev_`/`dev-` prefix is AUTHORITATIVE and tested first, so
# a feature slug that happens to end in a role marker (`dev_move_the_qa`,
# `dev_drop_the_operator`) is a dev — which is the whole point of naming buds
# by role. Before this, the marker regexes below silently reclassified them.
_DEV_WINDOW_RE = re.compile(r"^dev[-_]", re.IGNORECASE)

# T-0176 #3: grouping bucket for sessions with no live tmux session — keeps the
# sessions-list grouping honest against `tmux list-sessions`.
_NO_TMUX_SESSION = "(no tmux session)"


def _derive_role(
    window: str | None,
    task_id: str | None,
    initiative: str | None,
    *,
    extra_task_ids: list | None = None,
    extra_initiatives: list | None = None,
) -> str:
    """Map a session's identity fields → role enum:
    ``operator|prod-teamlead|qa|teamlead|dev``.

    Precedence (first match wins):
      1. operator window marker (`operator`, `<x>-operator`) → ``operator``
      2. prod-TL window marker (`prod-tl`, `<x>_prod_teamlead`, …)
         → ``prod-teamlead`` (T-0197 — BEFORE plain TL, since a prod-tl window
         also ends in `tl`)
      3. qa window marker (`qa`, `<x>-qa`) → ``qa`` (T-0197)
      4. user-conversation marker — the legacy `<gu_id>-user-conversation`
         (T-0478, the system-spawned intake session) plus the T-0964
         user-facing names `universal_bsq_session` and `user_session[_<who>]`
         → ``user-conversation``
      5. explicit TL window marker (`<x>-TL`, `<x>_teamlead`, …) → ``teamlead``
      6. default → ``dev``

    T-0175: ``task_id`` / ``initiative`` (and their ``extra_*`` lists) no longer
    influence the role — they are accepted for call-site compatibility but a
    teamlead is recognised *only* by an explicit window marker. A task-less,
    initiative-bound, marker-less session is a dev (it is most often a dev that
    finished its task), not a teamlead.

    `~` is the registry's "unset" sentinel and is treated as absent.
    """
    w = (window or "").strip()
    # T-0964 (0.): an explicit `dev_` prefix wins outright — see _DEV_WINDOW_RE.
    if _DEV_WINDOW_RE.match(w):
        return "dev"
    if _OPERATOR_WINDOW_RE.search(w):
        return "operator"
    if _PROD_TL_WINDOW_RE.search(w):
        return "prod-teamlead"
    if _QA_WINDOW_RE.search(w):
        return "qa"
    # T-0964: the user-facing names join the legacy `<gid>-user-conversation`
    # marker. `user_session` was already the human's own hand-launch convention
    # (recycle_gate._USER_SESSION_WINDOW_RE, T-0616) — it is the SAME role, and
    # deriving it here is what makes those sessions read their own contract.
    if (_USERCONV_WINDOW_RE.search(w) or _UNIVERSAL_WINDOW_RE.match(w)
            or _USER_SESSION_WINDOW_RE.search(w)):
        return "user-conversation"
    if _TL_WINDOW_RE.search(w):
        return "teamlead"
    return "dev"


# T-0509 (M11/F11.2): the roles a USER-launched session may MORPH into. A
# user-launched session is a USER session by default but may "take on a task and
# become dev, spawn teammates and become teamlead, or become operator if there's
# no operator working right now -- sessions are transient, system is persistent"
# (clarification-03 / voice-09).
# T-0932 adds the DE-differentiation direction: ``user-conversation``. The set
# above only ever climbed — a session could take on a task, take on a team or
# take the board, but never narrow back down — which left «отделение себя в
# юзер-сессию» (the stakeholder's own phrasing for half of gradual budding)
# with no primitive at all. Morphing back is what a session does right after it
# buds its work off to a dev: it sheds the task and returns to the
# conversation. See :mod:`bot_squad_worker.budding`.
MORPH_ROLES = ("dev", "teamlead", "operator", "user-conversation")


def _role_of(
    meta: dict | None,
    *,
    window: str | None = None,
    task_id: str | None = None,
    initiative: str | None = None,
    extra_task_ids: list | None = None,
    extra_initiatives: list | None = None,
) -> str:
    """Canonical session role: an explicit stored ``role`` (stamped by
    ``bsq morph``, T-0509) OVERRIDES the window-derived role; otherwise derive
    from the window marker via :func:`_derive_role`.

    A user session that morphs (user→dev/teamlead/operator) stamps ``role`` on
    its md WITHOUT renaming its tmux window — renaming would rotate the peer-bus
    SID (the address frozen at the filename), so the morph must be truly
    in-place. This centralizes the ``meta.get("role") or _derive_role(...)``
    idiom already used by idle_timeout / graceful_exit / recovery, so the
    override is honored uniformly: the role badge (list_sessions), the
    operator-singleton guard (live_operator_sids) and the reconciler dev-gates.

    An absent / ``~`` stamp falls through to derivation, so legacy mds (which
    never carried a ``role`` field) are unchanged. The explicit ``window`` /
    ``task_id`` / ``initiative`` kwargs let a live-pane caller pass the live
    tmux window instead of the persisted one.
    """
    m = meta or {}
    stored = m.get("role")
    if stored and stored != "~":
        return str(stored)
    return _derive_role(
        window if window is not None else m.get("window"),
        task_id if task_id is not None else m.get("task_id"),
        initiative if initiative is not None else m.get("initiative"),
        extra_task_ids=extra_task_ids,
        extra_initiatives=extra_initiatives,
    )


def _cwd_matches_repo(
    cwd: Path | str | None,
    repo_path: Path,
    repo_real: Path,
    *,
    allow_parent: bool = False,
) -> bool:
    """True when ``cwd`` belongs to the project's repo, for pane filtering and
    role-badge validation.

    Mirrors the active-pane cwd filter (T-0003): a cwd matches when it IS the
    repo, lives UNDER it, or — via resolved symlinks (symlinked dev clones) —
    matches the real target. ``repo_real`` is ``repo_path.resolve()`` (passed in
    so callers resolve it once). When ``allow_parent`` is set (operator panes
    live in the workspace *parent* of the dev clone, e.g. cwd=/home/x/bot-squad
    while repo=/home/x/bot-squad/dev), also accept a cwd that is an ANCESTOR of
    the repo. Returns False on a None/unparseable cwd.
    """
    if cwd is None or cwd == "":
        return False
    cwd_path = cwd if isinstance(cwd, Path) else Path(cwd)
    try:
        cwd_real = cwd_path.resolve()
    except OSError:
        cwd_real = cwd_path
    try:
        match = (
            cwd_path == repo_path
            or cwd_path.is_relative_to(repo_path)
            or cwd_real == repo_real
            or cwd_real.is_relative_to(repo_real)
        )
        if not match and allow_parent:
            match = (
                repo_path.is_relative_to(cwd_path)
                or repo_real.is_relative_to(cwd_path)
            )
        return match
    except (ValueError, TypeError):
        return False


def _get_user_home() -> str:
    """Return the home directory for the current user."""
    return str(Path.home())


def _session_file(data_dir: Path, slug: str, sid: str) -> Path:
    return data_dir / slug / "sessions" / f"{sid}.md"


def _find_session_md(sessions_dir: Path, sid: str, claude_uuid: str | None) -> Path | None:
    """Resolve a session md by SID, falling back to a claude_uuid scan.

    A tmux window rename leaves the metadata file at the pre-rename SID
    (e.g. ``S-alice-teamlead-p10.md``) while the live pane has computed a
    fresh SID (e.g. ``S-alice-newname-p10.md``). The SID-keyed lookup
    misses, so as a last resort scan the sessions dir for a file whose
    ``claude_uuid:`` field matches the live pane's uuid — the uuid is
    rename-invariant since it identifies the claude transcript, not the
    tmux address.
    """
    direct = sessions_dir / f"{sid}.md"
    if direct.exists():
        return direct
    if not claude_uuid or not sessions_dir.exists():
        return None
    for md in sessions_dir.glob("*.md"):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        if meta.get("claude_uuid") == claude_uuid:
            return md
    return None


# --- T-0949: concurrent session-md mutation safety --------------------------
#
# The session md is read-modify-written by FOUR concurrent writers: the
# ``idle_timeout`` / ``graceful_exit`` / ``telemetry`` (autocompact ceiling)
# apscheduler jobs — separate jobs on a 30-thread pool, so ``max_instances=1``
# only guards each against ITSELF — plus ``scripts/hooks/session_start.sh``,
# which runs in the session's own process. Each read the WHOLE md at tick
# start and later wrote back a mutated copy of that snapshot, so the last
# writer resurrected its stale view of every OTHER machine's fields: an
# ``exit_handoff_phase`` armed by graceful_exit was erased by idle_timeout's
# nudge stamp, a ``compact_stay_phase`` was erased by the ceiling's write, and
# the anti-loop guards those fields exist to be were silently defeated. Field
# separation between the state machines does NOT protect them — whole-file
# writes do not respect it. (The same class ``mdlock.task_lock`` was written
# for on the TASK mds, T-0373; this is its session-md half.)
#
# Two mechanisms, both here so every writer inherits them without call-site
# churn:
#
# 1. :func:`session_md_lock` — the SAME ``<file>.lock`` flock convention
#    ``mdlock`` uses, so the worker's scheduler threads and the SessionStart
#    hook (a separate process) mutually exclude, and any future writer gets
#    the same exclusion for free by spelling the lockfile the same way. (The
#    API only READS session mds today — measured, not assumed.) Held over the
#    whole read→merge→write of every session-md write.
# 2. :class:`SessionMeta` — a read remembers the snapshot it came from, so a
#    write applies only the keys THAT READER CHANGED onto a FRESH read of the
#    md. A concurrent machine's fields are never resurrected or erased, even
#    when the two reads are minutes apart. A plain dict (a caller REBUILDING
#    the md from scratch, e.g. :func:`suspend`) still writes whole-file, which
#    is what those callers mean.
#
# The write is always atomic now, via ``mdlock.atomic_write``'s UNIQUE tmp:
# the old ``atomic=True`` path used ONE shared ``<name>.tmp`` per md, so two
# writers racing on the same session clobbered each other's tmp.

_MD_LOCK_STATE = threading.local()


def _held_locks() -> dict:
    """Per-thread depth map of session-md locks this thread already holds.

    ``fcntl.flock`` is per open-file-description, so a second acquire from the
    same thread (a nested write inside a locked section) would DEADLOCK
    against itself. Re-entrant acquires are counted, not re-taken.
    """
    held = getattr(_MD_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _MD_LOCK_STATE.held = held
    return held


@contextlib.contextmanager
def session_md_lock(path: Path):
    """Exclusive cross-process lock for mutating a session md (T-0949).

    Same lockfile convention as :func:`mdlock.task_lock` (``<path>.lock``), so
    the worker's scheduler threads, any other process, and the SessionStart
    hook can mutually exclude on one file. Re-entrant within a thread.
    """
    key = str(path)
    held = _held_locks()
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    with _mdlock.task_lock(Path(path)):
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)


class SessionMeta(dict):
    """A session md's frontmatter PLUS the on-disk snapshot it was read from.

    Behaves as a plain dict everywhere; :func:`_write_session_metadata` uses
    ``baseline`` to write back only the keys this reader actually changed (and
    the ones it removed), merged onto the CURRENT file. ``source`` pins which
    md the snapshot came from — a write to a DIFFERENT path (the rename paths)
    is a whole-file write, since a merge would be against an unrelated file.
    """

    __slots__ = ("baseline", "source")

    def __init__(self, data: dict, source: Path | None = None):
        super().__init__(data)
        self.baseline = dict(data)
        self.source = str(source) if source is not None else None

    def rebase(self, on_disk: dict) -> None:
        """Adopt ``on_disk`` (what was just written) as the new snapshot."""
        self.clear()
        self.update(on_disk)
        self.baseline = dict(on_disk)


def _merge_session_meta(path: Path, meta: dict) -> dict:
    """Merge ``meta``'s OWN changes onto the md's current on-disk content.

    Whole-file (``dict(meta)``) unless ``meta`` is a :class:`SessionMeta` read
    from THIS path and the file still exists. Call inside
    :func:`session_md_lock`.
    """
    baseline = getattr(meta, "baseline", None)
    source = getattr(meta, "source", None)
    if baseline is None or source is None or source != str(path):
        return dict(meta)
    parsed = _frontmatter.parse_or_none(path.read_text()) if path.exists() else None
    if parsed is None:
        return dict(meta)
    merged = dict(parsed[0] or {})
    for k, v in meta.items():
        if k not in baseline or baseline[k] != v:
            merged[k] = v          # a key THIS reader set/changed wins
    for k in baseline:
        if k not in meta:
            merged.pop(k, None)    # ...and one it deliberately removed goes
    return merged


def _write_session_metadata(path: Path, meta: dict, *, atomic: bool = False) -> None:
    """Write a session metadata file with YAML frontmatter.

    T-0949: the write holds :func:`session_md_lock` over a re-read + merge of
    the caller's own changes (see :class:`SessionMeta`), and always lands via
    a UNIQUE tmp + ``os.replace``. ``atomic`` is retained for call-site
    compatibility and no longer selects anything — every write is atomic, and
    the shared ``<name>.tmp`` two racing writers used to clobber is gone.
    """
    path = Path(path)
    with session_md_lock(path):
        payload = _merge_session_meta(path, meta)
        # T-0075: delegate serialization to the shared frontmatter writer (lists
        # inline, None as `~`, timestamps unquoted). Map the legacy "~" string
        # sentinel → None so it still emits as `~` (unquoted) and round-trips to
        # None, byte-matching the pre-T-0075 hand-rolled output.
        norm = {k: (None if v == "~" else v) for k, v in payload.items()}
        body = f"---\n{_frontmatter.dump_frontmatter(norm)}---\n"
        _mdlock.atomic_write(path, body)
    if isinstance(meta, SessionMeta) and meta.source == str(path):
        # The caller keeps working with this dict after the write — hand it
        # what is now ON DISK, so its next mutation diffs against reality.
        meta.rebase(payload)


def _read_session_metadata(path: Path) -> dict | None:
    """Parse YAML frontmatter from a session metadata file (T-0075: shared
    pyyaml parser, so block- and inline-style lists read identically).

    Returns None if the file doesn't exist or has no frontmatter. The result is
    a :class:`SessionMeta` (a dict) that remembers this snapshot, so a later
    :func:`_write_session_metadata` of it cannot clobber a concurrent writer's
    fields (T-0949).
    """
    if not path.exists():
        return None
    parsed = _frontmatter.parse_or_none(path.read_text())
    if parsed is None:
        return None
    return SessionMeta(parsed[0] or {}, source=path)


# --- T-0949: the per-session RECYCLE LEASE ----------------------------------

_LEASE_STATE = threading.local()


@contextlib.contextmanager
def recycle_lease(md_path: Path):
    """Non-blocking per-session lease for the three recycle state machines.

    ``idle_timeout_tick``, ``graceful_exit_tick`` and ``telemetry_tick`` (the
    autocompact ceiling) are separate 60s apscheduler jobs registered
    microseconds apart, so they fire in the SAME second on the SAME session,
    and their only serializer was a racy check-then-act ``composer_free``
    capture: two threads both passed the phase check before either wrote it,
    then both injected into one pane (two contradictory prompts, or the
    doubled ``/compact`` the ``compact_stay_*`` fields exist to prevent).

    A machine takes this lease for the whole of its decide→act→write pass on
    one session. It NEVER blocks: a machine that cannot take it simply defers
    to its next tick, which is what every other gate on these paths does.

    Yields True when the lease is held (act), False when another machine has
    it (defer). Re-entrant within a thread.
    """
    path = Path(md_path)
    lock_path = path.parent / (path.name + ".recycle.lock")
    key = str(lock_path)
    held = getattr(_LEASE_STATE, "held", None)
    if held is None:
        held = {}
        _LEASE_STATE.held = held
    if held.get(key):
        held[key] += 1
        try:
            yield True
        finally:
            held[key] -= 1
        return
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        # Can't even open the lease file — fail OPEN rather than wedge every
        # recycler on a permissions problem (the pre-T-0949 behaviour).
        log.warning("recycle_lease: cannot open %s — proceeding unleased",
                    lock_path)
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        held[key] = 1
        try:
            yield True
        finally:
            held.pop(key, None)
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _parent_sid_of(meta: dict | None) -> str:
    """T-0128: read the persisted ``parent_sid`` from a session md, or "".

    Treats the ``~`` unset sentinel and missing field as absent (returns "").
    The empty string signals "legacy session" so the web tree falls back to
    the task→initiative→TL heuristic for that row.
    """
    if not meta:
        return ""
    v = meta.get("parent_sid")
    if v and v != "~":
        return str(v)
    return ""


def _parent_sid_heuristic_of(meta: dict | None) -> bool:
    """T-0647: True when ``parent_sid`` was *inferred* by
    ``backfill_parent_sid``'s nearest-live-TL heuristic rather than stamped at
    spawn time by the actual requesting session. A guessed parent is not the
    same claim as a recorded one — routing decisions that need the real
    spawn relationship (e.g. idle-notify target) must not treat the two as
    equivalent, since the guess can land on a TL with no real relationship to
    the session (journal-evidenced T-0647: p11/p16/p34 -> unrelated TL p23).
    """
    if not meta:
        return False
    return str(meta.get("parent_sid_heuristic", "")).lower() == "true"


def _get_current_user() -> str:
    """Return the OS username."""
    import getpass
    return getpass.getuser()


def _linux_user_from_sid(sid: str) -> str:
    """T-0157: extract the linux user from an SID ``S-<user>-<window>-p<pane>``.

    The SID's user segment is the linux login the session's tmux server runs
    as (set by ``compute_sid``). Usernames carry no ``-`` in practice (the
    same assumption ``WorkerRouter.for_sid`` makes), so ``split("-", 2)[1]``
    is the user. Returns "" when the SID doesn't parse (legacy / non-SID).
    """
    if sid and sid.startswith("S-"):
        parts = sid.split("-", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return ""


def _session_linux_user(sid: str, meta: dict | None) -> str:
    """T-0157: the linux user owning a session — explicit field wins, else SID.

    Prefer the ``linux_user`` frontmatter stamped at spawn time; fall back to
    the SID prefix so pre-T-0157 session mds (no field) still report a user.
    """
    if meta:
        v = meta.get("linux_user")
        if v and v != "~":
            return str(v)
    return _linux_user_from_sid(sid)


# T-0200: a tmux session name must never carry a shell/tmux metacharacter. A
# live incident produced the session `bot-squad-"prod-support` from an initiative
# value of `"prod-support.md` (a stray YAML quote that survived a flat parse).
# We sanitise the derived stem to this conservative charset so no quote / space /
# `$(...)` / etc. can ever leak into a `tmux new-session -s <name>` argument.
_SESSION_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _strip_surrounding_quotes(v: str) -> str:
    """Strip one layer of matching surrounding single/double quotes (T-0208).

    Quoted YAML scalars (`initiative: "x.md"`) are now the norm — the shared
    pyyaml dump (T-0075) and ``bsq task new`` both emit them. When such a
    value reaches spawn() unstripped, the trailing quote fails the
    ``.endswith(".md")`` validator. bsq's read_frontmatter strips upstream;
    this is the worker-side safety net.
    """
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _tmux_session_name(slug: str, initiative: str | None) -> str:
    """T-0001: per-initiative tmux session routing.

    Without an initiative, panes live in the project's main session named
    after ``slug`` — the operator and TL-less devs share that pane real
    estate. With an initiative, the spawned TL (and any devs spawned with
    the same initiative arg) land in a sibling session named
    ``<slug>-<initiative-stem>`` so the stakeholder can attach to one
    initiative team without the operator pane competing for the screen,
    and so initiatives don't accumulate windows in the main session.

    ``initiative`` is the basename of a file under ``vision/initiatives/``
    (e.g. ``multi-server-installation-process.md``); the stem (``Path.stem``)
    is what gets appended. Empty / None → main session.

    T-0200: the stem is sanitised to ``[A-Za-z0-9._-]`` so a malformed
    initiative value (e.g. a stray quote) can't produce a session name like
    ``bot-squad-"prod-support``. A stem that sanitises to empty → main session.
    """
    if not initiative:
        return slug
    stem = _SESSION_NAME_SAFE_RE.sub("", Path(initiative).stem)
    if not stem:
        return slug
    return f"{slug}-{stem}"


# T-0614: charset for the claude `--name` display value — the tmux-safe set
# plus the space, since the name is a human-facing label, not a tmux target.
_CLAUDE_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._\- ]")


def _claude_session_name(window: str, task_id: str | None) -> str:
    """T-0614: compose the claude ``--name`` display value for a session.

    The native ``/resume`` picker titles sessions by their first-message
    snippet unless a name is set (claude >= 2.1.196: ``--name`` at launch,
    persisted as a ``custom-title`` transcript record). We already compose a
    descriptive window name at spawn time — the same string the SID is
    derived from (``S-<user>-<window>-p<N>``; the pane suffix doesn't exist
    until after launch, so the window is the SID's human part). Reuse it,
    plus the primary task id when it isn't already embedded in the window.

    Sanitised to a conservative charset (T-0200 lesson: a stray quote in a
    caller-supplied value once produced a broken tmux session name); the
    result is additionally shlex-quoted at the call sites. Returns "" when
    nothing usable survives — callers then omit ``--name`` entirely.
    """
    parts = [window or ""]
    tid = (task_id or "").strip()
    if tid and tid != "~" and tid not in (window or ""):
        parts.append(tid)
    return _CLAUDE_NAME_SAFE_RE.sub("", " ".join(p for p in parts if p)).strip()


# T-0200: the tmux pane command for a Claude session is either the top-level
# ``claude`` binary or a version-named binary (e.g. ``2.1.139``) that Claude Code
# spawns for agent-teams subagents. Shared by ``list_sessions`` grouping and the
# ``gc_tmux_sessions`` reaper so both agree on what counts as a live claude pane.
_CLAUDE_CMD_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _is_claude_command(command: str) -> bool:
    return any(
        _agent_provider.get(name).command_matches(command)
        for name in _agent_provider.PROVIDERS
    )


def _provider_from_command(command: str) -> str | None:
    for name in _agent_provider.PROVIDERS:
        if _agent_provider.get(name).command_matches(command):
            return name
    return None


def _ensure_project_tmux_session(slug: str, cwd: str, initiative: str | None = None) -> None:
    """Ensure a long-lived tmux session for this slug (or slug+initiative) exists.

    Per the active-context-manager model: one tmux session per project,
    panes/windows live inside it. Survives across spawn/resume cycles.
    Caller must guarantee the session is created before any new-window.

    T-0001: when ``initiative`` is set, ensure (and reuse) a sibling session
    named ``<slug>-<initiative-stem>`` instead of the main project session.
    """
    target = _tmux_session_name(slug, initiative)
    has = _run(["tmux", "has-session", "-t", target])
    if has.returncode == 0:
        return
    # Create detached; -n _init parks a placeholder window we never use for
    # claude. claude windows are added via tmux new-window -t <target>:.
    # It must keep EXISTING (gc_orphan_tmux_sessions reads its name as the
    # ownership proof that lets a de-registered project's stray session be
    # told apart from a human's own unrelated tmux session — sessions.py
    # around _BOT_SQUAD_INIT_WINDOW), but nothing needs it VISIBLE while the
    # session is staffed with real windows: T-0927 (stakeholder, 2026-08-28,
    # "tmux _init window is irritating"). Blank its own status-bar label
    # (window-specific option, so it beats whatever the human's own
    # ~/.tmux.conf sets globally) so it stops showing up as a stray empty tab
    # without touching the keep-alive/ownership mechanism at all.
    _run([
        "tmux", "new-session", "-d",
        "-s", target,
        "-c", cwd,
        "-n", "_init",
    ])
    _run(["tmux", "set-window-option", "-t", f"{target}:_init",
          "window-status-format", ""])
    _run(["tmux", "set-window-option", "-t", f"{target}:_init",
          "window-status-current-format", ""])


# ---------------------------------------------------------------------------
# Session manager actions
# ---------------------------------------------------------------------------

def list_sessions(cfg: Any, slug: str) -> list[dict]:
    """List all Claude sessions for a project (active + paused).

    Active sessions are discovered via tmux list-panes.
    Paused sessions are read from data/<slug>/sessions/*.md.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"list_sessions: unknown project slug {slug!r}")

    repo_path = Path(project.repo_path)
    # Dereference symlinks so symlinked dev clones (e.g. signal_tracker/dev
    # → signal_tracker_mgmt) match panes whose cwd is the real target.
    try:
        repo_real = repo_path.resolve()
    except OSError:
        repo_real = repo_path
    data_dir = cfg.data_dir
    user = _get_current_user()
    user_home = _get_user_home()

    # --- Discover active panes ---
    active_sids: set[str] = set()
    rows: list[dict] = []

    try:
        panes = list_panes()
    except Exception:
        panes = []

    # T-0176 #3: the set of tmux sessions that are actually live right now (have
    # ≥1 pane). Suspended rows group by this — a stored tmux_session that no
    # longer appears here is stale and falls into the "(no tmux session)" bucket,
    # so the UI grouping matches `tmux list-sessions` instead of stale metadata.
    live_tmux_sessions = {p.session for p in panes if p.session}

    # T-0568: every live pane's computed SID, for the uuid-fallback ownership
    # guard below (an md whose stored SID is another live pane must not be
    # attributed to this one).
    all_pane_sids = {compute_sid(user, p.window, p.pane_id) for p in panes}

    # T-0668: build the /proc children-map ONCE for every pane's uuid walk
    # below, instead of _pane_claude_uuid_from_proc rebuilding it per pane
    # (see that function's T-0668 note; same fix T-0416 already applied to
    # _pane_has_live_claude/_live_agent_sids).
    proc_children = _proc_children_map()
    for pane in panes:
        pane_cwd = Path(pane.cwd) if pane.cwd else None
        provider_name = _provider_from_command(pane.command)
        if provider_name is None:
            continue
        if pane_cwd is None:
            continue
        # T-0003: operator pane lives in repo_workspace (parent of the dev
        # clone), e.g. cwd=/home/x/bot-squad while repo_path=/home/x/bot-squad/dev.
        # Accept the parent-cwd case only when bounded by tmux session == slug
        # (every project pane lives in a tmux session named after the slug, per
        # _ensure_project_tmux_session) or window == 'operator' (spawn fixes it
        # in routes_projects.create_project). Either bound prevents over-match
        # to unrelated panes whose cwd happens to be an ancestor of repo_path.
        # T-0220: this match is factored into _cwd_matches_repo, shared with the
        # suspended-row role-badge validation below.
        allow_parent = pane.session == slug or pane.window == "operator"
        if not _cwd_matches_repo(
            pane_cwd, repo_path, repo_real, allow_parent=allow_parent
        ):
            continue

        sid = compute_sid(user, pane.window, pane.pane_id)
        active_sids.add(sid)

        # T-0120: prefer the pane's /proc-walked uuid over the cwd-mtime
        # guess. discover_claude_uuid returns the same value for every pane
        # sharing a cwd, so the uuid fallback in _find_session_md would map
        # multiple panes to one md (3 active TLs in /home/almdudleer/bot-squad-mgmt
        # all attributed to the same started_at + initiative on staging).
        # /proc walk reads the uuid the live claude process has open — that's
        # per-pane. discover_claude_uuid stays as the fallback when the walk
        # finds nothing (e.g. claude not yet exec'd in a brand-new pane).
        proc_uuid = (
            _pane_claude_uuid_from_proc(pane.pid, user_home, proc_children)
            if provider_name == "claude"
            else _pane_agent_session_id_from_proc(
                pane.pid, provider_name, proc_children
            )
        )
        claude_uuid = proc_uuid or _agent_provider.get(
            provider_name
        ).discover_session_id(pane.cwd, user_home)

        # Check for last_prompt_at via .claude/last_user_prompt_ts mtime
        last_prompt_at = None
        prompt_ts_file = repo_path / ".claude" / "last_user_prompt_ts"
        if prompt_ts_file.exists():
            try:
                last_prompt_at = prompt_ts_file.stat().st_mtime
            except OSError:
                pass

        # Resolve the session md — SID first, then claude_uuid fallback.
        # T-0118: a tmux window rename moves the live pane's computed SID
        # away from the on-disk md filename; the uuid-keyed fallback
        # recovers started_at / task_id / etc. for renamed-window panes.
        sessions_dir_path = data_dir / slug / "sessions"
        session_md_path = _find_session_md(sessions_dir_path, sid, claude_uuid)
        existing = _read_session_metadata(session_md_path) if session_md_path else None
        if existing and existing.get("provider") in _agent_provider.PROVIDERS:
            provider_name = str(existing["provider"])

        # T-0568 guard: the uuid FALLBACK can land on an md that belongs to a
        # DIFFERENT live pane — discover_claude_uuid is a shared-cwd mtime
        # guess (every md-less pane in one cwd resolves to the newest jsonl),
        # and the /proc walk can transiently miss on a brand-new pane. If the
        # resolved md's own SID is another live pane, this pane must not wear
        # that session's identity/task (the p8↔T-0568 roster cross-attribution).
        # A rename-recovery md (T-0118) is unaffected: its stored SID points at
        # a window name that no longer exists, so it is never live.
        if existing is not None and session_md_path is not None:
            stored_owner_sid = str(existing.get("sid") or session_md_path.stem)
            if stored_owner_sid != sid and stored_owner_sid in all_pane_sids:
                session_md_path = None
                existing = None

        # T-0584: when the /proc walk missed (fresh spawn — cmdline carries no
        # --resume/--session-id), prefer the md-RECORDED claude_uuid over the
        # shared-cwd mtime guess. The guess is the cwd's newest jsonl — the
        # SAME uuid for every md-backed pane in the repo cwd, which cross-wired
        # every non-operator transcript link on /p/<slug>/sessions to the
        # newest session. The recorded binding is per-session; the live /proc
        # uuid stays authoritative when it resolves.
        if proc_uuid is None and existing is not None:
            recorded_uuid = existing.get("claude_uuid")
            if recorded_uuid and recorded_uuid != "~":
                claude_uuid = recorded_uuid

        # T-0960: the /proc walk DID resolve, but a `--resume` uuid names the
        # transcript this session was forked FROM, not the one it writes. The
        # md's uuid is recorded per-session by the SessionStart hook, so when
        # the two disagree AND the proc one is resume-only, the md wins. A
        # `--session-id` pane is untouched (that flag forces the uuid), and so
        # is the normal case where the two agree — the probe never runs there.
        elif proc_uuid is not None and existing is not None:
            recorded_uuid = existing.get("claude_uuid")
            if (recorded_uuid and recorded_uuid != "~"
                    and recorded_uuid != proc_uuid
                    and _proc_uuid_is_resume_only(
                        pane.pid, proc_uuid, proc_children)):
                log.info(
                    "sessions: %s carries --resume %s but its md records %s "
                    "— using the md (T-0960)", sid, proc_uuid, recorded_uuid)
                claude_uuid = recorded_uuid

        # T-0176 #4: a claude `/rename` changes the live tmux window name; sync
        # the stored SessionMd display label to match (it used to lag behind the
        # pre-rename window). uuid identity + the frozen sid/filename (the
        # peer-bus address) are untouched — only the human label is refreshed.
        # T-0568: never persist a GENERIC live name ('bash', blank — the decay
        # this ticket repairs) over a stored one; that would destroy the only
        # good copy of the name the reconcile pass restores from.
        if (
            existing is not None
            and session_md_path is not None
            and pane.window
            and not _is_generic_window(pane.window)
            and existing.get("window") != pane.window
        ):
            existing["window"] = pane.window
            try:
                _write_session_metadata(session_md_path, existing, atomic=True)
            except Exception:
                pass

        started_at = None
        task_id: str | None = None
        initiative: str = ""
        # Default to "active" for live panes; if the md frontmatter says
        # paused (Ctrl-C'd but pane left open) reflect that — otherwise
        # the UI shows every live pane as active even when the user paused it.
        live_status = "active"
        # Phase 9: extras for multi-binding. Empty list when unset.
        extra_task_ids: list[str] = []
        extra_initiatives: list[str] = []
        paused_at_meta: Any = None
        archived_flag = False
        owner_meta: str = ""  # T-0080 — UI-username owner stamp; "" = legacy
        owner_user_meta: str = ""  # T-0321 — per-user-scoping username; "" = legacy
        if existing:
            started_at = existing.get("started_at")
            tid = existing.get("task_id")
            if tid and tid != "~":
                task_id = tid
            init_val = existing.get("initiative")
            if init_val and init_val != "~":
                initiative = init_val
            md_status = existing.get("status", "")
            if md_status == "paused":
                live_status = "paused"
            etids = existing.get("extra_task_ids")
            if isinstance(etids, list):
                extra_task_ids = [t for t in etids if t and t != "~"]
            einits = existing.get("extra_initiatives")
            if isinstance(einits, list):
                extra_initiatives = [i for i in einits if i and i != "~"]
            paused_at_meta = existing.get("paused_at")
            archived_flag = str(existing.get("archived", "")).lower() == "true"
            own_val = existing.get("owner")
            if own_val and own_val != "~":
                owner_meta = str(own_val)
            ou_val = existing.get("owner_user")
            if ou_val and ou_val != "~":
                owner_user_meta = str(ou_val)
            # If the md was resolved via uuid fallback (stale SID after a
            # window rename), mark the stored SID as active too so the
            # suspended-md loop below doesn't double-emit the same session.
            stored_sid = existing.get("sid")
            if stored_sid and stored_sid != sid:
                active_sids.add(stored_sid)

        # T-0104: activity-derived status. The existing `status` (md/zombie)
        # is preserved for back-compat callers and action-button routing;
        # `activity` is the canonical label-display enum derived from the
        # jsonl mtime probe. Two fields — not a replacement — per the
        # binding-audit "don't replace existing status logic, extend it".
        jsonl_at = _pane_activity_at(
            pane.cwd, claude_uuid, user_home, provider_name
        )
        heartbeat_at = _peer_heartbeat_at(data_dir, slug, sid)
        # Fold jsonl mtime and peer-bus heartbeat into a single timestamp.
        # max() with None: pick whichever is non-None, or the larger when both.
        candidates = [t for t in (jsonl_at, heartbeat_at) if t is not None]
        activity_at = max(candidates) if candidates else None
        now_ts = time.time()
        activity = _derive_activity(live_status, activity_at, now_ts)
        # T-0046: an `active` pane whose jsonl has been quiet for
        # IDLE_AT_PROMPT_SECONDS is reclassified as needs-input by the
        # quick-status aggregator. The raw `status` stays "active" so
        # action-button routing (pause/suspend) is unaffected.
        active_at_prompt = _is_active_at_prompt(live_status, activity_at, now_ts)

        rows.append({
            "sid": sid,
            "status": live_status,
            "activity": activity,
            "activity_at": activity_at,
            "active_at_prompt": active_at_prompt,
            # T-0141: authoritative role — no longer "task-less ⟹ teamlead".
            # T-0509: a stored morph ``role`` overrides the window-derived role.
            "role": _role_of(
                existing or {},
                window=pane.window, task_id=task_id, initiative=initiative,
                extra_task_ids=extra_task_ids,
                extra_initiatives=extra_initiatives,
            ),
            "window": pane.window,
            "cwd": pane.cwd,
            "started_at": started_at,
            "last_prompt_at": last_prompt_at,
            "claude_uuid": claude_uuid,
            "provider": provider_name,
            "task_id": task_id,
            "initiative": initiative,
            "extra_task_ids": extra_task_ids,
            "extra_initiatives": extra_initiatives,
            "paused_at": paused_at_meta,
            "suspended_at": None,
            "archived": archived_flag,
            "owner": owner_meta,
            "owner_user": owner_user_meta,
            # T-0128: persisted spawn-time parent (the SID that requested this
            # spawn). Empty string when unset (legacy session) — the web tree
            # falls back to the task→initiative→TL heuristic in that case.
            "parent_sid": _parent_sid_of(existing),
            # T-0647: True iff the above was a backfill *guess*, not a
            # genuine spawn-time link — see _parent_sid_heuristic_of.
            "parent_sid_heuristic": _parent_sid_heuristic_of(existing),
            # T-0157: linux user that owns this session (explicit field, else
            # SID prefix). For active panes the SID's user IS the worker's user.
            "linux_user": _session_linux_user(sid, existing),
            # T-0078: live tmux session name — for the "copy `tmux a -t …`"
            # affordance the UI offers. Comes from list-panes' session_name
            # field; falls back to whatever the md has if the pane row
            # didn't carry one (legacy 5-field tmux output).
            "tmux_session": (
                pane.session
                or (existing.get("tmux_session") if existing else "")
                or ""
            ),
        })

    # --- Non-active sessions from metadata files ---
    # Surface: explicitly paused/suspended sessions AND zombies (md says
    # "active" but the pane is gone — e.g. user closed the tmux window).
    # Anything with no live pane and a claude_uuid is resurrectable.
    sessions_dir = data_dir / slug / "sessions"
    if sessions_dir.exists():
        for meta_file in sorted(sessions_dir.glob("*.md")):
            meta = _read_session_metadata(meta_file)
            if meta is None:
                continue
            sid = meta.get("sid", meta_file.stem)
            if sid in active_sids:
                continue  # listed as active above
            status = meta.get("status", "")
            # Display status: keep "paused" only if pane is still alive;
            # otherwise anything with no pane is "suspended" (resurrectable).
            display_status = "suspended"
            if status == "paused":
                # Pane is gone (we already filtered out alive SIDs) — treat as suspended.
                display_status = "suspended"
            elif status == "suspended":
                display_status = "suspended"
            elif status == "active":
                # Zombie: registry says active but pane is gone.
                display_status = "suspended"
            else:
                # Unknown status — surface as suspended so user can resurrect.
                display_status = "suspended"
            md_task_id = meta.get("task_id")
            if md_task_id == "~":
                md_task_id = None
            md_initiative = meta.get("initiative")
            if not md_initiative or md_initiative == "~":
                md_initiative = ""
            md_extra_tids = meta.get("extra_task_ids") or []
            if not isinstance(md_extra_tids, list):
                md_extra_tids = []
            md_extra_tids = [t for t in md_extra_tids if t and t != "~"]
            md_extra_inits = meta.get("extra_initiatives") or []
            if not isinstance(md_extra_inits, list):
                md_extra_inits = []
            md_extra_inits = [i for i in md_extra_inits if i and i != "~"]
            md_archived = str(meta.get("archived", "")).lower() == "true"
            md_owner_val = meta.get("owner")
            md_owner = str(md_owner_val) if (md_owner_val and md_owner_val != "~") else ""
            md_owner_user_val = meta.get("owner_user")
            md_owner_user = str(md_owner_user_val) if (md_owner_user_val and md_owner_user_val != "~") else ""
            md_tmux_session_val = meta.get("tmux_session")
            md_tmux_session_raw = (
                str(md_tmux_session_val)
                if md_tmux_session_val and md_tmux_session_val != "~"
                else ""
            )
            # T-0176 #3: group by LIVE tmux. Keep the stored tmux session only if
            # it still has live panes; otherwise this suspended row falls into the
            # "(no tmux session)" bucket rather than a phantom group.
            md_tmux_session = (
                md_tmux_session_raw
                if md_tmux_session_raw and md_tmux_session_raw in live_tmux_sessions
                else _NO_TMUX_SESSION
            )
            # T-0141: authoritative role for suspended rows too.
            # T-0509: a stored morph ``role`` overrides the window-derived role
            # (the cwd-mismatch neutralization below still guards an elevated
            # role whose persisted cwd doesn't belong to the project).
            md_role = _role_of(
                meta,
                window=meta.get("window", ""), task_id=md_task_id,
                initiative=md_initiative,
                extra_task_ids=md_extra_tids,
                extra_initiatives=md_extra_inits,
            )
            # T-0220 (T-0169 audit §5): an ACTIVE pane's elevated role is already
            # backed by a cwd filter (above). For a SUSPENDED row the role comes
            # from the persisted window name ALONE — so a row whose window claims
            # an elevated role (operator/teamlead/prod-tl/qa) but whose persisted
            # cwd doesn't actually belong to this project would render a
            # misleading badge. Validate the persisted cwd with the SAME matching
            # logic the active path uses; on mismatch, neutralize to the safe
            # "dev" default and flag the row for audit. A matching cwd — or an
            # absent cwd (legacy rows that never persisted one) — leaves the role
            # untouched, so existing good rows don't regress.
            md_cwd = meta.get("cwd", "")
            if md_cwd == "~":
                md_cwd = ""
            role_cwd_mismatch = False
            if md_role != "dev" and md_cwd:
                if not _cwd_matches_repo(
                    md_cwd, repo_path, repo_real,
                    allow_parent=(md_role == "operator"),
                ):
                    md_role = "dev"
                    role_cwd_mismatch = True
            rows.append({
                "sid": sid,
                "status": display_status,
                # T-0104: no live pane → activity is unambiguously suspended,
                # regardless of what the md frontmatter claims.
                "activity": "suspended",
                "activity_at": None,
                "active_at_prompt": False,
                "role": md_role,
                # T-0220: True when an elevated window-derived role was
                # neutralized because the persisted cwd didn't match the project.
                "role_cwd_mismatch": role_cwd_mismatch,
                "window": meta.get("window", ""),
                "cwd": meta.get("cwd", ""),
                "started_at": meta.get("started_at"),
                "last_prompt_at": meta.get("suspended_at") or meta.get("paused_at"),
                "claude_uuid": meta.get("claude_uuid"),
                "provider": meta.get("provider") or "claude",
                "task_id": md_task_id,
                "initiative": md_initiative,
                "extra_task_ids": md_extra_tids,
                "extra_initiatives": md_extra_inits,
                "paused_at": meta.get("paused_at"),
                "suspended_at": meta.get("suspended_at"),
                # T-0444: surface WHY/WHO an auto-close happened so the Processes
                # status badge can make a surprise auto-cleanup visible. Absent
                # on user/API suspends + legacy rows (badge shows nothing extra).
                "suspend_source": meta.get("suspend_source"),
                "suspend_reason": meta.get("suspend_reason"),
                "archived": md_archived,
                "owner": md_owner,
                "owner_user": md_owner_user,
                # T-0128: persisted spawn-time parent for suspended rows too.
                "parent_sid": _parent_sid_of(meta),
                # T-0647: True iff the above was a backfill guess, not genuine.
                "parent_sid_heuristic": _parent_sid_heuristic_of(meta),
                # T-0157: linux user owning this (suspended) session.
                "linux_user": _session_linux_user(sid, meta),
                # T-0078: surface tmux_session so the UI can still suggest the
                # right `tmux a -t …` even after suspend.
                "tmux_session": md_tmux_session,
            })

    # T-0285: stamp an explicit awaiting-input flag from the tg_stall blocked
    # markers. T-0977: a marker means the session DECLARED itself blocked (or
    # stall_sweep found it stuck on a modal) — NOT "it peer_sent an operator",
    # which was true of every report and badged whole lanes as waiting on him.
    # This is the precise "this one is waiting on you" signal — distinct from
    # the T-0346 paused/idle-at-prompt heuristic. Best-effort; the import is
    # lazy because tg_stall imports sessions back (cycle-safe at call time).
    try:
        from bot_squad_worker import tg_stall as _tg_stall
        _blocked = _tg_stall.blocked_sids(cfg, slug)
    except Exception:  # noqa: BLE001 — never let the watchdog wedge the list
        _blocked = set()

    # T-0662: stakeholder-assigned nicknames (orthogonal to the auto-derived
    # sid_label below) — load the global alias map once, not per-row.
    try:
        from bot_squad_worker import session_aliases as _session_aliases
        _alias_map = _session_aliases.load_aliases(data_dir)
    except Exception:  # noqa: BLE001 — never let a corrupt alias file wedge the list
        _alias_map = {}
    _aliases_by_sid: dict[str, list[str]] = {}
    for _label, _sid in _alias_map.items():
        _aliases_by_sid.setdefault(_sid, []).append(_label)

    for _r in rows:
        sid = _r.get("sid")
        # T-0636: slug-qualified display label — see sid_display_label.
        _r["sid_label"] = sid_display_label(sid, slug)
        # T-0662: labels pointing at this sid, sorted for stable display.
        _r["aliases"] = sorted(_aliases_by_sid.get(sid, []))
        blocked = sid in _blocked
        # P2-05: close-on-attach reconcile. A blocked, LIVE (active) session that
        # has resumed crunching (new jsonl activity after the block) got its
        # answer — the operator attached-and-typed, which the peer_send /
        # user_prompt_submit clear paths miss on the DPI/MAX host. Clear the
        # stale marker here so awaiting_input decays instead of sticking for the
        # 24h TTL. Only attempted for blocked active rows; best-effort.
        if blocked and _r.get("status") == "active":
            try:
                if _tg_stall.clear_if_resumed(cfg, slug, sid, _r.get("activity_at")):
                    blocked = False
            except Exception:  # noqa: BLE001 — never let the reconcile wedge the list
                pass
        _r["awaiting_input"] = blocked

    return rows


def pause(cfg: Any, slug: str, sid: str) -> dict:
    """Pause a running Claude session — INTERRUPT ONLY.

    Sends Ctrl-C to the pane so Claude stops whatever it's doing and returns
    to its prompt. The pane stays open; the user can type into it directly,
    or click Resume in the UI to re-mark status active. To FREE RESOURCES,
    use suspend() instead.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: unknown project slug {slug!r}")

    user = _get_current_user()
    data_dir = cfg.data_dir

    panes = list_panes()
    target_pane: PaneInfo | None = None
    for pane in panes:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    if target_pane is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: no active pane found for SID {sid!r}")

    # Update registry status; preserve everything else.
    meta_file = _session_file(data_dir, slug, sid)
    existing = _read_session_metadata(meta_file) or {}
    existing["status"] = "paused"
    existing["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_session_metadata(meta_file, existing)

    # Just the interrupt — no /exit, no kill-pane. T-0578: control keys ride
    # the mux delivery lock so the interrupt can't land mid-keystroke inside a
    # concurrent mux delivery (send-keys itself lives only in input_mux).
    from bot_squad_worker import input_mux
    with input_mux.delivery_lock(data_dir, sid):
        input_mux.raw_keys(target_pane.pane_id, "C-c", "")
    return {"ok": True, "paused": True}


def suspend(cfg: Any, slug: str, sid: str, *,
            source: str | None = None, reason: str | None = None) -> dict:
    """Suspend a Claude session — close the pane to free resources.

    The registry md is preserved (with claude_uuid). Use resume() to
    resurrect: a new tmux window is spawned with ``claude --resume <uuid>``.

    T-0444: callers that auto-close a session (e.g. the constant-team drained-
    member reaper) pass ``source``/``reason`` so the close is VISIBLE on the
    Processes status badge. A user/API suspend omits them (the operator did it
    themselves — no surprise to surface), leaving the md without those keys.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"suspend: unknown project slug {slug!r}")

    user = _get_current_user()
    user_home = _get_user_home()
    data_dir = cfg.data_dir

    panes = list_panes()
    target_pane: PaneInfo | None = None
    for pane in panes:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    meta_file = _session_file(data_dir, slug, sid)
    existing = _read_session_metadata(meta_file) or {}

    # If no live pane, treat as already suspended — just normalise the md.
    if target_pane is None:
        existing["status"] = "suspended"
        existing.setdefault("started_at", "~")
        existing.setdefault("task_id", "~")
        existing.setdefault("claude_uuid", existing.get("claude_uuid", "~"))
        if source:  # T-0444: visible-close stamp on the auto-close path
            existing["suspend_source"] = source
            existing["suspend_reason"] = reason or source
        _write_session_metadata(meta_file, existing)
        return {"ok": True, "suspended": True, "already_gone": True}

    claude_uuid = existing.get("claude_uuid")
    if not claude_uuid or claude_uuid == "~":
        claude_uuid = discover_claude_uuid(target_pane.cwd, user_home)
    started_at = existing.get("started_at") or "~"
    task_id = existing.get("task_id") or "~"

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # T-0080: preserve owner field across suspend/resume so per-user
    # listing filters keep working after a session is suspended.
    owner_val = existing.get("owner") or "~"
    # T-0321: same for owner_user (the per-user-scoping username).
    owner_user_val = existing.get("owner_user") or "~"
    # T-0078: preserve tmux_session across suspend → resume so a stale
    # SessionMd still carries the last-known session name (used by the
    # UI's resurrect affordance and by the one-shot backfill script).
    # Fall back to the live pane's session name when the md was missing.
    tmux_sess_val = existing.get("tmux_session")
    if not tmux_sess_val or tmux_sess_val == "~":
        tmux_sess_val = target_pane.session or "~"
    meta: dict = {
        "sid": sid,
        "status": "suspended",
        "window": target_pane.window,
        "cwd": target_pane.cwd,
        "claude_uuid": claude_uuid if claude_uuid else "~",
        "task_id": task_id,
        "started_at": started_at,
        "suspended_at": now,
        "owner": owner_val,
        "owner_user": owner_user_val,
        "tmux_session": tmux_sess_val,
    }
    # T-0964: the (slug, gid) key a user-conversation session is addressed by.
    # This whitelist is a REBUILD, so an unlisted field is silently dropped —
    # exactly how `model` was lost across suspend until T-0678. Dropping this
    # one un-keys a suspended attendant from its user, so
    # `_find_suspended_user_conversation` would never resume it and every
    # inbound message would spawn a fresh attendant instead.
    gid_val = existing.get(GLOBAL_USER_ID_FIELD)
    if gid_val and gid_val != "~":
        meta[GLOBAL_USER_ID_FIELD] = str(gid_val)
    provider_val = existing.get("provider")
    if provider_val in _agent_provider.PROVIDERS:
        meta["provider"] = provider_val
    # T-0678 reopen: preserve a per-session `model` override (bsq model set)
    # across suspend the same way owner/tmux_session are preserved above —
    # this whitelist previously dropped it silently, so resume()'s
    # meta.get("model") read (and last_operator_model()'s md scan) always
    # came back empty after a suspend, reverting to the fleet default.
    model_val = existing.get("model")
    if model_val and model_val != "~":
        meta["model"] = model_val
    # T-0702 audit: this whitelist rebuild dropped `initiative` / the Phase 9
    # multi-binding extras / a morph-stamped `role` too, the same class of
    # bug the T-0678 reopen fixed for `model` — every one of these is read
    # straight off the md by a live path: resume() uses `initiative` to route
    # the resurrect into the right per-initiative tmux sibling session
    # (T-0001) instead of dumping a TL back into the main project session;
    # bind_task / gc_dead_bindings / list_sessions all read `extra_task_ids`
    # / `extra_initiatives` as the multi-binding source of truth; and
    # `_role_of` (T-0509) honors a stored `role` morph stamp over the
    # window-derived heuristic — losing it silently reverts a morphed
    # user→dev/teamlead/operator session back to its pre-morph role. This
    # isn't a rare manual-suspend-only path either: idle_timeout's automatic
    # cache-window recycle (`_terminate_and_remember`) calls this same
    # suspend() on every idle-timeout tick, so a long-lived initiative TL or
    # multi-bound dev would lose these fields on its very first idle recycle.
    initiative_val = existing.get("initiative")
    if initiative_val and initiative_val != "~":
        meta["initiative"] = initiative_val
    extra_task_ids_val = existing.get("extra_task_ids")
    if isinstance(extra_task_ids_val, list) and extra_task_ids_val:
        meta["extra_task_ids"] = extra_task_ids_val
    extra_initiatives_val = existing.get("extra_initiatives")
    if isinstance(extra_initiatives_val, list) and extra_initiatives_val:
        meta["extra_initiatives"] = extra_initiatives_val
    role_val = existing.get("role")
    if role_val and role_val != "~":
        meta["role"] = role_val
    # T-0702 audit: `bsq drift off` stamps `drift_paused: true` (drift.py
    # reads it directly to skip the per-session drift-check nag) with no
    # fallback if it's lost — unlike model/owner/tmux_session this one has no
    # SID/heuristic backstop, so dropping it here silently un-silences a nag
    # the user explicitly turned off, the moment the session idle-recycles.
    if existing.get("drift_paused"):
        meta["drift_paused"] = existing["drift_paused"]
    # T-0926 follow-up: same loss-on-recycle hazard as drift_paused above —
    # `bsq pin on` has no SID/heuristic backstop either.
    if source:  # T-0444: visible-close stamp on the auto-close path
        meta["suspend_source"] = source
        meta["suspend_reason"] = reason or source
    _write_session_metadata(meta_file, meta)

    # Graceful exit then force-kill if needed. T-0578: the whole teardown key
    # sequence holds the mux delivery lock, so C-c / "/exit" can never splice
    # into the middle of a concurrent mux delivery (and a mid-flight batch
    # finishes before the exit keys land).
    from bot_squad_worker import input_mux
    with input_mux.delivery_lock(data_dir, sid):
        input_mux.raw_keys(target_pane.pane_id, "C-c", "")
        time.sleep(0.3)
        input_mux.raw_keys(target_pane.pane_id, "/exit", "Enter")

    deadline = time.time() + 10.0
    while time.time() < deadline:
        ids_now = {p.pane_id for p in list_panes()}
        if target_pane.pane_id not in ids_now:
            break
        time.sleep(0.5)
    else:
        _run(["tmux", "kill-pane", "-t", target_pane.pane_id])

    return {"ok": True, "suspended": True}


# --- T-0575: recycle-v2 resume side ----------------------------------------
# idle_timeout's compact-terminate-remember (T-0566) stamps ``resumable: true``
# + ``recycled_at`` + ``resume_hint`` on the md it suspends. This is the
# act-on-it half: gate the resume-vs-fresh choice on the stakeholder's rule
# (2026-07-04: "<50k tokens context → resume the same user's last session
# instead of spawning fresh").
#
# The CONSUMER of that stamp is ``dispatch.decide_dispatch`` (T-0575 wiring
# "(b)"), which scans session mds itself and surfaces a ``resume_recommended``
# hint when a remembered session ran THIS task and fits the budget. A standalone
# ``resumable_sessions()`` finder shipped alongside it at 74eef0f for wiring
# "(a)" — the user-conversation attendant path — and was removed under T-0720
# once that path was shown to be structurally unreachable (T-0564 exempts the
# role from every recycle path, so no attendant md is ever stamped); see the
# T-0720 note above ``_action_ensure_user_conversation``. decide_dispatch does
# not need the finder: it needs per-session task/initiative matching and already
# walks the mds for its other hints.

RESUME_MAX_CONTEXT_TOKENS = 50_000

# T-0722: fallback re-read window when the standard telemetry tail (256 KiB)
# holds no compact boundary. idle_timeout compacts and terminates, so the
# boundary is structurally near EOF — but a transcript with multi-hundred-KiB
# attachment lines after it can push it past the tail, and reading the last
# usage line instead would silently reinstate the pre-compact number. Rare
# one-shot read (only for remembered sessions, only on a dispatch decision).
_RESUME_RESCAN_BYTES = 4 * 1024 * 1024


def _resume_window_tokens(transcript: Path, size: int) -> int | None:
    """Tokens a ``--resume <uuid>`` of ``transcript`` would ACTUALLY reload.

    T-0722: the naive "last assistant ``usage`` line in the tail" reading is
    the PRE-compact window for exactly the sessions this gate exists to admit.
    ``idle_timeout`` sends ``/compact`` and terminates immediately, so no
    assistant turn ever runs afterwards and no post-compact ``usage`` line is
    ever written — the tail's last one predates the compact it was supposed to
    measure. (Measured live 2026-07-26: p164 read 95,498 where the real
    post-compact window is 11,015.)

    So: prefer ``compactMetadata.postTokens`` from the last compact boundary —
    the model's own count of what the summary leaves behind, which is what a
    resume replays. Only when real turns ran after the compact does the last
    ``usage`` line already reflect it and become the better (live) number.
    Returns None when nothing is measurable at all.
    """
    from bot_squad_worker import telemetry as T  # function-level: avoid cycle
    scan: dict = {"last_window": None}
    for window in (T._TAIL_BYTES, _RESUME_RESCAN_BYTES):
        text, _ = T._read_chunk(transcript, max(0, size - window))
        scan = T.scan_lines(text.splitlines())
        if scan["compact_post_window"] is not None:
            return (scan["last_window"] if scan["usage_after_compact"]
                    else scan["compact_post_window"])
        if size <= window:
            break  # whole file already scanned — there is no boundary
    return scan["last_window"]


def recycled_resume_eligible(claude_uuid: str | None,
                             user_home: str | None = None) -> tuple[bool, int | None]:
    """The T-0575 resume-vs-fresh gate: measure the remembered session's REAL
    context occupancy from its transcript and apply the <50k rule.

    Returns ``(eligible, tokens)``. The transcript for ``claude_uuid`` must
    exist (a missing one means ``--resume`` would fail — fresh spawn instead);
    a transcript that measures None stays eligible (a compact-terminate-
    remembered session is small by construction; resume failure still falls
    back to fresh spawn). See ``_resume_window_tokens`` for what "REAL" means —
    post-compact, not the pre-compact number the tail's last usage line carries.

    Measured from the transcript, NOT the telemetry record: the record is
    keyed by the pre-recycle SID and may predate the /compact — the transcript
    of the md's own uuid is exactly what ``--resume`` will reload.
    """
    from bot_squad_worker import telemetry as T  # function-level: avoid cycle
    if not claude_uuid or claude_uuid == "~":
        return False, None
    home = user_home or _get_user_home()
    transcript = T.find_transcript(home, claude_uuid)
    if transcript is None:
        return False, None
    try:
        size = transcript.stat().st_size
    except OSError:
        return False, None
    tokens = _resume_window_tokens(transcript, size)
    return (tokens is None or tokens < RESUME_MAX_CONTEXT_TOKENS), tokens


def resume(cfg: Any, slug: str, sid: str, initial_prompt: str | None = None,
           task_id: str | None = None) -> dict:
    """Resume a Claude session — handles paused, suspended, and zombie cases.

    - status=paused with live pane → just clear paused status; user types in tmux.
    - status=paused with no live pane → resurrect (window was closed externally).
    - status=suspended → resurrect (new window + ``claude --resume <uuid>``).
    - status=active with no live pane (zombie) → resurrect.
    - status=active with live pane → error (use pause/suspend first).

    T-0150: when ``initial_prompt`` is provided, it is delivered into the
    resumed composer (same composer-ready poll + paste-buffer path as
    ``spawn``). This powers the "resume an existing expert with a delta brief"
    flow — instead of spawning a fresh dev that re-researches from scratch, the
    caller resurrects the session that already did the related work and hands it
    the new, related task. Only delivered on the resurrect path (a new pane);
    a paused-but-live session is left for the user to type into directly.

    T-0166: when ``task_id`` is provided AND the session has no primary task
    (``task_id`` is ``~``/empty — the usual state of a suspended *expert* whose
    own task closed and was stripped to ``last_task_id`` by gc), the resumed
    session adopts ``task_id`` as its primary: we stamp ``meta["task_id"]`` and
    drop the ``.claude/task_id`` marker so the SessionStart hook agrees. Without
    this, ``bsq spawn <ticket> --bundle …`` auto-resuming an expert left the
    session with no primary, and the follow-up ``bind_task`` (which requires a
    primary) failed for *every* bundled ticket. An existing primary is left
    untouched — the new ticket then rides in via ``extra_task_ids`` as before.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    user = _get_current_user()

    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: no metadata found for SID {sid!r}")

    cwd = meta.get("cwd", str(project.repo_path))
    window = meta.get("window", "claude")
    claude_uuid = meta.get("claude_uuid")
    status = meta.get("status", "")

    # T-0166: adopt a primary task on resume when the session has none. A
    # suspended expert whose own task closed has had its primary stripped to
    # ``last_task_id`` by gc_dead_bindings, so it carries no ``task_id``; the
    # caller (bsq spawn auto-resume) passes the new primary here so the resumed
    # session is a real dev again and the follow-up bind_task has a primary to
    # attach extras to. Only fills an EMPTY primary — an expert still holding an
    # active task keeps it (the new ticket lands in extra_task_ids via bind).
    cur_primary = meta.get("task_id")
    adopt_task_id: str | None = None
    if task_id and (not cur_primary or cur_primary == "~"):
        meta["task_id"] = task_id
        # T-0525: carry the adopted primary to the resumed claude via the
        # per-process BOT_SQUAD_TASK_ID env (set on the launch command below),
        # NOT the shared `.claude/task_id` marker — same concurrent-clobber race
        # spawn() fixed. Best-effort remove any stale marker so an env-less
        # reader can't pick up old residue.
        adopt_task_id = task_id.strip()
        try:
            stale_marker = Path(cwd) / ".claude" / "task_id"
            if stale_marker.exists():
                stale_marker.unlink()
        except OSError:
            # Best-effort: meta["task_id"] above is the source of truth; the
            # hook preserves it via existing.get("task_id") when no marker/env.
            pass
    # T-0001: resurrect into the same tmux session the spawn put us in.
    # A TL spawned with an initiative lives in `<slug>-<initiative-stem>`;
    # without that routing, resume would dump it back into the main
    # project session next to the operator pane.
    init_meta = meta.get("initiative")
    resume_initiative = init_meta if (init_meta and init_meta != "~") else None

    # Is the original pane still alive?
    panes_now = list_panes()
    live_pane = None
    for pane in panes_now:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            live_pane = pane
            break

    if live_pane is not None:
        if status == "paused":
            # Just clear paused: user types in the tmux pane to continue.
            meta["status"] = "active"
            meta.pop("paused_at", None)
            _write_session_metadata(meta_file, meta)
            return {"ok": True, "sid": sid, "in_place": True}
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"resume: session {sid!r} has a live pane and is not paused — "
            f"nothing to do. Pause or suspend it first if you meant to restart."
        )

    # T-0001: resurrect into the same tmux session the spawn put us in
    # (main `<slug>` or sibling `<slug>-<initiative-stem>`).
    target_session = _tmux_session_name(slug, resume_initiative)
    _ensure_project_tmux_session(slug, cwd, resume_initiative)

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    provider_name = str(meta.get("provider") or "claude")
    provider = _agent_provider.get(provider_name)
    # T-0678: a per-session `model` override (bsq model set) takes precedence
    # over the fleet-wide settings.json default resume() would otherwise
    # silently inherit — this is what makes the override "stick across
    # recycles" instead of reverting the moment the tmux window recycles.
    resume_model = str(meta.get("model") or "")
    if resume_model == "~":
        resume_model = ""
    resume_effort = str(meta.get("effort") or "")
    if resume_effort == "~":
        resume_effort = ""
    # T-0871: `meta["model"]` is stamped ONLY for an EXPLICIT spawn model —
    # deliberate (T-0678: a role-defaulted session should follow the role
    # default at RESUME time, not freeze today's value). But the empty case
    # then passed "", i.e. no `--model` flag at all, so every recycle of a
    # role-defaulted session silently dropped to ~/.claude/settings.json. Fix
    # is to RE-RESOLVE the role default through the same helper spawn uses,
    # leaving the sticky-explicit path above untouched. Effort rides the same
    # path — and re-clamping a stamped value means a stale/hand-edited md
    # cannot reintroduce an overshoot on a recycle.
    if provider_name == "claude":
        _resume_role = _role_of(meta)
        _resume_config_dir = _caps_config_dir(cfg)
        if not resume_model:
            resume_model, _resume_model_source = _resolve_claude_model(
                _resume_config_dir, _resume_role, "")
        else:
            _resume_model_source = "session-md"
        try:
            resume_effort, _resume_effort_source = _resolve_claude_effort(
                _resume_config_dir, _resume_role, resume_effort)
        except ValueError as exc:
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"resume: {exc}") from exc
        log.info(
            "resume %s/%s role=%s model=%s (%s) effort=%s (%s)",
            slug, sid, _resume_role,
            resume_model or "-", _resume_model_source or "-",
            resume_effort or "-", _resume_effort_source or "-",
        )
    # T-0614: keep the /resume-picker entry readable across rotations —
    # --name combined with --resume renames the session (a fresh
    # custom-title record supersedes the old one). Uses the possibly-ADOPTED
    # primary (T-0166 set meta["task_id"] above) so an expert rebound to a
    # new ticket is titled by the ticket it now works.
    _display_name = _claude_session_name(window, meta.get("task_id"))
    cmd = provider.launch_command(
        resume_id=str(claude_uuid) if claude_uuid and claude_uuid != "~" else None,
        model=resume_model,
        display_name=_display_name,
        initial_prompt=None,
        effort=resume_effort,
    )
    # T-0525: when this resume ADOPTS a primary (T-0166 expert-rebind), carry it
    # to the new claude via the per-process env channel so a concurrent spawn
    # can't clobber it (the shared-marker race). Non-adopt resumes keep their
    # primary via --resume + the hook's existing-md fallback, no env needed.
    if adopt_task_id:
        cmd = f"BOT_SQUAD_TASK_ID={shlex.quote(adopt_task_id)} {cmd}"
    # T-0324 (H1): re-export the stored owner so the SessionStart hook's
    # env-level constant-team guard also covers RESUMED sessions. spawn()
    # passes BOT_SQUAD_OWNER but resume never did — a resumed constant-team
    # session carried no env vars, which is exactly the gap the stale-marker
    # cross-wire (p179→p181) slipped through.
    resume_owner = meta.get("owner")
    if resume_owner and resume_owner != "~":
        cmd = f"BOT_SQUAD_OWNER={shlex.quote(str(resume_owner))} {cmd}"

    result = _run([
        "tmux", "new-window", "-d",
        "-t", f"{target_session}:",
        "-n", window,
        "-c", cwd,
        "bash", "-lc", cmd,
    ])
    if result.returncode != 0:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: tmux new-window failed: {result.stderr}")

    # Discover new pane
    time.sleep(0.5)
    post_panes = list_panes()
    new_panes = [p for p in post_panes if p.pane_id not in pre_panes]
    if not new_panes:
        # Fallback: find by window name
        new_panes = [p for p in post_panes if p.window == window]
    if not new_panes:
        from bot_squad_worker.actions import ActionError
        raise ActionError("resume: could not locate new pane after tmux new-window")

    # Take the one with the highest numeric pane ID (most recently created)
    new_pane = max(new_panes, key=lambda p: int(p.pane_id.lstrip("%")) if p.pane_id.lstrip("%").isdigit() else 0)
    new_sid = compute_sid(user, new_pane.window, new_pane.pane_id)

    # T-0909: a resume spends on the model it resumes ON, so it lands in the
    # same ledger as a spawn — kind="resume" keeps the two separable. Excluding
    # resumes would make the compliance number read a corpus smaller than the
    # spend it claims to describe.
    if provider_name == "claude":
        try:
            from bot_squad_worker import model_dispatch as _model_dispatch
            _model_dispatch.record(
                cfg.data_dir,
                kind="resume",
                slug=slug,
                sid=new_sid,
                window=window,
                task_id=str(meta.get("task_id") or "").strip(),
                role=_resume_role,
                provider=provider_name,
                model=resume_model,
                source=_resume_model_source,
                effort=resume_effort,
                effort_source=_resume_effort_source,
                reason=str(meta.get("model_reason") or "").strip(),
                dispatched_by="resume",
            )
        except Exception:
            log.warning("resume %s/%s: model-dispatch ledger write failed",
                        slug, sid, exc_info=True)

    # Update metadata
    # T-0575: this resurrect CONSUMES the recycle-v2 "remembered" state — drop
    # it so decide_dispatch never offers an already-resumed session again.
    for _k in ("resumable", "recycled_at", "resume_hint"):
        meta.pop(_k, None)
    meta["status"] = "active"
    meta["sid"] = new_sid
    # T-0078: resurrect into the same tmux session the spawn put us in;
    # reflect that on the SessionMd so list_sessions surfaces the correct
    # `tmux a -t …` target without waiting for the hook.
    meta["tmux_session"] = target_session
    meta.pop("paused_at", None)
    new_meta_file = _session_file(data_dir, slug, new_sid)
    _write_session_metadata(new_meta_file, meta)

    # Remove old metadata file if SID changed
    if new_sid != sid and meta_file.exists():
        meta_file.unlink()

    # T-0072: migrate the peer-bus inbox triple to the new SID so messages
    # already in the pre-rotation inbox stay readable and peers still
    # addressing the old SID don't get silently dropped into a dead file.
    # Best-effort: a failure here doesn't break resume; it just leaves an
    # orphan inbox the migration script can mop up.
    if new_sid != sid:
        try:
            from bot_squad_worker import intersession as _is
            _is.rebind_sid(cfg, slug, sid, new_sid)
        except OSError:
            pass

    # T-0105: SID rotation — append the rotated SID to every task this
    # session was bound to (primary + extras). The pre-rotation SID is
    # already in history (from spawn / bind_task); now both old and new
    # remain for forensics. Idempotent if for some reason new_sid == sid.
    rotated_task_ids: list[str] = []
    primary_task = meta.get("task_id")
    if primary_task and primary_task != "~":
        rotated_task_ids.append(primary_task)
    extras_raw = meta.get("extra_task_ids") or []
    if isinstance(extras_raw, list):
        rotated_task_ids.extend(t for t in extras_raw if t and t != "~")
    if rotated_task_ids:
        backlog_dir = data_dir / slug / "backlog"
        for tid in rotated_task_ids:
            try:
                _append_task_session_history(backlog_dir, tid, new_sid)
            except OSError:
                pass

    # T-0150: deliver the delta brief into the resumed composer, same proven
    # path spawn() uses (composer-ready poll, then bracketed paste-buffer + a
    # separate Enter). If the composer never shows ❯, raise so the caller can
    # recover via inject_input — the pane is up, only the prompt didn't land.
    # T-0904: same provider-neutral orientation as spawn(). A resume is where
    # the reported incident actually happened — the operator re-drive resumes
    # (or respawns) the operator with `dispatch.operator_standing_task`, which
    # mentions neither bsq nor the peer bus.
    _prompt = initial_prompt
    if _boot.needs_prompt_orientation(provider_name):
        _prompt = _boot.with_orientation(
            initial_prompt, sid=new_sid, slug=slug,
            role=str(meta.get("role") or ""), data_dir=data_dir,
        )
    if _prompt:
        if not _wait_for_agent_composer_ready(new_pane.pane_id, provider_name):
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"resume: {provider_name} composer was not ready for sid {new_sid} within "
                f"{_COMPOSER_READY_TIMEOUT_SEC:.0f}s — initial_prompt not delivered "
                "(pane is up; recover via inject_input)"
            )
        _deliver_prompt(new_pane.pane_id, _prompt,
                        data_dir=data_dir, sid=new_sid)

    return {"ok": True, "sid": new_sid}


def _append_task_session_history(
    backlog_dir: Path, task_id: str, sid: str, ts: str | None = None
) -> bool:
    """T-0105: append `sid` to the task md's `session_history:` frontmatter
    list. Append-only, idempotent (de-duped — if `sid` is already in the
    list, no-op) and atomic (tmp + rename).

    Inline-list format: ``session_history: [SID, SID, ...]`` — chosen so
    the line-based worker readers (sessions/intersession) can
    pick it up. Block-yaml-format lists written by the api PATCH path
    would be invisible here (same hazard as the existing `blocked_by`
    field — audit Bug #4); inline format is the worker's source of truth.

    T-0291: alongside the SID, stamp a sidecar ``session_history_ts:`` map
    (``{SID: first-touch-ISO}``) so the UI can show a real first-touch time
    for a session that has since gone suspended/archived/legacy (the live
    `/sessions` join would otherwise render `—`). The map is first-touch-wins:
    because the SID dedup short-circuits below, an existing SID is never
    re-stamped. It's a parallel block-YAML field read only via the shared
    pyyaml parser — `session_history` stays the canonical inline SID list.
    `ts` defaults to now (UTC); callers/tests may pin it.

    Creates the field if absent, inserted after ``status:`` for stable
    ordering. Returns True iff the file was modified.

    Best-effort: returns False on any I/O or parse failure — the binding
    write itself is the source of truth, the task-md stamp is a forensic
    convenience.
    """
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        return False
    path = matches[0]
    try:
        text = path.read_text()
    except OSError:
        return False
    parsed = _frontmatter.parse_or_none(text)  # T-0075: shared parser
    if parsed is None:
        return False
    meta, body = parsed

    existing = _frontmatter.as_list(meta.get("session_history"))
    if sid in existing:
        return False  # idempotent — de-dup, preserve order (first-touch ts kept)

    new_list = existing + [sid]
    # T-0291: stamp this SID's first-touch ts into the sidecar map.
    stamp = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw_ts = meta.get("session_history_ts")
    ts_map = dict(raw_ts) if isinstance(raw_ts, dict) else {}
    ts_map.setdefault(sid, stamp)  # first-touch wins (sid is new here anyway)

    if "session_history" in meta:
        meta["session_history"] = new_list
        meta["session_history_ts"] = ts_map
    else:
        # Insert after `status` for stable ordering (else append at end).
        rebuilt: dict = {}
        inserted = False
        for k, v in meta.items():
            rebuilt[k] = v
            if k == "status":
                rebuilt["session_history"] = new_list
                rebuilt["session_history_ts"] = ts_map
                inserted = True
        if not inserted:
            rebuilt["session_history"] = new_list
            rebuilt["session_history_ts"] = ts_map
        meta = rebuilt

    content = _frontmatter.dump(meta, body)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)
    return True


def _write_task_initiative_if_absent(backlog_dir: Path, task_id: str, initiative: str) -> bool:
    """T-0038: stamp `initiative: <basename>` into the task md's frontmatter
    if no `initiative:` field is present.

    Existing-wins: if the task already has an initiative (even a different
    one), the file is left alone — the operator's manual classification is
    authoritative.

    Returns True iff the file was modified.
    """
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        return False
    path = matches[0]
    try:
        text = path.read_text()
    except OSError:
        return False
    parsed = _frontmatter.parse_or_none(text)  # T-0075: shared parser
    if parsed is None:
        return False
    meta, body = parsed
    if "initiative" in meta:
        return False  # already set (even if `~`) — don't clobber
    # Insert after the `status` key for stable ordering; if no status key,
    # append at the end of frontmatter.
    rebuilt: dict = {}
    inserted = False
    for k, v in meta.items():
        rebuilt[k] = v
        if k == "status":
            rebuilt["initiative"] = initiative
            inserted = True
    if not inserted:
        rebuilt["initiative"] = initiative
    content = _frontmatter.dump(rebuilt, body)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)
    return True


# T-0126: composer-ready poll. The `❯` rune is rendered by Claude Code's
# TUI inside the input box and never appears in the bash prompt that runs
# before claude takes over the pane, so its presence is a reliable signal
# that the composer accepts keystrokes. Total budget = 15s; interval 0.3s
# keeps the polling pressure on tmux well under one request per claude
# render frame on a loaded host.
#
# T-0897 correction: `❯` is NOT unique to the input box after all — measured
# live on a directory claude had never run in, the first-run "trust this
# folder" dialog reuses the same rune as its list-selector ("❯ No, exit" /
# "  Yes, I trust this folder"). See `_TRUST_DIALOG_MARKER` below for how
# that screen is told apart from the real composer.
_COMPOSER_READY_TIMEOUT_SEC = 15.0
_COMPOSER_READY_POLL_INTERVAL_SEC = 0.3

# T-0897: claude's first-ever launch in a directory blocks on a "Quick safety
# check … trust this folder?" dialog before the real composer exists. Measured
# live (fresh tmux pane, v2.1.251, `claude --dangerously-skip-permissions`):
# --dangerously-skip-permissions does NOT skip this screen (it only skips
# per-action tool permission prompts); a bracketed paste sent to it is
# silently swallowed (composer content unchanged); and a bare Enter sent to it
# confirms the DEFAULT-selected row, "No, exit" — which kills the whole claude
# process (`pane_current_command` measured flipping from "claude" to "bash").
# bot-squad already runs every spawn unattended, so this is answered the same
# way a human operator would: select "Yes, I trust this folder" and confirm.
_TRUST_DIALOG_MARKER = "trust this folder"

# T-0201: confirm-then-Enter knobs for _deliver_prompt. The old blind
# `time.sleep(0.4)` was too short for a large (~140-line) bracketed paste —
# Claude's composer had not finished ingesting the paste when Enter landed, so
# the Enter was swallowed / landed inside the bracketed-paste wrapper and the
# brief sat unsubmitted as "[Pasted text #1 +N lines]". We now (1) poll until
# the paste actually shows in the composer, then (2) send Enter and poll until
# the composer clears, re-sending Enter up to a cap so a genuinely stuck pane
# fails loudly rather than looping forever. Read at call time so tests can
# monkeypatch them to bound runtime.
_PASTE_LANDED_TIMEOUT_SEC = 8.0
_PASTE_LANDED_POLL_INTERVAL_SEC = 0.3
_SUBMIT_MAX_RETRIES = 5
_SUBMIT_CONFIRM_TIMEOUT_SEC = 4.0
_SUBMIT_CONFIRM_POLL_INTERVAL_SEC = 0.3


def _wait_for_claude_composer_ready(pane_id: str) -> bool:
    """Poll ``tmux capture-pane`` until Claude's composer prompt rune appears.

    Returns True as soon as ``❯`` shows up in the pane buffer, or False if
    the budget elapses with no marker. Used by ``spawn()`` to avoid the
    initial_prompt-drop regression (T-0126): send-keys against a pane that
    is still in bash / claude-init swallows the text or the Enter.

    Reads ``_COMPOSER_READY_TIMEOUT_SEC`` / ``_COMPOSER_READY_POLL_INTERVAL_SEC``
    at call time (not def time) so tests can monkeypatch the module-level
    constants to bound runtime.
    """
    return _wait_for_agent_composer_ready(pane_id, "claude")


def _dismiss_trust_dialog(pane_id: str, cap_stdout: str) -> bool:
    """If claude's first-run-in-this-directory trust dialog is on screen
    (``cap_stdout`` from a plain ``capture-pane -p``), answer it and report
    whether it fired. See ``_TRUST_DIALOG_MARKER`` for why this can't be left
    to the normal composer-ready / paste-then-Enter path.

    Selects "Yes, I trust this folder" rather than assuming its position:
    only sends Down when that row is not already the one carrying the ``❯``
    selector, so a reordered or single-row dialog in some other version still
    lands on the right choice instead of blindly hitting the default.
    """
    lines = cap_stdout.splitlines()
    trust_idx = next(
        (i for i, line in enumerate(lines) if _TRUST_DIALOG_MARKER in line.lower()),
        None,
    )
    if trust_idx is None:
        return False
    from bot_squad_worker import input_mux

    if "❯" not in lines[trust_idx]:
        input_mux.raw_keys(pane_id, "Down")
        time.sleep(0.1)
    input_mux.raw_keys(pane_id, "Enter")
    return True


def _wait_for_agent_composer_ready(pane_id: str, provider_name: str) -> bool:
    """Poll until the selected provider's interactive composer is visible."""
    timeout_sec = _COMPOSER_READY_TIMEOUT_SEC
    interval_sec = _COMPOSER_READY_POLL_INTERVAL_SEC
    markers = _agent_provider.get(provider_name).composer_markers
    iterations = max(1, int(timeout_sec / interval_sec))
    for _ in range(iterations):
        cap = _run(["tmux", "capture-pane", "-t", pane_id, "-p"])
        if cap.returncode == 0:
            if provider_name == _agent_provider.CLAUDE and _dismiss_trust_dialog(
                pane_id, cap.stdout
            ):
                time.sleep(interval_sec)
                continue
            if any(marker in cap.stdout for marker in markers):
                return True
        time.sleep(interval_sec)
    return False


def _composer_content(pane_id: str) -> str | None:
    """Return the text Claude's composer currently holds, or None if the pane
    can't be captured / has no composer line.

    The composer prompt line is ``❯ <content>``. ``""`` means an empty composer
    (nothing the caller pasted/typed is in it); a non-empty string means the
    composer holds pasted/typed content — the literal text for a short paste,
    or a ``[Pasted text #N +M lines]`` placeholder for a large bracketed paste
    (verified live, T-0201). The composer is always rendered at the BOTTOM of
    the TUI, so we take the last ``❯`` line: even if the conversation
    scrollback (or the pasted brief itself) contains a ``❯`` above, the input
    box is the bottom-most one.

    T-0897: an EMPTY composer is not blank — claude fills it with a dim,
    rotating placeholder hint (e.g. ``Try "how does <filepath> work?"``), and
    a plain ``capture-pane -p`` renders that hint as ordinary text
    indistinguishable from something actually pasted there. Measured live
    (fresh tmux pane, v2.1.251): the hint is wrapped in SGR 2 (dim/faint,
    ``\x1b[2m…\x1b[0m``) immediately after the marker, while real typed or
    pasted content — including the ``[Pasted text #N +M lines]`` placeholder —
    renders with NO styling there at all.

    T-0957: T-0897's original check treated ANY escape landing directly after
    the marker as decorative UI, not just a faint one — and a GENERATING pane
    also puts an escape there, a plain foreground reset (``ESC[39m``), ahead
    of genuinely pasted content (measured live, Claude Code 2.1.263: the rune
    line renders ``❯ NBSP ESC[39m <real text>`` mid-generation). That reads a
    real paste as an empty composer, so :func:`_deliver_prompt_unlocked` times
    out at step (1) and never sends Enter — the paste sits in the box,
    unsubmitted, exactly the T-0957 symptom. Only SGR 2 (faint) actually marks
    decorative chrome (the T-0897 hint, ``<no suggestion>``, a replayed
    message); :func:`input_mux._unfainted` already separates faint chrome from
    real content correctly (T-0962) and is reused here rather than re-deriving
    a second ad hoc escape classifier.

    The first-run TRUST DIALOG (T-0897's other finding) is a different screen
    that happens to share the ``❯`` rune for its own list selector
    (``❯ No, exit``), highlight-colored rather than faint — so the faint
    classifier alone would read its selected option as real composer content.
    It is not this composer at all (see ``_dismiss_trust_dialog``), so it is
    excluded by its own marker rather than by teaching the escape classifier a
    second, unrelated style: growing that classifier per screen is exactly the
    whitelist-can-never-be-complete trap ``composer_watch`` already warns
    about. The hint's own wording is intentionally NOT part of either check:
    it rotates between several example prompts and changes across claude
    versions, so pinning it would be a literal from memory the DoD explicitly
    rules out.
    """
    from bot_squad_worker import input_mux

    cap = _run(["tmux", "capture-pane", "-t", pane_id, "-p", "-e"])
    if cap.returncode != 0:
        return None
    if _TRUST_DIALOG_MARKER in cap.stdout.lower():
        return ""
    content: str | None = None
    markers = tuple(
        dict.fromkeys(
            marker
            for name in _agent_provider.PROVIDERS
            for marker in _agent_provider.get(name).composer_markers
        )
    )
    for line in cap.stdout.splitlines():
        for marker in markers:
            idx = line.find(marker)
            if idx == -1:
                continue
            tail = line[idx + len(marker):]
            # T-0897: the separator claude renders right after `❯` is U+00A0
            # (non-breaking space), not an ASCII space. `str.strip()` treats
            # NBSP as whitespace, so it is dropped along with any leading
            # ASCII space by the `.strip()` below without special-casing it.
            plain, _saw_faint = input_mux._unfainted(tail)
            content = plain.strip()
    return content


def _deliver_prompt(pane_id: str, text: str, *,
                    data_dir: Any = None, sid: str | None = None) -> None:
    """Reliably deliver a prompt into a claude composer (T-0126/T-0144/T-0201).

    T-0578: when the caller knows the session identity (``data_dir`` + ``sid``
    — all in-tree callers do), the whole paste+submit runs under the mux
    delivery lock so a concurrent peer nudge / send_input flush can never
    interleave with the prompt. Without identity the paste runs unlocked
    (pre-T-0578 behavior).

    Loads ``text`` into a dedicated tmux paste buffer and pastes it in
    bracketed-paste mode (``paste-buffer -p``), then submits with a *separate*
    Enter. This is the operator's proven manual recovery (load-buffer +
    paste-buffer + Enter): unlike ``send-keys`` of a long literal — which can
    interleave with the TUI, mis-escape, or exceed argv limits, and whose
    chained Enter lands *inside* the bracketed-paste wrap rather than
    submitting — a paste-buffer lands the whole prompt in claude's input box
    atomically as one block (embedded newlines stay newlines, not submits),
    and the trailing standalone Enter is what submits it.

    T-0201: the submit is now confirm-then-Enter instead of a blind
    ``time.sleep(0.4)``. A large (~140-line) bracketed paste takes ~1s to fully
    ingest; a 0.4s sleep let the Enter land before the paste settled, so the
    brief sat unsubmitted as "[Pasted text #1 +N lines]". We (1) poll until the
    paste actually appears in the composer, then (2) send Enter and poll until
    the composer clears, re-sending Enter up to ``_SUBMIT_MAX_RETRIES`` so a
    genuinely stuck pane fails loudly rather than looping forever.

    Raises ``ActionError`` if the paste never lands or the composer never
    clears — the pane is up and the text is in the buffer, so the caller can
    recover via ``inject_input`` / a manual Enter (same contract as the
    composer-ready check the callers run just before this).
    """
    from bot_squad_worker import input_mux
    from bot_squad_worker.actions import ActionError

    if data_dir is not None and sid:
        with input_mux.delivery_lock(data_dir, sid):
            return _deliver_prompt_unlocked(pane_id, text)
    return _deliver_prompt_unlocked(pane_id, text)


def _deliver_prompt_unlocked(pane_id: str, text: str) -> None:
    """The paste+confirm body of :func:`_deliver_prompt` (see its docstring)."""
    from bot_squad_worker import input_mux
    from bot_squad_worker.actions import ActionError

    import re as _re
    digits = _re.sub(r"[^0-9]", "", pane_id) or "x"
    buf = f"bsq-prompt-{digits}"
    # T-0201: load the text via `load-buffer -` (stdin), NOT `set-buffer -- <arg>`.
    # `set-buffer` passes the data as a command ARGUMENT, which tmux's own
    # command parser rejects with "command too long" for a large (~140-line)
    # brief — verified live — so the buffer was never created and nothing got
    # pasted. `load-buffer -` streams the body in over stdin with no length
    # limit, and (unlike a temp file) sidesteps the worker's read-only /tmp.
    _run(["tmux", "load-buffer", "-b", buf, "-"], input=text)
    _run(["tmux", "paste-buffer", "-t", pane_id, "-b", buf, "-p", "-d"])

    # (1) Confirm the paste landed in the composer (non-empty) before Enter.
    land_iters = max(1, int(_PASTE_LANDED_TIMEOUT_SEC / _PASTE_LANDED_POLL_INTERVAL_SEC))
    landed = False
    for _ in range(land_iters):
        if _composer_content(pane_id):
            landed = True
            break
        time.sleep(_PASTE_LANDED_POLL_INTERVAL_SEC)
    if not landed:
        raise ActionError(
            f"_deliver_prompt: paste never appeared in the composer for pane "
            f"{pane_id} within {_PASTE_LANDED_TIMEOUT_SEC:.0f}s — prompt not "
            "delivered (pane is up; recover via inject_input)"
        )

    # (2) Submit; confirm the composer cleared, re-sending Enter up to the cap.
    confirm_iters = max(1, int(_SUBMIT_CONFIRM_TIMEOUT_SEC / _SUBMIT_CONFIRM_POLL_INTERVAL_SEC))
    for _attempt in range(_SUBMIT_MAX_RETRIES):
        input_mux.raw_keys(pane_id, "Enter")
        for _ in range(confirm_iters):
            time.sleep(_SUBMIT_CONFIRM_POLL_INTERVAL_SEC)
            if _composer_content(pane_id) == "":
                return  # composer cleared -> the prompt was submitted
        # still showing the paste -> Enter was swallowed; resend on next loop.
    raise ActionError(
        f"_deliver_prompt: composer never cleared after {_SUBMIT_MAX_RETRIES} "
        f"Enter attempts for pane {pane_id} — prompt may be unsubmitted "
        "(pane is up; recover via inject_input / a manual Enter)"
    )


#: The general `owner` shape: a UI username (JWT claim), the `constant-team`
#: sentinel, or a TL SID. Colon-free ON PURPOSE — owner lands in a shell
#: env-var assignment (`BOT_SQUAD_OWNER=…`), so this class is a security
#: boundary, not a formality.
#:
#: ``\Z``, NOT ``$`` (T-0895, operator p322 review): in Python ``$`` also
#: matches BEFORE a trailing newline, so ``^…+$`` accepted ``"dev\n"`` —
#: measured True on both classes here. Today the ``.strip()`` above makes that
#: unreachable, but these patterns are declared the PRIMARY boundary rather
#: than a backstop to ``shlex.quote``, so they must hold for a caller that
#: hands over an unsanitized string. ``\Z`` is a true end-of-string anchor.
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")
#: T-0895: the ONE exception that may carry a colon — a routine binding.
#: `routines._spawn_for_routine` spawns with ``owner=routine:R-NNNN``, and that
#: exact string is the dedup/attribution KEY three other places already read
#: (`routines._live_routine_session`, the input_mux nudge source, the
#: `_send_stakeholder_dm(sid=…)` label), so it cannot be reshaped away without
#: a second field at all three sites. It is therefore allowed WHOLE-STRING:
#: the pattern is anchored and its tail is `R-<digits>` only, so nothing else
#: gains a colon — `routine:R-1; rm -rf /`, `routine:$(id)` and
#: `routine:R-0001 x` all still fail. (shlex.quote still wraps the result;
#: this keeps the *validator* as the primary boundary rather than leaning on
#: quoting alone.) ``\Z`` not ``$`` — see the note on `_OWNER_RE` above.
_ROUTINE_OWNER_RE = re.compile(r"^routine:R-\d{4,}\Z")


def _valid_owner(owner_clean: str) -> bool:
    """True for an owner string safe to put in a shell env assignment.

    Either the general colon-free class or the narrow ``routine:R-NNNN`` form
    (T-0895) — matched as a whole, never as a prefix/substring.
    """
    return bool(_OWNER_RE.match(owner_clean)
                or _ROUTINE_OWNER_RE.match(owner_clean))


def spawn(
    cfg: Any,
    slug: str,
    window: str,
    initial_prompt: str | None = None,
    task_id: str | None = None,
    initiative: str | None = None,
    owner: str | None = None,
    parent_sid: str | None = None,
    owner_user: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    model_reason: str | None = None,
    dispatched_by: str | None = None,
    global_user_id: str | None = None,
) -> dict:
    """Spawn a new Claude session in the project's repo.

    T-0623: ``model`` (optional) lands as ``claude --model <m>`` on the launch
    command. Absent/blank falls back to the role-based default from
    ``system_settings.toml`` [models] (see ``_read_model_defaults``) — the
    role is derived from ``window`` the same way ``_action_spawn_session``
    derives it for the operator-singleton guard. No role default resolves to
    Fable — a fleet-wide burn is only possible via an explicit ``model``.

    T-0871 (T-0866): a claude launch command now ALWAYS carries both
    ``--model`` and ``--effort``. Previously, a role with no [models] entry
    and no built-in got no ``--model`` at all and the spawned ``claude`` read
    the worker linux user's ``~/.claude/settings.json``; and nothing anywhere
    passed ``--effort``, so every turn ran at the CLI's own vendor-owned
    per-model ``default_effort``. Both resolve through the four-layer order
    ``explicit arg -> [section].<role> -> [section]."*" -> built-in <role> ->
    built-in "*"`` (:func:`_resolve_claude_model` /
    :func:`_resolve_claude_effort`), and effort is additionally CLAMPED to
    :data:`fleet_model.EFFORT_CEILING` — including an explicit ``effort``
    argument, which is deliberately not a way to bypass the ceiling.

    T-0909: ``model_reason`` is the free-text justification a dispatcher gives
    for reaching past the step-down model (``bsq spawn --why``). It is not
    validated or interpreted here — it is stamped on the session md and written
    to :mod:`model_dispatch`'s ledger so "why did this run on Opus" is
    answerable from data instead of from a role contract nobody is measured
    against. ``dispatched_by`` names the surface that asked (``bsq spawn``,
    ``routine``, ``api``…), which is what separates an agent's CHOICE from an
    automated caller's config default in the compliance number.

    Opens a new tmux window, starts claude (no resume), and optionally
    sends an initial_prompt after a short delay.

    If task_id is provided, it rides the per-process ``BOT_SQUAD_TASK_ID``
    env on the launch command (T-0525 — the shared ``.claude/task_id`` marker
    is retired, and T-0324 removed its last reader) so the SessionStart hook
    links the new session to that backlog task automatically.

    If initiative is provided (a filename under vision/initiatives/), the
    SessionStart hook is told via the BOT_SQUAD_INITIATIVE env var to use
    that file instead of the project's global active_initiative. Lets the
    stakeholder spawn multiple TLs on different initiatives in parallel.

    T-0001: when ``initiative`` is set, the new tmux window is routed into
    a sibling session named ``<slug>-<initiative-stem>`` instead of the
    main ``<slug>`` session (which hosts the operator pane). Devs spawned
    with the same initiative arg join that same sibling session — keeping
    initiative-team traffic separated per-team and the operator pane
    uncluttered. ``initiative=None`` keeps the legacy behaviour.

    If owner is provided (T-0080), the spawned session md gets stamped
    with ``owner: <username>`` so per-user listing filters can scope
    results without relying on the SID linux_user prefix. The owner is
    the UI username from the JWT claims, not the linux_user.

    T-0128: when ``parent_sid`` is provided, it is the SID of the session
    that requested this spawn (operator→TL, TL→dev). It is stamped into the
    new session's md frontmatter so the session-tree is reliable across
    worker restarts instead of being reconstructed from the
    task→initiative→TL heuristic. Strictly optional and backward-compatible:
    legacy spawns / callers that omit it leave the field unset, and the
    ``backfill_parent_sid`` pass populates it for them via the heuristic.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: unknown project slug {slug!r}")

    # T-0321: when the caller didn't pass an explicit owner_user (the human
    # scoping username), derive it from `owner` — so TL-spawned devs and
    # constant-team sessions still scope to a real user, not the sentinel.
    if owner_user is None:
        owner_user = _derive_owner_user(cfg, slug, owner)

    # T-0239: enforce the user-set parallel-sessions cap BEFORE any spawn side
    # effect (the task_id marker, tmux session, pane). At/over cap is a
    # capacity-reached refusal so the task stays pending, never a silent drop.
    # T-0966: `slug` also brings the per-project pace cap (the parallelism
    # target the user sets) into admission — it was advisory-only before.
    _enforce_parallel_cap(cfg, slug)
    # T-0306: same for the max_total_tokens budget (output tokens this quota
    # period). 0 = unlimited; over-budget refuses so the task stays pending.
    _enforce_token_cap(cfg)

    # T-0208: defense-in-depth — tolerate a quoted frontmatter scalar passed
    # through from a ticket (`initiative: "x.md"`). The bsq read_frontmatter
    # fix strips these upstream, but a caller (older bsq, raw worker action)
    # may still hand us `"x.md"`; normalize once here so the validator, the
    # task-md initiative stamp, the tmux session name, and the env var all
    # see the bare value.
    if initiative:
        initiative = _strip_surrounding_quotes(initiative.strip())

    cwd = str(project.repo_path)
    user = _get_current_user()

    # Item 3 (audit Fork-2 Part A): claim the task under bind_task's
    # ``.task-claim.lock`` for the WHOLE spawn span (check → spawn → seed-meta
    # stamp) so a concurrent spawn for the SAME task can't interleave and
    # double-bind. Refuse at the OPEN if a live session already owns it (before
    # writing the marker / opening a tmux window — no wasted session). The lock
    # is held until the seed-meta stamp below makes the new session a live owner,
    # then closed (closing the fd releases the flock); on any error path the
    # frame unwinds and CPython drops + closes the fd, releasing the lock. This
    # converts gc_stale_bindings from correctness-critical to a crash-only
    # backstop (see its docstring).
    _claim_fd = None
    if task_id:
        _claim_backlog = cfg.data_dir / slug / "backlog"
        _claim_backlog.mkdir(parents=True, exist_ok=True)
        _claim_fd = open(_claim_backlog / ".task-claim.lock", "w")
        fcntl.flock(_claim_fd, fcntl.LOCK_EX)
        _claim_live = _live_task_owner(cfg.data_dir, slug, task_id.strip())
        if _claim_live is not None:
            _claim_fd.close()
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"spawn: task {task_id.strip()} already bound to live session "
                f"{_claim_live}; refusing dup-bind"
            )

    # T-0525: the task_id is carried to the new claude via a PER-PROCESS env var
    # (BOT_SQUAD_TASK_ID, set in the launch command below) — NOT the old shared
    # `<repo>/.claude/task_id` marker. That marker was a single mutable file in
    # the SHARED working tree: under concurrent (cross-cluster) spawns, spawn B's
    # write clobbered spawn A's before A's SessionStart hook read it, cross-wiring
    # A's PRIMARY binding to B's task. The env is per-process, so concurrent
    # spawns share no mutable binding state and cannot clobber each other. We also
    # best-effort REMOVE any stale marker a pre-fix deploy (or an external claude)
    # may have left, so an env-less reader can't be poisoned by old residue.
    if task_id:
        try:
            stale_marker = project.repo_path / ".claude" / "task_id"
            if stale_marker.exists():
                stale_marker.unlink()
        except OSError:
            # Best-effort: the env channel is authoritative; a lingering marker is
            # ignored by the env-first hook for every worker spawn anyway.
            pass

    # T-0038: when both a task and an initiative are known at spawn time,
    # stamp the initiative onto the task md so it's queryable without grep
    # (board group-by, filter, etc.). Existing initiative wins — operator
    # classification is authoritative.
    if task_id and initiative:
        try:
            _write_task_initiative_if_absent(
                cfg.data_dir / slug / "backlog",
                task_id.strip(),
                initiative.strip(),
            )
        except OSError:
            # Best-effort: the spawn itself is the source of truth; the task
            # md stamp is a convenience for the UI.
            pass

    # T-0001: initiative-keyed tmux session routing — TL spawns with an
    # initiative arg land in a sibling `<slug>-<initiative-stem>` session,
    # not the main `<slug>` session shared with the operator. Devs spawned
    # with the same initiative arg join that sibling session, keeping the
    # operator pane uncluttered.
    target_session = _tmux_session_name(slug, initiative)
    _ensure_project_tmux_session(slug, cwd, initiative)

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    # bash -lc so claude (in ~/.local/bin) is on PATH — the worker's
    # systemd env does not include the user's local bin directory.
    # --dangerously-skip-permissions: see resume() rationale above.
    # BOT_SQUAD_INITIATIVE: per-session initiative override (Phase 4).
    # BOT_SQUAD_OWNER (T-0080): per-session owner stamp picked up by the
    # SessionStart hook and written into the SessionMd frontmatter.
    env_prefix_parts: list[str] = []
    # T-0525: per-process task binding channel. The SessionStart hook reads
    # BOT_SQUAD_TASK_ID in preference to the (now-unused) shared marker, so
    # concurrent spawns can't cross-wire each other's primary. Mirrors the
    # BOT_SQUAD_INITIATIVE / BOT_SQUAD_OWNER passthrough below.
    if task_id:
        env_prefix_parts.append(f"BOT_SQUAD_TASK_ID={shlex.quote(task_id.strip())}")
    if initiative:
        # Basic safety: only basename, must end .md, no slashes/..
        clean = initiative.strip()
        if "/" in clean or ".." in clean or not clean.endswith(".md"):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid initiative name {initiative!r}")
        env_prefix_parts.append(f"BOT_SQUAD_INITIATIVE={shlex.quote(clean)}")
    if owner:
        # Username sanity: alnum + _.- only. The username is API-provided
        # (JWT claim) but we still defence-in-depth-validate before shoving
        # it into a shell env-var assignment.
        owner_clean = owner.strip()
        if not owner_clean or not _valid_owner(owner_clean):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid owner {owner!r}")
        env_prefix_parts.append(f"BOT_SQUAD_OWNER={shlex.quote(owner_clean)}")
    if owner_user:
        # T-0321: the human UI username for per-user scoping (distinct from
        # `owner`, which doubles as the constant-team/TL-SID binding sentinel).
        # T-0895: the SAME class as `owner`, so it shares `_OWNER_RE` instead of
        # keeping a second copy of the literal — the copy had the `$`
        # trailing-newline hole after the owner one was fixed, which is how a
        # duplicated boundary regex normally rots.
        ou_clean = owner_user.strip()
        if not ou_clean or not _OWNER_RE.match(ou_clean):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid owner_user {owner_user!r}")
        env_prefix_parts.append(f"BOT_SQUAD_OWNER_USER={shlex.quote(ou_clean)}")
    env_prefix = (" ".join(env_prefix_parts) + " ") if env_prefix_parts else ""
    # T-0614: descriptive /resume-picker name — the SID-derived window string
    # (+ task id) we already compose, so the native picker is navigable
    # instead of showing first-message snippets. claude >= 2.1.196.
    _display_name = _claude_session_name(window, task_id)
    # T-0623: explicit model wins; else the role-based default; else no flag
    # (settings.json default). T-0694: whichever it is, it must pass through
    # fleet_model.resolve_model before landing on the launch command below —
    # this is the ONE choke point every spawn caller (bsq CLI, the
    # spawn_session/ensure_user_conversation worker actions) funnels through,
    # so an alias like 'fable' gets canonicalized instead of reaching
    # `claude --model` unresolved (silently ignored -> wrong model, zero
    # error) and a genuinely bogus value errors loudly here instead.
    _role = _derive_role(window, task_id, initiative)
    from bot_squad_worker import fleet_model as _fleet_model
    _explicit_choice = (model or "").strip()
    try:
        _provider_name = _agent_provider.provider_for_model(
            _explicit_choice,
            _fleet_model.get_provider(_caps_config_dir(cfg), linux_user=user),
            provider,
        )
    except ValueError as exc:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: {exc}") from exc
    # T-0871: for provider `claude` the model and the effort are BOTH always
    # stated. `_resolve_claude_model` cannot return "" (see its docstring), so
    # the launch command can no longer fall through to whatever
    # ~/.claude/settings.json happens to hold; `_resolve_claude_effort` does
    # the same for a flag we previously never passed at all.
    _model = ""
    _model_source = ""
    _effort = ""
    _effort_source = ""
    if _provider_name == "claude":
        _model, _model_source = _resolve_claude_model(
            _caps_config_dir(cfg), _role, _explicit_choice)
        try:
            _effort, _effort_source = _resolve_claude_effort(
                _caps_config_dir(cfg), _role, effort or "")
        except ValueError as exc:
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: {exc}") from exc
    elif _explicit_choice and _explicit_choice != "codex":
        _model, _model_source = _explicit_choice, "explicit"
    if _model:
        try:
            _model = _fleet_model.resolve_model(_model, provider=_provider_name)
        except ValueError as exc:
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: {exc}") from exc
    _provider = _agent_provider.get(_provider_name)
    # T-0871 DoD: the concrete resolved values and their PROVENANCE reach the
    # worker log at spawn — never None/unset, so an audit of why a session ran
    # a given model/effort does not have to reconstruct it from the config.
    log.info(
        "spawn %s/%s role=%s provider=%s model=%s (%s) effort=%s (%s)",
        slug, window, _role, _provider_name,
        _model or "-", _model_source or "-",
        _effort or "-", _effort_source or "-",
    )
    shell_cmd = env_prefix + _provider.launch_command(
        model=_model,
        display_name=_display_name,
        initial_prompt=None,
        effort=_effort,
    )
    _provider_sessions_before = (
        _provider.session_ids(cwd, _get_user_home())
        if _provider_name == "codex"
        else set()
    )

    result = _run([
        "tmux", "new-window", "-d",
        "-t", f"{target_session}:",
        "-n", window,
        "-c", cwd,
        "bash", "-lc", shell_cmd,
    ])
    if result.returncode != 0:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: tmux new-window failed: {result.stderr}")

    # Discover new pane
    time.sleep(0.5)
    post_panes = list_panes()
    new_panes = [p for p in post_panes if p.pane_id not in pre_panes]
    if not new_panes:
        new_panes = [p for p in post_panes if p.window == window]
    if not new_panes:
        from bot_squad_worker.actions import ActionError
        raise ActionError("spawn: could not locate new pane after tmux new-window")

    new_pane = max(new_panes, key=lambda p: int(p.pane_id.lstrip("%")) if p.pane_id.lstrip("%").isdigit() else 0)
    new_sid = compute_sid(user, new_pane.window, new_pane.pane_id)

    # T-0909: one durable line per dispatch. The `log.info` above says the same
    # thing to the journal, which rotates and needs privileges to read; this is
    # the copy `bsq model compliance` and the operator's session-start banner
    # read, and it is what this ticket's before/after number is computed from.
    # Never raises (see model_dispatch.record).
    try:
        from bot_squad_worker import model_dispatch as _model_dispatch
        _model_dispatch.record(
            cfg.data_dir,
            kind="spawn",
            slug=slug,
            sid=new_sid,
            window=window,
            task_id=(task_id or "").strip(),
            role=_role,
            provider=_provider_name,
            model=_model,
            source=_model_source,
            effort=_effort,
            effort_source=_effort_source,
            reason=(model_reason or "").strip(),
            dispatched_by=(dispatched_by or "").strip(),
        )
    except Exception:
        # Belt AND braces: `record` swallows its own errors, but the guarantee
        # "a cost-control ledger can never fail a spawn" must not depend on the
        # module a future edit might make raise (an import error, a signature
        # change). Pinned by test_a_broken_ledger_does_not_break_the_spawn.
        log.warning("spawn %s/%s: model-dispatch ledger write failed",
                    slug, window, exc_info=True)

    # T-0078: pre-stamp tmux_session on the SessionMd so the field is
    # populated even before the SessionStart hook fires (and survives the
    # hook's rewrite, which now preserves tmux_session via env passthrough).
    # We use the deterministic _tmux_session_name(slug, initiative) value;
    # the hook's `tmux display-message #S` read against the live pane will
    # converge on the same string. pane.session would also work but legacy
    # 5-field tmux output drops it.
    seed_meta_file = _session_file(cfg.data_dir, slug, new_sid)
    seed_meta = _read_session_metadata(seed_meta_file) or {}
    seed_meta.setdefault("sid", new_sid)
    seed_meta["tmux_session"] = target_session
    seed_meta["provider"] = _provider_name
    seed_meta.setdefault("window", window)
    seed_meta.setdefault("cwd", cwd)
    seed_meta.setdefault("role", _role)
    seed_meta.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    seed_meta.setdefault("status", "active")
    if owner:
        seed_meta.setdefault("owner", owner)
    if owner_user:
        seed_meta.setdefault("owner_user", owner_user)
    # T-0964: the (slug, gid) key a user-conversation session used to carry in
    # its window. It has to be stamped HERE, at spawn, for the same reason the
    # window worked: `live_user_conversation_sid` must be able to find this
    # attendant before the SessionStart hook has ever fired, or the very next
    # inbound message spawns a duplicate.
    if global_user_id:
        seed_meta.setdefault(GLOBAL_USER_ID_FIELD, str(global_user_id).strip())
    if initiative:
        seed_meta.setdefault("initiative", initiative)
    # T-0678: an EXPLICIT `model` arg (not the role-based/settings.json
    # fallback `_model` resolves to when this is blank — see above) is a
    # sticky per-session override: stamp it now so a later `resume()` still
    # honors it. A caller that omitted `model` gets no field here, so it
    # keeps following whatever the role/fleet default is at RESUME time,
    # rather than freezing today's fallback onto the session forever.
    # T-0698 audit: stamp the `_model` value already validated above via
    # fleet_model.resolve_model, not the raw `model` argument. `resume()`
    # reads this field straight onto `claude --model` with no validation step
    # of its own, so persisting an outright-bogus value would silently
    # reproduce the exact T-0694 bug (claude ignores an unrecognized --model
    # value and falls back to the account default with zero warning) the very
    # first time this session resumes. T-0704: a class alias like "opus" is a
    # deliberately UNexpanded passthrough here — `claude` resolves it to
    # latest-in-class at resume time, which is the point (never goes stale).
    # T-0871: the effort stamp is UNCONDITIONAL, unlike `model` above, and the
    # asymmetry is deliberate. `model` is sticky-only-when-explicit so a
    # role-defaulted session re-reads the role default at resume time instead
    # of freezing today's value (T-0678). Effort has no such per-session
    # override channel, and `resume()` re-clamps whatever it reads here
    # through `_resolve_claude_effort`, so stamping it always costs nothing
    # and makes "what effort is this session running at" answerable from the
    # md alone rather than from the launch command of a window that may be
    # long gone. `model_source` rides along for the same audit reason.
    if _provider_name == "claude":
        if _effort:
            seed_meta["effort"] = _effort
        if _model_source:
            seed_meta["model_source"] = _model_source
        # T-0909: the effort's PROVENANCE, alongside the model's. `effort` has
        # been stamped unconditionally since T-0871, which makes the stamp
        # ambiguous: "high" reads identically whether a dispatcher asked for it
        # or the role default supplied it. A relaunch that carries the value
        # forward needs to tell those apart — inheriting a role DEFAULT would
        # freeze today's config onto every successor, the exact staleness
        # T-0678 avoided for `model`. See autocompact._relaunch.
        if _effort_source:
            seed_meta["effort_source"] = _effort_source
        # T-0909: the stated reason for a premium model rides the md too, so a
        # session can be asked "why are you on Opus" without a ledger lookup.
        _reason_clean = (model_reason or "").strip()
        if _reason_clean:
            seed_meta["model_reason"] = _reason_clean[:500]
    if _explicit_choice and _provider_name == "claude":
        seed_meta["model"] = _model
    elif _provider_name == "codex":
        if _model:
            seed_meta["model"] = _model
        else:
            seed_meta.pop("model", None)
    discovered_id = None
    if _provider_name == "codex":
        # A fresh Codex command does not carry its generated UUID in /proc.
        # Find the new rollout by set difference so another Codex session in
        # the same shared cwd cannot be mistaken for this pane.
        for _ in range(10):
            discovered_id = _pane_agent_session_id_from_proc(
                new_pane.pid, _provider_name
            )
            if discovered_id:
                break
            new_ids = (
                _provider.session_ids(cwd, _get_user_home())
                - _provider_sessions_before
            )
            if new_ids:
                discovered_id = sorted(new_ids)[-1]
                break
            time.sleep(0.2)
    else:
        discovered_id = _provider.discover_session_id(cwd, _get_user_home())
    if discovered_id:
        seed_meta["claude_uuid"] = discovered_id
    # T-0157: stamp the spawning linux user so the SessionMd carries an
    # explicit user mark (the SID prefix already encodes it, but the field
    # makes per-user listing/grouping robust to SID rotation).
    seed_meta.setdefault("linux_user", user)
    # T-0128: stamp the requesting session's SID as parent_sid so the
    # session-tree survives worker restart without the heuristic. Only
    # when provided and non-self; never clobber an already-set value.
    if parent_sid:
        ps = str(parent_sid).strip()
        if ps and ps != "~" and ps != new_sid and not seed_meta.get("parent_sid"):
            seed_meta["parent_sid"] = ps
    if task_id:
        # Item 3: stamp the claim so the new session is IMMEDIATELY a live task
        # owner (before the SessionStart hook runs), closing the dup-bind TOCTOU.
        # This write is correctness-critical under the claim lock — it MUST raise
        # on failure (a swallowed error silently reopens the race), so it is NOT
        # wrapped in the best-effort OSError guard the no-task path keeps.
        seed_meta["task_id"] = task_id.strip()
        seed_meta["status"] = "active"
        _write_session_metadata(seed_meta_file, seed_meta)
        # Now a live owner — release the claim flock (closing the fd unlocks).
        if _claim_fd is not None:
            _claim_fd.close()
            _claim_fd = None
    else:
        try:
            _write_session_metadata(seed_meta_file, seed_meta)
        except OSError:
            # Best-effort: the hook will populate the field next time it fires.
            pass

    # T-0105: stamp the freshly-spawned SID into the task md's
    # session_history list so the task carries forensics for *every*
    # session that worked on it, not just the current binding. Best-effort.
    if task_id:
        try:
            _append_task_session_history(
                cfg.data_dir / slug / "backlog",
                task_id.strip(),
                new_sid,
            )
        except OSError:
            pass

    # Deliver the initial prompt once the composer is up.
    #
    # T-0126 first gated delivery on a `❯`-composer poll (kept below) but still
    # typed via `send-keys <long-literal>`, which is the unreliable part the
    # operator worked around by hand: a long send-keys literal can interleave
    # with the TUI, mis-escape, or exceed limits, and the trailing Enter lands
    # inside tmux's bracketed-paste wrap instead of submitting.
    #
    # T-0144 root-causes it: deliver via the operator's proven manual recovery
    # — load the text into a tmux paste buffer and paste it (bracketed-paste,
    # length-safe, atomic), then a SEPARATE Enter to submit. See
    # `_deliver_prompt`. The composer-ready poll still guards against typing
    # before claude's TUI exists; if `❯` never appears, raise so the caller can
    # recover via inject_input (the pane is up).
    # T-0904: a provider whose CLI fires no SessionStart hook (codex) never
    # receives the MESSAGE BUS / bsq orientation that hook prints, so it rides
    # the prompt instead — the one channel that reaches EVERY provider. Note
    # the delivery is no longer gated on the caller having supplied a brief: a
    # hook-less session spawned with no initial_prompt still has to be told
    # what it is sitting in, and used to be told nothing at all.
    _prompt = initial_prompt
    if _boot.needs_prompt_orientation(_provider_name):
        _prompt = _boot.with_orientation(
            initial_prompt, sid=new_sid, slug=slug, role=_role,
            data_dir=cfg.data_dir,
        )
    if _prompt:
        if not _wait_for_agent_composer_ready(new_pane.pane_id, _provider_name):
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"spawn: {_provider_name} composer was not ready for sid {new_sid} within "
                f"{_COMPOSER_READY_TIMEOUT_SEC:.0f}s — initial_prompt not delivered "
                "(pane is up; recover via inject_input)"
            )
        _deliver_prompt(new_pane.pane_id, _prompt,
                        data_dir=cfg.data_dir, sid=new_sid)

    return {"ok": True, "sid": new_sid}


# ---------------------------------------------------------------------------
# Phase 9: multi-binding helpers
# ---------------------------------------------------------------------------

def _full_task_set(meta: dict) -> set[str]:
    """Return {primary, *extras} of task IDs for a session md frontmatter."""
    out: set[str] = set()
    tid = meta.get("task_id")
    if tid and tid != "~":
        out.add(tid)
    extras = meta.get("extra_task_ids") or []
    if isinstance(extras, list):
        for t in extras:
            if t and t != "~":
                out.add(t)
    return out


def _full_initiative_set(meta: dict) -> set[str]:
    """Return {primary, *extras} of initiative basenames for a session md."""
    out: set[str] = set()
    init = meta.get("initiative")
    if init and init != "~":
        out.add(init)
    extras = meta.get("extra_initiatives") or []
    if isinstance(extras, list):
        for i in extras:
            if i and i != "~":
                out.add(i)
    return out


def _find_owner(
    data_dir: Path,
    slug: str,
    *,
    task_id: str | None = None,
    initiative: str | None = None,
) -> str | None:
    """Scan all session mds; return SID of the session that already holds the
    given task_id or initiative (primary or extras). None if free.
    """
    sess_dir = data_dir / slug / "sessions"
    if not sess_dir.exists():
        return None
    for md in sorted(sess_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        sid = meta.get("sid", md.stem)
        if task_id and task_id in _full_task_set(meta):
            return sid
        if initiative and initiative in _full_initiative_set(meta):
            return sid
    return None


# T-0237 Layer-1: strict task<->session binding cap. The reframe ("each task is
# a one-time run executed by a tightly-capped set of sessions") fixes the cap at
# ONE live session per task (operator-confirmed S1). Suspended/archived holders
# are historical records (their binding is GC'd to last_task_id) and never count
# toward the cap, so a fresh run can always rebind an abandoned task.
BINDING_CAP = 1


def _is_live_holder(meta: dict) -> bool:
    """Whether a session counts toward a task's binding cap: a running process
    (status active/paused), not a suspended/archived historical record.

    Uses the persisted ``status`` (which ``gc_sessions`` keeps tick-synced with
    real tmux panes) rather than a live pane scan, so the bind path stays a pure
    data operation under the claim flock.
    """
    if str(meta.get("archived", "")).lower() == "true":
        return False
    return str(meta.get("status", "")).lower() in ("active", "paused")


# T-0524: roles that ride the universal session lifecycle as always-on
# COORDINATION (the operator + team-leads), NOT the disposable leaf-dev workload
# the parallel cap is meant to govern. Counting them conflated the coordination
# layer with leaf devs: a healthy org (1 operator + N TLs + a few devs)
# false-fulled with almost no actual devs live, throttling the very spawn the
# F2.7 ramp wanted (TL-A: refused at 19/19 then 12/6 with few real devs live).
# ``prod-teamlead`` is a teamlead variant, so it is coordination too. ``qa`` /
# ``user-conversation`` stay COUNTED — they are transient workers that burn a
# Claude session, so excluding them would risk over-spawn (conservative: only
# stop counting the few always-on coordinators).
_COORDINATION_ROLES = frozenset({"operator", "teamlead", "prod-teamlead"})


def _counts_against_dev_cap(meta: dict) -> bool:
    """Whether a session consumes a slot of the LEAF-DEV parallel cap (T-0524).

    True only when the session is BOTH a live holder (status active/paused, not
    archived/suspended — so reaper-lag on dead/archived/suspended rows can never
    inflate the count) AND a leaf-dev role (NOT an always-on coordinator). The
    CALLER additionally requires the SID to map to a live claude pane
    (``_live_agent_sids``) — the T-0397/T-0402 reconcile that drops a crashed
    dev whose md still reads ``active``.
    """
    if not _is_live_holder(meta):
        return False
    return _role_of(meta) not in _COORDINATION_ROLES


def _live_task_owner(
    data_dir: Path, slug: str, task_id: str, *, exclude_sid: str | None = None
) -> str | None:
    """SID of a *live* session (see ``_is_live_holder``) already holding
    ``task_id`` (primary or extras), or None. Suspended/archived holders are
    skipped — they do not gatekeep a rebind under the T-0237 cap.

    T-0402: ``_is_live_holder`` trusts the persisted ``status: active`` alone,
    but a crashed dev's md lingers ``active`` until the next ``gc_sessions``
    tick reconciles it (T-0401). Until then such a phantom would gatekeep its
    task ('already bound to live session {dead}'), the exact failure this fn
    promises to prevent. So a holder must ALSO map to a pane running a live
    claude agent (``_live_agent_sids``) — the same reconcile the T-0397
    ``_count_live_sessions`` fix (d0b3cdc) applies to the parallel cap, checked
    here in real time rather than waiting on the next tick. Computed once per
    call (a tmux + /proc scan), so the bind path is no longer a pure data op
    but stays under the claim flock.
    """
    sess_dir = data_dir / slug / "sessions"
    if not sess_dir.exists():
        return None
    live = _live_agent_sids()
    for md in sorted(sess_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        sid = meta.get("sid", md.stem)
        if exclude_sid and sid == exclude_sid:
            continue
        if task_id in _full_task_set(meta) and _is_live_holder(meta) and sid in live:
            return sid
    return None


# T-0478 (M2/F2.4): user-conversation session identity helpers. A user-
# conversation session is keyed on its (slug, global_user_id) pair; the gid is
# carried IN the tmux window (`<gid>-user-conversation`) so the session is
# self-identifying from its SID alone — no md field that the SessionStart hook
# could drop. `ensure_user_conversation` (actions.py) computes the expected
# window for a known gid and matches it, so the gid is never parsed back OUT of
# the window (robust regardless of gid content).
#: ``\Z`` not ``$`` (T-0895): this guard's own docstring below calls itself the
#: thing that stops a crafted value smuggling a shell/tmux metacharacter into a
#: spawn command or a path — the third copy of the trailing-newline hole fixed
#: on the owner validators.
_GLOBAL_USER_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def user_conversation_window(global_user_id: str) -> str:
    """The LEGACY ``<gid>-user-conversation`` window name — and, still, the gid
    validator every caller relies on.

    T-0964 retired this as the name a NEW attendant is spawned under (the user
    must never be shown a ``gu_…`` id — :func:`user_facing_window` picks the
    name now), but it is NOT dead code and must keep producing the exact same
    string: :func:`live_user_conversation_sid` and
    ``_find_suspended_user_conversation`` match it as the fallback that keeps
    attendants spawned before the rename resolving to their user, and
    :mod:`uc_redrive` derives the same shape.

    Always ends in the ``user-conversation`` marker so ``_derive_role`` maps it
    to the user-conversation role regardless of the gid prefix. Raises on a gid
    that isn't a single safe segment (so a crafted value can't smuggle a shell /
    tmux metacharacter into the spawn command or a path) — which is why every
    caller still runs the value through here even when it discards the result.
    """
    gid = str(global_user_id or "").strip()
    if not _GLOBAL_USER_ID_RE.match(gid):
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"invalid global_user_id {global_user_id!r}")
    return f"{gid}-user-conversation"


#: T-0964: the md field that carries the (slug, gid) pairing now that the gid
#: is no longer spelled into the window. Whitelisted through every md rebuild
#: (``suspend``, ``resume``) the way ``owner_user`` and ``model`` are — a
#: rebuild that drops it un-keys a live attendant from its user.
GLOBAL_USER_ID_FIELD = "global_user_id"


def session_global_user_id(meta: dict | None) -> str:
    """The global user id a user-conversation session attends, or "".

    Reads the T-0964 md field, treating the registry's ``~`` unset sentinel and
    a missing field as absent — the same shape as :func:`_parent_sid_of`.
    """
    if not meta:
        return ""
    val = meta.get(GLOBAL_USER_ID_FIELD)
    if not val or val == "~":
        return ""
    return str(val).strip()


def _mothership_users(cfg: Any) -> list[dict]:
    """The mothership user records, or [] when the store is absent/unreadable.

    Read straight off disk (``data/_mothership/users.json``) rather than through
    the API: the worker has no HTTP client for its own API, and every caller
    here degrades to a name without a username rather than failing.
    """
    try:
        path = Path(cfg.data_dir) / "_mothership" / "users.json"
        data = json.loads(path.read_text())
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    users = data.get("users") if isinstance(data, dict) else None
    return [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []


def username_for_global_user_id(cfg: Any, global_user_id: str) -> str:
    """``gu_…`` → that user's username, or "" when unknown.

    T-0964: used ONLY to build a readable window suffix for a SECOND concurrent
    attendant (``user_session_flomaster``). The id itself is never rendered —
    an unresolvable gid falls back to a positional suffix, never to the gid.
    """
    gid = str(global_user_id or "").strip()
    if not gid:
        return ""
    for u in _mothership_users(cfg):
        if str(u.get("id") or "") == gid:
            name = str(u.get("username") or "").strip()
            return _sanitise_window(name) if name else ""
    return ""


def _window_from_sid(sid: str) -> str:
    """The tmux window embedded in a SID ``S-<user>-<window>-p<pane>``.

    Mirrors the SessionStart hook's derivation
    (``sid.rsplit("-p", 1)[0].split("-", 2)[-1]``) so a user-conversation
    session is identified by its IMMUTABLE SID alone — no md field the hook
    could drop. ``rsplit`` on the LAST ``-p`` and ``split(.., 2)`` tolerate
    ``-`` / ``-p`` inside the window (e.g. a gid carrying them). Returns "" when
    the SID has no ``-p`` pane segment.
    """
    s = str(sid or "")
    if "-p" not in s:
        return ""
    return s.rsplit("-p", 1)[0].split("-", 2)[-1]


def live_user_conversation_sid(
    cfg: Any, slug: str, global_user_id: str
) -> str | None:
    """SID of the LIVE user-conversation session attending ``(slug,
    global_user_id)``, or None when none is running.

    The single-attendant invariant behind ``ensure_user_conversation``: a
    non-None result means the user already has a live attendant, so a new
    inbound message is routed to it rather than spawning a duplicate.

    T-0478 (REOPENED) fix — this used to be a LIVE-PANE scan scoped to
    ``pane.session == slug``. That scoping was a structural dup-spawn bug:
    spawns land the window in the per-INITIATIVE sibling tmux session
    (``<slug>-<initiative-stem>``, see :func:`_tmux_session_name`), never the
    bare ``slug`` session, so the reuse match could NEVER fire and every
    inbound message fanned out a fresh attendant. We now scan this project's
    session mds (``data/<slug>/sessions/*.md`` — inherently project-scoped, and
    catching attendants in ANY tmux session) and identify the attendant by the
    window derived from each md's immutable ``sid`` (hook-proof; no custom md
    field). Liveness is confirmed against the real process via
    :func:`_live_agent_sids` (a pane+/proc scan, NOT tmux-session-scoped, NOT
    the md ``status`` field — a just-spawned task-less seed md has no
    ``status: active`` yet, so an md-status gate would reopen the very race the
    per-(slug,gid) flock in ``_action_ensure_user_conversation`` closes).
    Tolerant of a missing sessions dir / broken tmux server (→ None).

    T-0964 — TWO ways to match, because the key moved. The gid now lives in the
    md's ``global_user_id`` field (the window is a user-facing name and no
    longer spells it), and that is tried first; the legacy
    ``<gid>-user-conversation`` window match is kept as the fallback so an
    attendant spawned before this change still resolves to its user.
    """
    want = user_conversation_window(global_user_id)  # validates gid
    gid = str(global_user_id).strip()
    sess_dir = Path(cfg.data_dir) / slug / "sessions"
    if not sess_dir.exists():
        return None
    try:
        live = _live_agent_sids()
    except Exception:  # noqa: BLE001 — a tmux hiccup must not break the gate
        return None
    for md in sorted(sess_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        sid = str(meta.get("sid") or md.stem)
        if session_global_user_id(meta) != gid and _window_from_sid(sid) != want:
            continue
        if sid in live:
            return sid
    return None


def live_user_conversation_sids(cfg: Any, slug: str) -> list[dict]:
    """Every LIVE user-facing session on this project.

    T-0963/T-0964: this is the "how many universal sessions is this project
    running" read — what ``bsq start`` asks before it decides between launching
    one and telling the user where the running one is, and what
    :func:`user_facing_window` asks before it decides between
    ``universal_bsq_session`` and a per-user ``user_session_<who>``.

    A row is ``{sid, window, tmux_session, global_user_id}``. Role is derived
    the same way everything else derives it (:func:`_role_of`), so a session
    that MORPHED into ``user-conversation`` (the T-0932 «отделение себя в
    юзер-сессию» half of budding) counts — its window may still say
    ``universal_bsq_session`` at that moment.

    Tolerant of a missing sessions dir / broken tmux server (→ []).
    """
    sess_dir = Path(cfg.data_dir) / slug / "sessions"
    if not sess_dir.exists():
        return []
    try:
        live = _live_agent_sids()
    except Exception:  # noqa: BLE001 — a tmux hiccup must not break the read
        return []
    rows: list[dict] = []
    for md in sorted(sess_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        sid = str(meta.get("sid") or md.stem)
        if sid not in live:
            continue
        window = str(meta.get("window") or _window_from_sid(sid) or "")
        if _role_of(meta, window=window) != "user-conversation":
            continue
        rows.append({
            "sid": sid,
            "window": window,
            "tmux_session": str(meta.get("tmux_session") or ""),
            "global_user_id": session_global_user_id(meta),
        })
    return rows


def user_facing_window(cfg: Any, slug: str, global_user_id: str) -> str:
    """T-0964: the window name a NEW attendant for ``(slug, gid)`` should carry.

    ``universal_bsq_session`` when this project is running none — the ordinary
    case, and the one the stakeholder named. When one is already up for a
    DIFFERENT user, the newcomer gets ``user_session_<username>`` so two panes
    are tellable apart; an unresolvable username degrades to a positional
    ``user_session_2``, never to the gid (rendering the id is the thing this
    ticket exists to stop).

    Validates the gid via :func:`user_conversation_window` — the value still
    reaches a flock filename and a spawn command, so the safe-segment check has
    to keep happening even though the gid no longer lands in the window.
    """
    legacy = user_conversation_window(global_user_id)  # validates the gid
    gid = str(global_user_id).strip()
    # "Someone ELSE's attendant" has to mean the same thing here as it does in
    # `live_user_conversation_sid`, or the two disagree about whose session is
    # on screen. Measured on the live install: filtering on the md field ALONE
    # read the stakeholder's own pre-rename attendant (window-keyed, no field)
    # as a stranger and offered him `user_session_alexey` instead of
    # `universal_bsq_session`. So both matchers, same as there.
    existing = [r for r in live_user_conversation_sids(cfg, slug)
                if r.get("global_user_id") != gid
                and r.get("window") != legacy]
    if not existing:
        return UNIVERSAL_WINDOW
    name = username_for_global_user_id(cfg, global_user_id)
    if name:
        return f"{USER_SESSION_WINDOW}_{name}"
    taken = {r.get("window") for r in existing}
    for n in range(2, 100):
        cand = f"{USER_SESSION_WINDOW}_{n}"
        if cand not in taken:
            return cand
    return USER_SESSION_WINDOW


def project_of_sid(cfg: Any, sid: str) -> str:
    """The project slug that OWNS ``sid``, or ``""`` when nothing claims it.

    T-0746 item (e): a message aimed at a session must fall back to a
    DETERMINISTIC project — specifically the one the TARGET session belongs to,
    never the one whose conversation store the sender happened to write into.
    The live incident is exactly that pair pulled apart: the reaped session was
    watchrobot's while the message (and the "dropped" notice) landed in
    bot-squad's store, because the chat the stakeholder typed in is bound to
    bot-squad. Routing the fallback by the ARRIVAL store would have handed
    watchrobot's question to bot-squad's attendant.

    Ownership is read off disk, not off tmux: ``data/<slug>/sessions/<sid>.md``
    is written at spawn and SURVIVES the reap (verified on the live install —
    the p266 md is still there), which is what makes this resolvable for
    exactly the sessions this is for: the ones that are no longer running.

    Determinism has two halves. Projects are scanned in sorted slug order, and
    the direct ``<sid>.md`` hit wins over the rename-tolerant ``sid:``-field
    scan (:func:`_find_session_md`'s uuid fallback problem in reverse) — so a
    SID that somehow exists under two projects always resolves the same way
    rather than depending on dict ordering. Tolerant of a missing data dir /
    sessions dir / unreadable md (→ skipped, never raises).
    """
    want = str(sid or "").strip()
    if not want:
        return ""
    try:
        data_dir = Path(cfg.data_dir)
        slugs = sorted(cfg.projects)
    except (AttributeError, TypeError):
        return ""
    # Pass 1: the file is named for the SID — the normal case.
    for slug in slugs:
        if (data_dir / slug / "sessions" / f"{want}.md").is_file():
            return slug
    # Pass 2: a tmux window rename leaves the md at the PRE-rename filename
    # while its `sid:` field was rewritten (the mirror of the _find_session_md
    # case). Cheap enough — this only runs when pass 1 found nothing at all.
    for slug in slugs:
        sess_dir = data_dir / slug / "sessions"
        if not sess_dir.is_dir():
            continue
        for md in sorted(sess_dir.glob("*.md")):
            meta = _read_session_metadata(md)
            if meta is not None and str(meta.get("sid") or "") == want:
                return slug
    return ""


# T-0239 slice 2: enforce the user-set resource caps at spawn-time. The caps
# live in the API's system_settings.toml ([caps] section, written by
# /api/system-settings); the worker reads them FRESH on each spawn so a cap
# change takes effect on the next spawn after a worker restart (the PUT returns
# restart_required=True). 0 / absent = unlimited (a fresh/legacy install is
# uncapped — back-compat).
def _caps_config_dir(cfg: Any) -> Path:
    """The config dir holding system_settings.toml. Prefers cfg.config_dir;
    falls back to the install-root layout (``<data_dir>/../config``) so a test
    cfg that carries only data_dir still resolves it."""
    cd = getattr(cfg, "config_dir", None)
    if cd:
        return Path(cd)
    return Path(cfg.data_dir).parent / "config"


def _read_caps(config_dir: Path) -> dict:
    """Fresh-read the [caps] section from system_settings.toml. Missing file /
    unparseable / missing keys → unlimited (0). Negative → 0 (unlimited)."""
    path = Path(config_dir) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {"max_parallel_sessions": 0, "max_total_tokens": 0, "idle_suspend_sec": 0}
    caps = raw.get("caps", {}) or {}

    def _c(key: str) -> int:
        try:
            v = int(caps.get(key, 0))
        except (TypeError, ValueError):
            v = 0
        return v if v > 0 else 0

    return {
        "max_parallel_sessions": _c("max_parallel_sessions"),
        "max_total_tokens": _c("max_total_tokens"),
        # T-0408: idle-but-live suspend window (seconds), 0/absent = OFF.
        "idle_suspend_sec": _c("idle_suspend_sec"),
    }


# T-0623: per-role default `claude --model` value, keyed by the same role
# strings `_derive_role` returns (operator/prod-teamlead/qa/teamlead/dev/
# user-conversation). Ships with ONE built-in default (user-conversation →
# Sonnet, the stakeholder's explicit ask) so a fresh/legacy install without
# a [models] section in system_settings.toml still gets it; every other role
# is unset (falls through to the spawn's explicit `model` or the per-
# linux-user settings.json default). Deliberately no built-in default ever
# names Fable — that model is only ever used via an explicit spawn `model`.
# T-0704: the value is the BARE class alias "sonnet" (not the pinned
# claude-sonnet-5) so it auto-tracks latest-in-class and never goes stale —
# fleet_model.resolve_model passes it through and `claude` resolves it.
#
# T-0871: the ``"*"`` CATCH-ALL is what makes "bot-squad always states the
# model" true rather than aspirational. Before it, a role with no entry here
# and none in [models] (`qa`, `prod-teamlead`, any future role) produced NO
# --model flag at all, and the spawned `claude` silently read the worker linux
# user's ~/.claude/settings.json — a value bot-squad neither chose nor could
# name. "opus" is what that settings.json currently resolves to, so adding
# the catch-all is behaviour-neutral on this install while moving the choice
# from the vendor's file into ours.
_DEFAULT_MODEL_DEFAULTS: dict[str, str] = {
    "user-conversation": "sonnet",
    "*": "opus",
}

# T-0871: per-role default `claude --effort`, same shape and same catch-all
# rule as the model defaults above. `high` is the level EVERY assistant turn
# on this host already runs at (measured on T-0866: 41 082 turns over 7d,
# zero at any other level) — it was the Claude CLI's own per-model
# `default_effort`, never a bot-squad choice. Stating it explicitly is a
# behaviour no-op that takes the choice back; `fleet_model.EFFORT_CEILING`
# then keeps a future config/vendor change from raising it silently.
_DEFAULT_EFFORT_DEFAULTS: dict[str, str] = {"*": "high"}


def _read_defaults_section(
    config_dir: Path, section: str, builtin: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """``(file_map, builtin_map)`` for one role->value section of
    system_settings.toml. Kept SEPARATE (rather than merged) because the
    resolution order in :func:`_layered_default` is not "file wins per key" —
    a file-level ``"*"`` catch-all outranks a built-in ROLE entry, and the
    caller has to be able to report WHICH of the two a value came from.
    Missing file / unparseable / missing section -> an empty file map."""
    path = Path(config_dir) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}, dict(builtin)
    values = raw.get(section)
    if not isinstance(values, dict):
        return {}, dict(builtin)
    return {str(k): str(v) for k, v in values.items()}, dict(builtin)


def _layered_default(
    file_map: dict[str, str], builtin_map: dict[str, str], role: str
) -> tuple[str, str]:
    """Resolve ``role`` through the four layers, in order, returning
    ``(value, source)`` — e.g. ``("opus", "config:dev")`` or
    ``("sonnet", "builtin:*")``. ``("", "")`` when every layer is blank.

    Order: configured role -> configured ``"*"`` -> built-in role -> built-in
    ``"*"``. A blank value at any layer falls through rather than terminating
    the search: under T-0871 "no value" is never an acceptable answer, so an
    empty ``dev = ""`` in the file means "I have no opinion", not "send no
    flag" (which is what it used to mean, and what handed the choice back to
    the CLI's own default).
    """
    for label, source_map, key in (
        ("config", file_map, role),
        ("config", file_map, "*"),
        ("builtin", builtin_map, role),
        ("builtin", builtin_map, "*"),
    ):
        value = (source_map.get(key) or "").strip()
        if value:
            return value, f"{label}:{key}"
    return "", ""


def _read_model_defaults(config_dir: Path) -> dict[str, str]:
    """Fresh-read the [models] section from system_settings.toml — role name
    -> default model string, merged over the built-in
    ``_DEFAULT_MODEL_DEFAULTS`` (which since T-0871 carries a ``"*"``
    catch-all). Missing file / unparseable / missing [models] -> the built-in
    alone. Kept as the flat merged view for callers that just want the map;
    the spawn/resume seam uses :func:`_resolve_claude_model` instead, which
    needs the layer a value came from."""
    file_map, builtin = _read_defaults_section(
        config_dir, "models", _DEFAULT_MODEL_DEFAULTS)
    merged = dict(builtin)
    merged.update(file_map)
    return merged


def _read_effort_defaults(config_dir: Path) -> dict[str, str]:
    """T-0871: the [effort] twin of :func:`_read_model_defaults` — role name
    -> ``claude --effort`` level, merged over ``_DEFAULT_EFFORT_DEFAULTS``."""
    file_map, builtin = _read_defaults_section(
        config_dir, "effort", _DEFAULT_EFFORT_DEFAULTS)
    merged = dict(builtin)
    merged.update(file_map)
    return merged


def _resolve_claude_model(
    config_dir: Path, role: str, explicit: str = ""
) -> tuple[str, str]:
    """T-0871: the value AND the provenance of a claude session's ``--model``.

    Returns ``(model, source)`` and is the ONE resolver both :func:`spawn` and
    :func:`resume` call, so a recycle cannot resolve differently from the
    spawn that preceded it. For provider ``claude`` the model is never "":
    after the four config/built-in layers it falls back to the fleet default
    (``fleet_model.get_model``) and finally to a hard-coded ``sonnet``. That
    last rung is a should-never-happen guard, and its source string says so —
    the point of the whole chain is that the value on the launch command is
    always one bot-squad chose and can NAME, never one the CLI picked for us.
    """
    explicit = (explicit or "").strip()
    if explicit:
        return explicit, "explicit"

    file_map, builtin = _read_defaults_section(
        config_dir, "models", _DEFAULT_MODEL_DEFAULTS)
    value, source = _layered_default(file_map, builtin, role)
    if value:
        return value, source

    from bot_squad_worker import fleet_model as _fleet_model
    fleet = (_fleet_model.get_model(config_dir) or "").strip()
    if fleet and fleet != "codex":
        return fleet, "fleet"

    log.warning(
        "T-0871: no model default resolved for role %r (no [models] entry, no "
        "built-in, no fleet default) — falling back to the hard-coded "
        "'sonnet'. Add a [models] entry (or a '*' catch-all) in "
        "system_settings.toml.", role,
    )
    return "sonnet", "hardcoded-fallback"


def _resolve_claude_effort(
    config_dir: Path, role: str, explicit: str = ""
) -> tuple[str, str]:
    """T-0871: ``(effort, source)`` for a claude session, clamped.

    Same four-layer order as :func:`_resolve_claude_model`. Whichever layer
    wins, the value goes through :data:`fleet_model.resolve_effort` — so the
    ceiling applies to a per-session override exactly as it does to a config
    entry, and an unrecognized level raises rather than silently vanishing.
    """
    from bot_squad_worker import fleet_model as _fleet_model

    explicit = (explicit or "").strip()
    if explicit:
        value, source = explicit, "explicit"
    else:
        file_map, builtin = _read_defaults_section(
            config_dir, "effort", _DEFAULT_EFFORT_DEFAULTS)
        value, source = _layered_default(file_map, builtin, role)

    if not value:
        log.warning(
            "T-0871: no effort default resolved for role %r — falling back to "
            "the ceiling %r.", role, _fleet_model.EFFORT_CEILING,
        )
        return _fleet_model.EFFORT_CEILING, "hardcoded-fallback"

    clamped = _fleet_model.resolve_effort(value)
    if clamped != value:
        log.warning(
            "T-0871: effort %r for role %r (%s) exceeds EFFORT_CEILING — "
            "clamped to %r.", value, role, source, clamped,
        )
        source = f"{source}(clamped)"
    return clamped, source


def _proc_children_map() -> dict[int, list[int]]:
    """Build the ``PPid -> [child pids]`` map from a SINGLE /proc scan.

    T-0416: the per-pane liveness walk used to rebuild this from a full /proc
    iteration on EVERY ``_pane_has_live_claude`` call, so a single
    ``caps_utilization`` (the FE polls it per project) cost
    projects × panes × a full-/proc scan. Build it ONCE per live-count pass and
    hand it to each pane's subtree walk. Returns ``{}`` if /proc is unreadable.
    """
    children: dict[int, list[int]] = {}
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                st = (entry / "status").read_text()
            except OSError:
                continue
            m = re.search(r"^PPid:\s+(\d+)", st, re.M)
            if m:
                children.setdefault(int(m.group(1)), []).append(int(entry.name))
    except OSError:
        return {}
    return children


def _pane_has_live_claude(pane_pid: str, children: dict[int, list[int]] | None = None) -> bool:
    """True iff a supported live agent process exists in this pane's subtree.

    T-0397: pane EXISTENCE is not agent liveness. When claude exits, its tmux
    pane routinely lingers as a bash shell — that dead-claude pane holds no
    agent and must not consume a parallel-cap slot. Conversely a claude session
    briefly running a Bash *tool* shows ``pane_current_command == bash`` while
    claude is still alive as the pane's parent, so a foreground-command check
    would flap; the /proc-subtree walk (mirrors ``_pane_claude_uuid_from_proc``)
    is the stable signal — claude is found whether idle, busy, or fresh.

    T-0416: ``children`` is the prebuilt ``_proc_children_map()``. The hot caller
    (``_live_agent_sids``) builds it ONCE and passes it for every pane; standalone
    callers omit it and one is built on demand (so this never rebuilds per pane in
    the live-count path).
    """
    try:
        root = int(pane_pid)
    except (ValueError, TypeError):
        return False
    if children is None:
        children = _proc_children_map()
    queue: list[int] = [root]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            parts = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\x00")
        except OSError:
            parts = []
        if parts:
            for provider_name in _agent_provider.PROVIDERS:
                if _agent_provider.get(provider_name).process_matches(parts[0]):
                    return True
        queue.extend(children.get(pid, []))
    return False


def _live_agent_sids() -> set[str]:
    """SIDs whose tmux pane has a live supported agent process.
    the parallel cap should count (T-0397).

    Stricter than ``live_pane_map`` (which keys every pane, including the bash
    shells that dead-claude panes fall back to): a dead-claude pane consumes no
    agent slot, so it must not count. Liveness is verified against THIS worker's
    tmux server (the current linux user); a different user's sessions are
    reconciled by their own per-user worker and are not visible here.

    T-0416: the /proc children-map is built ONCE here and reused for every pane's
    subtree walk, instead of a full /proc rescan per pane.
    """
    user = _get_current_user()
    children = _proc_children_map()
    out: set[str] = set()
    for p in list_panes():
        if not _pane_has_live_claude(p.pid, children):
            continue
        try:
            out.add(compute_sid(user, p.window, p.pane_id))
        except Exception:
            continue
    return out


def _count_live_sessions(cfg: Any) -> int:
    """Count THIS worker-user's live sessions across every registered project.

    T-0417 (scope honesty): the cap VALUE is shared config (one
    ``[caps]`` block in system_settings.toml), but enforcement is
    PER-WORKER-USER. Each linux_user runs its own worker (the T-0157 multi-user
    substrate) and counts only sessions whose SID maps to a live claude pane on
    ITS OWN tmux server (``_live_agent_sids``). A session owned by another
    linux_user is in the shared-data-dir md scan but NOT in ``live``, so it is
    not counted here — that user's own worker enforces the cap against it. The
    cap is therefore a per-worker-user concurrency limit, not a single global
    host ceiling; the docstring, the caps meter label, and this enforcement now
    agree (pre-T-0417 the code claimed "system-wide" while only ever counting one
    user, so a cross-user session was silently dropped → under-enforcement).

    T-0397: a session counts only if it is a live-holder (status active/paused,
    not archived) AND its SID maps to a pane with a live claude agent
    (``_live_agent_sids``). The persisted ``status`` field ALONE is unreliable:
    even after T-0401 taught ``gc_sessions`` to reconcile pane_id-less phantoms
    too, that is a periodic TICK — a session that died since the last tick
    still reads ``active`` here, and a dead-claude pane that fell back to bash
    would pass a mere pane-existence check. Re-deriving liveness directly
    (rather than trusting the last reconcile pass) inflated the count (15/15
    while only ~9 agents were live) and made ``_enforce_parallel_cap`` silently
    refuse spawns at a false ceiling. ``backoff._live_count`` delegates here, so
    the AIMD effective_limit and the caps meter (items 7/22) inherit the
    corrected count.

    T-0524: the cap governs the disposable LEAF-DEV workload, so always-on
    COORDINATION sessions (operator + team-leads, see ``_counts_against_dev_cap``
    / ``_COORDINATION_ROLES``) are NOT counted. Conflating them with leaf devs
    false-fulled a healthy org (1 operator + N TLs + a few devs) with almost no
    actual devs live, throttling the F2.7 ramp. Combined with the live-holder +
    live-pane gates above, the count reflects ACTUAL leaf-dev load — not the
    coordination layer, and not reaper-lag on dead/archived/suspended rows.
    """
    return count_live_dev_sessions(cfg)


def count_live_dev_sessions(cfg: Any, slug: str | None = None) -> int:
    """Live leaf-dev sessions — the whole worker-user (``slug=None``) or ONE project.

    The counting body ``_count_live_sessions`` has always had; ``slug`` narrows
    the project loop to a single board. Read that docstring for why each gate is
    here (live-holder + live claude pane + not a coordination role).

    T-0966 made this public and per-project because ``pace.max_in_progress`` now
    gates on it. It is the RIGHT input for a parallelism cap for the reason the
    board label is the wrong one: this quantity is DERIVED from the process table
    and the tmux roster on every call, so no session can silently fail to
    maintain it, and a dev that dies without moving its ticket or clearing its
    binding stops being counted at the next read rather than at the next
    reconcile tick. The ``in_progress`` label is the opposite — a field a session
    has to remember to stamp, whose offset from reality therefore varies (7 of 8
    live devs had not stamped it when T-0966 was measured).
    """
    live = _live_agent_sids()
    n = 0
    slugs = [slug] if slug else list(getattr(cfg, "projects", {}) or {})
    for s in slugs:
        sess_dir = cfg.data_dir / s / "sessions"
        if not sess_dir.exists():
            continue
        for md in sess_dir.glob("*.md"):
            meta = _read_session_metadata(md)
            if meta and _counts_against_dev_cap(meta) and meta.get("sid", md.stem) in live:
                n += 1
    return n


def _enforce_parallel_cap(cfg: Any, slug: str | None = None) -> None:
    """Raise ActionError if spawning would exceed the EFFECTIVE concurrency.

    Three layers (T-0239 cap + T-0249 backoff governor + T-0966 pace cap):
      * the per-project ``pace.max_in_progress`` — the parallelism target the
        USER sets («таргет параллелизма 7»), counted as live dev sessions on
        THIS board (0 = unlimited); checked first because it is the tightest and
        the one he set by hand, and skipped entirely when ``slug`` is absent;
      * the hard ``max_parallel_sessions`` cap is the ceiling (0 = unlimited);
      * the WS-4 backoff governor depresses the effective limit BELOW the cap
        under Claude rate-limit / 5h-usage-limit pressure.
    Admission refuses (the task stays pending/QUEUED, never a silent drop — the
    T-0237 S4 contract) once live sessions reach the effective limit, and the
    message distinguishes a hard-cap refusal from a backoff (pressure) refusal.

    T-0966: before this, ``pace.max_in_progress`` was ADVISORY ONLY — a signal in
    the operator's brief, measured against the ``in_progress`` board LABEL. With
    the label unstamped by 7 of 8 live devs, the number he set read "1 of 7" and
    refused nothing while eight sessions ran. Admission is where a cap either
    binds or does not, so this is the layer it had to move to.
    """
    from bot_squad_worker import backoff as _backoff
    from bot_squad_worker.actions import ActionError

    if slug:
        try:
            from bot_squad_worker import pace as _pace
            pace_cap = _pace.max_in_progress(cfg, slug)  # 0 = unlimited
        except Exception:  # noqa: BLE001 — an unreadable pace.json is not a ceiling
            pace_cap = 0
        if pace_cap > 0:
            lanes = count_live_dev_sessions(cfg, slug)
            if lanes >= pace_cap:
                raise ActionError(
                    f"spawn: capacity reached — {lanes}/{pace_cap} live dev "
                    f"sessions on {slug} (pace.max_in_progress, the parallelism "
                    f"target); spawn refused, task stays pending"
                )

    cap = _read_caps(_caps_config_dir(cfg))["max_parallel_sessions"]
    effective = _backoff.effective_limit(cfg)  # already clamped to the ceiling
    live = _count_live_sessions(cfg)
    if live < effective:
        return
    if cap > 0 and effective >= cap:
        raise ActionError(
            f"spawn: capacity reached — {live}/{cap} parallel sessions live "
            f"(max_parallel_sessions cap); spawn refused, task stays pending"
        )
    raise ActionError(
        f"spawn: backoff — {live}/{effective} effective concurrency "
        f"(rate-limit/usage-limit pressure; hard cap={cap or 'unlimited'}); "
        f"spawn refused, task stays QUEUED, retry on ramp-up"
    )


def caps_utilization(cfg: Any) -> dict:
    """The live, ENFORCED resource-cap utilization for the UI meter (T-0335
    items 7 + 22).

    Returns the exact numbers spawn admission gates on — so the meter measures
    what is actually enforced, not a parallel estimate. The cap VALUES are shared
    config; the LIVE count (and thus enforcement) is per-worker-user (T-0417 —
    each linux_user's worker counts its own live sessions, see
    ``_count_live_sessions``):

      max_parallel_sessions : hard concurrency ceiling (0 = unlimited); shared cap
                              value, enforced per-worker-user
      effective_limit       : ceiling depressed by the AIMD backoff governor
                              ("12/15, throttled to 8"); the 10_000 unlimited
                              sentinel is normalised to 0 so the wire uses the
                              same 0=unlimited convention as the cap
      live_sessions         : THIS worker-user's sessions counted against the ceiling
      max_total_tokens      : output-token budget per quota period (0 = unlimited)
      output_since_anchor   : output tokens spent since the [quota] anchor (the
                              number enforced against max_total_tokens, item 7)
    """
    from bot_squad_worker import backoff as _backoff
    caps = _read_caps(_caps_config_dir(cfg))
    effective = _backoff.effective_limit(cfg)
    if caps["max_parallel_sessions"] == 0 and effective >= _backoff._UNLIMITED:
        effective = 0  # unlimited + no pressure → 0 on the wire (not the sentinel)
    # T-0448 (#5): pass the ALREADY-persisted backoff explainer through so the
    # FE "throttled to N" badge can say WHY (reason) and SINCE-WHEN (pressure/
    # ramp timestamps) instead of a static generic tooltip. Pure passthrough —
    # graceful None on cold start / disabled (load_state → None).
    bstate = _backoff.load_state(cfg) or {}
    return {
        "max_parallel_sessions": caps["max_parallel_sessions"],
        "effective_limit": effective,
        "live_sessions": _count_live_sessions(cfg),
        "max_total_tokens": caps["max_total_tokens"],
        "output_since_anchor": _output_since_anchor(cfg),
        "backoff_reason": bstate.get("reason"),
        "backoff_last_pressure_at": bstate.get("last_pressure_at"),
        "backoff_last_ramp_at": bstate.get("last_ramp_at"),
    }


def _anchor_key(cfg: Any) -> str:
    """The current ``[quota].set_at`` (or '' if unset) — the budget-period key.
    Re-anchoring (operator changes set_at) rebases the token-budget baseline."""
    path = Path(_caps_config_dir(cfg)) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    return str((raw.get("quota") or {}).get("set_at", "") or "")


def _project_output_total(cfg: Any) -> int:
    """Sum ``output_tokens_cum_total`` across every project's ``_quota.json`` —
    the run-wide cumulative output-token counter the telemetry sampler maintains."""
    total = 0
    for slug in getattr(cfg, "projects", {}) or {}:
        q = cfg.data_dir / slug / "_worker" / "telemetry" / "_quota.json"
        try:
            total += int(json.loads(q.read_text()).get("output_tokens_cum_total", 0))
        except (OSError, ValueError, TypeError):
            continue
    return total


def _token_baseline_path(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "token_cap" / "baseline.json"


def _output_since_anchor(cfg: Any) -> int:
    """Output tokens since the current quota anchor (T-0306 semantics B).

    Persists a ``{anchor_key, baseline_total}`` and rebases it whenever the
    ``[quota].set_at`` changes — so the budget frees on re-anchor. Returns
    ``max(0, current_total - baseline_total)``.
    """
    key = _anchor_key(cfg)
    total = _project_output_total(cfg)
    p = _token_baseline_path(cfg)
    try:
        prev = json.loads(p.read_text())
    except (OSError, ValueError):
        prev = {}
    if prev.get("anchor_key") != key:
        prev = {"anchor_key": key, "baseline_total": total}
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / (p.name + ".tmp")
        tmp.write_text(json.dumps(prev))
        os.rename(tmp, p)
    return max(0, total - int(prev.get("baseline_total", 0)))


def _enforce_token_cap(cfg: Any) -> None:
    """Raise ActionError if spawning would exceed the max_total_tokens budget.

    Budget per quota period (T-0306, operator semantics B): output tokens since
    the ``[quota]`` anchor vs the ``[caps].max_total_tokens`` cap. ``0`` =
    unlimited (a no-op). Refusal keeps the task pending (the T-0237 S4 contract),
    mirroring ``_enforce_parallel_cap``.
    """
    cap = _read_caps(_caps_config_dir(cfg))["max_total_tokens"]
    if cap <= 0:
        return
    since = _output_since_anchor(cfg)
    if since >= cap:
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"spawn: token budget reached — {since}/{cap} output tokens this "
            f"quota period (max_total_tokens cap); spawn refused, task stays "
            f"pending until the budget resets (re-anchor)"
        )


def _coordinator_user(cfg: Any) -> str:
    """The configured coordinator UI username (``[admin].coordinator_user``)."""
    path = Path(_caps_config_dir(cfg)) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    return str((raw.get("admin") or {}).get("coordinator_user", "") or "")


def _derive_owner_user(cfg: Any, slug: str, owner: str | None) -> str | None:
    """T-0321: derive the human per-user-scoping username when no explicit
    owner_user was passed. ``owner`` is overloaded:
      * a plain username  → that username;
      * ``constant-team`` → the coordinator user (system-managed teams);
      * a TL-SID (``S-…``) → the TL's own human (its md owner_user/owner),
        resolved one hop (mirrors the T-0135 API-side hop);
      * ``routine:R-NNNN`` → None: a routine binding is not a person, and the
        derived value would land in ``BOT_SQUAD_OWNER_USER`` where the
        colon-free owner_user validator rejects it — the SECOND half of the
        T-0895 spawn failure, hit right after the owner class was widened;
      * anything else / unresolvable → None (legacy → owner-based scoping).
    """
    if not owner or owner == "~":
        return None
    if owner == "constant-team":
        return _coordinator_user(cfg) or None
    if _ROUTINE_OWNER_RE.match(owner):
        return None
    if owner.startswith("S-"):
        meta = _read_session_metadata(_session_file(cfg.data_dir, slug, owner))
        if meta:
            cand = meta.get("owner_user") or meta.get("owner")
            if (cand and cand != "~" and not str(cand).startswith("S-")
                    and cand != "constant-team"):
                return str(cand)
        return None
    return owner  # already a username


def bind_task(cfg: Any, slug: str, sid: str, task_id: str) -> dict:
    """Append task_id to a dev session's extra_task_ids.

    Validates: session exists, session is a dev (has primary task_id), task
    file exists, task isn't already bound elsewhere. Sends a peer notification.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"bind_task: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"bind_task: no session metadata for SID {sid!r}")

    # T-0185: a constant-team / queue-consumer session must never carry a
    # single-ticket binding — it consumes a line-queue and has no single-ticket
    # DoD, so a bound ticket is meaningless (and triggers false drift nags). The
    # p38 incident wired an unrelated unassigned ticket onto a feedback-triage
    # session; refuse the bind outright with a clear error rather than the
    # generic "not a dev session" below. (defence-in-depth: drift.py also skips
    # these, so even a binding that slips in via another path won't nag.)
    if str(meta.get("owner") or "") == "constant-team":
        raise ActionError(
            f"bind_task: session {sid!r} is a constant-team/queue-consumer "
            f"(owner=constant-team) — single-ticket bindings are not allowed"
        )

    primary = meta.get("task_id")
    primary_empty = not primary or primary == "~"
    if primary_empty:
        # T-0525: an empty primary is either a legitimately task-less TL/operator
        # (refuse — they never hold a single ticket) OR an unbound/mis-bound DEV
        # whose primary the spawn-marker race left empty. For a dev we ADOPT
        # task_id as the PRIMARY below (the in-place repair the old append-only
        # path couldn't do), so an unbound session is fixable via the action, not
        # only a hand-edit. Role is resolved from the window marker (T-0175).
        role = _role_of(meta, task_id=primary)  # T-0509: honor a morph stamp
        if role != "dev":
            raise ActionError(
                f"bind_task: session {sid!r} is not a dev session (no primary task_id)"
            )

    backlog_dir = data_dir / slug / "backlog"
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise ActionError(f"bind_task: task not found: {task_id}")

    if task_id == primary or task_id in (meta.get("extra_task_ids") or []):
        # Already bound to this session — idempotent success.
        extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
        return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "already_bound": True}

    # T-0157: multi-user claim lock — first-to-claim wins. Two users (or two
    # concurrent dispatches) can race the check-then-write below; without a
    # lock both pass `_find_owner` (each sees the task free) and both write,
    # double-binding the task. Serialize the find+write under a per-project
    # flock so exactly one claimant wins; the loser sees the freshly-written
    # owner and raises. The lock file lives beside the backlog (shared store)
    # so it is visible across the coordinator + per-user worker processes.
    claim_lock = backlog_dir / ".task-claim.lock"
    claim_lock.parent.mkdir(parents=True, exist_ok=True)
    with open(claim_lock, "w") as _lockf:
        fcntl.flock(_lockf, fcntl.LOCK_EX)
        # T-0237 Layer-1: enforce the binding cap (= 1 LIVE session per task)
        # under the claim flock. Only *live* holders count — a suspended or
        # archived prior holder is history and must not block a fresh run
        # (otherwise an abandoned/crashed task is permanently un-rebindable).
        # S4: at capacity we refuse with a clear "capacity reached" state that
        # names the live holder; the task simply stays pending (the caller
        # surfaces it / queues it), never a silent drop or a double-bind.
        live_owner = _live_task_owner(data_dir, slug, task_id, exclude_sid=sid)
        if live_owner is not None:
            raise ActionError(
                f"bind_task: capacity reached (cap={BINDING_CAP}) — task "
                f"{task_id} is already bound to live session {live_owner}; "
                f"task stays pending"
            )

        # Re-read under the lock so a concurrent bind to THIS session (its own
        # extras growing) isn't clobbered by our stale snapshot — last write
        # wins on lost-update otherwise.
        meta = _read_session_metadata(meta_file) or meta
        extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
        cur_primary = meta.get("task_id")
        if not cur_primary or cur_primary == "~":
            # T-0525: ADOPT as PRIMARY — repair an unbound dev in place. Re-checked
            # under the lock so the primary-vs-extra decision is race-safe.
            meta["task_id"] = task_id
            primary_set = True
        else:
            if task_id not in extras:
                extras.append(task_id)
            meta["extra_task_ids"] = extras
            primary_set = False
        _write_session_metadata(meta_file, meta)
    # flock released on close

    # T-0105: stamp the binding SID into the new task's session_history.
    # Best-effort; the SessionMd write above is the source of truth.
    try:
        _append_task_session_history(backlog_dir, task_id, sid)
    except OSError:
        pass

    # T-0038: if the dev's session carries an initiative, propagate it to
    # the newly-bound task md (existing-wins). Lets multi-binding keep the
    # task-to-initiative graph consistent without operator intervention.
    sess_init = meta.get("initiative")
    if sess_init and sess_init != "~":
        try:
            _write_task_initiative_if_absent(backlog_dir, task_id, sess_init)
        except OSError:
            pass

    # Read the task title for a friendlier message.
    title = ""
    try:
        task_meta = _read_session_metadata(matches[0])
        if task_meta:
            title = str(task_meta.get("title") or "").strip()
    except Exception:
        pass

    text = (
        f"[BIND_TASK from stakeholder] Also work on {task_id}"
        + (f": {title}" if title else "")
        + f". Read data/{slug}/backlog/{matches[0].name} for scope."
    )
    try:
        from bot_squad_worker import intersession as _is
        # T-0827: send_notice — the bind notify interpolates a ticket TITLE,
        # so it is the tick text most likely to grow, and this frame
        # deliberately swallows everything ("best-effort; the binding is the
        # source of truth"). A refusal here would be seen by no one.
        _is.send_notice(cfg, slug, "stakeholder", sid, text)
    except Exception:
        # Peer notify is best-effort; the binding itself is the source of truth.
        pass

    return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras,
            "primary_set": primary_set}


def set_drift_paused(cfg: Any, slug: str, sid: str, paused: bool) -> dict:
    """T-0184: per-session off-ramp for the drift-check tick (T-0149).

    ``bsq drift off`` sets ``drift_paused: true`` on the SessionMd; ``bsq drift
    on`` clears it. ``drift.drift_check`` reads this flag and skips paused
    sessions. Resolves the md by SID with the rename-tolerant claude_uuid
    fallback so a window-renamed session can still pause itself. Idempotent.

    Returns ``{ok, sid, drift_paused}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"set_drift_paused: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    md_path = _find_session_md(sessions_dir, sid, None)
    if md_path is None:
        raise ActionError(f"set_drift_paused: no session metadata for SID {sid!r}")
    meta = _read_session_metadata(md_path)
    if meta is None:
        raise ActionError(f"set_drift_paused: unreadable session metadata for SID {sid!r}")

    if paused:
        meta["drift_paused"] = True
    else:
        meta.pop("drift_paused", None)
    _write_session_metadata(md_path, meta, atomic=True)
    return {"ok": True, "sid": meta.get("sid", sid), "drift_paused": bool(paused)}


def set_drive(cfg: Any, slug: str, sid: str, on: bool) -> dict:
    """T-0655: the operator's OWN drive=on/off toggle (``bsq drive off`` /
    ``bsq drive on``).

    ``drive`` governs whether THIS operator session stays alive across its
    own ~1h cache-expiry-while-idle window (:func:`recycle_gate.
    operator_drive_on`) — distinct from ``bsq pace pause`` (a project-wide
    dispatch gate) and from the human's own :func:`recycle_gate.
    user_session_exempt` sessions (exempted forever, not a self-toggle).
    ``drive`` defaults to ON implicitly (unset reads as on); this verb writes
    an EXPLICIT ``on``/``off`` string (rather than popping the field on
    "on", the way ``set_drift_paused`` does) so ``/state`` and any other
    reader can distinguish "explicitly re-enabled" from "never touched" —
    both behave identically to the recycle gate either way.

    Operator-role only: only the operator itself may decide its own
    continuity, per the stakeholder's explicit ask ("операторе может решить
    ... и поставить drive=off"). Refuses for any other role so a stray call
    from a dev/TL session can't silently no-op a field that governs nothing
    for it. Resolves the md by SID with the rename-tolerant claude_uuid
    fallback, mirroring :func:`set_drift_paused`.

    Returns ``{ok, sid, drive}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"set_drive: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    md_path = _find_session_md(sessions_dir, sid, None)
    if md_path is None:
        raise ActionError(f"set_drive: no session metadata for SID {sid!r}")
    meta = _read_session_metadata(md_path)
    if meta is None:
        raise ActionError(f"set_drive: unreadable session metadata for SID {sid!r}")

    role = meta.get("role") or _derive_role(
        meta.get("window"), meta.get("task_id"), meta.get("initiative"))
    if role != "operator":
        raise ActionError(
            f"set_drive: {sid!r} is role {role!r}, not operator — drive only "
            "applies to the operator's own continuity")

    meta["drive"] = "on" if on else "off"
    _write_session_metadata(md_path, meta, atomic=True)
    return {"ok": True, "sid": meta.get("sid", sid), "drive": meta["drive"]}


def set_model(cfg: Any, slug: str, sid: str, model: str) -> dict:
    """T-0678: durable PER-SESSION ``claude --model`` override (``bsq model
    set``), distinct from the fleet-wide default :func:`fleet_model.set_model`
    edits in the coordinator's ``~/.claude/settings.json`` (T-0630).

    Stamps (or, for ``model == ""``, clears) the ``model`` field on THIS
    session's SessionMd — mirrors :func:`set_drift_paused` / :func:`set_drive`.
    ``resume`` reads it back to add ``--model`` to the resurrect command
    (taking precedence over the fleet default) so the override survives a
    window recycle; :func:`last_operator_model` lets ``operator_redrive``'s
    full respawn (a brand-new SID, so it can't just re-read ITS OWN md) carry
    it forward too. Validated against the same :data:`fleet_model.
    ALLOWED_MODELS` allowlist — one SSOT for valid model strings, not a
    second copy of it here. Resolves the md by SID with the rename-tolerant
    claude_uuid fallback, mirroring :func:`set_drift_paused`.

    Returns ``{ok, sid, model}``.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker import fleet_model as _fleet_model

    # T-0707: resolve_model also enforces the account-level availability
    # gate (e.g. Fable 5's usage-credits requirement) — one SSOT with the
    # spawn seam, not a second allowlist-only copy of it here.
    raw_model = (model or "").strip()
    requested_provider = (
        "codex"
        if raw_model == "codex"
        or raw_model in _agent_provider.CODEX_MODEL_ALIASES
        or raw_model in _agent_provider.CODEX_MODELS
        else "claude"
    )
    try:
        model = _fleet_model.resolve_model(raw_model, provider=requested_provider)
    except ValueError as exc:
        raise ActionError(f"set_model: {exc}") from exc

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"set_model: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    md_path = _find_session_md(sessions_dir, sid, None)
    if md_path is None:
        raise ActionError(f"set_model: no session metadata for SID {sid!r}")
    meta = _read_session_metadata(md_path)
    if meta is None:
        raise ActionError(f"set_model: unreadable session metadata for SID {sid!r}")

    if requested_provider == "codex" and raw_model:
        meta["provider"] = "codex"
        if model:
            meta["model"] = model
        else:
            meta.pop("model", None)
    elif model:
        meta["provider"] = "claude"
        meta["model"] = model
    else:
        meta.pop("model", None)
        meta.pop("provider", None)
    _write_session_metadata(md_path, meta, atomic=True)
    return {"ok": True, "sid": meta.get("sid", sid), "model": model}


def last_operator_model(cfg: Any, slug: str) -> str:
    """T-0678: best-effort ``model`` override carried by the most recently
    started OPERATOR SessionMd for ``slug`` (live or archived — archival never
    deletes/moves the md, it just flips ``archived``/``status`` in place).

    ``operator_redrive``'s re-drive respawns a brand-new operator SID rather
    than resuming the dead one in place (Process Paradigm: sessions are
    transient), so a per-session ``model`` override set via ``bsq model set``
    would otherwise be lost the moment the operator recycles. This gives
    ``_respawn_operator`` a way to look up what the incarnation it's replacing
    had set, and pass it forward as the new spawn's explicit ``model`` — so
    the override reads as "sticky for the operator role on this project"
    across re-drives, not just within one tmux-resume chain. "" when no
    operator md carries one (the common case — most operators never set one,
    and fall through to the role/fleet default same as before).
    """
    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return ""
    best: dict | None = None
    for md in sessions_dir.glob("*.md"):
        meta = _read_session_metadata(md)
        if not meta or not (meta.get("model") or meta.get("provider") == "codex"):
            continue
        if _role_of(meta) != "operator":
            continue
        if best is None or _started_at_key(meta.get("started_at")) > _started_at_key(best.get("started_at")):
            best = meta
    if best and best.get("provider") == "codex":
        return str(best.get("model") or "codex")
    return str(best.get("model")) if best else ""


def morph_session(cfg: Any, slug: str, sid: str, role: str, *,
                  task_id: str | None = None, initiative: str | None = None,
                  window: str | None = None, cwd: str | None = None,
                  claude_uuid: str | None = None) -> dict:
    """T-0509 (M11/F11.2): MORPH a user session's role IN PLACE.

    "by default a user-launched claude session should always be [a user session],
    however, it can take on a task and become dev, spawn teammates and become
    teamlead, or become operator if there's no operator working right now --
    sessions are transient, system is persistent" (clarification-03 / voice-09).

    Stamps ``role`` (+ task_id / initiative) onto the session md WITHOUT renaming
    the tmux window — so the peer-bus SID (the address frozen at the md filename)
    is untouched and the morph is truly in-place. The stored ``role`` is then
    honored everywhere via :func:`_role_of` (role badge, operator-singleton
    guard, reconciler dev-gates). Resolves the md rename-tolerantly (SID, then
    claude_uuid); for an UNREGISTERED manually-launched user session (no md yet)
    it UPSERTS one from the live-pane fields the caller passes.

    Guards:
      * ``role`` ∈ :data:`MORPH_ROLES` (``dev`` | ``teamlead`` | ``operator``
        | ``user-conversation``).
      * ``operator`` morph is refused when another operator already holds the
        project — the one-operator-per-project singleton via the operator-identity
        SSOT (:func:`dispatch.live_operator_sids`, minus self — T-0472/T-0523).
      * an ``operator`` must NOT carry a dev task (it orchestrates and spawns
        devs for tickets, never self-binds a ticket — T-0523); its primary
        task is cleared on morph.
      * a ``user-conversation`` morph is the DE-differentiation (T-0932) and
        likewise sheds the primary task, for a sharper reason than the
        operator's: ``graceful_exit.work_done`` tests ``task_id`` BEFORE it
        falls through to the role, so a session that narrowed back to the
        conversation while still holding the task it just handed to a bud
        would be exited the moment that BUD reached ``totest`` — the root
        session killed by its own child's success. An explicit non-empty
        ``task_id`` is refused rather than silently dropped.

    Side effect worth naming: ``user-conversation`` is a recycle-EXEMPT role
    (``recycle_gate.role_exempt``, T-0564), so this morph also makes the
    session immune to the terminate-and-remember recycle paths. That is the
    intent — "the root session never exits" — and it costs nothing to a
    would-be abuser, since the same morph strips the task binding that made
    the session a dev in the first place.

    Returns ``{ok, sid, role, task_id, initiative, created}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"morph: unknown project slug {slug!r}")

    role = (role or "").strip().lower()
    if role not in MORPH_ROLES:
        raise ActionError(
            f"morph: role must be one of {sorted(MORPH_ROLES)} (got {role!r})"
        )

    sessions_dir = cfg.data_dir / slug / "sessions"
    md_path = _find_session_md(sessions_dir, sid, claude_uuid)
    created = md_path is None
    if md_path is None:
        # Unregistered, manually-launched user session — create its md from the
        # live-pane identity fields the CLI passed (it computed `sid` from the
        # same pane). Seed identity only; role/task/initiative are set below.
        md_path = _session_file(cfg.data_dir, slug, sid)
        meta: dict = {
            "sid": sid,
            "status": "active",
            "window": window or "~",
            "cwd": cwd or "~",
            "claude_uuid": claude_uuid or "~",
            "task_id": "~",
            "initiative": "~",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    else:
        meta = _read_session_metadata(md_path) or {}
        if not meta:
            raise ActionError(f"morph: unreadable session metadata for SID {sid!r}")
        # Refresh the rename-variant identity fields the caller can supply.
        if window:
            meta["window"] = window
        if cwd and not (meta.get("cwd") and meta["cwd"] != "~"):
            meta["cwd"] = cwd
        if claude_uuid and not (meta.get("claude_uuid") and meta["claude_uuid"] != "~"):
            meta["claude_uuid"] = claude_uuid

    if role == "user-conversation":
        # T-0932: narrowing back to the conversation means the work went
        # somewhere else. A task bind here is a contradiction in terms AND a
        # live hazard (see the docstring's graceful_exit note), so say so
        # instead of quietly ignoring the argument.
        if task_id and task_id != "~":
            raise ActionError(
                "morph: a user-conversation session must not carry a dev task "
                "— hand it to a bud first (`bsq bud dev <id>`), which sheds it "
                "here (T-0932)"
            )

    if role == "operator":
        # T-0523: the operator orchestrates — it must never self-claim a dev
        # assignment. Reject a task bind (the `~` sentinel is not a real bind).
        if task_id and task_id != "~":
            raise ActionError(
                "morph: operator must not bind a dev task — the operator "
                "orchestrates and spawns devs for tickets (T-0523)"
            )
        # T-0472: exactly one operator per project. Check the operator-identity
        # SSOT, EXCLUDING this session (a re-morph of an already-operator session
        # is a no-op, not a duplicate).
        from bot_squad_worker import dispatch as _dispatch
        self_ids = {sid, meta.get("sid")}
        others = [
            s for s in _dispatch.live_operator_sids(cfg, slug) if s not in self_ids
        ]
        if others:
            raise ActionError(
                f"morph: operator already running for {slug!r}: {others[0]} "
                "— exactly one operator per project (T-0472)"
            )

    meta["role"] = role
    # Task / initiative metadata. The operator never holds a single ticket (its
    # standing task is "clear the backlog"), so clear any primary on that morph;
    # dev / teamlead adopt what the caller passed.
    if role in ("operator", "user-conversation"):
        meta["task_id"] = "~"
    elif task_id is not None:
        meta["task_id"] = task_id or "~"
    if initiative is not None:
        meta["initiative"] = initiative or "~"

    _write_session_metadata(md_path, meta, atomic=True)

    def _norm(v):
        return None if (v is None or v == "~") else v

    return {
        "ok": True,
        "sid": meta.get("sid", sid),
        "role": role,
        "task_id": _norm(meta.get("task_id")),
        "initiative": _norm(meta.get("initiative")),
        "created": created,
    }


def _reap_session_sidecars(cfg: Any, slug: str, sid: str) -> list[str]:
    """T-0447 (#4): cascade-free a session's per-SID sidecar scratch when it
    becomes archived/historical — the peer-bus ``_chat`` triple (inbox/seen/
    heartbeat) and the ``_worker/telemetry/<sid>.json`` sample. Without this the
    session md is freed on archive but its sidecars leak forever (a malloc with
    no free that grows one set per session). Each owner module reaps its own
    files; this only orchestrates at the archive chokepoint. NEVER raises (a
    reap must not block the archive). Returns the removed paths."""
    removed: list[str] = []
    try:
        from bot_squad_worker import intersession as _intersession
        removed.extend(_intersession.reap_chat_sidecars(cfg, slug, sid))
    except Exception:  # belt-and-braces over the helper's own guard
        pass
    try:
        from bot_squad_worker import telemetry as _telemetry
        rec = _telemetry.reap_record(cfg, slug, sid)
        if rec is not None:
            removed.append(str(rec))
    except Exception:
        pass
    return removed


def archive_session(cfg: Any, slug: str, sid: str) -> dict:
    """Mark a session as archived in its frontmatter.

    Rules:
      - session must exist
      - session must be in 'suspended' state (no live pane). Active or
        paused sessions can't be archived — suspend first.

    Idempotent: archiving an already-archived session returns ok=True.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"archive_session: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"archive_session: no metadata for SID {sid!r}")

    # The session must have no live pane. Check tmux directly so we can't
    # rely on a stale md status flag.
    user = _get_current_user()
    for pane in list_panes():
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            raise ActionError(
                f"archive_session: {sid!r} still has a live pane — suspend first"
            )

    status = meta.get("status", "")
    if status not in ("suspended", "paused", "active"):
        # paused/active here mean stale md flags (we already verified no
        # live pane), so allow the archive — normalise to suspended first.
        pass

    meta["status"] = "suspended"
    meta["archived"] = "true"
    _write_session_metadata(meta_file, meta)
    # T-0447 (#4): the session is now historical — free its per-SID sidecars.
    _reap_session_sidecars(cfg, slug, sid)
    return {"ok": True, "sid": sid, "archived": True}


def unarchive_session(cfg: Any, slug: str, sid: str) -> dict:
    """Clear the archived flag on a session md."""
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unarchive_session: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unarchive_session: no metadata for SID {sid!r}")

    if "archived" in meta:
        meta.pop("archived", None)
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "archived": False}


def bind_initiative(cfg: Any, slug: str, sid: str, initiative: str) -> dict:
    """Append initiative basename to a TL session's extra_initiatives.

    Validates: session exists, session is a TL (no primary task_id),
    initiative file exists, initiative isn't already bound elsewhere.
    Sends a peer notification.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"bind_initiative: unknown project slug {slug!r}")

    # Safety: basename-only, must end .md, no traversal.
    clean = (initiative or "").strip()
    if not clean or "/" in clean or ".." in clean or not clean.endswith(".md"):
        raise ActionError(f"bind_initiative: invalid initiative name {initiative!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"bind_initiative: no session metadata for SID {sid!r}")

    primary_task = meta.get("task_id")
    if primary_task and primary_task != "~":
        raise ActionError(f"bind_initiative: session {sid!r} is a dev session, not a teamlead")

    init_path = data_dir / slug / "vision" / "initiatives" / clean
    if not init_path.exists():
        raise ActionError(f"bind_initiative: initiative not found: {clean}")

    primary_init = meta.get("initiative")
    if clean == primary_init or clean in (meta.get("extra_initiatives") or []):
        extras = [i for i in (meta.get("extra_initiatives") or []) if i and i != "~"]
        return {"ok": True, "sid": sid, "initiative": clean, "extras": extras, "already_bound": True}

    owner = _find_owner(data_dir, slug, initiative=clean)
    if owner is not None and owner != sid:
        raise ActionError(f"bind_initiative: initiative {clean} already bound to {owner}")

    extras = list(meta.get("extra_initiatives") or [])
    extras = [i for i in extras if i and i != "~"]
    extras.append(clean)
    meta["extra_initiatives"] = extras
    _write_session_metadata(meta_file, meta)

    text = (
        f"[BIND_INITIATIVE from stakeholder] Also coordinate {clean}. "
        f"Read data/{slug}/vision/initiatives/{clean} for context."
    )
    try:
        from bot_squad_worker import intersession as _is
        # T-0827: send_notice — the bind notify interpolates a ticket TITLE,
        # so it is the tick text most likely to grow, and this frame
        # deliberately swallows everything ("best-effort; the binding is the
        # source of truth"). A refusal here would be seen by no one.
        _is.send_notice(cfg, slug, "stakeholder", sid, text)
    except Exception:
        pass

    return {"ok": True, "sid": sid, "initiative": clean, "extras": extras}


def unbind_task(cfg: Any, slug: str, sid: str, task_id: str) -> dict:
    """Remove a task binding from a dev session.

    If `task_id` matches the session's primary task_id, the call fails —
    the primary is the session's identity. Removes from extra_task_ids
    otherwise. Idempotent if the task isn't bound.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unbind_task: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unbind_task: no session metadata for SID {sid!r}")

    primary = meta.get("task_id")
    if primary == task_id:
        raise ActionError(
            f"unbind_task: {task_id} is the primary task of {sid!r}; "
            "cannot unbind the session's identity"
        )

    extras = list(meta.get("extra_task_ids") or [])
    extras = [t for t in extras if t and t != "~"]
    if task_id not in extras:
        return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "changed": False}
    extras = [t for t in extras if t != task_id]
    meta["extra_task_ids"] = extras
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "changed": True}


def unbind_initiative(cfg: Any, slug: str, sid: str, initiative: str) -> dict:
    """Remove an initiative from a TL session's bindings.

    If the initiative matches the primary `initiative` field, that field is
    cleared (leaving extras intact). Otherwise the value is filtered out of
    `extra_initiatives`. Returns the resulting extras list.

    Idempotent: unbinding a not-present initiative returns ok=True.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unbind_initiative: unknown project slug {slug!r}")

    clean = (initiative or "").strip()
    if not clean or "/" in clean or ".." in clean:
        raise ActionError(f"unbind_initiative: invalid initiative name {initiative!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unbind_initiative: no session metadata for SID {sid!r}")

    primary_init = meta.get("initiative")
    extras = list(meta.get("extra_initiatives") or [])
    extras = [i for i in extras if i and i != "~"]

    changed = False
    if primary_init == clean:
        meta["initiative"] = "~"
        changed = True
    if clean in extras:
        extras = [i for i in extras if i != clean]
        meta["extra_initiatives"] = extras
        changed = True

    if changed:
        _write_session_metadata(meta_file, meta)

    return {"ok": True, "sid": sid, "initiative": clean, "extras": extras, "changed": changed}


# ---------------------------------------------------------------------------
# Binding-graph reconcilers (T-0073 / T-0077)
# ---------------------------------------------------------------------------

def _current_user_sid_prefix() -> str:
    """Return ``S-<linux_user>-`` so gc helpers can scope to their own user.

    The worker can only see its own linux user's tmux server via ``list_panes``;
    another user's sessions would be misclassified as zombies without this.
    SessionMd filenames embed the linux user as the second SID segment, so a
    prefix-match keeps reconcilers safely user-scoped without needing
    cross-user coordination.
    """
    return f"S-{_get_current_user()}-"


def gc_sessions(cfg: Any, slug: str) -> dict:
    """T-0077: flip md ``status: active`` to ``suspended`` when no live pane.

    Walks ``data/<slug>/sessions/*.md`` filtered to the current linux user.
    For each SessionMd claiming ``status: active`` whose SID has no LIVE
    claude agent (``_live_agent_sids`` — a real ``claude`` process in the
    pane's /proc subtree, T-0397), rewrites the md atomically with
    ``status: suspended`` + ``suspended_at: <now>``. Original ``started_at``
    and ``claude_uuid`` are preserved so the session remains resurrectable
    via ``resume()``. Skips mds with ``archived: true`` (operator intent).

    T-0401: liveness used to be decided by "SID is in ``list_panes()``",
    gated by a T-0134 guard that additionally REQUIRED an explicit
    ``pane_id`` field on the md before a mismatch could flip it — a
    SessionMd with no ``pane_id`` (legacy schema, or a pane whose id was
    never recorded) was "unverifiable" and left ``active`` forever. That let
    a genuinely dead session (no pane_id, no matching pane) sit
    phantom-active indefinitely, silently holding its task binding + mail
    (the ``multi_server-TL-p30`` incident: dead since its tmux window closed,
    never reconciled because it had no recorded ``pane_id``). Switching the
    liveness signal to ``_live_agent_sids()`` — the SAME reconciliation
    ``_count_live_sessions``/``_live_task_owner`` already trust — closes that
    gap WITHOUT reopening T-0134: it recomputes each live pane's sid from
    tmux state directly (``compute_sid(user, window, pane_id)``), so a
    genuinely-alive legacy session (its tmux pane, and the claude process in
    it, still running) still resolves to a live sid and is spared — no
    recorded ``pane_id`` field required. It's process liveness, not a
    recent-activity clock, so a quiet-but-alive session (idle, no recent
    output, process still up) is also spared — only a pane with no live
    claude process anywhere is flipped.

    Returns ``{"ok": True, "scanned": N, "repaired": K, "sids": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_sessions: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "repaired": 0, "sids": []}

    user = _get_current_user()
    user_prefix = f"S-{user}-"
    live = _live_agent_sids()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    scanned = 0
    repaired: list[str] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        if meta.get("status") != "active":
            continue
        if str(meta.get("archived", "")).lower() == "true":
            continue
        sid = meta.get("sid", md.stem)
        if sid in live:
            continue
        meta["status"] = "suspended"
        meta["suspended_at"] = now
        # T-0444: stamp WHY + WHO closed this so the auto-cleanup is VISIBLE on
        # the Processes status badge (the doctrine's "visible close"). This path
        # is the silent idle/no-pane suspend that "ships dark" today.
        meta["suspend_source"] = "gc_sessions"
        meta["suspend_reason"] = "auto-suspended: no live claude pane"
        _write_session_metadata(md, meta, atomic=True)
        repaired.append(sid)
    return {"ok": True, "scanned": scanned, "repaired": len(repaired), "sids": repaired}


# T-0200: idle grace before an empty per-initiative/per-team tmux session is
# reaped. Default 1h (the DoD threshold). A session created seconds ago by an
# in-flight spawn is well inside this window, so the reaper never races a
# just-spawned team. Override via env for tests / faster local cleanup.
_TMUX_GC_IDLE_SEC = float(os.environ.get("BOT_SQUAD_TMUX_GC_IDLE_SEC") or 3600)
# T-0350: a demand-driven constant-team sibling (user-feedback etc.) whose triage
# dev has exited is just a lingering empty bare-shell — it has finished its
# queue and has no reason to wait the full hour. Reap it on a much shorter grace
# so the empty session doesn't accumulate (the recurring leak the operator kept
# killing by hand). Still > a spawn settle window so we never race a mid-spawn.
_CONSTANT_TMUX_GC_IDLE_SEC = float(os.environ.get("BOT_SQUAD_CONSTANT_TMUX_GC_IDLE_SEC") or 120)


def gc_tmux_sessions(cfg: Any, slug: str) -> dict:
    """T-0200: reap idle, empty per-initiative/per-team tmux sessions.

    The T-0001 per-initiative routing creates a sibling tmux session
    ``<slug>-<stem>`` for each initiative/constant team, each parking a never-used
    ``_init`` placeholder window. When the team's claude windows exit, the
    ``_init`` window keeps the otherwise-empty session alive forever — the
    "7 spurious ``bot-squad-<initiative>`` sessions" the stakeholder hit. This
    reconciler enforces the invariant *a sibling tmux session exists only while
    its team has a live claude pane*: any ``<slug>-*`` session with **zero** live
    claude panes whose last activity is older than ``_TMUX_GC_IDLE_SEC`` is
    killed. The on-disk Team md is untouched (``reconcile_teams`` keeps the
    project team), so "idle teams keep on-disk entity but no tmux session".

    Guard rails:
      - The bare ``<slug>`` main session is NEVER reaped (project home; hosts the
        operator pane and is recreated on demand anyway).
      - Only sessions whose panes are rooted in this project's repo cwd are
        eligible, so a coincidentally ``<slug>-``prefixed session belonging to
        another project / a human is spared.
      - A session with ≥1 live claude pane is spared regardless of idle time.

    Same pass serves the periodic GC (DoD #2) and the one-shot migration of
    pre-existing empties (DoD #4): an old spurious session is already idle past
    the grace, so it is reaped on the first tick it is seen.

    Returns ``{"ok": True, "reaped": [<name>, ...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_tmux_sessions: unknown project slug {slug!r}")

    prefix = f"{slug}-"
    repo_path = Path(project.repo_path)
    try:
        repo_real = repo_path.resolve()
    except OSError:
        repo_real = repo_path

    # Bucket live panes by tmux session: claude-pane count + are any panes rooted
    # in this project (the ownership gate).
    claude_counts: dict[str, int] = {}
    rooted: dict[str, bool] = {}
    for p in list_panes():
        sess = p.session
        if not sess:
            continue
        if _is_claude_command(p.command):
            claude_counts[sess] = claude_counts.get(sess, 0) + 1
        if not rooted.get(sess) and p.cwd:
            # T-0220: shared with the active-pane / suspended-row cwd match.
            if _cwd_matches_repo(Path(p.cwd), repo_path, repo_real):
                rooted[sess] = True

    res = _run(["tmux", "list-sessions", "-F", "#{session_name}|#{session_activity}"])
    if res.returncode != 0:
        # No tmux server / no sessions — nothing to reap.
        return {"ok": True, "reaped": []}

    # T-0350: which sibling stems are demand-driven constant teams (shorter grace).
    try:
        from bot_squad_worker.constant_teams import constant_team_stems
        ct_stems = constant_team_stems(cfg, slug)
    except Exception:  # noqa: BLE001
        ct_stems = set()

    now = time.time()
    reaped: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, activity_s = line.partition("|")
        if name == slug or not name.startswith(prefix):
            continue  # main session or another project — never reap
        if claude_counts.get(name, 0) > 0:
            continue  # staffed — has a live claude pane
        if not rooted.get(name):
            continue  # not rooted in this project's repo — not ours
        try:
            activity = float(activity_s)
        except (TypeError, ValueError):
            activity = 0.0
        # T-0350: a finished constant-team sibling reaps fast; everything else
        # keeps the long grace.
        grace = _CONSTANT_TMUX_GC_IDLE_SEC if name[len(prefix):] in ct_stems else _TMUX_GC_IDLE_SEC
        if now - activity < grace:
            continue  # still within the idle grace (e.g. mid-spawn)
        kill = _run(["tmux", "kill-session", "-t", name])
        if kill.returncode == 0:
            reaped.append(name)
    return {"ok": True, "reaped": reaped}


# T-0802: how long a session must stay orphaned before it is reaped. Long on
# purpose — the failure mode we must not have is reaping a LIVE project's
# sessions during a transient de-registration (a deploy that ships a
# projects.toml missing a slug does exactly that, and has: see 2d7d427). Those
# windows are minutes; six hours is not reachable by one. The operator is
# alerted on the FIRST sighting, so the quarantine is also the human's window
# to intervene, not just a timer.
_ORPHAN_TMUX_QUARANTINE_SEC = float(
    os.environ.get("BOT_SQUAD_ORPHAN_TMUX_QUARANTINE_SEC") or 21600)
#: Set to 0 to alert but never kill (an install that would rather triage by hand).
_ORPHAN_TMUX_REAP = (os.environ.get("BOT_SQUAD_ORPHAN_TMUX_REAP") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}
#: The placeholder window `_ensure_project_tmux_session` parks in every session
#: it creates. Nothing else in this codebase — and nothing a human types — makes
#: a window with this name, which is what makes it usable as proof of ownership.
_BOT_SQUAD_INIT_WINDOW = "_init"


def _orphan_ledger_path(cfg: Any) -> Path:
    """Cross-project worker state (the per-slug dirs are the wrong home: an
    orphan by definition belongs to no slug)."""
    return Path(cfg.data_dir) / "_worker" / "orphan_tmux.json"


def gc_orphan_tmux_sessions(cfg: Any) -> dict:
    """T-0802: reap (or at minimum SURFACE) tmux sessions that bot-squad created
    and that belong to no registered project.

    WHY THIS EXISTS AS A SEPARATE PASS. ``gc_tmux_sessions`` above is
    per-project and structurally cannot see this class of session — twice over:

      1. It is called as ``fn(cfg, slug)`` for ``slug in cfg.projects``, and its
         first act is ``cfg.projects.get(slug)`` → ``ActionError`` on an unknown
         slug. An unregistered session's name is never any registered slug, so
         no tick ever passes it.
      2. Even reached, its loop skips any name that is not ``<slug>-``prefixed,
         requires a pane rooted in that project's repo, and spares any session
         holding a live claude pane.

    The incident this comes from: a stray session named for the test fixture
    slug ``test-project`` accumulated one live ``claude`` process per worker
    suite run — 70 of them over three days, the host's RAM and all 8 GB of swap
    consumed — while every reconciler in the tick ran normally, because there
    was nothing in the system whose job was to look at a session no project
    claims. The suite leak itself is fixed at source (``tests/conftest.py``
    ``_isolate_actions_config`` + ``tests/fake_bin/tmux``); this is the half
    that makes the NEXT unclaimed session someone's problem within a tick
    instead of nobody's for three days.

    A SESSION IS ONLY TOUCHED WHEN ALL OF THESE HOLD — the point is that
    "unregistered" alone is nowhere near sufficient:

      * its name is neither a registered slug nor ``<registered-slug>-*`` (so a
        sibling session is left to the T-0200 reaper that understands it);
      * it carries the ``_init`` placeholder window, which only
        ``_ensure_project_tmux_session`` creates — this is the ownership proof,
        and it is what keeps a human's own tmux session (``work``, ``vim``,
        anything) out of scope no matter what it is called;
      * no pane in it is rooted in ANY registered project's repo;
      * it is not the session this process is running in;
      * it has been continuously orphaned for ``_ORPHAN_TMUX_QUARANTINE_SEC``.

    A LIVE CLAUDE PANE IS NOT A REPRIEVE HERE, and that is the deliberate
    inversion of the T-0200 rule. There, a claude pane means a staffed team
    doing work. Here, the four conditions above have already established that
    bot-squad made this session for a project that does not exist — so a claude
    pane in it is not work, it is precisely the runaway process the ticket is
    about. Sparing it would reproduce the bug: all 70 leaked sessions held one.

    Returns ``{"ok": True, "reaped": [...], "sighted": [...], "quarantined":
    [...]}`` — ``sighted`` is the first-tick-seen set the caller alerts on
    (``jobs.binding_gc_tick``), ``quarantined`` those still inside the grace.
    """
    registered = set(cfg.projects or {})
    prefixes = tuple(f"{s}-" for s in registered)

    res = _run(["tmux", "list-sessions", "-F", "#{session_name}|#{session_activity}"])
    if res.returncode != 0:
        return {"ok": True, "reaped": [], "sighted": [], "quarantined": []}

    names: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.partition("|")[0]
        if not name or name in registered or name.startswith(prefixes):
            continue
        names.append(name)
    if not names:
        _prune_orphan_ledger(cfg, set())
        return {"ok": True, "reaped": [], "sighted": [], "quarantined": []}

    # Which of those carry bot-squad's own placeholder window.
    wres = _run(["tmux", "list-windows", "-a", "-F", "#{session_name}|#{window_name}"])
    ours = {ln.partition("|")[0] for ln in wres.stdout.splitlines()
            if ln.strip().partition("|")[2].strip() == _BOT_SQUAD_INIT_WINDOW}

    # Repo roots of every registered project, resolved once.
    repos: list[tuple[Path, Path]] = []
    for project in (cfg.projects or {}).values():
        rp = Path(getattr(project, "repo_path", "") or "")
        if not str(rp):
            continue
        try:
            repos.append((rp, rp.resolve()))
        except OSError:
            repos.append((rp, rp))

    self_pane = os.environ.get("TMUX_PANE") or ""
    self_session = ""
    rooted: set[str] = set()
    claude_panes: dict[str, int] = {}
    for p in list_panes():
        if not p.session:
            continue
        if self_pane and p.pane_id == self_pane:
            self_session = p.session
        if _is_claude_command(p.command):
            claude_panes[p.session] = claude_panes.get(p.session, 0) + 1
        if p.cwd and p.session not in rooted:
            if any(_cwd_matches_repo(Path(p.cwd), rp, rr) for rp, rr in repos):
                rooted.add(p.session)

    candidates = [n for n in names
                  if n in ours and n not in rooted and n != self_session]

    now = time.time()
    ledger = _read_orphan_ledger(cfg)
    reaped: list[dict] = []
    sighted: list[dict] = []
    quarantined: list[dict] = []
    for name in candidates:
        entry = ledger.get(name)
        if not isinstance(entry, dict) or not entry.get("first_seen"):
            # FIRST SIGHTING — never reaped on the same tick it is discovered.
            # The alert goes out now precisely so the quarantine below is a
            # human's window to say "that one is mine", not a silent countdown.
            ledger[name] = {"first_seen": now, "claude_panes": claude_panes.get(name, 0)}
            sighted.append({"session": name,
                            "claude_panes": claude_panes.get(name, 0),
                            "reap_after_sec": _ORPHAN_TMUX_QUARANTINE_SEC})
            continue
        try:
            first_seen = float(entry.get("first_seen") or 0.0)
        except (TypeError, ValueError):
            first_seen = now
        orphaned_for = now - first_seen
        entry["claude_panes"] = claude_panes.get(name, 0)
        if orphaned_for < _ORPHAN_TMUX_QUARANTINE_SEC or not _ORPHAN_TMUX_REAP:
            quarantined.append({"session": name, "orphaned_for": orphaned_for,
                                "claude_panes": claude_panes.get(name, 0)})
            continue
        kill = _run(["tmux", "kill-session", "-t", name])
        if kill.returncode == 0:
            reaped.append({"session": name, "orphaned_for": orphaned_for,
                           "claude_panes": claude_panes.get(name, 0)})
            ledger.pop(name, None)
        else:
            log.warning("gc_orphan_tmux_sessions: kill-session %s failed: %s",
                        name, kill.stderr.strip())

    _write_orphan_ledger(cfg, ledger, keep=set(candidates))
    return {"ok": True, "reaped": reaped, "sighted": sighted,
            "quarantined": quarantined}


def _read_orphan_ledger(cfg: Any) -> dict:
    try:
        data = json.loads(_orphan_ledger_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_orphan_ledger(cfg: Any, ledger: dict, keep: set[str]) -> None:
    """Persist the ledger, dropping names that are no longer orphaned.

    IT MUST BE PERSISTENT, not a module global: the clock that matters is how
    long the SESSION has been orphaned, and a worker restart (or a deploy —
    they are frequent here) would otherwise reset every candidate's timer to
    zero and the quarantine would never elapse. That is the failure mode where
    a reaper exists, looks healthy, and reaps nothing, forever.
    """
    ledger = {k: v for k, v in ledger.items() if k in keep}
    path = _orphan_ledger_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("gc_orphan_tmux_sessions: could not persist %s", path)


def _prune_orphan_ledger(cfg: Any, keep: set[str]) -> None:
    if _orphan_ledger_path(cfg).exists():
        _write_orphan_ledger(cfg, _read_orphan_ledger(cfg), keep=keep)


def _started_at_key(value: Any) -> tuple[int, str]:
    """Sort key for ``started_at`` — newer wins. Missing values rank lowest."""
    if not value or value == "~":
        return (0, "")
    return (1, str(value))


def resolve_session(cfg: Any, slug: str, ident: str) -> dict | None:
    """T-0176 #1/#2 addressability shim: resolve a display SID *or* a claude_uuid
    to its canonical SessionMd, following ``merged_into`` so an address to a
    deduped-away zombie redirects to the surviving keeper.

    This is the guarantee that the dedup migration (and the eventual UUID re-key,
    [[T-0187]]) never invalidates a live address: a peer-bus caller can pass the
    current ``S-<user>-<window>-pN`` SID *or* the uuid and reach the same live
    session. Returns the resolved meta dict, or None if nothing matches.
    """
    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return None

    def _lookup(key: str) -> dict | None:
        direct = sessions_dir / f"{key}.md"
        if direct.exists():
            return _read_session_metadata(direct)
        for md in sessions_dir.glob("*.md"):
            meta = _read_session_metadata(md)
            if meta is None:
                continue
            if meta.get("sid") == key or meta.get("claude_uuid") == key:
                return meta
        return None

    meta = _lookup(ident)
    if meta is None:
        return None
    seen: set[str] = set()
    while meta is not None:
        mi = meta.get("merged_into")
        if not mi or mi == "~" or mi in seen:
            break
        seen.add(mi)
        nxt = _lookup(mi)
        if nxt is None:
            break
        meta = nxt
    return meta


def dedup_sessions(cfg: Any, slug: str, *, dry_run: bool = True) -> dict:
    """T-0176 #5/#6: collapse duplicate SessionMds to one keeper per logical
    session, marking the rest ``merged_into: <keeper>`` so the UI can show one
    row per logical session instead of the p92..p99 parade.

    Two failure modes (see scenarios/T-0176):

      * **Mode A — same ``claude_uuid``**: one conversation that produced several
        SessionMds because the tmux pane id rotated across a restart
        (e.g. ``multi-p8`` / ``multi-p9`` sharing one uuid). Unambiguous.
      * **Mode B — distinct uuids, same ``window`` + task**: a spawn storm
        (e.g. ``T-0080-p92..p99``), eight separate claude launches all bound to
        one task in one window. Keyed on **task AND window** — never task alone —
        and **constant-team sessions are excluded** (owner ``constant-team`` or a
        ``constant_team: true`` initiative), so a feedback processor mis-bound to
        someone else's task is never swept into their cluster.

    Keeper = the live session if any, else the most-recent ``started_at``. Live
    sessions are never archived. ``dry_run=True`` (default) reports the merges it
    *would* make without writing. Idempotent: already-``merged_into`` rows are
    left alone.

    Returns ``{ok, dry_run, merged_count, merges: [{loser, keeper, mode}, ...]}``.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker.constant_teams import constant_team_stems

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"dedup_sessions: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "dry_run": dry_run, "merged_count": 0, "merges": []}

    user = _get_current_user()
    user_prefix = f"S-{user}-"
    live_sids = {compute_sid(user, p.window, p.pane_id) for p in list_panes()}
    const_stems = constant_team_stems(cfg, slug)

    rows: list[tuple[str, dict, Any]] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        rows.append((meta.get("sid", md.stem), meta, md))

    def _keeper_key(item: tuple[str, dict, Any]) -> tuple:
        sid, meta, _ = item
        live = 1 if sid in live_sids else 0
        return (live, _started_at_key(meta.get("started_at")), sid)

    def _already_merged(meta: dict) -> bool:
        return bool(meta.get("merged_into") and meta.get("merged_into") != "~")

    def _is_constant(meta: dict) -> bool:
        if str(meta.get("owner") or "") == "constant-team":
            return True
        stem = Path(str(meta.get("initiative") or "")).stem
        return bool(stem and stem in const_stems)

    def _task_key(meta: dict) -> str | None:
        for field in ("task_id", "last_task_id"):
            v = meta.get(field)
            if v and v != "~":
                return str(v)
        return None

    merged: dict[str, tuple[str, str]] = {}  # loser_sid -> (keeper_sid, mode)

    def _collapse(members: list[tuple[str, dict, Any]], mode: str) -> None:
        if len(members) < 2:
            return
        keeper = max(members, key=_keeper_key)[0]
        for sid, meta, _ in members:
            if sid == keeper or sid in merged or sid in live_sids:
                continue
            # Migration guardrail (T-0176): NEVER archive a status=active row.
            # The destructive part targets dead zombies only; an active-but-not-
            # live row is left for gc_sessions to suspend first.
            if str(meta.get("status") or "").lower() == "active":
                continue
            if _already_merged(meta):
                continue
            merged[sid] = (keeper, mode)

    # Mode A — exact: same claude_uuid.
    by_uuid: dict[str, list[tuple[str, dict, Any]]] = {}
    for sid, meta, md in rows:
        u = meta.get("claude_uuid")
        if not u or u == "~":
            continue
        by_uuid.setdefault(u, []).append((sid, meta, md))
    for members in by_uuid.values():
        _collapse(members, "uuid")

    # Mode B — heuristic: same (task, window), constant teams excluded.
    by_taskwin: dict[tuple[str, str], list[tuple[str, dict, Any]]] = {}
    for sid, meta, md in rows:
        if sid in merged or _is_constant(meta):
            continue
        task = _task_key(meta)
        if task is None:
            continue
        by_taskwin.setdefault((task, meta.get("window") or ""), []).append((sid, meta, md))
    for members in by_taskwin.values():
        _collapse(members, "task-window")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    row_by_sid = {sid: (meta, md) for sid, meta, md in rows}
    if not dry_run:
        for loser, (keeper, _mode) in merged.items():
            meta, md = row_by_sid[loser]
            meta["merged_into"] = keeper
            meta["archived"] = "true"
            if meta.get("status") == "active":
                meta["status"] = "suspended"
                meta.setdefault("suspended_at", now)
                # T-0444: visible-close stamp (symmetry with gc_sessions).
                meta["suspend_source"] = "merge"
                meta["suspend_reason"] = f"merged into {keeper}"
            meta["archive_reason"] = f"merged-into:{keeper}"
            _write_session_metadata(md, meta, atomic=True)
            # T-0447 (#4): merged-away loser is historical — free its sidecars.
            _reap_session_sidecars(cfg, slug, loser)

    merges = [
        {"loser": loser, "keeper": keeper, "mode": mode}
        for loser, (keeper, mode) in sorted(merged.items())
    ]
    return {"ok": True, "dry_run": dry_run, "merged_count": len(merges), "merges": merges}


def gc_stale_bindings(cfg: Any, slug: str) -> dict:
    """T-0073: strip stale primary ``task_id`` from sessions losing a dup race.

    Item 3 (audit Fork-2 Part A): this is now a CRASH-ONLY BACKSTOP, not the
    primary dup-bind defence. ``spawn`` refuses a dup-bind at the open under the
    ``.task-claim.lock`` and stamps ``task_id+status:active`` so a fresh session
    is immediately a live owner — so a >1-claimant group can only arise from a
    crash between the claim check and the stamp (or legacy pre-Item-3 data), not
    from a normal concurrent spawn. Kept as defence-in-depth.

    For each ``task_id`` claimed by >1 SessionMd (under the current linux
    user's prefix), picks the winner = (live pane AND latest ``started_at``)
    or (latest ``started_at``) when no claimant is live. Strips ``task_id``
    from the losers, preserving the old value under ``last_task_id`` for
    forensics, and marks ``archive_reason: stale-binding``. Does NOT touch
    sessions with ``archived: true`` (operator already classified them) and
    does NOT touch a session that is the lone claimant of its ``task_id``.

    Returns ``{"ok": True, "scanned": N, "stripped": K, "details": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_stale_bindings: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "stripped": 0, "details": []}

    user = _get_current_user()
    user_prefix = f"S-{user}-"
    live_sids = {compute_sid(user, p.window, p.pane_id) for p in list_panes()}

    # Group SessionMds by primary task_id (current user only).
    by_task: dict[str, list[tuple[Path, dict, str]]] = {}
    scanned = 0
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        tid = meta.get("task_id")
        if not tid or tid == "~":
            continue
        sid = meta.get("sid", md.stem)
        by_task.setdefault(tid, []).append((md, meta, sid))

    details: list[dict] = []
    for task_id, claimants in by_task.items():
        if len(claimants) < 2:
            continue
        # Winner: prefer (live, latest started_at). Sort descending — first wins.
        def _rank(item: tuple[Path, dict, str]) -> tuple[int, tuple[int, str]]:
            _, m, s = item
            return (1 if s in live_sids else 0, _started_at_key(m.get("started_at")))
        ordered = sorted(claimants, key=_rank, reverse=True)
        winner_md, winner_meta, winner_sid = ordered[0]
        for md, meta, sid in ordered[1:]:
            if str(meta.get("archived", "")).lower() == "true":
                # Operator already archived; still strip the dangling task_id so
                # _find_owner stops blocking new binds, but don't double-mark.
                meta["last_task_id"] = meta.get("task_id")
                meta["task_id"] = "~"
                _write_session_metadata(md, meta, atomic=True)
                details.append({
                    "sid": sid, "task_id": task_id, "winner": winner_sid,
                    "stripped": True, "archived_already": True,
                    "was_live": sid in live_sids,
                })
                continue
            meta["last_task_id"] = meta.get("task_id")
            meta["task_id"] = "~"
            meta["archive_reason"] = "stale-binding"
            _write_session_metadata(md, meta, atomic=True)
            # T-0227: was_live distinguishes the genuine CONCURRENT-LIVE dup (the
            # T-0218 race — the stripped loser is still running claude against the
            # shared worktree = a co-edit hazard the operator must kill) from the
            # crash-only case (a dead-pane md the backstop silently cleans up).
            details.append({
                "sid": sid, "task_id": task_id, "winner": winner_sid,
                "stripped": True, "archived_already": False,
                "was_live": sid in live_sids,
            })

    return {"ok": True, "scanned": scanned, "stripped": len(details), "details": details}


def _task_status(data_dir: Path, slug: str, task_id: str) -> str | None:
    """Return a backlog task's ``status`` frontmatter, or None if the file is gone.

    Resolves ``data/<slug>/backlog/<task_id>-*.md`` (the canonical naming). A
    missing file yields None so callers can treat "task deleted" distinctly
    from "task closed". Parsing is the same minimal frontmatter reader the
    rest of the worker uses.
    """
    backlog_dir = data_dir / slug / "backlog"
    if not backlog_dir.exists():
        return None
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        direct = backlog_dir / f"{task_id}.md"
        if direct.exists():
            matches = [direct]
    if not matches:
        return None
    parsed = _frontmatter.parse_or_none(matches[0].read_text())  # T-0075
    if parsed is None:
        return None
    status = parsed[0].get("status")
    if status is None:
        return None
    return str(status) or None


def _initiative_exists(data_dir: Path, slug: str, initiative: str) -> bool:
    init = (initiative or "").strip()
    if not init or init == "~":
        return True  # nothing bound → nothing stale
    return (data_dir / slug / "vision" / "initiatives" / init).exists()


def backfill_parent_sid(cfg: Any, slug: str) -> dict:
    """T-0128: populate ``parent_sid`` for sessions that predate the field.

    The spawn path now stamps ``parent_sid`` (the SID that requested the
    spawn) at creation time, but legacy sessions — and sessions created by
    Claude Code's native agent-teams feature, which never goes through
    ``sessions.spawn`` — lack it. This idempotent reconciler fills the gap
    using the *same heuristic* the web tree falls back to: a session nests
    under the team-lead of the team that lists it (``teams.tl_for_sid``,
    which resolves the tmux-session-keyed Team projection rebuilt by
    ``reconcile_teams``). Mirrors the rendering rule "a dev nests under its
    TL" but persists the result so it survives a worker restart.

    Strictly fill-once: a session that already has a ``parent_sid`` is left
    untouched — the spawn-time value is authoritative and never overwritten.
    Runs after ``reconcile_teams`` in the binding-gc tick so the Team
    projection it consults is fresh.

    T-0647: every fill this way is stamped ``parent_sid_heuristic: true`` —
    it is a *guess* (whichever TL currently occupies the team's shared lead
    slot), not a recorded spawn relationship, and can land on a TL with no
    real tie to the session once that slot changes hands. Consumers that need
    the actual parent (e.g. idle-notify routing) must check the flag rather
    than treat every ``parent_sid`` as equally authoritative.

    Only DEV rows (carrying a primary ``task_id``) are backfilled — this
    mirrors the web tree's ``isDevRow`` gate. Non-dev rows (TL / prod-tl / qa)
    are tree roots unless an explicit spawn-time ``parent_sid`` was stamped, so
    the heuristic must never invent a parent for them. Non-dev rows are also
    self-healed against an earlier over-eager backfill: an operator is always
    a root (any ``parent_sid`` cleared), and for other non-dev rows the value
    is cleared only when it matches the heuristic's fingerprint
    (``== tl_for_sid(row)``) — preserving a genuine spawn-time parent such as
    operator→TL.

    Returns ``{"ok": True, "scanned": N, "filled": K, "corrected": C,
    "details": [...]}`` where ``corrected`` counts cleared non-dev rows.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker import teams as _teams

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"backfill_parent_sid: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "filled": 0, "details": []}

    scanned = 0
    filled = 0
    corrected = 0
    details: list[dict] = []
    for md in sorted(sessions_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        sid = meta.get("sid", md.stem)
        role = _role_of(meta)  # T-0509: honor a morph stamp
        tid = meta.get("task_id")
        is_dev = bool(tid and tid != "~")

        if not is_dev:
            # Non-dev rows (operator / TL / prod-tl / qa) are tree ROOTS in the
            # web tree, which nests only dev rows — so the tl_for_sid heuristic
            # must never invent a parent for them. Self-heal artifacts an
            # earlier over-eager backfill left behind: an operator is *always*
            # a root (clear unconditionally); for other non-dev rows clear only
            # when the stored value matches what the heuristic would produce
            # (its fingerprint), so a genuine spawn-time parent (e.g.
            # operator→TL) is preserved.
            if _parent_sid_of(meta):
                try:
                    heuristic = _teams.tl_for_sid(cfg, slug, sid)
                except Exception:
                    heuristic = None
                if role == "operator" or (
                    heuristic and _parent_sid_of(meta) == heuristic
                ):
                    meta.pop("parent_sid", None)
                    meta.pop("parent_sid_heuristic", None)
                    try:
                        _write_session_metadata(md, meta, atomic=True)
                    except OSError:
                        continue
                    corrected += 1
                    details.append({"sid": sid, "cleared": True})
            continue

        # Dev row. Fill-once: never overwrite an existing (spawn-time) parent.
        if _parent_sid_of(meta):
            continue
        try:
            parent = _teams.tl_for_sid(cfg, slug, sid)
        except Exception:
            parent = None
        if not parent or parent == sid:
            continue
        meta["parent_sid"] = parent
        meta["parent_sid_heuristic"] = "true"
        try:
            _write_session_metadata(md, meta, atomic=True)
        except OSError:
            continue
        filled += 1
        details.append({"sid": sid, "parent_sid": parent})
    return {
        "ok": True,
        "scanned": scanned,
        "filled": filled,
        "corrected": corrected,
        "details": details,
    }


def reconcile_primary_from_history(cfg: Any, slug: str) -> dict:
    """T-0525: deterministically repair a cross-wired / unbound LIVE session
    primary from the authoritative ticket ``session_history``.

    THE INVARIANT this stands on: ``spawn`` appends the new SID to the INTENDED
    ticket's ``session_history`` with the correct task_id (a worker-side write,
    immune to the shared-marker race). So a ticket's ``session_history`` is the
    SSOT of "which session was spawned/bound for me"; the session-md ``task_id``
    primary is the unreliable artifact (the SessionStart hook could stamp it from
    a clobbered marker — the pre-fix cross-wire). This pass reconciles the
    artifact back to the SSOT — the in-place primary repair ``bind_task`` cannot
    do (it only appends extras).

    Rules, per LIVE session (a pane exists for its SID; suspended/archived rows
    are history and belong to ``gc_dead_bindings``) under the current user prefix:

      * Compute the session's HOME tickets = open-ish tickets whose
        ``session_history`` lists this SID, EXCLUDING tickets already bound as
        ``extra_task_ids`` (those are legitimate extra bindings, not the primary)
        and the current primary.
      * The current primary is CONSISTENT iff its ticket's ``session_history``
        lists this SID. If consistent, leave it.
      * If the primary is a PHANTOM claimant (not listed by its own ticket) OR
        empty, and there is EXACTLY ONE home candidate, rewrite the primary to it
        (cross-wire repair / unbound adopt). Zero or multiple candidates →
        ambiguous, leave for the other passes (no guessing).
      * Independently, clear a ``last_task_id`` that points at a ticket whose
        ``session_history`` does NOT list this SID — false residue that feeds the
        reconciler oscillation the incident reported.

    TL/operator-role windows never adopt a task primary (they are legitimately
    task-less). Idempotent and convergent: after a rewrite the primary is listed
    by its own ticket, so the next pass is a no-op (no flip-flop).

    Runs BEFORE ``gc_dead_bindings`` / ``gc_stale_bindings`` in the tick so the
    corrected, consistent primary informs those passes (and a primary cross-wired
    onto a CLOSED ticket is repaired here before gc_dead_bindings would strip it).

    Returns ``{"ok": True, "scanned": N, "rewritten": K, "details": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"reconcile_primary_from_history: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    backlog_dir = cfg.data_dir / slug / "backlog"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "rewritten": 0, "details": []}

    user = _get_current_user()
    user_prefix = f"S-{user}-"
    live_sids = {compute_sid(user, p.window, p.pane_id) for p in list_panes()}
    LIVE_STATUSES = {"open", "in_progress", "paused", "totest", "reopened", "planned"}

    # Index every backlog ticket → (status, set(session_history SIDs)). Built once
    # so the per-session scan is a cheap dict lookup. Keyed by the ticket's `id`
    # frontmatter (the canonical T-NNNN), which is what session primaries hold.
    ticket_hist: dict[str, tuple[str, set[str]]] = {}
    if backlog_dir.exists():
        for tmd in sorted(backlog_dir.glob("*.md")):
            try:
                parsed = _frontmatter.parse_or_none(tmd.read_text())
            except OSError:
                continue
            if parsed is None:
                continue
            tmeta = parsed[0]
            tid = str(tmeta.get("id") or "").strip()
            if not tid:
                continue
            status = str(tmeta.get("status") or "").strip()
            hist = set(_frontmatter.as_list(tmeta.get("session_history")))
            ticket_hist[tid] = (status, hist)

    scanned = 0
    details: list[dict] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        sid = meta.get("sid", md.stem)
        # Only LIVE sessions — a suspended/archived row is a historical record
        # owned by gc_dead_bindings; rewriting it would fight that pass.
        if sid not in live_sids:
            continue
        # A TL/operator window must never adopt a single-ticket primary.
        role = _role_of(meta)  # T-0509: honor a morph stamp
        if role != "dev":
            continue
        # T-0324: a constant-team session must never adopt a primary either —
        # its window (e.g. `user-feedback`) derives role "dev", so the role
        # gate alone would let the adopt branch recreate the p181 cross-wire
        # worker-side. Its cross-wired primaries are stripped by
        # reconcile_constant_team_primaries, never repaired toward it.
        if meta.get("owner") == "constant-team":
            continue

        primary = meta.get("task_id")
        primary = primary if (primary and primary != "~") else None
        extras = {t for t in (meta.get("extra_task_ids") or []) if t and t != "~"}
        changed = False

        primary_consistent = (
            primary is not None
            and primary in ticket_hist
            and sid in ticket_hist[primary][1]
        )

        if not primary_consistent:
            candidates = [
                tid for tid, (st, hist) in ticket_hist.items()
                if sid in hist and tid not in extras and tid != primary
                and st in LIVE_STATUSES
            ]
            if len(candidates) == 1:
                new_primary = candidates[0]
                meta["task_id"] = new_primary
                changed = True
                details.append({
                    "sid": sid,
                    "old_primary": primary or "~",
                    "new_primary": new_primary,
                    "reason": "adopt-from-history" if primary is None else "rewrite-crosswired",
                })

        # Clear false last_task_id residue (oscillation feeder).
        ltid = meta.get("last_task_id")
        if ltid and ltid != "~":
            lt_hist = ticket_hist.get(ltid, ("", set()))[1]
            if sid not in lt_hist:
                meta["last_task_id"] = "~"
                changed = True
                details.append({"sid": sid, "cleared_last_task_id": ltid})

        if changed:
            _write_session_metadata(md, meta, atomic=True)

    rewritten = len([d for d in details if "new_primary" in d])
    return {"ok": True, "scanned": scanned, "rewritten": rewritten, "details": details}


def reconcile_constant_team_primaries(cfg: Any, slug: str) -> dict:
    """T-0324: strip a primary ``task_id`` off any ``owner: constant-team``
    session md — whatever path set it.

    A constant-team session is a queue consumer (feedback triage, user intake);
    it has no single ticket, and ``bind_task`` refuses to give it one (T-0185).
    But the p179→p181 incident proved a primary can arrive via OTHER paths (a
    stale SessionStart marker back then; any future writer tomorrow). This pass
    is the belt-and-braces invariant enforcer: no constant-team md holds a
    primary, live or suspended, and the tick self-heals one that appears.

    The stripped value is NOT preserved as ``last_task_id`` — it was never a
    legitimate binding, and ``last_task_id`` feeds the idle-trim / redispatch
    heuristics (T-0202/T-0233). The ticket's ``session_history`` is untouched:
    it never listed the constant-team SID (that disagreement is how the
    incident was detectable), and history stays forensics-only.

    Returns ``{"ok": True, "scanned": N, "stripped": [sids]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(
            f"reconcile_constant_team_primaries: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "stripped": []}

    scanned = 0
    stripped: list[str] = []
    for md in sorted(sessions_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        if meta.get("owner") != "constant-team":
            continue
        tid = meta.get("task_id")
        if not tid or tid == "~":
            continue
        meta["task_id"] = "~"
        _write_session_metadata(md, meta, atomic=True)
        stripped.append(meta.get("sid", md.stem))

    return {"ok": True, "scanned": scanned, "stripped": stripped}


def rehome_primary(cfg: Any, slug: str, task_id: str, to_sid: str) -> dict:
    """T-0324 (H2): safely re-home a task's PRIMARY binding onto ``to_sid``.

    Neither ``bind_task`` (adopt-empty / append-extras only) nor ``unbind_task``
    (refuses to touch the primary) can repair a MIS-SET primary — the p179→p181
    incident's only remedy was hand-editing session-md frontmatter, which races
    ``binding_gc``. This is the missing operator/TL repair tool:

      * refuses a constant-team or TL/operator target (same admission rules as
        ``bind_task`` — a re-home must not create the very state it repairs);
      * refuses a target already holding a DIFFERENT primary (unbind/close that
        first — no silent clobber);
      * under the ``.task-claim.lock`` flock (the same lock spawn/bind_task
        serialize on, so binding_gc's stale-dup pass never sees a half-move):
        strips ``task_id`` off every OTHER session md holding it as primary,
        then stamps it as ``to_sid``'s primary;
      * appends ``to_sid`` to the ticket's ``session_history`` (idempotent) so
        the task-md SSOT agrees with the repaired session md.

    Idempotent: re-homing onto the current holder strips any other claimants
    and succeeds. Returns ``{"ok", "task_id", "to_sid", "stripped": [sids]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"rehome_primary: unknown project slug {slug!r}")

    task_id = (task_id or "").strip()
    to_sid = (to_sid or "").strip()
    if not task_id or not to_sid:
        raise ActionError("rehome_primary: task_id and to_sid are required")

    data_dir = cfg.data_dir
    backlog_dir = data_dir / slug / "backlog"
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise ActionError(f"rehome_primary: task not found: {task_id}")

    sessions_dir = data_dir / slug / "sessions"
    to_md = _session_file(data_dir, slug, to_sid)
    to_meta = _read_session_metadata(to_md)
    if to_meta is None:
        raise ActionError(f"rehome_primary: no metadata for target SID {to_sid!r}")

    # Same admission rules as bind_task: never (re)create a primary on a
    # constant-team or coordination-role session.
    if to_meta.get("owner") == "constant-team":
        raise ActionError(
            f"rehome_primary: target {to_sid!r} is a constant-team session — "
            "it must never hold a primary single-ticket binding")
    role = _role_of(to_meta)  # T-0509: honor a morph stamp
    if role != "dev":
        raise ActionError(
            f"rehome_primary: target {to_sid!r} is a {role} session — only a "
            "dev session can hold a primary task binding")

    cur = to_meta.get("task_id")
    cur = cur if (cur and cur != "~") else None
    if cur is not None and cur != task_id:
        raise ActionError(
            f"rehome_primary: target {to_sid!r} already holds primary {cur} — "
            "unbind/close that first; refusing to clobber")

    claim_lock = backlog_dir / ".task-claim.lock"
    claim_lock.parent.mkdir(parents=True, exist_ok=True)
    stripped: list[str] = []
    with open(claim_lock, "w") as _lockf:
        fcntl.flock(_lockf, fcntl.LOCK_EX)
        # Strip every OTHER claimant (live or not) of this primary. The value
        # is not preserved as last_task_id: a mis-set primary was never a
        # legitimate binding (cf reconcile_constant_team_primaries).
        for md in sorted(sessions_dir.glob("*.md")):
            if md == to_md:
                continue
            meta = _read_session_metadata(md)
            if meta is None:
                continue
            if meta.get("task_id") == task_id:
                meta["task_id"] = "~"
                _write_session_metadata(md, meta, atomic=True)
                stripped.append(meta.get("sid", md.stem))

        # Re-read under the lock (a concurrent bind may have grown extras).
        to_meta = _read_session_metadata(to_md) or to_meta
        to_meta["task_id"] = task_id
        _write_session_metadata(to_md, to_meta, atomic=True)

    try:
        _append_task_session_history(backlog_dir, task_id, to_sid)
    except OSError:
        # Best-effort: the session-md move above is the repair; history
        # append is the audit trail.
        pass

    return {"ok": True, "task_id": task_id, "to_sid": to_sid, "stripped": stripped}


def gc_dead_bindings(cfg: Any, slug: str) -> dict:
    """T-0142: clear task/initiative bindings whose target is closed or gone.

    Refreshes every SessionMd's bindings against the *current* state on disk
    (recomputed each dispatch tick, per the T-0142 DoD):

      - primary ``task_id`` pointing at a backlog task that is ``closed`` or
        whose md is missing → stripped to ``last_task_id`` (this is the live
        symptom the stakeholder reported: suspended devs still showing the
        old task_id in the sessions UI long after the task closed).
      - entries in ``extra_task_ids`` for closed/missing tasks → dropped.
      - primary ``initiative`` whose file is missing → stripped to
        ``last_initiative``; missing entries in ``extra_initiatives`` dropped.

    Bindings for ``open`` / ``in_progress`` / ``totest`` / ``reopened`` tasks
    are left untouched — they are still meaningfully claimed. Duplicate-claim
    races are a separate pass (``gc_stale_bindings``); this pass is about
    *dead* targets, not contested ones.

    Returns ``{"ok": True, "scanned": N, "cleared": K, "details": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_dead_bindings: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "cleared": 0, "details": []}

    data_dir = cfg.data_dir
    user = _get_current_user()
    user_prefix = f"S-{user}-"
    # statuses that mean the binding is still legitimately held
    LIVE_STATUSES = {"open", "in_progress", "paused", "totest", "reopened", "planned"}

    scanned = 0
    details: list[dict] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        sid = meta.get("sid", md.stem)
        changed = False

        # --- primary task_id ---
        tid = meta.get("task_id")
        if tid and tid != "~":
            st = _task_status(data_dir, slug, tid)
            if st is None or st == "closed":
                meta["last_task_id"] = tid
                meta["task_id"] = "~"
                meta["archive_reason"] = f"dead-binding:task-{'missing' if st is None else 'closed'}"
                changed = True
                details.append({"sid": sid, "cleared": tid,
                                "reason": "missing" if st is None else "closed"})

        # --- extra_task_ids ---
        extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
        kept_extras = []
        for et in extras:
            st = _task_status(data_dir, slug, et)
            if st is None or st == "closed":
                changed = True
                details.append({"sid": sid, "cleared": et,
                                "reason": "extra-missing" if st is None else "extra-closed"})
            else:
                kept_extras.append(et)
        if kept_extras != extras:
            meta["extra_task_ids"] = kept_extras

        # --- primary initiative ---
        init = meta.get("initiative")
        if init and init != "~" and not _initiative_exists(data_dir, slug, init):
            meta["last_initiative"] = init
            meta["initiative"] = "~"
            changed = True
            details.append({"sid": sid, "cleared": init, "reason": "initiative-missing"})

        # --- extra_initiatives ---
        einits = [i for i in (meta.get("extra_initiatives") or []) if i and i != "~"]
        kept_inits = [i for i in einits if _initiative_exists(data_dir, slug, i)]
        if kept_inits != einits:
            meta["extra_initiatives"] = kept_inits
            changed = True

        if changed:
            _write_session_metadata(md, meta, atomic=True)

    return {"ok": True, "scanned": scanned, "cleared": len(details), "details": details}


def _parse_ts_epoch(value: Any) -> float | None:
    """Best-effort epoch seconds from a frontmatter timestamp, or None.

    Timestamps are written as ``strftime("%Y-%m-%dT%H:%M:%SZ")`` strings, but
    pyyaml parses ISO-8601 timestamps into ``datetime`` objects on read — so a
    value may be either a ``datetime`` or a string depending on whether it has
    round-tripped through a load. Handle both; a naive datetime/string is
    assumed UTC (every timestamp this codebase writes is UTC).
    """
    if value is None or value == "~" or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _session_idle_age(meta: dict, user_home: str, now_epoch: float) -> float | None:
    """Seconds since this session last did anything, or None if undeterminable.

    Prefers the live Claude transcript activity time (most accurate); falls back
    to the on-disk ``suspended_at`` / ``updated_at`` / ``started_at`` stamps.
    Returns ``None`` when no signal exists so the caller can decline to reap a
    session whose age it cannot positively establish.
    """
    act = _pane_activity_at(
        str(meta.get("cwd") or ""),
        meta.get("claude_uuid"),
        user_home,
        str(meta.get("provider") or "claude"),
    )
    if act is not None:
        return max(0.0, now_epoch - act)
    for key in ("suspended_at", "updated_at", "started_at"):
        ts = _parse_ts_epoch(meta.get(key))
        if ts is not None:
            return max(0.0, now_epoch - ts)
    return None


# ---------------------------------------------------------------------------
# T-0288 — reaper run log ("N processes reaped today").
#
# archive_dead_teammates is THE reaper (T-0233 stale-exited reap + the T-0288
# 12h idle-suspend arm above): every sid it archives is logged here as one
# JSONL line in a day-bucketed file under
# ``<data_dir>/<slug>/_worker/reaper/<YYYY-MM-DD>.jsonl`` (UTC date), flock-
# appended the same way input_mux.enqueue serialises its queue writes — this
# tree is shared across every linux user's worker instance on a multi-tenant
# install, so concurrent appends from different users' ticks must not
# interleave a partial line. Query with ``reaped_today`` / ``reaped_since``.
# No UI surface yet (T-0288 DoD holds the per-row hint + footnote rendering
# behind T-0637's Sessions-area declutter verdict) — this is the queryable
# backend the eventual footnote will call.
# ---------------------------------------------------------------------------
_REAP_LOG_SUBDIR = "reaper"


def _reap_log_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / _REAP_LOG_SUBDIR


def _reap_log_path(cfg: Any, slug: str, date_str: str) -> Path:
    return _reap_log_dir(cfg, slug) / f"{date_str}.jsonl"


def _log_reap_event(cfg: Any, slug: str, sid: str, reason: str, *,
                     now: float | None = None) -> None:
    """Append one reap event. Best-effort — a log-write failure must never
    block or fail the archive it is recording."""
    ts_epoch = time.time() if now is None else now
    date_str = time.strftime("%Y-%m-%d", time.gmtime(ts_epoch))
    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_epoch))
    line = json.dumps(
        {"sid": sid, "reason": reason, "at": ts_iso, "epoch": ts_epoch},
        ensure_ascii=False,
    )
    try:
        log_dir = _reap_log_dir(cfg, slug)
        log_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = open(log_dir / f"{date_str}.lock", "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with open(_reap_log_path(cfg, slug, date_str), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        finally:
            lock_fd.close()
    except OSError:
        pass  # measurement is best-effort; never wedge the reaper on it


def reaped_since(cfg: Any, slug: str, since_epoch: float, *,
                  now: float | None = None) -> int:
    """Count reap events logged by ``_log_reap_event`` at/after ``since_epoch``.

    Reads the day-bucketed jsonl files spanning ``[since_epoch, now]`` (UTC
    dates). Missing log dir / files / unparseable lines all read as absent —
    never raises."""
    now_epoch = time.time() if now is None else now
    log_dir = _reap_log_dir(cfg, slug)
    if not log_dir.exists():
        return 0
    start_date = datetime.fromtimestamp(since_epoch, tz=timezone.utc).date()
    end_date = datetime.fromtimestamp(now_epoch, tz=timezone.utc).date()
    count = 0
    d = start_date
    while d <= end_date:
        try:
            with open(_reap_log_path(cfg, slug, d.isoformat()), encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    ep = rec.get("epoch")
                    if isinstance(ep, (int, float)) and ep >= since_epoch:
                        count += 1
        except OSError:
            pass
        d += timedelta(days=1)
    return count


def reaped_today(cfg: Any, slug: str, *, now: float | None = None) -> int:
    """'N processes reaped today' (T-0288 DoD) — reap events since UTC
    midnight, sourced from the reaper run log."""
    now_epoch = time.time() if now is None else now
    midnight = datetime.fromtimestamp(now_epoch, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    return reaped_since(cfg, slug, midnight, now=now_epoch)


def archive_dead_teammates(cfg: Any, slug: str) -> dict:
    """T-0142/T-0144: auto-archive cleanly-delivered dev teammates.

    Runs on every dispatch tick so a TL never has to manually archive a dev
    whose work is done. Operates only on **dev-role** sessions (a TL/operator
    is never auto-archived) under two rules:

      1. **Exited + delivered.** A dev whose claude pane is gone AND whose task
         is ``totest`` / ``closed`` / missing (or had no binding) is the clean
         post-totest exit the stakeholder wants archived: flip
         ``status: suspended`` + ``archived: true``, clear its task binding,
         and best-effort kill any lingering tmux window. This makes a dev
         *zombie impossible* (T-0144): no live pane + done ⟹ archived, never
         left ``active`` with a stale binding.

      2. **Live + verified-done.** A dev still holding a live pane whose task
         is ``closed`` (TL-verified) is suspended (pane closed) then archived —
         the aggressive working-set trim. We deliberately do NOT force-suspend
         a *live* ``totest`` dev: the TL may still be iterating review with it,
         and killing live in-progress work is the one irreversible mistake to
         avoid. Crashed in-progress devs (pane gone, task still open) are left
         resumable for ``gc_sessions`` to mark suspended.

         T-0202: ``gc_dead_bindings`` runs *earlier in the same tick* and
         strips a just-closed primary ``task_id`` to ``~`` (preserving it as
         ``last_task_id``), so a live verified-done dev never actually
         presents here as live+closed — it presents as live + no current
         task. Rule 2 therefore also consults ``last_task_id``: a live dev
         with no current task, no surviving ``extra_task_ids``, an *idle*
         pane, and ``last_task_id`` → ``closed`` is trimmed with reason
         ``live-last-closed``. Closed-only (never ``totest``) preserves the
         rule-2 safety above; the idle guard (``IDLE_AT_PROMPT_SECONDS`` of
         jsonl quiet) additionally spares a pane that is still mid-write.

      3. **Exited + stale (T-0233).** An *exited* (pane-gone) dev whose task is
         still open (``open`` / ``in_progress`` / ``reopened`` / ``planned``)
         but whose one-time run has done nothing past ``session_stale_sec()``
         (``BOT_SQUAD_SESSION_STALE_SEC``, default 24h) is a crashed/abandoned
         run — reaped with reason ``exited-stale``. The binding is preserved as
         ``last_task_id`` (the task stays open and re-dispatchable to a fresh
         session, per kill-not-resume). Only a run whose age can be positively
         established (transcript activity, else ``suspended_at`` / ``updated_at``
         / ``started_at``) is eligible; LIVE panes and non-dev (operator/TL)
         sessions are never stale-reaped — the role and live guards above hold.

    Returns ``{"ok": True, "scanned": N, "archived": K, "sids": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"archive_dead_teammates: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "archived": 0, "sids": []}

    data_dir = cfg.data_dir
    user = _get_current_user()
    user_home = _get_user_home()
    user_prefix = f"S-{user}-"
    live_panes = list_panes()
    # T-0211: map each live pane to its FULL SID (window + pane_id). The
    # kill-lingering-window cleanup below must target a pane by this SID, never
    # by window name: window names are derived from the ticket slug and recur
    # across respawns, so a dead session's window name collides with a
    # *different* live dev's window in the same tmux session — and a name match
    # would kill that live sibling. `user` equals every processed sid's
    # linux_user because the md loop below is filtered to `user_prefix`, so this
    # keying is identical to the `sid` values it is matched against.
    live_pane_by_sid = {
        compute_sid(user, p.window, p.pane_id): p for p in live_panes
    }
    live_sids = set(live_pane_by_sid)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    now_epoch = time.time()

    scanned = 0
    archived: list[str] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        if str(meta.get("archived", "")).lower() == "true":
            continue
        sid = meta.get("sid", md.stem)
        role = _role_of(  # T-0509: honor a morph stamp
            meta,
            extra_task_ids=[t for t in (meta.get("extra_task_ids") or []) if t and t != "~"],
            extra_initiatives=[i for i in (meta.get("extra_initiatives") or []) if i and i != "~"],
        )
        if role != "dev":
            continue

        is_live = sid in live_sids
        tid = meta.get("task_id")
        has_task = bool(tid and tid != "~")
        st = _task_status(data_dir, slug, tid) if has_task else None
        reason = None
        ltid = None
        if is_live:
            # Live dev: only trim when verified-done (task closed).
            if has_task and st == "closed":
                reason = "live-closed"
            elif not has_task:
                # T-0202: the binding for a just-closed task was already
                # stripped by gc_dead_bindings earlier in this tick, so the
                # verified-done signal lives in last_task_id. Trim only when
                # the dev holds no other live work (gc_dead_bindings has
                # already pruned closed extras, so any survivor is a real
                # claim) and its pane is idle — a missing jsonl activity
                # signal counts as idle, mirroring _derive_activity.
                ltid = meta.get("last_task_id")
                extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
                if (
                    ltid and ltid != "~" and not extras
                    and _task_status(data_dir, slug, ltid) == "closed"
                ):
                    activity_at = _pane_activity_at(
                        str(meta.get("cwd") or ""),
                        meta.get("claude_uuid"),
                        user_home,
                        str(meta.get("provider") or "claude"),
                    )
                    if activity_at is None or (now_epoch - activity_at) >= IDLE_AT_PROMPT_SECONDS:
                        reason = "live-last-closed"
            if reason is None:
                # T-0335 item-10 (Fork-2 Part B): an idle-but-live dev — pane gone
                # quiet past the suspend window — is suspended so it stops holding
                # a slot, even with work still open. T-0288: window defaults to
                # DEFAULT_IDLE_SUSPEND_SEC (12h, Ch. III HARD spec) unless the
                # operator explicitly set [caps].idle_suspend_sec = 0 (OFF).
                # Spared when it is awaiting TG input (blocked_sids: a real "waiting
                # on you" signal, not a leak — T-0977 made that true, by moving
                # the marker's trigger off "peer_sent an operator", which spared
                # every lane that had merely filed a report) or its pane is
                # still active. A session
                # with no transcript activity signal is spared (age unknowable). The
                # task binding is preserved as last_task_id below and the task stays
                # open — reversible, re-dispatchable to a fresh session
                # (kill-not-resume); a non-TG long wait reads as idle, which is why
                # this is opt-in.
                idle_window = _session_idle_suspend_sec(cfg)
                # T-0426: never idle-suspend a dev that owns an in_progress
                # ticket — that strands the ticket (in_progress, owner='-', no
                # auto-re-dispatch; manually resurrected). in_progress is the
                # explicit "actively working" claim; an idle open/planned/
                # reopened dev is still reaped (binding→last_task_id,
                # re-dispatchable). Operator opt-A, the strand-bleed fix.
                if idle_window > 0 and st != "in_progress":
                    try:
                        from bot_squad_worker import tg_stall as _tg_stall
                        _blocked = _tg_stall.blocked_sids(cfg, slug)
                    except Exception:  # noqa: BLE001 — never let it wedge the sweep
                        _blocked = set()
                    if sid not in _blocked:
                        activity_at = _pane_activity_at(
                            str(meta.get("cwd") or ""),
                            meta.get("claude_uuid"),
                            user_home,
                            str(meta.get("provider") or "claude"),
                        )
                        if activity_at is not None and (now_epoch - activity_at) >= idle_window:
                            reason = "idle-suspend"
        else:
            # Exited dev: archive when delivered or orphaned.
            if not has_task:
                reason = "exited-no-task"
            elif st is None:
                reason = "exited-task-missing"
            elif st in ("totest", "closed"):
                reason = f"exited-{st}"
            else:
                # T-0233: the task is still open (open/in_progress/reopened/
                # planned) but this one-time run's pane is gone and it has done
                # nothing past the staleness grace — a crashed/abandoned run.
                # Reap it so it stops lingering in the working set; the binding
                # is preserved below as last_task_id (the task itself stays open
                # and re-dispatchable to a fresh session). Only an exited run
                # whose age we can positively establish is eligible — a session
                # with no age signal is left alone.
                age = _session_idle_age(meta, user_home, now_epoch)
                if age is not None and age >= session_stale_sec():
                    reason = "exited-stale"
        if reason is None:
            continue

        if is_live:
            try:
                suspend(cfg, slug, sid)
            except Exception:
                pass  # if it won't suspend, still record the archive intent
            meta = _read_session_metadata(md) or meta

        # Best-effort: kill THIS session's own lingering tmux window — matched
        # by full SID (window + pane_id), never by window name. T-0211: window
        # names recur across respawns of the same ticket, so a name match could
        # (and did) hit a live *sibling* dev's pane in the same tmux session.
        # An exited dev has no pane here (no-op); a live dev was already closed
        # by suspend() above, so this only mops up a pane that outlived its
        # claude.
        own_pane = live_pane_by_sid.get(sid)
        if own_pane is not None:
            _run(["tmux", "kill-window", "-t", own_pane.pane_id])

        meta["status"] = "suspended"
        meta.setdefault("suspended_at", now)
        if has_task:
            meta["last_task_id"] = tid
            meta["task_id"] = "~"
        elif reason == "live-last-closed":
            # suspend() rewrote the md with a fixed key set that drops
            # last_task_id — restore the signal that justified this trim.
            meta["last_task_id"] = ltid
        meta["archived"] = "true"
        meta["archive_reason"] = f"auto-archive:{reason}"
        _write_session_metadata(md, meta, atomic=True)
        archived.append(sid)
        _log_reap_event(cfg, slug, sid, reason, now=now_epoch)

    return {"ok": True, "scanned": scanned, "archived": len(archived), "sids": archived}


_WINDOW_SANITISE_RE = re.compile(r"[^A-Za-z0-9_-]")

# T-0568: window names that carry no identity — what a window decays to when
# created without ``-n`` (tmux names it after the running command) or when a
# per-user tmux server's automatic-rename tracks pane_current_command. A
# version string ("2.1.139") is the agent-teams subagent binary name tmux
# reports for Claude Code's version-named launchers. `~` is the registry's
# "unset" sentinel.
_GENERIC_WINDOW_NAMES = frozenset(
    {"bash", "sh", "zsh", "fish", "claude", "node", "~"})
_CLAUDE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _is_generic_window(name: str | None) -> bool:
    """True when ``name`` is blank or a shell/binary default — i.e. NOT a
    meaningful session name (T-0568)."""
    n = (name or "").strip()
    if not n:
        return True
    return n in _GENERIC_WINDOW_NAMES or bool(_CLAUDE_VERSION_RE.match(n))


def _sanitise_window(name: str) -> str:
    """Window-name token used in SID derivation (mirrors hook_my_sid.sh)."""
    return _WINDOW_SANITISE_RE.sub("_", (name or "").strip())


def sync_session_name(cfg: Any, slug: str, sid: str, name: str) -> dict:
    """T-0142: rename a session from a single source of truth (the tmux window).

    The session name is one value reflected in three surfaces — the UI/sessions
    row label, the tmux window name, and (cosmetically) the claude tab title.
    The tmux window name is the source of truth; this action is the single
    mutation point the UI and ``bsq team rename`` call so all surfaces stay in
    sync within a tick.

    For a **live** session, renames the tmux window (which rotates the
    pane-derived SID), migrates the SessionMd to the new SID path, rebinds the
    peer-bus inbox triple (T-0072), and re-reconciles the team roster. For a
    **suspended** session (no live pane), updates the registry ``window`` field
    in place — the rename takes visual effect immediately and the SID will
    settle on the next resume.

    Returns ``{"ok": True, "sid": <old>, "new_sid": <new>, "name": <window>}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"sync_session_name: unknown project slug {slug!r}")

    clean = _sanitise_window(name)
    if not clean:
        raise ActionError(f"sync_session_name: invalid name {name!r}")

    data_dir = cfg.data_dir
    user = _get_current_user()
    sessions_dir = data_dir / slug / "sessions"

    # Resolve the md (SID-keyed, falling back to a claude_uuid scan for a
    # session whose window was already renamed out from under its SID).
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        meta_file2 = _find_session_md(sessions_dir, sid, None)
        if meta_file2 is not None:
            meta_file = meta_file2
            meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"sync_session_name: no metadata for SID {sid!r}")

    # Find the live pane for this SID.
    target_pane: PaneInfo | None = None
    for pane in list_panes():
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    if target_pane is None:
        # Suspended/dead: display-rename only, no SID rotation.
        meta["window"] = clean
        _write_session_metadata(meta_file, meta)
        return {"ok": True, "sid": sid, "new_sid": sid, "name": clean}

    # Live: rename the tmux window, rotate the SID, migrate md + peer bus.
    return _rename_live_pane(cfg, slug, meta_file, meta, target_pane, clean)


def _rename_live_pane(
    cfg: Any, slug: str, meta_file: Path, meta: dict, pane: PaneInfo, clean: str,
) -> dict:
    """Rename a LIVE pane's tmux window and carry the registry along: rotate
    the SID, migrate the md to the new path, rebind the peer-bus inbox
    (T-0072) and re-reconcile the team roster. The single rename core shared
    by ``sync_session_name`` (UI / ``bsq team rename``) and the T-0568
    ``reconcile_window_names`` tick pass. ``clean`` must be pre-sanitised."""
    old_sid = str(meta.get("sid") or meta_file.stem)
    _run(["tmux", "rename-window", "-t", pane.pane_id, clean])
    new_sid = compute_sid(_get_current_user(), clean, pane.pane_id)

    meta["sid"] = new_sid
    meta["window"] = clean
    new_meta_file = _session_file(cfg.data_dir, slug, new_sid)
    _write_session_metadata(new_meta_file, meta)
    if new_sid != old_sid and meta_file.exists() and meta_file != new_meta_file:
        meta_file.unlink()

    if new_sid != old_sid:
        try:
            from bot_squad_worker import intersession as _is
            _is.rebind_sid(cfg, slug, old_sid, new_sid)
        except Exception:
            pass  # peer-bus rebind is best-effort; gc will reconcile
        try:
            from bot_squad_worker import teams as _teams
            _teams.reconcile_teams(cfg, slug)
        except Exception:
            pass

    return {"ok": True, "sid": old_sid, "new_sid": new_sid, "name": clean}


def reconcile_window_names(cfg: Any, slug: str) -> dict:
    """T-0568: 60s-tick repair pass — sessions must not stay 'bash'/blank.

    The tmux window name is the naming SSOT (SID = ``S-<user>-<window>-p<N>``),
    but nothing repaired a window that decayed to a generic name: a human
    starting claude in an unnamed window (default name = the running command,
    'bash'), a per-user tmux server auto-renaming to the current command, or a
    manual rename drifting the live name away from the registry. Team status
    filled with ``bash-pN`` rows, and a BLANK window name is worse — it breaks
    registration entirely (``hook_my_sid.sh`` exits on an empty window, so no
    session md is ever written).

    For each live claude pane in this project's repo whose window name is
    generic/blank, derive the best real name and rename through the same core
    ``sync_session_name`` uses (SID rotation + md migration + peer-bus rebind):

      * the md's stored ``window`` field, when non-generic (the registry kept
        the good name while the live window decayed);
      * else the primary ``task_id`` (a bound dev named 'bash' becomes
        ``T-NNNN`` — which the hook's window-name tier also re-derives);
      * else, ONLY for a blank name, a ``claude-<uuid8>``/``claude-p<N>``
        placeholder so the next hook fire can register the session at all;
      * a generic-but-named window with no registry record and no binding is a
        human's own window — left alone.

    The md is resolved by SID, then by the pane's authoritative /proc-walked
    uuid — NEVER the shared-cwd mtime guess (``discover_claude_uuid``), which
    attributes every md-less pane in a cwd to the same md (the cross-attribution
    hazard this ticket's companion guard closes in ``list_sessions``).

    Idempotent: a repaired window is non-generic next tick. Returns
    ``{"ok": True, "renamed": [{sid, new_sid, new_window}, ...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"reconcile_window_names: unknown project slug {slug!r}")

    repo_path = Path(project.repo_path)
    try:
        repo_real = repo_path.resolve()
    except OSError:
        repo_real = repo_path
    sessions_dir = cfg.data_dir / slug / "sessions"
    user = _get_current_user()
    user_home = _get_user_home()

    renamed: list[dict] = []
    try:
        panes = list_panes()
    except Exception:
        return {"ok": True, "renamed": renamed}

    # T-0668: one /proc children-map for every pane's uuid walk below, instead
    # of a full /proc rescan per pane (see _pane_claude_uuid_from_proc's note).
    proc_children = _proc_children_map()
    for pane in panes:
        if not _is_claude_command(pane.command):
            continue
        if not _is_generic_window(pane.window):
            continue
        allow_parent = pane.session == slug or pane.window == "operator"
        if not _cwd_matches_repo(
            pane.cwd, repo_path, repo_real, allow_parent=allow_parent
        ):
            continue

        sid = compute_sid(user, pane.window, pane.pane_id)
        claude_uuid = _pane_claude_uuid_from_proc(pane.pid, user_home, proc_children)
        md_path = _find_session_md(sessions_dir, sid, claude_uuid)
        meta = _read_session_metadata(md_path) if md_path else None

        desired: str | None = None
        if meta is not None:
            stored = str(meta.get("window") or "").strip()
            if stored and not _is_generic_window(stored):
                desired = stored
            else:
                tid = meta.get("task_id")
                if tid and tid != "~":
                    desired = str(tid)
        if desired is None and not (pane.window or "").strip():
            desired = (
                f"claude-{claude_uuid[:8]}" if claude_uuid
                else f"claude-{pane.pane_id.lstrip('%')}"
            )
        if not desired:
            continue
        clean = _sanitise_window(desired)
        if not clean or clean == pane.window:
            continue

        if meta is not None and md_path is not None:
            try:
                res = _rename_live_pane(cfg, slug, md_path, meta, pane, clean)
            except Exception:
                continue
            renamed.append({
                "sid": res["sid"], "new_sid": res["new_sid"], "new_window": clean,
            })
        else:
            # No registry record (blank-window pane): bare tmux rename so the
            # next SessionStart/hook fire can compute a SID and register it.
            _run(["tmux", "rename-window", "-t", pane.pane_id, clean])
            renamed.append({
                "sid": sid,
                "new_sid": compute_sid(user, clean, pane.pane_id),
                "new_window": clean,
            })

    return {"ok": True, "renamed": renamed}
