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


def test_for_user_returns_user_sock_when_socket_exists(tmp_path: Path):
    """When the per-user worker socket file exists, for_user routes to it."""
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    edem_sock = sock.parent / "user-edem.sock"
    edem_sock.touch()
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer",
                     known_users={"edem"})
    c = r.for_user("edem")
    assert c.sock_path == edem_sock


def test_for_user_falls_back_to_coordinator_when_socket_missing(tmp_path: Path):
    """T-0080: missing per-user socket falls back to coordinator so second-
    user spawn / pause / resume actions still succeed in single-tenant
    installs where no per-user worker is running."""
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer",
                     known_users={"edem"})
    c = r.for_user("edem")
    assert c.sock_path == sock


def test_for_sid_routes_via_linux_user(tmp_path: Path):
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    edem_sock = sock.parent / "user-edem.sock"
    edem_sock.touch()
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer",
                     known_users={"edem"})

    c1 = r.for_sid("S-almdudleer-spec5-p2")
    assert c1.sock_path == sock

    c2 = r.for_sid("S-edem-feature-p3")
    assert c2.sock_path == edem_sock


def test_for_sid_falls_back_to_coordinator_when_user_sock_missing(tmp_path: Path):
    """T-0080: SID for a user without a worker socket falls back to coordinator."""
    sock = tmp_path / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True)
    r = WorkerRouter(coordinator_sock=sock, coordinator_user="almdudleer",
                     known_users={"edem"})
    c = r.for_sid("S-edem-feature-p3")
    assert c.sock_path == sock


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
