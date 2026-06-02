"""Tests for T-0159 use-cases API (app.routes_usecases).

list / get / put (create + edit). The run endpoint is exercised in the manual
walkthrough (data/bot-squad/scenarios/T-0159-use-cases.md) since it spawns a
real session; here we cover the storage CRUD that the UI tab drives.
"""
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


_UC = """\
---
id: UC-demo
title: Demo flow
user_persona: Operator
goal: Do the thing
preconditions: logged in
success_criteria: thing is done
related_tickets: T-0159
status: active
---

# Demo flow

## Steps
1. Open the page
2. Click the button

## Feedback
- [2026-06-02 · S-x] button was hidden
"""


def test_empty_list(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.get("/api/projects/test-project/use_cases")
    assert r.status_code == 200
    assert r.json() == []


def test_create_get_list(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put("/api/projects/test-project/use_cases/UC-demo",
                  json={"content": _UC})
        assert r.status_code == 200 and r.json()["ok"] is True

        r = c.get("/api/projects/test-project/use_cases")
        assert r.status_code == 200
        lst = r.json()
        assert len(lst) == 1
        assert lst[0]["id"] == "UC-demo"
        assert lst[0]["title"] == "Demo flow"
        assert lst[0]["status"] == "active"

        r = c.get("/api/projects/test-project/use_cases/UC-demo")
        assert r.status_code == 200
        uc = r.json()
        assert uc["goal"] == "Do the thing"
        assert uc["related_tickets"] == "T-0159"
        assert "Click the button" in uc["body"]


def test_get_missing_404(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.get("/api/projects/test-project/use_cases/UC-nope")
    assert r.status_code == 404


def test_bad_id_rejected(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.get("/api/projects/test-project/use_cases/..%2Fetc")
    assert r.status_code in (400, 404)


def test_empty_content_rejected(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.put("/api/projects/test-project/use_cases/UC-demo",
                  json={"content": "   "})
    assert r.status_code == 400


def test_edit_replaces(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        c.put("/api/projects/test-project/use_cases/UC-demo", json={"content": _UC})
        r = c.put("/api/projects/test-project/use_cases/UC-demo",
                  json={"content": _UC.replace("Do the thing", "Do the new thing")})
        assert r.status_code == 200
        r = c.get("/api/projects/test-project/use_cases/UC-demo")
        assert r.json()["goal"] == "Do the new thing"
