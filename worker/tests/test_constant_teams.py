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


# --- T-0350: reap idle constant-team members once their queue drains ---------

def _member_row(sid, *, window="user-feedback", activity="idle", status="active",
                owner="constant-team", initiative="user-feedback.md",
                started_at="2026-06-21T00:00:00Z"):
    return {"sid": sid, "window": window, "activity": activity, "status": status,
            "owner": owner, "initiative": initiative, "started_at": started_at}


def _setup_uf(cfg, slug, *, inbox_lines: int, cursor: int):
    """A user-feedback constant team with an inbox.log of N lines + a cursor."""
    _write_initiative(cfg, slug, "user-feedback", {
        "constant_team": "true", "consume": "feedback/inbox.log",
        "team_window": "user-feedback", "team_role": "dev",
    })
    fb = cfg.data_dir / slug / "feedback"
    fb.mkdir(parents=True, exist_ok=True)
    (fb / "inbox.log").write_text("".join(f"line {i}\n" for i in range(inbox_lines)))
    ct._save_state(cfg, slug, "user-feedback", {"cursor_lines": cursor, "last_spawn_at": 0})


def test_gc_drained_members_reaps_idle_member_when_drained(cfg_slug, monkeypatch):
    cfg, slug, _ = cfg_slug
    _setup_uf(cfg, slug, inbox_lines=5, cursor=5)  # drained
    suspended: list[str] = []
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [_member_row("S-x-user-feedback-p9")])
    monkeypatch.setattr(S, "suspend", lambda c, s, sid: suspended.append(sid) or {"ok": True})
    out = ct.gc_drained_members(cfg, slug, now=2_000_000_000.0)
    assert suspended == ["S-x-user-feedback-p9"]
    assert out["reaped"] == ["S-x-user-feedback-p9"]


def test_gc_drained_members_spares_running_member(cfg_slug, monkeypatch):
    cfg, slug, _ = cfg_slug
    _setup_uf(cfg, slug, inbox_lines=5, cursor=5)  # drained
    suspended: list[str] = []
    monkeypatch.setattr(S, "list_sessions",
                        lambda c, s: [_member_row("S-x-user-feedback-p9", activity="running")])
    monkeypatch.setattr(S, "suspend", lambda c, s, sid: suspended.append(sid))
    ct.gc_drained_members(cfg, slug, now=2_000_000_000.0)
    assert suspended == []  # actively triaging → never interrupted


def test_gc_drained_members_spares_when_queue_not_drained(cfg_slug, monkeypatch):
    cfg, slug, _ = cfg_slug
    _setup_uf(cfg, slug, inbox_lines=5, cursor=3)  # 2 lines pending
    suspended: list[str] = []
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [_member_row("S-x-user-feedback-p9")])
    monkeypatch.setattr(S, "suspend", lambda c, s, sid: suspended.append(sid))
    ct.gc_drained_members(cfg, slug, now=2_000_000_000.0)
    assert suspended == []  # real unprocessed feedback → the member has work


def test_gc_drained_members_spares_fresh_member(cfg_slug, monkeypatch):
    cfg, slug, _ = cfg_slug
    _setup_uf(cfg, slug, inbox_lines=5, cursor=5)
    suspended: list[str] = []
    # started 10s ago — still in the bringup settle window
    monkeypatch.setattr(S, "list_sessions",
                        lambda c, s: [_member_row("S-x-user-feedback-p9",
                                                  started_at="2026-06-21T00:00:00Z")])
    monkeypatch.setattr(S, "suspend", lambda c, s, sid: suspended.append(sid))
    # now = started_at + 10s
    import datetime
    base = datetime.datetime(2026, 6, 21, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    ct.gc_drained_members(cfg, slug, now=base + 10)
    assert suspended == []  # too fresh — could be mid-bringup


def test_gc_drained_members_ignores_non_constant_team(cfg_slug, monkeypatch):
    cfg, slug, _ = cfg_slug
    _setup_uf(cfg, slug, inbox_lines=5, cursor=5)
    suspended: list[str] = []
    monkeypatch.setattr(S, "list_sessions",
                        lambda c, s: [_member_row("S-x-dev-p1", owner="alexey",
                                                  window="dev", initiative="other.md")])
    monkeypatch.setattr(S, "suspend", lambda c, s, sid: suspended.append(sid))
    ct.gc_drained_members(cfg, slug, now=2_000_000_000.0)
    assert suspended == []  # a real dev is never reaped by this


# --- T-0345: the parallel-session cap is backpressure, not an ERROR ----------

def test_spawn_member_treats_cap_as_quiet_backpressure(cfg_slug, monkeypatch, caplog):
    """At 15/15 the constant tick fired an ERROR+traceback every 60s. The cap is
    normal backpressure — defer quietly (return None, no ERROR log)."""
    cfg, slug, _ = cfg_slug
    from bot_squad_worker.actions import ActionError

    def _capped(*a, **k):
        raise ActionError(
            "spawn: capacity reached — 15/15 parallel sessions live "
            "(max_parallel_sessions cap); spawn refused, task stays pending")

    monkeypatch.setattr(S, "spawn", _capped)
    with caplog.at_level("DEBUG"):
        sid = ct._spawn_member(cfg, slug, window="user-feedback",
                               init_filename="user-feedback.md", brief="b")
    assert sid is None
    assert not [r for r in caplog.records if r.levelname == "ERROR"]  # no spam


def test_spawn_member_still_errors_on_a_real_failure(cfg_slug, monkeypatch, caplog):
    """A genuine spawn fault (not the cap) still logs ERROR — we only quiet the cap."""
    cfg, slug, _ = cfg_slug

    def _boom(*a, **k):
        raise RuntimeError("tmux new-window failed")

    monkeypatch.setattr(S, "spawn", _boom)
    with caplog.at_level("ERROR"):
        sid = ct._spawn_member(cfg, slug, window="user-feedback",
                               init_filename="user-feedback.md", brief="b")
    assert sid is None
    assert any(r.levelname == "ERROR" for r in caplog.records)


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


def test_always_on_brief_is_keepalive_not_self_archive():
    """T-0335 item-17: an always-on (no-consume) keep-alive team has no queue to
    drain, so its brief must NOT tell the session to auto-archive when drained —
    it must persist as a standing loop. The drained→archive rule is correct only
    for demand-driven (glob/log) teams."""
    brief = ct._compose_brief(
        name="dogfood-loop", mission="continuously dogfood the product",
        role="dev", items=[], triage_prompt="", consume_kind="none",
    )
    low = brief.lower()
    assert "auto-archive" not in low and "auto archives" not in low
    assert "standing" in low or "keep-alive" in low or "do not self-archive" in low


def test_demand_driven_brief_still_self_archives():
    """The glob/log brief keeps the drained→auto-archive rule (regression guard)."""
    brief = ct._compose_brief(
        name="prod-support", mission="triage alerts", role="dev",
        items=["_alerts/a.md"], triage_prompt="", consume_kind="glob",
    )
    assert "auto-archive" in brief.lower()


def test_finished_initiative_skipped(cfg_slug):
    """T-0335 item-16: a constant-team initiative whose basename is in
    ``vision/finished_initiatives`` is skipped by tick() — no re-staffing even
    with pending work, so a shipped initiative stops resurrecting its team."""
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "prod-support",
                      {"constant_team": "true", "team_size": 1, "consume": "_alerts/*.md"})
    alerts = cfg.data_dir / slug / "_alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / "alert-1.md").write_text("# DB latency spike\n500ms p99")

    # Mark it finished (basename-with-.md, the format _read_finished writes).
    fin = cfg.data_dir / slug / "vision" / "finished_initiatives"
    fin.write_text("prod-support.md\n")

    res = ct.tick(cfg, slug)
    assert spawns == []
    assert res["actions"] == []


def test_finished_initiative_skip_strips_prefix(cfg_slug):
    """The finished file may carry the legacy ``initiatives/`` prefix; tick()
    must still match it against the bare basename."""
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "prod-support",
                      {"constant_team": "true", "team_size": 1, "consume": "_alerts/*.md"})
    alerts = cfg.data_dir / slug / "_alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / "alert-1.md").write_text("boom")

    fin = cfg.data_dir / slug / "vision" / "finished_initiatives"
    fin.write_text("initiatives/prod-support.md\n")

    ct.tick(cfg, slug)
    assert spawns == []


def test_unfinished_initiative_still_spawns_with_finished_file_present(cfg_slug):
    """A different initiative listed as finished must not suppress an active one."""
    cfg, slug, spawns = cfg_slug
    _write_initiative(cfg, slug, "prod-support",
                      {"constant_team": "true", "team_size": 1, "consume": "_alerts/*.md"})
    alerts = cfg.data_dir / slug / "_alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / "alert-1.md").write_text("boom")

    fin = cfg.data_dir / slug / "vision" / "finished_initiatives"
    fin.write_text("some-other-initiative.md\n")

    ct.tick(cfg, slug)
    assert len(spawns) == 1


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


def test_read_frontmatter_strips_yaml_quotes(cfg_slug, tmp_path):
    """T-0200: a YAML-quoted scalar must not keep its quotes — a stray `"` in
    `name`/`team_window` is the literal-quote escape bug's origin."""
    p = tmp_path / "quoted.md"
    p.write_text(
        '---\n'
        'name: "prod-support"\n'
        "team_window: 'prod-support'\n"
        'team_size: 1\n'
        '---\n\n# body\n'
    )
    fm = ct._read_frontmatter(p)
    assert fm["name"] == "prod-support"
    assert fm["team_window"] == "prod-support"
    assert fm["team_size"] == "1"
