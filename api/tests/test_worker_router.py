"""Tests for the Phase 2 WorkerRouter."""
from __future__ import annotations

from pathlib import Path

from app.worker_client import WorkerRouter


def test_for_user_returns_coordinator_client_for_coordinator(tmp_path: Path):
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer",
                     known_users={"edem"})
    c = r.for_user("almdudleer")
    assert c.sock_path == sock


def test_for_user_returns_user_sock_for_non_coordinator(tmp_path: Path):
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer",
                     known_users={"edem"})
    c = r.for_user("edem")
    assert c.sock_path == sock.parent / "user-edem.sock"


def test_for_sid_routes_via_linux_user(tmp_path: Path):
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer",
                     known_users={"edem"})

    c1 = r.for_sid("S-almdudleer-spec5-p2")
    assert c1.sock_path == sock

    c2 = r.for_sid("S-edem-feature-p3")
    assert c2.sock_path == sock.parent / "user-edem.sock"


def test_for_sid_falls_back_to_coordinator_for_unknown_format(tmp_path: Path):
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer")
    c = r.for_sid("not-a-sid")
    assert c.sock_path == sock


def test_user_for_sid_parses_user(tmp_path: Path):
    r = WorkerRouter(coordinator_sock=tmp_path / "x.sock",
                     coordinator_user="almdudleer")
    assert r.user_for_sid("S-edem-foo-p1") == "edem"
    assert r.user_for_sid("garbage") is None


def test_all_user_workers_includes_coordinator_and_known_users(tmp_path: Path):
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    r = WorkerRouter(
        coordinator_sock=sock,
        coordinator_user="almdudleer",
        known_users={"edem", "dima"},
    )
    pairs = r.all_user_workers()
    users = {u for (u, _) in pairs}
    assert users == {"almdudleer", "edem", "dima"}

    # Sock paths per user
    by_user = {u: c.sock_path for (u, c) in pairs}
    assert by_user["almdudleer"] == sock
    assert by_user["edem"] == sock.parent / "user-edem.sock"
    assert by_user["dima"] == sock.parent / "user-dima.sock"


def test_known_users_dedupes_coordinator(tmp_path: Path):
    """If coordinator user appears in known_users, it shouldn't duplicate."""
    sock = tmp_path / "x.sock"
    r = WorkerRouter(
        coordinator_sock=sock,
        coordinator_user="almdudleer",
        known_users={"almdudleer", "edem"},
    )
    users = [u for (u, _) in r.all_user_workers()]
    assert users.count("almdudleer") == 1
    assert "edem" in users
