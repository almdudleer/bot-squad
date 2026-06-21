"""Project-scope write authorization (T-0381).

dogfood (T-0331, session p206) proved that PROJECT-WRITE routes were gated by
``Depends(require_auth)`` ONLY — so any authenticated non-admin (e.g. the
server_member ``aqice``) could trigger deploys, rewrite the fleet-wide
AGENTS.md (prompt injection across every agent), enable autonomous runs, spawn
agent sessions (cost abuse), and delete tasks. This is the privesc/authz
cluster alongside [[T-0376]] (create_server), T-0377 (is_self bypass), T-0379
(stale-image deploy).

DECIDED MODEL (operator, 2026-06-21): project writes require the caller to be
an OWNER/MEMBER of the project; admin is always allowed. There is, however, NO
per-project membership/owner store today — the only privilege tier modelled at
project scope is the GLOBAL admin/member role (``mothership_users_store`` /
``auth.toml`` ``is_admin``). So the enforceable gate TODAY is **admin-only**,
which also matches the already-correctly-gated project routes
(``create_project``, ``/system-settings``, ``clones/pull-master`` — all
``require_admin``). In the single-brain model the install operator is the admin.

``require_project_member`` is the single SSOT dependency for that gate and the
extension point: when per-project roles land (T-0216 project-role phase), add
the owner/member lookup HERE and every gated route inherits it — do not scatter
the check across the route modules.

NOTE: until project-roles exist this DENIES non-admin global_members (e.g.
``timpo`` on watchrobot) from project WRITES via the web API. Reads stay broad.
Deny-by-default is the correct posture for a P1 authz fix; loosening later (once
membership is modelled) is a one-line change here.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from app.routes_auth import require_auth


def require_project_member(slug: str, user: dict = Depends(require_auth)) -> dict:
    """Gate a project-WRITE route: 403 unless the caller may write to ``slug``.

    Layered on ``require_auth`` (so 401 still fires for the unauthenticated).
    ``slug`` is resolved from the route's path parameter by FastAPI.

    Today this authorizes global admins only — see the module docstring for the
    project-membership extension point (T-0216).
    """
    if user.get("is_admin"):
        return user
    raise HTTPException(
        status_code=403,
        detail=f"project {slug!r} writes require admin",
    )
