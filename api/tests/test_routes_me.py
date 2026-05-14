"""Tests for /api/me — onboarding (T-0012) + tg-chat-id binding (T-0019)."""
from __future__ import annotations

import threading
import time
import tomllib
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


def _set_env(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(root / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def test_onboarding_unauthenticated(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/me/onboarding")
    assert r.status_code == 401


def test_onboarding_initial_state(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/me/onboarding")
    assert r.status_code == 200
    assert r.json() == {"steps_seen": [], "skipped": False}


def test_onboarding_seen_roundtrip(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/me/onboarding/seen", json={"step": "srv.intro"})
        assert r.status_code == 200, r.text
        assert r.json() == {"steps_seen": ["srv.intro"], "skipped": False}

        # Idempotent.
        r2 = client.post("/api/me/onboarding/seen", json={"step": "srv.intro"})
        assert r2.json() == {"steps_seen": ["srv.intro"], "skipped": False}

        # Second step appends.
        r3 = client.post("/api/me/onboarding/seen", json={"step": "srv.9_1.help_spotlight"})
        assert r3.json()["steps_seen"] == ["srv.intro", "srv.9_1.help_spotlight"]

        # Fresh GET reflects state.
        r4 = client.get("/api/me/onboarding")
        assert r4.json()["steps_seen"] == ["srv.intro", "srv.9_1.help_spotlight"]

    # Persisted to disk and reads back as a TOML array.
    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert raw["user_meta"]["testuser"]["seen_steps"] == [
        "srv.intro",
        "srv.9_1.help_spotlight",
    ]


def test_onboarding_seen_rejects_blank(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/me/onboarding/seen", json={"step": ""})
        assert r.status_code == 400
        r2 = client.post("/api/me/onboarding/seen", json={})
        assert r2.status_code == 400


def test_onboarding_seen_rejects_sentinel(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/me/onboarding/seen", json={"step": "__skip_all__"})
    # The sentinel is reserved for /skip; clients must not be able to plant it
    # via /seen and corrupt the "did the user explicitly skip?" signal.
    assert r.status_code == 400


def test_onboarding_skip(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/me/onboarding/skip")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped"] is True
        assert "__skip_all__" in body["steps_seen"]

        # Idempotent.
        r2 = client.post("/api/me/onboarding/skip")
        assert r2.json() == body


def test_onboarding_writer_omits_seen_steps_when_empty(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Roundtrip: a fresh auth.toml without seen_steps stays clean after any
    write that doesn't touch onboarding (so old installs don't get a noisy
    `seen_steps = []` injected the first time an admin edits a user)."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        # Create a second user so we can patch testuser without admin-protection.
        client.post("/api/users", json={"username": "bob", "password": "x", "is_admin": True})
        # Patch bob with a no-op-ish change.
        client.patch("/api/users/bob", json={"linux_user": "bob2"})

    raw_text = (tmp_bot_squad / "config" / "auth.toml").read_text()
    assert "seen_steps" not in raw_text


def test_onboarding_state_survives_admin_patch(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Admin patching a user's linux_user/is_admin must NOT wipe seen_steps."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "x"})
        # bob logs in and dismisses a step.
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"username": "bob", "password": "x"})
        client.post("/api/me/onboarding/seen", json={"step": "srv.intro"})

        # testuser admin patches bob.
        client.post("/api/auth/logout")
        _login(client)
        client.patch("/api/users/bob", json={"linux_user": "bob-os"})

        # bob's onboarding state is intact.
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"username": "bob", "password": "x"})
        r = client.get("/api/me/onboarding")
    assert r.json()["steps_seen"] == ["srv.intro"]


# ── T-0019: tg-chat-id binding ────────────────────────────────────────────


class _FakeTgWorker:
    """Helper attached to the fake worker fixture.

    Records ``tg_notify`` invocations and lets the test override the response
    payload (e.g. to simulate a Telegram failure).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response: dict = {"ok": True, "sent": True}


@pytest.fixture
def fake_tg_worker(tmp_bot_squad: Path):
    """Fake worker that captures /actions/tg_notify calls for assertions."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()
    state = _FakeTgWorker()

    @fake.post("/actions/tg_notify")
    def tg_notify(params: dict) -> dict:
        state.calls.append(params)
        return state.response

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield state

    server.should_exit = True
    thread.join(timeout=5)


def test_get_me_returns_profile_with_unbound_tg_chat_id(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/me")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "testuser"
    assert body["linux_user"] == "almdudleer"
    assert body["is_admin"] is True
    assert body["tg_chat_id"] is None


def test_auth_me_carries_tg_chat_id(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        # Empty by default — present as "" (auth.me's _enrich returns the raw
        # field, not the null-or-string shape of GET /api/me).
        r = client.get("/api/auth/me")
        assert r.status_code == 200, r.text
        assert r.json()["tg_chat_id"] == ""

        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "404580642"})
        r2 = client.get("/api/auth/me")
        assert r2.json()["tg_chat_id"] == "404580642"


def test_put_tg_chat_id_roundtrips_through_auth_toml(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/me/tg-chat-id", json={"tg_chat_id": "404580642"})
        assert r.status_code == 200, r.text
        assert r.json()["tg_chat_id"] == "404580642"

        # GET reflects it.
        r2 = client.get("/api/me")
        assert r2.json()["tg_chat_id"] == "404580642"

    # Persisted to disk as a TOML string.
    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert raw["user_meta"]["testuser"]["tg_chat_id"] == "404580642"

    # And a fresh AuthConfig.load parses it back.
    from app.config import AuthConfig

    cfg = AuthConfig.load(tmp_bot_squad / "config")
    assert cfg.user_meta["testuser"].tg_chat_id == "404580642"


def test_put_tg_chat_id_accepts_negative(tmp_bot_squad: Path, monkeypatch) -> None:
    """TG group chats have negative ids — must be accepted."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/me/tg-chat-id", json={"tg_chat_id": "-100123"})
    assert r.status_code == 200
    assert r.json()["tg_chat_id"] == "-100123"


def test_put_tg_chat_id_empty_clears_binding(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Setting an empty string clears the binding; the toml writer omits the
    line so old installs stay byte-identical."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "12345"})
        r = client.put("/api/me/tg-chat-id", json={"tg_chat_id": ""})
        assert r.status_code == 200
        assert r.json()["tg_chat_id"] is None

    raw_text = (tmp_bot_squad / "config" / "auth.toml").read_text()
    assert "tg_chat_id" not in raw_text


def test_put_tg_chat_id_rejects_non_numeric(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/me/tg-chat-id", json={"tg_chat_id": "abc"})
    assert r.status_code == 400


def test_writer_omits_tg_chat_id_when_unbound(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A fresh auth.toml without tg_chat_id stays clean after writes that
    don't touch the field (matches the seen_steps omission pattern)."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "x", "is_admin": True})
        client.patch("/api/users/bob", json={"linux_user": "bob2"})

    raw_text = (tmp_bot_squad / "config" / "auth.toml").read_text()
    assert "tg_chat_id" not in raw_text


def test_tg_chat_id_survives_admin_patch(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Admin patching linux_user/is_admin must NOT wipe a user's tg_chat_id."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "x"})

        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"username": "bob", "password": "x"})
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "777"})

        client.post("/api/auth/logout")
        _login(client)
        client.patch("/api/users/bob", json={"linux_user": "bob-os"})

        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"username": "bob", "password": "x"})
        r = client.get("/api/me")
    assert r.json()["tg_chat_id"] == "777"


def test_test_ping_400_when_unbound(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/me/tg-chat-id/test")
    assert r.status_code == 400
    assert "no tg_chat_id bound" in r.json()["detail"]


def test_test_ping_calls_worker_tg_notify(
    tmp_bot_squad: Path, monkeypatch, fake_tg_worker: _FakeTgWorker
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "404580642"})
        r = client.post("/api/me/tg-chat-id/test")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert len(fake_tg_worker.calls) == 1
    call = fake_tg_worker.calls[0]
    assert call["chat_id"] == "404580642"
    assert "test ping" in call["message"]
    assert call["user"] == "testuser"


def test_test_ping_surfaces_worker_error(
    tmp_bot_squad: Path, monkeypatch, fake_tg_worker: _FakeTgWorker
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    fake_tg_worker.response = {"ok": False, "error": "telegram 403"}
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "404580642"})
        r = client.post("/api/me/tg-chat-id/test")
    assert r.status_code == 502
    assert "telegram 403" in r.json()["detail"]
