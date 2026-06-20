"""T-0287: coalesce concurrent worker restarts (trailing-edge debounce).

N near-simultaneous deploys each launch a detached worker-restart. Each claims a
shared marker with its own token, waits the debounce window, then restarts ONLY
if its token is still the latest — a later deploy's claim supersedes it (and that
later restart picks up all synced code). So a burst collapses to exactly ONE
restart, always covering the most-recently-synced code.
"""
from __future__ import annotations

from bot_squad_worker.deploy import _coalesce_write, _coalesce_winner


def test_single_claim_is_winner(tmp_path):
    m = tmp_path / "_worker" / "restart_coalesce.token"
    _coalesce_write(str(m), "qA")
    assert _coalesce_winner(str(m), "qA") is True


def test_last_writer_wins_others_skip(tmp_path):
    m = tmp_path / "_worker" / "restart_coalesce.token"
    _coalesce_write(str(m), "qA")
    _coalesce_write(str(m), "qB")
    _coalesce_write(str(m), "qC")
    # exactly one winner — the last claimant
    assert _coalesce_winner(str(m), "qA") is False
    assert _coalesce_winner(str(m), "qB") is False
    assert _coalesce_winner(str(m), "qC") is True


def test_missing_marker_is_not_winner(tmp_path):
    m = tmp_path / "_worker" / "nope.token"
    assert _coalesce_winner(str(m), "qX") is False


def test_write_is_atomic_replace(tmp_path):
    # overwriting an existing marker leaves exactly the new token (no partial)
    m = tmp_path / "_worker" / "restart_coalesce.token"
    _coalesce_write(str(m), "first")
    _coalesce_write(str(m), "second")
    assert m.read_text().strip() == "second"


def test_restart_script_includes_coalesce_when_marker_given(tmp_path):
    from bot_squad_worker.deploy import _build_worker_restart_script
    script = _build_worker_restart_script(
        tmp_path / "worker", tmp_path / "pip", tmp_path / "w.sock",
        tmp_path / "r.log", tmp_path / "r.FAIL", "svc.service", 5, 10,
        coalesce_marker=tmp_path / "_worker" / "restart_coalesce.token",
        token="q42",
    )
    assert "_coalesce_write" in script
    assert "_coalesce_winner" in script
    assert "COALESCED" in script
    assert "q42" in script


def test_restart_script_plain_sleep_without_marker(tmp_path):
    from bot_squad_worker.deploy import _build_worker_restart_script
    script = _build_worker_restart_script(
        tmp_path / "worker", tmp_path / "pip", tmp_path / "w.sock",
        tmp_path / "r.log", tmp_path / "r.FAIL", "svc.service", 5, 10,
    )
    assert "_coalesce" not in script
    assert "sleep 5" in script
