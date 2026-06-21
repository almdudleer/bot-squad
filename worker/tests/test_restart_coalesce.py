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


def _coalesce_payloads(script):
    import re
    # each `python -c "<payload>"` — payload has no inner double-quotes
    return re.findall(r'-c "([^"]*)"', script)


def test_coalesce_script_payloads_are_valid_python(tmp_path):
    """Regression: a bare path/UUID interpolated into `python -c` source is
    invalid Python (`_coalesce_write(/home/..., 1b6a39cf-...)` → SyntaxError),
    which silently broke every deploy's worker restart."""
    from bot_squad_worker.deploy import _build_worker_restart_script
    script = _build_worker_restart_script(
        tmp_path / "worker", tmp_path / "pip", tmp_path / "w.sock",
        tmp_path / "r.log", tmp_path / "r.FAIL", "svc.service", 5, 10,
        coalesce_marker=tmp_path / "_worker" / "restart_coalesce.token",
        token="1b6a39cf-21d7-4489-94eb-159bc57f72cb",  # realistic queue_id
    )
    payloads = [p for p in _coalesce_payloads(script) if "_coalesce" in p]
    assert payloads, "no python -c coalesce payloads found"
    for p in payloads:
        compile(p, "<coalesce>", "exec")  # must not raise SyntaxError


def test_coalesce_script_roundtrip_executes(tmp_path):
    """End-to-end: the generated write+winner payloads actually run and agree."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    import bot_squad_worker
    from bot_squad_worker.deploy import _build_worker_restart_script
    # T-0380: the `python -c` children import bot_squad_worker — give them a
    # PYTHONPATH to the package root so the test passes regardless of cwd (the
    # full-suite run from the repo root otherwise ModuleNotFoundError'd; it only
    # passed when pytest ran from worker/).
    _pkg_root = str(Path(bot_squad_worker.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": _pkg_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    marker = tmp_path / "_worker" / "restart_coalesce.token"
    token = "1b6a39cf-21d7-4489-94eb-159bc57f72cb"
    script = _build_worker_restart_script(
        tmp_path / "worker", tmp_path / "pip", tmp_path / "w.sock",
        tmp_path / "r.log", tmp_path / "r.FAIL", "svc.service", 5, 10,
        coalesce_marker=marker, token=token,
    )
    payloads = _coalesce_payloads(script)
    write_py = next(p for p in payloads if "_coalesce_write" in p)
    win_py = next(p for p in payloads if "_coalesce_winner" in p)
    # run write (token + marker passed as argv, never injected into source)
    subprocess.run([sys.executable, "-c", write_py, str(marker), token], check=True, env=env)
    assert marker.read_text().strip() == token
    # this token is the winner → exit 0
    assert subprocess.run([sys.executable, "-c", win_py, str(marker), token], env=env).returncode == 0
    # a stale token is NOT the winner → exit 1
    assert subprocess.run([sys.executable, "-c", win_py, str(marker), "stale"], env=env).returncode == 1


def test_restart_script_plain_sleep_without_marker(tmp_path):
    from bot_squad_worker.deploy import _build_worker_restart_script
    script = _build_worker_restart_script(
        tmp_path / "worker", tmp_path / "pip", tmp_path / "w.sock",
        tmp_path / "r.log", tmp_path / "r.FAIL", "svc.service", 5, 10,
    )
    assert "_coalesce" not in script
    assert "sleep 5" in script
