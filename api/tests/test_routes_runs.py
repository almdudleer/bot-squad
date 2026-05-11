"""Tests for /api/projects/{slug}/runs endpoints."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_deploy_dirs(data_dir: Path, slug: str) -> dict[str, Path]:
    base = data_dir / slug / "_jobs" / "deploy"
    dirs = {
        "queue": base / "queue",
        "processing": base / "processing",
        "processed": base / "processed",
        "runs": base / "runs",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _write_queue_file(queue_dir: Path, queue_id: str, target: str = "staging",
                       reason: str = "test", requested_by: str = "pytest",
                       queued_at: float | None = None) -> Path:
    if queued_at is None:
        queued_at = time.time()
    ts_ms = int(queued_at * 1000)
    filename = f"{ts_ms}-{queue_id}.json"
    payload = {
        "queue_id": queue_id,
        "slug": "test-project",
        "target": target,
        "reason": reason,
        "requested_by": requested_by,
        "queued_at": queued_at,
    }
    p = queue_dir / filename
    p.write_text(json.dumps(payload))
    return p


def _write_processed_file(processed_dir: Path, queue_id: str, target: str = "staging",
                           reason: str = "test", requested_by: str = "pytest",
                           queued_at: float | None = None, ok: bool = True,
                           rc: int = 0) -> Path:
    if queued_at is None:
        queued_at = time.time()
    ts_ms = int(queued_at * 1000)
    stem = f"{ts_ms}-{queue_id}"
    suffix = ".ok" if ok else f".fail.{rc}"
    filename = f"{stem}{suffix}"
    payload = {
        "queue_id": queue_id,
        "slug": "test-project",
        "target": target,
        "reason": reason,
        "requested_by": requested_by,
        "queued_at": queued_at,
    }
    p = processed_dir / filename
    p.write_text(json.dumps(payload))
    return p


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/runs
# ---------------------------------------------------------------------------

def test_list_runs_empty(tmp_bot_squad: Path, monkeypatch):
    """Empty deploy dirs → empty list."""
    _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs")
    assert r.status_code == 200
    assert r.json() == []


def test_list_runs_queued(tmp_bot_squad: Path, monkeypatch):
    """Queued run appears with status='queued'."""
    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    _write_queue_file(dirs["queue"], "aaa-bbb-ccc")

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["status"] == "queued"
    assert data[0]["id"] == "aaa-bbb-ccc"
    assert data[0]["target"] == "staging"


def test_list_runs_processed_ok(tmp_bot_squad: Path, monkeypatch):
    """Processed ok run appears with status='ok' and rc=0."""
    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    _write_processed_file(dirs["processed"], "abc-def", ok=True)

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["status"] == "ok"
    assert data[0]["rc"] == 0


def test_list_runs_processed_fail(tmp_bot_squad: Path, monkeypatch):
    """Processed fail run appears with status='fail' and correct rc."""
    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    _write_processed_file(dirs["processed"], "abc-def", ok=False, rc=1)

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs")
    assert r.status_code == 200
    data = r.json()
    assert data[0]["status"] == "fail"
    assert data[0]["rc"] == 1


def test_list_runs_mixed_states(tmp_bot_squad: Path, monkeypatch):
    """Multiple runs in different states are all returned."""
    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    now = time.time()
    _write_queue_file(dirs["queue"], "q-id-001", queued_at=now - 300)
    _write_processed_file(dirs["processed"], "p-id-001", ok=True, queued_at=now - 200)
    _write_processed_file(dirs["processed"], "f-id-001", ok=False, rc=2, queued_at=now - 100)

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3
    # Newest-first by queued_at
    statuses = [d["status"] for d in data]
    assert "queued" in statuses
    assert "ok" in statuses
    assert "fail" in statuses


def test_list_runs_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs")
    assert r.status_code == 401


def test_list_runs_unknown_project_404(tmp_bot_squad: Path, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/no-such-project/runs")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/runs/{id}/log
# ---------------------------------------------------------------------------

def test_get_run_log_success(tmp_bot_squad: Path, monkeypatch):
    """Existing log file is returned as text/plain."""
    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    log_file = dirs["runs"] / "test-run-id.log"
    log_file.write_text("line1\nline2\n")

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs/test-run-id/log")
    assert r.status_code == 200
    assert "line1" in r.text
    assert "line2" in r.text


def test_get_run_log_missing_404(tmp_bot_squad: Path, monkeypatch):
    """Missing log file → 404."""
    _make_deploy_dirs(tmp_bot_squad / "data", "test-project")

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs/nonexistent-id/log")
    assert r.status_code == 404


def test_get_run_log_capped(tmp_bot_squad: Path, monkeypatch):
    """Log file larger than 2 MB is capped when ?full not set."""
    from app.routes_runs import _LOG_CAP_BYTES

    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    log_file = dirs["runs"] / "big-run.log"
    # Write 2.1 MB of data
    big_content = "x" * (_LOG_CAP_BYTES + 100 * 1024)
    log_file.write_bytes(big_content.encode())

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs/big-run/log")
    assert r.status_code == 200
    assert len(r.content) <= _LOG_CAP_BYTES


def test_get_run_log_full_not_capped(tmp_bot_squad: Path, monkeypatch):
    """?full=1 returns the entire log file regardless of size."""
    from app.routes_runs import _LOG_CAP_BYTES

    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    log_file = dirs["runs"] / "big-run2.log"
    big_content = "y" * (_LOG_CAP_BYTES + 50 * 1024)
    log_file.write_bytes(big_content.encode())

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs/big-run2/log?full=1")
    assert r.status_code == 200
    assert len(r.content) > _LOG_CAP_BYTES


def test_get_run_log_requires_auth(tmp_bot_squad: Path, monkeypatch):
    dirs = _make_deploy_dirs(tmp_bot_squad / "data", "test-project")
    (dirs["runs"] / "x.log").write_text("hi")

    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/runs/x/log")
    assert r.status_code == 401
