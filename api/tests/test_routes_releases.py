"""Tests for the public release-feed router (T-0082) + T-0087 extensions.

T-0082 covers the four original DoD cases:

- ``/latest`` returns the entry whose version matches ``current`` with a
  populated ``tarball_url`` field.
- ``/{v1}`` and ``/{v2}`` return the matching entries.
- ``/_files/{v1}.tar.gz`` streams the exact bytes whose sha256 matches
  the manifest.
- 404 cases for unknown version and unknown tarball.

T-0087 adds the mothership UI surface:

- ``GET /api/releases/_all`` (mothership-only, auth-required) returns
  the full manifest list newest-first.
- ``POST /api/releases/_notes_draft`` stages a ``<version>.md`` notes
  file for the next prod.sh cut, using the same vYYYY.MM.DD.N counter.

The router is mounted at ``/api/releases`` and reads the manifest from
``<DATA_DIR>/bot-squad/releases/index.json``. The tests seed both that
manifest and the two backing tarball stubs under the conftest's
``tmp_bot_squad`` data dir.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client(
    tmp_bot_squad: Path, monkeypatch, *, mothership: bool = False
) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv(
        "WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock")
    )
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    # Same posture as the telemetry tests — point WEB_DIST at nothing
    # so the SPA catch-all doesn't swallow /api 404s we're asserting on.
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)
    return TestClient(build_app())


def _login(client: TestClient) -> None:
    r = client.post(
        "/api/auth/login", json={"username": "testuser", "password": "test"}
    )
    assert r.status_code == 200, r.text


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


# ---------------------------------------------------------------------------
# T-0087 — GET /_all (mothership-only, auth-required)
# ---------------------------------------------------------------------------


def test_all_returns_full_manifest_newest_first(
    tmp_bot_squad: Path, monkeypatch
):
    """Happy path: every manifest entry comes back with tarball_url set,
    ordered newest-first regardless of on-disk order."""
    seeded = _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/releases/_all")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 2
    # Newest-first: v2026.05.16.2 before v2026.05.16.1. prod.sh appends
    # newest entries, so the file order is v1 -> v2; the router reverses.
    assert rows[0]["version"] == "v2026.05.16.2"
    assert rows[1]["version"] == "v2026.05.16.1"
    # Per-entry shape mirrors /latest: every manifest field round-trips
    # AND tarball_url is filled in for both entries.
    for row, version in zip(rows, ["v2026.05.16.2", "v2026.05.16.1"]):
        expected = seeded[version]
        for k, v in expected.items():
            assert row[k] == v
        assert row["tarball_url"].endswith(
            f"/api/releases/_files/{version}.tar.gz"
        )


def test_all_empty_when_no_manifest(tmp_bot_squad: Path, monkeypatch):
    """Fresh mothership before the first cut → 200 with empty list, not 404.

    The detached /latest endpoint 404s on a missing manifest because the
    consumer expects "no release yet" to be a transport failure. The
    operator-facing _all returns an empty list so the UI table renders
    its empty-state banner without parsing an error envelope."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/releases/_all")
    assert r.status_code == 200
    assert r.json() == []


def test_all_is_404_on_non_mothership(tmp_bot_squad: Path, monkeypatch):
    """Detached single-install → _all is 404, same posture as _telemetry GET."""
    _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch, mothership=False) as client:
        _login(client)
        r = client.get("/api/releases/_all")
    assert r.status_code == 404


def test_all_requires_auth(tmp_bot_squad: Path, monkeypatch):
    """No cookie on a mothership install → 401."""
    _seed_releases(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.get("/api/releases/_all")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# T-0087 — POST /_notes_draft
# ---------------------------------------------------------------------------


def _today_utc_date_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y.%m.%d")


def test_notes_draft_writes_next_version_md(tmp_bot_squad: Path, monkeypatch):
    """Happy path: POST stages a <next-version>.md file under releases_dir
    and the response echoes the computed version."""
    # Seed an existing manifest so the counter logic kicks in (N starts at
    # ``max(existing today's-N) + 1`` per prod.sh step 2).
    releases_dir = tmp_bot_squad / "data" / "bot-squad" / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    date_tag = _today_utc_date_tag()
    existing_version = f"v{date_tag}.3"
    (releases_dir / "index.json").write_text(
        json.dumps(
            {
                "current": existing_version,
                "releases": [
                    {
                        "version": existing_version,
                        "git_sha": "0" * 40,
                        "created_at": "2026-05-16T16:00:00Z",
                        "tarball_path": (
                            f"data/bot-squad/releases/{existing_version}.tar.gz"
                        ),
                        "sha256": "deadbeef",
                        "notes": "",
                    }
                ],
            }
        )
    )

    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post(
            "/api/releases/_notes_draft",
            json={"notes": "# Hello\n\nFirst draft.\n"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    expected_version = f"v{date_tag}.4"
    assert body == {
        "ok": True,
        "version": expected_version,
        "path": f"data/bot-squad/releases/{expected_version}.md",
    }
    notes_file = releases_dir / f"{expected_version}.md"
    assert notes_file.is_file()
    assert notes_file.read_text() == "# Hello\n\nFirst draft.\n"


def test_notes_draft_counter_starts_at_one_with_no_manifest(
    tmp_bot_squad: Path, monkeypatch
):
    """No manifest yet → first draft is v<today>.1 (matches prod.sh)."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post(
            "/api/releases/_notes_draft", json={"notes": "first cut"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == f"v{_today_utc_date_tag()}.1"
    releases_dir = tmp_bot_squad / "data" / "bot-squad" / "releases"
    assert (releases_dir / f"{body['version']}.md").is_file()


def test_notes_draft_replaces_existing_draft(tmp_bot_squad: Path, monkeypatch):
    """Re-POSTing before cut overwrites the staged file — the counter
    only advances after prod.sh appends to the manifest, so iterations
    on copy keep the same target version."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        first = client.post(
            "/api/releases/_notes_draft", json={"notes": "draft 1"}
        )
        assert first.status_code == 200
        second = client.post(
            "/api/releases/_notes_draft", json={"notes": "draft 2 — final"}
        )
        assert second.status_code == 200
    # Same version both times.
    assert first.json()["version"] == second.json()["version"]
    notes_file = (
        tmp_bot_squad
        / "data"
        / "bot-squad"
        / "releases"
        / f"{first.json()['version']}.md"
    )
    assert notes_file.read_text() == "draft 2 — final"


def test_notes_draft_rejects_empty(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        empty = client.post(
            "/api/releases/_notes_draft", json={"notes": ""}
        )
        whitespace = client.post(
            "/api/releases/_notes_draft", json={"notes": "   \n\t"}
        )
        missing_key = client.post("/api/releases/_notes_draft", json={})
        wrong_type = client.post(
            "/api/releases/_notes_draft", json={"notes": 123}
        )
    assert empty.status_code == 400
    assert whitespace.status_code == 400
    assert missing_key.status_code == 400
    assert wrong_type.status_code == 400


def test_notes_draft_is_404_on_non_mothership(
    tmp_bot_squad: Path, monkeypatch
):
    with _client(tmp_bot_squad, monkeypatch, mothership=False) as client:
        _login(client)
        r = client.post(
            "/api/releases/_notes_draft", json={"notes": "x"}
        )
    assert r.status_code == 404
    # No file should have been written.
    releases_dir = tmp_bot_squad / "data" / "bot-squad" / "releases"
    if releases_dir.is_dir():
        assert not list(releases_dir.glob("*.md"))


def test_notes_draft_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/releases/_notes_draft", json={"notes": "x"}
        )
    assert r.status_code == 401
