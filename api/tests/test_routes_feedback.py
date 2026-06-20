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


# ---------------------------------------------------------------------------
# T-0283 Pillar-C: feedback as nestable cross-store artifacts.
# ---------------------------------------------------------------------------
def _fb_dir(tmp_bot_squad: Path) -> Path:
    return tmp_bot_squad / "data" / "test-project" / "feedback"


def _new_doc(c, title="Mother"):
    return c.post(
        "/api/projects/test-project/docs", json={"category": "design", "title": title}
    ).json()["id"]


def test_list_feedback_exposes_id_and_parent(tmp_bot_squad: Path, monkeypatch):
    fb = _fb_dir(tmp_bot_squad)
    (fb / "F-legacy.md").write_text("# Legacy\n\nraw\n")
    (fb / "F-fm.md").write_text(
        "---\nid: F-fm\ntitle: With FM\nparent_doc_id: D-0001\n---\n\n# body\n"
    )
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        rows = c.get("/api/projects/test-project/feedback").json()
    by_id = {r["id"]: r for r in rows}
    assert by_id["F-legacy"]["parent_doc_id"] is None
    assert by_id["F-fm"]["parent_doc_id"] == "D-0001"
    # name (with .md) still present for the existing put/promote/content flows
    assert by_id["F-legacy"]["name"] == "F-legacy.md"


def test_feedback_children_cross_store(tmp_bot_squad: Path, monkeypatch):
    (_fb_dir(tmp_bot_squad) / "F-theme.md").write_text("# Theme\n\nraw\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        # a doc nested under the feedback theme
        child = c.post(
            "/api/projects/test-project/docs",
            json={"category": "design", "title": "Evidence", "parent_doc_id": "F-theme"},
        ).json()["id"]
        kids = c.get("/api/projects/test-project/feedback/F-theme.md/children").json()
    assert [(k["id"], k["kind"]) for k in kids] == [(child, "doc")]


def test_set_feedback_parent_on_legacy_adds_frontmatter(tmp_bot_squad: Path, monkeypatch):
    fb = _fb_dir(tmp_bot_squad)
    (fb / "F-x.md").write_text("# X\n\nraw feedback body\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        mother = _new_doc(c)
        r = c.put(
            "/api/projects/test-project/feedback/F-x.md/parent",
            json={"parent_doc_id": mother},
        )
        assert r.status_code == 200, r.text
        rows = c.get("/api/projects/test-project/feedback").json()
    parent = next(x["parent_doc_id"] for x in rows if x["id"] == "F-x")
    assert parent == mother
    # legacy body preserved under the new frontmatter block
    assert "raw feedback body" in (fb / "F-x.md").read_text()


def test_set_feedback_parent_clear(tmp_bot_squad: Path, monkeypatch):
    fb = _fb_dir(tmp_bot_squad)
    (fb / "F-y.md").write_text("---\nid: F-y\nparent_doc_id: D-0001\n---\n\nbody\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/F-y.md/parent",
            json={"parent_doc_id": None},
        )
        assert r.status_code == 200, r.text
        rows = c.get("/api/projects/test-project/feedback").json()
    assert next(x["parent_doc_id"] for x in rows if x["id"] == "F-y") is None


def test_set_feedback_parent_unknown_404(tmp_bot_squad: Path, monkeypatch):
    (_fb_dir(tmp_bot_squad) / "F-z.md").write_text("# Z\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put(
            "/api/projects/test-project/feedback/F-z.md/parent",
            json={"parent_doc_id": "D-9999"},
        )
    assert r.status_code == 404, r.text


def test_set_feedback_parent_cycle_rejected(tmp_bot_squad: Path, monkeypatch):
    fb = _fb_dir(tmp_bot_squad)
    # F-a is parent of D via... build F-a -> (child doc D) then try D as F-a's parent.
    (fb / "F-a.md").write_text("# A\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        d = c.post(
            "/api/projects/test-project/docs",
            json={"category": "design", "title": "D", "parent_doc_id": "F-a"},
        ).json()["id"]
        # F-a's parent = d would close F-a -> d -> F-a
        r = c.put(
            f"/api/projects/test-project/feedback/F-a.md/parent",
            json={"parent_doc_id": d},
        )
    assert r.status_code == 400, r.text
    assert "cycle" in r.text.lower()


def test_feedback_id_is_full_stem_and_roundtrips(tmp_bot_squad: Path, monkeypatch):
    """T-0283 regression (WS-2 repro): a dated/sequenced feedback filename's
    artifact id is the FULL stem (not an F-NNNN prefix), and that one id form
    round-trips through list -> parent_doc_id ref -> children/parent endpoints
    (with or without a trailing .md)."""
    fb = _fb_dir(tmp_bot_squad)
    (fb / "F-0001-2026-05-18-shared-tree.md").write_text("# Theme\n\nraw\n")
    full = "F-0001-2026-05-18-shared-tree"
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        rows = c.get("/api/projects/test-project/feedback").json()
        assert rows[0]["id"] == full  # full stem, not "F-0001"
        # a doc nests under the feedback theme using list_feedback's id field
        child = c.post(
            "/api/projects/test-project/docs",
            json={"category": "design", "title": "Evidence", "parent_doc_id": full},
        ).json()["id"]
        # children endpoint resolves the SAME id (no .md) — WS-2's 400 repro
        kids = c.get(f"/api/projects/test-project/feedback/{full}/children").json()
        assert [(k["id"], k["kind"]) for k in kids] == [(child, "doc")]
        # and the .md form still works (back-compat with the name form)
        kids2 = c.get(f"/api/projects/test-project/feedback/{full}.md/children").json()
        assert {k["id"] for k in kids2} == {child}
        # PUT parent by the bare id
        mother = _new_doc(c, title="Mother")
        r = c.put(
            f"/api/projects/test-project/feedback/{full}/parent",
            json={"parent_doc_id": mother},
        )
        assert r.status_code == 200, r.text
        rows2 = c.get("/api/projects/test-project/feedback").json()
    assert next(x["parent_doc_id"] for x in rows2 if x["id"] == full) == mother
