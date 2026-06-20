"""Docs API tests — flat docs + nested/grouped docs (T-0234).

Mirrors the FastAPI TestClient + tmp_bot_squad style of test_routes_projects.py.
The nested-docs model (T-0234) lets a "mother" doc carry CHILD artifacts via a
``parent_doc_id`` frontmatter field. These tests pin the API contract that
Team 2's docs UI (T-0235) consumes:

  - POST   /api/projects/{slug}/docs           (NewDoc.parent_doc_id optional)
  - GET    /api/projects/{slug}/docs/{id}      (payload carries parent_doc_id +
                                                child_doc_ids)
  - GET    /api/projects/{slug}/docs/{id}/children
  - PUT    /api/projects/{slug}/docs/{id}/parent  (adopt / disown)
  - GET    /api/projects/{slug}/docs           (each item carries parent_doc_id)
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


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


def _create(client, *, category="product", title="Doc", parent_doc_id=None):
    body = {"category": category, "title": title}
    if parent_doc_id is not None:
        body["parent_doc_id"] = parent_doc_id
    r = client.post("/api/projects/test-project/docs", json=body)
    return r


# ---------------------------------------------------------------------------
# back-compat: a flat doc with no parent keeps working
# ---------------------------------------------------------------------------
def test_flat_doc_round_trip_no_parent(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = _create(client, title="Flat")
        assert r.status_code == 200, r.text
        doc_id = r.json()["id"]

        # listing carries parent_doc_id = None for a root doc
        listing = client.get("/api/projects/test-project/docs").json()
        item = next(d for d in listing if d["id"] == doc_id)
        assert item["parent_doc_id"] is None

        # get serves it, no parent, no children
        got = client.get(f"/api/projects/test-project/docs/{doc_id}").json()
        assert got["parent_doc_id"] is None
        assert got["child_doc_ids"] == []


# ---------------------------------------------------------------------------
# create child with parent_doc_id persists + round-trips
# ---------------------------------------------------------------------------
def test_create_child_with_parent_persists(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        mother = _create(client, title="Mother").json()["id"]
        child_r = _create(client, title="Insight", parent_doc_id=mother)
        assert child_r.status_code == 200, child_r.text
        child = child_r.json()["id"]
        assert child_r.json()["parent_doc_id"] == mother

        got = client.get(f"/api/projects/test-project/docs/{child}").json()
        assert got["parent_doc_id"] == mother

        # and the frontmatter actually persisted on disk
        docs_root = tmp_bot_squad / "data" / "test-project" / "docs"
        files = list(docs_root.rglob(f"{child}-*.md"))
        assert len(files) == 1
        assert f"parent_doc_id: {mother}" in files[0].read_text()


# ---------------------------------------------------------------------------
# fetch a mother's children
# ---------------------------------------------------------------------------
def test_mother_children_listing(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        mother = _create(client, title="Mother").json()["id"]
        c1 = _create(client, title="Insight", parent_doc_id=mother).json()["id"]
        c2 = _create(client, category="design", title="Diagram",
                     parent_doc_id=mother).json()["id"]
        # an unrelated root doc must NOT show up as a child
        _create(client, title="Unrelated")

        # GET /{id}/children
        kids = client.get(f"/api/projects/test-project/docs/{mother}/children").json()
        kid_ids = {k["id"] for k in kids}
        assert kid_ids == {c1, c2}
        assert all(k["parent_doc_id"] == mother for k in kids)

        # child_doc_ids on the mother's payload mirrors it
        got = client.get(f"/api/projects/test-project/docs/{mother}").json()
        assert set(got["child_doc_ids"]) == {c1, c2}


# ---------------------------------------------------------------------------
# adopt / disown re-parents an existing doc
# ---------------------------------------------------------------------------
def test_adopt_and_disown_reparent(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        mother = _create(client, title="Mother").json()["id"]
        orphan = _create(client, title="Was Flat").json()["id"]

        # adopt: set parent on an existing root doc
        r = client.put(
            f"/api/projects/test-project/docs/{orphan}/parent",
            json={"parent_doc_id": mother},
        )
        assert r.status_code == 200, r.text
        assert r.json()["parent_doc_id"] == mother
        kids = client.get(f"/api/projects/test-project/docs/{mother}/children").json()
        assert {k["id"] for k in kids} == {orphan}

        # disown: clear the parent (null) → back to root
        r = client.put(
            f"/api/projects/test-project/docs/{orphan}/parent",
            json={"parent_doc_id": None},
        )
        assert r.status_code == 200, r.text
        assert r.json()["parent_doc_id"] is None
        got = client.get(f"/api/projects/test-project/docs/{orphan}").json()
        assert got["parent_doc_id"] is None
        kids = client.get(f"/api/projects/test-project/docs/{mother}/children").json()
        assert kids == []


# ---------------------------------------------------------------------------
# self-parent / cycle rejected
# ---------------------------------------------------------------------------
def test_self_parent_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc = _create(client, title="Solo").json()["id"]
        r = client.put(
            f"/api/projects/test-project/docs/{doc}/parent",
            json={"parent_doc_id": doc},
        )
        assert r.status_code == 400, r.text
        assert "self" in r.json()["detail"].lower() or "cycle" in r.json()["detail"].lower()


def test_cycle_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        a = _create(client, title="A").json()["id"]
        b = _create(client, title="B", parent_doc_id=a).json()["id"]
        # B is already a child of A; making A a child of B would close a cycle.
        r = client.put(
            f"/api/projects/test-project/docs/{a}/parent",
            json={"parent_doc_id": b},
        )
        assert r.status_code == 400, r.text
        assert "cycle" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# create with a non-existent parent is rejected (explicit relationship)
# ---------------------------------------------------------------------------
def test_create_with_missing_parent_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = _create(client, title="Child", parent_doc_id="D-9999")
        assert r.status_code == 404, r.text


def test_adopt_missing_parent_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc = _create(client, title="X").json()["id"]
        r = client.put(
            f"/api/projects/test-project/docs/{doc}/parent",
            json={"parent_doc_id": "D-9999"},
        )
        assert r.status_code == 404, r.text


def test_children_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/docs/D-0001/children")
    assert r.status_code == 401


def test_set_parent_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.put(
            "/api/projects/test-project/docs/D-0001/parent",
            json={"parent_doc_id": None},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# DELETE doc (T-0276): remove file, clean up ticket backlinks, reject a
# mother doc with children, tombstone the id (no reclaim).
# ---------------------------------------------------------------------------
def test_delete_doc_removes_file(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc_id = _create(client, title="Throwaway").json()["id"]
        r = client.delete(f"/api/projects/test-project/docs/{doc_id}")
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True
        # gone from get + listing
        assert client.get(f"/api/projects/test-project/docs/{doc_id}").status_code == 404
        listing = client.get("/api/projects/test-project/docs").json()
        assert all(d["id"] != doc_id for d in listing)


def test_delete_doc_not_found_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.delete("/api/projects/test-project/docs/D-9999")
        assert r.status_code == 404, r.text


def test_delete_doc_with_children_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        mother = _create(client, title="Mother").json()["id"]
        child = _create(client, title="Child", parent_doc_id=mother).json()["id"]
        r = client.delete(f"/api/projects/test-project/docs/{mother}")
        assert r.status_code == 409, r.text
        assert child in r.text  # the blocking child is named
        # mother survives the rejected delete
        assert client.get(f"/api/projects/test-project/docs/{mother}").status_code == 200


def test_delete_doc_cleans_up_ticket_backlink(tmp_bot_squad: Path, monkeypatch):
    # Seed a ticket the doc can link to.
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / "T-0001-sample.md").write_text(
        "---\nid: T-0001\ntitle: Sample\nstatus: open\nrelated_docs: []\n---\n\nbody\n",
        encoding="utf-8",
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc_id = _create(client, title="Linked").json()["id"]
        assert client.post(
            f"/api/projects/test-project/docs/{doc_id}/link",
            json={"ticket": "T-0001"},
        ).status_code == 200
        # delete the doc → its backlink is scrubbed from the ticket's related_docs
        assert client.delete(
            f"/api/projects/test-project/docs/{doc_id}"
        ).status_code == 200
    text = (backlog / "T-0001-sample.md").read_text(encoding="utf-8")
    assert doc_id not in text


def test_delete_doc_tombstones_id_no_reclaim(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        first = _create(client, title="First").json()["id"]
        assert client.delete(
            f"/api/projects/test-project/docs/{first}"
        ).status_code == 200
        # the allocator is monotonic — the next id is NOT the reclaimed one
        second = _create(client, title="Second").json()["id"]
        assert second != first
        assert int(second.split("-")[1]) > int(first.split("-")[1])


# ---------------------------------------------------------------------------
# T-0283 Pillar-C: docs participate in the cross-store artifact tree.
# ---------------------------------------------------------------------------
def test_doc_get_exposes_kind_and_artifact_alias(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        mother = _create(client, title="Mother").json()["id"]
        child = _create(client, title="Child", parent_doc_id=mother).json()["id"]
        got = client.get(f"/api/projects/test-project/docs/{mother}").json()
    assert got["kind"] == "doc"
    # additive alias: both keys present, child_artifact_ids is a superset
    assert set(got["child_doc_ids"]) == {child}
    assert set(got["child_artifact_ids"]) == {child}


def test_doc_children_include_cross_store(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        mother = _create(client, title="Theme").json()["id"]
        doc_child = _create(client, title="DocChild", parent_doc_id=mother).json()["id"]
        # a use-case nested under the doc mother (cross-store)
        uc_child = client.post(
            "/api/projects/test-project/use_cases",
            json={"title": "UC child", "parent_doc_id": mother},
        ).json()["id"]
        kids = client.get(f"/api/projects/test-project/docs/{mother}/children").json()
        by_id = {k["id"]: k for k in kids}
        # mother GET: child_doc_ids is docs-only; child_artifact_ids is the superset
        got = client.get(f"/api/projects/test-project/docs/{mother}").json()
    assert by_id[doc_child]["kind"] == "doc"
    assert by_id[uc_child]["kind"] == "use_case"
    assert set(got["child_doc_ids"]) == {doc_child}
    assert set(got["child_artifact_ids"]) == {doc_child, uc_child}


def test_doc_reparent_under_use_case_cross_store(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        uc = client.post(
            "/api/projects/test-project/use_cases", json={"title": "Mother UC"}
        ).json()["id"]
        doc = _create(client, title="Insight").json()["id"]
        r = client.put(
            f"/api/projects/test-project/docs/{doc}/parent",
            json={"parent_doc_id": uc},
        )
        assert r.status_code == 200, r.text
        assert r.json()["parent_doc_id"] == uc
        # the UC's cross-store children include this doc
        kids = client.get(f"/api/projects/test-project/use_cases/{uc}/children").json()
    assert {k["id"] for k in kids} == {doc}


def test_doc_reparent_cross_store_cycle_rejected(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc = _create(client, title="D").json()["id"]
        # a UC nested under the doc
        uc = client.post(
            "/api/projects/test-project/use_cases",
            json={"title": "U", "parent_doc_id": doc},
        ).json()["id"]
        # making the doc's parent the UC would close doc -> uc -> doc
        r = client.put(
            f"/api/projects/test-project/docs/{doc}/parent",
            json={"parent_doc_id": uc},
        )
    assert r.status_code == 400, r.text
    assert "cycle" in r.text.lower()
