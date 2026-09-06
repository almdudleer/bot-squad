"""T-0929 — the project's drive state + the ultimate off-switch, over HTTP.

> "And there should be explicit UI state where I could easily turn them off for
> a project, just stop any automatic activity all at once. And explicitly see if
> it's happening in the first place." — the stakeholder, 2026-08-28.

Both endpoints are PROXIES to the worker. That is the design and it is what
these tests pin: the api owns NO copy of the mechanism registry (a second
hand-maintained list of automatic subsystems would drift and then lie about what
STOP stopped — this ticket's own root cause with a new face), so with no worker
the read must 502 rather than invent a "nothing is running".
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import build_app

_P = "/api/projects/test-project"

_SNAP = {
    "ok": True,
    "state": "all_tasks",
    "label": "all tasks — drive the backlog end to end",
    "running": True,
    "paused": False,
    "max_in_progress": 0,
    "drive": {"scope": "all", "stop_when": "scope_exhausted", "on_stop": "nothing",
              "set_by": None, "set_at": None, "source_text": None,
              "configured": False, "invalid": {}, "state": "all_tasks"},
    "quota": {"max_in_progress": 0, "weekly_target_pct": None,
              "spend_pct": None, "verdict": None},
    "autopilots": [],
    "mechanisms": [
        {"key": "operator_redrive", "why": "respawns the operator",
         "gated": True, "active": True},
        {"key": "telemetry", "why": "samples usage", "gated": False, "active": True},
    ],
}


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    c = TestClient(build_app())
    r = c.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text
    return c


def test_get_returns_the_workers_snapshot_verbatim(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client, \
            patch("app.worker_client.WorkerClient.call_action",
                  new=AsyncMock(return_value=_SNAP)) as call:
        r = client.get(f"{_P}/automation")
    assert r.status_code == 200, r.text
    assert r.json() == _SNAP
    assert call.await_args.args[0] == "automation_status"
    assert call.await_args.args[1] == {"slug": "test-project"}


def test_get_502s_rather_than_inventing_a_state_with_no_worker(
        tmp_bot_squad: Path, monkeypatch):
    """No worker means we do not KNOW whether anything is running. A confident
    "nothing is" would be the class of false statement this ticket removes."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get(f"{_P}/automation")
    assert r.status_code == 502, r.text


def test_setting_a_state_forwards_it_and_returns_the_RESULT(
        tmp_bot_squad: Path, monkeypatch):
    stopped = {**_SNAP, "state": "off", "running": False, "paused": True,
               "label": "off — nothing automatic runs"}
    with _client(tmp_bot_squad, monkeypatch) as client, \
            patch("app.worker_client.WorkerClient.call_action",
                  new=AsyncMock(return_value=stopped)) as call:
        r = client.post(f"{_P}/automation/state",
                        json={"state": "off", "source_text": "stop everything"})
    assert r.status_code == 200, r.text
    # the RESULTING state, not an echo of the request — a surface that claims a
    # state it does not have is the reported defect
    assert r.json()["running"] is False
    assert call.await_args.args[0] == "automation_set_state"
    params = call.await_args.args[1]
    assert params["slug"] == "test-project"
    assert params["state"] == "off"
    assert params["source_text"] == "stop everything"
    assert params["requested_by"]


def test_dashes_are_accepted_for_underscores(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client, \
            patch("app.worker_client.WorkerClient.call_action",
                  new=AsyncMock(return_value=_SNAP)) as call:
        r = client.post(f"{_P}/automation/state", json={"state": "finish-up"})
    assert r.status_code == 200, r.text
    assert call.await_args.args[1]["state"] == "finish_up"


def test_an_out_of_set_state_is_400_naming_the_closed_set_never_a_fallback(
        tmp_bot_squad: Path, monkeypatch):
    """A settings surface that quietly ignores what you set it to is the defect
    D-0069 named and this ticket re-reports. `custom` is REPORTED, never set."""
    with _client(tmp_bot_squad, monkeypatch) as client, \
            patch("app.worker_client.WorkerClient.call_action",
                  new=AsyncMock(return_value=_SNAP)) as call:
        for bad in ("turbo", "custom", "", "ALL_TASKS"):
            r = client.post(f"{_P}/automation/state", json={"state": bad})
            assert r.status_code == 400, (bad, r.text)
            assert "all_tasks" in r.json()["detail"]
    call.assert_not_awaited()


def test_unknown_project_is_404_on_both_verbs(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        assert client.get("/api/projects/nope/automation").status_code == 404
        assert client.post("/api/projects/nope/automation/state",
                           json={"state": "off"}).status_code == 404
