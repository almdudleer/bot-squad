"""Use cases / user flows (T-0159) — read, edit, and launch testing runs.

Storage: ``data/<slug>/use_cases/<id>.md`` — YAML frontmatter
(user_persona, goal, preconditions, success_criteria, related_tickets, status)
plus markdown body (``## Steps`` + ``## Feedback``). The project page surfaces
these in a "Use Cases" tab (list / view / edit / run); the run action spawns a
testing-dev session pre-briefed on the flow (mirrors ``bsq usecase run``).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.routes_auth import require_auth
from app.worker_client import WorkerError

router = APIRouter(
    prefix="/projects/{slug}/use_cases",
    tags=["use_cases"],
    dependencies=[Depends(require_auth)],
)

_MAX_CONTENT_BYTES = 200 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def _uc_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "use_cases"


def _validate_id(uc_id: str) -> None:
    if not _ID_RE.match(uc_id):
        raise HTTPException(status_code=400, detail=f"invalid use-case id: {uc_id!r}")


def _parse(path: Path) -> dict:
    """Return ``{id, meta..., body, raw}`` for a use-case md (tolerant of no FM)."""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    meta: dict = {}
    body = text
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        body = m.group(2).lstrip("\n")
    return {**meta, "id": path.stem, "body": body, "raw": text}


@router.get("")
def list_use_cases(slug: str, request: Request) -> list[dict]:
    d = _uc_dir(request, slug)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        uc = _parse(f)
        out.append({
            "id": uc["id"],
            "title": uc.get("title", uc["id"]),
            "status": uc.get("status", ""),
            "user_persona": uc.get("user_persona", ""),
            "goal": uc.get("goal", ""),
        })
    return out


@router.get("/{uc_id}")
def get_use_case(slug: str, uc_id: str, request: Request) -> dict:
    _validate_id(uc_id)
    path = _uc_dir(request, slug) / f"{uc_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"use case not found: {uc_id}")
    return _parse(path)


class PutUseCase(BaseModel):
    content: str


class NewUseCase(BaseModel):
    title: str


@router.post("")
def create_use_case(slug: str, request: Request, body: NewUseCase,
                    user: dict = Depends(require_auth)) -> dict:
    """T-0174: allocate a UC-NNNN id atomically and write a stub use case.

    Replaces hand-typing ``id:`` into the frontmatter (the old flow). The
    filename stem IS the id — what every other route here keys on — so legacy
    slug-named UCs (``UC-<slug>.md``) coexist with the new numeric ones.
    """
    import json

    from app import idalloc

    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")

    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    uc_id = idalloc.allocate_id(cfg.data_dir, slug, "uc")

    d = _uc_dir(request, slug)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uc_id}.md"
    fm = "\n".join([
        f"id: {uc_id}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "user_persona: TBD",
        "goal: TBD",
        "preconditions: TBD",
        "success_criteria: TBD",
        "related_tickets: []",
        "status: draft",
    ])
    content = f"---\n{fm}\n---\n\n# {title}\n\n## Steps\n\n1. TBD\n\n## Feedback\n\n"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": uc_id}


@router.put("/{uc_id}")
def put_use_case(slug: str, uc_id: str, request: Request, body: PutUseCase,
                 user: dict = Depends(require_auth)) -> dict:
    _validate_id(uc_id)
    content = body.content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="content must not be empty")
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")

    d = _uc_dir(request, slug)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uc_id}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": uc_id}


@router.delete("/{uc_id}")
def delete_use_case(slug: str, uc_id: str, request: Request,
                    user: dict = Depends(require_auth)) -> dict:
    """Delete a use case (T-0276): remove the ``.md`` and cascade its flows.

    A use case OWNS its user-flows (stored under ``<uc_id>/flows/``); they
    can't exist without it, so the whole ``<uc_id>/`` subtree is removed with
    the file. ``uc_id`` is regex-validated (no slashes, can't start with a dot)
    so the ``root / uc_id`` rmtree can't traverse out of the use-cases dir. The
    UC-NNNN id is tombstoned (monotonic allocator, never reissued), matching
    the docs delete contract.
    """
    _validate_id(uc_id)
    root = _uc_dir(request, slug)
    path = root / f"{uc_id}.md"
    owned_dir = root / uc_id
    if not path.exists() and not owned_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"use case not found: {uc_id}")
    if path.exists():
        path.unlink()
    if owned_dir.is_dir():
        shutil.rmtree(owned_dir)
    return {"ok": True, "id": uc_id, "deleted": True}


def _section(body: str, heading: str) -> str:
    m = re.search(rf"^{re.escape(heading)}\s*\n(.*?)(?=^\#\# |\Z)", body,
                  re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _compose_run_brief(slug: str, uc: dict) -> str:
    steps = _section(uc["body"], "## Steps") or "(no steps recorded — see the use case)"
    feedback = _section(uc["body"], "## Feedback")
    fb_block = (
        f"\n\nPRIOR FEEDBACK on this use case — verify each is addressed, flag "
        f"regressions:\n{feedback}\n" if feedback else ""
    )
    return (
        f"You are a TESTING-DEV session running use case {uc['id']} for project "
        f"'{slug}'. Walk this user flow MANUALLY (playwright MCP) on the real "
        f"app, capture evidence per step, and report a PASS/FAIL verdict — do "
        f"NOT change code.\n\n"
        f"== USE CASE: {uc.get('title', uc['id'])} ({uc['id']}) ==\n"
        f"- Persona:          {uc.get('user_persona', '(unspecified)')}\n"
        f"- Goal:             {uc.get('goal', '(unspecified)')}\n"
        f"- Preconditions:    {uc.get('preconditions', '(unspecified)')}\n"
        f"- Success criteria: {uc.get('success_criteria', '(unspecified)')}\n"
        f"- Related tickets:  {uc.get('related_tickets', '(none)')}\n\n"
        f"Steps to walk:\n{steps}{fb_block}\n\n"
        f"== HOW TO TEST (manual-first, T-0158) ==\n"
        f"1. Narrate the flow + confirm preconditions (target = staging.botsquad.dev).\n"
        f"2. Drive each step with playwright MCP; screenshot + note expected-vs-actual per step.\n"
        f"3. PASS only if every step matches AND the success criterion is observably met.\n"
        f"4. Triage each failure: REGRESSION (contradicts a related-ticket DoD / matches old "
        f"feedback) vs NEW ISSUE. File a backlog ticket via task_new for each.\n"
        f"5. Submit process friction with `bsq feedback submit --usecase {uc['id']} '<note>'`.\n"
        f"6. Report a one-line PASS/FAIL summary to your TL via `bsq peer send`.\n\n"
        f"FIRST read AGENT_INSTRUCTIONS.md for the staging JWT/playwright recipe. "
        f"Track work on tickets, not superpowers docs. `bsq --help` for verbs."
    )


@router.post("/{uc_id}/run")
async def run_use_case(slug: str, uc_id: str, request: Request,
                       user: dict = Depends(require_auth)) -> dict:
    """Spawn a testing-dev session pre-briefed on this use case."""
    _validate_id(uc_id)
    path = _uc_dir(request, slug) / f"{uc_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"use case not found: {uc_id}")
    uc = _parse(path)
    brief = _compose_run_brief(slug, uc)
    window = f"uctest-{uc_id}"[:40]

    wrouter = request.app.state.worker_router
    client = wrouter.for_user(user["linux_user"])
    params = {
        "slug": slug,
        "window": window,
        "owner": user["username"],
        "initial_prompt": brief,
    }
    try:
        res = await client.call_action("spawn_session", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "id": uc_id, "window": window, "sid": res.get("sid")}


# --------------------------------------------------------------------------
# User flows attached to a use case (T-0173).
#
# Storage: ``data/<slug>/use_cases/<uc-id>/flows/UF-NNNN-<slug>.md`` — frontmatter
# (id, uc_id, title, status, created) + a markdown body that carries a numbered
# ``## Steps`` list and a ``## Mermaid`` fenced block. The UC page renders the
# mermaid client-side and hands agents both the prose and the diagram so they
# can walk the flow manually (agent-manual-first, T-0158) before automating.
#
# The in-system node/branch model (DoD "optional v2") is DEFERRED — markdown +
# mermaid is the v1 surface; follow-up ticket tracks the structured editor.
# --------------------------------------------------------------------------
import json  # noqa: E402

# T-0180: flows use the "UF-" (user-flow) prefix, deliberately distinct from
# curated feedback's "F-" (feedback/F-NNNN-*.md) so a bare id is never ambiguous.
_FLOW_ID_RE = re.compile(r"^UF-\d{4}$")


def _slugify(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s).lower().strip()
    s = re.sub(r"[\s-]+", "-", s).strip("-")
    return s[:max_len]


def _flows_dir(request: Request, slug: str, uc_id: str) -> Path:
    return _uc_dir(request, slug) / uc_id / "flows"


def _validate_flow_id(flow_id: str) -> None:
    if not _FLOW_ID_RE.match(flow_id):
        raise HTTPException(status_code=400, detail=f"invalid flow id: {flow_id!r}")


def _uc_exists(request: Request, slug: str, uc_id: str) -> bool:
    root = _uc_dir(request, slug)
    return (root / f"{uc_id}.md").exists() or (root / uc_id).is_dir()


def _find_flow(request: Request, slug: str, uc_id: str, flow_id: str) -> Optional[Path]:
    d = _flows_dir(request, slug, uc_id)
    if not d.exists():
        return None
    for f in d.glob(f"{flow_id}-*.md"):
        return f
    for f in d.glob(f"{flow_id}.md"):
        return f
    return None


@router.get("/{uc_id}/flows")
def list_flows(slug: str, uc_id: str, request: Request) -> list[dict]:
    _validate_id(uc_id)
    d = _flows_dir(request, slug, uc_id)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        fl = _parse(f)
        # `_parse` sets id = path.stem (right for UCs, where the stem IS the id),
        # but a flow's stem is `UF-NNNN-<slug>` — derive the canonical UF-NNNN.
        parts = f.stem.split("-", 2)
        fid = "-".join(parts[:2]) if len(parts) >= 2 else f.stem
        out.append({
            "id": fid,
            "uc_id": uc_id,
            "title": fl.get("title", fid),
            "status": fl.get("status", ""),
        })
    return out


@router.get("/{uc_id}/flows/{flow_id}")
def get_flow(slug: str, uc_id: str, flow_id: str, request: Request) -> dict:
    _validate_id(uc_id)
    _validate_flow_id(flow_id)
    path = _find_flow(request, slug, uc_id, flow_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"flow not found: {flow_id}")
    fl = _parse(path)
    # `_parse` set id = full stem (`UF-NNNN-<slug>`); force the canonical UF-NNNN
    # so the round-trip id matches what list_flows / the validator expect.
    fl["id"] = flow_id
    fl["uc_id"] = uc_id
    return fl


class NewFlow(BaseModel):
    title: str


@router.post("/{uc_id}/flows")
def create_flow(slug: str, uc_id: str, request: Request, body: NewFlow,
                user: dict = Depends(require_auth)) -> dict:
    """Allocate a UF-NNNN id atomically and write a stub flow under a use case.

    Mirrors the worker ``flow_new`` action's storage + frontmatter + mermaid
    stub so a web create and an agent ``bsq flow new`` are byte-compatible.
    """
    from app import idalloc

    _validate_id(uc_id)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")

    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    if not _uc_exists(request, slug, uc_id):
        raise HTTPException(status_code=404, detail=f"use case not found: {uc_id}")

    d = _flows_dir(request, slug, uc_id)
    d.mkdir(parents=True, exist_ok=True)
    flow_id = idalloc.allocate_id(cfg.data_dir, slug, "flow")
    path = d / f"{flow_id}-{_slugify(title)}.md"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = "\n".join([
        f"id: {flow_id}",
        f"uc_id: {uc_id}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "status: draft",
        f"created: {now}",
    ])
    body_md = (
        f"# {title}\n\n## Steps\n\n1. TBD\n\n## Mermaid\n\n"
        "```mermaid\ngraph TD\n  A[start] --> B[TBD]\n```\n\n"
        "(new user flow — T-0173)\n"
    )
    content = f"---\n{fm}\n---\n\n{body_md}"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": flow_id, "uc_id": uc_id}


@router.put("/{uc_id}/flows/{flow_id}")
def put_flow(slug: str, uc_id: str, flow_id: str, request: Request, body: PutUseCase,
             user: dict = Depends(require_auth)) -> dict:
    _validate_id(uc_id)
    _validate_flow_id(flow_id)
    content = body.content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="content must not be empty")
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")
    path = _find_flow(request, slug, uc_id, flow_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"flow not found: {flow_id}")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": flow_id, "uc_id": uc_id}
