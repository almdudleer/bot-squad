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
from app.payload_guard import str_field
from app.project_scaffold import (
    ScaffoldError,
    scaffold_attach_destructive,
    scaffold_new_from_scratch,
    scaffold_paths_as_they_are,
)
from app.project_authz import require_project_member
from app.quick_status import aggregate_project_status
from app.routes_auth import require_admin, require_auth
from app.worker_client import WorkerClient, WorkerError, WorkerRouter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_auth)])

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
        # T-0156: optional forum-thread id — bare TOML integer, emitted only
        # when set so DM/general-feed projects don't grow a spurious key.
        topic = p.get("tg_topic_id")
        if topic not in (None, ""):
            out.append(f"tg_topic_id = {int(topic)}")
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

    T-0236 (with T-0203): pointer-ize, don't inline. This previously read the
    full operator role md (~5KB / ~1.3k tokens) into the spawn prompt on every
    operator spawn. Consistent with the T-0203 context-bloat work, the brief now
    hands the operator a small POINTER and lets it pull its full orientation on
    demand via ``bsq brief`` (which emits product + team protocol + the operator
    role contract) — no loss of guidance, far less spawn context. We still gate
    the pointer on the install having been scaffolded (the seeded
    ``data/bot-squad/vision/roles/operator.md`` marker) so a fresh, unseeded
    install falls back to the self-contained placeholder (the follow-on to write
    that doc is T-0052's sibling DoD bullet).

    NOTE (T-0716/D-0043): the seeded copy is an install-shape MARKER only. The
    contract the operator actually reads is the git SSOT
    ``api/app/resources/roles/operator.md`` — which is what the pointer names."""
    role_md = cfg.data_dir / "bot-squad" / "vision" / "roles" / "operator.md"
    if role_md.exists():
        return (
            f'You are the bot-squad project-operator for "{slug}".\n\n'
            "Run `bsq brief` to load your full orientation on demand — it prints "
            "the product overview, the team protocol, and your operator role "
            "contract ($BOT_SQUAD/api/app/resources/roles/operator.md). "
            "Pull other docs "
            "(tickets, initiatives, AGENT_INSTRUCTIONS.md) only when you need "
            "them rather "
            "than holding them in context.\n\n"
            "You are a transient DISPATCHER, not a persistent chat: when on, your "
            "standing task is to clear the backlog autonomously — don't wait on "
            "the user (they check in and correct you), drive open tasks forward "
            "within the parallelism + token constraints. Full contract in your "
            "role doc.\n\n"
            "First actions: run `bsq brief`, then `bsq inbox check`, then start "
            "clearing the backlog."
        )
    return _OPERATOR_PLACEHOLDER.format(slug=slug)


@router.post("", status_code=201)
async def create_project(
    request: Request,
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    cfg: ApiConfig = request.app.state.api_config
    slug = str_field(payload, "slug")
    display_name = str_field(payload, "display_name")
    mode = str_field(payload, "mode") or None

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
    repo_path_str = str_field(payload, "repo_path")
    repo_master_str = ""
    repo_workspace_str = ""
    scaffold_summary: dict | None = None

    if mode is None:
        # T-0021 back-compat: registry entry only, no scaffolding. The
        # caller takes responsibility for the on-disk layout.
        pass
    elif mode == "attach_destructive":
        mother_dir = _require_abs_path("mother_dir", str_field(payload, "mother_dir"))
        existing = _require_abs_path("existing_path", str_field(payload, "existing_path"))
        existing_becomes = str_field(payload, "existing_becomes")
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
        mother_dir = _require_abs_path("mother_dir", str_field(payload, "mother_dir"))
        repo_path = _require_abs_path("repo_path", repo_path_str)
        repo_master = _require_abs_path("repo_master", str_field(payload, "repo_master"))
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
        mother_dir = _require_abs_path("mother_dir", str_field(payload, "mother_dir"))
        git_remote = str_field(payload, "git_remote") or None
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

    # T-0386 / INI-04: create-on-project provisioning of the per-project forum
    # topics (#feedback / #deploy-logs / #team-queries). Best-effort and
    # idempotent — it no-ops when the supergroup (tg_chat) isn't configured yet
    # (the supergroup is a 1-time manual setup; the operator can re-run the
    # provision action afterwards). Never blocks project creation.
    topics_created: list[str] | None = None
    try:
        topics_res = await request.app.state.worker_router.coordinator().call_action(
            "provision_project_topics", {"slug": slug}, timeout=10.0,
        )
        topics_created = topics_res.get("created")
    except WorkerError as e:
        log.info("create_project %s: forum-topic provisioning skipped: %s", slug, e)

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
        "topics_created": topics_created,
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
        # T-0156: per-project TG binding so the settings page can pre-fill.
        "tg_chat": proj.tg_chat,
        "tg_topic_id": proj.tg_topic_id,
        "counts": counts,
    }


def _count(path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.glob("*.md"))


def _write_projects_toml(config_dir: Path, projects_raw: dict[str, dict]) -> None:
    """Atomically rewrite projects.toml from a raw project dict."""
    text = _serialize_projects_toml(projects_raw)
    path = config_dir / "projects.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.rename(tmp, path)


async def _reload_worker_projects(request: Request, slug: str) -> None:
    """Nudge the worker to re-read projects.toml (best-effort, like create)."""
    try:
        await request.app.state.worker_router.coordinator().call_action(
            "reload_projects", {}, timeout=5.0,
        )
    except WorkerError as e:
        log.warning("project %s: worker reload_projects failed: %s", slug, e)


@router.put("/{slug}/tg")
async def put_project_tg(
    slug: str,
    request: Request,
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    """T-0156: set a project's Telegram binding — group/DM chat + optional
    forum topic. Body: ``{"tg_chat": "<id>", "tg_topic_id": <int|null>}``.

    ``tg_chat`` accepts a normal DM chat id or a (negative) group/supergroup
    id. ``tg_topic_id`` only makes sense for a forum-enabled group; it is
    rejected without a ``tg_chat``. Both empty/null clears the binding.
    """
    cfg: ApiConfig = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object required")

    chat = str(payload.get("tg_chat", "") or "").strip()
    if chat and not chat.lstrip("-").isdigit():
        raise HTTPException(
            status_code=400, detail="tg_chat must be an integer chat id or empty"
        )

    topic_raw = payload.get("tg_topic_id")
    topic: int | None = None
    if topic_raw not in (None, ""):
        try:
            topic = int(topic_raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="tg_topic_id must be an integer or empty"
            )
        if topic <= 0:
            raise HTTPException(
                status_code=400, detail="tg_topic_id must be a positive integer"
            )
    if topic is not None and not chat:
        raise HTTPException(
            status_code=400, detail="tg_topic_id requires a tg_chat group binding"
        )

    config_dir = cfg.config_dir
    new_raw = _read_projects_toml(config_dir)
    if slug not in new_raw:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    new_raw[slug]["tg_chat"] = chat
    if topic is None:
        new_raw[slug].pop("tg_topic_id", None)
    else:
        new_raw[slug]["tg_topic_id"] = topic

    _write_projects_toml(config_dir, new_raw)
    request.app.state.api_config = ApiConfig.load(config_dir)
    await _reload_worker_projects(request, slug)
    return {"slug": slug, "tg_chat": chat, "tg_topic_id": topic}


@router.post("/{slug}/tg/test")
async def test_project_tg(
    slug: str,
    request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Send a test ping to the project's bound chat (+ topic) via the worker's
    ``tg_notify`` action. Resolution (chat + topic) happens worker-side from
    the slug, so this exercises the exact production send path."""
    cfg: ApiConfig = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    if not (proj.tg_chat or "").strip():
        raise HTTPException(status_code=400, detail="no tg_chat bound for project")

    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action(
            "tg_notify",
            {
                "slug": slug,
                "message": f"bot-squad test ping for project {slug} — binding works",
            },
            timeout=10.0,
        )
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=str(result))
    return {"ok": True, "sent": bool(result.get("sent", True))}


@router.post("/{slug}/deploy", status_code=202)
async def queue_deploy(
    slug: str, request: Request, payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: deploy = privileged
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
    target = str_field(payload, "target")
    reason = str_field(payload, "reason")
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


# T-0572 (Occam pass, D-0046): the /{slug}/clones + /clones/pull-master proxy
# routes (T-0296) and the /{slug}/repo-agents-md GET/PUT pair went with the
# Clones and Workflow pages they served. The worker's clone_status/pull_master
# actions remain (ops CLI territory); AGENTS.md is edited as a file in the repo.
