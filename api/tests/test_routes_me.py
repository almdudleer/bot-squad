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


# ── T-0061: per-attachment tg-chat-id ─────────────────────────────────────


def _attach_testuser(tmp: Path, *, tg_chat_id: str = "") -> tuple[str, str]:
    """Mint a GlobalUser + Attachment for testuser; rewrite auth.toml.

    Returns ``(global_user_id, server_id)``. Used to put the fixture user
    into the migrated state without going through /api/auth/attach (which
    requires a global password) — these tests want to assert the route
    plumbing on top of an already-attached row.
    """
    from app.mothership_users_store import MothershipUsersStore
    from app.mothership_store import MothershipStore

    mship = MothershipStore(tmp / "data" / "_mothership")
    mship.register_self_if_missing(
        base_url="https://this.test", display_name="this.test"
    )
    server_id = next(s.id for s in mship.list_servers() if s.is_self)

    store = MothershipUsersStore(tmp / "data" / "_mothership")
    gu, _ = store.upsert_user_by_username(
        username="testuser",
        password_hash="$2b$12$placeholder",
    )
    store.upsert_attachment(
        global_user_id=gu.id,
        server_id=server_id,
        server_username="testuser",
        tg_chat_id=tg_chat_id,
    )
    # Rewrite auth.toml so testuser carries attached_to_global_user — the
    # new endpoints branch on this field.
    auth_path = tmp / "config" / "auth.toml"
    text = auth_path.read_text()
    text = text.replace(
        "is_admin = true\n",
        f'is_admin = true\nattached_to_global_user = "{gu.id}"\n',
        1,
    )
    auth_path.write_text(text)
    return gu.id, server_id


def test_get_attachment_tg_chat_id_reads_from_attachment_store(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Migrated user → GET returns Attachment.tg_chat_id, not UserMeta's."""
    _, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="11122233")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get(f"/api/me/attachment/{server_id}/tg-chat-id")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server_id"] == server_id
    assert body["tg_chat_id"] == "11122233"


def test_get_attachment_tg_chat_id_self_sentinel(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """``server_id="self"`` resolves to this install's is_self id."""
    _, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="99")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/me/attachment/self/tg-chat-id")
    assert r.status_code == 200
    body = r.json()
    assert body["server_id"] == server_id
    assert body["tg_chat_id"] == "99"


def test_put_attachment_tg_chat_id_writes_to_store(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """PUT updates the Attachment file, not auth.toml's UserMeta."""
    from app.mothership_users_store import MothershipUsersStore

    gu_id, server_id = _attach_testuser(tmp_bot_squad)
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            f"/api/me/attachment/{server_id}/tg-chat-id",
            json={"tg_chat_id": "555444"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["tg_chat_id"] == "555444"

    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    attachment = store.get_attachment(gu_id, server_id)
    assert attachment is not None
    assert attachment.tg_chat_id == "555444"
    # auth.toml's UserMeta.tg_chat_id is left untouched (migrated branch
    # writes to the store, not back into auth.toml).
    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert "tg_chat_id" not in raw["user_meta"]["testuser"]


def test_put_attachment_tg_chat_id_preserves_seen_steps(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A TG write must not blow away the Attachment's seen_steps tuple."""
    from app.mothership_users_store import MothershipUsersStore

    gu_id, server_id = _attach_testuser(tmp_bot_squad)
    # Seed seen_steps on the existing attachment row.
    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    store.upsert_attachment(
        global_user_id=gu_id,
        server_id=server_id,
        server_username="testuser",
        seen_steps=("srv.intro", "srv.9_5.tg_binding"),
    )
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put(
            f"/api/me/attachment/{server_id}/tg-chat-id",
            json={"tg_chat_id": "7"},
        )
    after = store.get_attachment(gu_id, server_id)
    assert after is not None
    assert after.seen_steps == ("srv.intro", "srv.9_5.tg_binding")
    assert after.tg_chat_id == "7"


def test_put_attachment_tg_chat_id_rejects_non_numeric(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _, server_id = _attach_testuser(tmp_bot_squad)
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            f"/api/me/attachment/{server_id}/tg-chat-id",
            json={"tg_chat_id": "abc"},
        )
    assert r.status_code == 400


def test_attachment_unmigrated_user_falls_back_to_user_meta(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """No attached_to_global_user → endpoint reads UserMeta.tg_chat_id.

    Back-compat for the window between rollout of these endpoints and the
    one-shot ``users_split.py`` migration.
    """
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "123456"})
        r = client.get("/api/me/attachment/self/tg-chat-id")
    assert r.status_code == 200
    assert r.json()["tg_chat_id"] == "123456"


def test_attachment_unmigrated_user_put_writes_user_meta(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """PUT on unmigrated user still works — writes to UserMeta (legacy)."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/me/attachment/self/tg-chat-id", json={"tg_chat_id": "789"}
        )
        assert r.status_code == 200
        assert r.json()["tg_chat_id"] == "789"
        # Legacy GET sees the same value.
        legacy = client.get("/api/me").json()
    assert legacy["tg_chat_id"] == "789"

    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert raw["user_meta"]["testuser"]["tg_chat_id"] == "789"


def test_list_attachments_migrated(tmp_bot_squad: Path, monkeypatch) -> None:
    """GET /api/me/attachments returns one row per Attachment."""
    _, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="42")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/me/attachments")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["global_user_id"]  # truthy
    assert len(body["attachments"]) == 1
    row = body["attachments"][0]
    assert row["server_id"] == server_id
    assert row["tg_chat_id"] == "42"
    assert row["legacy_unmigrated"] is False


def test_list_attachments_unmigrated(tmp_bot_squad: Path, monkeypatch) -> None:
    """Un-migrated user → synthetic single-row from UserMeta."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "888"})
        r = client.get("/api/me/attachments")
    body = r.json()
    assert body["global_user_id"] is None
    assert len(body["attachments"]) == 1
    row = body["attachments"][0]
    assert row["server_username"] == "testuser"
    assert row["tg_chat_id"] == "888"
    assert row["legacy_unmigrated"] is True


def test_attachment_test_ping_uses_attachment_chat_id(
    tmp_bot_squad: Path, monkeypatch, fake_tg_worker: _FakeTgWorker
) -> None:
    """test ping reads chat_id from Attachment, not UserMeta."""
    _, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="77777")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post(f"/api/me/attachment/{server_id}/tg-chat-id/test")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert fake_tg_worker.calls[-1]["chat_id"] == "77777"
    assert fake_tg_worker.calls[-1]["user"] == "testuser"


def test_attachment_test_ping_400_when_unbound(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post(f"/api/me/attachment/{server_id}/tg-chat-id/test")
    assert r.status_code == 400
    assert "no tg_chat_id bound" in r.json()["detail"]


def test_legacy_tg_chat_id_endpoints_still_work_post_t0061(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Legacy /api/me/tg-chat-id keeps working for un-migrated users.

    Sanity check that adding the per-attachment surface didn't accidentally
    short-circuit the existing endpoints — un-migrated rows are the steady
    state until ``users_split.py`` runs on every install.
    """
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/me/tg-chat-id", json={"tg_chat_id": "42"})
        assert r.status_code == 200
        me = client.get("/api/me").json()
    assert me["tg_chat_id"] == "42"


# ── T-0218: per-project personal override + resolved view (D-0022 stub) ────
#
# Stub scope: routes + shapes + validation + the global/server resolution are
# real; the per-project override is not yet persisted (GET null, PUT echoes).


def test_project_tg_chat_id_get_returns_null_stub(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/me/project/some-proj/tg-chat-id")
    assert r.status_code == 200, r.text
    assert r.json() == {"slug": "some-proj", "tg_chat_id": None}


def test_project_tg_chat_id_put_validates_and_echoes(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        # Valid digits echo back.
        r = client.put(
            "/api/me/project/p1/tg-chat-id", json={"tg_chat_id": "404580642"}
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"slug": "p1", "tg_chat_id": "404580642"}
        # Negative group id ok.
        r2 = client.put(
            "/api/me/project/p1/tg-chat-id", json={"tg_chat_id": "-100123"}
        )
        assert r2.json()["tg_chat_id"] == "-100123"
        # Empty clears → null.
        r3 = client.put("/api/me/project/p1/tg-chat-id", json={"tg_chat_id": ""})
        assert r3.json() == {"slug": "p1", "tg_chat_id": None}
        # Non-numeric rejected.
        r4 = client.put(
            "/api/me/project/p1/tg-chat-id", json={"tg_chat_id": "abc"}
        )
        assert r4.status_code == 400


def test_notifications_resolved_global_only(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Un-migrated user with only a global binding → effective source=global."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "111"})
        r = client.get("/api/me/notifications/resolved?slug=proj-x")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["levels"]["global"] == {"tg_chat_id": "111", "set": True}
    assert body["levels"]["server"]["set"] is False
    assert body["levels"]["project"] == {
        "slug": "proj-x",
        "tg_chat_id": None,
        "set": False,
    }
    assert body["effective"] == {"tg_chat_id": "111", "source": "global"}


def test_notifications_resolved_server_overrides_global(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Migrated user: per-server override wins over global; project still null."""
    _, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="222")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "111"})
        r = client.get("/api/me/notifications/resolved?server_id=self&slug=proj-x")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["levels"]["global"] == {"tg_chat_id": "111", "set": True}
    assert body["levels"]["server"]["server_id"] == server_id
    assert body["levels"]["server"]["tg_chat_id"] == "222"
    assert body["levels"]["server"]["set"] is True
    assert body["effective"] == {"tg_chat_id": "222", "source": "server"}


def test_notifications_resolved_none_when_nothing_set(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/me/notifications/resolved?slug=proj-x")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effective"] == {"tg_chat_id": None, "source": "none"}


def test_project_test_ping_resolves_and_calls_worker(
    tmp_bot_squad: Path, monkeypatch, fake_tg_worker: _FakeTgWorker
) -> None:
    """Project test pings the RESOLVED chat (global fallback today)."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "404580642"})
        r = client.post("/api/me/project/proj-x/tg-chat-id/test")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert fake_tg_worker.calls[-1]["chat_id"] == "404580642"
    assert "proj-x" in fake_tg_worker.calls[-1]["message"]


def test_project_test_ping_400_when_nothing_resolves(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/me/project/proj-x/tg-chat-id/test")
    assert r.status_code == 400
    assert "no tg_chat_id resolves" in r.json()["detail"]


# ── T-0218 follow-up: per-project override PERSISTENCE (D-0022 Q2) ─────────
#
# The override is stored in a ``project_tg_chat_ids: {slug -> chat_id}`` map —
# on the Attachment (migrated users) or UserMeta (un-migrated). GET reads it
# back, the resolved view ranks it above server+global, and the test ping
# fires to the project-resolved chat.


def test_project_tg_chat_id_persists_unmigrated(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Un-migrated user: PUT a project override → GET reads it back; the value
    lands in auth.toml's per-user project_tg_chat_ids table."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        put = client.put(
            "/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": "333"}
        )
        assert put.status_code == 200, put.text
        assert put.json() == {"slug": "proj-x", "tg_chat_id": "333"}
        get = client.get("/api/me/project/proj-x/tg-chat-id")
    assert get.json() == {"slug": "proj-x", "tg_chat_id": "333"}

    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert raw["user_meta"]["testuser"]["project_tg_chat_ids"]["proj-x"] == "333"


def test_project_override_wins_in_resolved_unmigrated(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Un-migrated: project override beats the global binding in the resolved view."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "111"})
        client.put("/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": "333"})
        r = client.get("/api/me/notifications/resolved?slug=proj-x")
    body = r.json()
    assert body["levels"]["global"] == {"tg_chat_id": "111", "set": True}
    assert body["levels"]["project"] == {
        "slug": "proj-x",
        "tg_chat_id": "333",
        "set": True,
    }
    assert body["effective"] == {"tg_chat_id": "333", "source": "project"}
    # A DIFFERENT project with no override still falls through to global.
    with TestClient(app) as client:
        _login(client)
        r2 = client.get("/api/me/notifications/resolved?slug=other-proj")
    assert r2.json()["effective"] == {"tg_chat_id": "111", "source": "global"}


def test_project_tg_chat_id_persists_migrated(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Migrated user: the override lands in Attachment.project_tg_chat_ids, not
    auth.toml."""
    from app.mothership_users_store import MothershipUsersStore

    gu_id, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="222")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        put = client.put(
            "/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": "333"}
        )
        assert put.status_code == 200, put.text
        get = client.get("/api/me/project/proj-x/tg-chat-id")
    assert get.json() == {"slug": "proj-x", "tg_chat_id": "333"}

    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    att = store.get_attachment(gu_id, server_id)
    assert att is not None
    assert att.project_tg_chat_ids == {"proj-x": "333"}
    # The migrated branch never writes the override back into auth.toml.
    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert "project_tg_chat_ids" not in raw["user_meta"]["testuser"]


def test_project_override_wins_in_resolved_migrated(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Migrated: project > server > global precedence in the resolved view."""
    _attach_testuser(tmp_bot_squad, tg_chat_id="222")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "111"})
        client.put("/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": "333"})
        r = client.get("/api/me/notifications/resolved?server_id=self&slug=proj-x")
    body = r.json()
    assert body["levels"]["server"]["tg_chat_id"] == "222"
    assert body["levels"]["project"]["tg_chat_id"] == "333"
    assert body["effective"] == {"tg_chat_id": "333", "source": "project"}


def test_project_put_preserves_server_chat_and_seen_steps(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Writing a project override must not clobber the Attachment's per-server
    tg_chat_id or seen_steps tuple."""
    from app.mothership_users_store import MothershipUsersStore

    gu_id, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="222")
    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    store.upsert_attachment(
        global_user_id=gu_id,
        server_id=server_id,
        server_username="testuser",
        tg_chat_id="222",
        seen_steps=("welcome", "tour"),
    )
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": "333"})

    att = store.get_attachment(gu_id, server_id)
    assert att is not None
    assert att.tg_chat_id == "222"
    assert att.seen_steps == ("welcome", "tour")
    assert att.project_tg_chat_ids == {"proj-x": "333"}


def test_project_override_empty_clears_migrated(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """PUT empty removes the slug from the map → GET null, resolved falls back."""
    from app.mothership_users_store import MothershipUsersStore

    gu_id, server_id = _attach_testuser(tmp_bot_squad, tg_chat_id="222")
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": "333"})
        cleared = client.put(
            "/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": ""}
        )
        assert cleared.json() == {"slug": "proj-x", "tg_chat_id": None}
        resolved = client.get(
            "/api/me/notifications/resolved?server_id=self&slug=proj-x"
        )
    assert resolved.json()["effective"] == {"tg_chat_id": "222", "source": "server"}
    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    att = store.get_attachment(gu_id, server_id)
    assert att is not None
    assert "proj-x" not in att.project_tg_chat_ids


def test_project_test_ping_uses_project_override(
    tmp_bot_squad: Path, monkeypatch, fake_tg_worker: _FakeTgWorker
) -> None:
    """The project test ping fires to the project-override chat, not global."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "111"})
        client.put("/api/me/project/proj-x/tg-chat-id", json={"tg_chat_id": "333"})
        r = client.post("/api/me/project/proj-x/tg-chat-id/test")
    assert r.status_code == 200, r.text
    assert fake_tg_worker.calls[-1]["chat_id"] == "333"


def test_writer_omits_project_map_when_empty(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """No project overrides → auth.toml carries no project_tg_chat_ids key
    (byte-clean roundtrip, same discipline as seen_steps/tg_chat_id)."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/me/tg-chat-id", json={"tg_chat_id": "111"})
    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert "project_tg_chat_ids" not in raw["user_meta"]["testuser"]
