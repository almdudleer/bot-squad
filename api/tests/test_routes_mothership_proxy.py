"""Per-server-call routing proxy (T-0023).

The mothership exposes ``GET/POST/PUT/PATCH/DELETE /api/m/servers/{id}/api/{path}``
which forwards to the attached server's API using the stored server-bearer.
This is the BE half of the seam contract in
``docs/architecture/D-0017-mothership-seam.md`` (section "Per-server backend
client (T-0023)"). The FE consumes it via ``apiFor(serverId)`` in
``web/src/mothership/api.ts``.

These tests use ``httpx.MockTransport`` to stand in for the upstream
single-install API — we never need a real second server to verify the
proxy's behaviour.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import routes_mothership
from app.install_tokens import hash_token, mint_server_bearer
from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore


# ---- helpers ----------------------------------------------------------------


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    return TestClient(build_app())


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def _seed_connected_server(
    tmp_bot_squad: Path,
    *,
    server_id: str = "srv_test01",
    base_url: str = "https://target.example.com",
    projects_cache: list[dict] | None = None,
) -> tuple[AttachedServer, str]:
    """Seed a server in the registry in the ``connected`` state with a
    bearer-plaintext sidecar on disk.

    Returns ``(entry, plaintext_bearer)``. The plaintext is also written to
    ``DATA_DIR/_mothership/bearers/<server_id>`` (the file p29 lands at
    /connect time — schema frozen in the seam doc amendment).
    """
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    bearer = mint_server_bearer()
    entry = AttachedServer(
        id=server_id,
        display_name="Target Test",
        base_url=base_url,
        owner_user="testuser",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        install_state="connected",
        server_bearer_hash=hash_token(bearer),
        projects_cache=projects_cache or [],
    )
    store.write([entry])
    bearers_dir = tmp_bot_squad / "data" / "_mothership" / "bearers"
    bearers_dir.mkdir(parents=True, exist_ok=True)
    bearer_path = bearers_dir / server_id
    bearer_path.write_text(bearer, encoding="utf-8")
    bearer_path.chmod(0o600)
    return entry, bearer


def _seed_self_server(
    tmp_bot_squad: Path,
    *,
    server_id: str = "srv_self01",
    base_url: str = "https://mothership.test",
) -> AttachedServer:
    """Seed the mothership's OWN ``is_self`` server with NO bearer sidecar.

    The mothership never minted a server_bearer for itself, so the bearers/
    sidecar file is deliberately absent — proving the self fan-in path needs
    no bearer (T-0312).
    """
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    entry = AttachedServer(
        id=server_id,
        display_name="This Server",
        base_url=base_url,
        owner_user="testuser",
        created_at=datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        install_state="ready",
        is_self=True,
    )
    store.write([entry])
    return entry


class _Recorder:
    """Captures every upstream call the proxy makes, returns canned responses."""

    def __init__(self, responder):
        self.calls: list[httpx.Request] = []
        self._responder = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._responder(request)


def _install_mock_upstream(monkeypatch, responder) -> _Recorder:
    """Replace ``routes_mothership._proxy_client`` with one wired to a
    ``httpx.MockTransport`` that calls ``responder(request)`` for each call.
    """
    recorder = _Recorder(responder)

    def _factory(base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler),
            base_url=base_url,
        )

    monkeypatch.setattr(routes_mothership, "_proxy_client", _factory)
    return recorder


# ---- tests ------------------------------------------------------------------


def test_proxy_forwards_get_with_bearer(tmp_bot_squad: Path, monkeypatch):
    """Happy path: GET /api/m/servers/<id>/api/health proxies to
    ``<server.base_url>/api/health`` with ``Authorization: Bearer <bearer>``
    and returns the upstream response body + status.
    """
    _, bearer = _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        base_url="https://target.example.com",
    )

    def responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "ts": 42},
            headers={"content-type": "application/json"},
        )

    recorder = _install_mock_upstream(monkeypatch, responder)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/m/servers/srv_test01/api/health")

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "ts": 42}
    assert len(recorder.calls) == 1
    upstream = recorder.calls[0]
    assert upstream.method == "GET"
    assert str(upstream.url) == "https://target.example.com/api/health"
    assert upstream.headers.get("authorization") == f"Bearer {bearer}"


def test_proxy_unknown_server_returns_404_from_proxy(
    tmp_bot_squad: Path, monkeypatch
):
    """The proxy's own 404 (not FastAPI's no-route 404) — distinguish via
    the response body. No upstream call is made.
    """
    recorder = _install_mock_upstream(
        monkeypatch,
        lambda req: pytest.fail(f"unexpected upstream call: {req.url}"),
    )

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/m/servers/srv_unknown/api/health")

    assert r.status_code == 404
    assert "server not found" in r.text.lower()
    assert recorder.calls == []


def test_proxy_upstream_unreachable_returns_502(
    tmp_bot_squad: Path, monkeypatch
):
    """If the upstream errors out (timeout, conn reset, DNS), the proxy
    returns 502 with a detail mentioning the upstream — the FE fanOut
    layer keys off this to flag a server as dead without poisoning
    siblings.
    """
    _seed_connected_server(tmp_bot_squad, server_id="srv_dead")

    def explode(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known")

    _install_mock_upstream(monkeypatch, explode)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/m/servers/srv_dead/api/health")

    assert r.status_code == 502
    assert "upstream" in r.text.lower()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_proxy_forwards_non_get_methods_with_body(
    tmp_bot_squad: Path, monkeypatch, method: str
):
    """Non-GET methods carry a JSON body through to the upstream.

    apiFor() exposes the same surface as the single-install API — that
    surface includes mutations (PATCH/PUT/POST/DELETE on tasks, sessions,
    vision, etc.), so the proxy must be method-agnostic.
    """
    _, bearer = _seed_connected_server(tmp_bot_squad, server_id="srv_mut01")
    body_in = {"title": "new task", "priority": 7}

    def responder(req: httpx.Request) -> httpx.Response:
        # echo the request body back so the test can assert the proxy
        # passed the bytes through unmodified.
        return httpx.Response(
            200,
            json={"echoed": json.loads(req.content), "method": req.method},
            headers={"content-type": "application/json"},
        )

    recorder = _install_mock_upstream(monkeypatch, responder)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.request(
            method,
            "/api/m/servers/srv_mut01/api/projects/foo/backlog",
            json=body_in,
        )

    assert r.status_code == 200, r.text
    assert r.json() == {"echoed": body_in, "method": method}
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call.method == method
    assert str(call.url).endswith("/api/projects/foo/backlog")
    assert call.headers.get("authorization") == f"Bearer {bearer}"


def test_proxy_forwards_upstream_status_codes(
    tmp_bot_squad: Path, monkeypatch
):
    """Upstream 4xx/5xx pass through unchanged. The mothership doesn't
    reinterpret them — the FE handles per-server error semantics.

    EXCEPTION: upstream 502 from the proxy itself (httpx.RequestError) is
    distinct from an upstream that *responds with* a 502. The latter is
    a genuine upstream answer and should pass through.
    """
    _seed_connected_server(tmp_bot_squad, server_id="srv_err01")

    def responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            418, json={"error": "i'm a teapot"},
            headers={"content-type": "application/json"},
        )

    _install_mock_upstream(monkeypatch, responder)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/m/servers/srv_err01/api/health")

    assert r.status_code == 418
    assert r.json() == {"error": "i'm a teapot"}


# ---- is_self server → local fan-in, no bearer (T-0312) ----------------------


def test_proxy_self_server_fans_into_local_without_bearer(
    tmp_bot_squad: Path, monkeypatch
):
    """T-0312: the generic proxy resolves the ``is_self`` server LOCALLY (no
    bearer required) — same data the home view's ``list_server_projects``
    already fans in — so the cross-server project route never 503s when
    reached directly for the mothership's own server.

    The self server has no bearer sidecar; the old code read the (missing)
    bearer and 503'd. The fix dispatches in-process to the local app, so the
    ``_proxy_client`` upstream hop is never taken (recorder stays empty).
    """
    _seed_self_server(tmp_bot_squad, server_id="srv_self01")
    recorder = _install_mock_upstream(
        monkeypatch,
        lambda req: pytest.fail(f"unexpected upstream proxy hop: {req.url}"),
    )

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        local = client.get("/api/projects")
        assert local.status_code == 200, local.text
        r = client.get("/api/m/servers/srv_self01/api/projects")

    assert r.status_code == 200, r.text
    assert r.json() == local.json()
    assert recorder.calls == []


def test_proxy_self_server_forwards_session_identity(
    tmp_bot_squad: Path, monkeypatch
):
    """T-0312: the self fan-in carries the caller's session so the local
    routes authenticate as the same user. ``/api/auth/me`` round-trips the
    logged-in username through the proxy unchanged."""
    _seed_self_server(tmp_bot_squad, server_id="srv_self02")
    _install_mock_upstream(
        monkeypatch,
        lambda req: pytest.fail(f"unexpected upstream proxy hop: {req.url}"),
    )

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        direct = client.get("/api/auth/me")
        assert direct.status_code == 200, direct.text
        via_proxy = client.get("/api/m/servers/srv_self02/api/auth/me")

    assert via_proxy.status_code == 200, via_proxy.text
    assert via_proxy.json().get("username") == direct.json().get("username")


# ---- /servers/{id}/projects (cached + refresh) ------------------------------


def test_servers_projects_returns_cached_list(tmp_bot_squad: Path, monkeypatch):
    """GET /api/m/servers/<id>/projects returns the registry's
    projects_cache for that server — no upstream call. T-0025 polls this
    on the all-projects page to render server groups instantly.
    """
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_p01",
        projects_cache=[
            {"slug": "alpha", "display_name": "Alpha", "status": "working"},
            {"slug": "beta", "display_name": "Beta", "status": "idle"},
        ],
    )
    recorder = _install_mock_upstream(
        monkeypatch,
        lambda req: pytest.fail(f"unexpected upstream call: {req.url}"),
    )

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/m/servers/srv_p01/projects")

    assert r.status_code == 200
    assert r.json() == [
        {"slug": "alpha", "display_name": "Alpha", "status": "working"},
        {"slug": "beta", "display_name": "Beta", "status": "idle"},
    ]
    assert recorder.calls == []


def test_servers_projects_unknown_server_404(tmp_bot_squad: Path, monkeypatch):
    """Cached-projects on unknown server → 404 from the route itself."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/m/servers/srv_unknown/projects")
    assert r.status_code == 404


def test_servers_projects_refresh_fetches_upstream_and_updates_cache(
    tmp_bot_squad: Path, monkeypatch
):
    """POST /api/m/servers/<id>/projects/refresh proxies GET /api/projects
    to the upstream, writes the result into the registry's projects_cache,
    and returns the updated list. Subsequent GETs see the new cache.
    """
    _, bearer = _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_r01",
        projects_cache=[
            {"slug": "stale", "display_name": "Stale", "status": "idle"},
        ],
    )

    fresh_listing = [
        {"slug": "alpha", "display_name": "Alpha"},
        {"slug": "beta", "display_name": "Beta"},
    ]

    def responder(req: httpx.Request) -> httpx.Response:
        assert str(req.url).endswith("/api/projects")
        assert req.headers.get("authorization") == f"Bearer {bearer}"
        return httpx.Response(
            200, json=fresh_listing,
            headers={"content-type": "application/json"},
        )

    _install_mock_upstream(monkeypatch, responder)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        refresh = client.post("/api/m/servers/srv_r01/projects/refresh")
        assert refresh.status_code == 200, refresh.text
        # Refresh returns the persisted cache shape, which normalizes a
        # ``status`` field even if upstream didn't supply one.
        body = refresh.json()
        assert [p["slug"] for p in body] == ["alpha", "beta"]
        for p in body:
            assert p["status"] == "idle"

        cached = client.get("/api/m/servers/srv_r01/projects").json()
        assert [p["slug"] for p in cached] == ["alpha", "beta"]

    # Registry on disk reflects the new cache.
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    on_disk = store.get_server("srv_r01")
    assert on_disk is not None
    assert [p["slug"] for p in on_disk.projects_cache] == ["alpha", "beta"]


def test_servers_projects_refresh_upstream_dead_does_not_clobber_cache(
    tmp_bot_squad: Path, monkeypatch
):
    """If the upstream is unreachable during a refresh, the existing cache
    must NOT be cleared — a transient outage shouldn't blank the
    all-projects view's last-known-good for that server.
    """
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_dead02",
        projects_cache=[
            {"slug": "keep_me", "display_name": "Keep Me", "status": "idle"},
        ],
    )

    def explode(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dead")

    _install_mock_upstream(monkeypatch, explode)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post("/api/m/servers/srv_dead02/projects/refresh")
        assert r.status_code == 502
        cached = client.get("/api/m/servers/srv_dead02/projects").json()

    assert [p["slug"] for p in cached] == ["keep_me"]
