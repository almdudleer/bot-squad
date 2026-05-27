from __future__ import annotations

import subprocess
import threading
import time
import tomllib
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


def _seed_git_repo(repo: Path) -> None:
    """Plant a minimal local git repo for Mode-3 attach tests."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "--quiet", "-m", "seed"],
        cwd=repo, check=True,
    )


def _client(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    app = build_app()
    return TestClient(app)


def _login(client) -> None:
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})


@pytest.fixture
def fake_worker_with_sessions(tmp_bot_squad: Path, request):
    """Worker fake that returns a configurable list_sessions response.

    Use via indirect parametrization or override `sessions` in the closure.
    Defaults to an empty list — i.e. status='idle', status_since=None.
    """
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    sessions: list[dict] = getattr(request, "param", None) or []

    fake = FastAPI()

    @fake.post("/actions/list_sessions")
    def list_sessions(params: dict | None = None) -> dict:
        return {"sessions": sessions}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock

    server.should_exit = True
    thread.join(timeout=5)


def test_projects_list_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects")
    assert r.status_code == 401


def test_projects_list_shape_has_quick_status(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    """T-0016: each project carries the canonical quick-status fields."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body == [{
        "slug": "test-project",
        "display_name": "Test Project",
        "status": "idle",
        "status_since": None,
    }]


@pytest.mark.parametrize(
    "fake_worker_with_sessions",
    [[
        {"sid": "S-u-x-p1", "status": "active", "started_at": "2026-05-14T09:00:00Z"},
        {"sid": "S-u-y-p2", "status": "paused", "paused_at": "2026-05-14T10:00:00Z"},
    ]],
    indirect=True,
)
def test_projects_list_active_wins_as_working(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    """Any active session → status=working, with since=max(started_at)."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["status"] == "working"
    assert body[0]["status_since"] == "2026-05-14T09:00:00Z"


@pytest.mark.parametrize(
    "fake_worker_with_sessions",
    [[
        {"sid": "S-u-x-p1", "status": "paused", "paused_at": "2026-05-14T11:00:00Z"},
    ]],
    indirect=True,
)
def test_projects_list_paused_only_is_needs_input(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects")
    body = r.json()
    assert body[0]["status"] == "needs-input"
    assert body[0]["status_since"] == "2026-05-14T11:00:00Z"


def test_projects_list_dead_worker_renders_idle(tmp_bot_squad: Path, monkeypatch):
    """Partial failure mode: dead worker → empty contribution → idle pill,
    NOT a 502. T-0025's cross-server view depends on this invariant."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "broken.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    with TestClient(build_app()) as client:
        _login(client)
        r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["status"] == "idle"
    assert body[0]["status_since"] is None


def test_project_get(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/test-project")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "test-project"
    assert "counts" in body


def test_project_get_unknown_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/nope")
    assert r.status_code == 404


def test_get_repo_agents_md(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").write_text("# Agents\n")
    try:
        with _client(tmp_bot_squad, monkeypatch) as client:
            _login(client)
            r = client.get("/api/projects/test-project/repo-agents-md")
        assert r.status_code == 200
        assert r.json()["content"] == "# Agents\n"
    finally:
        (repo / "AGENTS.md").unlink(missing_ok=True)


def test_get_repo_agents_md_missing_404(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").unlink(missing_ok=True)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/test-project/repo-agents-md")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# T-0087 — POST /projects/{slug}/deploy
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_worker_deploy(tmp_bot_squad: Path):
    """Minimal fake worker that records deploy calls and returns the same
    envelope the real worker returns."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    calls: list[dict] = []
    fake = FastAPI()

    @fake.post("/actions/deploy")
    def deploy(params: dict | None = None) -> dict:
        calls.append(params or {})
        return {"ok": True, "queue_id": "abc-123", "queued_at": 1234567890.0}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock, calls

    server.should_exit = True
    thread.join(timeout=5)


def test_deploy_queue_happy_path(
    tmp_bot_squad: Path, monkeypatch, fake_worker_deploy
):
    """Happy path: POST queues a deploy via the worker action and returns
    the worker envelope verbatim."""
    _, calls = fake_worker_deploy
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects/test-project/deploy",
            json={"target": "staging", "reason": "smoke test"},
        )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body == {
        "ok": True,
        "queue_id": "abc-123",
        "queued_at": 1234567890.0,
    }
    # Worker received the slug from the path + the auth context's username.
    assert len(calls) == 1
    payload = calls[0]
    assert payload["slug"] == "test-project"
    assert payload["target"] == "staging"
    assert payload["reason"] == "smoke test"
    assert payload["requested_by"] == "testuser"


def test_deploy_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects/test-project/deploy",
            json={"target": "staging", "reason": "x"},
        )
    assert r.status_code == 401


def test_deploy_unknown_slug_404(
    tmp_bot_squad: Path, monkeypatch, fake_worker_deploy
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects/nope/deploy",
            json={"target": "staging", "reason": "x"},
        )
    assert r.status_code == 404


def test_deploy_missing_fields_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_deploy
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        missing_target = client.post(
            "/api/projects/test-project/deploy",
            json={"reason": "x"},
        )
        missing_reason = client.post(
            "/api/projects/test-project/deploy",
            json={"target": "staging"},
        )
    assert missing_target.status_code == 400
    assert missing_reason.status_code == 400


def test_deploy_unknown_target_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_deploy
):
    """The conftest's test-project lists only ``staging`` in
    deploy_targets, so a request for ``prod`` is a clean 400 before we
    bother the worker."""
    _, calls = fake_worker_deploy
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects/test-project/deploy",
            json={"target": "prod", "reason": "x"},
        )
    assert r.status_code == 400
    # Worker should not have been called.
    assert calls == []


def test_get_repo_agents_md_unknown_project_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/nope/repo-agents-md")
    assert r.status_code == 404


def test_put_repo_agents_md(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").write_text("# Old\n")
    try:
        with _client(tmp_bot_squad, monkeypatch) as client:
            _login(client)
            r = client.put(
                "/api/projects/test-project/repo-agents-md",
                json={"content": "# New\n"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert (repo / "AGENTS.md").read_text() == "# New\n"
    finally:
        (repo / "AGENTS.md").unlink(missing_ok=True)


def test_put_repo_agents_md_empty_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.put(
            "/api/projects/test-project/repo-agents-md",
            json={"content": ""},
        )
    assert r.status_code == 400


def test_put_repo_agents_md_too_large_rejected(tmp_bot_squad: Path, monkeypatch):
    repo = Path("/tmp/test-repo")
    repo.mkdir(parents=True, exist_ok=True)
    try:
        with _client(tmp_bot_squad, monkeypatch) as client:
            _login(client)
            r = client.put(
                "/api/projects/test-project/repo-agents-md",
                json={"content": "x" * (200 * 1024 + 1)},
            )
        assert r.status_code == 400
    finally:
        (repo / "AGENTS.md").unlink(missing_ok=True)


def test_repo_agents_md_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/repo-agents-md")
    assert r.status_code == 401


# T-0021 — POST /api/projects: minimal create affordance.

def test_create_project_round_trip(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={"slug": "new-proj", "display_name": "New Proj"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # T-0051 added an optional `scaffold` summary field that is None
        # on the back-compat minimal-create (no mode passed).
        assert body == {
            "slug": "new-proj",
            "display_name": "New Proj",
            "status": "idle",
            "status_since": None,
            "scaffold": None,
        }

        listing = client.get("/api/projects").json()
        slugs = {p["slug"] for p in listing}
        assert {"test-project", "new-proj"} <= slugs

        detail = client.get("/api/projects/new-proj").json()
        assert detail["slug"] == "new-proj"
        assert detail["counts"] == {
            "backlog": 0, "vision": 0, "feedback": 0, "sessions": 0,
        }

    # Data dirs were actually created.
    for sub in ("backlog", "vision", "feedback", "sessions"):
        assert (tmp_bot_squad / "data" / "new-proj" / sub).is_dir()


def test_create_project_collision_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={"slug": "test-project", "display_name": "dup"},
        )
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_create_project_invalid_slug_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        for bad in ("Bad", "1leading", "has space", "has.dot", ""):
            r = client.post(
                "/api/projects",
                json={"slug": bad, "display_name": "X"},
            )
            assert r.status_code == 400, f"slug {bad!r} should 400, got {r.status_code}"


def test_create_project_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.post(
            "/api/projects",
            json={"slug": "p", "display_name": "P"},
        )
    assert r.status_code == 401


# T-0054 — POST /api/projects nudges the worker so peer_send to a fresh
# slug works without a worker restart.

@pytest.fixture
def fake_worker_reload(tmp_bot_squad: Path):
    """Fake worker that records reload_projects + peer_send calls.

    The worker side tracks ``known`` slugs across reload calls so peer_send
    to an unknown slug fails — mirroring the real-worker invariant that the
    coordinator only routes for projects it has loaded. ``known`` is seeded
    with the conftest's test-project and grows when reload_projects is
    invoked (we re-read projects.toml off the on-disk config dir, just like
    the real worker would).
    """
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    import tomllib

    calls: dict[str, list[dict]] = {"reload_projects": [], "peer_send": [], "list_sessions": []}

    config_dir = tmp_bot_squad / "config"
    known: set[str] = set()

    def _refresh_known() -> None:
        known.clear()
        raw = tomllib.loads((config_dir / "projects.toml").read_text())
        known.update((raw.get("projects") or {}).keys())

    _refresh_known()

    fake = FastAPI()

    @fake.post("/actions/reload_projects")
    def reload_projects(params: dict | None = None) -> dict:
        calls["reload_projects"].append(params or {})
        _refresh_known()
        return {"ok": True, "projects": sorted(known), "count": len(known)}

    from fastapi import HTTPException as _HTTPException

    @fake.post("/actions/peer_send")
    def peer_send(params: dict | None = None) -> dict:
        params = params or {}
        calls["peer_send"].append(params)
        slug = params.get("slug")
        if slug not in known:
            raise _HTTPException(status_code=400, detail=f"unknown slug {slug!r}")
        return {"ok": True, "delivered_to": [params.get("to")]}

    @fake.post("/actions/list_sessions")
    def list_sessions(params: dict | None = None) -> dict:
        calls["list_sessions"].append(params or {})
        return {"sessions": []}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock, calls

    server.should_exit = True
    thread.join(timeout=5)


def test_create_project_nudges_worker_then_peer_send_succeeds(
    tmp_bot_squad: Path, monkeypatch, fake_worker_reload,
):
    """Round-trip: POST /api/projects → worker reload_projects fires →
    peer_send to the brand-new slug succeeds without a worker restart."""
    sock, calls = fake_worker_reload
    import httpx

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={"slug": "fresh", "display_name": "Fresh"},
        )
    assert r.status_code == 201, r.text

    # API actually called the worker.
    assert len(calls["reload_projects"]) == 1

    # And the fake worker now routes peer_send to the fresh slug — proving
    # the in-memory view is up to date (the fixture re-reads projects.toml
    # on every reload, same as the real Config.load).
    async def _peer():
        transport = httpx.AsyncHTTPTransport(uds=str(sock))
        async with httpx.AsyncClient(transport=transport, base_url="http://w") as c:
            resp = await c.post(
                "/actions/peer_send",
                json={
                    "slug": "fresh",
                    "from_sid": "S-test-p1",
                    "to": "S-x",
                    "text": "hi",
                },
            )
            return resp.status_code, resp.json()

    import asyncio
    status, body = asyncio.run(_peer())
    assert status == 200, body
    assert body == {"ok": True, "delivered_to": ["S-x"]}


def test_create_project_returns_201_even_if_worker_unreachable(
    tmp_bot_squad: Path, monkeypatch,
):
    """No worker socket exists → reload call fails → API still 201s, and
    the on-disk state (projects.toml + dirs) is consistent."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    # Point WORKER_SOCK at a path that does not exist — no fake worker.
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "missing.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    with TestClient(build_app()) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={"slug": "lonely", "display_name": "Lonely"},
        )
    assert r.status_code == 201, r.text
    # On-disk state is consistent regardless of the worker's reachability.
    assert (tmp_bot_squad / "data" / "lonely" / "backlog").is_dir()
    projects_toml = (tmp_bot_squad / "config" / "projects.toml").read_text()
    assert "[projects.lonely]" in projects_toml


# ---------------------------------------------------------------------------
# T-0051 — POST /api/projects with `mode` for deep-flow scaffolding +
#          GET /api/projects/_/create-modes for the wizard rationale.
# ---------------------------------------------------------------------------


def test_create_modes_endpoint_returns_md(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/_/create-modes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "content" in body
    text = body["content"]
    # All three modes named; the rationale block is present. Doubles as
    # a smoke test that the canonical file shipped with the API bundle.
    assert "New from scratch" in text
    assert "paths-as-they-are" in text.lower()
    assert "destructive" in text.lower()
    assert "Why does bot-squad require this structure?" in text


def test_create_project_paths_as_they_are_round_trip(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    elsewhere = tmp_bot_squad / "elsewhere"
    existing_dev = elsewhere / "myproj-dev"
    existing_master = elsewhere / "myproj-master"
    _seed_git_repo(existing_dev)
    _seed_git_repo(existing_master)
    mother = tmp_bot_squad / "home" / "myproj"

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={
                "slug": "myproj",
                "display_name": "My Proj",
                "mode": "paths_as_they_are",
                "mother_dir": str(mother),
                "repo_path": str(existing_dev),
                "repo_master": str(existing_master),
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "myproj"
    assert body["scaffold"]["ops_linked"] == [
        str(existing_dev / "ops"),
        str(existing_master / "ops"),
    ]
    assert body["scaffold"]["ops_skipped"] == []

    # On-disk state: mother + .bot-squad.toml + ops symlinks all there.
    assert mother.is_dir()
    cfg_raw = tomllib.loads((mother / ".bot-squad.toml").read_text())
    assert cfg_raw["repo_path"] == str(existing_dev)
    assert cfg_raw["repo_master"] == str(existing_master)
    assert cfg_raw["repo_workspace"] == str(mother)
    for clone in (existing_dev, existing_master):
        link = clone / "ops"
        assert link.is_symlink()
        # Resolves to the install's per-slug data dir.
        assert link.resolve() == (tmp_bot_squad / "data" / "myproj").resolve()

    # The registry write picked up repo_master + repo_workspace too.
    pt = (tmp_bot_squad / "config" / "projects.toml").read_text()
    assert f'repo_master = "{existing_master}"' in pt
    assert f'repo_workspace = "{mother}"' in pt


def test_create_project_new_from_scratch_round_trip(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    mother = tmp_bot_squad / "home" / "fresh"

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={
                "slug": "fresh",
                "display_name": "Fresh",
                "mode": "new_from_scratch",
                "mother_dir": str(mother),
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "fresh"
    # Both clones got fresh ops symlinks (no prior ops/ on either).
    assert (mother / "dev" / "ops").is_symlink()
    assert (mother / "master" / "ops").is_symlink()
    # And both are real git checkouts.
    assert (mother / "dev" / ".git").exists()
    assert (mother / "master" / ".git").exists()

    cfg_raw = tomllib.loads((mother / ".bot-squad.toml").read_text())
    assert cfg_raw["repo_path"] == str(mother / "dev")
    assert cfg_raw["repo_master"] == str(mother / "master")

    pt = (tmp_bot_squad / "config" / "projects.toml").read_text()
    assert f'repo_path = "{mother / "dev"}"' in pt
    assert f'repo_master = "{mother / "master"}"' in pt


def test_create_project_unknown_mode_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={
                "slug": "bad",
                "display_name": "Bad",
                "mode": "not_a_mode",
            },
        )
    assert r.status_code == 400
    assert "unknown mode" in r.json()["detail"]


def test_create_project_destructive_mode_501(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    """Mode 2 is peeled to a follow-up; until it lands the API must
    answer 501 so the FE can render a 'come back later' state instead
    of silently falling through to a minimal-create."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={
                "slug": "dst",
                "display_name": "Dst",
                "mode": "attach_destructive",
                "mother_dir": "/tmp/dst",
                "existing_path": "/tmp/dst-src",
                "existing_becomes": "dev",
                "confirm_destructive_move": True,
            },
        )
    assert r.status_code == 501
    assert "not implemented" in r.json()["detail"]


def test_create_project_paths_as_they_are_missing_field_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={
                "slug": "miss",
                "display_name": "Miss",
                "mode": "paths_as_they_are",
                "mother_dir": "/tmp/miss",
                "repo_path": "/tmp/miss-dev",
                # repo_master missing
            },
        )
    assert r.status_code == 400
    assert "repo_master" in r.json()["detail"]


def test_create_project_paths_as_they_are_relative_path_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={
                "slug": "rel",
                "display_name": "Rel",
                "mode": "paths_as_they_are",
                "mother_dir": "relative/path",
                "repo_path": "/tmp/x",
                "repo_master": "/tmp/y",
            },
        )
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"]


def test_create_project_paths_as_they_are_repo_missing_400(
    tmp_bot_squad: Path, monkeypatch, fake_worker_with_sessions: Path,
):
    """If a repo path doesn't exist on disk, the scaffold pre-check
    bubbles a ScaffoldError up as a 400 — and no half-built state
    (mother dir, projects.toml entry) is left behind."""
    mother = tmp_bot_squad / "home" / "ghost"
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={
                "slug": "ghost",
                "display_name": "Ghost",
                "mode": "paths_as_they_are",
                "mother_dir": str(mother),
                "repo_path": str(tmp_bot_squad / "no-dev"),
                "repo_master": str(tmp_bot_squad / "no-master"),
            },
        )
    assert r.status_code == 400, r.text
    assert "does not exist" in r.json()["detail"]
    assert not mother.exists()
    # And projects.toml did NOT pick up a phantom entry.
    pt = (tmp_bot_squad / "config" / "projects.toml").read_text()
    assert "[projects.ghost]" not in pt


def test_create_project_requires_admin(tmp_bot_squad: Path, monkeypatch):
    # Demote testuser so the admin gate fires (default fixture is admin).
    auth_path = tmp_bot_squad / "config" / "auth.toml"
    auth_path.write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post(
            "/api/projects",
            json={"slug": "p", "display_name": "P"},
        )
    assert r.status_code == 403
