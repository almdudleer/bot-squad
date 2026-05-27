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


def _seed_install_bundle(tmp_bot_squad: Path) -> Path:
    """Write a minimal install bundle inside ``tmp_bot_squad`` carrying the
    four placeholder lines the substitution + assertions rely on. Self-
    contained so tests don't depend on the repo layout being reachable from
    ``__file__`` (the docker test runner mounts only ``api/`` at ``/app``,
    so the real ``scripts/install`` isn't visible inside the container).
    """
    bundle = tmp_bot_squad / "install-bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "install.sh").write_text(
        '#!/usr/bin/env bash\n'
        'BOTSQUAD_INSTALL_TOKEN="${BOTSQUAD_INSTALL_TOKEN:-__INSTALL_TOKEN__}"\n'
        'BOTSQUAD_MOTHERSHIP_URL="${BOTSQUAD_MOTHERSHIP_URL:-__MOTHERSHIP_URL__}"\n'
        'BOTSQUAD_CLONE_URL="${BOTSQUAD_CLONE_URL:-__CLONE_URL__}"\n'
        'BOTSQUAD_REPO_REF="${BOTSQUAD_REPO_REF:-__REPO_REF__}"\n',
        encoding="utf-8",
    )
    (bundle / "bootstrap-claude-instructions.md").write_text(
        '- Install token: `__INSTALL_TOKEN__`\n'
        '- Mothership URL: `__MOTHERSHIP_URL__`\n',
        encoding="utf-8",
    )
    return bundle


def _client(tmp_bot_squad: Path, monkeypatch, *, mothership: bool):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(_seed_install_bundle(tmp_bot_squad)))
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
    """Mothership build — router mounted, only the self-register entry present.

    T-0055 self-registers ``MOTHERSHIP_BASE_URL`` on boot, so a fresh registry
    is no longer empty. Confirm the one row is the self entry with
    ``is_self=True``, and no token surface leaks.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/m/servers")
    assert r.status_code == 200
    listing = r.json()
    assert len(listing) == 1
    self_entry = listing[0]
    assert self_entry["is_self"] is True
    assert self_entry["base_url"] == "https://mothership.test"
    assert self_entry["install_state"] == "ready"
    # Token surface stays stripped on the public projection.
    assert "install_token_hash" not in self_entry
    assert "server_bearer_hash" not in self_entry


def test_self_register_is_idempotent_across_reboots(tmp_bot_squad: Path, monkeypatch):
    """Booting the app twice against the same DATA_DIR yields one self entry,
    not two. Dedup is by ``base_url`` (trailing-slash insensitive)."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True):
        pass
    # Second boot — same DATA_DIR via the same monkeypatched env. We re-enter
    # the context manager to trigger another build_app() pass.
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/m/servers")
    listing = r.json()
    assert len(listing) == 1
    assert listing[0]["is_self"] is True

    # Direct store check — the on-disk row was not duplicated.
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    servers = store.list_servers()
    assert len(servers) == 1
    assert servers[0].is_self is True
    assert servers[0].base_url == "https://mothership.test"


def test_self_register_migrates_on_host_rename(tmp_bot_squad: Path, monkeypatch):
    """T-0109 — boot the mothership at URL_OLD, then re-boot at URL_NEW. The
    is_self row's base_url must MIGRATE in place (one row, new URL), not
    leave a stale zombie behind. Real-world driver: the .org→.dev domain
    flip (T-0033) shipped before this migration logic landed and left a
    duplicate srv_1a3a44462eee… row in production.

    Bypasses ``_client()`` because that helper hardcodes MOTHERSHIP_BASE_URL;
    this test needs the URL to change between the two boots.
    """
    # Common env (same DATA_DIR across both boots — that's how we exercise
    # the persistence + migration path).
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(_seed_install_bundle(tmp_bot_squad)))
    monkeypatch.setenv("BOTSQUAD_CLONE_URL", "https://example.com/bot-squad.git")
    monkeypatch.setenv("BOTSQUAD_REPO_REF", "master")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP", "1")

    # First boot: register self at the old host.
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    build_app()

    # Second boot: same DATA_DIR, different host.
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://renamed.test")
    build_app()

    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    servers = store.list_servers()
    assert len(servers) == 1, \
        f"host rename should migrate the is_self row, not duplicate; got {len(servers)} rows: {[s.base_url for s in servers]}"
    assert servers[0].is_self is True
    assert servers[0].base_url == "https://renamed.test", \
        f"old URL must be GC'd from disk after rename; got {servers[0].base_url}"


def test_self_register_promotes_existing_row_to_is_self(tmp_bot_squad: Path, monkeypatch):
    """If the registry already has a row whose base_url matches the
    mothership's own URL (e.g. an admin added it manually before T-0055
    shipped), boot should flip its ``is_self`` flag instead of duplicating.
    """
    # Seed the store before app boot.
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.register_server(
        display_name="manual-add",
        base_url="https://mothership.test",
        owner_user="testuser",
    )
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        listing = client.get("/api/m/servers").json()
    assert len(listing) == 1
    assert listing[0]["is_self"] is True
    assert listing[0]["base_url"] == "https://mothership.test"


def test_self_register_falls_back_when_base_url_unset(tmp_bot_squad: Path, monkeypatch):
    """If ``MOTHERSHIP_BASE_URL`` is unset, the self-register uses the
    documented fallback so a misconfigured deploy still surfaces SOME entry
    in the unified view. Follow-up ticket should make this strict."""
    # Re-prep the env without MOTHERSHIP_BASE_URL.
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv(
        "WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock")
    )
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.delenv("MOTHERSHIP_BASE_URL", raising=False)
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(_seed_install_bundle(tmp_bot_squad)))
    from app.main import build_app

    build_app()
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    servers = store.list_servers()
    assert len(servers) == 1
    assert servers[0].is_self is True
    assert servers[0].base_url == "https://staging.botsquad.dev"


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
        # The listing also contains the T-0055 self-register row; pick out the
        # row we just minted by id.
        listing = client.get("/api/m/servers").json()
        srv = next(s for s in listing if s["id"] == body["id"])
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
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(_seed_install_bundle(tmp_bot_squad)))
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
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(_seed_install_bundle(tmp_bot_squad)))
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


# ---- invite-join flow (T-0026) ---------------------------------------------


from app.install_tokens import INVITE_PREFIX  # noqa: E402 — section-local


def _mint_server(client) -> tuple[str, str]:
    """Helper: log in, mint a server, return (server_id, install_token)."""
    _login(client)
    body = client.post(
        "/api/m/servers",
        json={"display_name": "S", "base_url": "https://s.example.com"},
    ).json()
    return body["id"], body["install_token"]


def test_invite_mint_round_trip(tmp_bot_squad: Path, monkeypatch):
    """POST /api/m/servers/{id}/invites returns a plaintext invite token
    exactly once, with the correct prefix + role + target_username.

    Roundtrip: store the server, mint an invite, look up the server again
    and confirm (a) the registry has the SHA-256 hash but NOT the plaintext,
    (b) the public projection strips the hash entirely.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _install_token = _mint_server(client)
        r = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "edem", "role": "non-admin"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        token = body["invite_token"]
        assert token.startswith(INVITE_PREFIX)
        assert body["server_id"] == sid
        assert body["target_username"] == "edem"
        assert body["role"] == "non-admin"
        assert body["expires_at"] is not None
        # Install URL embeds the invite token — same /i/<token>/install.sh
        # shape so install.sh can prefix-detect at run-time.
        assert token in body["install_url"]
        assert token in body["instructions_url"]

        # Listing must NOT leak invite plaintext OR hash.
        listing = client.get("/api/m/servers").json()
        srv = next(s for s in listing if s["id"] == sid)
        assert "invites" in srv
        assert len(srv["invites"]) == 1
        inv = srv["invites"][0]
        assert inv["target_username"] == "edem"
        assert inv["role"] == "non-admin"
        assert "hash" not in inv  # public projection strips it

        # On-disk hash is the SHA-256 of the plaintext.
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(sid)
        assert on_disk is not None
        assert len(on_disk.invites) == 1
        assert on_disk.invites[0]["hash"] == hash_token(token)


def test_invite_mint_admin_role(tmp_bot_squad: Path, monkeypatch):
    """role='admin' is accepted and round-trips. installer/join must echo it
    back so the installer can decide whether to add the user to the
    bot-squad group."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        r = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "operator", "role": "admin"},
        )
        assert r.status_code == 200
        token = r.json()["invite_token"]
        client.cookies.clear()
        join = client.post("/api/m/installer/join", json={"token": token})
        assert join.status_code == 200, join.text
        assert join.json() == {
            "server_id": sid,
            "target_username": "operator",
            "role": "admin",
        }


def test_invite_mint_rejects_bad_role(tmp_bot_squad: Path, monkeypatch):
    """Only 'admin' / 'non-admin' are valid. Anything else → 400."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        r = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "x", "role": "root"},
        )
    assert r.status_code == 400


def test_invite_mint_unknown_server_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post(
            "/api/m/servers/srv_does_not_exist/invites",
            json={"target_username": "x", "role": "admin"},
        )
    assert r.status_code == 404


def test_installer_join_burns_invite(tmp_bot_squad: Path, monkeypatch):
    """POST /api/m/installer/join validates + burns the invite token,
    returns (server_id, target_username, role). Second attempt → 410.

    Burn semantics match the install-token contract: single-use. The
    invite plaintext leaves the user's tmpfs after one redemption.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        mint = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "edem", "role": "non-admin"},
        ).json()
        token = mint["invite_token"]
        client.cookies.clear()  # installer has no session cookie

        first = client.post("/api/m/installer/join", json={"token": token})
        assert first.status_code == 200, first.text
        assert first.json() == {
            "server_id": sid,
            "target_username": "edem",
            "role": "non-admin",
        }

        # Burned — registry no longer carries the invite at all.
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(sid)
        assert on_disk is not None
        assert on_disk.invites == []

        second = client.post("/api/m/installer/join", json={"token": token})
    assert second.status_code == 410


def test_installer_join_unknown_token_410(tmp_bot_squad: Path, monkeypatch):
    """Bogus invite token → 410 (matches the install-token unhappy-path)."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/m/installer/join",
            json={"token": "bsq_invite_definitelyNotReal"},
        )
    assert r.status_code == 410


def test_installer_join_expired_invite_410(tmp_bot_squad: Path, monkeypatch):
    """Expired invite → 410. Backdates expires_at directly on disk."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        token = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "edem", "role": "non-admin"},
        ).json()["invite_token"]

        # Backdate the invite's expiry directly via the store.
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        servers = store.list_servers()
        idx = next(i for i, s in enumerate(servers) if s.id == sid)
        old = servers[idx]
        stale_invites = [
            {**inv, "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1))
                .isoformat().replace("+00:00", "Z")}
            for inv in old.invites
        ]
        servers[idx] = type(old)(
            **{**old.__dict__, "invites": stale_invites},
        )
        store.write(servers)
        client.cookies.clear()
        r = client.post("/api/m/installer/join", json={"token": token})
    assert r.status_code == 410


# ---- install-token vs invite-token NON-INTERCHANGEABILITY -------------------
# Locks the DoD bullet: the two token kinds MUST NOT cross-validate. A
# leaked install_token cannot redeem an invite, and a leaked invite_token
# cannot redeem a /connect handshake (which would mint a server bearer
# the invitee was never supposed to hold).


def test_install_token_rejected_on_installer_join(tmp_bot_squad: Path, monkeypatch):
    """Install token presented to /installer/join → 400 'invite token
    required'. The 400 (vs 410) distinguishes "wrong KIND" from "wrong
    OR expired token" so audit logs can flag prefix mismatches as
    potential leakage attempts rather than benign TTL expiries.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _sid, install_token = _mint_server(client)
        client.cookies.clear()
        r = client.post("/api/m/installer/join", json={"token": install_token})
    assert r.status_code == 400
    assert "invite token" in r.json()["detail"].lower()


def test_invite_token_rejected_on_installer_connect(tmp_bot_squad: Path, monkeypatch):
    """Invite token presented to /installer/connect → 400 'install token
    required'. Even though both burn-points are POST + bearer-shaped,
    /connect is the install-side handshake (mints a server_bearer) and
    invitees are not supposed to hold a server_bearer.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        invite_token = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "edem", "role": "non-admin"},
        ).json()["invite_token"]
        client.cookies.clear()
        r = client.post(
            "/api/m/installer/connect",
            json={"token": invite_token, "server_meta": {"hostname": "h"}},
        )
    assert r.status_code == 400


def test_invite_token_rejected_as_checkpoint_bearer(tmp_bot_squad: Path, monkeypatch):
    """Invite token CAN'T be used as the checkpoint bearer. The bearer
    surface (Authorization: Bearer ...) is for install_token or
    server_bearer only; an invite is for /installer/join exclusively."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        invite_token = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "edem", "role": "non-admin"},
        ).json()["invite_token"]
        client.cookies.clear()
        r = client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {invite_token}"},
            json={"checkpoint": "x", "status": "begin"},
        )
    assert r.status_code == 401


def test_invite_token_bundle_get_serves_install_sh(tmp_bot_squad: Path, monkeypatch):
    """The bundle /i/<token>/install.sh path accepts BOTH install AND
    invite tokens — install.sh self-detects the prefix at runtime. Without
    this, an invitee's curl|bash would 410 before install.sh ever sees
    its first argument.

    Validates the symmetry but keeps the burn-point single: re-fetching
    the bundle does not consume the invite.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        invite_token = client.post(
            f"/api/m/servers/{sid}/invites",
            json={"target_username": "edem", "role": "non-admin"},
        ).json()["invite_token"]
        client.cookies.clear()

        r = client.get(f"/i/{invite_token}/install.sh")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/x-shellscript")
        # The invite token is substituted into the same BOTSQUAD_INSTALL_TOKEN
        # slot — install.sh detects the bsq_invite_ prefix and branches.
        assert f'BOTSQUAD_INSTALL_TOKEN:-{invite_token}' in r.text

        # No burn — re-fetch is byte-identical and the invite is still live.
        r2 = client.get(f"/i/{invite_token}/install.sh")
        assert r2.status_code == 200
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(sid)
        assert on_disk is not None
        assert len(on_disk.invites) == 1  # still there, not burned


# ---- T-0129: install-token revoke + re-mint --------------------------------


def test_install_token_revoke_clears_hash_and_invalidates(tmp_bot_squad: Path, monkeypatch):
    """POST /api/m/servers/{id}/install-tokens/revoke clears the hash + expiry.

    After revoke the install_token plaintext the FE has must no longer
    authenticate /connect (returns 410) — the burn-side contract is the
    same as a missing token from the registry's POV.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, token = _mint_server(client)
        r = client.post(f"/api/m/servers/{sid}/install-tokens/revoke")
        assert r.status_code == 200, r.text
        body = r.json()
        # Public projection — install_state stays pending; the hash + expiry
        # are absent (stripped by to_public; never serialised regardless).
        assert body["id"] == sid
        assert body["install_state"] == "pending"
        # On-disk row has the credential bits cleared.
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(sid)
        assert on_disk is not None
        assert on_disk.install_token_hash is None
        assert on_disk.install_token_expires_at is None
        # The plaintext that the FE / installer still holds no longer
        # authenticates /connect.
        client.cookies.clear()
        r2 = client.post(
            "/api/m/installer/connect",
            json={"token": token, "server_meta": {"hostname": "h"}},
        )
    assert r2.status_code == 410


def test_install_token_revoke_is_idempotent(tmp_bot_squad: Path, monkeypatch):
    """Re-revoking a server whose token is already cleared returns 200, not 409.

    The FE doesn't track whether /connect has burned the token already;
    a 200 either way means the button is safe to spam. Covers both
    paths: (a) already-revoked, (b) already-burned-by-/connect.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _ = _mint_server(client)
        first = client.post(f"/api/m/servers/{sid}/install-tokens/revoke")
        assert first.status_code == 200, first.text
        second = client.post(f"/api/m/servers/{sid}/install-tokens/revoke")
        assert second.status_code == 200, second.text
        assert second.json()["install_state"] == "pending"

        # Path (b): connect burns the token, then revoke is still 200.
        sid2, token2 = _mint_server(client)
        client.cookies.clear()
        connect = client.post(
            "/api/m/installer/connect",
            json={"token": token2, "server_meta": {"hostname": "h"}},
        )
        assert connect.status_code == 200, connect.text
        # Restore cookie auth for the super-admin revoke.
        _login(client)
        r = client.post(f"/api/m/servers/{sid2}/install-tokens/revoke")
    assert r.status_code == 200
    # State stays at "connected" — revoke on a burned server is a no-op
    # for the credential bits (already None) but doesn't roll back the
    # /connect transition.
    assert r.json()["install_state"] == "connected"


def test_install_token_revoke_unknown_server_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post("/api/m/servers/srv_does_not_exist/install-tokens/revoke")
    assert r.status_code == 404


def test_install_token_revoke_requires_super_admin(tmp_bot_squad: Path, monkeypatch):
    """Non-admin → 403. Same gate as /api/m/users."""
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        r = client.post("/api/m/servers/srv_x/install-tokens/revoke")
    assert r.status_code == 403


def test_install_token_remint_returns_fresh_and_invalidates_prior(tmp_bot_squad: Path, monkeypatch):
    """POST /install-tokens/mint mints a fresh token and invalidates the old.

    Re-mint envelope matches POST /api/m/servers — install_token +
    install_url + instructions_url + expires_at, plus the server id.
    The PRIOR plaintext must no longer authenticate /connect after the
    re-mint, otherwise rotation would leave two simultaneously-valid
    tokens until the old TTL expires.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, old_token = _mint_server(client)
        r = client.post(f"/api/m/servers/{sid}/install-tokens/mint")
        assert r.status_code == 200, r.text
        body = r.json()
        new_token = body["install_token"]
        assert new_token.startswith(INSTALL_PREFIX)
        assert new_token != old_token
        assert body["id"] == sid
        assert new_token in body["install_url"]
        assert new_token in body["instructions_url"]
        assert body["expires_at"] is not None

        # On-disk hash is the SHA-256 of the new plaintext, not the old.
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
        on_disk = store.get_server(sid)
        assert on_disk is not None
        assert on_disk.install_token_hash == hash_token(new_token)
        assert on_disk.install_state == "pending"

        # The OLD plaintext no longer authenticates /connect.
        client.cookies.clear()
        old_connect = client.post(
            "/api/m/installer/connect",
            json={"token": old_token, "server_meta": {"hostname": "h"}},
        )
        assert old_connect.status_code == 410
        # The NEW plaintext does.
        new_connect = client.post(
            "/api/m/installer/connect",
            json={"token": new_token, "server_meta": {"hostname": "h"}},
        )
    assert new_connect.status_code == 200


def test_install_token_remint_after_expiry(tmp_bot_squad: Path, monkeypatch):
    """Re-mint works when the prior token expired without a /connect — the
    primary motivating case in T-0129. Confirms the state-pending gate
    treats an expired-but-pending row as eligible.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, _old_token = _mint_server(client)
        # Backdate expiry directly on disk.
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
        # Server is still ``pending`` — only the expiry moved.
        r = client.post(f"/api/m/servers/{sid}/install-tokens/mint")
    assert r.status_code == 200, r.text
    assert r.json()["install_token"].startswith(INSTALL_PREFIX)


def test_install_token_remint_rejects_burned_server(tmp_bot_squad: Path, monkeypatch):
    """Re-mint against a server that already burned its token via /connect → 409.

    The consumer holds a server_bearer it would never know to drop, so
    re-minting an install_token for it would silently dead-letter.
    """
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        sid, token = _mint_server(client)
        client.cookies.clear()
        connect = client.post(
            "/api/m/installer/connect",
            json={"token": token, "server_meta": {"hostname": "h"}},
        )
        assert connect.status_code == 200
        _login(client)
        r = client.post(f"/api/m/servers/{sid}/install-tokens/mint")
    assert r.status_code == 409


def test_install_token_remint_unknown_server_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post("/api/m/servers/srv_does_not_exist/install-tokens/mint")
    assert r.status_code == 404


def test_install_token_remint_requires_super_admin(tmp_bot_squad: Path, monkeypatch):
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        r = client.post("/api/m/servers/srv_x/install-tokens/mint")
    assert r.status_code == 403


def test_invite_mint_requires_auth(tmp_bot_squad: Path, monkeypatch):
    """Cookie-auth gate on the invite-mint surface — only logged-in
    botsquad.dev users can issue invites."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/m/servers/srv_x/invites",
            json={"target_username": "edem", "role": "admin"},
        )
    assert r.status_code == 401
