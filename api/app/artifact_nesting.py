"""Cross-store artifact nesting (T-0283 Pillar-C).

Feedback, use-cases and docs are three separate on-disk stores. The reframe
treats them as one nestable ARTIFACT model: any artifact may carry a
``parent_doc_id`` frontmatter field naming ANY other artifact (doc ``D-NNNN``,
use-case ``UC-NNNN`` or feedback ``F-...``), and parent/child/ancestry edges
resolve ACROSS the three stores. This module is the single place that knows the
store layout + the uniform ``parent_doc_id`` field, so ``routes_docs`` /
``routes_usecases`` / ``routes_feedback`` share one cycle-safe implementation.

Storage is in-place: nothing moves between stores. Feedback is
frontmatter-tolerant — legacy raw-markdown ``F-*.md`` files (no frontmatter)
read as root artifacts and only grow a frontmatter block when first reparented.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)

# kinds, in the order children/listing prefer when surfacing summaries.
KIND_DOC = "doc"
KIND_USE_CASE = "use_case"
KIND_FEEDBACK = "feedback"


@dataclass
class ArtifactRef:
    kind: str
    id: str
    path: Path
    parent_doc_id: str | None
    title: str
    status: str


def _docs_root(project_root: Path) -> Path:
    return project_root / "docs"


def _uc_root(project_root: Path) -> Path:
    return project_root / "use_cases"


def _fb_root(project_root: Path) -> Path:
    return project_root / "feedback"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(meta, body)``; ``({}, text)`` when there's no frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, m.group(2)


def _id_from_stem(stem: str) -> str:
    """``D-0001-some-slug`` -> ``D-0001``; ``UC-0002`` / ``F-x`` -> unchanged."""
    m = re.match(r"^([A-Za-z]+-\d{4})(?:-.*)?$", stem)
    return m.group(1) if m else stem


def _ref_for(kind: str, path: Path) -> ArtifactRef:
    meta, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
    stem = path.stem
    if kind == KIND_FEEDBACK:
        # Feedback has no canonical short id and is keyed by FILENAME everywhere
        # (list/put/promote), so the full stem IS the artifact id. Deriving an
        # `F-NNNN` prefix here breaks dated names (`F-2026-04-15-...`) and makes
        # stored parent refs disagree with the children/parent endpoints.
        art_id = stem
    else:
        art_id = str(meta.get("id") or _id_from_stem(stem))
    parent = meta.get("parent_doc_id")
    parent = str(parent).strip() if parent else None
    title = str(meta.get("title") or art_id)
    status = str(meta.get("status") or "")
    return ArtifactRef(kind=kind, id=art_id, path=path, parent_doc_id=parent,
                       title=title, status=status)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Public: ``(meta, body)``; ``({}, text)`` when there's no frontmatter."""
    return _split_frontmatter(text)


def with_frontmatter(meta: dict, body: str) -> str:
    """Re-emit ``body`` under ``meta``'s frontmatter (empty meta → body as-is).

    Used by feedback's content PUT to preserve the nesting frontmatter across a
    body edit — a body replace must never silently drop ``parent_doc_id`` (the
    same re-graft discipline as the task verbatim guard, T-0289)."""
    if not meta:
        return body
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{fm}---\n\n{body.lstrip(chr(10))}"


def ref_for_path(kind: str, path: Path) -> ArtifactRef:
    """Public: build an :class:`ArtifactRef` for a known-kind artifact file.

    Used by per-store list endpoints that already hold the path and want the
    uniform id/parent_doc_id/title without re-scanning every store.
    """
    return _ref_for(kind, path)


def iter_artifacts(project_root: Path) -> Iterator[ArtifactRef]:
    """Yield every artifact across the three stores."""
    docs = _docs_root(project_root)
    if docs.exists():
        for cdir in sorted(p for p in docs.iterdir() if p.is_dir()):
            for f in sorted(cdir.glob("*.md")):
                yield _ref_for(KIND_DOC, f)
    uc = _uc_root(project_root)
    if uc.exists():
        # Top-level *.md only — a `<uc_id>/` subdir holds flows, not artifacts.
        for f in sorted(uc.glob("*.md")):
            yield _ref_for(KIND_USE_CASE, f)
    fb = _fb_root(project_root)
    if fb.exists():
        for f in sorted(fb.glob("*.md")):
            yield _ref_for(KIND_FEEDBACK, f)


def find_artifact(project_root: Path, artifact_id: str) -> ArtifactRef | None:
    for ref in iter_artifacts(project_root):
        if ref.id == artifact_id:
            return ref
    return None


def parent_of(project_root: Path, artifact_id: str) -> str | None:
    ref = find_artifact(project_root, artifact_id)
    return ref.parent_doc_id if ref else None


def _summary(ref: ArtifactRef) -> dict:
    return {
        "id": ref.id,
        "title": ref.title,
        "kind": ref.kind,
        "status": ref.status,
        "parent_doc_id": ref.parent_doc_id,
    }


def children_of(project_root: Path, artifact_id: str) -> list[dict]:
    """Cross-store: every artifact whose ``parent_doc_id`` points at ``artifact_id``."""
    return [
        _summary(ref)
        for ref in iter_artifacts(project_root)
        if ref.parent_doc_id == artifact_id
    ]


def would_cycle(project_root: Path, artifact_id: str, new_parent: str) -> bool:
    """True if making ``new_parent`` the parent of ``artifact_id`` closes a cycle.

    Walks the ancestry chain up from ``new_parent`` across all stores; a
    visited-set guards against any pre-existing loop so the walk terminates.
    """
    if new_parent == artifact_id:
        return True
    seen: set[str] = set()
    cur: str | None = new_parent
    while cur and cur not in seen:
        if cur == artifact_id:
            return True
        seen.add(cur)
        cur = parent_of(project_root, cur)
    return False


def set_parent(ref: ArtifactRef, new_parent: str | None) -> None:
    """Set (or clear, on ``None``) ``ref``'s ``parent_doc_id``, preserving body.

    Tolerant of a legacy frontmatter-less feedback file: a frontmatter block is
    synthesised (carrying ``id`` + the new parent) above the existing body.
    """
    text = ref.path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    had_frontmatter = bool(meta)
    if not had_frontmatter:
        # Legacy artifact: seed a minimal frontmatter so the field has a home.
        meta = {"id": ref.id}
    if new_parent is None:
        meta.pop("parent_doc_id", None)
    else:
        meta["parent_doc_id"] = new_parent
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    out = f"---\n{fm}---\n\n{body.lstrip(chr(10))}"
    tmp = ref.path.with_suffix(ref.path.suffix + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, ref.path)
