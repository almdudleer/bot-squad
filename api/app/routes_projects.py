"""Project list/detail routes."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tomllib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import ApiConfig
from app.quick_status import aggregate_project_status
from app.routes_auth import require_admin, require_auth
from app.worker_client import WorkerClient, WorkerError, WorkerRouter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_auth)])

_MAX_CONTENT_BYTES = 200 * 1024  # 200 KB

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


async def _project_sessions(
    wrouter: WorkerRouter, slug: str
) -> list[dict]:
    """Fan out list_sessions across every user worker; merge by sid.

    Mirrors routes_sessions.list_sessions but inlined here so /api/projects
    can derive quick-status without an extra round trip from the FE. Dead
    workers degrade to an empty contribution (status = idle) rather than
    502'ing the whole project list — same partial-failure shape T-0025 uses.
    """
    async def _one(client: WorkerClient, who: str) -> list[dict]:
        try:
            result = await client.call_action(
                "list_sessions", {"slug": slug}, timeout=5.0,
            )
            return result.get("sessions", [])
        except WorkerError as e:
            log.warning("quick_status list_sessions for %s/%s failed: %s", slug, who, e)
            return []
        except Exception as e:
            log.warning("quick_status list_sessions for %s/%s crashed: %s", slug, who, e)
            return []

    pairs = wrouter.all_user_workers()
    results = await asyncio.gather(*[_one(c, u) for (u, c) in pairs])
    merged: dict[str, dict] = {}
    for batch in results:
        for row in batch:
            sid = row.get("sid")
            if sid and sid not in merged:
                merged[sid] = row
    return list(merged.values())


@router.get("")
async def list_projects(request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    wrouter: WorkerRouter = request.app.state.worker_router

    slugs = list(cfg.projects.keys())
    sessions_per_project = await asyncio.gather(
        *[_project_sessions(wrouter, s) for s in slugs]
    )

    out: list[dict] = []
    for slug, rows in zip(slugs, sessions_per_project):
        p = cfg.projects[slug]
        status_info = aggregate_project_status(rows)
        out.append({
            "slug": p.slug,
            "display_name": p.display_name,
            "status": status_info["status"],
            "status_since": status_info["status_since"],
        })
    return out


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _serialize_projects_toml(projects_raw: dict[str, dict]) -> str:
    """Hand-rolled writer for projects.toml.

    Mirrors _serialize_auth_toml in routes_users.py: strict, key-ordered,
    no third-party dep. Each project block emits the full Project schema
    (loader requires every key) — missing fields default to "" / [].
    """
    out: list[str] = []
    out.append("# bot-squad project registry. Managed by /api/projects.")
    out.append("")
    for slug in sorted(projects_raw):
        p = projects_raw[slug]
        out.append(f"[projects.{slug}]")
        for key in (
            "slug", "display_name", "repo_path",
            "deploy_branch", "master_branch",
            "prod_url", "staging_url", "dev_url",
        ):
            out.append(f'{key} = "{_toml_escape(str(p.get(key, "")))}"')
        targets = p.get("deploy_targets") or []
        items = ", ".join(f'"{_toml_escape(str(t))}"' for t in targets)
        out.append(f"deploy_targets = [{items}]")
        out.append(f'tg_chat = "{_toml_escape(str(p.get("tg_chat", "")))}"')
        out.append("")
    return "\n".join(out)


def _read_projects_toml(config_dir: Path) -> dict[str, dict]:
    raw = tomllib.loads((config_dir / "projects.toml").read_text())
    return dict(raw.get("projects", {}))


@router.post("", status_code=201)
def create_project(
    request: Request,
    payload: dict,
    _admin: dict = Depends(require_admin),
) -> dict:
    cfg: ApiConfig = request.app.state.api_config
    slug = (payload.get("slug") or "").strip()
    display_name = (payload.get("display_name") or "").strip()
    repo_path = (payload.get("repo_path") or "").strip()

    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must match ^[a-z][a-z0-9_-]*$",
        )
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name required")
    if slug in cfg.projects:
        raise HTTPException(status_code=400, detail=f"project already exists: {slug}")

    # Re-read on-disk so a hand-edited projects.toml isn't clobbered. The
    # in-memory cfg.projects is loaded once at app start; any out-of-band edit
    # would be lost if we serialized cfg.projects directly.
    config_dir = cfg.config_dir
    new_raw = _read_projects_toml(config_dir)
    if slug in new_raw:
        raise HTTPException(status_code=400, detail=f"project already exists: {slug}")
    new_raw[slug] = {
        "slug": slug,
        "display_name": display_name,
        "repo_path": repo_path,
        "deploy_branch": "",
        "master_branch": "",
        "prod_url": "",
        "staging_url": "",
        "dev_url": "",
        "deploy_targets": [],
        "tg_chat": "",
    }

    text = _serialize_projects_toml(new_raw)
    path = config_dir / "projects.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.rename(tmp, path)

    data_dir = cfg.project_data_dir(slug)
    for sub in ("backlog", "vision", "feedback", "sessions"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    # Hot-reload the API's view; the worker still reads projects.toml on
    # startup, so peer/session routing for this slug requires a worker
    # restart. TODO: a worker `reload_projects` action — separate ticket,
    # out of scope for the minimal create affordance.
    request.app.state.api_config = ApiConfig.load(config_dir)

    return {
        "slug": slug,
        "display_name": display_name,
        "status": "idle",
        "status_since": None,
    }


@router.get("/{slug}")
def get_project(slug: str, request: Request) -> dict:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    data_dir = cfg.project_data_dir(slug)
    counts = {
        "backlog": _count(data_dir / "backlog"),
        "vision": _count(data_dir / "vision"),
        "feedback": _count(data_dir / "feedback"),
        "sessions": _count(data_dir / "sessions"),
    }
    return {
        "slug": proj.slug,
        "display_name": proj.display_name,
        "deploy_branch": proj.deploy_branch,
        "prod_url": proj.prod_url,
        "staging_url": proj.staging_url,
        "dev_url": proj.dev_url,
        "counts": counts,
    }


def _count(path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.glob("*.md"))


@router.post("/{slug}/deploy", status_code=202)
async def queue_deploy(
    slug: str, request: Request, payload: dict, user: dict = Depends(require_auth)
) -> dict:
    """Queue a deploy job via the worker's ``deploy`` action.

    Body: ``{"target": "prod"|"staging", "reason": "<text>"}``. The
    ``slug`` is taken from the path; ``requested_by`` is filled from
    the auth context so the worker's audit envelope matches what CLI
    callers emit (``ops/bot-squad-bin/deploy`` sets ``requested_by`` to
    the linux user).

    T-0087: the mothership UI's "Cut a release" button uses this with
    ``target=prod, reason="cut release"``. Until now the API never
    exposed the deploy queue — all queueing came from the CLI helper.
    Mirroring it here lets the UI run the same single-action pattern
    without growing a separate worker action.

    Returns the worker's envelope verbatim
    (``{ok: true, queue_id, queued_at}``) so the FE can poll
    ``GET /api/projects/{slug}/runs`` for the matching ``queue_id`` and
    pivot off ``status``.
    """
    cfg: ApiConfig = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object required")
    target = (payload.get("target") or "").strip()
    reason = (payload.get("reason") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target required")
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")

    # ``deploy_targets`` is the project's allow-list (e.g. ["staging"]
    # for non-mothership projects, ["staging", "prod"] for bot-squad).
    # Worker re-validates, but a 400 here gives the operator a faster
    # signal than a generic worker error.
    if target not in (proj.deploy_targets or []):
        raise HTTPException(
            status_code=400,
            detail=f"unknown target {target!r} for project {slug!r}",
        )

    requested_by = (
        user.get("username") if isinstance(user, dict) else None
    ) or "api"

    client = request.app.state.worker_router.coordinator()
    try:
        return await client.call_action(
            "deploy",
            {
                "slug": slug,
                "target": target,
                "reason": reason,
                "requested_by": requested_by,
            },
            timeout=10.0,
        )
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{slug}/repo-agents-md")
def get_repo_agents_md(slug: str, request: Request) -> dict:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    path = proj.repo_path / "AGENTS.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="AGENTS.md not found in repo")
    return {"content": path.read_text(encoding="utf-8")}


@router.put("/{slug}/repo-agents-md")
def put_repo_agents_md(slug: str, request: Request, payload: dict) -> dict:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    content = payload.get("content") or ""
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail="content must be valid UTF-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")
    path = proj.repo_path / "AGENTS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)
    return {"ok": True}
