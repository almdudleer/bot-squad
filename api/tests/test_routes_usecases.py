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


def test_create_allocates_uc_id(tmp_bot_squad: Path, monkeypatch):
    """T-0174: POST allocates a UC-NNNN id atomically — no hand-typed id."""
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post("/api/projects/test-project/use_cases", json={"title": "First flow"})
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "UC-0001"
        # Counter advanced; second create gets the next id.
        r2 = c.post("/api/projects/test-project/use_cases", json={"title": "Second flow"})
        assert r2.json()["id"] == "UC-0002"
        # The stub is listable and stem-keyed (filename == id).
        lst = c.get("/api/projects/test-project/use_cases").json()
        ids = {u["id"] for u in lst}
        assert {"UC-0001", "UC-0002"} <= ids
        counter = tmp_bot_squad / "data" / "test-project" / "_counters" / "uc.txt"
        assert counter.read_text().strip() == "2"


def test_create_ignores_legacy_slug_ucs(tmp_bot_squad: Path, monkeypatch):
    """Legacy slug-named UCs are non-numeric and must not bump the counter."""
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        c.put("/api/projects/test-project/use_cases/UC-demo", json={"content": _UC})
        r = c.post("/api/projects/test-project/use_cases", json={"title": "Numeric one"})
        assert r.json()["id"] == "UC-0001"


def test_create_empty_title_rejected(tmp_bot_squad: Path, monkeypatch):
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.post("/api/projects/test-project/use_cases", json={"title": "  "})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# DELETE use case (T-0276): remove the file and cascade its owned flows dir.
# ---------------------------------------------------------------------------
def test_delete_use_case_removes_file(tmp_bot_squad: Path, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    uc_id = c.post(
        "/api/projects/test-project/use_cases", json={"title": "Throwaway"}
    ).json()["id"]
    r = c.delete(f"/api/projects/test-project/use_cases/{uc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert c.get(f"/api/projects/test-project/use_cases/{uc_id}").status_code == 404


def test_delete_use_case_not_found_404(tmp_bot_squad: Path, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    r = c.delete("/api/projects/test-project/use_cases/UC-9999")
    assert r.status_code == 404, r.text


def test_delete_use_case_cascades_flows(tmp_bot_squad: Path, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    uc_id = c.post(
        "/api/projects/test-project/use_cases", json={"title": "WithFlows"}
    ).json()["id"]
    # A use case owns its flows (stored under <uc_id>/flows/). Seed one on disk.
    flows_dir = tmp_bot_squad / "data" / "test-project" / "use_cases" / uc_id / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    (flows_dir / "UF-0001-x.md").write_text(
        "---\nid: UF-0001\n---\n\nflow\n", encoding="utf-8"
    )
    assert c.delete(
        f"/api/projects/test-project/use_cases/{uc_id}"
    ).status_code == 200
    # both the file and the owned flows subtree are gone
    uc_root = tmp_bot_squad / "data" / "test-project" / "use_cases"
    assert not (uc_root / f"{uc_id}.md").exists()
    assert not (uc_root / uc_id).exists()


def test_delete_use_case_with_cross_store_child_rejected(tmp_bot_squad: Path, monkeypatch):
    """Item 5: a use-case with a CROSS-STORE child (a doc parented under it)
    must not be deletable — would silently orphan the child. The UC delete had
    NO child guard at all (only cascaded its owned flows)."""
    c = _logged_in(tmp_bot_squad, monkeypatch)
    uc_id = c.post(
        "/api/projects/test-project/use_cases", json={"title": "Mother UC"}
    ).json()["id"]
    doc = c.post(
        "/api/projects/test-project/docs",
        json={"title": "Doc child", "category": "architecture", "parent_doc_id": uc_id},
    )
    assert doc.status_code == 200, doc.text
    doc_id = doc.json()["id"]
    r = c.delete(f"/api/projects/test-project/use_cases/{uc_id}")
    assert r.status_code == 409, r.text
    assert doc_id in r.text
    # mother survives the rejected delete
    assert c.get(f"/api/projects/test-project/use_cases/{uc_id}").status_code == 200


# ---------------------------------------------------------------------------
# T-0283 Pillar-C: use-cases as nestable cross-store artifacts.
# ---------------------------------------------------------------------------
def _new_doc(c, title="Mother", category="design", parent=None):
    body = {"category": category, "title": title}
    if parent is not None:
        body["parent_doc_id"] = parent
    return c.post("/api/projects/test-project/docs", json=body).json()["id"]


def _new_uc(c, title="UC", parent=None):
    body = {"title": title}
    if parent is not None:
        body["parent_doc_id"] = parent
    return c.post("/api/projects/test-project/use_cases", json=body).json()["id"]


def test_get_use_case_exposes_kind_and_parent(tmp_bot_squad, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    uc = _new_uc(c)
    got = c.get(f"/api/projects/test-project/use_cases/{uc}").json()
    assert got["kind"] == "use_case"
    assert got["parent_doc_id"] is None
    assert got["child_artifact_ids"] == []


def test_create_use_case_with_parent_persists(tmp_bot_squad, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    mother = _new_doc(c, title="Theme")
    uc = _new_uc(c, parent=mother)
    got = c.get(f"/api/projects/test-project/use_cases/{uc}").json()
    assert got["parent_doc_id"] == mother


def test_create_use_case_unknown_parent_404(tmp_bot_squad, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    r = c.post(
        "/api/projects/test-project/use_cases",
        json={"title": "Orphan", "parent_doc_id": "D-9999"},
    )
    assert r.status_code == 404, r.text


def test_use_case_children_cross_store(tmp_bot_squad, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    uc = _new_uc(c, title="Mother UC")
    child_doc = _new_doc(c, title="Insight", parent=uc)
    child_uc = _new_uc(c, title="Sub UC", parent=uc)
    kids = c.get(f"/api/projects/test-project/use_cases/{uc}/children").json()
    got = {(k["id"], k["kind"]) for k in kids}
    assert got == {(child_doc, "doc"), (child_uc, "use_case")}
    # mother's own GET surfaces the child ids too
    parent_view = c.get(f"/api/projects/test-project/use_cases/{uc}").json()
    assert set(parent_view["child_artifact_ids"]) == {child_doc, child_uc}


def test_set_use_case_parent_then_clear(tmp_bot_squad, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    mother = _new_doc(c, title="M")
    uc = _new_uc(c)
    r = c.put(
        f"/api/projects/test-project/use_cases/{uc}/parent",
        json={"parent_doc_id": mother},
    )
    assert r.status_code == 200, r.text
    assert c.get(f"/api/projects/test-project/use_cases/{uc}").json()["parent_doc_id"] == mother
    # clear
    c.put(f"/api/projects/test-project/use_cases/{uc}/parent", json={"parent_doc_id": None})
    assert c.get(f"/api/projects/test-project/use_cases/{uc}").json()["parent_doc_id"] is None


def test_set_use_case_parent_cycle_rejected(tmp_bot_squad, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    a = _new_uc(c, title="A")
    b = _new_uc(c, title="B", parent=a)  # B under A
    # Making A's parent B would close A->B->A.
    r = c.put(
        f"/api/projects/test-project/use_cases/{a}/parent",
        json={"parent_doc_id": b},
    )
    assert r.status_code == 400, r.text
    assert "cycle" in r.text.lower()


def test_set_use_case_parent_unknown_404(tmp_bot_squad, monkeypatch):
    c = _logged_in(tmp_bot_squad, monkeypatch)
    uc = _new_uc(c)
    r = c.put(
        f"/api/projects/test-project/use_cases/{uc}/parent",
        json={"parent_doc_id": "UC-9999"},
    )
    assert r.status_code == 404, r.text
