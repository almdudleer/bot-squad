"""Tests for T-0067 attach_hooks — the post-attach seam Bundle B calls."""
from __future__ import annotations

from pathlib import Path

from app.attach_hooks import (
    clear_enable_pending,
    is_enable_pending,
    on_attach,
)


def test_on_attach_writes_marker(tmp_path: Path) -> None:
    result = on_attach(tmp_path, "bob")
    assert result["ok"] is True
    marker = tmp_path / "_users" / "bob" / "enable-worker.pending"
    assert marker.is_file()
    # 4-char year + dash → looks like ISO timestamp; just check non-empty.
    body = marker.read_text().strip()
    assert body
    # mode 0644 — readable across the install (no secret).
    assert (marker.stat().st_mode & 0o777) == 0o644


def test_on_attach_is_idempotent(tmp_path: Path) -> None:
    on_attach(tmp_path, "bob")
    first = (tmp_path / "_users" / "bob" / "enable-worker.pending").read_text()
    # Same user twice — overwrites timestamp, no error.
    result = on_attach(tmp_path, "bob")
    assert result["ok"] is True
    # File still exists (the marker is the source of truth).
    assert (tmp_path / "_users" / "bob" / "enable-worker.pending").is_file()
    # Body may match or be a fresh timestamp; either is fine.
    _ = first


def test_on_attach_rejects_invalid_username(tmp_path: Path) -> None:
    # Path-traversal attempt + uppercase + too long.
    for bad in ("../etc/passwd", "Bob", "x" * 64, "", "with spaces", "with/slash"):
        result = on_attach(tmp_path, bad)
        assert result["ok"] is False
        assert "invalid" in (result["reason"] or "")


def test_is_enable_pending_true_after_attach(tmp_path: Path) -> None:
    assert is_enable_pending(tmp_path, "bob") is False
    on_attach(tmp_path, "bob")
    assert is_enable_pending(tmp_path, "bob") is True


def test_clear_enable_pending_removes_marker(tmp_path: Path) -> None:
    on_attach(tmp_path, "bob")
    assert clear_enable_pending(tmp_path, "bob") is True
    assert is_enable_pending(tmp_path, "bob") is False
    # Idempotent: second clear is a no-op (returns False because nothing
    # was actually removed, not an error).
    assert clear_enable_pending(tmp_path, "bob") is False


def test_separate_users_have_separate_markers(tmp_path: Path) -> None:
    """Bundle D's per-user isolation invariant: per-user markers don't leak
    across users."""
    on_attach(tmp_path, "alice")
    on_attach(tmp_path, "bob")
    assert is_enable_pending(tmp_path, "alice") is True
    assert is_enable_pending(tmp_path, "bob") is True
    clear_enable_pending(tmp_path, "alice")
    # Clearing alice must not touch bob.
    assert is_enable_pending(tmp_path, "alice") is False
    assert is_enable_pending(tmp_path, "bob") is True
