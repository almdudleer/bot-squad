"""T-0630: operator pause/resume + fleet-default model routes.

Thin RPC wrappers over the worker's operator_pause/operator_resume (T-0522,
already tested worker-side) and the new fleet_model_get/fleet_model_set
(T-0630). This file covers: authz (pause/resume + PUT model require the
project-write gate per D-0056; GET model stays a plain authenticated read),
requested_by propagation, unknown-slug 4xx, and the model allowlist reject.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.main import build_app

_P = "/api/projects/test-project"


# ---------------------------------------------------------------------------
# Fake worker recording every action call + params
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_worker_operator(tmp_bot_squad: Path):
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    calls: list[tuple[str, dict]] = []
    state = {"model": ""}

    fake = FastAPI()

    @fake.post("/actions/operator_pause")
    async def operator_pause(request: Request) -> dict:
        params = await request.json()
        calls.append(("operator_pause", params))
        return {"ok": True, "paused": {"reason": params.get("reason", "")}, "was_already_paused": False}

    @fake.post("/actions/operator_resume")
    async def operator_resume(request: Request) -> dict:
        params = await request.json()
        calls.append(("operator_resume", params))
        return {"ok": True, "was_paused": True}

    @fake.post("/actions/fleet_model_get")
    async def fleet_model_get(request: Request) -> dict:
        params = await request.json()
        calls.append(("fleet_model_get", params))
        return {"ok": True, "model": state["model"]}

    @fake.post("/actions/fleet_model_set")
    async def fleet_model_set(request: Request) -> dict:
        params = await request.json()
        calls.append(("fleet_model_set", params))
        state["model"] = params["model"]
        return {"ok": True, "model": params["model"]}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock, calls, state

    server.should_exit = True
    thread.join(timeout=5)


def _client(tmp_bot_squad: Path, monkeypatch, sock_path: Path) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def _make_nonadmin(tmp_bot_squad: Path) -> None:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )


# ---------------------------------------------------------------------------
# Behavior: pause/resume
# ---------------------------------------------------------------------------

def test_pause_returns_worker_result_and_requested_by(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.post(f"{_P}/operator/pause", json={"reason": "hit limits"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    name, params = calls[-1]
    assert name == "operator_pause"
    assert params["slug"] == "test-project"
    assert params["reason"] == "hit limits"
    assert params["requested_by"] == "testuser"


def test_pause_without_reason_omits_it(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.post(f"{_P}/operator/pause", json={})
    assert r.status_code == 200, r.text
    _, params = calls[-1]
    assert "reason" not in params


def test_resume_returns_worker_result_and_requested_by(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.post(f"{_P}/operator/resume")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "was_paused": True}
    name, params = calls[-1]
    assert name == "operator_resume"
    assert params["slug"] == "test-project"
    assert params["requested_by"] == "testuser"


def test_pause_unknown_slug_404(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.post("/api/projects/no-such-project/operator/pause", json={})
    assert r.status_code == 404


def test_resume_unknown_slug_404(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.post("/api/projects/no-such-project/operator/resume")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Behavior: GET/PUT worker model
# ---------------------------------------------------------------------------

def test_get_model_reads_worker_state(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, state = fake_worker_operator
    state["model"] = "claude-opus-4-8"
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.get(f"{_P}/worker/model")
    assert r.status_code == 200, r.text
    assert r.json() == {"model": "claude-opus-4-8"}


def test_put_model_round_trips_through_worker(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, calls, state = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.put(f"{_P}/worker/model", json={"model": "claude-fable-5"})
    assert r.status_code == 200, r.text
    assert r.json() == {"model": "claude-fable-5"}
    assert state["model"] == "claude-fable-5"
    name, params = calls[-1]
    assert name == "fleet_model_set"
    assert params == {"model": "claude-fable-5"}


def test_put_model_clears_with_empty_string(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, state = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.put(f"{_P}/worker/model", json={"model": ""})
    assert r.status_code == 200, r.text
    assert state["model"] == ""


def test_put_model_accepts_codex_provider_choice(
    tmp_bot_squad, monkeypatch, fake_worker_operator
):
    sock, calls, state = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.put(f"{_P}/worker/model", json={"model": "codex"})
    assert r.status_code == 200, r.text
    assert state["model"] == "codex"
    assert calls[-1] == ("fleet_model_set", {"model": "codex"})


def test_put_model_rejects_bad_value_without_hitting_worker(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.put(f"{_P}/worker/model", json={"model": "gpt-5"})
    assert r.status_code == 400
    assert not any(name == "fleet_model_set" for name, _ in calls)


def test_get_model_unknown_slug_404(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.get("/api/projects/no-such-project/worker/model")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Authz: pause/resume + PUT model require project-write (admin today, T-0381);
# GET model stays a plain authenticated read.
# ---------------------------------------------------------------------------

def test_nonadmin_403_on_pause_resume_and_put_model(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, _ = fake_worker_operator
    _make_nonadmin(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        assert client.post(f"{_P}/operator/pause", json={}).status_code == 403
        assert client.post(f"{_P}/operator/resume").status_code == 403
        assert client.put(f"{_P}/worker/model", json={"model": "claude-sonnet-5"}).status_code == 403


def test_nonadmin_can_still_read_model(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, _ = fake_worker_operator
    _make_nonadmin(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)
        r = client.get(f"{_P}/worker/model")
    assert r.status_code == 200, r.text


def test_admin_passes_all_gates(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        _login(client)  # conftest testuser is admin
        assert client.post(f"{_P}/operator/pause", json={}).status_code not in (401, 403)
        assert client.post(f"{_P}/operator/resume").status_code not in (401, 403)
        assert client.put(f"{_P}/worker/model", json={"model": "claude-sonnet-5"}).status_code not in (401, 403)


def test_anonymous_401_on_everything(tmp_bot_squad, monkeypatch, fake_worker_operator):
    sock, _calls, _ = fake_worker_operator
    with _client(tmp_bot_squad, monkeypatch, sock) as client:
        assert client.post(f"{_P}/operator/pause", json={}).status_code == 401
        assert client.post(f"{_P}/operator/resume").status_code == 401
        assert client.get(f"{_P}/worker/model").status_code == 401
        assert client.put(f"{_P}/worker/model", json={"model": "claude-sonnet-5"}).status_code == 401
