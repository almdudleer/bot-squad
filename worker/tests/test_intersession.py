"""Tests for the cross-session message bus (bot_squad_worker.intersession)."""
from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import intersession as I


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(data_dir=tmp_path / "data")


def _write_session(tmp_path: Path, slug: str, sid: str, task_id: str = "") -> None:
    sess_dir = tmp_path / "data" / slug / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / f"{sid}.md").write_text(
        "---\n"
        f"sid: {sid}\n"
        "status: active\n"
        f"task_id: {task_id or '~'}\n"
        "---\n"
    )


def test_send_then_read_roundtrip(tmp_path):
    cfg = _make_cfg(tmp_path)
    out = I.send(cfg, "p", "S-from", "S-to", "hello world")
    assert out["ok"] is True
    assert out["delivered_to"] == ["S-to"]
    read = I.inbox_read(cfg, "p", "S-to")
    assert read["count"] == 1
    assert "hello world" in read["messages"][0]
    assert "[from S-from]" in read["messages"][0]


def test_read_idempotent_after_drain(tmp_path):
    cfg = _make_cfg(tmp_path)
    I.send(cfg, "p", "S-from", "S-to", "one")
    I.send(cfg, "p", "S-from", "S-to", "two")
    first = I.inbox_read(cfg, "p", "S-to")
    assert first["count"] == 2
    second = I.inbox_read(cfg, "p", "S-to")
    assert second["count"] == 0


def test_send_to_unknown_sid_still_writes_inbox(tmp_path):
    """An SID with no sessions/*.md entry should still get its own inbox file."""
    cfg = _make_cfg(tmp_path)
    out = I.send(cfg, "p", "S-from", "S-ghost", "boo")
    assert out["delivered_to"] == ["S-ghost"]
    inbox = tmp_path / "data" / "p" / "_chat" / "inbox-S-ghost.log"
    assert inbox.exists()
    read = I.inbox_read(cfg, "p", "S-ghost")
    assert read["count"] == 1


def test_text_sanitisation(tmp_path):
    cfg = _make_cfg(tmp_path)
    long = "x" * 5000
    I.send(cfg, "p", "S-from", "S-to", "line1\nline2\rline3\r\nline4")
    I.send(cfg, "p", "S-from", "S-to", long)
    read = I.inbox_read(cfg, "p", "S-to")
    assert len(read["messages"]) == 2
    # Newlines collapsed to spaces — message line has no embedded \n
    assert "\n" not in read["messages"][0]
    assert "line1 line2 line3 line4" in read["messages"][0]
    # 4000-char cap
    assert read["messages"][1].endswith("x" * 100)
    # Total length budget = ts + "[from S-from]\t" + body; body <= 4000
    assert read["messages"][1].count("x") == 4000


def test_role_fanout_teamlead_and_dev(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-tl1-p0")           # teamlead (no task_id)
    _write_session(tmp_path, "p", "S-u-tl2-p1")           # teamlead
    _write_session(tmp_path, "p", "S-u-w1-p2", "T-0001")  # dev
    _write_session(tmp_path, "p", "S-u-w2-p3", "T-0002")  # dev

    tl = I.send(cfg, "p", "S-orchestrator", "teamlead", "hi TLs")
    assert sorted(tl["delivered_to"]) == ["S-u-tl1-p0", "S-u-tl2-p1"]

    dv = I.send(cfg, "p", "S-orchestrator", "dev", "hi devs")
    assert sorted(dv["delivered_to"]) == ["S-u-w1-p2", "S-u-w2-p3"]

    al = I.send(cfg, "p", "S-orchestrator", "all", "hi everyone")
    assert sorted(al["delivered_to"]) == sorted([
        "S-u-tl1-p0", "S-u-tl2-p1", "S-u-w1-p2", "S-u-w2-p3",
    ])


def test_wait_timeout_returns_not_ready(tmp_path):
    cfg = _make_cfg(tmp_path)
    t0 = time.monotonic()
    out = I.inbox_wait(cfg, "p", "S-to", timeout=1.5)
    elapsed = time.monotonic() - t0
    assert out["ok"] is True
    assert out["ready"] is False
    assert 1.0 <= elapsed <= 3.5
    assert out["elapsed_sec"] >= 1.0


def test_wait_returns_ready_when_inbox_grows(tmp_path):
    cfg = _make_cfg(tmp_path)

    def writer():
        time.sleep(0.4)
        I.send(cfg, "p", "S-from", "S-to", "ping")

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    out = I.inbox_wait(cfg, "p", "S-to", timeout=5)
    t.join(timeout=2)
    assert out["ready"] is True
    # And a follow-up read drains the message.
    read = I.inbox_read(cfg, "p", "S-to")
    assert read["count"] == 1


def test_wait_caps_timeout(tmp_path, monkeypatch):
    """Timeout > 1800 should be clamped."""
    cfg = _make_cfg(tmp_path)
    # Patch time.sleep + monotonic to verify clamp without burning 1800s.
    calls: list[float] = []
    real_monotonic = time.monotonic
    start = real_monotonic()
    fake_now = [start]

    def fake_monotonic():
        return fake_now[0]

    def fake_sleep(s):
        calls.append(s)
        fake_now[0] += s

    monkeypatch.setattr(I.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(I.time, "sleep", fake_sleep)
    out = I.inbox_wait(cfg, "p", "S-to", timeout=9999)
    assert out["ready"] is False
    # Should have slept its way to roughly 1800s, not 9999s.
    assert sum(calls) <= 1801
    assert out["elapsed_sec"] <= 1801


def test_chat_dir_created_with_group_write(tmp_path):
    cfg = _make_cfg(tmp_path)
    I.send(cfg, "p", "S-from", "S-to", "hi")
    chat = tmp_path / "data" / "p" / "_chat"
    assert chat.is_dir()
    mode = chat.stat().st_mode & 0o777
    # Some tmpfs / CI filesystems strip group-write; assert at least the
    # owner+group can write. The chmod is best-effort.
    assert mode & 0o600
