"""Tests for the consumer-telemetry router (T-0088).

Covers the DoD cases from
``backlog/T-0088-consumer-version-telemetry-to-mothership.md``:

* POST with a known ``install_id`` persists and is round-trippable via GET.
* POST with an unknown ``install_id`` → 403 (and doesn't leak whether the
  registry is empty vs. just doesn't contain that id).
* POST with no ``install_id`` → 400.
* GET is mothership-only — 404 unless ``MOTHERSHIP=1``.
* GET requires cookie auth on a mothership build.

The tests seed servers directly via ``MothershipStore`` rather than going
through the install flow so they stay focused on the telemetry surface.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _client(tmp_bot_squad: Path, monkeypatch, *, mothership: bool) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv(
        "WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock")
    )
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    # WEB_DIST has to point at a non-existent path so the SPA catch-all
    # is not mounted and our 404 expectations on /api/releases/* are
    # genuinely from the router (matches routes_mothership tests).
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)
    return TestClient(build_app())


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def _store(tmp_bot_squad: Path) -> MothershipStore:
    return MothershipStore(tmp_bot_squad / "data" / "_mothership")


def _seed_server(
    tmp_bot_squad: Path,
    *,
    server_id: str = "srv_consumer_one",
    display_name: str = "Consumer One",
    base_url: str = "https://consumer-one.example",
) -> AttachedServer:
    """Insert an attached-server row directly so telemetry tests don't
    depend on the install-token flow."""
    store = _store(tmp_bot_squad)
    entry = AttachedServer(
        id=server_id,
        display_name=display_name,
        base_url=base_url,
        owner_user="testuser",
        created_at="2026-05-16T16:00:00Z",
        install_state="ready",
    )
    existing = store.list_servers()
    store.write(existing + [entry])
    return entry


def _telemetry_body(install_id: str, **overrides) -> dict:
    body = {
        "install_id": install_id,
        "installed_version": "v2026.05.16.2",
        "last_check_at": "2026-05-16T17:00:00+00:00",
        "last_apply_at": "2026-05-16T16:30:00+00:00",
        "last_apply_outcome": "success",
        "current_git_sha": "abc123def456",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# DoD: POST persists + GET round-trips it
# ---------------------------------------------------------------------------

def test_post_valid_install_id_persists(tmp_bot_squad: Path, monkeypatch):
    """Happy path: POST with a known install_id stores the snapshot."""
    _seed_server(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/releases/_telemetry", json=_telemetry_body("srv_consumer_one")
        )
    assert r.status_code == 204

    # Read directly off the store so this assertion doesn't ride on GET.
    server = _store(tmp_bot_squad).get_server("srv_consumer_one")
    assert server is not None
    assert server.release == {
        "installed_version": "v2026.05.16.2",
        "last_check_at": "2026-05-16T17:00:00+00:00",
        "last_apply_at": "2026-05-16T16:30:00+00:00",
        "last_apply_outcome": "success",
        "current_git_sha": "abc123def456",
    }


def test_round_trip_post_then_get(tmp_bot_squad: Path, monkeypatch):
    """POST + GET on the same client round-trips the snapshot with the
    registry's display_name joined in for the UI."""
    _seed_server(
        tmp_bot_squad,
        server_id="srv_round_trip",
        display_name="Round Trip Consumer",
    )
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        post = client.post(
            "/api/releases/_telemetry", json=_telemetry_body("srv_round_trip")
        )
        assert post.status_code == 204
        _login(client)
        get = client.get("/api/releases/_telemetry")
    assert get.status_code == 200
    rows = get.json()
    assert isinstance(rows, list)
    matching = [r for r in rows if r["install_id"] == "srv_round_trip"]
    assert len(matching) == 1
    row = matching[0]
    assert row["install_name"] == "Round Trip Consumer"
    assert row["installed_version"] == "v2026.05.16.2"
    assert row["last_apply_outcome"] == "success"
    assert row["current_git_sha"] == "abc123def456"


def test_post_updates_existing_snapshot(tmp_bot_squad: Path, monkeypatch):
    """A second POST replaces (not appends to) the prior snapshot —
    v0 stores only the latest per install."""
    _seed_server(tmp_bot_squad, server_id="srv_replay")
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        first = client.post(
            "/api/releases/_telemetry",
            json=_telemetry_body("srv_replay", installed_version="v2026.05.16.1"),
        )
        assert first.status_code == 204
        second = client.post(
            "/api/releases/_telemetry",
            json=_telemetry_body(
                "srv_replay",
                installed_version="v2026.05.16.2",
                last_apply_outcome="failed:apply",
            ),
        )
        assert second.status_code == 204

    server = _store(tmp_bot_squad).get_server("srv_replay")
    assert server is not None
    assert server.release["installed_version"] == "v2026.05.16.2"
    assert server.release["last_apply_outcome"] == "failed:apply"


def test_post_drops_extra_fields(tmp_bot_squad: Path, monkeypatch):
    """Anything outside the spec'd allowlist must not be persisted."""
    _seed_server(tmp_bot_squad, server_id="srv_extra")
    body = _telemetry_body("srv_extra")
    body["secret_field"] = "leaked"
    body["another_extra"] = {"nested": True}
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post("/api/releases/_telemetry", json=body)
    assert r.status_code == 204

    server = _store(tmp_bot_squad).get_server("srv_extra")
    assert server is not None
    assert "secret_field" not in server.release
    assert "another_extra" not in server.release


# ---------------------------------------------------------------------------
# DoD: unknown install_id → 403
# ---------------------------------------------------------------------------

def test_post_unknown_install_id_returns_403(tmp_bot_squad: Path, monkeypatch):
    _seed_server(tmp_bot_squad, server_id="srv_known")
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/releases/_telemetry", json=_telemetry_body("srv_does_not_exist")
        )
    assert r.status_code == 403


def test_post_missing_install_id_returns_400(tmp_bot_squad: Path, monkeypatch):
    """The route distinguishes "client sent no id" (400) from "id is
    unknown" (403) so a misbehaving consumer surfaces the right fix."""
    _seed_server(tmp_bot_squad)
    body = _telemetry_body("placeholder")
    body.pop("install_id")
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post("/api/releases/_telemetry", json=body)
    assert r.status_code == 400


def test_post_empty_install_id_returns_400(tmp_bot_squad: Path, monkeypatch):
    _seed_server(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/releases/_telemetry", json=_telemetry_body("   ")  # whitespace
        )
    assert r.status_code == 400


def test_post_on_non_mothership_403s_every_id(tmp_bot_squad: Path, monkeypatch):
    """Detached single-installs have an empty mothership_store, so every
    install_id is "unknown" → 403. The POST endpoint stays mounted (it's
    on the always-mounted releases router) so a stray probe is still
    answered deterministically."""
    # Note: no _seed_server — and importantly no MOTHERSHIP=1, so the
    # store is also untouched by the self-register path.
    with _client(tmp_bot_squad, monkeypatch, mothership=False) as client:
        r = client.post(
            "/api/releases/_telemetry", json=_telemetry_body("srv_anything")
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DoD: GET respects mothership-only gating
# ---------------------------------------------------------------------------

def test_get_is_404_on_non_mothership(tmp_bot_squad: Path, monkeypatch):
    """Single-install (MOTHERSHIP unset) → GET returns 404 even after a
    valid session login. The detach build doesn't expose the roll-up."""
    with _client(tmp_bot_squad, monkeypatch, mothership=False) as client:
        _login(client)
        r = client.get("/api/releases/_telemetry")
    assert r.status_code == 404


def test_get_requires_auth_on_mothership(tmp_bot_squad: Path, monkeypatch):
    """No cookie → 401, same as the other ``/api/m`` cookie-surface routes."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.get("/api/releases/_telemetry")
    assert r.status_code == 401


def test_get_includes_servers_with_no_telemetry_yet(
    tmp_bot_squad: Path, monkeypatch
):
    """A server registered but never reporting still appears in the
    roll-up with null release fields — so the UI can flag "registered
    but not checking in"."""
    _seed_server(
        tmp_bot_squad, server_id="srv_silent", display_name="Silent Consumer"
    )
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/releases/_telemetry")
    assert r.status_code == 200
    rows = r.json()
    matching = [row for row in rows if row["install_id"] == "srv_silent"]
    assert len(matching) == 1
    silent = matching[0]
    assert silent["install_name"] == "Silent Consumer"
    assert silent["installed_version"] is None
    assert silent["last_check_at"] is None
    assert silent["last_apply_outcome"] is None
