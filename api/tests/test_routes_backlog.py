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


def test_create_and_patch_planned_status(tmp_bot_squad: Path, monkeypatch):
    # T-0058: planned is a first-class status; round-trip create → patch → patch.
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "Backlogged", "status": "planned"},
        )
        assert r.status_code == 200
        tid = r.json()["id"]
        assert r.json()["status"] == "planned"

        r = client.patch(
            f"/api/projects/test-project/backlog/{tid}",
            json={"status": "open"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "open"

        r = client.patch(
            f"/api/projects/test-project/backlog/{tid}",
            json={"status": "planned"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "planned"


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


def test_create_task_uses_shared_counter(tmp_bot_squad: Path, monkeypatch):
    """T-0174: web create now allocates from data/<slug>/_counters/task.txt —
    the SAME counter the worker's task_new uses, unifying what used to be two
    independent locks (api .lock vs worker .task-id.lock)."""
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post("/api/projects/test-project/backlog", json={"title": "One"})
        assert r.json()["id"] == "T-0001"
        counter = tmp_bot_squad / "data" / "test-project" / "_counters" / "task.txt"
        assert counter.read_text().strip() == "1"
        r = client.post("/api/projects/test-project/backlog", json={"title": "Two"})
        assert r.json()["id"] == "T-0002"
        assert counter.read_text().strip() == "2"


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


# ---------------------------------------------------------------------------
# T-0038: PATCH linkage fields (initiative / parent_task / blocked_by)
# ---------------------------------------------------------------------------

def test_patch_task_sets_initiative(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"initiative": "multi-server-installation-process.md"},
        )
    assert r.status_code == 200
    assert r.json()["initiative"] == "multi-server-installation-process.md"


def test_patch_task_clears_initiative_with_null(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n"
        "initiative: foo.md\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"initiative": None},
        )
    assert r.status_code == 200
    assert r.json().get("initiative") is None


def test_patch_task_rejects_bad_initiative_basename(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"initiative": "../etc/passwd"},
        )
    assert r.status_code == 400


def test_patch_task_sets_parent_and_blocked_by(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0010-foo.md").write_text(
        "---\nid: T-0010\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0010",
            json={"parent_task": "T-0007", "blocked_by": ["T-0001", "T-0002"]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["parent_task"] == "T-0007"
    assert body["blocked_by"] == ["T-0001", "T-0002"]


def test_patch_task_rejects_malformed_blocked_by(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0010-foo.md").write_text(
        "---\nid: T-0010\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0010",
            json={"blocked_by": ["nope"]},
        )
    assert r.status_code == 400


def test_backlog_returns_initiative_field(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n"
        "initiative: alpha.md\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    assert r.status_code == 200
    [task] = r.json()
    assert task["initiative"] == "alpha.md"


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
    # T-0121: error message must cite the canonical set so direct-file editors
    # see exactly what's allowed without grepping the source.
    detail = r.json()["detail"]
    for canon in ("planned", "open", "in_progress", "totest", "reopened", "closed"):
        assert canon in detail, f"canonical {canon!r} missing from 400 detail: {detail!r}"


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
# POST /api/projects/{slug}/backlog/{id}/comments — CUT (T-0335 item 18)
# ---------------------------------------------------------------------------

def test_comments_channel_is_cut(tmp_bot_squad: Path, monkeypatch):
    """T-0335 item 18: the orphaned ``## Comments`` channel is removed — the
    board comment kebab posts a Progress note instead. The route no longer
    exists (404/405, never a 200), so a stale client can't resurrect it."""
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-0001/comments",
            json={"body": "great comment"},
        )
    assert r.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Phase 7 — parsed sections + progress endpoint
# ---------------------------------------------------------------------------


def test_list_returns_parsed_sections(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nI want X.\n\n"
        "## Context\n\nctx text\n\n"
        "## Progress\n\n- 2026-05-12T16:00:00Z · S-x · started\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["verbatim"] == "I want X."
    assert t["context"] == "ctx text"
    assert "S-x · started" in t["progress"]


def test_list_legacy_body_goes_to_verbatim(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nLegacy free body.\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    t = r.json()[0]
    assert t["verbatim"] == "Legacy free body."
    assert t["context"] == ""
    assert t["progress"] == ""


def test_create_with_verbatim_request_composes_body(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "New", "verbatim_request": "I want the moon."},
        )
    assert r.status_code == 200
    t = r.json()
    assert t["verbatim"] == "I want the moon."
    assert "## Verbatim request" in t["body"]


# ---------------------------------------------------------------------------
# Phase 8 — priority field + reorder endpoint
# ---------------------------------------------------------------------------


def test_create_task_priority_default_zero_if_first(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "First task"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 0


def test_create_task_priority_default_max_open_plus_100(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-a.md").write_text(
        "---\nid: T-0001\ntitle: A\nstatus: open\npriority: 200\n---\n\nbody\n"
    )
    (backlog / "T-0002-b.md").write_text(
        "---\nid: T-0002\ntitle: B\nstatus: open\npriority: 500\n---\n\nbody\n"
    )
    # Closed task with very high priority — must be ignored for the default calc.
    (backlog / "T-0003-c.md").write_text(
        "---\nid: T-0003\ntitle: C\nstatus: closed\npriority: 9999\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "New"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 600


def test_create_task_priority_explicit(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "Explicit", "priority": 42},
        )
    assert r.status_code == 200
    assert r.json()["priority"] == 42


def test_create_task_priority_negative_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "X", "priority": -1},
        )
    assert r.status_code == 400


def test_patch_priority_succeeds(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\npriority: 100\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001/priority",
            json={"priority": 250},
        )
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 250
    # And it persisted to the file
    text = (backlog / "T-0001-foo.md").read_text()
    assert "priority: 250" in text


def test_patch_priority_rejects_negative(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001/priority",
            json={"priority": -5},
        )
    assert r.status_code == 400


def test_patch_priority_rejects_non_int(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001/priority",
            json={"priority": "high"},
        )
    assert r.status_code == 400


def test_patch_priority_task_not_found(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-9999/priority",
            json={"priority": 100},
        )
    assert r.status_code == 404


def test_patch_priority_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001/priority",
            json={"priority": 100},
        )
    assert r.status_code == 401


def test_list_returns_priority_field(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-a.md").write_text(
        "---\nid: T-0001\ntitle: A\nstatus: open\npriority: 200\n---\n\nbody\n"
    )
    (backlog / "T-0002-b.md").write_text(
        "---\nid: T-0002\ntitle: B\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    by_id = {t["id"]: t for t in r.json()}
    assert by_id["T-0001"]["priority"] == 200
    assert by_id["T-0002"]["priority"] is None


def test_progress_endpoint_proxies_to_worker(tmp_bot_squad, monkeypatch, fake_worker):
    """POST /progress proxies to worker task_progress_add."""
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nI want X.\n"
    )
    import threading
    import time
    import uvicorn
    from fastapi import FastAPI

    sock = fake_worker
    # Replace the noop-only fake_worker with one that handles task_progress_add too.
    # The fixture already started a server bound to `sock`; we replace it.
    # Simpler: spin up a second route via a dedicated lightweight app at the same UDS path.
    # But uvicorn is already on that socket. Instead, stop it and start fresh.

    # Use a separate fake worker by overriding the WORKER_SOCK env elsewhere.
    fresh_sock = tmp_bot_squad / "data" / "_sock" / "worker2.sock"
    captured: dict = {}
    app = FastAPI()

    @app.post("/actions/task_progress_add")
    def handle(params: dict | None = None) -> dict:
        captured.update(params or {})
        return {"ok": True, "task_id": (params or {}).get("task_id"), "line_appended": "stub"}

    config = uvicorn.Config(app, uds=str(fresh_sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if fresh_sock.exists():
            break
        time.sleep(0.05)

    monkeypatch.setenv("WORKER_SOCK", str(fresh_sock))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")

    from fastapi.testclient import TestClient
    from app.main import build_app
    try:
        with TestClient(build_app()) as client:
            client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
            r = client.post(
                "/api/projects/test-project/backlog/T-0001/progress",
                json={"sid": "S-test-p1", "text": "phase-7 smoke"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert captured["slug"] == "test-project"
        assert captured["task_id"] == "T-0001"
        assert captured["sid"] == "S-test-p1"
        assert captured["text"] == "phase-7 smoke"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_progress_endpoint_404_on_unknown_task(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-9999/progress",
            json={"sid": "S-x", "text": "x"},
        )
    assert r.status_code == 404


def test_progress_endpoint_400_empty_text(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-0001/progress",
            json={"sid": "S-x", "text": "   "},
        )
    assert r.status_code == 400


def test_progress_endpoint_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog/T-0001/progress",
            json={"sid": "S-x", "text": "x"},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Phase 9: _session_map_by_task respects extras
# ---------------------------------------------------------------------------

def test_session_map_by_task_includes_primary(tmp_bot_squad: Path):
    """Baseline: primary task_id maps to the session row."""
    from app.routes_backlog import _session_map_by_task
    sessions = tmp_bot_squad / "data" / "test-project" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "S-u-w-p1.md").write_text(
        "---\nsid: S-u-w-p1\nstatus: active\ntask_id: T-0001\n"
        "extra_task_ids: []\n---\n"
    )
    out = _session_map_by_task(sessions)
    assert out == {"T-0001": {"sid": "S-u-w-p1", "status": "active"}}


def test_session_map_by_task_includes_extras(tmp_bot_squad: Path):
    """Phase 9: extras from extra_task_ids surface the same session row."""
    from app.routes_backlog import _session_map_by_task
    sessions = tmp_bot_squad / "data" / "test-project" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "S-u-w-p1.md").write_text(
        "---\nsid: S-u-w-p1\nstatus: active\ntask_id: T-0001\n"
        "extra_task_ids: [T-0002, T-0003]\n---\n"
    )
    out = _session_map_by_task(sessions)
    assert out == {
        "T-0001": {"sid": "S-u-w-p1", "status": "active"},
        "T-0002": {"sid": "S-u-w-p1", "status": "active"},
        "T-0003": {"sid": "S-u-w-p1", "status": "active"},
    }


def test_session_map_by_task_active_beats_paused_for_extras(tmp_bot_squad: Path):
    from app.routes_backlog import _session_map_by_task
    sessions = tmp_bot_squad / "data" / "test-project" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    # Active session has T-0002 as primary.
    (sessions / "S-u-w-p1.md").write_text(
        "---\nsid: S-u-w-p1\nstatus: active\ntask_id: T-0002\n---\n"
    )
    # Paused session lists T-0002 as an extra — must NOT override active.
    (sessions / "S-u-w-p2.md").write_text(
        "---\nsid: S-u-w-p2\nstatus: paused\ntask_id: T-0009\n"
        "extra_task_ids: [T-0002]\n---\n"
    )
    out = _session_map_by_task(sessions)
    assert out["T-0002"]["sid"] == "S-u-w-p1"
    assert out["T-0002"]["status"] == "active"


# ---------------------------------------------------------------------------
# T-0080: owner stamping on create
# ---------------------------------------------------------------------------

def test_create_task_stamps_owner_from_username(tmp_bot_squad: Path, monkeypatch):
    """POST /backlog stamps `owner: <username>` so per-user task scoping
    (deferred) has the data without a backfill."""
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/backlog",
            json={"title": "owned task"},
        )
    assert r.status_code == 200
    tid = r.json()["id"]
    md = tmp_bot_squad / "data" / "test-project" / "backlog"
    matches = list(md.glob(f"{tid}-*.md"))
    assert matches, "task md not created"
    text = matches[0].read_text()
    assert "owner: testuser" in text


# ---------------------------------------------------------------------------
# T-0289: PATCH must NOT let a body replace clobber '## Verbatim request'
# (verbatim is human-only — re-graft the original on any body write).
# ---------------------------------------------------------------------------
def _create_with_body(client, body: str) -> str:
    r = client.post(
        "/api/projects/test-project/backlog",
        json={"title": "Guard me", "body": body, "status": "open"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_patch_body_cannot_overwrite_verbatim(tmp_bot_squad: Path, monkeypatch):
    client = _client_logged_in(tmp_bot_squad, monkeypatch)
    original = (
        "## Verbatim request\n\nORIGINAL stakeholder words\n\n"
        "## Context\n\nold context\n"
    )
    tid = _create_with_body(client, original)

    # A body PATCH that rewrites verbatim AND edits context.
    tampered = (
        "## Verbatim request\n\nHIJACKED by an agent\n\n"
        "## Context\n\nnew context\n"
    )
    r = client.patch(
        f"/api/projects/test-project/backlog/{tid}", json={"body": tampered}
    )
    assert r.status_code == 200, r.text
    got = r.json()
    # verbatim is preserved (re-grafted); the context edit still lands.
    assert "ORIGINAL stakeholder words" in got["verbatim"]
    assert "HIJACKED" not in got["verbatim"]
    assert "new context" in got["context"]


def test_patch_body_preserves_verbatim_when_omitted(tmp_bot_squad: Path, monkeypatch):
    """A body that drops the verbatim heading entirely must not lose it."""
    client = _client_logged_in(tmp_bot_squad, monkeypatch)
    tid = _create_with_body(
        client, "## Verbatim request\n\nKEEP THIS\n\n## Context\n\nc\n"
    )
    r = client.patch(
        f"/api/projects/test-project/backlog/{tid}",
        json={"body": "## Context\n\nonly context now\n"},
    )
    assert r.status_code == 200, r.text
    assert "KEEP THIS" in r.json()["verbatim"]


def test_patch_body_non_canonical_sections_survive(tmp_bot_squad: Path, monkeypatch):
    """Re-grafting verbatim must not mangle non-canonical sections (Finding/
    DoD on QA tickets): a legit DoD edit lands, verbatim stays original."""
    client = _client_logged_in(tmp_bot_squad, monkeypatch)
    original = (
        "## Verbatim request\n\nSACRED\n\n"
        "## Finding\n\na bug\n\n## DoD\n\nold dod\n"
    )
    tid = _create_with_body(client, original)
    edited = (
        "## Verbatim request\n\nTAMPER\n\n"
        "## Finding\n\na bug\n\n## DoD\n\nnew dod text\n"
    )
    r = client.patch(
        f"/api/projects/test-project/backlog/{tid}", json={"body": edited}
    )
    assert r.status_code == 200, r.text
    # The non-canonical sections (Finding/DoD) parse into `verbatim` since they
    # aren't recognized headings — assert the original verbatim words survive,
    # the tamper is dropped, and the edited DoD + Finding content is intact.
    verbatim = r.json()["verbatim"]
    assert "SACRED" in verbatim and "TAMPER" not in verbatim
    assert "new dod text" in verbatim
    assert "## Finding" in verbatim
