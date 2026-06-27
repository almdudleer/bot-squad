"""Tests for the unified read-only transparency surface (T-0511 / M11-F11.4).

ONE endpoint aggregates operator-state-doc + session tree + backlog + quota so a
fresh operator/stakeholder can read the whole system state without talking to the
operator. The surface is strictly READ-ONLY.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client_logged_in(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


def _anon_client(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Shape + aggregation
# ---------------------------------------------------------------------------

def test_transparency_aggregates_the_four_surfaces(tmp_bot_squad: Path, monkeypatch):
    proj = tmp_bot_squad / "data" / "test-project"
    # operator state-doc (T-0473 artifact)
    (proj / "artifacts").mkdir(parents=True, exist_ok=True)
    (proj / "artifacts" / "operator-state.md").write_text(
        "# Operator state — test-project\n\n## Priorities\n\nShip the thing.\n"
    )
    # backlog
    (proj / "backlog" / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: in_progress\ninitiative: x.md\n---\n\nbody\n"
    )
    (proj / "backlog" / "T-0002-bar.md").write_text(
        "---\nid: T-0002\ntitle: Bar\nstatus: closed\n---\n\nbody\n"
    )
    # quota / pace (mirror the worker pace.json layout)
    pace_dir = proj / "_worker" / "pace"
    pace_dir.mkdir(parents=True, exist_ok=True)
    (pace_dir / "pace.json").write_text(
        '{"max_in_progress": 13, "initiatives": {"x.md": {"weight": 2.0, "priority": 5}}}'
    )

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/transparency")
    assert r.status_code == 200
    body = r.json()

    # All four surfaces present.
    assert set(body) >= {"operator_state", "sessions", "backlog", "quota"}

    # Operator state-doc surfaced verbatim.
    assert body["operator_state"]["exists"] is True
    assert "Ship the thing." in body["operator_state"]["content"]
    assert body["operator_state"]["path"].endswith("operator-state.md")

    # Backlog: counts + tasks.
    assert body["backlog"]["counts"]["in_progress"] == 1
    assert body["backlog"]["counts"]["closed"] == 1
    ids = {t["id"] for t in body["backlog"]["tasks"]}
    assert ids == {"T-0001", "T-0002"}

    # Quota: max-in-progress + derived in_progress count + pause + initiatives.
    assert body["quota"]["max_in_progress"] == 13
    assert body["quota"]["in_progress"] == 1
    assert body["quota"]["paused"] is False
    assert body["quota"]["initiatives"]["x.md"]["weight"] == 2.0

    # Sessions: present (list; empty when no worker is reachable in tests).
    assert isinstance(body["sessions"], list)


def test_transparency_degrades_when_state_doc_absent(tmp_bot_squad: Path, monkeypatch):
    # Fresh project: no operator-state.md, no pace.json yet.
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/transparency")
    assert r.status_code == 200
    body = r.json()
    assert body["operator_state"]["exists"] is False
    assert body["operator_state"]["content"] is None
    # Defaults: unlimited (0) max, not paused, no initiatives.
    assert body["quota"]["max_in_progress"] == 0
    assert body["quota"]["paused"] is False
    assert body["quota"]["initiatives"] == {}
    assert body["backlog"]["tasks"] == []


def test_transparency_reflects_global_pause(tmp_bot_squad: Path, monkeypatch):
    proj = tmp_bot_squad / "data" / "test-project"
    # Mirror operator_redrive's pause-flag layout (presence = paused).
    ord_dir = proj / "_worker" / "operator_redrive"
    ord_dir.mkdir(parents=True, exist_ok=True)
    (ord_dir / "operator_paused.flag").write_text('{"paused_by": "user", "reason": "lunch"}')

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/transparency")
    assert r.status_code == 200
    assert r.json()["quota"]["paused"] is True


def test_transparency_skips_unparseable_backlog(tmp_bot_squad: Path, monkeypatch):
    proj = tmp_bot_squad / "data" / "test-project"
    (proj / "backlog" / "T-bad.md").write_text("no frontmatter")
    (proj / "backlog" / "T-0003-ok.md").write_text(
        "---\nid: T-0003\ntitle: OK\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/transparency")
    assert r.status_code == 200
    tasks = r.json()["backlog"]["tasks"]
    assert [t["id"] for t in tasks] == ["T-0003"]


def test_transparency_unknown_project_404(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/nope/transparency")
    assert r.status_code == 404


def test_transparency_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/transparency")
    assert r.status_code == 401
