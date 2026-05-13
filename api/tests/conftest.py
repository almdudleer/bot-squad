"""API test fixtures."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI


@pytest.fixture
def tmp_bot_squad(tmp_path: Path) -> Path:
    """Provide a complete bot-squad layout for tests."""
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "_sock").mkdir(parents=True)
    (tmp_path / "data" / "_worker").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "backlog").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "vision").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "feedback").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "sessions").mkdir(parents=True)

    (tmp_path / "config" / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        'repo_path = "/tmp/test-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (tmp_path / "config" / "auth.toml").write_text(
        '[users]\n'
        # bcrypt hash of "test" (rounds=12)
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        # Phase 2 multi-user: default test user is admin so tests can use
        # any linux-user SID without tripping the ownership gate. Tests that
        # care about non-admin behaviour rewrite auth.toml themselves.
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = true\n'
        '[session]\nttl = "7d"\n'
    )
    return tmp_path


@pytest.fixture
def fake_worker(tmp_bot_squad: Path):
    """Start a minimal fake worker in a background thread."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()

    @fake.post("/actions/noop")
    def noop(params: dict | None = None) -> dict:
        return {"ok": True, "ts": 99}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for socket to bind.
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock

    server.should_exit = True
    thread.join(timeout=5)


# Keep alias for test files that still reference fake_worker_tg
@pytest.fixture
def fake_worker_tg(fake_worker):
    return fake_worker
