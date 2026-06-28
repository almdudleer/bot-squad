"""T-0480: tests for migrate_initiatives_to_tasks.py.

Initiatives (vision/initiatives/*.md, mixed frontmatter / no-frontmatter shapes)
become tasks marked `kind: initiative`. Old refs keep resolving via an alias
index. Migration is dry-run by default, idempotent, reversible.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from migrate_initiatives_to_tasks import (
    build_plan,
    derive_initiative,
    derive_status,
    normalize_ref,
    run,
)

SLUG = "bot-squad"


# --- fixture builders -------------------------------------------------------

def _mk(data_dir: Path, *, initiatives: dict, active=None, finished=None,
        tasks: dict | None = None, counter: int = 10) -> Path:
    root = data_dir / SLUG
    (root / "vision" / "initiatives").mkdir(parents=True, exist_ok=True)
    (root / "backlog").mkdir(parents=True, exist_ok=True)
    (root / "_counters").mkdir(parents=True, exist_ok=True)
    for name, content in initiatives.items():
        (root / "vision" / "initiatives" / name).write_text(content, encoding="utf-8")
    if active is not None:
        (root / "vision" / "active_initiatives").write_text("\n".join(active) + "\n", encoding="utf-8")
    if finished is not None:
        (root / "vision" / "finished_initiatives").write_text("\n".join(finished) + "\n", encoding="utf-8")
    for name, content in (tasks or {}).items():
        (root / "backlog" / name).write_text(content, encoding="utf-8")
    (root / "_counters" / "task.txt").write_text(str(counter), encoding="utf-8")
    return root


FM_INIT = "---\nid: INI-01\nname: \"persistent-initiatives\"\ncreated: 2026-06-19T17:53:12Z\n---\n\n# persistent-initiatives\n\nbody one\n"
NOFM_INI = "# INI-02 — Product independence\n\nUmbrella body here.\n"
BARE = "A parent initiative for all small UI tweaks"


# --- normalize_ref ----------------------------------------------------------

def test_normalize_ref():
    assert normalize_ref("foo.md") == "foo"
    assert normalize_ref("foo") == "foo"


# --- derive_initiative ------------------------------------------------------

def test_derive_with_frontmatter(tmp_path: Path):
    p = tmp_path / "INI-01-persistent-initiatives.md"
    p.write_text(FM_INIT, encoding="utf-8")
    d = derive_initiative(p)
    assert d["old_id"] == "INI-01"
    assert d["stem"] == "INI-01-persistent-initiatives"
    assert d["title"] == "persistent-initiatives"
    assert d["created"] == "2026-06-19T17:53:12Z"
    assert "body one" in d["body"]
    # aka carries both the short INI id and the filename stem
    assert "INI-01" in d["aka"]
    assert "INI-01-persistent-initiatives" in d["aka"]


def test_derive_no_frontmatter_ini_filename(tmp_path: Path):
    p = tmp_path / "INI-02-product-independence.md"
    p.write_text(NOFM_INI, encoding="utf-8")
    d = derive_initiative(p)
    # no frontmatter → INI id comes from the filename prefix, title from the H1
    assert d["old_id"] == "INI-02"
    assert d["title"] == "INI-02 — Product independence"
    assert "INI-02" in d["aka"]
    assert "INI-02-product-independence" in d["aka"]
    assert "Umbrella body" in d["body"]


def test_derive_bare_slug_file(tmp_path: Path):
    p = tmp_path / "ui-polish.md"
    p.write_text(BARE, encoding="utf-8")
    d = derive_initiative(p)
    assert d["old_id"] is None
    assert d["stem"] == "ui-polish"
    assert d["title"]  # non-empty (falls back to stem)
    assert d["aka"] == ["ui-polish"]


# --- derive_status ----------------------------------------------------------

def test_status_active_finished_default():
    active = {"ui-polish.md"}
    finished = {"old-thing.md"}
    assert derive_status("ui-polish.md", active, finished) == "in_progress"
    assert derive_status("old-thing.md", active, finished) == "closed"
    assert derive_status("never-listed.md", active, finished) == "open"


# --- build_plan -------------------------------------------------------------

def test_plan_projects_sequential_tids_process_paradigm_last(tmp_path: Path):
    _mk(tmp_path, initiatives={
        "INI-01-persistent-initiatives.md": FM_INIT,
        "ui-polish.md": BARE,
        "process-paradigm.md": "# Process Paradigm\n\nbinds many\n",
    }, active=["ui-polish.md"], counter=543)
    plan = build_plan(tmp_path, SLUG)
    assert plan["count"] == 3
    assert plan["counter_before"] == 543
    # process-paradigm migrates LAST (highest id) — it binds 50+ live tickets
    assert plan["initiatives"][-1]["stem"] == "process-paradigm"
    ids = [i["new_id"] for i in plan["initiatives"]]
    assert ids == ["T-0544", "T-0545", "T-0546"]
    # status mapping flowed through
    by_stem = {i["stem"]: i for i in plan["initiatives"]}
    assert by_stem["ui-polish"]["status"] == "in_progress"


def test_plan_alias_index_and_child_match_vs_phantom(tmp_path: Path):
    _mk(
        tmp_path,
        initiatives={
            "ui-polish.md": BARE,
            "INI-01-persistent-initiatives.md": FM_INIT,
        },
        active=["ui-polish.md"],
        tasks={
            # child referencing a real initiative by .md basename → matched
            "T-0001-a.md": "---\nid: T-0001\ntitle: A\nstatus: open\ninitiative: ui-polish.md\n---\n\nx\n",
            # child referencing by bare stem → matched
            "T-0002-b.md": "---\nid: T-0002\ntitle: B\nstatus: open\ninitiative: INI-01\n---\n\nx\n",
            # child referencing a PHANTOM initiative (no file) → unmatched
            "T-0003-c.md": "---\nid: T-0003\ntitle: C\nstatus: open\ninitiative: maintenance\n---\n\nx\n",
        },
        counter=543,
    )
    plan = build_plan(tmp_path, SLUG)
    idx = plan["alias_index"]
    # ui-polish resolves by stem AND .md basename; INI-01 resolves by short id
    assert idx["ui-polish"] == idx.get("ui-polish")  # present
    assert normalize_ref("ui-polish.md") in idx
    assert "INI-01" in idx
    assert "INI-01-persistent-initiatives" in idx
    ch = plan["children"]
    assert ch["matched"] == 2
    assert ch["unmatched"] == 1
    assert ch["unmatched_refs"].get("maintenance") == 1


# --- D-B: INI-04 lives at vision/ ROOT and must be included -----------------

INI04_NAME = "INI-04-structured-stakeholder-comms-2026-06-21.md"
INI04_BODY = "# INI-04 — Structured stakeholder comms\n\nstakeholder 2026-06-21 body\n"


def test_includes_extra_root_initiative_ini04(tmp_path: Path):
    root = _mk(tmp_path, initiatives={
        "ui-polish.md": BARE,
        "process-paradigm.md": "# Process Paradigm\n\nbinds many\n",
    }, active=["ui-polish.md"], counter=543)
    # INI-04 sits at vision/ ROOT, NOT in vision/initiatives/
    (root / "vision" / INI04_NAME).write_text(INI04_BODY, encoding="utf-8")
    # a neighbouring source doc that must NOT be swept in
    (root / "vision" / "INI-XX-process-paradigm-SOURCE-VERBATIM.md").write_text("# src\n", encoding="utf-8")
    plan = build_plan(tmp_path, SLUG)
    stems = [i["stem"] for i in plan["initiatives"]]
    assert "INI-04-structured-stakeholder-comms-2026-06-21" in stems
    assert "INI-XX-process-paradigm-SOURCE-VERBATIM" not in stems  # source doc excluded
    assert plan["count"] == 3
    # process-paradigm still migrates LAST
    assert plan["initiatives"][-1]["stem"] == "process-paradigm"
    ini04 = next(i for i in plan["initiatives"] if i["stem"].startswith("INI-04"))
    assert ini04["old_id"] == "INI-04"
    assert "INI-04" in plan["alias_index"]


def test_apply_archives_root_initiative(tmp_path: Path):
    root = _mk(tmp_path, initiatives={"ui-polish.md": BARE}, active=["ui-polish.md"], counter=543)
    (root / "vision" / INI04_NAME).write_text(INI04_BODY, encoding="utf-8")
    run(tmp_path, SLUG, apply=True)
    # the root-level original is archived under _migrated/ (not deleted)
    assert (root / "vision" / "initiatives" / "_migrated" / INI04_NAME).exists()
    assert not (root / "vision" / INI04_NAME).exists()


# --- run: dry-run writes nothing; apply is idempotent -----------------------

def test_dry_run_writes_no_task_files_no_counter_bump(tmp_path: Path):
    root = _mk(tmp_path, initiatives={"ui-polish.md": BARE}, active=["ui-polish.md"], counter=543)
    manifest_out = tmp_path / "manifest.json"
    run(tmp_path, SLUG, apply=False, manifest_out=manifest_out)
    # no initiative-task created in backlog
    assert list((root / "backlog").glob("T-05*.md")) == []
    # counter untouched
    assert (root / "_counters" / "task.txt").read_text().strip() == "543"
    # originals NOT moved
    assert (root / "vision" / "initiatives" / "ui-polish.md").exists()
    # but the manifest WAS written (the operator-review artifact)
    assert manifest_out.exists()
    m = json.loads(manifest_out.read_text())
    assert m["count"] == 1


def test_apply_creates_tasks_moves_originals_writes_index(tmp_path: Path):
    root = _mk(tmp_path, initiatives={
        "ui-polish.md": BARE,
        "INI-01-persistent-initiatives.md": FM_INIT,
    }, active=["ui-polish.md"], counter=543)
    run(tmp_path, SLUG, apply=True)
    # two initiative-tasks now in backlog, each kind: initiative
    created = list((root / "backlog").glob("T-05*.md"))
    assert len(created) == 2
    assert all("kind: initiative" in p.read_text() for p in created)
    # originals archived (not deleted) under _migrated/
    assert (root / "vision" / "initiatives" / "_migrated" / "ui-polish.md").exists()
    assert not (root / "vision" / "initiatives" / "ui-polish.md").exists()
    # alias index written and resolves
    idx = json.loads((root / "vision" / "initiative_aliases.json").read_text())
    assert "ui-polish" in idx
    # counter advanced by 2
    assert (root / "_counters" / "task.txt").read_text().strip() == "545"


def test_apply_is_idempotent(tmp_path: Path):
    root = _mk(tmp_path, initiatives={"ui-polish.md": BARE}, active=["ui-polish.md"], counter=543)
    run(tmp_path, SLUG, apply=True)
    after_first = sorted(p.name for p in (root / "backlog").glob("T-*.md"))
    counter_first = (root / "_counters" / "task.txt").read_text().strip()
    # a second apply must NOT re-migrate (originals already archived → nothing to do)
    run(tmp_path, SLUG, apply=True)
    after_second = sorted(p.name for p in (root / "backlog").glob("T-*.md"))
    assert after_first == after_second
    assert (root / "_counters" / "task.txt").read_text().strip() == counter_first
