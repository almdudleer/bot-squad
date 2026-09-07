"""T-0972 DoD 6: a torn routine md no longer stops a monitor SILENTLY.

Routine `md` files under `data/` are live shared state — sessions edit them,
the worker re-reads them on its tick. Nothing makes those edits atomic:
`Path.write_text()` truncates in place and streams, so a reader arriving
mid-write gets a prefix, a prefix has no closing `---`, and `parse_or_none`
answers None.

`_routine_from_md` used to answer None too, so the routine simply vanished
from `_monitor_routines()` and `list_routines()`. The monitor stopped
evaluating and nothing anywhere said so — strictly worse than the `bsq` half
of this ticket, which at least refuses loudly in front of whoever broke it.

`test_the_old_behaviour_was_a_silent_disappearance` is the control: it shows
the pre-fix code path producing exactly that silence, so the assertions below
are not passing against a reader incapable of the failure.
"""
from __future__ import annotations

import logging

import pytest

from bot_squad_worker import routines


GOOD = """\
---
id: R-0099
title: probe routine
trigger: monitor
schedule: ""
status: active
monitor:
  metric: probe
---

## Instruction

Do the thing.
"""


@pytest.fixture(autouse=True)
def _clean_cache():
    routines._LAST_GOOD_MD.clear()
    yield
    routines._LAST_GOOD_MD.clear()


def _md(tmp_path, text=GOOD):
    p = tmp_path / "R-0099-probe.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_a_healthy_md_parses_and_is_remembered(tmp_path):
    """Healthy case first — and the cache must actually be populated, or the
    degrade path below would be dead code that no test could distinguish from
    working."""
    p = _md(tmp_path)
    r = routines._routine_from_md(p)
    assert r is not None and r.id == "R-0099"
    assert str(p) in routines._LAST_GOOD_MD


def test_a_torn_read_serves_the_last_good_version_and_says_so(tmp_path, caplog):
    p = _md(tmp_path)
    assert routines._routine_from_md(p).id == "R-0099"

    # exactly what write_text leaves behind mid-stream: a prefix
    p.write_text(GOOD[: len(GOOD) // 2], encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=routines.__name__):
        r = routines._routine_from_md(p)

    assert r is not None and r.id == "R-0099", (
        "the monitor must keep evaluating on the last good text")
    assert any("stopped parsing" in rec.message or "stopped parsing" in rec.getMessage()
               for rec in caplog.records), (
        "the whole defect was that this was SILENT — a degraded read that logs "
        "nothing is the same bug with an extra branch")


def test_recovery_returns_to_the_new_text(tmp_path):
    p = _md(tmp_path)
    routines._routine_from_md(p)
    p.write_text(GOOD[:40], encoding="utf-8")
    routines._routine_from_md(p)
    p.write_text(GOOD.replace("probe routine", "renamed routine"), encoding="utf-8")
    r = routines._routine_from_md(p)
    assert r.title == "renamed routine", "a fixed file must win over the cache"


def test_a_file_that_never_parsed_stays_quiet(tmp_path, caplog):
    """A README in the routines dir is not a regression. Announcing it every
    tick would train everyone to ignore the channel this fix depends on."""
    p = tmp_path / "notes.md"
    p.write_text("just some notes, no frontmatter\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=routines.__name__):
        assert routines._routine_from_md(p) is None
    assert caplog.records == []


def test_an_unreadable_file_also_degrades(tmp_path, caplog):
    p = _md(tmp_path)
    routines._routine_from_md(p)
    p.unlink()
    with caplog.at_level(logging.ERROR, logger=routines.__name__):
        r = routines._routine_from_md(p)
    assert r is not None and r.id == "R-0099"
    assert any("unreadable" in rec.getMessage() for rec in caplog.records)


def test_the_old_behaviour_was_a_silent_disappearance(tmp_path, caplog):
    """THE CONTROL. Reproduce the pre-fix reader against the same torn file.

    It returns None and logs nothing, so the routine drops out of the monitor
    registry with no trace anywhere. If this ever stops being true, the arms
    above are no longer testing what they claim.
    """
    from bot_squad_worker import frontmatter as fm

    p = _md(tmp_path)
    p.write_text(GOOD[: len(GOOD) // 2], encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        parsed = fm.parse_or_none(p.read_text(encoding="utf-8"))
        old_result = None if not parsed else parsed
    assert old_result is None, (
        "a torn routine md must still be unparseable, or this control is "
        "measuring nothing")
    assert caplog.records == [], "and the old path said nothing at all"
