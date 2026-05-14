"""Mothership build-flag seam + install-flow surface.

Build-flag seam: single-install builds (MOTHERSHIP unset) MUST NOT expose
/api/m/* or /i/*; mothership builds (MOTHERSHIP=1) mount the centralization
layer.

Install-flow surface (T-0024): token issuance, install-bundle serve, the
/connect handshake that burns the install token + mints a server bearer,
and the SSE checkpoint stream consumed by the install wizard UI.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.install_tokens import (
    INSTALL_PREFIX,
    SERVER_PREFIX,
    hash_token,
    mint_install_token,
)
from app.mothership_store import MothershipStore


def _client(tmp_bot_squad: Path, monkeypatch, *, mothership: bool):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    # Bundle dir: point at the repo's scripts/install so the substituted
    # install.sh + instructions.md actually contain the placeholder lines.
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    monkeypatch.setenv("BOTSQUAD_CLONE_URL", "https://example.com/bot-squad.git")
    monkeypatch.setenv("BOTSQUAD_REPO_REF", "master")
    # WEB_DIST has to point at a non-existent path so the SPA catch-all is
    # not mounted — otherwise it would shadow expected 404s on /i/<token>/*
    # paths in the MOTHERSHIP-off detach test.
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)
    return TestClient(build_app())


def _login(client) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


# ---- build-flag seam (T-0009 baseline) --------------------------------------


def test_mothership_off_means_no_routes(tmp_bot_squad: Path, monkeypatch):
    """Single-install build — /api/m/* + /i/* must not exist at all."""
    with _client(tmp_bot_squad, monkeypatch, mothership=False) as client:
        _login(client)
        r1 = client.get("/api/m/servers")
        # SPA fallback owns /i/<token>/install.sh in single-install builds:
        # confirm there is no real mothership route there.
        r2 = client.get("/i/anything/install.sh")
    assert r1.status_code == 404
    # /i/* falls through to the SPA only if /app/web/dist exists — in tests
    # it doesn't, so we expect 404 either way.
    assert r2.status_code == 404


def test_mothership_on_mounts_servers_empty(tmp_bot_squad: Path, monkeypatch):
    """Mothership build — router mounted, empty registry returns []."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/m/servers")
    assert r.status_code == 200
    assert r.json() == []


def test_mothership_on_requires_auth(tmp_bot_squad: Path, monkeypatch):
    """Cookie-auth routes inherit the same auth dependency as the rest."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.get("/api/m/servers")
    assert r.status_code == 401


# ---- token issuance (T-0024) ------------------------------------------------


def test_post_servers_mints_install_token(tmp_bot_squad: Path, monkeypatch):
    """POST /api/m/servers returns plaintext install_token exactly once."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post(
            "/api/m/servers",
            json={"display_name": "Test Srv", "base_url": "https://test.example.com"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"].startswith("srv_")
        token = body["install_token"]
        assert token.startswith(INSTALL_PREFIX)
        # install_url embeds the token in the URL path (not as a query string).
        assert token in body["install_url"]

        # Subsequent GET /api/m/servers MUST NOT leak the plaintext token or
        # the hash — only the public projection of the registry entry.
        listing = client.get("/api/m/servers").json()
        assert len(listing) == 1
        srv = listing[0]
        assert srv["id"] == body["id"]
        assert srv["install_state"] == "pending"
        assert "install_token" not in srv
        assert "install_token_hash" not in srv
        assert "server_bearer_hash" not in srv

        # Token hash IS persisted (verified via store, not the API).
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(body["id"])
        assert on_disk is not None
        assert on_disk.install_token_hash == hash_token(token)
        assert on_disk.install_token_expires_at is not None


# ---- install bundle GETs (no auth, no burn) ---------------------------------


def test_install_bundle_serves_substituted_script(tmp_bot_squad: Path, monkeypatch):
    """GET /i/<token>/install.sh substitutes 4 placeholders + does NOT burn."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        sid = body["id"]

        # Bundle GETs require no session cookie — installer fetches them
        # from a fresh box. Drop the session cookie to confirm.
        client.cookies.clear()

        r = client.get(f"/i/{token}/install.sh")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/x-shellscript")
        text = r.text
        # All four placeholders substituted; no leftover __PLACEHOLDER__ lines
        # on the substitution targets.
        assert f'BOTSQUAD_INSTALL_TOKEN:-{token}' in text
        assert "BOTSQUAD_MOTHERSHIP_URL:-https://mothership.test" in text
        assert "BOTSQUAD_CLONE_URL:-https://example.com/bot-squad.git" in text
        assert "BOTSQUAD_REPO_REF:-master" in text
        # The substitution targets section comments still reference the
        # placeholders by name — that's fine. Just check the executable
        # default-values lines are filled in.

        # Idempotent: re-fetch the same URL twice — token must still be
        # valid after the first GET. (Burn happens only at /connect.)
        r2 = client.get(f"/i/{token}/install.sh")
        assert r2.status_code == 200
        assert r2.text == r.text

        # And the instructions.md alongside it.
        r3 = client.get(f"/i/{token}/instructions.md")
        assert r3.status_code == 200, r3.text
        assert r3.headers["content-type"].startswith("text/markdown")
        assert token in r3.text
        assert "https://mothership.test" in r3.text

        # Store state unchanged after bundle GETs.
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(sid)
        assert on_disk is not None
        assert on_disk.install_token_hash == hash_token(token)
        assert on_disk.install_state == "pending"


def test_install_bundle_unknown_token_410(tmp_bot_squad: Path, monkeypatch):
    """Bogus install token → 410 Gone (so the installer + claude-supervisor
    can give the user the 'token expired' recipe verbatim).
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.get("/i/bsq_install_clearlyNotReal/install.sh")
    assert r.status_code == 410


def test_install_bundle_expired_token_410(tmp_bot_squad: Path, monkeypatch):
    """Expired install token → 410. Confirms TTL check runs."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        sid = body["id"]
        # Backdate expiry directly on disk to simulate a 24h-old token.
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        servers = store.list_servers()
        idx = next(i for i, s in enumerate(servers) if s.id == sid)
        old = servers[idx]
        servers[idx] = type(old)(
            **{**old.__dict__, "install_token_expires_at":
              (datetime.now(timezone.utc) - timedelta(hours=1))
              .isoformat().replace("+00:00", "Z")}
        )
        store.write(servers)
        client.cookies.clear()
        r = client.get(f"/i/{token}/install.sh")
    assert r.status_code == 410


# ---- /installer/connect handshake ------------------------------------------


def test_installer_connect_burns_token_and_mints_bearer(tmp_bot_squad: Path, monkeypatch):
    """POST /api/m/installer/connect: validate install_token, burn it,
    mint + return server_bearer. install_state pending → connected."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        sid = body["id"]
        client.cookies.clear()  # installer has no session cookie

        r = client.post(
            "/api/m/installer/connect",
            json={
                "token": token,
                "server_meta": {
                    "hostname": "fresh-box.example.com",
                    "install_dir": "/home/www/bot-squad",
                    "coordinator_user": "almdudleer",
                },
            },
        )
        assert r.status_code == 200, r.text
        out = r.json()
        bearer = out["server_bearer"]
        assert bearer.startswith(SERVER_PREFIX)
        assert out["server_id"] == sid

        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(sid)
        assert on_disk is not None
        assert on_disk.install_state == "connected"
        assert on_disk.install_token_hash is None
        assert on_disk.install_token_expires_at is None
        assert on_disk.server_bearer_hash == hash_token(bearer)


def test_installer_connect_burned_token_is_gone(tmp_bot_squad: Path, monkeypatch):
    """Second /connect with the same install_token → 410 Gone."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        client.cookies.clear()
        payload = {"token": token, "server_meta": {"hostname": "h"}}

        first = client.post("/api/m/installer/connect", json=payload)
        assert first.status_code == 200, first.text
        second = client.post("/api/m/installer/connect", json=payload)
    assert second.status_code == 410


def test_installer_connect_unknown_token_410(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/m/installer/connect",
            json={"token": "bsq_install_nope", "server_meta": {"hostname": "h"}},
        )
    assert r.status_code == 410


# ---- /installer/checkpoint POST + SSE replay -------------------------------


def test_checkpoint_accepts_install_token_pre_connect(tmp_bot_squad: Path, monkeypatch):
    """Installer can POST checkpoints during the pre-/connect phase, while
    its only bearer is the install token. Required because the installer
    runs `require_*` checkpoints BEFORE `mothership_handshake`."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        sid = body["id"]
        client.cookies.clear()

        r = client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "checkpoint": "require_linux",
                "status": "begin",
                "hostname": "fresh-box",
                "ts": "2026-05-14T17:30:00Z",
            },
        )
        assert r.status_code in (200, 204), r.text

        # Persisted to the per-server jsonl log so the wizard UI can replay
        # on reconnect.
        log = tmp_bot_squad / "data" / "_mothership" / "checkpoints" / f"{sid}.jsonl"
        assert log.exists()
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        assert lines[0]["checkpoint"] == "require_linux"
        assert lines[0]["status"] == "begin"


def test_checkpoint_accepts_server_bearer_post_connect(tmp_bot_squad: Path, monkeypatch):
    """Post-/connect, the installer's bearer is the server_bearer."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        sid = body["id"]
        client.cookies.clear()
        connect = client.post(
            "/api/m/installer/connect",
            json={"token": token, "server_meta": {"hostname": "h"}},
        ).json()
        bearer = connect["server_bearer"]

        r = client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {bearer}"},
            json={
                "checkpoint": "docker_compose_up",
                "status": "done",
                "hostname": "h",
                "ts": "2026-05-14T17:35:00Z",
            },
        )
        assert r.status_code in (200, 204), r.text

        log = tmp_bot_squad / "data" / "_mothership" / "checkpoints" / f"{sid}.jsonl"
        assert log.exists()


def test_checkpoint_rejects_bogus_bearer(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": "Bearer bsq_server_definitelyNotReal"},
            json={"checkpoint": "x", "status": "begin"},
        )
    assert r.status_code == 401


def test_checkpoint_post_burned_install_token_rejected(tmp_bot_squad: Path, monkeypatch):
    """After /connect, the install_token must no longer authenticate
    checkpoint POSTs — the installer is required to switch to server_bearer.
    Prevents replay if the install_token leaks late.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        client.cookies.clear()
        client.post(
            "/api/m/installer/connect",
            json={"token": token, "server_meta": {"hostname": "h"}},
        )
        r = client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {token}"},
            json={"checkpoint": "x", "status": "begin"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_sse_replays_persisted_checkpoints(tmp_bot_squad: Path, monkeypatch):
    """SSE handler's replay phase yields each persisted event verbatim,
    then a ``: replay-complete`` sentinel, before subscribing to the live
    queue. We consume the route function's response body directly via its
    async iterator rather than over a transport — that exercises the
    actual production code path (same generator Starlette would drive)
    without depending on the test transport's streaming behaviour.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    monkeypatch.setenv("MOTHERSHIP", "1")

    app = build_app()
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    entry, _token = store.register_server(
        display_name="S",
        base_url="https://s.example.com",
        owner_user="testuser",
    )
    for cp, status in [
        ("require_linux", "begin"),
        ("require_linux", "done"),
        ("install_tmux", "begin"),
    ]:
        store.append_checkpoint(
            entry.id,
            {"checkpoint": cp, "status": status, "hostname": "h",
             "ts": "2026-05-14T17:40:00Z"},
        )

    # Build a fake Request that satisfies the bits checkpoints_stream
    # touches: app.state.api_config (for _store) and is_disconnected().
    class _FakeRequest:
        def __init__(self) -> None:
            self.app = app
            self._disconnected = False
        async def is_disconnected(self) -> bool:
            return self._disconnected

    from app.routes_mothership import checkpoints_stream
    request = _FakeRequest()
    resp = await checkpoints_stream(entry.id, request)  # type: ignore[arg-type]
    assert resp.media_type == "text/event-stream"

    chunks: list[bytes] = []
    saw_sentinel = False
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        if b": replay-complete" in chunks[-1]:
            saw_sentinel = True
            request._disconnected = True  # let the generator exit cleanly
            break
    assert saw_sentinel
    # Drain any trailing chunks the generator emits before its exit.
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        # The disconnect check above only fires AFTER one wait_for cycle,
        # so we might pick up a single keepalive line before the generator
        # returns. Bound the drain so a regression in disconnect detection
        # surfaces as a test hang instead of an infinite loop.
        if len(chunks) > 10:
            break

    body = b"".join(chunks).decode("utf-8")
    events = [
        json.loads(block.split("data: ", 1)[1])
        for block in body.split("\n\n")
        if block.startswith("data: ")
    ]
    assert [e["checkpoint"] for e in events] == [
        "require_linux", "require_linux", "install_tmux",
    ]
    assert [e["status"] for e in events] == ["begin", "done", "begin"]


@pytest.mark.asyncio
async def test_sse_forwards_live_events_after_replay(tmp_bot_squad: Path, monkeypatch):
    """A checkpoint event published to the broadcaster AFTER the SSE
    handler enters the subscribe loop must reach the listener as a
    fresh ``data: ...`` frame. Locks the live-broadcast half of the
    contract — independent of any HTTP transport.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    monkeypatch.setenv("MOTHERSHIP", "1")

    app = build_app()
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    entry, _token = store.register_server(
        display_name="S",
        base_url="https://s.example.com",
        owner_user="testuser",
    )

    class _FakeRequest:
        def __init__(self) -> None:
            self.app = app
            self._disconnected = False
        async def is_disconnected(self) -> bool:
            return self._disconnected

    import asyncio as _asyncio
    from app.routes_mothership import _broadcast, checkpoints_stream

    request = _FakeRequest()
    resp = await checkpoints_stream(entry.id, request)  # type: ignore[arg-type]

    async def _drain_until_sentinel() -> None:
        async for chunk in resp.body_iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
            if ": replay-complete" in text:
                return

    async def _drain_until_live() -> dict:
        async for chunk in resp.body_iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
            if text.startswith("data: "):
                return json.loads(text.split("data: ", 1)[1].strip())
        raise AssertionError("body_iterator exhausted before live event")

    await _drain_until_sentinel()

    # Publish a live event. The subscribe loop's 0.5s poll picks it up
    # from the broadcast queue and yields a `data: ...` frame.
    _broadcast(entry.id, {
        "checkpoint": "live_event",
        "status": "begin",
        "hostname": "h",
        "ts": "2026-05-14T17:45:00Z",
    })

    received = await _asyncio.wait_for(_drain_until_live(), timeout=3.0)
    request._disconnected = True

    assert received["checkpoint"] == "live_event"
    assert received["status"] == "begin"


def test_sse_requires_auth(tmp_bot_squad: Path, monkeypatch):
    """SSE stream is for the wizard UI in the browser → cookie-auth."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.get("/api/m/servers/srv_anything/checkpoints")
    assert r.status_code == 401
