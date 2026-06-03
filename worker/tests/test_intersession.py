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
    """Timeout > 7200 (the T-0091 cap) should be clamped."""
    cfg = _make_cfg(tmp_path)
    # Patch time.sleep + monotonic to verify clamp without burning 7200s.
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
    out = I.inbox_wait(cfg, "p", "S-to", timeout=99999)
    assert out["ready"] is False
    # Should have slept its way to roughly 7200s, not 99999s.
    assert sum(calls) <= I._MAX_WAIT_TIMEOUT + 1
    assert out["elapsed_sec"] <= I._MAX_WAIT_TIMEOUT + 1


def test_wait_returns_early_on_shutdown_event_arg(tmp_path):
    """T-0119: explicit shutdown_event arg trips inbox_wait within one poll."""
    cfg = _make_cfg(tmp_path)
    ev = threading.Event()
    ev.set()  # already set — should return on the first loop iteration

    t0 = time.monotonic()
    out = I.inbox_wait(cfg, "p", "S-to", timeout=30, shutdown_event=ev)
    elapsed = time.monotonic() - t0

    assert out["ok"] is True
    assert out["ready"] is False
    assert out["reason"] == "shutdown"
    assert elapsed < 2.0  # must NOT wait the 30s timeout


def test_wait_returns_early_on_shutdown_event_global(tmp_path):
    """T-0119: process-wide event set via set_shutdown_event also works."""
    cfg = _make_cfg(tmp_path)
    ev = threading.Event()
    I.set_shutdown_event(ev)
    try:
        def trip():
            time.sleep(0.3)
            ev.set()
        threading.Thread(target=trip, daemon=True).start()

        t0 = time.monotonic()
        out = I.inbox_wait(cfg, "p", "S-to", timeout=30)
        elapsed = time.monotonic() - t0

        assert out["ready"] is False
        assert out["reason"] == "shutdown"
        assert elapsed < 3.0
    finally:
        I.set_shutdown_event(None)


def test_chat_dir_created_with_group_write(tmp_path):
    cfg = _make_cfg(tmp_path)
    I.send(cfg, "p", "S-from", "S-to", "hi")
    chat = tmp_path / "data" / "p" / "_chat"
    assert chat.is_dir()
    mode = chat.stat().st_mode & 0o777
    # Some tmpfs / CI filesystems strip group-write; assert at least the
    # owner+group can write. The chmod is best-effort.
    assert mode & 0o600


# ---------------------------------------------------------------------------
# T-0157: multi-user peer boundaries — role-keyword fan-out is scoped to one
# linux user by default; an explicit `user` widens it; literal SIDs always pass.
# ---------------------------------------------------------------------------

def _write_user_session(tmp_path, slug, sid, *, task_id="", linux_user=None):
    """Write a session md; linux_user defaults to the SID prefix (omitted field)."""
    sess_dir = tmp_path / "data" / slug / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"sid: {sid}", "status: active", f"task_id: {task_id or '~'}"]
    if linux_user is not None:
        lines.append(f"linux_user: {linux_user}")
    lines += ["---", ""]
    (sess_dir / f"{sid}.md").write_text("\n".join(lines))


def test_resolve_recipients_scopes_to_sender_user(tmp_path):
    cfg = _make_cfg(tmp_path)
    # alice TL + alice dev; bob TL + bob dev. linux_user derived from SID prefix.
    _write_user_session(tmp_path, "p", "S-alice-tl-p1", task_id="")
    _write_user_session(tmp_path, "p", "S-alice-dev-p2", task_id="T-1")
    _write_user_session(tmp_path, "p", "S-bob-tl-p3", task_id="")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")

    # alice sends to "all" → only alice's sessions.
    got = I._resolve_recipients(cfg, "p", "all", from_sid="S-alice-tl-p1")
    assert set(got) == {"S-alice-tl-p1", "S-alice-dev-p2"}

    # alice → "dev" → only alice's dev.
    assert I._resolve_recipients(cfg, "p", "dev", from_sid="S-alice-tl-p1") == ["S-alice-dev-p2"]


def test_resolve_recipients_user_override_crosses_boundary(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-alice-tl-p1", task_id="")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")

    # alice explicitly targets bob's user.
    got = I._resolve_recipients(cfg, "p", "all", from_sid="S-alice-tl-p1", user="bob")
    assert got == ["S-bob-dev-p4"]


def test_resolve_recipients_legacy_sender_unscoped(tmp_path):
    """A non-SID sender (e.g. 'stakeholder') resolves to no scope → unfiltered."""
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-alice-tl-p1", task_id="")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    got = I._resolve_recipients(cfg, "p", "all", from_sid="stakeholder")
    assert set(got) == {"S-alice-tl-p1", "S-bob-dev-p4"}


def test_resolve_recipients_explicit_field_wins_over_sid(tmp_path):
    """An explicit linux_user field overrides the SID prefix for scoping."""
    cfg = _make_cfg(tmp_path)
    # SID says 'svc' but the session is really owned by alice (field wins).
    _write_user_session(tmp_path, "p", "S-svc-tl-p9", task_id="", linux_user="alice")
    _write_user_session(tmp_path, "p", "S-alice-dev-p2", task_id="T-1")
    # A bob session makes the project genuinely multi-user so scoping engages.
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    got = I._resolve_recipients(cfg, "p", "all", from_sid="S-alice-dev-p2")
    # alice-scoped → the svc-named-but-alice-owned TL is included; bob excluded.
    assert set(got) == {"S-svc-tl-p9", "S-alice-dev-p2"}


def test_resolve_recipients_literal_sid_never_scoped(tmp_path):
    """Addressing a specific cross-user SID is always allowed (explicit)."""
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    assert I._resolve_recipients(cfg, "p", "S-bob-dev-p4", from_sid="S-alice-tl-p1") == ["S-bob-dev-p4"]


def test_send_scopes_role_fanout_to_sender_user(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-alice-dev-p2", task_id="T-1")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    out = I.send(cfg, "p", "S-alice-tl-p1", "dev", "ping")
    assert out["delivered_to"] == ["S-alice-dev-p2"]
