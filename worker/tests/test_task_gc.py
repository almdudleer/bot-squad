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
