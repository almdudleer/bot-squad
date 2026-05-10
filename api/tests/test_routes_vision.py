from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _logged_in(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    c = TestClient(build_app())
    c.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return c


def _anon(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Existing read test
# ---------------------------------------------------------------------------

def test_vision_lists_files_and_initiatives(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "north-star.md").write_text("# North star\n\nAim true.\n")
    (vision / "strategy.md").write_text("# Strategy\n\nBet on X.\n")
    (vision / "initiatives").mkdir()
    (vision / "initiatives" / "v0.6.md").write_text("# v0.6\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.get("/api/projects/test-project/vision")
    assert r.status_code == 200
    names = [x["name"] for x in r.json()]
    assert "north-star.md" in names
    assert "strategy.md" in names
    assert "initiatives/v0.6.md" in names


# ---------------------------------------------------------------------------
# PUT /api/projects/{slug}/vision/{name}
# ---------------------------------------------------------------------------

def test_put_vision_replaces_content(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "north-star.md").write_text("# Old content\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/vision/north-star.md",
            json={"content": "# New content\n"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert (vision / "north-star.md").read_text() == "# New content\n"


def test_put_vision_initiative(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "initiatives").mkdir()
    (vision / "initiatives" / "v0.6.md").write_text("# Old\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/vision/initiatives%2Fv0.6.md",
            json={"content": "# Updated initiative\n"},
        )
    assert r.status_code == 200
    assert (vision / "initiatives" / "v0.6.md").read_text() == "# Updated initiative\n"


def test_put_vision_file_not_found(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/vision/nonexistent.md",
            json={"content": "# content\n"},
        )
    assert r.status_code == 404


def test_put_vision_empty_content_rejected(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "north-star.md").write_text("# Old\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/vision/north-star.md",
            json={"content": ""},
        )
    assert r.status_code == 400


def test_put_vision_too_large_rejected(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "north-star.md").write_text("# Old\n")
    big = "x" * (200 * 1024 + 1)
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/vision/north-star.md",
            json={"content": big},
        )
    assert r.status_code == 400


def test_put_vision_traversal_rejected(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/vision/invalid_name!.md",
            json={"content": "# x\n"},
        )
    assert r.status_code == 400


def test_put_vision_requires_auth(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "north-star.md").write_text("# Old\n")
    with _anon(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/vision/north-star.md",
            json={"content": "# x\n"},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/vision — new initiative
# ---------------------------------------------------------------------------

def test_post_initiative_creates_file(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/vision",
            json={"kind": "initiative", "name": "My Initiative", "content": "# My Initiative\n"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "name" in data
    # File should exist
    init_dir = vision / "initiatives"
    assert init_dir.exists()
    assert any(init_dir.glob("*.md"))


def test_post_initiative_default_content(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/vision",
            json={"kind": "initiative", "name": "Auto Content"},
        )
    assert r.status_code == 200
    init_dir = vision / "initiatives"
    files = list(init_dir.glob("*.md"))
    assert len(files) == 1
    assert "Auto Content" in files[0].read_text()


def test_post_initiative_409_on_collision(tmp_bot_squad: Path, monkeypatch):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "initiatives").mkdir()
    (vision / "initiatives" / "my-init.md").write_text("# existing\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/vision",
            json={"kind": "initiative", "name": "My Init"},
        )
    assert r.status_code == 409


def test_post_initiative_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _anon(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/vision",
            json={"kind": "initiative", "name": "X"},
        )
    assert r.status_code == 401
