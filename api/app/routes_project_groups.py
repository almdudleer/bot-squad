"""Per-project user-GROUP management endpoints (T-0496).

CRUD for a project's groups + membership management. voice-05: groups are
per-project, UI-managed, each with a role / access scope / prompt; admins manage
them. Mounted under ``/api/m`` (MOTHERSHIP build only) because membership keys on
the mothership ``global_user_id`` (T-0488 cross-server identity).

Auth: project-access-gated via ``app.project_authz`` — the SSOT for project
read/write gating (admin-only today; the per-project-membership extension point
is documented there, T-0216). Reads use ``require_project_read``, writes use
``require_project_member``. Both resolve ``slug`` from the path.

Route map (prefix ``/projects`` -> ``/api/m/projects``):
* GET    /{slug}/groups                          list groups            (read)
* POST   /{slug}/groups                          create a group         (write)
* GET    /{slug}/groups/{group_id}               get one group          (read)
* PATCH  /{slug}/groups/{group_id}               update a group         (write)
* DELETE /{slug}/groups/{group_id}               delete a group         (write)
* GET    /{slug}/groups/{group_id}/members       list a group's members (read)
* PUT    /{slug}/groups/{group_id}/members/{gid} bind user -> group     (write)
* DELETE /{slug}/members/{gid}                    drop a user's membership (write)
* GET    /{slug}/members/{gid}/group             which group is user in (read)

The last route EXPOSES the T-0478 user-conversation seam: the conversational
role reads it (or imports ``ProjectGroupsStore.group_for_user`` in-process) to
load the group's prompt + enforce its access scope. Loading/enforcing is NOT
built here (deferred to the role); this is the data surface it consumes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.project_authz import require_project_member, require_project_read
from app.project_groups_store import ProjectGroupsStore


# Session-auth surface; per-route project-access gates layer on top.
router = APIRouter(prefix="/projects", tags=["project-groups"])


def _store(request: Request) -> ProjectGroupsStore:
    return ProjectGroupsStore(request.app.state.api_config.data_dir)


def _require_known_project(request: Request, slug: str) -> None:
    """404 if ``slug`` is not a registered project — a group can't exist for a
    project that doesn't (and the path is otherwise unbounded user input)."""
    if request.app.state.api_config.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


# ---- group CRUD -------------------------------------------------------------


@router.get("/{slug}/groups", dependencies=[Depends(require_project_read)])
def list_groups(slug: str, request: Request) -> dict:
    _require_known_project(request, slug)
    return {"groups": [g.to_public() for g in _store(request).list_groups(slug)]}


@router.post("/{slug}/groups", dependencies=[Depends(require_project_member)])
def create_group(slug: str, request: Request, payload: dict) -> dict:
    _require_known_project(request, slug)
    try:
        group = _store(request).create_group(
            slug,
            name=str(payload.get("name") or ""),
            role=str(payload.get("role") or ""),
            access_scope=str(payload.get("access_scope") or ""),
            prompt=str(payload.get("prompt") or ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return group.to_public()


@router.get("/{slug}/groups/{group_id}", dependencies=[Depends(require_project_read)])
def get_group(slug: str, group_id: str, request: Request) -> dict:
    _require_known_project(request, slug)
    group = _store(request).get_group(slug, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"unknown group: {group_id}")
    return group.to_public()


@router.patch("/{slug}/groups/{group_id}", dependencies=[Depends(require_project_member)])
def update_group(slug: str, group_id: str, request: Request, payload: dict) -> dict:
    _require_known_project(request, slug)
    # Only forward keys actually present so a partial PATCH leaves the rest intact.
    fields = {k: payload[k] for k in ("name", "role", "access_scope", "prompt") if k in payload}
    try:
        group = _store(request).update_group(slug, group_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if group is None:
        raise HTTPException(status_code=404, detail=f"unknown group: {group_id}")
    return group.to_public()


@router.delete("/{slug}/groups/{group_id}", dependencies=[Depends(require_project_member)])
def delete_group(slug: str, group_id: str, request: Request) -> dict:
    _require_known_project(request, slug)
    removed = _store(request).delete_group(slug, group_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"unknown group: {group_id}")
    return {"deleted": group_id}


# ---- membership -------------------------------------------------------------


@router.get("/{slug}/groups/{group_id}/members", dependencies=[Depends(require_project_read)])
def list_members(slug: str, group_id: str, request: Request) -> dict:
    _require_known_project(request, slug)
    store = _store(request)
    if store.get_group(slug, group_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown group: {group_id}")
    return {"group_id": group_id, "members": store.list_members(slug, group_id)}


@router.put(
    "/{slug}/groups/{group_id}/members/{global_user_id}",
    dependencies=[Depends(require_project_member)],
)
def set_membership(slug: str, group_id: str, global_user_id: str, request: Request) -> dict:
    _require_known_project(request, slug)
    try:
        _store(request).set_membership(slug, global_user_id, group_id)
    except ValueError as e:
        # Unknown group / empty user id -> client error.
        raise HTTPException(status_code=400, detail=str(e))
    return {"slug": slug, "global_user_id": global_user_id, "group_id": group_id}


@router.delete(
    "/{slug}/members/{global_user_id}",
    dependencies=[Depends(require_project_member)],
)
def remove_membership(slug: str, global_user_id: str, request: Request) -> dict:
    _require_known_project(request, slug)
    removed = _store(request).remove_membership(slug, global_user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="user is not a member of any group")
    return {"slug": slug, "global_user_id": global_user_id, "removed": True}


@router.get(
    "/{slug}/members/{global_user_id}/group",
    dependencies=[Depends(require_project_read)],
)
def group_for_user(slug: str, global_user_id: str, request: Request) -> dict:
    """T-0478 seam: the group a user is in for this project, as
    ``{role, access_scope, prompt}`` (``group`` is ``null`` when none). The
    conversational role consumes this to load the prompt + enforce scope."""
    _require_known_project(request, slug)
    group = _store(request).group_for_user(slug, global_user_id)
    return {
        "slug": slug,
        "global_user_id": global_user_id,
        "group": group.to_public() if group is not None else None,
    }
