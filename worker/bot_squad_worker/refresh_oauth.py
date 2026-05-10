"""Claude OAuth refresh — v1 placeholder.

The full port from cctv-backend's ops_refresh_oauth.py is deferred to a later
spec. This placeholder verifies that the ``claude`` binary is on PATH and
returns a successful noop result.

Real refresh logic (token expiry check + HTTPS token rotation) will be added
in a future spec once the credential file path and binary invocation are
confirmed for the production systemd unit.

Return contract:
    {"ok": True|False, "action": "refreshed"|"still-valid"|"noop"|"failed", "detail": str}
"""
from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# Path to the claude binary — may not be on the default PATH inside systemd.
_CLAUDE_BINARY = "/home/almdudleer/.local/bin/claude"


def refresh_oauth(cfg: "Config") -> dict:
    """Check that the claude binary is reachable; return a noop result.

    This is a placeholder for the full OAuth refresh flow.  The real
    implementation (porting cctv's credential-rotation logic) is deferred
    to spec #X because the credential file path and binary invocation
    semantics inside the systemd unit need additional verification.

    DONE_WITH_CONCERNS: placeholder only.
    """
    try:
        proc = subprocess.run(
            [_CLAUDE_BINARY, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            log.warning("refresh_oauth: claude --version exited %d", proc.returncode)
            return {
                "ok": False,
                "action": "failed",
                "detail": f"claude --version exited {proc.returncode}: {proc.stderr.strip()[:200]}",
            }
        version_line = (proc.stdout or proc.stderr or "").strip().splitlines()[0]
        log.info("refresh_oauth: binary reachable — %s", version_line)
        return {
            "ok": True,
            "action": "noop",
            "detail": f"v1 placeholder; real refresh deferred. claude: {version_line}",
        }
    except FileNotFoundError:
        log.warning("refresh_oauth: claude binary not found at %s", _CLAUDE_BINARY)
        return {
            "ok": False,
            "action": "failed",
            "detail": f"claude binary not found at {_CLAUDE_BINARY}",
        }
    except Exception as e:
        log.exception("refresh_oauth: unexpected error")
        return {
            "ok": False,
            "action": "failed",
            "detail": str(e),
        }
