"""Tests for the worker UDS client.

Spins up a real FastAPI test server bound to a Unix socket, then exercises
the client against it. Avoids mocks because the IPC contract IS the test.
"""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.worker_client import WorkerClient, WorkerError


@pytest.fixture
async def fake_worker_socket(tmp_path: Path):
    """Run a tiny FastAPI app on a UDS in-process for the duration of one test."""
    import uvicorn

    sock = tmp_path / "worker.sock"
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.0.0", "uptime": 1.0}

    @app.post("/actions/noop")
    def noop(params: dict | None = None) -> dict:
        return {"ok": True, "ts": 12345}

    @app.post("/actions/{name}")
    def unknown(name: str, params: dict | None = None) -> dict:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"unknown action: {name!r}")

    config = uvicorn.Config(app, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    # Wait for socket to bind.
    for _ in range(50):
        if sock.exists():
            break
        await asyncio.sleep(0.01)

    yield sock

    server.should_exit = True
    await task


async def test_health_returns_dict(fake_worker_socket):
    client = WorkerClient(fake_worker_socket)
    r = await client.health()
    assert r["ok"] is True


async def test_noop_returns_dict(fake_worker_socket):
    client = WorkerClient(fake_worker_socket)
    r = await client.call_action("noop", {})
    assert r["ok"] is True


async def test_unknown_action_raises(fake_worker_socket):
    client = WorkerClient(fake_worker_socket)
    with pytest.raises(WorkerError) as excinfo:
        await client.call_action("rm-rf", {})
    assert "unknown action" in str(excinfo.value)


async def test_socket_not_found_raises(tmp_path):
    client = WorkerClient(tmp_path / "nope.sock")
    with pytest.raises(WorkerError):
        await client.health()
