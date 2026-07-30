"""Tests for T-0482 user-controlled execution pace (bot_squad_worker.pace).

Promotes the manual walkthrough in
``data/bot-squad/scenarios/T-0482-...md`` (written + walked live first, per
T-0158): the pace-control config EXISTS, is user-settable, persists, normalises
the initiative id (.md-vs-stem), and is readable by the T-0475 consumer — and the
GLOBAL pause shares ONE SSOT with operator_redrive.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import pace
from bot_squad_worker import operator_redrive as ord_
from tests.test_jobs import _make_config_with_project, _make_project_with_repo


@pytest.fixture
def cfg_slug(tmp_path: Path):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug).mkdir(parents=True, exist_ok=True)
    return cfg, slug


# --------------------------------------------------------------------------
# Defaults / back-compat
# --------------------------------------------------------------------------

def test_fresh_project_is_uncapped_and_running(cfg_slug):
    cfg, slug = cfg_slug
    conf = pace.read_config(cfg, slug)
    # Asserted key-by-key, NOT as a whole-dict equality (T-0828).
    #
    # This test is named for two facts — uncapped, and running — and those are
    # the two it should pin. It previously asserted `conf == {...}`, which meant
    # ANY new key on the pace-control view red-ed it: T-0828 added `drive` and
    # this was the only failure in a 3259-test run. p536 dodged the same
    # landmine deliberately in the same hour (it put a new field outside
    # `q["counts"]` because three suites pin that dict exactly), which makes
    # whole-dict equality a named hazard class here rather than a one-off.
    #
    # The drive block's own defaults are NOT left unstated by this narrowing —
    # test_absent_drive_block_reads_as_todays_behaviour below is the dedicated
    # pin for them, and it says so in one place instead of as a side effect of
    # an unrelated assertion.
    assert conf["max_in_progress"] == 0
    assert conf["paused"] is False
    assert conf["initiatives"] == {}
    assert pace.max_in_progress(cfg, slug) == 0


def test_absent_drive_block_reads_as_todays_behaviour(cfg_slug):
    """T-0828 DoD-2: absent == today's behaviour, and this is the pin for it.

    A project with no ``drive`` key must read as the widest scope, the implicit
    stopping rule and no alert — i.e. exactly what every project did before this
    lane existed. Shipping it as a no-op BY CONSTRUCTION is the property that
    makes it safe to land ahead of L2/L3/L4 (the T-0799 pattern).

    ``configured`` is False here and that is the load-bearing half: a project
    that never set a mode and one deliberately set to ``all`` BOTH read
    ``scope == "all"``, and telling those apart is the stakeholder's actual
    question («какой режим драйва щас стоит»).
    """
    cfg, slug = cfg_slug
    for drive in (pace.read_drive(cfg, slug), pace.read_config(cfg, slug)["drive"]):
        assert drive == {
            "scope": "all",
            "stop_when": "scope_exhausted",
            "on_stop": "nothing",
            "set_by": None,
            "set_at": None,
            "source_text": None,
            "configured": False,
            "invalid": {},
        }
    # The defaults are the documented constants, not numbers repeated by hand.
    assert pace.DRIVE_DEFAULTS == {
        "scope": "all", "stop_when": "scope_exhausted", "on_stop": "nothing"}


def test_legacy_pace_json_written_before_this_lane_still_loads(cfg_slug):
    """T-0828 DoD-2, second half: a ``pace.json`` written BEFORE drive modes
    existed still loads, and its existing settings survive untouched.

    Byte-for-byte the shape T-0482 shipped — no ``drive`` key at all. The risk
    this closes is a reader that assumes the new key is present.
    """
    cfg, slug = cfg_slug
    p = pace._config_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{\n  "initiatives": {\n    "process-paradigm.md": {\n'
        '      "paused": false,\n      "priority": 5,\n      "weight": 2.0\n'
        '    }\n  },\n  "max_in_progress": 7\n}\n'
    )
    conf = pace.read_config(cfg, slug)
    assert conf["max_in_progress"] == 7
    assert conf["initiatives"]["process-paradigm.md"] == {
        "weight": 2.0, "priority": 5, "paused": False}
    assert conf["drive"]["configured"] is False
    assert conf["drive"]["scope"] == "all"


def test_set_drive_records_the_axes_and_the_words_that_set_them(cfg_slug):
    """T-0828 DoD-5. ``source_text`` is not decoration and not a log — it is the
    visibility half. His complaint was «я просил закончить всё что в опен, но
    видимо это не интерпретировалось»; storing the phrase is what lets every
    surface answer "did my instruction land"."""
    cfg, slug = cfg_slug
    got = pace.set_drive(
        cfg, slug,
        scope="open_reopened",
        set_by="S-almdudleer-operator-p533",
        source_text="закончить всё что в опен",
    )
    assert got["scope"] == "open_reopened"
    assert got["configured"] is True
    assert got["set_by"] == "S-almdudleer-operator-p533"
    assert got["source_text"] == "закончить всё что в опен"
    assert got["set_at"].endswith("Z")
    # Untouched axes keep their defaults rather than being blanked.
    assert got["stop_when"] == "scope_exhausted"
    assert got["on_stop"] == "nothing"
    # ...and it round-trips through a fresh read, not just the return value.
    assert pace.read_drive(cfg, slug) == got


def test_set_drive_is_partial_and_keeps_the_axes_it_was_not_given(cfg_slug):
    cfg, slug = cfg_slug
    pace.set_drive(cfg, slug, scope="in_progress", source_text="в ин прогресс")
    pace.set_drive(cfg, slug, on_stop="alert", set_by="S-other")
    d = pace.read_drive(cfg, slug)
    assert d["scope"] == "in_progress"      # survived the second write
    assert d["on_stop"] == "alert"
    assert d["source_text"] == "в ин прогресс"  # None means "leave alone"
    assert d["set_by"] == "S-other"         # provenance is refreshed every write


def test_set_drive_rejects_an_unknown_value_and_names_the_allowed_set(cfg_slug):
    """T-0828 DoD-3: reject rather than coerce. A settings surface that quietly
    ignores what you set it to reproduces the exact defect he reported, so the
    error is NAMED and carries the closed set — the message is what the CLI
    prints, so it has to be actionable on its own."""
    cfg, slug = cfg_slug
    with pytest.raises(pace.DriveModeError) as ei:
        pace.set_drive(cfg, slug, scope="opne")
    msg = str(ei.value)
    assert "opne" in msg
    assert "open_reopened" in msg and "in_progress" in msg and "all" in msg

    for field, bad in (("stop_when", "forever"), ("on_stop", "ALERT!")):
        with pytest.raises(pace.DriveModeError):
            pace.set_drive(cfg, slug, **{field: bad})


def test_a_rejected_write_leaves_the_store_completely_untouched(cfg_slug):
    """Validate-all-before-writing. A call with one good and one bad axis must
    not half-apply: a partially-applied settings write is worse than a rejected
    one, because every surface then reports a state nobody asked for."""
    cfg, slug = cfg_slug
    pace.set_drive(cfg, slug, scope="open_reopened", source_text="как было")
    before = pace.read_drive(cfg, slug)

    with pytest.raises(pace.DriveModeError):
        pace.set_drive(cfg, slug, scope="in_progress", on_stop="NOPE")

    assert pace.read_drive(cfg, slug) == before  # incl. set_at — nothing was written


def test_set_drive_refuses_a_call_that_sets_nothing(cfg_slug):
    cfg, slug = cfg_slug
    with pytest.raises(ValueError):
        pace.set_drive(cfg, slug)


def test_clear_drive_returns_to_exactly_the_never_set_state(cfg_slug):
    """T-0828 DoD-5, the clearing half. Without it the only way out of a mode is
    to set the default explicitly — which every surface then reports as a
    deliberate choice (``configured: True``), so it cannot express "I have no
    standing mode"."""
    cfg, slug = cfg_slug
    fresh = pace.read_drive(cfg, slug)

    pace.set_drive(cfg, slug, scope="in_progress", on_stop="alert",
                   source_text="потратить квоту")
    assert pace.read_drive(cfg, slug)["configured"] is True

    assert pace.clear_drive(cfg, slug) is True
    assert pace.read_drive(cfg, slug) == fresh          # byte-identical to never-set
    assert pace.clear_drive(cfg, slug) is False         # idempotent


def test_clear_drive_keeps_the_other_pace_settings(cfg_slug):
    """Clearing the MODE must not clear the pace config around it."""
    cfg, slug = cfg_slug
    pace.set_max_in_progress(cfg, slug, 4)
    pace.set_initiative(cfg, slug, "process-paradigm", weight=3.0)
    pace.set_drive(cfg, slug, scope="all")

    pace.clear_drive(cfg, slug)

    conf = pace.read_config(cfg, slug)
    assert conf["max_in_progress"] == 4
    assert conf["initiatives"]["process-paradigm.md"]["weight"] == 3.0
    assert conf["drive"]["configured"] is False


def test_an_out_of_set_value_ON_DISK_is_reported_not_silently_swapped(cfg_slug):
    """The READ path's half of "reject, never coerce".

    It must not raise — the worker tick and the UI both call this, and a
    hand-edited file must not take the drive down. But the fallback must be
    VISIBLE: an absent value that reads identically to a real one is the
    silent-None failure D-0069 calls the most dangerous line in the design.
    """
    cfg, slug = cfg_slug
    p = pace._config_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"drive": {"scope": "opne", "on_stop": "ALERT!"}}')

    d = pace.read_drive(cfg, slug)
    assert d["scope"] == "all"                    # effective value = the default
    assert d["on_stop"] == "nothing"
    assert d["invalid"] == {"scope": "opne", "on_stop": "ALERT!"}  # raw values kept
    assert d["stop_when"] == "scope_exhausted"    # the untouched axis is unaffected
    assert d["configured"] is True


def test_read_drive_never_raises_on_a_hostile_config(cfg_slug):
    """Widening on an unreadable setting is safe; narrowing would HIDE work, and
    raising would take the drive down. So this path swallows shape errors and
    reports them, and L2 (T-0829) depends on that contract."""
    cfg, slug = cfg_slug
    p = pace._config_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    for body in ('{ not json', '{"drive": "open_reopened"}', '{"drive": null}',
                 '{"drive": {"scope": 5, "stop_when": ["x"], "on_stop": {"a": 1}}}',
                 '[]'):
        p.write_text(body)
        d = pace.read_drive(cfg, slug)
        assert d["scope"] in pace.DRIVE_SCOPES
        assert d["stop_when"] in pace.DRIVE_STOP_WHEN
        assert d["on_stop"] in pace.DRIVE_ON_STOP


def test_validate_drive_value_is_the_single_gate(cfg_slug):
    """Every accepted value is in the closed set, and an unknown FIELD is an
    error too — so a future flag added without wiring cannot write a key nobody
    validates."""
    for field, choices in pace.DRIVE_CHOICES.items():
        for good in choices:
            assert pace.validate_drive_value(field, good) == good
        assert pace.validate_drive_value(field, f"  {choices[0]}  ") == choices[0]
    with pytest.raises(pace.DriveModeError):
        pace.validate_drive_value("nonesuch", "all")


def test_initiative_pace_defaults_for_unconfigured(cfg_slug):
    cfg, slug = cfg_slug
    assert pace.initiative_pace(cfg, slug, "anything") == {
        "weight": 1.0, "priority": 0, "paused": False,
    }


# --------------------------------------------------------------------------
# max_in_progress
# --------------------------------------------------------------------------

def test_set_max_in_progress_persists(cfg_slug):
    cfg, slug = cfg_slug
    assert pace.set_max_in_progress(cfg, slug, 3) == 3
    assert pace.max_in_progress(cfg, slug) == 3
    # survives a fresh read (persisted to disk, not just in-memory)
    assert pace.read_config(cfg, slug)["max_in_progress"] == 3


def test_max_in_progress_clamps_negative_to_unlimited(cfg_slug):
    cfg, slug = cfg_slug
    assert pace.set_max_in_progress(cfg, slug, -5) == 0


def test_max_in_progress_garbage_is_zero(cfg_slug):
    cfg, slug = cfg_slug
    assert pace.set_max_in_progress(cfg, slug, "nan") == 0


# --------------------------------------------------------------------------
# per-initiative weight / priority / pause + .md normalisation
# --------------------------------------------------------------------------

def test_normalize_initiative_collapses_stem_and_md():
    assert pace.normalize_initiative("process-paradigm") == "process-paradigm.md"
    assert pace.normalize_initiative("process-paradigm.md") == "process-paradigm.md"
    assert pace.normalize_initiative("vision/initiatives/x.md") == "x.md"
    assert pace.normalize_initiative("  spaced  ") == "spaced.md"
    assert pace.normalize_initiative("") == ""


def test_set_initiative_stores_under_md_key(cfg_slug):
    cfg, slug = cfg_slug
    pace.set_initiative(cfg, slug, "process-paradigm", weight=2.0, priority=1)
    inits = pace.read_config(cfg, slug)["initiatives"]
    assert set(inits) == {"process-paradigm.md"}
    assert inits["process-paradigm.md"] == {"weight": 2.0, "priority": 1, "paused": False}


def test_stem_and_md_address_the_same_entry(cfg_slug):
    cfg, slug = cfg_slug
    pace.set_initiative(cfg, slug, "process-paradigm", weight=2.0)
    # the .md form must UPDATE, not create a second row
    pace.set_initiative(cfg, slug, "process-paradigm.md", paused=True)
    inits = pace.read_config(cfg, slug)["initiatives"]
    assert list(inits) == ["process-paradigm.md"]
    assert inits["process-paradigm.md"] == {"weight": 2.0, "priority": 0, "paused": True}


def test_set_initiative_partial_update_keeps_other_fields(cfg_slug):
    cfg, slug = cfg_slug
    pace.set_initiative(cfg, slug, "x", weight=3.0, priority=5)
    pace.set_initiative(cfg, slug, "x", paused=True)  # only paused
    assert pace.initiative_pace(cfg, slug, "x") == {"weight": 3.0, "priority": 5, "paused": True}


def test_set_initiative_empty_name_rejected(cfg_slug):
    cfg, slug = cfg_slug
    with pytest.raises(ValueError):
        pace.set_initiative(cfg, slug, "   ", weight=1.0)


def test_clear_initiative(cfg_slug):
    cfg, slug = cfg_slug
    pace.set_initiative(cfg, slug, "x", weight=2.0)
    assert pace.clear_initiative(cfg, slug, "x.md") is True
    assert pace.read_config(cfg, slug)["initiatives"] == {}
    # clearing an absent one is a no-op False
    assert pace.clear_initiative(cfg, slug, "x") is False


# --------------------------------------------------------------------------
# global pause shares ONE SSOT with operator_redrive
# --------------------------------------------------------------------------

def test_global_pause_delegates_to_operator_redrive(cfg_slug):
    cfg, slug = cfg_slug
    assert pace.read_config(cfg, slug)["paused"] is False
    pace.pause(cfg, slug, by="tester", reason="r")
    # the SAME flag operator_redrive reads
    assert ord_.is_paused(cfg, slug) is True
    assert pace.read_config(cfg, slug)["paused"] is True
    assert pace.resume(cfg, slug) is True
    assert ord_.is_paused(cfg, slug) is False
    assert pace.read_config(cfg, slug)["paused"] is False


def test_operator_redrive_pause_is_visible_to_pace(cfg_slug):
    cfg, slug = cfg_slug
    # a pause set DIRECTLY via operator_redrive (e.g. M2 path) reads back through pace
    ord_.pause(cfg, slug, by="m2")
    assert pace.read_config(cfg, slug)["paused"] is True
    ord_.resume(cfg, slug)


# --------------------------------------------------------------------------
# corruption tolerance
# --------------------------------------------------------------------------

def test_unreadable_config_falls_back_to_defaults(cfg_slug):
    cfg, slug = cfg_slug
    p = pace._config_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    conf = pace.read_config(cfg, slug)
    assert conf["max_in_progress"] == 0
    assert conf["initiatives"] == {}
