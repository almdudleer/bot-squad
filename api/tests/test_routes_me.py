"""Tests for /api/me/onboarding — per-user step tracker (T-0012)."""
from __future__ import annotations

import tomllib
from pathlib import Path

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
