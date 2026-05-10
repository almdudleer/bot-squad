"""API test fixtures."""
from __future__ import annotations

import hashlib
import hmac
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
        'deploy_branch = "agent_team/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (tmp_path / "config" / "auth.toml").write_text(
        '[telegram_login]\n'
        'allowed_ids = [12345]\n'
        'session_ttl = "7d"\n'
    )
    return tmp_path


@pytest.fixture
def fake_worker_tg(tmp_bot_squad: Path):
    """Start a fake worker (handles tg_verify_login) in a background thread."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()

    BOT_TOKEN = "TESTBOT:TOKEN"

    @fake.post("/actions/tg_verify_login")
    def tg_verify_login(body: dict) -> dict:
        payload = body.get("payload", {})
        if "hash" not in payload or "auth_date" not in payload or "id" not in payload:
            return {"ok": False, "error": "missing required field"}
        received_hash = payload["hash"]
        fields = {k: v for k, v in payload.items() if k != "hash"}
        data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
        expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return {"ok": False, "error": "bad TG login hash"}
        return {"ok": True, "user": {"id": int(fields["id"]), "first_name": fields.get("first_name", "")}}

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
