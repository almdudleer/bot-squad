"""T-0391 (audit Fork-6 READY): install-state FSM close.

A peer server reached ``connected`` at ``/installer/connect`` but nothing ever
moved it to ``ready`` — the state the FE gates cross-server fan-out on — so peers
froze at ``connected`` forever. This closes the loop: when the installer posts
its TERMINAL checkpoint (``print_attach`` / ``done``), the server transitions
connected|failed → ready.

Flaw-watch: ``ready`` hinges on an exact install.sh step name. The constant
``TERMINAL_INSTALL_CHECKPOINT`` is co-located with a contract test that asserts
it matches the terminal step of ``INSTALL_STEPS`` (and is NOT the invite path's
``print_join_attach``, which must not flip an existing server to ready).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore


_BCRYPT_TEST = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        "[users]\n"
        f'testuser = "{_BCRYPT_TEST}"\n'
        "[user_meta.testuser]\n"
        'linux_user = "almdudleer"\n'
        "is_admin = true\n"
        '[session]\nttl = "7d"\n'
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    return TestClient(build_app())


def _connect(client: TestClient) -> tuple[str, str]:
    """Register + connect a server; return (server_id, server_bearer). Leaves
    the server at install_state=connected."""
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    body = client.post(
        "/api/m/servers", json={"display_name": "S", "base_url": "https://s.example.com"}
    ).json()
    token, sid = body["install_token"], body["id"]
    client.cookies.clear()
    bearer = client.post(
        "/api/m/installer/connect", json={"token": token, "server_meta": {"hostname": "h"}}
    ).json()["server_bearer"]
    return sid, bearer


def _state(tmp_bot_squad: Path, sid: str) -> str:
    return MothershipStore(tmp_bot_squad / "data" / "_mothership").get_server(sid).install_state


# ---- store: mark_install_state FSM ------------------------------------------


def _srv(state: str) -> AttachedServer:
    return AttachedServer(
        id="srv_x", display_name="x", base_url="https://x", owner_user="u",
        created_at="2026-01-01T00:00:00Z", install_state=state,
    )


def test_mark_install_state_connected_to_ready(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([_srv("connected")])
    out = store.mark_install_state("srv_x", "ready", allowed_from={"connected", "failed"})
    assert out.install_state == "ready"
    assert _state(tmp_bot_squad, "srv_x") == "ready"


def test_mark_install_state_failed_to_ready_recovery(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([_srv("failed")])
    out = store.mark_install_state("srv_x", "ready", allowed_from={"connected", "failed"})
    assert out.install_state == "ready"


def test_mark_install_state_noop_when_state_not_allowed(tmp_bot_squad: Path):
    """An already-ready (or pending) server is left untouched — idempotent + safe."""
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([_srv("ready")])
    out = store.mark_install_state("srv_x", "ready", allowed_from={"connected", "failed"})
    assert out.install_state == "ready"  # unchanged, no error

    store.write([_srv("pending")])
    out = store.mark_install_state("srv_x", "ready", allowed_from={"connected", "failed"})
    assert out.install_state == "pending"  # NOT promoted from pending


def test_mark_install_state_unknown_returns_none(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([])
    assert store.mark_install_state("nope", "ready", allowed_from={"connected"}) is None


# ---- route: terminal checkpoint flips connected → ready ----------------------


def test_terminal_checkpoint_marks_ready(tmp_bot_squad: Path, monkeypatch):
    from app.routes_mothership import TERMINAL_INSTALL_CHECKPOINT

    with _client(tmp_bot_squad, monkeypatch) as client:
        sid, bearer = _connect(client)
        assert _state(tmp_bot_squad, sid) == "connected"
        r = client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"checkpoint": TERMINAL_INSTALL_CHECKPOINT, "status": "done"},
        )
        assert r.status_code in (200, 204), r.text
    assert _state(tmp_bot_squad, sid) == "ready"


def test_nonterminal_checkpoint_leaves_connected(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        sid, bearer = _connect(client)
        client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"checkpoint": "install_docker", "status": "done"},
        )
    assert _state(tmp_bot_squad, sid) == "connected"


def test_terminal_checkpoint_begin_does_not_mark_ready(tmp_bot_squad: Path, monkeypatch):
    from app.routes_mothership import TERMINAL_INSTALL_CHECKPOINT

    with _client(tmp_bot_squad, monkeypatch) as client:
        sid, bearer = _connect(client)
        client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"checkpoint": TERMINAL_INSTALL_CHECKPOINT, "status": "begin"},
        )
    assert _state(tmp_bot_squad, sid) == "connected"  # only 'done' promotes


def test_invite_terminal_checkpoint_does_not_mark_ready(tmp_bot_squad: Path, monkeypatch):
    """The invite path's terminal step (print_join_attach) must NOT flip an
    existing server's state."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        sid, bearer = _connect(client)
        client.post(
            "/api/m/installer/checkpoint",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"checkpoint": "print_join_attach", "status": "done"},
        )
    assert _state(tmp_bot_squad, sid) == "connected"


# ---- contract: the constant matches install.sh's terminal step --------------


def _find_install_sh() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "install" / "install.sh",
        Path(os.environ.get("INSTALL_BUNDLE_DIR", "/app/scripts/install")) / "install.sh",
        Path("/app/scripts/install/install.sh"),
    ]
    return next((p for p in candidates if p.is_file()), None)


def _steps_array(text: str, name: str) -> list[str]:
    m = re.search(rf"{name}=\((.*?)\)", text, re.DOTALL)
    assert m, f"{name} array not found in install.sh"
    return [w for w in m.group(1).split() if w and not w.startswith("#")]


def test_terminal_constant_matches_install_sh_contract():
    from app.routes_mothership import TERMINAL_INSTALL_CHECKPOINT

    sh = _find_install_sh()
    if sh is None:
        pytest.skip("install.sh not present in this test image (api-only mount)")
    text = sh.read_text(encoding="utf-8")
    install_steps = _steps_array(text, "INSTALL_STEPS")
    invite_steps = _steps_array(text, "INVITE_STEPS")
    assert install_steps[-1] == TERMINAL_INSTALL_CHECKPOINT, (
        f"install.sh INSTALL_STEPS terminal is {install_steps[-1]!r} but "
        f"TERMINAL_INSTALL_CHECKPOINT is {TERMINAL_INSTALL_CHECKPOINT!r} — they MUST agree"
    )
    assert invite_steps[-1] != TERMINAL_INSTALL_CHECKPOINT, (
        "the invite path's terminal step must NOT equal the install terminal "
        "(it would flip an existing server to ready)"
    )
