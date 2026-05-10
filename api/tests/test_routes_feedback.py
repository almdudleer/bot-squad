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

def test_feedback_lists(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id520.md").write_text("# Feedback\n\nText\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.get("/api/projects/test-project/feedback")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "F-2026-04-15-id520.md"


# ---------------------------------------------------------------------------
# PUT /api/projects/{slug}/feedback/{name}
# ---------------------------------------------------------------------------

def test_put_feedback_replaces_content(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id520.md").write_text("# Old\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/F-2026-04-15-id520.md",
            json={"content": "# Updated\n\nNew content.\n"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert (fb / "F-2026-04-15-id520.md").read_text() == "# Updated\n\nNew content.\n"


def test_put_feedback_not_found(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/F-nonexistent.md",
            json={"content": "# x\n"},
        )
    assert r.status_code == 404


def test_put_feedback_empty_content_rejected(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id520.md").write_text("# Old\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/F-2026-04-15-id520.md",
            json={"content": ""},
        )
    assert r.status_code == 400


def test_put_feedback_too_large_rejected(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id520.md").write_text("# Old\n")
    big = "x" * (200 * 1024 + 1)
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/F-2026-04-15-id520.md",
            json={"content": big},
        )
    assert r.status_code == 400


def test_put_feedback_traversal_rejected(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/not-valid-name.md",
            json={"content": "# x\n"},
        )
    assert r.status_code == 400


def test_put_feedback_requires_auth(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id520.md").write_text("# Old\n")
    with _anon(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/F-2026-04-15-id520.md",
            json={"content": "# x\n"},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/feedback/{name}/promote
# ---------------------------------------------------------------------------

def test_promote_default_title_from_h1(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id520.md").write_text(
        "# User wants dark mode\n\nThis would be really helpful.\n"
    )
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)

    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/feedback/F-2026-04-15-id520.md/promote",
            json={},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "task_id" in data

    # Task file should exist and title matches H1
    task_id = data["task_id"]
    files = list(backlog.glob(f"{task_id}-*.md"))
    assert len(files) == 1
    from app.markdown_parser import parse_task
    task = parse_task(files[0])
    assert "dark mode" in task["title"].lower()
    assert "from" in task
    assert task["from"] == "F-2026-04-15-id520.md"


def test_promote_explicit_title(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id521.md").write_text(
        "# Some H1\n\nSome feedback here.\n"
    )
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)

    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/feedback/F-2026-04-15-id521.md/promote",
            json={"title": "Custom task title"},
        )
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    files = list(backlog.glob(f"{task_id}-*.md"))
    from app.markdown_parser import parse_task
    task = parse_task(files[0])
    assert task["title"] == "Custom task title"


def test_promote_appends_footer_to_feedback(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id522.md").write_text(
        "# Feedback title\n\nContent here.\n"
    )
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)

    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/feedback/F-2026-04-15-id522.md/promote",
            json={},
        )
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    fb_content = (fb / "F-2026-04-15-id522.md").read_text()
    assert "Promoted to backlog task" in fb_content
    assert task_id in fb_content


def test_promote_multiple_times(tmp_bot_squad: Path, monkeypatch):
    """Each promote creates a new task and appends another footer line."""
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id523.md").write_text(
        "# Repeat promote\n\nFeedback.\n"
    )
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)

    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r1 = c.post(
            "/api/projects/test-project/feedback/F-2026-04-15-id523.md/promote",
            json={},
        )
        r2 = c.post(
            "/api/projects/test-project/feedback/F-2026-04-15-id523.md/promote",
            json={},
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["task_id"] != r2.json()["task_id"]
    fb_content = (fb / "F-2026-04-15-id523.md").read_text()
    assert fb_content.count("Promoted to backlog task") == 2


def test_promote_no_h1_uses_filename(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id524.md").write_text("Some feedback without a heading.\n")
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)

    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/feedback/F-2026-04-15-id524.md/promote",
            json={},
        )
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    files = list(backlog.glob(f"{task_id}-*.md"))
    from app.markdown_parser import parse_task
    task = parse_task(files[0])
    # Title should be filename without extension
    assert "F-2026-04-15-id524" in task["title"]


def test_promote_not_found(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/feedback/F-nonexistent.md/promote",
            json={},
        )
    assert r.status_code == 404


def test_promote_requires_auth(tmp_bot_squad: Path, monkeypatch):
    fb = tmp_bot_squad / "data" / "test-project" / "feedback"
    (fb / "F-2026-04-15-id520.md").write_text("# Feedback\n\nText\n")
    with _anon(tmp_bot_squad, monkeypatch) as c:
        r = c.post(
            "/api/projects/test-project/feedback/F-2026-04-15-id520.md/promote",
            json={},
        )
    assert r.status_code == 401
