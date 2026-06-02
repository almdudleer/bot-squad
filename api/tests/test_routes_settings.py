"""Tests for /api/system-settings — admin-only system config."""
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
    # T-0171: the mothership-attached state (which locks the TG fields) is read
    # from these envs. Clear them so the default test posture is "standalone /
    # detached" (editable) unless a test sets them explicitly.
    monkeypatch.delenv("MOTHERSHIP", raising=False)
    monkeypatch.delenv("BOT_SQUAD_MOTHERSHIP_URL", raising=False)
    monkeypatch.delenv("BOTSQUAD_MOTHERSHIP_URL", raising=False)
    # Need a secrets.toml for the api fixture (worker mounts it; for api it
    # only matters when reading bot_token_set).
    (root / "config" / "secrets.toml").write_text(
        '[telegram]\nbot_token = ""\nauth_age_max = 86400\n'
    )


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def test_unauthenticated_401(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/system-settings")
    assert r.status_code == 401


def test_non_admin_403(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        r = client.get("/api/system-settings")
    assert r.status_code == 403


def test_get_defaults_when_missing(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/system-settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tg"]["bot_token_set"] is False
    assert body["tg"]["quiet_hours_start_utc"] == 17
    assert body["tg"]["quiet_hours_end_utc"] == 5
    assert body["session"]["ttl"] == "7d"
    assert body["admin"]["coordinator_user"] == "almdudleer"


def test_put_writes_settings_and_creates_file(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={
                "tg": {"quiet_hours_start_utc": 18, "quiet_hours_end_utc": 6},
                "session": {"ttl": "14d"},
                "admin": {"coordinator_user": "almdudleer"},
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["restart_required"] is True
    assert body["tg"]["quiet_hours_start_utc"] == 18
    assert body["tg"]["quiet_hours_end_utc"] == 6
    assert body["session"]["ttl"] == "14d"
    # File on disk
    path = tmp_bot_squad / "config" / "system_settings.toml"
    raw = tomllib.loads(path.read_text())
    assert raw["tg"]["quiet_hours_start_utc"] == 18
    assert raw["tg"]["quiet_hours_end_utc"] == 6
    assert raw["session"]["ttl"] == "14d"


def test_put_bot_token_writes_secrets(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"bot_token": "12345:ABCDEF"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tg"]["bot_token_set"] is True
    raw = tomllib.loads((tmp_bot_squad / "config" / "secrets.toml").read_text())
    assert raw["telegram"]["bot_token"] == "12345:ABCDEF"


def test_put_bot_token_clear(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/system-settings", json={"tg": {"bot_token": "abc"}})
        r = client.put("/api/system-settings", json={"tg": {"bot_token": ""}})
    assert r.status_code == 200
    assert r.json()["tg"]["bot_token_set"] is False


def test_put_validates_quiet_hours(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"quiet_hours_start_utc": 99}})
        assert r.status_code == 400
        r = client.put("/api/system-settings", json={"tg": {"quiet_hours_end_utc": -1}})
        assert r.status_code == 400


def test_put_validates_ttl(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"session": {"ttl": "five days"}})
    assert r.status_code == 400


# --- T-0171: per-server default chat + lock-when-attached -------------------


def test_get_default_chat_and_not_managed_when_standalone(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/system-settings")
    body = r.json()
    assert body["tg"]["default_chat_id"] == ""
    assert body["tg"]["managed_by_mothership"] is False
    assert body["tg"]["mothership_url"] is None


def test_put_default_chat_id_roundtrip(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"default_chat_id": "404580642"}})
        assert r.status_code == 200, r.text
        assert r.json()["tg"]["default_chat_id"] == "404580642"
        # persisted to system_settings.toml (non-secret)
        raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
        assert raw["tg"]["default_chat_id"] == "404580642"
        # survives a fresh GET
        assert client.get("/api/system-settings").json()["tg"]["default_chat_id"] == "404580642"


def test_managed_when_attached_consumer_and_writes_refused(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    # Attached consumer: not the mothership, but pointed at one.
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example/")
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        body = client.get("/api/system-settings").json()
        assert body["tg"]["managed_by_mothership"] is True
        assert body["tg"]["mothership_url"] == "https://mom.example"
        # The locked fields cannot be written while attached.
        assert client.put("/api/system-settings", json={"tg": {"bot_token": "x"}}).status_code == 409
        assert client.put("/api/system-settings", json={"tg": {"default_chat_id": "1"}}).status_code == 409
        # Non-locked fields still save fine.
        assert client.put("/api/system-settings", json={"session": {"ttl": "1d"}}).status_code == 200


def test_not_managed_on_mothership_self(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    # The mothership itself owns @bot_squad_bot — token stays editable.
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example")
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        body = client.get("/api/system-settings").json()
        assert body["tg"]["managed_by_mothership"] is False
        # writes to the token are accepted (not locked)
        assert client.put("/api/system-settings", json={"tg": {"bot_token": "ok:tok"}}).status_code == 200


def test_get_does_not_leak_bot_token(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "secrets.toml").write_text(
        '[telegram]\nbot_token = "REAL:SECRET"\nauth_age_max = 86400\n'
    )
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/system-settings")
    body = r.json()
    assert body["tg"]["bot_token_set"] is True
    assert "bot_token" not in body["tg"]
    assert "REAL:SECRET" not in r.text
