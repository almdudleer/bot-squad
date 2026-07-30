"""T-0783a: the ``pickup_queue`` worker action — the surface a session reaches
via ``bsq pickup``.

``operator_status`` already answered "is there work" with a COUNT. This answers
the question that was actually missing — WHICH work is takeable — so the two are
tested side by side here: the same board that reports ``pending_backlog: 2``
reports exactly one takeable ticket, which is the whole distinction.

Fixture shape follows ``test_operator_pause_actions``: temp Config + project,
``actions._get_config`` mocked to it, and no live tmux (so nothing holds a task
unless a test says so).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bot_squad_worker.actions as A
from bot_squad_worker import pickup
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError, dispatch as act_dispatch
from tests.test_jobs import _make_config_with_project, _make_project_with_repo


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug / "backlog").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / slug / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    return cfg, slug


def _write_task(cfg, slug, tid, **fields):
    fields.setdefault("title", f"work for {tid}")
    fields.setdefault("status", "open")
    fields.setdefault("priority", "P2")
    fields.setdefault("updated", "2099-01-01T00:00:00Z")
    body = "\n".join(f"{k}: {v}" for k, v in fields.items())
    (cfg.data_dir / slug / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\n{body}\n---\n\nbody\n"
    )


def test_the_action_returns_the_banded_queue_and_the_brief(cfg_slug):
    cfg, slug = cfg_slug
    _write_task(cfg, slug, "T-0719", status="reopened", priority="P1",
                title="REGRESSION reply-by-sid routing")

    out = act_dispatch("pickup_queue", {"slug": slug})

    assert out["ok"] is True
    assert out["slug"] == slug
    assert [r["id"] for r in out["pickup"]] == ["T-0719"]
    assert out["counts"]["pickup"] == 1
    assert "T-0719" in out["brief"]


def test_the_brief_is_the_same_text_the_redrive_injects(cfg_slug):
    """One rendering, two consumers. Two format strings is how the CLI and the
    operator brief drift into disagreeing about what is takeable."""
    cfg, slug = cfg_slug
    _write_task(cfg, slug, "T-1", status="reopened", priority="P1")

    out = act_dispatch("pickup_queue", {"slug": slug})
    expected = pickup.pickup_brief(pickup.pickup_queue(cfg, slug))
    assert out["brief"] == expected


def test_pending_backlog_counts_work_the_pickup_queue_does_not_offer(cfg_slug):
    """The gap this action closes, asserted directly: two pending tickets, one
    takeable. A count cannot tell an operator which one to dispatch at."""
    cfg, slug = cfg_slug
    _write_task(cfg, slug, "T-1", status="reopened", priority="P1")
    _write_task(cfg, slug, "T-2", status="totest", priority="P1")

    from bot_squad_worker import operator_redrive as ord_
    assert ord_.count_pending_backlog(cfg, slug) == 2

    out = act_dispatch("pickup_queue", {"slug": slug})
    assert out["counts"]["pickup"] == 1
    assert [r["id"] for r in out["pickup"]] == ["T-1"]


def test_band_filter_returns_only_that_band(cfg_slug):
    cfg, slug = cfg_slug
    _write_task(cfg, slug, "T-1", status="reopened", priority="P1")
    _write_task(cfg, slug, "T-2", status="open", priority=None)  # priority-missing

    out = act_dispatch("pickup_queue", {"slug": slug, "band": "pickup"})
    assert [r["id"] for r in out["pickup"]] == ["T-1"]
    assert "triage" not in out and "excluded" not in out

    out = act_dispatch("pickup_queue", {"slug": slug, "band": "triage"})
    assert [r["id"] for r in out["triage"]] == ["T-2"]
    assert "pickup" not in out


def test_the_counts_are_unfiltered_so_a_band_view_still_shows_the_whole_board(cfg_slug):
    cfg, slug = cfg_slug
    _write_task(cfg, slug, "T-1", status="reopened", priority="P1")
    _write_task(cfg, slug, "T-2", status="closed")

    out = act_dispatch("pickup_queue", {"slug": slug, "band": "pickup"})
    assert out["counts"] == {"pickup": 1, "triage": 0, "excluded": 1, "board": 2}


def test_an_empty_board_is_a_valid_answer_not_an_error(cfg_slug):
    cfg, slug = cfg_slug
    out = act_dispatch("pickup_queue", {"slug": slug})
    assert out["ok"] is True
    assert out["pickup"] == []
    assert out["brief"] == pickup.EMPTY_PICKUP_LINE


# --- strict param contract + unknown-slug guard -----------------------------

def test_rejects_unexpected_params(cfg_slug):
    cfg, slug = cfg_slug
    with pytest.raises(ActionError, match="unexpected"):
        act_dispatch("pickup_queue", {"slug": slug, "bogus": 1})


def test_requires_slug(cfg_slug):
    with pytest.raises(ActionError, match="missing required params"):
        act_dispatch("pickup_queue", {})


def test_rejects_an_unknown_band(cfg_slug):
    cfg, slug = cfg_slug
    with pytest.raises(ActionError, match="band must be one of"):
        act_dispatch("pickup_queue", {"slug": slug, "band": "takeable"})


def test_unknown_slug(cfg_slug):
    with pytest.raises(ActionError, match="unknown project slug"):
        act_dispatch("pickup_queue", {"slug": "no-such-proj"})
