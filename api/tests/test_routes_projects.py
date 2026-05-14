from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


def _client(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    app = build_app()
    return TestClient(app)


def _login(client) -> None:
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})


@pytest.fixture
def fake_worker_with_sessions(tmp_bot_squad: Path, request):
    """Worker fake that returns a configurable list_sessions response.

    Use via indirect parametrization or override `sessions` in the closure.
    Defaults to an empty list — i.e. status='idle', status_since=None.
    """
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    sessions: list[dict] = getattr(request, "param", None) or []

    fake = FastAPI()

    @fake.post("/actions/list_sessions")
    def list_sessions(params: dict | None = None) -> dict:
        return {"sessions": sessions}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock

    server.should_exit = True
    thread.join(timeout=5)


def test_projects_list_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects")
    assert r.status_code == 401


def test_projects_list_shape_has_quick_status(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    """T-0016: each project carries the canonical quick-status fields."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body == [{
        "slug": "test-project",
        "display_name": "Test Project",
        "status": "idle",
        "status_since": None,
    }]


@pytest.mark.parametrize(
    "fake_worker_with_sessions",
    [[
        {"sid": "S-u-x-p1", "status": "active", "started_at": "2026-05-14T09:00:00Z"},
        {"sid": "S-u-y-p2", "status": "paused", "paused_at": "2026-05-14T10:00:00Z"},
    ]],
    indirect=True,
)
def test_projects_list_active_wins_as_working(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    """Any active session → status=working, with since=max(started_at)."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["status"] == "working"
    assert body[0]["status_since"] == "2026-05-14T09:00:00Z"


@pytest.mark.parametrize(
    "fake_worker_with_sessions",
    [[
        {"sid": "S-u-x-p1", "status": "paused", "paused_at": "2026-05-14T11:00:00Z"},
    ]],
    indirect=True,
)
def test_projects_list_paused_only_is_needs_input(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects")
    body = r.json()
    assert body[0]["status"] == "needs-input"
    assert body[0]["status_since"] == "2026-05-14T11:00:00Z"


def test_projects_list_dead_worker_renders_idle(tmp_bot_squad: Path, monkeypatch):
    """Partial failure mode: dead worker → empty contribution → idle pill,
    NOT a 502. T-0025's cross-server view depends on this invariant."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "broken.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    with TestClient(build_app()) as client:
        _login(client)
        r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["status"] == "idle"
    assert body[0]["status_since"] is None


def test_project_get(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/test-project")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "test-project"
    assert "counts" in body


def test_project_get_unknown_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/nope")
    assert r.status_code == 404


def test_get_repo_agents_md(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").write_text("# Agents\n")
    try:
        with _client(tmp_bot_squad, monkeypatch) as client:
            _login(client)
            r = client.get("/api/projects/test-project/repo-agents-md")
        assert r.status_code == 200
        assert r.json()["content"] == "# Agents\n"
    finally:
        (repo / "AGENTS.md").unlink(missing_ok=True)


def test_get_repo_agents_md_missing_404(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").unlink(missing_ok=True)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/test-project/repo-agents-md")
    assert r.status_code == 404


def test_get_repo_agents_md_unknown_project_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/nope/repo-agents-md")
    assert r.status_code == 404


def test_put_repo_agents_md(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").write_text("# Old\n")
    try:
        with _client(tmp_bot_squad, monkeypatch) as client:
            _login(client)
            r = client.put(
                "/api/projects/test-project/repo-agents-md",
                json={"content": "# New\n"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert (repo / "AGENTS.md").read_text() == "# New\n"
    finally:
        (repo / "AGENTS.md").unlink(missing_ok=True)


def test_put_repo_agents_md_empty_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.put(
            "/api/projects/test-project/repo-agents-md",
            json={"content": ""},
        )
    assert r.status_code == 400


def test_put_repo_agents_md_too_large_rejected(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    try:
        with _client(tmp_bot_squad, monkeypatch) as client:
            _login(client)
            r = client.put(
                "/api/projects/test-project/repo-agents-md",
                json={"content": "x" * (200 * 1024 + 1)},
            )
        assert r.status_code == 400
    finally:
        (repo / "AGENTS.md").unlink(missing_ok=True)


def test_repo_agents_md_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/repo-agents-md")
    assert r.status_code == 401
