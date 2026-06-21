"""T-0147 — product-analytics endpoint tests."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client_logged_in(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


def _anon_client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


def _seed(tmp_bot_squad: Path) -> None:
    data = tmp_bot_squad / "data" / "test-project"
    backlog = data / "backlog"
    sessions = data / "sessions"
    processed = data / "_jobs" / "deploy" / "processed"
    processed.mkdir(parents=True, exist_ok=True)

    # tickets: 2 closed (one with created→updated for MTTR), 1 open
    (backlog / "T-0001-a.md").write_text(
        "---\nid: T-0001\ntitle: A\nstatus: closed\n"
        "created: 2026-05-20T00:00:00Z\nupdated: 2026-05-22T00:00:00Z\n---\n\nbody\n"
    )
    (backlog / "T-0002-b.md").write_text(
        "---\nid: T-0002\ntitle: B\nstatus: closed\nupdated: 2026-05-21T00:00:00Z\n---\n\nbody\n"
    )
    (backlog / "T-0003-c.md").write_text(
        "---\nid: T-0003\ntitle: C\nstatus: open\n---\n\nbody\n"
    )

    # sessions: 2 with started_at, varying status + one archived
    (sessions / "S-x-1.md").write_text(
        "---\nsid: S-x-1\nstatus: active\nstarted_at: 2026-05-20T10:00:00Z\narchived: false\n---\n"
    )
    (sessions / "S-x-2.md").write_text(
        "---\nsid: S-x-2\nstatus: suspended\nstarted_at: 2026-05-21T10:00:00Z\narchived: true\n---\n"
    )

    # deploys: 2 ok, 1 fail (epoch-ms filenames)
    (processed / "1778783881367-aaa.ok").write_text("")
    (processed / "1778790786496-bbb.ok").write_text("")
    (processed / "1778785872678-ccc.fail.6").write_text("")


def test_analytics_requires_auth(tmp_bot_squad, monkeypatch):
    client = _anon_client(tmp_bot_squad, monkeypatch)
    r = client.get("/api/projects/test-project/analytics")
    assert r.status_code == 401


def test_analytics_unknown_project_404(tmp_bot_squad, monkeypatch):
    client = _client_logged_in(tmp_bot_squad, monkeypatch)
    r = client.get("/api/projects/nope/analytics")
    assert r.status_code == 404


def test_analytics_aggregates(tmp_bot_squad, monkeypatch):
    _seed(tmp_bot_squad)
    client = _client_logged_in(tmp_bot_squad, monkeypatch)
    r = client.get("/api/projects/test-project/analytics")
    assert r.status_code == 200
    body = r.json()

    assert body["slug"] == "test-project"
    assert body["sessions"]["total"] == 2
    assert body["sessions"]["archived"] == 1
    assert body["sessions"]["by_status"] == {"active": 1, "suspended": 1}

    assert body["tickets"]["total"] == 3
    assert body["tickets"]["by_status"]["closed"] == 2
    assert body["tickets"]["by_status"]["open"] == 1
    # Only T-0001 has both created + updated → exactly one MTTR sample (2 days).
    assert body["tickets"]["time_to_close"]["closed_measured"] == 1
    assert body["tickets"]["time_to_close"]["mean_days"] == 2.0

    assert body["deploys"]["total"] == 3
    assert body["deploys"]["ok"] == 2
    assert body["deploys"]["fail"] == 1
    assert body["deploys"]["success_rate"] == round(2 / 3, 3)
    assert body["deploys"]["last_deploy_at"] is not None
    # T-0360: the per-widget time scopes are exposed so the FE can label each
    # widget explicitly (day window vs deploy-week window differ).
    assert body["window_days"] == 14
    assert body["deploy_weeks"] == 8


def test_analytics_empty_project_is_zeroed(tmp_bot_squad, monkeypatch):
    client = _client_logged_in(tmp_bot_squad, monkeypatch)
    r = client.get("/api/projects/test-project/analytics")
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"]["total"] == 0
    assert body["tickets"]["total"] == 0
    assert body["deploys"]["total"] == 0
    assert body["deploys"]["success_rate"] is None
    # zero-filled per-day series still spans the window
    assert len(body["sessions"]["per_day"]) == body["window_days"]
