"""T-0600 (N2): POST /backlog enforces the T-0577 dedupe-vs-create gate.

The worker's ``task_new`` rejects near-duplicate mints (T-0577); the web
board's create previously minted unconditionally (feedback
F-2026-07-05-bsq-228ba88f8c), so near-dup protection was only as strong as
the least-guarded ingest surface. The API now ranks the new title(+verbatim)
against the existing backlog via the mirrored ``task_search`` and answers
409 + candidates + a ``force: true`` escape hatch — the same UX the CLI lane
gives.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app

BACKLOG = "/api/projects/test-project/backlog"
DUP_TITLE = "unique dedupe probe alpha bravo charlie"


def _client_logged_in(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


def test_near_duplicate_create_is_409_with_candidates(tmp_bot_squad, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        seed = client.post(BACKLOG, json={"title": DUP_TITLE})
        assert seed.status_code == 200
        seed_id = seed.json()["id"]

        dup = client.post(BACKLOG, json={"title": DUP_TITLE})
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["error"] == "near_duplicate"
    assert "force" in detail["message"]
    assert {"id": seed_id, "title": DUP_TITLE} in detail["candidates"]


def test_force_true_bypasses_the_gate(tmp_bot_squad, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        seed = client.post(BACKLOG, json={"title": DUP_TITLE})
        forced = client.post(BACKLOG, json={"title": DUP_TITLE, "force": True})
    assert seed.status_code == 200
    assert forced.status_code == 200
    assert forced.json()["id"] != seed.json()["id"]


def test_force_must_be_a_boolean(tmp_bot_squad, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.post(BACKLOG, json={"title": "typed force gate probe", "force": "yes"})
    assert r.status_code == 400
    assert r.json()["detail"] == "force must be a boolean"


def test_distinct_title_still_mints(tmp_bot_squad, monkeypatch):
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        a = client.post(BACKLOG, json={"title": "wire the sprocket flange resolver"})
        b = client.post(BACKLOG, json={"title": "migrate telemetry ingest cursor checkpoints"})
    assert a.status_code == 200
    assert b.status_code == 200


def test_verbatim_request_contributes_to_the_dedupe_query(tmp_bot_squad, monkeypatch):
    # Worker parity: task_new ranks title+verbatim, and candidates match on
    # title+body — so re-submitting words that live in an existing ticket's
    # verbatim body flags, where the (partly novel) title alone would not.
    # Token math at the default 0.9 threshold: title alone = 4 tokens, 3
    # covered (0.75 → mints); title+verbatim = 11 distinct tokens, 10 covered
    # (0.909 → 409).
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        seed = client.post(BACKLOG, json={
            "title": DUP_TITLE,
            "verbatim_request": "quick brown fox jumps over lazy dog fence",
        })
        title_only = client.post(BACKLOG, json={"title": "quick brown fox zebrafish"})
        with_verbatim = client.post(BACKLOG, json={
            "title": "quick brown fox zebrafish",
            "verbatim_request": "jumps over lazy dog fence alpha bravo",
        })
    assert seed.status_code == 200
    assert title_only.status_code == 200  # 0.75 coverage — below the gate
    assert with_verbatim.status_code == 409  # verbatim pushed coverage over


def test_threshold_knob_is_read_from_system_settings(tmp_bot_squad, monkeypatch):
    # Same [tasks].dedupe_threshold knob the worker config reads. Setting it
    # above 1.0 makes even an exact duplicate pass — proving the file is
    # consulted, not a hardcoded constant.
    (tmp_bot_squad / "config" / "system_settings.toml").write_text(
        "[tasks]\ndedupe_threshold = 1.1\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        seed = client.post(BACKLOG, json={"title": DUP_TITLE})
        dup = client.post(BACKLOG, json={"title": DUP_TITLE})
    assert seed.status_code == 200
    assert dup.status_code == 200  # gate disabled by the raised threshold


def test_single_token_titles_never_flag(tmp_bot_squad, monkeypatch):
    # Parity with the worker's _TASK_DEDUPE_MIN_TOKENS floor: a 1-token query
    # trivially "covers" any hit, which is noise, not a duplicate.
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        a = client.post(BACKLOG, json={"title": "zzqqxglobber"})
        b = client.post(BACKLOG, json={"title": "zzqqxglobber"})
    assert a.status_code == 200
    assert b.status_code == 200


def test_task_search_mirror_is_byte_identical():
    """api/app/task_search.py is a MIRROR of worker/bot_squad_worker/
    task_search.py (idalloc precedent: separate runtimes, no shared import —
    byte-identity is the anti-drift contract). Skips where the worker tree
    isn't mounted (the docker test recipe mounts api/ only)."""
    api_copy = Path(__file__).resolve().parents[1] / "app" / "task_search.py"
    candidates = [
        # repo checkout: <repo>/api/tests/… → <repo>/worker/…
        Path(__file__).resolve().parents[2] / "worker" / "bot_squad_worker" / "task_search.py",
        # docker test recipe mounts api/ at /app; mount worker/ at /worker to arm this
        Path("/worker/bot_squad_worker/task_search.py"),
    ]
    worker_copy = next((p for p in candidates if p.exists()), None)
    if worker_copy is None:
        pytest.skip("worker tree not available in this environment")
    assert worker_copy.read_bytes() == api_copy.read_bytes(), (
        "worker/bot_squad_worker/task_search.py and api/app/task_search.py "
        "have drifted — re-sync them (they must stay byte-identical)."
    )
