"""T-0488: TG sender -> GlobalUser linkage endpoint (single bot user recognition).

The worker (tg_listener) calls this endpoint on the inbound TG path to resolve
the sender to a cross-server GlobalUser, minting one on first contact. The
endpoint is token-gated (shared-secret ``WORKER_API_TOKEN`` Bearer) and
fails closed — no configured token, or a missing/mismatched Bearer, is 401.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_users_store import MothershipUsersStore

WORKER_TOKEN = "worker-secret-token-xyz"


def _client(tmp_bot_squad: Path, monkeypatch, *, mothership: bool = True,
            worker_token: str | None = WORKER_TOKEN) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)
    if worker_token is None:
        monkeypatch.delenv("WORKER_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WORKER_API_TOKEN", worker_token)
    return TestClient(build_app())


def _auth(token: str = WORKER_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_first_contact_creates_and_links(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(
        "/api/m/tg/resolve-or-link",
        json={"tg_user_id": "555123", "display_name": "Alexey", "slug": "test-project"},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["global_user_id"].startswith("gu_")
    assert body["slug"] == "test-project"

    # Stored in the cross-server registry under the TG sender id.
    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    linked = store.user_by_tg_user_id("555123")
    assert linked is not None
    assert linked.id == body["global_user_id"]
    assert linked.tg_user_id == "555123"


def test_subsequent_message_is_recognized(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    first = client.post(
        "/api/m/tg/resolve-or-link",
        json={"tg_user_id": "555123"}, headers=_auth(),
    ).json()
    second = client.post(
        "/api/m/tg/resolve-or-link",
        json={"tg_user_id": "555123"}, headers=_auth(),
    )
    assert second.status_code == 200
    body = second.json()
    assert body["created"] is False
    assert body["global_user_id"] == first["global_user_id"]


def test_missing_bearer_is_401(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post("/api/m/tg/resolve-or-link", json={"tg_user_id": "1"})
    assert r.status_code == 401


def test_wrong_bearer_is_401(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(
        "/api/m/tg/resolve-or-link",
        json={"tg_user_id": "1"}, headers=_auth("not-the-token"),
    )
    assert r.status_code == 401


def test_unconfigured_token_fails_closed(tmp_bot_squad: Path, monkeypatch):
    """No WORKER_API_TOKEN in the environment => reject everything (401),
    never run open. Defense against shipping the code before the secret."""
    client = _client(tmp_bot_squad, monkeypatch, worker_token=None)
    r = client.post(
        "/api/m/tg/resolve-or-link",
        json={"tg_user_id": "1"}, headers=_auth("anything"),
    )
    assert r.status_code == 401


def test_missing_tg_user_id_is_400(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(
        "/api/m/tg/resolve-or-link", json={"display_name": "x"}, headers=_auth(),
    )
    assert r.status_code == 400
