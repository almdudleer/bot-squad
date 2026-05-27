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
from app.project_scaffold import (
    ScaffoldError,
    scaffold_attach_destructive,
    scaffold_new_from_scratch,
    scaffold_paths_as_they_are,
)
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
    Optional T-0051 fields (`repo_master`, `repo_workspace`) are emitted
    only when present so projects created via the T-0021 minimal flow
    don't pick up spurious empty keys.
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
        for opt_key in ("repo_master", "repo_workspace"):
            v = p.get(opt_key)
            if v:
                out.append(f'{opt_key} = "{_toml_escape(str(v))}"')
        targets = p.get("deploy_targets") or []
        items = ", ".join(f'"{_toml_escape(str(t))}"' for t in targets)
        out.append(f"deploy_targets = [{items}]")
        out.append(f'tg_chat = "{_toml_escape(str(p.get("tg_chat", "")))}"')
        out.append("")
    return "\n".join(out)


def _read_projects_toml(config_dir: Path) -> dict[str, dict]:
    raw = tomllib.loads((config_dir / "projects.toml").read_text())
    return dict(raw.get("projects", {}))


_VALID_MODES = (
    "new_from_scratch",
    "paths_as_they_are",
    "attach_destructive",
)


def _require_abs_path(payload_key: str, raw: str) -> Path:
    """Reject relative paths / empty paths at the API boundary. Scaffold
    helpers expect absolute paths so they can build symlink targets
    deterministically."""
    if not raw:
        raise HTTPException(status_code=400, detail=f"{payload_key} required")
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(
            status_code=400,
            detail=f"{payload_key} must be an absolute path",
        )
    return p


_OPERATOR_PLACEHOLDER = (
    "You are the bot-squad project-operator for {slug}. The stakeholder "
    "will brief you when the role doc is written."
)


def _load_operator_brief(cfg: ApiConfig, slug: str) -> str:
    """Build the initial_prompt for a freshly-spawned operator session.

    Reads the canonical operator role md from the install's `bot-squad`
    project data dir (where the dev repo's vision tree gets symlinked to
    on each install) and prepends the per-project framing. Falls back to
    a minimal placeholder when the role doc is absent so a fresh install
    without the bot-squad project still ships a usable brief — the
    follow-on to actually write `vision/roles/operator.md` is T-0052's
    sibling DoD bullet (file at brief time if missing)."""
    role_md = cfg.data_dir / "bot-squad" / "vision" / "roles" / "operator.md"
    if role_md.exists():
        body = role_md.read_text(encoding="utf-8")
        return (
            f'You are the bot-squad project-operator for "{slug}". '
            f"Your role doc follows.\n\n{body}"
        )
    return _OPERATOR_PLACEHOLDER.format(slug=slug)


@router.post("", status_code=201)
async def create_project(
    request: Request,
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    cfg: ApiConfig = request.app.state.api_config
    slug = (payload.get("slug") or "").strip()
    display_name = (payload.get("display_name") or "").strip()
    mode = (payload.get("mode") or "").strip() or None

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
    if mode is not None and mode not in _VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown mode {mode!r}; expected one of {_VALID_MODES}",
        )

    # Re-read on-disk so a hand-edited projects.toml isn't clobbered. The
    # in-memory cfg.projects is loaded once at app start; any out-of-band edit
    # would be lost if we serialized cfg.projects directly.
    config_dir = cfg.config_dir
    new_raw = _read_projects_toml(config_dir)
    if slug in new_raw:
        raise HTTPException(status_code=400, detail=f"project already exists: {slug}")

    # T-0051: per-project data dirs must exist BEFORE scaffold so the
    # ops symlinks it plants into the clones have a real target. The
    # minimal back-compat (mode=None) path also benefits — the worker's
    # reload nudge expects the data dir present.
    data_dir = cfg.project_data_dir(slug)
    for sub in ("backlog", "vision", "feedback", "sessions"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    # Defaults — minimal/back-compat path overwrites only repo_path.
    repo_path_str = (payload.get("repo_path") or "").strip()
    repo_master_str = ""
    repo_workspace_str = ""
    scaffold_summary: dict | None = None

    if mode is None:
        # T-0021 back-compat: registry entry only, no scaffolding. The
        # caller takes responsibility for the on-disk layout.
        pass
    elif mode == "attach_destructive":
        mother_dir = _require_abs_path("mother_dir", (payload.get("mother_dir") or "").strip())
        existing = _require_abs_path("existing_path", (payload.get("existing_path") or "").strip())
        existing_becomes = (payload.get("existing_becomes") or "").strip()
        if existing_becomes not in ("dev", "master"):
            raise HTTPException(
                status_code=400,
                detail="existing_becomes must be 'dev' or 'master'",
            )
        # T-0122: the destructive move is the only operation in the
        # wizard that mutates the user's existing repo. The confirm flag
        # is a defence-in-depth gate against API callers that didn't go
        # through the FE checkbox — the 400 carries the literal rollback
        # so a mis-fire is recoverable from the error response alone.
        if not payload.get("confirm_destructive_move"):
            renamed = mother_dir / existing_becomes
            raise HTTPException(
                status_code=400,
                detail=(
                    "confirm_destructive_move must be true to proceed with "
                    f"the destructive rename of {existing} into {renamed}. "
                    f"Rollback if you mis-fire: mv {renamed} {existing}"
                ),
            )
        try:
            result = scaffold_attach_destructive(
                slug=slug,
                mother_dir=mother_dir,
                existing=existing,
                existing_becomes=existing_becomes,
                install_data_dir=cfg.data_dir,
            )
        except ScaffoldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        repo_path_str = str(result.repo_path)
        repo_master_str = str(result.repo_master)
        repo_workspace_str = str(result.repo_workspace)
        scaffold_summary = {
            "ops_linked": [str(p) for p in result.ops_linked],
            "ops_skipped": [str(p) for p in result.ops_skipped],
        }
    elif mode == "paths_as_they_are":
        mother_dir = _require_abs_path("mother_dir", (payload.get("mother_dir") or "").strip())
        repo_path = _require_abs_path("repo_path", repo_path_str)
        repo_master = _require_abs_path("repo_master", (payload.get("repo_master") or "").strip())
        try:
            result = scaffold_paths_as_they_are(
                slug=slug,
                mother_dir=mother_dir,
                repo_path=repo_path,
                repo_master=repo_master,
                install_data_dir=cfg.data_dir,
            )
        except ScaffoldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        repo_path_str = str(result.repo_path)
        repo_master_str = str(result.repo_master)
        repo_workspace_str = str(result.repo_workspace)
        scaffold_summary = {
            "ops_linked": [str(p) for p in result.ops_linked],
            "ops_skipped": [str(p) for p in result.ops_skipped],
        }
    elif mode == "new_from_scratch":
        mother_dir = _require_abs_path("mother_dir", (payload.get("mother_dir") or "").strip())
        git_remote = (payload.get("git_remote") or "").strip() or None
        try:
            result = scaffold_new_from_scratch(
                slug=slug,
                mother_dir=mother_dir,
                git_remote=git_remote,
                install_data_dir=cfg.data_dir,
            )
        except ScaffoldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        repo_path_str = str(result.repo_path)
        repo_master_str = str(result.repo_master)
        repo_workspace_str = str(result.repo_workspace)
        scaffold_summary = {
            "ops_linked": [str(p) for p in result.ops_linked],
            "ops_skipped": [str(p) for p in result.ops_skipped],
        }

    new_raw[slug] = {
        "slug": slug,
        "display_name": display_name,
        "repo_path": repo_path_str,
        "deploy_branch": "",
        "master_branch": "",
        "prod_url": "",
        "staging_url": "",
        "dev_url": "",
        "deploy_targets": [],
        "tg_chat": "",
        "repo_master": repo_master_str,
        "repo_workspace": repo_workspace_str,
    }

    text = _serialize_projects_toml(new_raw)
    path = config_dir / "projects.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.rename(tmp, path)

    # Hot-reload the API's view, then nudge the worker so its in-memory
    # project list picks up the new slug without a restart (T-0054). On-disk
    # state is already consistent above; if the worker is unreachable we log
    # and still 201 — the next worker restart will pick up projects.toml.
    # Staleness window: roughly (now → next worker restart) during which
    # slug-keyed worker actions (tg_notify, deploy, etc.) will 404 the new
    # slug. peer_send is unaffected — its inbox files are slug-directory-
    # scoped, not config-list-scoped.
    request.app.state.api_config = ApiConfig.load(config_dir)
    try:
        await request.app.state.worker_router.coordinator().call_action(
            "reload_projects", {}, timeout=5.0,
        )
    except WorkerError as e:
        log.warning("create_project %s: worker reload_projects failed: %s", slug, e)

    # T-0052: spawn the per-project operator session right after scaffold.
    # Only fires for deep-flow scaffolded creates — the minimal back-compat
    # path (mode=None) leaves the on-disk layout to the caller, so there's
    # no project repo for an operator to live in. Failures don't roll back
    # the project (already on disk and registered); the API returns 201
    # with a `spawn_error` so the FE can show a manual-spawn nudge.
    operator_sid: str | None = None
    spawn_error: str | None = None
    if scaffold_summary is not None:
        cfg_after = request.app.state.api_config
        operator_brief = _load_operator_brief(cfg_after, slug)
        spawn_client = request.app.state.worker_router.for_user(admin["linux_user"])
        spawn_params: dict = {
            "slug": slug,
            "window": "operator",
            "initial_prompt": operator_brief,
        }
        if admin.get("username"):
            spawn_params["owner"] = admin["username"]
        try:
            spawn_result = await spawn_client.call_action(
                "spawn_session", spawn_params, timeout=10.0,
            )
            operator_sid = spawn_result.get("sid")
        except WorkerError as e:
            log.warning("create_project %s: operator spawn_session failed: %s", slug, e)
            spawn_error = str(e)

    return {
        "slug": slug,
        "display_name": display_name,
        "status": "idle",
        "status_since": None,
        "scaffold": scaffold_summary,
        "operator_sid": operator_sid,
        "spawn_error": spawn_error,
    }


# T-0051 — single source of truth for the three-mode wizard text.
# Vendored canonical copy lives at
# ``api/app/resources/project-create-modes.md`` (the dir name avoids
# the repo-wide ``data/`` gitignore for runtime state). It is consumed
# by:
#   - the FE wizard's rationale-expander (via this endpoint)
#   - the per-user bot-squad-manager session, which has the same text
#     mirrored into AGENT_INSTRUCTIONS.md by
#     scripts/cli/sync_project_create_modes.py
# Anything else that wants the text should fetch it here so drift is
# impossible by construction. The route is registered BEFORE the
# catch-all ``/{slug}`` so `_` (which doesn't match the slug regex
# anyway) routes here.
@router.get("/_/create-modes")
def get_create_modes() -> dict:
    md_path = Path(__file__).parent / "resources" / "project-create-modes.md"
    if not md_path.exists():
        raise HTTPException(
            status_code=500,
            detail="project-create-modes.md missing from API bundle",
        )
    return {"content": md_path.read_text(encoding="utf-8")}


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
