"""T-0295 (b): GET /api/projects/<slug>/constant-teams — the read-only
constant-team tick-state surface.

Guards the two things that make this endpoint honest rather than merely
present:
  * it reports the state the WORKER TICK actually reads/writes (config from
    ``vision/initiatives/*.md`` frontmatter, state from
    ``_worker/constant_teams/<name>.json``), not a plausible-looking summary;
  * a team the tick refuses to staff (retired / finished / no consume source)
    says so, instead of rendering as a healthy team that merely has 0 members.
"""
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _logged_in(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    c = TestClient(build_app())
    c.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return c


def _anon(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


def _project(tmp_bot_squad: Path) -> Path:
    return tmp_bot_squad / "data" / "test-project"


def _write_team(tmp_bot_squad: Path, stem: str, **fm) -> Path:
    init_dir = _project(tmp_bot_squad) / "vision" / "initiatives"
    init_dir.mkdir(parents=True, exist_ok=True)
    lines = "".join(f"{k}: {v}\n" for k, v in fm.items())
    p = init_dir / f"{stem}.md"
    p.write_text(f"---\nconstant_team: true\n{lines}---\n\nmission text\n", encoding="utf-8")
    return p


def _write_state(tmp_bot_squad: Path, name: str, state: dict) -> Path:
    d = _project(tmp_bot_squad) / "_worker" / "constant_teams"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(state), encoding="utf-8")
    return p


def _get(tmp_bot_squad: Path, monkeypatch) -> dict:
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.get("/api/projects/test-project/constant-teams")
    assert r.status_code == 200, r.text
    return r.json()


def _team(payload: dict, name: str) -> dict:
    return next(t for t in payload["teams"] if t["name"] == name)


# ---------------------------------------------------------------------------
# Shape / auth
# ---------------------------------------------------------------------------

def test_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon(tmp_bot_squad, monkeypatch) as c:
        assert c.get("/api/projects/test-project/constant-teams").status_code == 401


def test_unknown_project_404s(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        assert c.get("/api/projects/nope/constant-teams").status_code == 404


def test_no_teams_returns_empty_list_not_error(tmp_bot_squad: Path, monkeypatch):
    """The live install's current state: no constant-team config at all. The
    caller must get an empty list plus WHERE we looked — not a 404 or a crash."""
    payload = _get(tmp_bot_squad, monkeypatch)
    assert payload["teams"] == []
    assert "vision/initiatives" in payload["config_dir"]


def test_non_constant_initiative_is_not_listed(tmp_bot_squad: Path, monkeypatch):
    init_dir = _project(tmp_bot_squad) / "vision" / "initiatives"
    init_dir.mkdir(parents=True, exist_ok=True)
    (init_dir / "ui-polish.md").write_text("---\nname: ui-polish\n---\n\nbody\n")
    assert _get(tmp_bot_squad, monkeypatch)["teams"] == []


# ---------------------------------------------------------------------------
# Config + tick state join
# ---------------------------------------------------------------------------

def test_log_team_reports_queue_depth_past_cursor(tmp_bot_squad: Path, monkeypatch):
    """`.log` consume: pending = NON-BLANK lines past the persisted cursor —
    the tick's own definition (`_log_new_lines`). Counting all lines instead
    would report perpetual backlog on a fully-drained queue."""
    _write_team(tmp_bot_squad, "triage", name="triage", team_size=2,
                consume="feedback/inbox.log")
    log = _project(tmp_bot_squad) / "feedback" / "inbox.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("one\ntwo\n\nthree\nfour\n", encoding="utf-8")
    _write_state(tmp_bot_squad, "triage", {"cursor_lines": 3, "last_spawn_at": 1780369372.5})

    t = _team(_get(tmp_bot_squad, monkeypatch), "triage")
    assert t["consume_kind"] == "log"
    assert t["queue_depth"] == 2          # "three", "four" — blank line ignored
    assert t["cursor_lines"] == 3
    assert t["team_size"] == 2
    assert t["last_spawn_at"] == 1780369372.5
    assert t["last_spawn_iso"].endswith("Z")
    assert t["state_present"] is True
    assert t["staffable"] is True
    assert t["not_staffable_reason"] is None


def test_glob_team_reports_matching_file_count(tmp_bot_squad: Path, monkeypatch):
    _write_team(tmp_bot_squad, "alerts", name="alerts", consume="_alerts/*.md")
    alerts = _project(tmp_bot_squad) / "_alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / "a.md").write_text("a")
    (alerts / "b.md").write_text("b")
    (alerts / "ignored.txt").write_text("c")

    t = _team(_get(tmp_bot_squad, monkeypatch), "alerts")
    assert t["consume_kind"] == "glob"
    assert t["queue_depth"] == 2
    assert t["cursor_lines"] is None  # cursor is a log-mode concept only


def test_state_file_keyed_by_team_name_not_file_stem(tmp_bot_squad: Path, monkeypatch):
    """The worker keys its state file on the frontmatter `name`, not the file
    stem. Reading the wrong file would silently report 'never spawned'."""
    _write_team(tmp_bot_squad, "some-file-stem", name="realname", consume="q/*.md")
    _write_state(tmp_bot_squad, "realname", {"last_spawn_at": 1780369372.0})

    t = _team(_get(tmp_bot_squad, monkeypatch), "realname")
    assert t["stem"] == "some-file-stem"
    assert t["state_file"] == "_worker/constant_teams/realname.json"
    assert t["last_spawn_at"] == 1780369372.0


def test_missing_state_reports_never_spawned(tmp_bot_squad: Path, monkeypatch):
    _write_team(tmp_bot_squad, "fresh", name="fresh", consume="q/*.md")
    t = _team(_get(tmp_bot_squad, monkeypatch), "fresh")
    assert t["state_present"] is False
    assert t["last_spawn_at"] is None
    assert t["last_spawn_iso"] is None


def test_window_prefix_defaults_to_name(tmp_bot_squad: Path, monkeypatch):
    """window_prefix is the FE's fallback join key onto live sessions — it must
    mirror the worker's `(team_window or name)[:40]`."""
    _write_team(tmp_bot_squad, "a", name="a", consume="q/*.md")
    assert _team(_get(tmp_bot_squad, monkeypatch), "a")["window_prefix"] == "a"
    _write_team(tmp_bot_squad, "b", name="b", team_window="custom-win", consume="q/*.md")
    assert _team(_get(tmp_bot_squad, monkeypatch), "b")["window_prefix"] == "custom-win"


# ---------------------------------------------------------------------------
# "The tick will not staff this" — the cases a bare member count would hide
# ---------------------------------------------------------------------------

def test_no_consume_source_is_not_staffable(tmp_bot_squad: Path, monkeypatch):
    """T-0457: the always-on (no-consume) mode is retired; the tick warns and
    spawns nothing. Reporting it as a healthy 0-member team would be a lie."""
    _write_team(tmp_bot_squad, "noconsume", name="noconsume")
    t = _team(_get(tmp_bot_squad, monkeypatch), "noconsume")
    assert t["staffable"] is False
    assert "consume" in t["not_staffable_reason"]
    assert t["queue_depth"] is None


def test_retired_team_flagged(tmp_bot_squad: Path, monkeypatch):
    """Audit item 8 denylist: even a re-seeded config is never staffed."""
    _write_team(tmp_bot_squad, "user-feedback", name="user-feedback",
                consume="feedback/inbox.log")
    t = _team(_get(tmp_bot_squad, monkeypatch), "user-feedback")
    assert t["retired"] is True
    assert t["staffable"] is False


def test_finished_initiative_is_not_staffable(tmp_bot_squad: Path, monkeypatch):
    """T-0335 item-16: a finished initiative stops being re-staffed."""
    _write_team(tmp_bot_squad, "shipped", name="shipped", consume="q/*.md")
    (_project(tmp_bot_squad) / "vision" / "finished_initiatives").write_text(
        "initiatives/shipped.md\n", encoding="utf-8")
    t = _team(_get(tmp_bot_squad, monkeypatch), "shipped")
    assert t["finished"] is True
    assert t["staffable"] is False


# ---------------------------------------------------------------------------
# Orphan state — the relics actually sitting on the live install
# ---------------------------------------------------------------------------

def test_orphan_state_file_is_surfaced_not_dropped(tmp_bot_squad: Path, monkeypatch):
    """A state json whose initiative file is gone (exactly what the live
    install has for prod-support/user-feedback post-T-0480). It is reported as
    unconfigured rather than hidden — a json on disk no surface mentions is the
    blind spot this endpoint exists to close."""
    _write_state(tmp_bot_squad, "prod-support", {"last_spawn_at": time.time()})
    t = _team(_get(tmp_bot_squad, monkeypatch), "prod-support")
    assert t["configured"] is False
    assert t["config_source"] is None
    assert t["retired"] is True
    assert t["staffable"] is False
    assert t["team_size"] is None


def test_configured_team_is_not_double_listed_as_orphan(tmp_bot_squad: Path, monkeypatch):
    _write_team(tmp_bot_squad, "dual", name="dual", consume="q/*.md")
    _write_state(tmp_bot_squad, "dual", {"last_spawn_at": 1.0})
    payload = _get(tmp_bot_squad, monkeypatch)
    assert [t["name"] for t in payload["teams"]] == ["dual"]
    assert payload["teams"][0]["configured"] is True


def test_corrupt_state_json_degrades_to_unknown(tmp_bot_squad: Path, monkeypatch):
    """A half-written json must not 500 the whole page."""
    _write_team(tmp_bot_squad, "broken", name="broken", consume="q/*.md")
    d = _project(tmp_bot_squad) / "_worker" / "constant_teams"
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    t = _team(_get(tmp_bot_squad, monkeypatch), "broken")
    assert t["last_spawn_at"] is None
    assert t["state_present"] is True
