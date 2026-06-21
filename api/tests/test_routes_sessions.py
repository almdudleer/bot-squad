"""Tests for /api/projects/{slug}/sessions endpoints."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_SESSION = {
    "sid": "S-almdudleer-spec5-p2",
    "status": "active",
    # T-0104: activity-derived enum; worker emits this alongside raw status.
    "activity": "running",
    "activity_at": 1_700_000_000.0,
    "window": "spec5",
    "cwd": "/home/almdudleer/signal_tracker_mgmt",
    "started_at": None,
    "last_prompt_at": None,
    "claude_uuid": "9d7b3153-0000-0000-0000-000000000001",
}


@pytest.fixture
def fake_worker_sessions(tmp_bot_squad: Path):
    """Fake worker that handles session management actions."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()

    @fake.post("/actions/noop")
    def noop(params: dict | None = None) -> dict:
        return {"ok": True, "ts": 99}

    @fake.post("/actions/list_sessions")
    def list_sessions(params: dict | None = None) -> dict:
        return {"sessions": [_SAMPLE_SESSION]}

    @fake.post("/actions/telemetry_get")
    def telemetry_get(params: dict | None = None) -> dict:
        return {
            "sessions": [{
                "sid": "S-almdudleer-spec5-p2",
                "context": {"tokens": 420000, "pct": 84.0, "ceiling": 500000,
                            "model": "claude-opus-4-8"},
                "memory": {"files": 3, "bytes": 4000, "tokens_est": 1000},
                "output_tokens_cum": 12345,
                "rate_limited": False,
            }],
            "quota": {
                "burn_tokens_per_hr": 7200.0,
                "projected_exhaustion_at": None,
                "throttled": False,
                "rate_limit_429": {"count": 0, "last_at": None},
            },
            "caps": {
                "max_parallel_sessions": 15,
                "effective_limit": 8,
                "live_sessions": 12,
                "max_total_tokens": 1_000_000,
                "output_since_anchor": 250_000,
            },
        }

    @fake.post("/actions/pause_session")
    def pause_session(params: dict | None = None) -> dict:
        return {"ok": True, "paused": True}

    @fake.post("/actions/suspend_session")
    def suspend_session(params: dict | None = None) -> dict:
        return {"ok": True, "suspended": True}

    @fake.post("/actions/resume_session")
    def resume_session(params: dict | None = None) -> dict:
        return {"ok": True, "sid": "S-almdudleer-spec5-p3"}

    @fake.post("/actions/spawn_session")
    def spawn_session(params: dict | None = None) -> dict:
        return {"ok": True, "sid": "S-almdudleer-new-window-p4"}

    @fake.post("/actions/peer_send")
    def peer_send(params: dict | None = None) -> dict:
        return {"ok": True, "delivered_to": [(params or {}).get("to", "")]}

    @fake.post("/actions/bind_task")
    def bind_task(params: dict | None = None) -> dict:
        p = params or {}
        return {
            "ok": True,
            "sid": p.get("sid", ""),
            "task_id": p.get("task_id", ""),
            "extras": [p.get("task_id", "")],
        }

    @fake.post("/actions/bind_initiative")
    def bind_initiative(params: dict | None = None) -> dict:
        p = params or {}
        return {
            "ok": True,
            "sid": p.get("sid", ""),
            "initiative": p.get("initiative", ""),
            "extras": [p.get("initiative", "")],
        }

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


def _client_logged_in(tmp_bot_squad: Path, monkeypatch, sock_path: Path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


def _anon_client(tmp_bot_squad: Path, monkeypatch, sock_path: Path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/telemetry  (T-0210)
# ---------------------------------------------------------------------------

def test_get_telemetry_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/telemetry")
    assert r.status_code == 200
    data = r.json()
    assert data["sessions"][0]["context"]["tokens"] == 420000
    assert data["sessions"][0]["context"]["pct"] == 84.0
    assert data["quota"]["burn_tokens_per_hr"] == 7200.0
    assert data["quota"]["throttled"] is False
    # T-0335 items 7 + 22: the enforced caps block passes through to the UI meter.
    assert data["caps"]["max_parallel_sessions"] == 15
    assert data["caps"]["effective_limit"] == 8
    assert data["caps"]["live_sessions"] == 12
    assert data["caps"]["output_since_anchor"] == 250_000


def test_get_telemetry_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/telemetry")
    assert r.status_code == 401


def test_get_telemetry_unknown_project_404(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/no-such-project/telemetry")
    assert r.status_code == 404


def test_get_telemetry_dead_worker_returns_empty(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "nonexistent.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.get("/api/projects/test-project/telemetry")
    assert r.status_code == 200
    assert r.json() == {"sessions": [], "quota": {}, "caps": {}}


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/sessions
# ---------------------------------------------------------------------------

def test_list_sessions_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["sid"] == "S-almdudleer-spec5-p2"
    assert data[0]["status"] == "active"
    # T-0104: derived activity field passes through unchanged.
    assert data[0]["activity"] == "running"
    assert data[0]["activity_at"] == 1_700_000_000.0
    # T-0232: explicit `live` boolean (running|idle = alive) for the FE
    # live-only view. running → live.
    assert data[0]["live"] is True


def test_is_live_maps_running_and_idle_only():
    # T-0232: live = the session is alive in tmux (running or idle). paused +
    # suspended (and any archived → suspended) are NOT live → dropped from view.
    from app.routes_sessions import _is_live
    assert _is_live("running") is True
    assert _is_live("idle") is True
    assert _is_live("paused") is False
    assert _is_live("suspended") is False
    assert _is_live("") is False
    assert _is_live(None) is False


def test_list_sessions_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 401


def test_list_sessions_unknown_project_404(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/no-such-project/sessions")
    assert r.status_code == 404


def test_list_sessions_dead_worker_returns_empty_list(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """Phase 2 fan-out: a dead worker doesn't 502 the whole list — it logs and skips."""
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/pause
# ---------------------------------------------------------------------------

def test_pause_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pause")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "paused": True}


def test_pause_session_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pause")
    assert r.status_code == 401


def test_pause_session_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pause")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# POST / DELETE /api/projects/{slug}/sessions/{sid}/pin  (T-0437)
# ---------------------------------------------------------------------------

def test_pin_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["pinned"] is True
    assert data["sid"] == "S-almdudleer-spec5-p2"
    assert data["pinned_by"] == "testuser"
    assert data["pinned_at"]  # iso timestamp stamped


def test_pin_then_list_stamps_pinned(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    """A pinned session surfaces with pinned/pinned_by/pinned_at on the list —
    one fetch feeds both the Processes view and the Board card."""
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["sid"] == "S-almdudleer-spec5-p2"
    assert row["pinned"] is True
    assert row["pinned_by"] == "testuser"
    assert row["pinned_at"]


def test_list_unpinned_default_false(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["pinned"] is False
    assert "pinned_by" not in row


def test_unpin_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
        r = client.delete("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
        assert r.status_code == 200, r.text
        assert r.json()["pinned"] is False
        assert r.json()["was_pinned"] is True
        # And the list no longer flags it.
        row = client.get("/api/projects/test-project/sessions").json()[0]
    assert row["pinned"] is False


def test_unpin_idempotent_when_not_pinned(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.delete("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
    assert r.status_code == 200
    assert r.json()["was_pinned"] is False


def test_pin_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
    assert r.status_code == 401


def test_pin_requires_admin_403_for_nonadmin(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    """T-0381: pin is a project WRITE → gated by require_project_member
    (admin-only today). A non-admin gets 403, not a silent pin."""
    _set_auth_with_meta(tmp_bot_squad, is_admin=False, linux_user="tu")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
    assert r.status_code == 403


def test_unpin_requires_admin_403_for_nonadmin(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    _set_auth_with_meta(tmp_bot_squad, is_admin=False, linux_user="tu")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.delete("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pin")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/resume
# ---------------------------------------------------------------------------

def test_resume_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "sid" in data


def test_resume_session_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume")
    assert r.status_code == 401


def test_resume_session_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume")
    assert r.status_code == 502


def test_resume_forwards_task_id_and_prompt_to_worker(tmp_bot_squad: Path, monkeypatch):
    """T-0407: /resume forwards task_id + initial_prompt to the worker resume
    action (which adopts an empty primary as task_id, T-0166, and delivers the
    prompt) — so reusing a session onto a new task is ONE round-trip with the
    task as PRIMARY, replacing the resume + separate bind_task (which only
    appended to extra_task_ids and left the old binding primary)."""
    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"

    captured: list[dict] = []
    app = FastAPI()

    @app.post("/actions/resume_session")
    def resume(params: dict | None = None) -> dict:
        captured.append(params or {})
        return {"ok": True, "sid": "S-almdudleer-spec5-p2"}

    cfg = uvicorn.Config(app, uds=str(coord_sock), log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if coord_sock.exists():
            break
        time.sleep(0.05)
    try:
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.post(
                "/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume",
                json={"task_id": "T-0042", "initial_prompt": "Work T-0042 now."},
            )
        assert r.status_code == 200, r.text
        assert captured, "worker resume_session was not called"
        assert captured[0].get("task_id") == "T-0042"
        assert captured[0].get("initial_prompt") == "Work T-0042 now."
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_resume_bare_omits_task_id(tmp_bot_squad: Path, monkeypatch):
    """A bare resume (no body) must NOT send task_id/initial_prompt — the worker
    leaves an existing primary untouched, but we also don't want to forward
    empty keys that could confuse the allowlist."""
    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"

    captured: list[dict] = []
    app = FastAPI()

    @app.post("/actions/resume_session")
    def resume(params: dict | None = None) -> dict:
        captured.append(params or {})
        return {"ok": True, "sid": "S-almdudleer-spec5-p2"}

    cfg = uvicorn.Config(app, uds=str(coord_sock), log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if coord_sock.exists():
            break
        time.sleep(0.05)
    try:
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume")
        assert r.status_code == 200, r.text
        assert captured, "worker resume_session was not called"
        assert "task_id" not in captured[0]
        assert "initial_prompt" not in captured[0]
    finally:
        server.should_exit = True
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions  (spawn)
# ---------------------------------------------------------------------------

def test_spawn_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "sid" in data


def test_spawn_session_with_prompt(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window", "initial_prompt": "Hello Claude"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_spawn_session_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window"},
        )
    assert r.status_code == 401


def test_spawn_session_empty_window_400(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "   "},
        )
    assert r.status_code == 400


def test_spawn_session_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window"},
        )
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/dev-spawn-request
# ---------------------------------------------------------------------------

def _write_session_md(tmp_bot_squad: Path, sid: str, *, status: str = "active",
                      task_id: str = "~") -> None:
    """Drop a session md file into the test project's sessions/ dir."""
    p = tmp_bot_squad / "data" / "test-project" / "sessions" / f"{sid}.md"
    p.write_text(
        "---\n"
        f"sid: {sid}\n"
        f"status: {status}\n"
        f"task_id: {task_id}\n"
        "window: tl\n"
        "cwd: /tmp/test-repo\n"
        "claude_uuid: ~\n"
        "---\n"
    )


def test_dev_spawn_request_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    _write_session_md(tmp_bot_squad, "S-testuser-tl-p0")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/dev-spawn-request",
            json={
                "tl_sid": "S-testuser-tl-p0",
                "instructions": "Build the heatmaps feature.",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "S-testuser-tl-p0" in data["delivered_to"]


def test_dev_spawn_request_with_task(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    _write_session_md(tmp_bot_squad, "S-testuser-tl-p0")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/dev-spawn-request",
            json={
                "tl_sid": "S-testuser-tl-p0",
                "task_id": "T-0042",
                "instructions": "Pick up T-0042.",
            },
        )
    assert r.status_code == 200


def test_dev_spawn_request_unknown_tl_400(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/dev-spawn-request",
            json={"tl_sid": "S-nope", "instructions": "x"},
        )
    assert r.status_code == 400


def test_dev_spawn_request_target_is_dev_400(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    _write_session_md(tmp_bot_squad, "S-testuser-dev-p1", task_id="T-0001")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/dev-spawn-request",
            json={"tl_sid": "S-testuser-dev-p1", "instructions": "x"},
        )
    assert r.status_code == 400
    assert "dev worker" in r.json()["detail"]


def test_dev_spawn_request_inactive_tl_400(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    _write_session_md(tmp_bot_squad, "S-testuser-tl-p0", status="suspended")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/dev-spawn-request",
            json={"tl_sid": "S-testuser-tl-p0", "instructions": "x"},
        )
    assert r.status_code == 400
    assert "not active" in r.json()["detail"]


def test_dev_spawn_request_empty_instructions_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    _write_session_md(tmp_bot_squad, "S-testuser-tl-p0")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/dev-spawn-request",
            json={"tl_sid": "S-testuser-tl-p0", "instructions": "   "},
        )
    assert r.status_code == 400


def test_dev_spawn_request_requires_auth(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/dev-spawn-request",
            json={"tl_sid": "S-x", "instructions": "x"},
        )
    assert r.status_code == 401


def test_dev_spawn_request_unknown_project_404(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/no-such-project/dev-spawn-request",
            json={"tl_sid": "S-x", "instructions": "x"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Phase 2: per-user worker routing
# ---------------------------------------------------------------------------


def _set_auth_with_meta(tmp_bot_squad: Path, *, is_admin: bool, linux_user: str) -> None:
    """Rewrite auth.toml so testuser has a specific linux_user + admin flag."""
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        f'[user_meta.testuser]\n'
        f'linux_user = "{linux_user}"\n'
        f'is_admin = {"true" if is_admin else "false"}\n'
        '[session]\nttl = "7d"\n'
    )


def test_pause_non_admin_other_user_sid_403(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """Non-admin testuser (linux_user=tu) cannot pause an SID for linux_user=edem."""
    _set_auth_with_meta(tmp_bot_squad, is_admin=False, linux_user="tu")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-edem-foo-p2/pause")
    assert r.status_code == 403
    assert "linux_user" in r.json()["detail"]


def test_pause_admin_other_user_sid_ok(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """Admin can act on any user's SID.

    T-0080: WorkerRouter.for_user now falls back to the coordinator
    socket when a per-user socket is missing, so this call succeeds via
    the coordinator's fake worker (proving the ownership gate let us
    through without 403'ing).
    """
    _set_auth_with_meta(tmp_bot_squad, is_admin=True, linux_user="tu")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-edem-foo-p2/pause")
    assert r.status_code == 200


def test_list_sessions_fans_out_and_merges(
    tmp_bot_squad: Path, monkeypatch,
):
    """list_sessions with two configured users hits both sockets and merges."""
    import threading
    import time

    import uvicorn
    from fastapi import FastAPI

    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"
    edem_sock = sock_dir / "user-edem.sock"

    def _spawn(sock: Path, sid: str):
        app = FastAPI()

        @app.post("/actions/list_sessions")
        def list_sessions(params: dict | None = None) -> dict:
            return {"sessions": [{"sid": sid, "status": "active", "window": "w",
                                  "cwd": "/"}]}

        cfg = uvicorn.Config(app, uds=str(sock), log_level="warning")
        server = uvicorn.Server(cfg)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.05)
        return server, t

    s1, t1 = _spawn(coord_sock, "S-tu-spec-p1")
    s2, t2 = _spawn(edem_sock, "S-edem-spec-p2")
    try:
        # tu is coordinator; edem appears in user_meta so it's a known user.
        (tmp_bot_squad / "config" / "auth.toml").write_text(
            '[users]\n'
            'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
            '[user_meta.testuser]\n'
            'linux_user = "tu"\n'
            'is_admin = true\n'
            '[user_meta.edem]\n'
            'linux_user = "edem"\n'
            'is_admin = false\n'
            '[session]\nttl = "7d"\n'
        )
        monkeypatch.setenv("BOT_SQUAD_COORDINATOR_USER", "tu")
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.get("/api/projects/test-project/sessions")
        assert r.status_code == 200
        sids = sorted(row["sid"] for row in r.json())
        assert sids == ["S-edem-spec-p2", "S-tu-spec-p1"]
    finally:
        s1.should_exit = True
        s2.should_exit = True
        t1.join(timeout=5)
        t2.join(timeout=5)


def test_list_sessions_tolerates_dead_user_worker(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """A non-responsive user worker must NOT block list_sessions."""
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = true\n'
        '[user_meta.deaduser]\n'
        'linux_user = "deaduser"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    monkeypatch.setenv("BOT_SQUAD_COORDINATOR_USER", "almdudleer")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 200
    # At minimum, the coordinator's sessions are returned.
    sids = [row["sid"] for row in r.json()]
    assert "S-almdudleer-spec5-p2" in sids


# ---------------------------------------------------------------------------
# Phase 9: bind_task / bind_initiative
# ---------------------------------------------------------------------------

def test_bind_task_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions/S-almdudleer-w-p1/bind/task",
            json={"task_id": "T-0042"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["task_id"] == "T-0042"
    assert data["extras"] == ["T-0042"]


def test_bind_task_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions/S-x-p1/bind/task",
            json={"task_id": "T-0042"},
        )
    assert r.status_code == 401


def test_bind_task_cross_user_403(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    """Non-admin can't bind tasks for an SID owned by another linux_user."""
    _set_auth_with_meta(tmp_bot_squad, is_admin=False, linux_user="tu")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions/S-edem-foo-p2/bind/task",
            json={"task_id": "T-0042"},
        )
    assert r.status_code == 403


def test_bind_initiative_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions/S-almdudleer-tl-p0/bind/initiative",
            json={"initiative": "v0.8-foo.md"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["initiative"] == "v0.8-foo.md"


def test_bind_initiative_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions/S-x-p1/bind/initiative",
            json={"initiative": "v0.8-foo.md"},
        )
    assert r.status_code == 401


def test_bind_initiative_cross_user_403(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    _set_auth_with_meta(tmp_bot_squad, is_admin=False, linux_user="tu")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions/S-edem-foo-p2/bind/initiative",
            json={"initiative": "v0.8-foo.md"},
        )
    assert r.status_code == 403


def test_bind_task_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post(
            "/api/projects/test-project/sessions/S-almdudleer-w-p1/bind/task",
            json={"task_id": "T-0042"},
        )
    assert r.status_code == 502


def test_spawn_routes_to_callers_linux_user(
    tmp_bot_squad: Path, monkeypatch,
):
    """spawn lands in the logged-in user's linux_user socket, not coordinator."""
    import threading
    import time

    import uvicorn
    from fastapi import FastAPI

    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"
    edem_sock = sock_dir / "user-edem.sock"

    coord_calls: list[dict] = []
    edem_calls: list[dict] = []

    def _spawn_fake(sock: Path, calls: list[dict]) -> tuple[uvicorn.Server, threading.Thread]:
        app = FastAPI()

        @app.post("/actions/spawn_session")
        def spawn(params: dict | None = None) -> dict:
            calls.append(params or {})
            return {"ok": True, "sid": f"S-x-{len(calls)}"}

        cfg = uvicorn.Config(app, uds=str(sock), log_level="warning")
        server = uvicorn.Server(cfg)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.05)
        return server, t

    s1, t1 = _spawn_fake(coord_sock, coord_calls)
    s2, t2 = _spawn_fake(edem_sock, edem_calls)
    try:
        # testuser → linux_user=edem; coordinator is someone else.
        # T-0381: spawn_session is now admin-gated (require_project_member);
        # this test verifies WORKER ROUTING (lands on caller's own socket),
        # which is orthogonal to authz — so the caller is admin here. The
        # non-admin→403 gate itself is covered by test_project_write_authz.py.
        (tmp_bot_squad / "config" / "auth.toml").write_text(
            '[users]\n'
            'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
            '[user_meta.testuser]\n'
            'linux_user = "edem"\n'
            'is_admin = true\n'
            '[session]\nttl = "7d"\n'
        )
        monkeypatch.setenv("BOT_SQUAD_COORDINATOR_USER", "almdudleer")
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.post("/api/projects/test-project/sessions",
                            json={"window": "feature-x"})
        assert r.status_code == 200
        # The call must have landed on edem's socket, not coordinator.
        assert len(edem_calls) == 1
        assert edem_calls[0].get("window") == "feature-x"
        assert coord_calls == []
    finally:
        s1.should_exit = True
        s2.should_exit = True
        t1.join(timeout=5)
        t2.join(timeout=5)


# ---------------------------------------------------------------------------
# T-0080: owner field stamping + per-user list filtering
# ---------------------------------------------------------------------------


def _write_session_md_full(tmp_bot_squad: Path, sid: str, *, owner: str = "",
                           status: str = "active", owner_user: str = "") -> None:
    """Drop an owner-stamped session md for the list-filter tests."""
    p = tmp_bot_squad / "data" / "test-project" / "sessions" / f"{sid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "---",
        f"sid: {sid}",
        f"status: {status}",
        "window: w",
        "cwd: /tmp/test-repo",
        "claude_uuid: ~",
        "task_id: ~",
    ]
    if owner:
        parts.append(f"owner: {owner}")
    if owner_user:
        parts.append(f"owner_user: {owner_user}")
    parts.extend(["---", ""])
    p.write_text("\n".join(parts))


def _auth_aqice_nonadmin(tmp_bot_squad: Path) -> None:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'aqice = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.aqice]\n'
        'linux_user = "aqice"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )


def test_pause_owner_user_wins_authoritative(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """T-0321 invariant 1: owner_user is the authoritative scoping match.

    owner is the constant-team SENTINEL, but owner_user=aqice → aqice (non-admin)
    can pause it. This is exactly the case the old owner-only scoping broke.
    """
    sid = "S-almdudleer-feature-p97"
    _write_session_md_full(tmp_bot_squad, sid, owner="constant-team",
                           owner_user="aqice")
    _auth_aqice_nonadmin(tmp_bot_squad)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker_sessions))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "aqice", "password": "test"})
    r = client.post(f"/api/projects/test-project/sessions/{sid}/pause")
    assert r.status_code == 200, r.text


def test_pause_other_owner_user_blocked(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """T-0321 invariant 1: owner_user belonging to someone else → 403 (even if
    the legacy owner field would have matched the caller)."""
    sid = "S-almdudleer-feature-p96"
    _write_session_md_full(tmp_bot_squad, sid, owner="aqice", owner_user="alexey")
    _auth_aqice_nonadmin(tmp_bot_squad)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker_sessions))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "aqice", "password": "test"})
    r = client.post(f"/api/projects/test-project/sessions/{sid}/pause")
    assert r.status_code == 403, r.text
    assert "owned by" in r.json()["detail"]


def test_spawn_forwards_owner_user_to_worker(tmp_bot_squad: Path, monkeypatch):
    """T-0321: spawning forwards owner_user (= caller username) to the worker."""
    import threading
    import time as _time

    import uvicorn
    from fastapi import FastAPI

    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"

    captured: list[dict] = []
    app = FastAPI()

    @app.post("/actions/spawn_session")
    def spawn(params: dict | None = None) -> dict:
        captured.append(params or {})
        return {"ok": True, "sid": "S-x-y-p1"}

    cfg = uvicorn.Config(app, uds=str(coord_sock), log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if coord_sock.exists():
            break
        _time.sleep(0.05)
    try:
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.post(
                "/api/projects/test-project/sessions",
                json={"window": "feature-x"},
            )
        assert r.status_code == 200, r.text
        assert captured, "worker spawn_session was not called"
        assert captured[0].get("owner_user") == "testuser"
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_spawn_forwards_owner_to_worker(tmp_bot_squad: Path, monkeypatch):
    """Spawning stamps the caller's UI username as `owner` in worker params."""
    import threading
    import time as _time

    import uvicorn
    from fastapi import FastAPI

    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"

    captured: list[dict] = []
    app = FastAPI()

    @app.post("/actions/spawn_session")
    def spawn(params: dict | None = None) -> dict:
        captured.append(params or {})
        return {"ok": True, "sid": "S-x-y-p1"}

    cfg = uvicorn.Config(app, uds=str(coord_sock), log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if coord_sock.exists():
            break
        _time.sleep(0.05)

    try:
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.post(
                "/api/projects/test-project/sessions",
                json={"window": "feature-x"},
            )
        assert r.status_code == 200, r.text
        assert captured, "worker spawn_session was not called"
        assert captured[0].get("owner") == "testuser"
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_list_sessions_non_admin_drops_other_owners(
    tmp_bot_squad: Path, monkeypatch,
):
    """Non-admin user sees only sessions whose `owner` equals their username."""
    import threading
    import time as _time

    import uvicorn
    from fastapi import FastAPI

    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"

    rows = [
        {"sid": "S-x-a-p1", "status": "active", "window": "a", "cwd": "/",
         "owner": "alexey"},
        {"sid": "S-x-b-p2", "status": "active", "window": "b", "cwd": "/",
         "owner": "testuser"},
        {"sid": "S-x-c-p3", "status": "suspended", "window": "c", "cwd": "/",
         "owner": ""},  # legacy / unstamped
    ]
    app = FastAPI()

    @app.post("/actions/list_sessions")
    def ls(params: dict | None = None) -> dict:
        return {"sessions": rows}

    cfg = uvicorn.Config(app, uds=str(coord_sock), log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if coord_sock.exists():
            break
        _time.sleep(0.05)

    try:
        # Mark testuser as non-admin so the filter applies.
        _set_auth_with_meta(tmp_bot_squad, is_admin=False, linux_user="tu")
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.get("/api/projects/test-project/sessions")
        assert r.status_code == 200
        sids = sorted(row["sid"] for row in r.json())
        # Only the row stamped owner=testuser survives — the other-owner
        # row and the legacy unstamped row are both filtered out.
        assert sids == ["S-x-b-p2"]
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_list_sessions_admin_sees_all_owners(
    tmp_bot_squad: Path, monkeypatch,
):
    """Admin sees every row, regardless of owner stamp."""
    import threading
    import time as _time

    import uvicorn
    from fastapi import FastAPI

    sock_dir = tmp_bot_squad / "data" / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)
    coord_sock = sock_dir / "worker.sock"

    rows = [
        {"sid": "S-x-a-p1", "status": "active", "window": "a", "cwd": "/",
         "owner": "alexey"},
        {"sid": "S-x-b-p2", "status": "active", "window": "b", "cwd": "/",
         "owner": "aqice"},
        {"sid": "S-x-c-p3", "status": "suspended", "window": "c", "cwd": "/",
         "owner": ""},
    ]
    app = FastAPI()

    @app.post("/actions/list_sessions")
    def ls(params: dict | None = None) -> dict:
        return {"sessions": rows}

    cfg = uvicorn.Config(app, uds=str(coord_sock), log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if coord_sock.exists():
            break
        _time.sleep(0.05)

    try:
        # testuser is admin by default in tmp_bot_squad fixture.
        with _client_logged_in(tmp_bot_squad, monkeypatch, coord_sock) as client:
            r = client.get("/api/projects/test-project/sessions")
        assert r.status_code == 200
        sids = sorted(row["sid"] for row in r.json())
        assert sids == ["S-x-a-p1", "S-x-b-p2", "S-x-c-p3"]
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_pause_owner_field_wins_over_sid_prefix(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """SessionMd owner stamp is authoritative; SID linux_user prefix is fallback.

    T-0080 scenario: aqice (linux_user=aqice) spawned a session via the
    coordinator (so the SID says S-almdudleer-...). The md is stamped
    owner=aqice. aqice can pause it; alexey-as-non-admin cannot.
    """
    sid = "S-almdudleer-feature-p99"
    _write_session_md_full(tmp_bot_squad, sid, owner="aqice")

    # aqice (non-admin) acts on the owner-stamped SID — should pass the
    # ownership check (because owner==aqice matches the username claim).
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'aqice = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.aqice]\n'
        'linux_user = "aqice"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker_sessions))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "aqice", "password": "test"})
    r = client.post(f"/api/projects/test-project/sessions/{sid}/pause")
    assert r.status_code == 200


def test_pause_other_user_owner_blocked(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """Non-admin gets 403 when SessionMd owner is someone else."""
    sid = "S-almdudleer-feature-p98"
    _write_session_md_full(tmp_bot_squad, sid, owner="alexey")

    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'aqice = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.aqice]\n'
        'linux_user = "aqice"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker_sessions))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "aqice", "password": "test"})
    r = client.post(f"/api/projects/test-project/sessions/{sid}/pause")
    assert r.status_code == 403
    assert "owned by" in r.json()["detail"]


def test_suspend_tl_sid_alias_resolves_to_human_owner(
    tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path,
):
    """T-0135: TL spawns dev with owner=<TL-SID>; TL's human owner can suspend.

    Setup: TL session md is stamped owner=aqice (human owner). The TL spawns
    a dev whose md is stamped owner=<TL-SID>. aqice's JWT should clear the
    suspend check via the TL-SID alias hop, returning 200 not 403.
    """
    tl_sid = "S-aqice-tl-p0"
    dev_sid = "S-aqice-feature-p99"
    _write_session_md_full(tmp_bot_squad, tl_sid, owner="aqice")
    _write_session_md_full(tmp_bot_squad, dev_sid, owner=tl_sid)

    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'aqice = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.aqice]\n'
        'linux_user = "aqice"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker_sessions))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "aqice", "password": "test"})
    r = client.post(f"/api/projects/test-project/sessions/{dev_sid}/suspend")
    assert r.status_code == 200, r.text

    # Negative: a different user should still get 403 — the alias only
    # rescues the TL's actual human owner, not anyone.
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'mallory = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.mallory]\n'
        'linux_user = "mallory"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "mallory", "password": "test"})
    r = client.post(f"/api/projects/test-project/sessions/{dev_sid}/suspend")
    assert r.status_code == 403
