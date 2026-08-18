"""T-0880: the deploy/health signals could not see a per-user worker at all.

These tests bind REAL Unix sockets and speak REAL HTTP over them, because the
thing under test is precisely whether we can find and interrogate a second
process serving the same install. Mocking the transport would assert the shape
of the answer while skipping the question.
"""
from __future__ import annotations

import http.server
import json
import socket
import socketserver
import threading
from pathlib import Path

import pytest

from bot_squad_worker import worker_census as wc

DEPLOYED = "d" * 40
STALE_SHA = "5" * 40


class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):  # HTTPServer's own version assumes AF_INET
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


def _serve(sock_path: Path, body: dict | None, status: int = 200):
    """Run a worker-shaped /health on a real UDS. body=None → 500 (a worker
    that is up but broken), so 'alive' is never inferred from 'the file is
    there'."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if body is None:
                self.send_response(500)
                self.end_headers()
                return
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # silence
            pass

    srv = _UnixHTTPServer(str(sock_path), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture()
def sock_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "_sock"
    d.mkdir(parents=True)
    return tmp_path / "data"


def _health(sha: str) -> dict:
    return {"ok": True, "git_sha": sha, "boot_git_sha": sha,
            "install_git_sha": DEPLOYED, "uptime": 12.5}


def test_census_distinguishes_a_stale_per_user_worker_from_the_coordinator(sock_dir):
    """DoD 4, the whole point: two workers on ONE config/install, one converged
    and one days behind, and the report must not be able to say "deployed"."""
    servers = [
        _serve(sock_dir / "_sock" / "worker.sock", _health(DEPLOYED)),
        _serve(sock_dir / "_sock" / "user-flomaster.sock", _health(STALE_SHA)),
    ]
    try:
        rows = wc.census(sock_dir, deployed_sha=DEPLOYED)
        by_kind = {r["kind"]: r for r in rows}

        assert len(rows) == 2, rows
        assert by_kind["coordinator"]["state"] == wc.CONVERGED
        assert by_kind["user"]["state"] == wc.STALE
        assert by_kind["user"]["linux_user"] == "flomaster"
        assert by_kind["user"]["label"] == "user:flomaster"
        # the coordinator is labelled by ROLE even though its socket is
        # owned by a real account — otherwise the two rows can collide
        assert by_kind["coordinator"]["label"].startswith("coordinator")

        summary = wc.summarize(rows)
        assert summary["all_converged"] is False
        # The stale worker must be NAMED — "1/2 converged" alone still lets a
        # reader assume the missing one is unimportant.
        assert "flomaster" in summary["line"]
        assert wc.STALE in summary["line"]
    finally:
        for s in servers:
            s.shutdown()


def test_all_converged_only_when_every_worker_matches(sock_dir):
    servers = [
        _serve(sock_dir / "_sock" / "worker.sock", _health(DEPLOYED)),
        _serve(sock_dir / "_sock" / "user-flomaster.sock", _health(DEPLOYED)),
    ]
    try:
        summary = wc.summarize(wc.census(sock_dir, deployed_sha=DEPLOYED))
        assert summary["all_converged"] is True
        assert summary["counts"][wc.CONVERGED] == 2
    finally:
        for s in servers:
            s.shutdown()


def test_a_worker_that_cannot_report_its_sha_is_unknown_not_converged(sock_dir):
    """The pre-T-0880 live reading: a per-user worker answered "" for every sha
    because git refused a tree owned by another user. Empty must never compare
    equal to the deployed sha, and must not read as stale either."""
    servers = [
        _serve(sock_dir / "_sock" / "worker.sock", _health(DEPLOYED)),
        _serve(sock_dir / "_sock" / "user-flomaster.sock", _health("")),
    ]
    try:
        rows = wc.census(sock_dir, deployed_sha=DEPLOYED)
        flo = [r for r in rows if r["linux_user"] == "flomaster"][0]
        assert flo["state"] == wc.UNKNOWN
        assert flo["state"] != wc.CONVERGED
        assert wc.summarize(rows)["all_converged"] is False
        assert "flomaster" in wc.summarize(rows)["line"]
    finally:
        for s in servers:
            s.shutdown()


def test_a_dead_socket_is_unreachable_not_converged(sock_dir):
    """A leftover socket file with nothing behind it — the shape a crashed
    per-user worker leaves. Must not silently count as converged."""
    (sock_dir / "_sock" / "worker.sock").write_bytes(b"")
    (sock_dir / "_sock" / "user-ghost.sock").write_bytes(b"")
    rows = wc.census(sock_dir, deployed_sha=DEPLOYED, timeout=1.0)
    assert {r["state"] for r in rows} == {wc.UNREACHABLE}
    assert wc.summarize(rows)["all_converged"] is False


def test_unknown_deployed_sha_does_not_certify_anything(sock_dir):
    """If we do not know what was deployed, no worker can be called converged."""
    servers = [_serve(sock_dir / "_sock" / "worker.sock", _health(DEPLOYED))]
    try:
        rows = wc.census(sock_dir, deployed_sha="")
        assert rows[0]["state"] == wc.UNKNOWN
        assert wc.summarize(rows)["all_converged"] is False
    finally:
        for s in servers:
            s.shutdown()


def test_a_worker_that_is_up_but_erroring_is_unreachable(sock_dir):
    servers = [_serve(sock_dir / "_sock" / "user-flomaster.sock", None)]
    try:
        rows = wc.census(sock_dir, deployed_sha=DEPLOYED)
        assert rows[0]["state"] == wc.UNREACHABLE
        assert "500" in rows[0]["detail"]
    finally:
        for s in servers:
            s.shutdown()


def test_no_sockets_is_not_a_pass(sock_dir):
    summary = wc.summarize(wc.census(sock_dir, deployed_sha=DEPLOYED))
    assert summary["all_converged"] is False
    assert "nothing could be measured" in summary["line"]
