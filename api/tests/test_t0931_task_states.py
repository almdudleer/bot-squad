"""T-0931: task state machine — blocked_on_user, transition graph, predicates.

Three things pinned here:

1. ``api/app/task_states.py`` is a byte-identical mirror of
   ``worker/bot_squad_worker/task_states.py`` (the api can't import the
   worker package — same anti-drift contract as ``registry.py``/
   ``idalloc.py``, see ``test_registry_mirror_is_byte_identical``).
2. ``TICKET_STATUSES`` there matches ``routes_backlog._VALID_STATUSES``
   (the T-0889 lockstep pattern, extended to this new mirror).
3. The pure transition/predicate functions behave as designed, AND the live
   PATCH endpoint actually enforces the graph (not just the pure function in
   isolation — a correct reading of a function is not a finding until you
   prove it executes).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.routes_backlog import _VALID_STATUSES
from app.task_states import (
    ACTIVE_STATES,
    PARKED_STATES,
    TICKET_STATUSES,
    TRANSITIONS,
    invalid_transition_detail,
    is_parked,
    is_valid_transition,
)


def _client_logged_in(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


# ---------------------------------------------------------------------------
# Mirror + SSOT lockstep
# ---------------------------------------------------------------------------

def test_task_states_mirror_is_byte_identical() -> None:
    api_copy = Path(__file__).resolve().parents[1] / "app" / "task_states.py"
    candidates = [
        Path(__file__).resolve().parents[3] / "worker" / "bot_squad_worker" / "task_states.py",
        Path("/worker/bot_squad_worker/task_states.py"),
    ]
    worker_copy = next((p for p in candidates if p.exists()), None)
    if worker_copy is None:
        pytest.skip("worker tree not available in this environment")
    assert worker_copy.read_bytes() == api_copy.read_bytes(), (
        "worker/bot_squad_worker/task_states.py and api/app/task_states.py "
        "have drifted — re-sync them (they must stay byte-identical)."
    )


def test_ticket_statuses_matches_the_valid_statuses_ssot():
    assert set(TICKET_STATUSES) == set(_VALID_STATUSES)


def test_blocked_on_user_is_in_the_ssot():
    # The whole point of the ticket: this status must actually exist.
    assert "blocked_on_user" in _VALID_STATUSES


def test_to_accept_is_in_the_ssot():
    # T-0944: the whole point of that ticket — this status must actually exist.
    assert "to_accept" in _VALID_STATUSES


def test_every_status_has_an_entry_in_transitions_or_is_terminal_only():
    # Every status must be reachable from SOME edge, and every status that
    # appears as a source key's target must itself be a real status.
    for src, targets in TRANSITIONS.items():
        assert src in TICKET_STATUSES
        for t in targets:
            assert t in TICKET_STATUSES, f"{src} -> {t}: {t!r} is not a real status"


# ---------------------------------------------------------------------------
# Transition graph — pure function
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("frm,to", [
    ("planned", "open"),
    ("planned", "in_progress"),  # T-0591 F3.3 names this pair explicitly
    ("open", "in_progress"),
    ("open", "planned"),
    ("in_progress", "blocked_on_user"),
    ("blocked_on_user", "in_progress"),
    ("in_progress", "to_accept"),  # T-0944: dev delivers here, not to totest
    ("to_accept", "totest"),       # operator accepted -> human's queue
    ("to_accept", "reopened"),     # operator bounces
    ("to_accept", "closed"),
    ("totest", "closed"),
    ("totest", "reopened"),
    ("reopened", "in_progress"),
    ("closed", "reopened"),
    ("in_progress", "closed"),  # direct won't-fix/cancel is a real action
    ("blocked_on_user", "closed"),
])
def test_valid_transitions_accepted(frm, to):
    assert is_valid_transition(frm, to)


@pytest.mark.parametrize("frm,to", [
    ("totest", "open"),        # would collide with reopened's meaning
    ("closed", "in_progress"), # must go through reopened
    ("closed", "totest"),
    ("blocked_on_user", "totest"),  # can't skip the unblock step
    ("planned", "totest"),
    ("in_progress", "totest"),  # T-0944: must go via to_accept now
    ("to_accept", "open"),      # only totest/reopened/closed are real exits
    ("to_accept", "in_progress"),
])
def test_invalid_transitions_rejected(frm, to):
    assert not is_valid_transition(frm, to)


def test_noop_transition_always_allowed():
    for s in TICKET_STATUSES:
        assert is_valid_transition(s, s)


def test_unknown_from_status_is_permissive_not_locked_out():
    # A legacy/garbage status predating this graph must be CORRECTABLE to any
    # real status, not trapped — see task_states.is_valid_transition docstring.
    assert is_valid_transition("bogus-legacy-value", "open")
    assert not is_valid_transition("bogus-legacy-value", "also-not-real")


def test_invalid_transition_detail_cites_allowed_targets():
    detail = invalid_transition_detail("totest", "open")
    assert "totest" in detail and "open" in detail
    for target in TRANSITIONS["totest"]:
        assert target in detail


# ---------------------------------------------------------------------------
# Orchestration-demand predicate (T-0932's ask)
# ---------------------------------------------------------------------------

def test_blocked_on_user_and_paused_and_planned_and_closed_are_parked():
    for s in ("planned", "paused", "blocked_on_user", "closed"):
        assert is_parked(s), f"{s} should be parked"


def test_open_in_progress_totest_reopened_are_active():
    for s in ("open", "in_progress", "totest", "reopened"):
        assert not is_parked(s), f"{s} should be active"


def test_to_accept_is_active():
    # T-0944: delivered-but-unaccepted work is still orchestration load, just
    # aimed at the operator rather than a dev.
    assert not is_parked("to_accept")


def test_parked_and_active_partition_the_whole_enum():
    assert PARKED_STATES | ACTIVE_STATES == set(TICKET_STATUSES)
    assert PARKED_STATES & ACTIVE_STATES == set()


# ---------------------------------------------------------------------------
# Live enforcement at the PATCH write boundary
# ---------------------------------------------------------------------------

def test_patch_rejects_invalid_transition(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: totest\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "open"},
        )
    assert r.status_code == 400
    assert "totest" in r.json()["detail"] and "open" in r.json()["detail"]
    # And the file itself is untouched — a refused write is a refused write.
    assert "status: totest" in (backlog / "T-0001-foo.md").read_text()


def test_patch_accepts_valid_transition(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: in_progress\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "blocked_on_user"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "blocked_on_user"


def test_patch_accepts_blocked_on_user_resolving_back_to_in_progress(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: blocked_on_user\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "in_progress"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"


def test_patch_rejects_dev_skipping_operator_acceptance(tmp_bot_squad: Path, monkeypatch):
    # T-0944: `in_progress -> totest` is gone — delivery must go via `to_accept`.
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: in_progress\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "totest"},
        )
    assert r.status_code == 400
    assert "in_progress" in r.json()["detail"] and "totest" in r.json()["detail"]
    assert "status: in_progress" in (backlog / "T-0001-foo.md").read_text()


def test_patch_accepts_delivery_to_to_accept_then_operator_acceptance(
    tmp_bot_squad: Path, monkeypatch
):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: in_progress\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "to_accept"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "to_accept"
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "totest"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "totest"


def test_patch_noop_status_is_never_rejected(tmp_bot_squad: Path, monkeypatch):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: totest\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.patch(
            "/api/projects/test-project/backlog/T-0001",
            json={"status": "totest", "title": "Renamed"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "totest"
    assert r.json()["title"] == "Renamed"
