"""F7 / T-0233-extension: auto-GC throwaway QA tasks so they stop leaking onto
the board (a dangling loop). A throwaway task is detected by its TITLE prefix
(``QA-TEST-DELETEME``) or a ``throwaway: true`` flag — NEVER by body text, so a
real ticket that merely *mentions* the convention is left alone."""
from __future__ import annotations

import os
import types
from pathlib import Path

from bot_squad_worker import task_gc


def _cfg(tmp_path: Path):
    return types.SimpleNamespace(data_dir=tmp_path)


def _write_task(tmp_path: Path, name: str, *, title: str, status: str = "open",
                body: str = "", throwaway=None, age_sec: float = 0.0, now: float = 1_000_000.0):
    backlog = tmp_path / "proj" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    fm = f"---\nid: {name}\ntitle: {title!r}\nstatus: {status}\n"
    if throwaway is not None:
        fm += f"throwaway: {str(throwaway).lower()}\n"
    fm += "---\n\n"
    p = backlog / f"{name}.md"
    p.write_text(fm + body, encoding="utf-8")
    if age_sec:
        os.utime(p, (now - age_sec, now - age_sec))
    return p


# --- pure detection ---------------------------------------------------------

def test_is_throwaway_detects_title_prefix_case_insensitive():
    assert task_gc.is_throwaway_task("QA-TEST-DELETEME-board dogfood", {}) is True
    assert task_gc.is_throwaway_task("qa-test-deleteme x", {}) is True
    assert task_gc.is_throwaway_task("Normal feature ticket", {}) is False


def test_is_throwaway_detects_flag():
    assert task_gc.is_throwaway_task("Anything", {"throwaway": True}) is True
    assert task_gc.is_throwaway_task("Anything", {"throwaway": "true"}) is True
    assert task_gc.is_throwaway_task("Anything", {"throwaway": False}) is False


def test_is_throwaway_ignores_body_mentions():
    # detection takes (title, meta) — a ticket whose BODY discusses the
    # convention is NOT throwaway.
    assert task_gc.is_throwaway_task(
        "Add auto-GC for QA-TEST-DELETEME tasks", {}) is False


# --- GC behavior ------------------------------------------------------------

def test_gc_archives_stale_throwaway(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0262", title="QA-TEST-DELETEME-board dogfood",
                status="open", age_sec=7200, now=now)  # 2h old, grace 1h
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert "T-0262" in out["archived"]
    backlog = tmp_path / "proj" / "backlog"
    assert not (backlog / "T-0262.md").exists()          # off the board
    assert (backlog / "_gc" / "T-0262.md").exists()      # archived (reversible)


def test_gc_archives_closed_throwaway_immediately(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0900", title="QA-TEST-DELETEME-x",
                status="closed", age_sec=0, now=now)  # fresh but closed
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert "T-0900" in out["archived"]


def test_gc_leaves_fresh_open_throwaway(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0901", title="QA-TEST-DELETEME-in-progress",
                status="open", age_sec=60, now=now)  # 1 min old < grace
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []
    assert (tmp_path / "proj" / "backlog" / "T-0901.md").exists()


def test_gc_leaves_normal_tasks_even_if_old(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0100", title="Real feature ticket",
                status="open", age_sec=999999, now=now)
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []
    assert (tmp_path / "proj" / "backlog" / "T-0100.md").exists()


def test_gc_ignores_a_normal_task_that_mentions_the_marker_in_body(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "F7-feat", title="Add auto-GC of throwaway tasks",
                status="open", body="We purge QA-TEST-DELETEME tasks.",
                age_sec=999999, now=now)
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []


def test_gc_no_backlog_is_safe(tmp_path):
    assert task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj")["archived"] == []


# === T-0484: general task cleanup (stale + duplicate) =======================
# Generalizes the QA-throwaway GC into a real cleanup process: (a) age-based
# archival of stale/abandoned tasks, (b) duplicate detection + merge. Both are
# reversible (off-board archive to backlog/_gc/). The existing throwaway GC
# (tested above) is untouched.

# --- stale detection (pure) -------------------------------------------------

def test_is_stale_open_task_past_grace():
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "open"}, grace + 1) is True
    assert task_gc.is_stale_task({"status": "planned"}, grace + 1) is True
    assert task_gc.is_stale_task({"status": "reopened"}, grace + 1) is True


def test_is_stale_active_task_never_stale():
    # in-progress / totest tasks are being worked — age must not reap them.
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "in_progress"}, grace * 10) is False
    assert task_gc.is_stale_task({"status": "totest"}, grace * 10) is False


def test_is_stale_closed_task_never_stale():
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "closed"}, grace * 10) is False


def test_is_stale_fresh_open_task_not_stale():
    assert task_gc.is_stale_task({"status": "open"}, 60) is False


def test_is_stale_skips_throwaway():
    # throwaway tasks have their own faster GC path — don't double-handle.
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "open", "throwaway": True}, grace + 1) is False


# --- stale find + age-based archive -----------------------------------------

def test_find_stale_tasks_reports_only_aged_inactive(tmp_path):
    now = 1_000_000.0
    grace = task_gc.stale_task_gc_sec()
    _write_task(tmp_path, "T-0500", title="Old abandoned task",
                status="open", age_sec=grace + 100, now=now)
    _write_task(tmp_path, "T-0501", title="Fresh task",
                status="open", age_sec=60, now=now)
    _write_task(tmp_path, "T-0502", title="Active task",
                status="in_progress", age_sec=grace + 100, now=now)
    out = task_gc.find_stale_tasks(_cfg(tmp_path), "proj", now=now)
    ids = {s["id"] for s in out["stale"]}
    assert ids == {"T-0500"}


def test_gc_stale_tasks_archives_reversibly(tmp_path):
    now = 1_000_000.0
    grace = task_gc.stale_task_gc_sec()
    _write_task(tmp_path, "T-0500", title="Old abandoned task",
                status="open", age_sec=grace + 100, now=now)
    out = task_gc.gc_stale_tasks(_cfg(tmp_path), "proj", now=now)
    assert "T-0500" in out["archived"]
    backlog = tmp_path / "proj" / "backlog"
    assert not (backlog / "T-0500.md").exists()        # off the board
    assert (backlog / "_gc" / "T-0500.md").exists()    # archived (reversible)


def test_gc_stale_tasks_leaves_active_and_fresh(tmp_path):
    now = 1_000_000.0
    grace = task_gc.stale_task_gc_sec()
    _write_task(tmp_path, "T-0502", title="Active task",
                status="in_progress", age_sec=grace + 100, now=now)
    _write_task(tmp_path, "T-0501", title="Fresh task",
                status="open", age_sec=60, now=now)
    out = task_gc.gc_stale_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []
    backlog = tmp_path / "proj" / "backlog"
    assert (backlog / "T-0502.md").exists()
    assert (backlog / "T-0501.md").exists()


# --- duplicate detection (suggestion) + merge -------------------------------

def test_find_duplicate_tasks_groups_by_normalized_title(tmp_path):
    _write_task(tmp_path, "T-0600", title="Fix the login bug", status="open")
    _write_task(tmp_path, "T-0601", title="fix the   LOGIN  bug!", status="open")
    _write_task(tmp_path, "T-0602", title="Unrelated work", status="open")
    out = task_gc.find_duplicate_tasks(_cfg(tmp_path), "proj")
    groups = out["duplicates"]
    assert len(groups) == 1
    g = groups[0]
    assert set(g["ids"]) == {"T-0600", "T-0601"}
    assert g["suggested_keep"] == "T-0600"  # lowest numeric id = the original


def test_find_duplicate_tasks_ignores_throwaway(tmp_path):
    _write_task(tmp_path, "T-0610", title="QA-TEST-DELETEME-x", status="open")
    _write_task(tmp_path, "T-0611", title="QA-TEST-DELETEME-x", status="open")
    out = task_gc.find_duplicate_tasks(_cfg(tmp_path), "proj")
    assert out["duplicates"] == []


def test_merge_tasks_archives_dups_and_annotates_keeper(tmp_path):
    _write_task(tmp_path, "T-0600", title="Fix the login bug", status="open")
    _write_task(tmp_path, "T-0601", title="fix the login bug", status="open")
    out = task_gc.merge_tasks(_cfg(tmp_path), "proj", "T-0600", ["T-0601"])
    assert out["kept"] == "T-0600"
    assert "T-0601" in out["merged"]
    backlog = tmp_path / "proj" / "backlog"
    # dup off the board, preserved in _gc (reversible)
    assert not (backlog / "T-0601.md").exists()
    archived = backlog / "_gc" / "T-0601.md"
    assert archived.exists()
    assert "merged_into" in archived.read_text(encoding="utf-8")
    # keeper carries a back-reference so the merge is discoverable + reversible
    assert "T-0601" in (backlog / "T-0600.md").read_text(encoding="utf-8")


def test_merge_tasks_reports_missing_ids(tmp_path):
    _write_task(tmp_path, "T-0600", title="Fix the login bug", status="open")
    out = task_gc.merge_tasks(_cfg(tmp_path), "proj", "T-0600", ["T-9999"])
    assert out["merged"] == []
    assert "T-9999" in out["missing"]
