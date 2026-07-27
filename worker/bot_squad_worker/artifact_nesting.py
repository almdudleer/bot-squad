"""Cross-store artifact nesting (T-0283 Pillar-C).

Feedback and docs are separate on-disk stores. The reframe treats them as one
nestable ARTIFACT model: any artifact may carry a ``parent_doc_id``
frontmatter field naming ANY other artifact (doc ``D-NNNN`` or feedback
``F-...``), and parent/child/ancestry edges resolve ACROSS the stores. This
module is the single place that knows the store layout + the uniform
``parent_doc_id`` field, so ``routes_docs`` / ``routes_feedback`` share one
cycle-safe implementation.

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


def id_from_stem(stem: str) -> str:
    """``D-0001-some-slug`` -> ``D-0001``; ``UC-0002`` / ``F-x`` -> unchanged.

    T-0751 made this THE derivation for docs too. ``routes_docs`` used to carry
    its own copy (``stem.split("-", 2)[:2]``) which assumed every filename is
    ``D-NNNN-<slug>``, so ``roadmap/03-docs-artifacts.md`` listed as ``03-docs``
    — an id no endpoint could resolve. A stem that is not an allocated id IS
    its own id; that is what makes the list and the detail endpoint agree.
    """
    m = re.match(r"^([A-Za-z]+-\d{4,})(?:-.*)?$", stem)  # T-0371: ids cross 9999
    return m.group(1) if m else stem


#: Longest artifact id we will consider — a filename component's practical cap.
_MAX_ARTIFACT_ID_BYTES = 255


def is_valid_artifact_id(artifact_id: str) -> bool:
    """The ONE shape gate for a caller-supplied artifact id (T-0751).

    Deliberately NOT a charset whitelist. Ids are DERIVED from filenames by
    :func:`id_from_stem`, so any charset narrower than "what a filename may
    hold" re-creates the bug this replaced: a doc the list endpoint advertises
    and the detail endpoint then 400s. What is excluded is only what can never
    BE a single filename component — a path separator, a NUL, the two dot
    entries, empty, or longer than a name a filesystem will store.

    This is defence in depth, not the traversal barrier: :func:`find_doc` never
    interpolates an id into a path, it compares against ids derived from files
    it enumerated itself.
    """
    if not artifact_id or artifact_id in {".", ".."}:
        return False
    if "/" in artifact_id or "\\" in artifact_id or "\x00" in artifact_id:
        return False
    return len(artifact_id.encode("utf-8")) <= _MAX_ARTIFACT_ID_BYTES


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
        art_id = str(meta.get("id") or id_from_stem(stem))
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


def iter_docs(project_root: Path) -> Iterator[ArtifactRef]:
    """Yield every DOC artifact, in the storage layout's own order.

    One level of category dirs, exactly what ``routes_docs.list_docs`` walks —
    so "listed" and "resolvable" cover the same set of files by construction.
    """
    docs = _docs_root(project_root)
    if not docs.exists():
        return
    for cdir in sorted(p for p in docs.iterdir() if p.is_dir()):
        for f in sorted(cdir.glob("*.md")):
            yield _ref_for(KIND_DOC, f)


def find_doc(project_root: Path, doc_id: str) -> ArtifactRef | None:
    """Resolve a doc id to its file WITHOUT putting the id in a path (T-0751).

    The old resolver globbed ``docs/*/{doc_id}-*.md`` — a caller-controlled
    string interpolated into a path pattern, with a ``^D-\\d{4,}$`` regex as the
    only thing between it and traversal. That same regex is what made 19 listed
    docs unopenable. Enumerating the tree and comparing DERIVED ids drops both
    problems at once: the id shape may widen to whatever filenames exist, and no
    caller string ever reaches the filesystem.
    """
    if not is_valid_artifact_id(doc_id):
        return None
    for ref in iter_docs(project_root):
        if ref.id == doc_id:
            return ref
    return None


def iter_artifacts(project_root: Path) -> Iterator[ArtifactRef]:
    """Yield every artifact across the stores."""
    yield from iter_docs(project_root)
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
