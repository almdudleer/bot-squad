"""Tests for migrate_backlog.py.

Uses fixtures patterned on the real signal-tracker BACKLOG.md to verify
section-to-status mapping and item splitting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from migrate_backlog import decompose


SAMPLE = """\
# Project Board

## Must
- **Fix subscribe bug**. Description here.

## Should
- **Daily quiet-recheck digest**. Today users see nothing on quiet days.

## To Test
- **v0.6.0 — full Topic Monitoring**. STAGING RELEASE-READY.

## Reopened

## Released
See CHANGELOG. v0.5.1 (2026-04-23).

## Could
- [ ] Recheck model switch: budget caps + usage-API reconciliation.
- [x] Per-subscriber TG v2. Shipped.

## User Feedback
- **2026-04-15 (id=520, user=1)**: price sourcing → Promoted → ✅ shipped.
"""


def test_decompose_returns_tasks_and_feedback(tmp_path: Path) -> None:
    src = tmp_path / "BACKLOG.md"
    src.write_text(SAMPLE)
    out = decompose(src)
    by_status: dict[str, list[dict]] = {}
    for t in out["tasks"]:
        by_status.setdefault(t["status"], []).append(t)
    assert any("subscribe" in t["title"].lower() for t in by_status["open"])
    assert any("digest" in t["title"].lower() for t in by_status["open"])
    assert any("topic monitoring" in t["title"].lower() for t in by_status["totest"])
    assert len(out["feedback"]) == 1
    assert "price sourcing" in out["feedback"][0]["body"].lower()


def test_decompose_skips_done_could_items(tmp_path: Path) -> None:
    src = tmp_path / "BACKLOG.md"
    src.write_text(SAMPLE)
    out = decompose(src)
    titles = [t["title"].lower() for t in out["tasks"]]
    assert any("budget caps" in t for t in titles)
    # The [x]-marked TG v2 item should be status: closed, not open.
    closed = [t for t in out["tasks"] if t["status"] == "closed"]
    assert any("tg v2" in t["title"].lower() for t in closed)
