"""T-0950: `bsq ticket update` stamps `status_since` on a REAL transition, and
only on a real transition — never on an unrelated re-save or a no-op status
echo. Mirrors `test_bsq_ticket_transitions.py`'s harness."""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_status_since", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_status_since", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(path: Path, *, status: str, status_since: str = "") -> None:
    fm = ["id: T-0001", "title: demo", f"status: {status}", "provenance: T-0001"]
    if status_since:
        fm.append(f"status_since: {status_since}")
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\n## Verbatim request\n\nreal words\n")


def _run_update(tmp_path, monkeypatch, status):
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", status])
    args.func(args)


def test_real_transition_stamps_status_since(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="in_progress")
    _run_update(tmp_path, monkeypatch, "blocked_on_user")
    fm = bsq.read_frontmatter(p)
    assert fm["status"] == "blocked_on_user"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", fm["status_since"])


def test_noop_status_never_restamps(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="totest", status_since="2020-01-01T00:00:00Z")
    _run_update(tmp_path, monkeypatch, "totest")
    fm = bsq.read_frontmatter(p)
    assert fm["status_since"] == "2020-01-01T00:00:00Z"


def test_reentry_gets_a_fresh_stamp(tmp_path, monkeypatch):
    """T-0997: this used real wall-clock transitions with nothing between them,
    so before T-1016's monotonic `_fresh_since_stamp` it flaked ~1 run in 4 —
    whenever two of the three second-resolution stamps landed in the same
    second. T-1016 makes every real transition strictly greater than the last
    regardless of clock resolution, so `second != first` now holds by
    construction, not by wall-clock luck; measured 0/70 failures after the fix
    vs 47/70 on a copy with the fix reverted (interleaved runs, 2026-09-07)."""
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="in_progress", status_since="2020-01-01T00:00:00Z")
    _run_update(tmp_path, monkeypatch, "blocked_on_user")
    first = bsq.read_frontmatter(p)["status_since"]
    assert first != "2020-01-01T00:00:00Z"
    _run_update(tmp_path, monkeypatch, "in_progress")
    _run_update(tmp_path, monkeypatch, "blocked_on_user")
    second = bsq.read_frontmatter(p)["status_since"]
    assert second != first  # a fresh stay gets a fresh clock


def test_reentry_within_same_second_gets_monotonic_stamp(tmp_path, monkeypatch):
    """T-1016: status_since is stamped at whole-second resolution, so three
    real transitions landing inside one wall-clock second must not collapse
    to an identical stamp — status_deadlines.py's alert dedup is keyed on
    (ticket_id, status, status_since), so an unchanged stamp on re-entry reads
    as "still the same stay" and swallows the alert. Freeze the clock instead
    of relying on wall-clock luck to land in the same second (the original
    flake failed 13/20 runs depending on exactly that)."""
    frozen = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(bsq, "datetime", _FrozenDatetime)
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="in_progress", status_since="2020-01-01T00:00:00Z")
    _run_update(tmp_path, monkeypatch, "blocked_on_user")
    first = bsq.read_frontmatter(p)["status_since"]
    _run_update(tmp_path, monkeypatch, "in_progress")
    _run_update(tmp_path, monkeypatch, "blocked_on_user")
    second = bsq.read_frontmatter(p)["status_since"]
    assert first == "2025-01-01T00:00:00Z"
    assert second != first
    assert second == "2025-01-01T00:00:02Z"  # two same-second collisions, each bumped 1s


def test_first_ever_status_write_is_stamped(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    p.write_text("---\nid: T-0001\ntitle: demo\n---\n\n## Verbatim request\n\nx\n")
    _run_update(tmp_path, monkeypatch, "open")
    fm = bsq.read_frontmatter(p)
    assert fm["status"] == "open"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", fm["status_since"])
