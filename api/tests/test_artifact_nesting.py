"""Cross-store artifact nesting (T-0283 Pillar-C).

The shared module resolves parent/child/ancestry edges across the artifact
stores — docs (``docs/<cat>/D-*.md``) and feedback (``feedback/F-*.md``) —
via a uniform ``parent_doc_id`` frontmatter field. Feedback is
frontmatter-tolerant: legacy raw-markdown files with no frontmatter read as
root artifacts.
"""
from __future__ import annotations

from pathlib import Path

from app import artifact_nesting as AN


def _doc(root: Path, doc_id: str, *, category="design", parent=None, title="Doc"):
    d = root / "docs" / category
    d.mkdir(parents=True, exist_ok=True)
    fm = [f"id: {doc_id}", f"title: {title}", f"category: {category}", "status: draft"]
    if parent is not None:
        fm.append(f"parent_doc_id: {parent}")
    (d / f"{doc_id}-x.md").write_text("---\n" + "\n".join(fm) + "\n---\n\nbody\n")


def _fb(root: Path, name: str, *, parent=None, legacy=False):
    d = root / "feedback"
    d.mkdir(parents=True, exist_ok=True)
    if legacy:
        (d / f"{name}.md").write_text(f"# {name}\n\nraw feedback, no frontmatter\n")
        return
    fm = [f"id: {name}", f"title: {name}"]
    if parent is not None:
        fm.append(f"parent_doc_id: {parent}")
    (d / f"{name}.md").write_text("---\n" + "\n".join(fm) + "\n---\n\n# fb\n")


def test_find_artifact_across_stores(tmp_path: Path):
    _doc(tmp_path, "D-0001")
    _fb(tmp_path, "F-a")
    assert AN.find_artifact(tmp_path, "D-0001").kind == "doc"
    assert AN.find_artifact(tmp_path, "F-a").kind == "feedback"
    assert AN.find_artifact(tmp_path, "D-9999") is None


def test_legacy_feedback_reads_as_root(tmp_path: Path):
    _fb(tmp_path, "F-legacy", legacy=True)
    ref = AN.find_artifact(tmp_path, "F-legacy")
    assert ref is not None and ref.kind == "feedback"
    assert ref.parent_doc_id is None  # no frontmatter → root


def test_children_of_is_cross_store(tmp_path: Path):
    # A doc mother with a doc child AND a feedback child (cross-store).
    _doc(tmp_path, "D-0001")
    _doc(tmp_path, "D-0002", parent="D-0001")
    _fb(tmp_path, "F-evid", parent="D-0001")
    _doc(tmp_path, "D-0003")  # unrelated root
    kids = AN.children_of(tmp_path, "D-0001")
    got = {(c["id"], c["kind"]) for c in kids}
    assert got == {("D-0002", "doc"), ("F-evid", "feedback")}


def test_parent_of_reads_field(tmp_path: Path):
    _doc(tmp_path, "D-0001")
    _fb(tmp_path, "F-a", parent="D-0001")
    assert AN.parent_of(tmp_path, "F-a") == "D-0001"
    assert AN.parent_of(tmp_path, "D-0001") is None


def test_would_cycle_self_parent(tmp_path: Path):
    _doc(tmp_path, "D-0001")
    assert AN.would_cycle(tmp_path, "D-0001", "D-0001") is True


def test_would_cycle_cross_store_ancestry(tmp_path: Path):
    # D-0001 -> F-mid -> F-c (chain via parent_doc_id). Making F-c's parent
    # D-0001 would close a cross-store cycle.
    _doc(tmp_path, "D-0001", parent="F-mid")
    _fb(tmp_path, "F-mid", parent="F-c")
    _fb(tmp_path, "F-c")
    assert AN.would_cycle(tmp_path, "F-c", "D-0001") is True
    # An unrelated new parent does not cycle.
    _doc(tmp_path, "D-0009")
    assert AN.would_cycle(tmp_path, "F-c", "D-0009") is False


def test_set_parent_writes_and_clears(tmp_path: Path):
    _fb(tmp_path, "F-a")
    ref = AN.find_artifact(tmp_path, "F-a")
    AN.set_parent(ref, "D-0007")
    assert AN.parent_of(tmp_path, "F-a") == "D-0007"
    AN.set_parent(AN.find_artifact(tmp_path, "F-a"), None)
    assert AN.parent_of(tmp_path, "F-a") is None


def test_set_parent_on_legacy_feedback_adds_frontmatter(tmp_path: Path):
    _fb(tmp_path, "F-legacy", legacy=True)
    ref = AN.find_artifact(tmp_path, "F-legacy")
    AN.set_parent(ref, "D-0001")
    assert AN.parent_of(tmp_path, "F-legacy") == "D-0001"
    # body preserved
    body = (tmp_path / "feedback" / "F-legacy.md").read_text()
    assert "raw feedback, no frontmatter" in body
