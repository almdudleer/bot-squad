"""Welcome screen support — resolves the operator tmux session name.

The post-install "you're all set" screen at the FE route `/welcome` (T-0013)
needs to render a copyable `tmux a -t <operator-session>` command. The
operator session name is a deploy-time constant set by the installer
(``BOTSQUAD_OPERATOR_SESSION`` env var, default ``bot-squad-operator``),
not a project-scoped runtime fact — so we expose it via a tiny dedicated
endpoint rather than fanning out the project-scoped ``list_sessions``
worker action.

Auth: requires login (same surface as the rest of the UI).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from app.routes_auth import require_auth

router = APIRouter(
    prefix="/welcome",
    tags=["welcome"],
    dependencies=[Depends(require_auth)],
)


# Default matches scripts/install/install.sh:30 (BOTSQUAD_OPERATOR_SESSION).
DEFAULT_OPERATOR_SESSION = "bot-squad-operator"


@router.get("/operator")
def get_operator_session() -> dict:
    """Return the tmux session name for the bot-squad operator.

    The installer creates this session unconditionally (idempotent) and
    bakes the name into the install via ``BOTSQUAD_OPERATOR_SESSION``.
    The UI uses it to render the ``tmux a -t <name>`` attach command on
    the post-install /welcome screen.
    """
    name = os.environ.get("BOTSQUAD_OPERATOR_SESSION", DEFAULT_OPERATOR_SESSION)
    # Defensive: env vars stringify to "", which would render a useless
    # `tmux a -t` command with no target. Fall through to the default.
    if not name.strip():
        name = DEFAULT_OPERATOR_SESSION
    return {"session": name.strip()}
