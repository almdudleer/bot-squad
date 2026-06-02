"""Tests for T-0154 constant teams (bot_squad_worker.constant_teams).

Mirrors the manual walkthrough in
``data/bot-squad/scenarios/T-0154-constant-teams.md`` (written + walked first,
per T-0158): a demand-driven team spawns only when there is pending work AND it
is below team_size. The core safety property — an idle queue is a no-op — is the
first test, because that is what makes it safe to land the dogfood initiatives
on a live host.

``S.spawn`` and ``S.list_sessions`` are mocked: spawn records its calls instead
of opening tmux; list_sessions returns a controllable roster.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import constant_teams as ct
from bot_squad_worker import sessions as S
from tests.test_jobs import _make_config_with_project, _make_project_with_repo


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug / "vision" / "initiatives").mkdir(parents=True, exist_ok=True)
    # Reset the per-team spawn cooldown so back-to-back ticks in a test aren't
    # throttled by wall-clock.
    monkeypatch.setattr(ct, "_SPAWN_COOLDOWN_SEC", 0)
    spawns: list[dict] = []

    def _fake_spawn(c, s, window, initial_prompt=None, initiative=None, owner=None):
        sid = f"S-spawned-{len(spawns)}"
        spawns.append({"window": window, "initiative": initiative,
                       "prompt": initial_prompt, "owner": owner, "sid": sid})
        return {"ok": True, "sid": sid}

    monkeypatch.setattr(S, "spawn", _fake_spawn)
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [])
    return cfg, slug, spawns


def _write_initiative(cfg, slug, name, frontmatter: dict) -> Path:
    fm = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    p = cfg.data_dir / slug / "vision" / "initiatives" / f"{name}.md"
    p.write_text(f"---\nname: {name}\n{fm}\n---\n\n# {name}\n")
    return p


def test_idle_glob_queue_is_noop(cfg_slug):
    """Empty alert dir → no spawn (the safety property)."""
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "prod-support",
                      {"constant_team": "true", "team_size": 1, "consume": "_alerts/*.md"})
    res = ct.tick(cfg, slug)
    assert res["actions"] == []
    assert spawns == []


def test_glob_alert_spawns_triage(cfg_slug):
    """An alert file present + team below size → exactly one triage spawn."""
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "prod-support",
                      {"constant_team": "true", "team_size": 1, "consume": "_alerts/*.md"})
    alerts = cfg.data_dir / slug / "_alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / "alert-1.md").write_text("# DB latency spike\n500ms p99")

    res = ct.tick(cfg, slug)
    assert len(spawns) == 1
    assert spawns[0]["initiative"] == "prod-support.md"
    assert "alert-1.md" in spawns[0]["prompt"]
    assert res["actions"][0]["action"] == "spawned"


def test_at_capacity_no_spawn(cfg_slug, monkeypatch):
    """A live member already on the initiative → no further spawn (gating)."""
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "prod-support",
                      {"constant_team": "true", "team_size": 1, "consume": "_alerts/*.md"})
    alerts = cfg.data_dir / slug / "_alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / "alert-1.md").write_text("boom")

    # One active member bound to the initiative occupies the only slot.
    rows = [{"sid": "S-live", "status": "active",
             "initiative": "prod-support.md", "window": "prod-support"}]
    monkeypatch.setattr(S, "list_sessions", lambda c, s: rows)
    ct.tick(cfg, slug)
    assert spawns == []


def test_log_queue_advances_cursor(cfg_slug):
    """A .log consume source hands new lines to the dev and advances the cursor
    so the same feedback is not reprocessed on the next tick."""
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "user-feedback",
                      {"constant_team": "true", "team_size": 1,
                       "consume": "feedback/inbox.log"})
    fb = cfg.data_dir / slug / "feedback"
    fb.mkdir(parents=True, exist_ok=True)
    log = fb / "inbox.log"
    log.write_text("2026-06-02T00:00:00Z | S-x | sidebar is confusing\n")

    ct.tick(cfg, slug)
    assert len(spawns) == 1
    assert "sidebar is confusing" in spawns[0]["prompt"]

    # Second tick with no new lines → no spawn (cursor consumed the batch).
    ct.tick(cfg, slug)
    assert len(spawns) == 1

    # Append a new line → next tick spawns again with only the new line.
    with open(log, "a") as f:
        f.write("2026-06-02T01:00:00Z | S-y | spawn races\n")
    ct.tick(cfg, slug)
    assert len(spawns) == 2
    assert "spawn races" in spawns[1]["prompt"]
    assert "sidebar is confusing" not in spawns[1]["prompt"]


def test_non_constant_initiative_ignored(cfg_slug):
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "regular-feature", {"status": "open"})
    ct.tick(cfg, slug)
    assert spawns == []


def test_kill_switch(cfg_slug, monkeypatch):
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "prod-support",
                      {"constant_team": "true", "team_size": 1, "consume": "_alerts/*.md"})
    alerts = cfg.data_dir / slug / "_alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / "a.md").write_text("x")
    monkeypatch.setenv("BOT_SQUAD_CONSTANT_TEAMS_DISABLED", "1")
    res = ct.tick(cfg, slug)
    assert res.get("disabled") is True
    assert spawns == []


# ---------------------------------------------------------------------------
# T-0177 — constant_team_stems: which initiatives keep their own team
# ---------------------------------------------------------------------------

def test_constant_team_stems_returns_flagged_initiatives(cfg_slug):
    cfg, slug, _ = cfg_slug
    _write_initiative(cfg, slug, "user-feedback", {"constant_team": "true"})
    _write_initiative(cfg, slug, "prod-support", {"constant_team": "true"})
    _write_initiative(cfg, slug, "operator-ux", {})  # normal initiative
    assert ct.constant_team_stems(cfg, slug) == {"user-feedback", "prod-support"}


def test_constant_team_stems_empty_when_none_flagged(cfg_slug):
    cfg, slug, _ = cfg_slug
    _write_initiative(cfg, slug, "operator-ux", {})
    assert ct.constant_team_stems(cfg, slug) == set()
