"""Post-attach hooks for newly-attached GlobalUsers.

T-0067 coordination seam: Bundle B's ``/api/auth/attach`` endpoint (which
maps a GlobalUser to a local linux_user on this install) calls
``on_attach(linux_user)`` here AFTER the GlobalUser row has landed. We
record a "first-login enablement pending" marker so the UI can surface
the one-shot enable script (`scripts/install/enable-per-user-worker.sh`)
to the human.

The API process (in docker) and the coordinator worker (on the host as
the install-owner) both lack the privilege to run the enable flow on
behalf of another user — `loginctl enable-linger <other>` needs sudo,
and `systemctl --user enable` needs to run AS that user. So this hook
deliberately does NOT escalate; it just leaves a marker the user can
act on themselves once they ssh in.

The marker lives at::

    <data_dir>/_users/<linux_user>/enable-worker.pending

Mode 0644, readable across the install (it carries no secrets — just an
ISO timestamp of when the attach happened). The enable script can `rm`
it on success; the UI can poll it to render the "you still need to run
the enable script" banner.

Idempotent: re-calling ``on_attach`` for the same user updates the
timestamp but doesn't error.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_LINUX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _marker_path(data_dir: Path, linux_user: str) -> Path:
    return data_dir / "_users" / linux_user / "enable-worker.pending"


def on_attach(data_dir: Path, linux_user: str) -> dict:
    """Record that ``linux_user`` still needs to run the per-user worker
    enable script. Best-effort, fire-and-forget; never raises.

    Returns ``{"ok": bool, "marker": <path or None>, "reason": <str or None>}``
    so callers can log the outcome but should not branch on it.
    """
    if not isinstance(linux_user, str) or not _LINUX_USER_RE.match(linux_user):
        log.warning("attach_hooks.on_attach: invalid linux_user %r", linux_user)
        return {"ok": False, "marker": None, "reason": "invalid linux_user"}

    marker = _marker_path(Path(data_dir), linux_user)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker.write_text(f"{ts}\n")
        marker.chmod(0o644)
    except OSError as e:
        log.warning("attach_hooks.on_attach: write %s failed: %s", marker, e)
        return {"ok": False, "marker": str(marker), "reason": str(e)}

    log.info("attach_hooks.on_attach: enablement pending marker → %s", marker)
    return {"ok": True, "marker": str(marker), "reason": None}


def is_enable_pending(data_dir: Path, linux_user: str) -> bool:
    """Return True iff ``linux_user`` still has a pending enablement marker.

    Used by /api/me (or wherever the UI surfaces the banner) to decide
    whether to nudge the user. Bare existence check — the marker's
    content is just a timestamp for debugging.
    """
    if not isinstance(linux_user, str) or not _LINUX_USER_RE.match(linux_user):
        return False
    return _marker_path(Path(data_dir), linux_user).is_file()


def clear_enable_pending(data_dir: Path, linux_user: str) -> bool:
    """Remove the enablement-pending marker.

    The enable script can call this (via a small worker action) after a
    successful run so the UI banner stops nudging. Returns True iff a
    marker was actually removed.
    """
    if not isinstance(linux_user, str) or not _LINUX_USER_RE.match(linux_user):
        return False
    p = _marker_path(Path(data_dir), linux_user)
    if not p.is_file():
        return False
    try:
        p.unlink()
        return True
    except OSError as e:
        log.warning("attach_hooks.clear_enable_pending: unlink %s failed: %s", p, e)
        return False
