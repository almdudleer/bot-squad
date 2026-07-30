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
    # T-0828 grew read_config by one key (`drive`). The exact-equality assertion
    # is KEPT rather than loosened to a subset check: this test's job is to say
    # what the full default view IS, and a subset check would have let the drive
    # block ship without anyone stating its defaults here.
    assert conf == {
        "max_in_progress": 0,
        "paused": False,
        "initiatives": {},
        "drive": {
            "scope": "all",
            "stop_when": "scope_exhausted",
            "on_stop": "nothing",
            "set_by": None,
            "set_at": None,
            "source_text": None,
            "configured": False,
            "invalid": {},
        },
    }
    assert pace.max_in_progress(cfg, slug) == 0


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
