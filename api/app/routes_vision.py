"""Vision read + write endpoints."""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.markdown_writer import slugify
from app.project_authz import require_project_member
from app.routes_auth import require_auth

router = APIRouter(
    prefix="/projects/{slug}/vision",
    tags=["vision"],
    dependencies=[Depends(require_auth)],
)

_MAX_CONTENT_BYTES = 200 * 1024  # 200 KB

# Valid names: top-level <name>.md, initiatives/<name>.md, or roles/<name>.md.
_VISION_NAME_RE = re.compile(
    r"^(?:[A-Za-z0-9_.-]+\.md"
    r"|initiatives/[A-Za-z0-9_.-]+\.md"
    r"|roles/[A-Za-z0-9_.-]+\.md)$"
)

# AGENT_INSTRUCTIONS.md is a sibling of vision/ (under data/<slug>/) — it's
# surfaced through this listing so the Workflow page can render it alongside
# the vision docs without a separate endpoint.
_AGENT_INSTRUCTIONS_FILENAME = "AGENT_INSTRUCTIONS.md"


def _vision_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "vision"


def _validate_vision_name(name: str) -> None:
    if not _VISION_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid vision file name: {name!r}")


def _validate_content(content: str) -> None:
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail="content must be valid UTF-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")


def _read_active_initiatives(vision_dir: Path) -> set[str]:
    """Return the set of active initiative basenames.

    Source of truth: ``vision/active_initiatives`` (one basename per line).
    Legacy fallback: if that file is missing but ``vision/active_initiative``
    (singular) exists with a non-empty value, seed the set with it. The
    legacy file is not written back to; the next activate/deactivate call
    writes the new file and the legacy becomes stale.
    """
    plural = vision_dir / "active_initiatives"
    if plural.exists():
        out: set[str] = set()
        for line in plural.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("initiatives/"):
                line = line[len("initiatives/"):]
            out.add(line)
        return out
    legacy = vision_dir / "active_initiative"
    if legacy.exists():
        raw = legacy.read_text().strip()
        if raw.startswith("initiatives/"):
            raw = raw[len("initiatives/"):]
        return {raw} if raw else set()
    return set()


def _write_active_initiatives(vision_dir: Path, names: set[str]) -> None:
    """Atomic write of the active-initiative set."""
    plural = vision_dir / "active_initiatives"
    plural.parent.mkdir(parents=True, exist_ok=True)
    sorted_names = sorted(names)
    payload = "\n".join(sorted_names) + ("\n" if sorted_names else "")
    tmp = plural.parent / (plural.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.rename(tmp, plural)


def _read_finished_initiatives(vision_dir: Path) -> set[str]:
    """Return basenames of initiatives marked finished (shipped, archived)."""
    p = vision_dir / "finished_initiatives"
    if not p.exists():
        return set()
    out: set[str] = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("initiatives/"):
            line = line[len("initiatives/"):]
        out.add(line)
    return out


def _write_finished_initiatives(vision_dir: Path, names: set[str]) -> None:
    p = vision_dir / "finished_initiatives"
    p.parent.mkdir(parents=True, exist_ok=True)
    sorted_names = sorted(names)
    payload = "\n".join(sorted_names) + ("\n" if sorted_names else "")
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.rename(tmp, p)


def _initiative_entries_from_tasks(project_data_dir: Path) -> list[dict]:
    """T-0480 Phase-3a: kind:initiative TASKS are the initiatives now. Surface
    each as a vision-list initiative entry (keyed by its legacy basename stem,
    from ``aka``, so existing web links/keys keep working). active/finished are
    derived from the task status — replacing the active_/finished_initiatives
    sidecars. This is what lets the Vision view survive once the legacy
    vision/initiatives/ files are archived (3b)."""
    from app.markdown_parser import ParseError, parse_task

    backlog = project_data_dir / "backlog"
    out: list[dict] = []
    if not backlog.is_dir():
        return out
    for f in sorted(backlog.glob("T-*.md")):
        try:
            t = parse_task(f)
        except (ParseError, OSError):
            continue
        if t.get("kind") != "initiative":
            continue
        aka = t.get("aka") or []
        if isinstance(aka, str):
            aka = [aka]
        # legacy basename stem = the filename-stem alias (last aka), else the id
        stem = aka[-1] if aka else t.get("id")
        status = t.get("status")
        out.append({
            "name": f"initiatives/{stem}.md",
            "content": t.get("body", ""),
            "active": status == "in_progress",
            "finished": status == "closed",
            "task_id": t.get("id"),
            "kind": "initiative",
        })
    return out


@router.get("")
def list_vision(slug: str, request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    project_data_dir = cfg.project_data_dir(slug)
    vision_dir = project_data_dir / "vision"
    out: list[dict] = []
    # Surface AGENT_INSTRUCTIONS.md (sibling of vision/, injected at session
    # start) so the Workflow page can render+edit it without a new endpoint.
    agent_instructions = project_data_dir / _AGENT_INSTRUCTIONS_FILENAME
    if agent_instructions.exists():
        out.append({
            "name": _AGENT_INSTRUCTIONS_FILENAME,
            "content": agent_instructions.read_text(),
            "active": False,
        })
    # T-0480 Phase-3a: kind:initiative tasks ARE the initiatives now — surface
    # them first, then dedup the legacy vision/ files against them (during the
    # transition both exist; after 3b archival only the tasks remain).
    init_entries = _initiative_entries_from_tasks(project_data_dir)
    out.extend(init_entries)
    task_stems = {
        e["name"][len("initiatives/"):-len(".md")]
        for e in init_entries
        if e["name"].startswith("initiatives/") and e["name"].endswith(".md")
    }
    if not vision_dir.exists():
        return out
    # Top-level *.md (product.md, constitution.md). Skip _archive/ entirely.
    # Dedup: a root-located initiative (e.g. INI-04) already surfaced via its task.
    for f in sorted(vision_dir.glob("*.md")):
        if f.stem in task_stems:
            continue
        out.append({"name": f.name, "content": f.read_text(), "active": False})
    active_set = _read_active_initiatives(vision_dir)
    finished_set = _read_finished_initiatives(vision_dir)
    initiatives_dir = vision_dir / "initiatives"
    if initiatives_dir.exists():
        for f in sorted(initiatives_dir.glob("*.md")):
            if f.stem in task_stems:
                continue  # already surfaced via its kind:initiative task (dedup)
            out.append({
                "name": f"initiatives/{f.name}",
                "content": f.read_text(),
                "active": f.name in active_set,
                "finished": f.name in finished_set,
            })
    roles_dir = vision_dir / "roles"
    if roles_dir.exists():
        for f in sorted(roles_dir.glob("*.md")):
            out.append({
                "name": f"roles/{f.name}",
                "content": f.read_text(),
                "active": False,
            })
    return out


def _normalize_initiative_basename(name: str) -> str:
    name = (name or "").strip()
    if name.startswith("initiatives/"):
        name = name[len("initiatives/"):]
    if not name or "/" in name or ".." in name or not name.endswith(".md"):
        raise HTTPException(status_code=400, detail=f"invalid initiative name: {name!r}")
    return name


def _find_initiative_task(request: Request, slug: str, name: str):
    """T-0480 Phase-3b-1: resolve an initiative basename to its kind:initiative
    backlog task file (so writers act on the TASK, surviving 3b-2 archival), or
    None if it isn't a migrated/created initiative. Tries the alias index first,
    then falls back to an aka scan (covers tasks not yet in the index, e.g.
    UI-created initiatives)."""
    from app.initiative_resolver import normalize_ref, resolve_initiative_ref
    from app.markdown_parser import ParseError, parse_task

    cfg = request.app.state.api_config
    backlog = cfg.project_data_dir(slug) / "backlog"
    tid = resolve_initiative_ref(cfg.data_dir, slug, name)
    if tid:
        for f in sorted(backlog.glob(f"{tid}-*.md")):
            return f
        direct = backlog / f"{tid}.md"
        if direct.exists():
            return direct
    want = normalize_ref(name)
    if backlog.is_dir():
        for f in sorted(backlog.glob("T-*.md")):
            try:
                t = parse_task(f)
            except (ParseError, OSError):
                continue
            if t.get("kind") != "initiative":
                continue
            aka = t.get("aka") or []
            if isinstance(aka, str):
                aka = [aka]
            if want in {normalize_ref(a) for a in aka}:
                return f
    return None


def _set_initiative_status(task_path, status: str) -> None:
    from app.markdown_writer import merge_task_update
    merge_task_update(task_path, {"status": status})


@router.post("/active_initiatives/{name}")
def activate_initiative(
    slug: str,
    name: str,
    request: Request,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Mark an initiative active. T-0480 3b-1: active == task status in_progress."""
    name = _normalize_initiative_basename(name)
    task = _find_initiative_task(request, slug, name)
    if task is not None:
        _set_initiative_status(task, "in_progress")
        return {"ok": True, "active": [name]}
    # legacy fallback: a non-migrated dir initiative still uses the sidecar
    vision_dir = _vision_dir(request, slug)
    target = vision_dir / "initiatives" / name
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"initiative not found: {name}")
    active = _read_active_initiatives(vision_dir)
    active.add(name)
    _write_active_initiatives(vision_dir, active)
    return {"ok": True, "active": sorted(active)}


@router.delete("/active_initiatives/{name}")
def deactivate_initiative(
    slug: str,
    name: str,
    request: Request,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Deactivate an initiative. T-0480 3b-1: active→open on the task."""
    name = _normalize_initiative_basename(name)
    task = _find_initiative_task(request, slug, name)
    if task is not None:
        _set_initiative_status(task, "open")
        return {"ok": True, "active": []}
    vision_dir = _vision_dir(request, slug)
    active = _read_active_initiatives(vision_dir)
    active.discard(name)
    _write_active_initiatives(vision_dir, active)
    return {"ok": True, "active": sorted(active)}


@router.post("/finished_initiatives/{name}")
def mark_initiative_finished(
    slug: str,
    name: str,
    request: Request,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Mark an initiative finished. T-0480 3b-1: finished == task status closed."""
    name = _normalize_initiative_basename(name)
    task = _find_initiative_task(request, slug, name)
    if task is not None:
        _set_initiative_status(task, "closed")
        return {"ok": True, "finished": [name], "active": []}
    vision_dir = _vision_dir(request, slug)
    target = vision_dir / "initiatives" / name
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"initiative not found: {name}")
    active = _read_active_initiatives(vision_dir)
    if name in active:
        active.discard(name)
        _write_active_initiatives(vision_dir, active)
    finished = _read_finished_initiatives(vision_dir)
    finished.add(name)
    _write_finished_initiatives(vision_dir, finished)
    return {"ok": True, "finished": sorted(finished), "active": sorted(active)}


@router.delete("/finished_initiatives/{name}")
def unmark_initiative_finished(
    slug: str,
    name: str,
    request: Request,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Reopen a finished initiative (back to 'open'). T-0480 3b-1: closed→open."""
    name = _normalize_initiative_basename(name)
    task = _find_initiative_task(request, slug, name)
    if task is not None:
        _set_initiative_status(task, "open")
        return {"ok": True, "finished": []}
    vision_dir = _vision_dir(request, slug)
    finished = _read_finished_initiatives(vision_dir)
    finished.discard(name)
    _write_finished_initiatives(vision_dir, finished)
    return {"ok": True, "finished": sorted(finished)}


@router.put("/active_initiative")
def put_active_initiative(
    slug: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Back-compat: replace the active set with a single value (or clear).

    Prefer the new POST/DELETE /active_initiatives/{name} endpoints.
    """
    name = (payload.get("name") or "").strip()
    vision_dir = _vision_dir(request, slug)
    if not name:
        _write_active_initiatives(vision_dir, set())
        return {"ok": True, "active": []}
    name = _normalize_initiative_basename(name)
    task = _find_initiative_task(request, slug, name)
    if task is not None:
        _set_initiative_status(task, "in_progress")
        return {"ok": True, "active": [name]}
    target = vision_dir / "initiatives" / name
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"initiative not found: {name}")
    _write_active_initiatives(vision_dir, {name})
    return {"ok": True, "active": [name]}


@router.put("/{name:path}")
def put_vision(
    slug: str,
    name: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    _validate_vision_name(name)
    content = payload.get("content") or ""
    _validate_content(content)

    # T-0480 3b-1: editing an initiative's content writes its TASK body, not the
    # legacy file (so it survives 3b-2 archival).
    if name.startswith("initiatives/"):
        task = _find_initiative_task(request, slug, name[len("initiatives/"):])
        if task is not None:
            from app.markdown_writer import merge_task_update
            merge_task_update(task, {}, body=content)
            return {"ok": True}

    vision_dir = _vision_dir(request, slug)
    if name == _AGENT_INSTRUCTIONS_FILENAME:
        path = vision_dir.parent / _AGENT_INSTRUCTIONS_FILENAME
    else:
        path = vision_dir / name

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"vision file not found: {name}")

    # Atomic write
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)

    return {"ok": True}


@router.post("")
def post_vision(
    slug: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    kind = payload.get("kind")
    if kind != "initiative":
        raise HTTPException(status_code=400, detail="only kind=initiative is supported")

    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be empty")

    content = payload.get("content")
    if content is None:
        content = f"# {name}\n"

    _validate_content(content)

    # T-0480 3b-1: a new initiative is a kind:initiative TASK, not a vision file.
    from datetime import datetime, timezone

    from app import idalloc
    from app.markdown_writer import write_task

    cfg = request.app.state.api_config
    stem = slugify(name)
    tid = idalloc.allocate_id(cfg.data_dir, slug, "task")
    backlog = cfg.project_data_dir(slug) / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = {
        "id": tid,
        "title": name,
        "status": "open",
        "kind": "initiative",
        "priority": 0,
        "created": now,
        # a human creating an initiative in the UI IS the stakeholder directing it
        "provenance": f"stakeholder:{now[:10]}",
        "aka": [stem],
        "owner": user.get("username") or None,
    }
    path = backlog / f"{tid}-{stem}.md"
    write_task(path, fm, content)
    return {"ok": True, "task_id": tid, "name": f"initiatives/{stem}.md"}
