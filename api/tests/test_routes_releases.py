"""Tests for the public release-feed router (T-0082).

Covers the four DoD cases:

- ``/latest`` returns the entry whose version matches ``current`` with a
  populated ``tarball_url`` field.
- ``/{v1}`` and ``/{v2}`` return the matching entries.
- ``/_files/{v1}.tar.gz`` streams the exact bytes whose sha256 matches
  the manifest.
- 404 cases for unknown version and unknown tarball.

The router is mounted at ``/api/releases`` and reads the manifest from
``<DATA_DIR>/bot-squad/releases/index.json``. The tests seed both that
manifest and the two backing tarball stubs under the conftest's
``tmp_bot_squad`` data dir.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv(
        "WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock")
    )
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


def _seed_releases(tmp_bot_squad: Path) -> dict[str, dict]:
    """Write two real tar.gz stubs + manifest pinned to the second.

    Returns ``{version: entry}`` for the two seeded releases so tests
    can assert on the recorded sha256 without recomputing it.
    """
    releases_dir = tmp_bot_squad / "data" / "bot-squad" / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    def _stub(version: str, payload: bytes) -> dict:
        tar = releases_dir / f"{version}.tar.gz"
        tar.write_bytes(payload)
        return {
            "version": version,
            "git_sha": "0" * 40,
            "created_at": "2026-05-16T16:00:00Z",
            "tarball_path": f"data/bot-squad/releases/{version}.tar.gz",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "notes": f"notes for {version}",
        }

    v1 = _stub("v2026.05.16.1", b"first release stub bytes\n")
    v2 = _stub("v2026.05.16.2", b"second release stub bytes\n")

    manifest = {"current": v2["version"], "releases": [v1, v2]}
    (releases_dir / "index.json").write_text(json.dumps(manifest))
    return {v1["version"]: v1, v2["version"]: v2}


# ---------------------------------------------------------------------------
# DoD #1 — /latest returns the second entry with tarball_url populated
# ---------------------------------------------------------------------------

def test_latest_returns_current_entry_with_tarball_url(tmp_bot_squad: Path, monkeypatch):
    seeded = _seed_releases(tmp_bot_squad)
    expected = seeded["v2026.05.16.2"]
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/latest")
    assert r.status_code == 200
    body = r.json()
    # All manifest fields round-trip.
    for k, v in expected.items():
        assert body[k] == v
    # tarball_url is computed at response time off the request base URL.
    assert body["tarball_url"].endswith(
        "/api/releases/_files/v2026.05.16.2.tar.gz"
    )
    assert body["tarball_url"].startswith("http")


def test_latest_is_anonymous(tmp_bot_squad: Path, monkeypatch):
    """No auth header required — v0 spec is explicit on this."""
    _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/latest")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# DoD #2 — /<v1> and /<v2> return correct entries
# ---------------------------------------------------------------------------

def test_get_specific_version_v1(tmp_bot_squad: Path, monkeypatch):
    seeded = _seed_releases(tmp_bot_squad)
    expected = seeded["v2026.05.16.1"]
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/v2026.05.16.1")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == expected["version"]
    assert body["sha256"] == expected["sha256"]
    assert body["tarball_url"].endswith(
        "/api/releases/_files/v2026.05.16.1.tar.gz"
    )


def test_get_specific_version_v2(tmp_bot_squad: Path, monkeypatch):
    seeded = _seed_releases(tmp_bot_squad)
    expected = seeded["v2026.05.16.2"]
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/v2026.05.16.2")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == expected["version"]
    assert body["sha256"] == expected["sha256"]


# ---------------------------------------------------------------------------
# DoD #3 — /_files/<v>.tar.gz streams correct bytes (sha256 matches)
# ---------------------------------------------------------------------------

def test_tarball_streams_exact_bytes_with_matching_sha(
    tmp_bot_squad: Path, monkeypatch
):
    seeded = _seed_releases(tmp_bot_squad)
    expected = seeded["v2026.05.16.1"]
    on_disk = (
        tmp_bot_squad
        / "data"
        / "bot-squad"
        / "releases"
        / "v2026.05.16.1.tar.gz"
    ).read_bytes()
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/_files/v2026.05.16.1.tar.gz")
    assert r.status_code == 200
    assert r.content == on_disk
    assert hashlib.sha256(r.content).hexdigest() == expected["sha256"]
    assert r.headers["content-type"] == "application/gzip"
    assert r.headers["content-length"] == str(len(on_disk))


# ---------------------------------------------------------------------------
# DoD #4 — 404 cases
# ---------------------------------------------------------------------------

def test_unknown_version_returns_404_json(tmp_bot_squad: Path, monkeypatch):
    _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/v9999.99.99.9")
    assert r.status_code == 404
    # JSON error envelope, not HTML — consumers expect to .json() this.
    assert r.headers["content-type"].startswith("application/json")
    assert "v9999.99.99.9" in r.json()["detail"]


def test_unknown_tarball_returns_404_json(tmp_bot_squad: Path, monkeypatch):
    _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/_files/v9999.99.99.9.tar.gz")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_missing_manifest_returns_404(tmp_bot_squad: Path, monkeypatch):
    """Detached single-install with no manifest → /latest is a clean 404."""
    # No _seed_releases call → no manifest on disk.
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/latest")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_tarball_rejects_path_traversal(tmp_bot_squad: Path, monkeypatch):
    """Defense-in-depth: ``..`` in the filename is refused before stat."""
    _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        # TestClient normalises ``..`` segments before dispatch, so we
        # smuggle the traversal as a non-slashed dotted name instead.
        r = client.get("/api/releases/_files/..hidden.tar.gz")
    assert r.status_code == 404


def test_tarball_rejects_non_targz_name(tmp_bot_squad: Path, monkeypatch):
    _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/releases/_files/index.json")
    assert r.status_code == 404
