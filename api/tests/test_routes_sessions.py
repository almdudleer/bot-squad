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
    "window": "spec5",
    "cwd": "/home/almdudleer/signal_tracker_mgmt",
    "started_at": None,
    "last_prompt_at": None,
    "claude_uuid": "9d7b3153-0000-0000-0000-000000000001",
    "linked_tasks": [],
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
    """Admin can act on any user's SID."""
    _set_auth_with_meta(tmp_bot_squad, is_admin=True, linux_user="tu")
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        # Sub-worker socket for edem doesn't exist; we expect a 502 from the
        # call attempt, NOT a 403 — which proves the ownership gate let us
        # through.
        r = client.post("/api/projects/test-project/sessions/S-edem-foo-p2/pause")
    assert r.status_code == 502


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
                                  "cwd": "/", "linked_tasks": []}]}

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
        (tmp_bot_squad / "config" / "auth.toml").write_text(
            '[users]\n'
            'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
            '[user_meta.testuser]\n'
            'linux_user = "edem"\n'
            'is_admin = false\n'
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
