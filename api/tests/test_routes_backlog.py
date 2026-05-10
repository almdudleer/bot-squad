from pathlib import Path

import pytest
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
# Existing read tests
# ---------------------------------------------------------------------------

def test_backlog_returns_parsed_tasks(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    (backlog / "T-0002-bar.md").write_text(
        "---\nid: T-0002\ntitle: Bar\nstatus: closed\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 2
    by_id = {t["id"]: t for t in tasks}
    assert by_id["T-0001"]["status"] == "open"
    assert by_id["T-0002"]["title"] == "Bar"


def test_backlog_skips_unparseable(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-bad.md").write_text("no frontmatter")
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    # Bad files are reported but don't 500.
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/backlog
# ---------------------------------------------------------------------------

def test_create_task_happy_path(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "New task", "body": "details", "status": "open"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "New task"
    assert data["id"].startswith("T-")
    assert data["status"] == "open"


def test_create_task_default_status_open(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "Minimal"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def test_create_task_empty_title_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "   "},
        )
    assert r.status_code == 400


def test_create_task_invalid_status_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "X", "status": "bad-status"},
        )
    assert r.status_code == 400


def test_create_task_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "X"},
        )
    assert r.status_code == 401


def test_create_task_allocates_sequential_ids(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0005-existing.md").write_text(
        "---\nid: T-0005\ntitle: E\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "Next"},
        )
    assert r.status_code == 200
    assert r.json()["id"] == "T-0006"


# ---------------------------------------------------------------------------
# PATCH /api/projects/{slug}/backlog/{id}
# ---------------------------------------------------------------------------

def test_patch_task_status(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "closed"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "closed"


def test_patch_task_title(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Old\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"title": "New title"},
        )
    assert r.status_code == 200
    assert r.json()["title"] == "New title"


def test_patch_task_not_found(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-9999",
            json={"status": "closed"},
        )
    assert r.status_code == 404


def test_patch_task_invalid_id_format(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/not-a-task-id",
            json={"status": "closed"},
        )
    assert r.status_code == 400


def test_patch_task_invalid_status(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "invalid"},
        )
    assert r.status_code == 400


def test_patch_task_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "closed"},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/projects/{slug}/backlog/{id}
# ---------------------------------------------------------------------------

def test_delete_task_happy_path(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.delete("/api/projects/test-project/backlog/T-0001")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["deleted_id"] == "T-0001"
    assert not (backlog / "T-0001-foo.md").exists()


def test_delete_task_not_found(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.delete("/api/projects/test-project/backlog/T-9999")
    assert r.status_code == 404


def test_delete_task_invalid_id_format(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.delete("/api/projects/test-project/backlog/NOT_VALID")
    assert r.status_code == 400


def test_delete_task_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.delete("/api/projects/test-project/backlog/T-0001")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/backlog/{id}/comments
# ---------------------------------------------------------------------------

def test_add_comment_happy_path(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-0001/comments",
            json={"body": "great comment"},
        )
    assert r.status_code == 200
    task = r.json()
    assert "great comment" in task["body"]
    assert "## Comments" in task["body"]


def test_add_comment_empty_body_rejected(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-0001/comments",
            json={"body": "   "},
        )
    assert r.status_code == 400


def test_add_comment_task_not_found(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-9999/comments",
            json={"body": "hi"},
        )
    assert r.status_code == 404


def test_add_comment_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-0001/comments",
            json={"body": "hi"},
        )
    assert r.status_code == 401


def test_add_comment_uses_jwt_username(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-0001/comments",
            json={"body": "authored comment"},
        )
    assert r.status_code == 200
    assert "testuser" in r.json()["body"]
