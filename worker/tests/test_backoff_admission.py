"""T-0250 (WS-4 S3): admission consults the backoff governor.

``_enforce_parallel_cap`` now refuses a spawn when live sessions reach the
EFFECTIVE concurrency (``backoff.effective_limit``), not just the hard cap.
Under pressure the governor depresses effective below the cap, so admission
queues spawns earlier — and the refusal message distinguishes a hard-cap
refusal from a backoff (pressure) refusal. Both keep the task pending/queued
(the T-0237 S4 contract), never a silent drop.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import backoff as B
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError, SpawnBackpressure
from bot_squad_worker.sessions import _enforce_parallel_cap, _write_session_metadata


def _make_cfg(tmp_path: Path, cap: int = 15) -> types.SimpleNamespace:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "system_settings.toml").write_text(
        f"[caps]\nmax_parallel_sessions = {cap}\nmax_total_tokens = 0\n"
    )
    data_dir = tmp_path / "data"
    (data_dir / "p1" / "sessions").mkdir(parents=True)
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=data_dir,
                                 config_dir=tmp_path / "config")


# SID set backing the patched ``_live_agent_sids`` (T-0397): a session counts
# toward the cap only if its SID is here (a pane with a live claude process).
_LIVE_AGENTS: set[str] = set()


def _live(cfg, sid, *, pane=True, window="w") -> None:
    meta = {"sid": sid, "status": "active", "window": window, "task_id": "~",
            "initiative": "~"}
    _write_session_metadata(cfg.data_dir / "p1" / "sessions" / f"{sid}.md", meta)
    if pane:
        _LIVE_AGENTS.add(sid)
    else:
        _LIVE_AGENTS.discard(sid)


@pytest.fixture(autouse=True)
def _no_panes(monkeypatch):
    _LIVE_AGENTS.clear()
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set(_LIVE_AGENTS))


def test_backoff_pressure_refuses_below_hard_cap(tmp_path):
    """Governor depressed effective to 3; 3 live < cap 15 but >= effective -> queue."""
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 3, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    for i in range(3):
        _live(cfg, f"S-u-x-p{i}")
    with pytest.raises(ActionError, match="backoff"):
        _enforce_parallel_cap(cfg)


def test_hard_cap_message_when_effective_at_cap(tmp_path):
    """No depression (effective == cap): the message is the hard-cap one."""
    cfg = _make_cfg(tmp_path, cap=2)  # no backoff state -> effective == cap 2
    _live(cfg, "S-u-a-p1")
    _live(cfg, "S-u-b-p2")
    with pytest.raises(ActionError, match="capacity reached"):
        _enforce_parallel_cap(cfg)


def test_under_effective_allows(tmp_path):
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 5, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    for i in range(3):  # 3 < 5
        _live(cfg, f"S-u-x-p{i}")
    _enforce_parallel_cap(cfg)  # no raise


def test_backoff_effective_excludes_coordinators(tmp_path):
    """T-0524: the AIMD effective-concurrency count also governs LEAF-DEV load.
    With effective depressed to 2, a cluster of 1 operator + 2 TLs + 1 dev must
    NOT false-full — only the single leaf dev counts (1 < 2 effective)."""
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 2, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    _live(cfg, "S-u-op-p1", window="p1-operator")
    _live(cfg, "S-u-tl-p2", window="p1-TL")
    _live(cfg, "S-u-tl-p3", window="p1-TL")
    _live(cfg, "S-u-dev-p4", window="feature-x")
    _enforce_parallel_cap(cfg)  # 1 leaf dev < 2 effective → no raise


def test_kill_switch_restores_cap_only(tmp_path, monkeypatch):
    """BOT_SQUAD_BACKOFF=0 -> ignore depressed state, admit up to the hard cap."""
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 2, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    monkeypatch.setenv("BOT_SQUAD_BACKOFF", "0")
    for i in range(5):  # 5 < cap 15, but > depressed 2
        _live(cfg, f"S-u-x-p{i}")
    _enforce_parallel_cap(cfg)  # no raise — kill-switch ignores backoff


# --- T-1007: the classification travels as a TYPE, not as message text -------
#
# Four call sites used to recover "is this backpressure?" by testing the
# exception's TEXT. The admission check has THREE refusal wordings (pace
# ceiling, hard cap, AIMD backoff) and the hard-cap branch requires a configured
# cap -- so with max_parallel_sessions unset it is unreachable and every real
# refusal carried the one wording the test did not match. Measured live
# 2026-09-06: a monitor breach was told "nobody is working on it" while the
# raiser's own text said the task stays queued and retries.
#
# These construct the exception FROM THE RAISER, never from a literal: a
# hand-typed string would re-freeze the wording this change exists to stop
# depending on.


def test_backoff_refusal_is_typed_backpressure(tmp_path):
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 3, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    for i in range(3):
        _live(cfg, f"S-u-x-p{i}")
    with pytest.raises(SpawnBackpressure) as ei:
        _enforce_parallel_cap(cfg)
    # and it is still an ActionError, so every existing `except ActionError`
    # keeps catching it -- the change cannot silently un-handle a refusal
    assert isinstance(ei.value, ActionError)


def test_hard_cap_refusal_is_typed_backpressure(tmp_path):
    cfg = _make_cfg(tmp_path, cap=2)
    _live(cfg, "S-u-a-p1")
    _live(cfg, "S-u-b-p2")
    with pytest.raises(SpawnBackpressure) as ei:
        _enforce_parallel_cap(cfg)
    assert isinstance(ei.value, ActionError)


def test_a_plain_ActionError_saying_capacity_reached_is_NOT_backpressure():
    """The arm that fails on the pre-T-1007 implementation.

    ``bind_task`` refuses with its own unrelated capacity message. The old
    substring test classified it as spawn backpressure; the type does not.
    Unreachable from the spawn callers today (``spawn`` makes no ``bind_task``
    call), so this pins a latent false positive rather than a live one -- and it
    is the arm that distinguishes a type check from a text check at all.
    """
    e = ActionError("bind_task: capacity reached (cap=1) — task stays pending")
    assert not isinstance(e, SpawnBackpressure)

# --- T-1007: the READERS, not just the raiser (gap named by p664) ------------
#
# The three tests above pin what the RAISER produces. They do not pin what the
# four readers DO with it, and p664 named the consequence: revert the readers to
# substring matching and every string-asserting test in the fleet stays green,
# because the wording is unchanged and the wording is all they check.
#
# THE LOAD-BEARING CASE IS THE NEGATIVE ONE: a refusal whose message does NOT
# contain the old substring must STILL be classified as backpressure. That is
# the case that is broken today -- with max_parallel_sessions unset the matching
# branch is unreachable, so every real refusal on this box takes this path.


def _routine_reader_classification(monkeypatch, exc):
    """Drive routines.py's reader with `exc` and return its (sid, failure)."""
    from bot_squad_worker import routines as R
    from bot_squad_worker import sessions as _S

    def _boom(*a, **kw):
        raise exc
    monkeypatch.setattr(_S, "spawn", _boom)
    ev = R.FireEvent(kind="fire", value=1, threshold=0, judge="numeric_gt",
                     breach_first_seen=None)
    rt = types.SimpleNamespace(id="R-9999", title="t", instruction="i",
                               monitor={}, file_path=Path("/dev/null"))
    monkeypatch.setattr(R, "_handler_brief", lambda *a, **kw: "brief")
    return R._spawn_routine_handler(object(), "p1", rt, ev)


def test_reader_defers_on_a_refusal_WITHOUT_the_old_substring(monkeypatch):
    """The arm that is broken before T-1007 and green after it."""
    from bot_squad_worker import routines as R
    from bot_squad_worker.actions import SpawnBackpressure
    exc = SpawnBackpressure(
        "spawn: backoff — 12/8 effective concurrency (rate-limit/usage-limit "
        "pressure; hard cap=unlimited); spawn refused, task stays QUEUED, "
        "retry on ramp-up")
    assert "capacity reached" not in str(exc)      # the whole point
    sid, failure = _routine_reader_classification(monkeypatch, exc)
    assert sid is None
    assert failure == R.SPAWN_DEFER_CAPACITY       # deferred, NOT degraded


def test_reader_still_degrades_on_a_genuine_fault(monkeypatch):
    """The silence arm: a real failure must NOT be laundered into a defer."""
    from bot_squad_worker import routines as R
    from bot_squad_worker.actions import ActionError
    sid, failure = _routine_reader_classification(
        monkeypatch, ActionError("spawn: tmux new-window failed: no server"))
    assert sid is None
    assert failure != R.SPAWN_DEFER_CAPACITY
    assert "tmux new-window failed" in failure

