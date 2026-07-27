"""Tests for worker FastAPI app on Unix socket."""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot_squad_worker.server import build_app


def test_health_endpoint():
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body
    assert "uptime" in body


def test_health_reports_frozen_boot_git_sha():
    """T-0335 item-13 (Fork-5): /health carries the worker's frozen-at-boot
    git_sha so a deploy can detect running != deployed worker code."""
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/health")
    body = r.json()
    assert "git_sha" in body
    assert isinstance(body["git_sha"], str)


def test_actions_noop():
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/actions/noop", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_actions_unknown_returns_400():
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/actions/wipe-disk", json={})
    assert r.status_code == 400
    assert "unknown action" in r.json()["detail"]


def test_actions_bad_params_returns_400():
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/actions/noop", json={"oops": 1})
    assert r.status_code == 400


def test_health_surfaces_a_pending_restart(tmp_path, monkeypatch) -> None:
    """T-0739: the worker's own /health makes the same restart_pending vs
    sha_drift discrimination /api/health does.

    An operator debugging a drift reaches for whichever surface is closest;
    having only one of the two carry the answer is how the T-0717 confusion got
    re-derived from scratch. Absent when nothing is owed (no green noise)."""
    import json
    import time
    from types import SimpleNamespace

    from bot_squad_worker import actions as A

    wdir = tmp_path / "data" / "_worker"
    wdir.mkdir(parents=True)
    cfg = SimpleNamespace(data_dir=tmp_path / "data")
    monkeypatch.setattr(A, "_CONFIG", cfg)

    with TestClient(build_app()) as client:
        assert "restart_pending" not in client.get("/health").json()

        (wdir / "restart_inflight.json").write_text(
            json.dumps({"at": time.time(), "expected_by": time.time() + 180,
                        "queue_id": "q1"})
        )
        body = client.get("/health").json()

    assert body["restart_pending"]["state"] == "in_flight"
    assert body["restart_pending"]["overdue"] is False


def test_health_never_fails_on_a_broken_restart_probe(monkeypatch) -> None:
    """/health is what the deploy smoke asserts against — a probe error here
    must degrade to 'no pending restart', never to a 500 that reads as a failed
    restart and marks the deploy broken."""
    from bot_squad_worker import actions as A

    monkeypatch.setattr(A, "_CONFIG", None)  # raises ActionError in _get_config
    with TestClient(build_app()) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert "restart_pending" not in r.json()
