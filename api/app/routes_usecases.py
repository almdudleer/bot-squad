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
