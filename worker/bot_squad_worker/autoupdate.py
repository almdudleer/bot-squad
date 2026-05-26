"""Consumer-side autoupdate poller (T-0083).

Each non-mothership bot-squad install runs this tick periodically to poll
the mothership release feed (T-0082) and queue an apply job (T-0084) when
a newer version is published. The apply pipeline itself is out of scope
here — this module only enqueues file-based jobs that T-0084 will drain.

State files (all under ``cfg.data_dir / "_worker"``):

* ``autoupdate.json`` — singleton describing the currently-installed
  version and last-poll bookkeeping. Schema::

      {
        "installed_version": "v2026.05.16.1" | null,
        "last_check_at": "<iso8601>",
        "last_apply_at": "<iso8601>" | null,
        "last_apply_outcome": "success" | "failed:<step>" | "never",
        "current_git_sha": "<sha40>" | null
      }

  ``last_check_at`` is bumped every tick regardless of outcome so the
  operator UI (T-0089) can show liveness.

* ``autoupdate_queue/<uuid>.json`` — apply jobs. Each file is a full
  manifest entry as produced by T-0081 (canonical schema lives in that
  ticket; we treat it as opaque here and forward it verbatim).

Version comparison
------------------

Releases are tagged ``vYYYY.MM.DD.N`` with zero-padded date components
(see T-0081). With ``N`` rarely exceeding a single digit in practice,
lexicographic string comparison agrees with chronological order
(``v2026.05.16.1`` < ``v2026.05.16.2`` < ``v2026.06.01.1``). To stay
correct even when ``N`` rolls into double digits on a busy day, we parse
the trailing counter as an int (``_parse_version``).

Mothership self-exclusion
-------------------------

The poller short-circuits to a no-op on the mothership itself. T-0086
will normally prevent the tick from being scheduled at all, but this is
belt-and-suspenders — the producer of releases must never consume them.

Telemetry piggy-back (T-0088)
-----------------------------

Each tick also POSTs the local autoupdate.json state to
``<mothership>/api/releases/_telemetry`` so the mothership UI (T-0087)
can render an installs grid showing every consumer's current version +
last apply outcome. The POST is best-effort — a failure (transport,
4xx, 5xx) never blocks the rest of the tick, and we send a fresher
snapshot on the next cycle. The consumer's ``install_id`` is read from
``BOT_SQUAD_INSTALL_ID`` if set, else from
``<data_dir>/_worker/install.id`` which the installer script writes
after the mothership ``/connect`` handshake.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mothership detection — canonical helper from T-0086.
# ---------------------------------------------------------------------------

from bot_squad_worker.install_role import is_mothership


# ---------------------------------------------------------------------------
# Env / constants
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes (overridable via env)
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
LATEST_PATH = "/api/releases/latest"
TELEMETRY_PATH = "/api/releases/_telemetry"
INSTALL_ID_FILE = "install.id"
# T-0089: operator pause flag honored by both the poller (skip enqueue)
# and the apply drainer (skip drain). last_check_at still advances so the
# UI's "checked Nm ago" liveness clock stays accurate while paused.
PAUSED_FLAG_FILE = "autoupdate_paused.flag"


def interval_seconds() -> int:
    """Tick cadence, in seconds. Reads ``BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS``."""
    raw = os.environ.get("BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS")
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        log.warning(
            "autoupdate: invalid BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS=%r; using default",
            raw,
        )
        return DEFAULT_INTERVAL_SECONDS


def mothership_url() -> Optional[str]:
    """Resolve the mothership base URL from env.

    Primary key: ``BOT_SQUAD_MOTHERSHIP_URL`` (canonical for this initiative).
    Fallback: ``BOTSQUAD_MOTHERSHIP_URL`` (the install-script convention from
    ``scripts/install/install.sh``; kept for parity with existing consumers).
    """
    url = (
        os.environ.get("BOT_SQUAD_MOTHERSHIP_URL")
        or os.environ.get("BOTSQUAD_MOTHERSHIP_URL")
    )
    if not url:
        return None
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# State paths / I/O
# ---------------------------------------------------------------------------

def state_path(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "autoupdate.json"


def install_id_path(cfg: Any) -> Path:
    """Path of the persisted Chapter-I install_id (``srv_<hex>``).

    Written by ``scripts/install/install.sh`` after the mothership ``/connect``
    handshake mints a ``server_id`` (T-0024 — alongside the existing
    ``$BOTSQUAD_STATE_DIR/server.token``). Living under ``data/_worker/``
    keeps it next to ``autoupdate.json`` so the worker can read it without
    needing to know the installer user's ``$HOME``.
    """
    return cfg.data_dir / "_worker" / INSTALL_ID_FILE


def install_id(cfg: Any) -> Optional[str]:
    """Resolve this consumer's mothership-issued install_id.

    Preference order:
      1. ``BOT_SQUAD_INSTALL_ID`` env var (override for tests + ad-hoc fixes).
      2. ``<data_dir>/_worker/install.id`` (written at install time).
      3. ``None`` — caller treats telemetry POST as a no-op (logs once at
         debug; tick still runs).
    """
    env = os.environ.get("BOT_SQUAD_INSTALL_ID")
    if env:
        v = env.strip()
        if v:
            return v
    p = install_id_path(cfg)
    if not p.is_file():
        return None
    try:
        v = p.read_text().strip()
    except OSError as e:
        log.warning("autoupdate: could not read install.id at %s: %s", p, e)
        return None
    return v or None


def queue_dir(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "autoupdate_queue"


def paused_flag_path(cfg: Any) -> Path:
    """Path of the operator pause flag (T-0089).

    Presence of the file = paused; absence = active. We use a flag file
    rather than a JSON field so the API can toggle it atomically without
    racing the poller's autoupdate.json writer.
    """
    return cfg.data_dir / "_worker" / PAUSED_FLAG_FILE


def is_paused(cfg: Any) -> bool:
    """T-0089: True iff the operator has paused autoupdate on this consumer."""
    return paused_flag_path(cfg).exists()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(cfg: Any) -> dict:
    """Return the autoupdate.json contents (default skeleton if missing)."""
    p = state_path(cfg)
    if not p.exists():
        return {
            "installed_version": None,
            "last_check_at": None,
            "last_apply_at": None,
            "last_apply_outcome": "never",
            "current_git_sha": None,
        }
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("autoupdate: could not parse %s: %s; treating as missing", p, e)
        return {
            "installed_version": None,
            "last_check_at": None,
            "last_apply_at": None,
            "last_apply_outcome": "never",
            "current_git_sha": None,
        }


def save_state(cfg: Any, state: dict) -> None:
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Version compare
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple[int, int, int, int]:
    """Parse ``vYYYY.MM.DD.N`` into a sortable tuple.

    Tolerant: a malformed version sorts as the smallest possible tuple so
    that a malformed *installed* version always loses to a sane manifest
    entry (we'd rather try to update than get stuck).
    """
    s = v[1:] if v.startswith("v") else v
    parts = s.split(".")
    try:
        y = int(parts[0])
        m = int(parts[1])
        d = int(parts[2])
        n = int(parts[3]) if len(parts) > 3 else 0
    except (IndexError, ValueError):
        return (-1, -1, -1, -1)
    return (y, m, d, n)


def is_newer(candidate: str, installed: str) -> bool:
    """True iff ``candidate`` > ``installed`` under the release scheme."""
    return _parse_version(candidate) > _parse_version(installed)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def _fetch_latest(base_url: str, *, timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> Optional[dict]:
    """GET ``<base>/api/releases/latest``; one retry on transient network error.

    Returns the manifest entry dict on success, ``None`` on permanent failure
    (network, non-2xx, or non-JSON body). Caller is responsible for the
    success/failure bookkeeping in autoupdate.json.

    Factored out so T-0088 can piggy-back a telemetry POST on the same tick
    without re-doing the polling/retry logic.
    """
    url = f"{base_url.rstrip('/')}{LATEST_PATH}"
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            resp = httpx.get(url, timeout=timeout)
            resp.raise_for_status()
            entry = resp.json()
            if not isinstance(entry, dict):
                log.warning("autoupdate: unexpected body shape from %s: %r", url, type(entry))
                return None
            return entry
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_err = e
            log.info(
                "autoupdate: transient fetch error (attempt %d) for %s: %s",
                attempt, url, e,
            )
            continue
        except httpx.HTTPStatusError as e:
            log.warning("autoupdate: HTTP %d from %s", e.response.status_code, url)
            return None
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("autoupdate: non-JSON body from %s: %s", url, e)
            return None
    log.warning("autoupdate: gave up after retry for %s: %s", url, last_err)
    return None


# ---------------------------------------------------------------------------
# Telemetry POST (T-0088)
# ---------------------------------------------------------------------------

# Fields the mothership expects (mirrored from
# ``api/app/routes_releases.py:_TELEMETRY_FIELDS``). We send the local
# autoupdate.json state verbatim minus anything not on this list, so we
# never accidentally leak a future debug-only state key over the wire.
_TELEMETRY_FIELDS = (
    "installed_version",
    "last_check_at",
    "last_apply_at",
    "last_apply_outcome",
    "current_git_sha",
)


def _post_telemetry(
    base_url: str,
    iid: str,
    state: dict,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> bool:
    """Best-effort POST of telemetry state to the mothership.

    Returns True on 2xx, False on anything else (incl. transport errors,
    timeouts, 4xx, 5xx). Telemetry is advisory — a failed POST never
    blocks the rest of the tick, and we deliberately do NOT retry: the
    next tick (15 min by default) will resend a fresher snapshot anyway.

    Logs 403 at WARNING because that means the consumer's ``install.id``
    no longer matches a registry row — typically the mothership re-issued
    the install, or this consumer was removed. The operator needs to know.
    """
    url = f"{base_url.rstrip('/')}{TELEMETRY_PATH}"
    body = {"install_id": iid, **{k: state.get(k) for k in _TELEMETRY_FIELDS}}
    try:
        resp = httpx.post(url, json=body, timeout=timeout)
    except (httpx.TransportError, httpx.TimeoutException) as e:
        log.info("autoupdate: telemetry POST transport error for %s: %s", url, e)
        return False
    if resp.status_code == 403:
        log.warning(
            "autoupdate: telemetry POST rejected (403) — install_id %r not in "
            "mothership registry (re-install or removed?)",
            iid,
        )
        return False
    if resp.status_code >= 400:
        log.info(
            "autoupdate: telemetry POST returned HTTP %d for %s",
            resp.status_code, url,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Apply-job queue
# ---------------------------------------------------------------------------

def _enqueue_apply(cfg: Any, entry: dict) -> Path:
    """Write the manifest entry to a fresh queue file. Returns the path."""
    qdir = queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    p = qdir / f"{uuid.uuid4().hex}.json"
    p.write_text(json.dumps(entry, indent=2, sort_keys=True))
    return p


# ---------------------------------------------------------------------------
# Per-tick handler — factored so T-0088 can plug a telemetry POST in here.
# ---------------------------------------------------------------------------

def _handle_latest(cfg: Any, entry: dict) -> str:
    """Compare ``entry`` to the recorded installed version and act.

    Returns one of:

    * ``"first_run_stamped"`` — no prior state; stamped manifest as installed.
    * ``"enqueued"`` — newer version detected; apply job written.
    * ``"paused"`` — newer version available but operator paused enqueue (T-0089).
    * ``"up_to_date"`` — manifest version matches installed.
    * ``"older"``     — manifest version is older than installed (no-op).
    * ``"bad_entry"`` — manifest entry missing required fields (no-op).
    """
    version = entry.get("version")
    if not isinstance(version, str) or not version:
        log.warning("autoupdate: manifest entry missing 'version': %r", entry)
        return "bad_entry"

    state = load_state(cfg)
    installed = state.get("installed_version")

    if not installed:
        # First run: we don't know what's actually installed, but the spec
        # says assume aligned with the current latest — do NOT apply.
        # First-run stamping is a bookkeeping op, not an apply, so we still
        # do it even when paused (T-0089).
        state["installed_version"] = version
        state["current_git_sha"] = entry.get("git_sha") or state.get("current_git_sha")
        save_state(cfg, state)
        log.info("autoupdate: first-run stamp -> installed_version=%s", version)
        return "first_run_stamped"

    if is_newer(version, installed):
        # T-0089: skip enqueue while operator paused; last_check_at still
        # advances upstream in tick() so the UI's liveness clock keeps moving.
        if is_paused(cfg):
            log.info(
                "autoupdate: newer version %s available but PAUSED (T-0089) — not enqueuing",
                version,
            )
            return "paused"
        path = _enqueue_apply(cfg, entry)
        log.info(
            "autoupdate: newer version available (%s > %s); enqueued %s",
            version, installed, path.name,
        )
        return "enqueued"

    if version == installed:
        return "up_to_date"

    log.debug("autoupdate: manifest version %s older than installed %s — ignoring",
              version, installed)
    return "older"


# ---------------------------------------------------------------------------
# Public tick
# ---------------------------------------------------------------------------

def tick(cfg: Any) -> None:
    """One poll cycle. Safe to call from APScheduler.

    Bumps ``last_check_at`` on every invocation (even on transient failures)
    so the operator UI can distinguish "poller alive but mothership down"
    from "poller dead".
    """
    # Belt-and-suspenders mothership exclusion (T-0086 should also prevent
    # scheduling this tick at all). Pass cfg.config_dir so the check reads
    # THIS install's projects.toml, not the hardcoded /home/www/bot-squad
    # fallback — the latter mis-flagged sibling/dogfood installs as
    # mothership and silently no-op'd their pollers.
    if is_mothership(config_dir=getattr(cfg, "config_dir", None)):
        log.debug("autoupdate: mothership self-exclusion — tick is a no-op")
        return

    base_url = mothership_url()
    if not base_url:
        log.debug("autoupdate: BOT_SQUAD_MOTHERSHIP_URL not set — tick is a no-op")
        return

    # Touch last_check_at up front so a crash mid-fetch still records the
    # attempt. We rewrite state again on success.
    state = load_state(cfg)
    state["last_check_at"] = _now_iso()
    save_state(cfg, state)

    entry = _fetch_latest(base_url)
    if entry is not None:
        try:
            _handle_latest(cfg, entry)
        except Exception:
            log.exception("autoupdate: _handle_latest raised on entry=%r", entry)

    # T-0088: piggy-back telemetry on every tick (even when the manifest
    # fetch failed) so the operator UI's "last seen" clock keeps moving
    # while the mothership is intermittently unreachable in EITHER direction.
    # We re-load state here so the snapshot includes any updates _handle_latest
    # just wrote (e.g. first_run_stamped bumping installed_version). Failure
    # is silent at the tick level — _post_telemetry already logs.
    iid = install_id(cfg)
    if not iid:
        log.debug(
            "autoupdate: no install.id (and BOT_SQUAD_INSTALL_ID unset) — "
            "skipping telemetry POST"
        )
        return
    try:
        _post_telemetry(base_url, iid, load_state(cfg))
    except Exception:
        log.exception("autoupdate: telemetry POST raised unexpectedly")
