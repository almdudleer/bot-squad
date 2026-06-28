"""T-0470 / M1-F1.7 — hook-driven lifecycle measurement.

Covers the two halves of the unified surface:
  * HOOK SIGNALS — the per-SID Stop/UserPromptSubmit markers the Claude hooks
    write, and ``hook_idle_age`` (the stall clock the timeout decision reads).
  * ENGINE EVENTS — ``emit`` / ``read_events`` / ``summarize`` / ``reap_events``
    (the session_timeout / session_recycled records, for operator measurement).
"""
from __future__ import annotations

import time
import types
from pathlib import Path

from bot_squad_worker import lifecycle_events as L


# --- hook markers / stall clock --------------------------------------------

def test_marker_path_shape_and_sid_sanitised(tmp_path):
    p = L.marker_path(str(tmp_path), "S-u-w-p5", L.MARKER_STOP)
    assert p == tmp_path / ".claude" / "bsq_lifecycle" / "S-u-w-p5.stop"
    # a slash in a pathological SID can't escape the marker dir
    p2 = L.marker_path(str(tmp_path), "a/b", L.MARKER_ACTIVE)
    assert p2.parent == tmp_path / ".claude" / "bsq_lifecycle"
    assert p2.name == "a_b.active"


def test_touch_marker_creates_dir_and_file(tmp_path):
    out = L.touch_marker(str(tmp_path), "S-u-w-p5", L.MARKER_STOP)
    assert out is not None and out.exists()
    assert out == L.marker_path(str(tmp_path), "S-u-w-p5", L.MARKER_STOP)
    # empty cwd / sid is a no-op (best-effort, never raises)
    assert L.touch_marker("", "S-u-w-p5", L.MARKER_STOP) is None
    assert L.touch_marker(str(tmp_path), "", L.MARKER_STOP) is None


def test_hook_idle_age_none_when_no_marker(tmp_path):
    # no Stop marker yet → None (caller falls back to the jsonl mtime)
    assert L.hook_idle_age(str(tmp_path), "S-u-w-p5", time.time()) is None
    assert L.hook_idle_age("", "S-u-w-p5", time.time()) is None


def test_hook_idle_age_stall_from_stop(tmp_path):
    sid = "S-u-w-p5"
    L.touch_marker(str(tmp_path), sid, L.MARKER_STOP)
    # backdate the Stop marker by 120s → stall clock reads ~120s
    stop = L.marker_path(str(tmp_path), sid, L.MARKER_STOP)
    anchor = time.time() - 120
    import os
    os.utime(stop, (anchor, anchor))
    age = L.hook_idle_age(str(tmp_path), sid, time.time())
    assert age is not None and 118 <= age <= 122


def test_hook_idle_age_zero_when_active_newer(tmp_path):
    sid = "S-u-w-p5"
    import os
    L.touch_marker(str(tmp_path), sid, L.MARKER_STOP)
    stop = L.marker_path(str(tmp_path), sid, L.MARKER_STOP)
    old = time.time() - 120
    os.utime(stop, (old, old))
    # a NEWER .active marker means a turn is in progress → busy → 0.0
    L.touch_marker(str(tmp_path), sid, L.MARKER_ACTIVE)
    assert L.hook_idle_age(str(tmp_path), sid, time.time()) == 0.0


def test_hook_idle_age_stale_active_does_not_reset(tmp_path):
    """A .active OLDER than .stop (turn ended after the prompt) → still idle."""
    sid = "S-u-w-p5"
    import os
    L.touch_marker(str(tmp_path), sid, L.MARKER_ACTIVE)
    active = L.marker_path(str(tmp_path), sid, L.MARKER_ACTIVE)
    old = time.time() - 300
    os.utime(active, (old, old))
    L.touch_marker(str(tmp_path), sid, L.MARKER_STOP)
    stop = L.marker_path(str(tmp_path), sid, L.MARKER_STOP)
    anchor = time.time() - 100
    os.utime(stop, (anchor, anchor))
    age = L.hook_idle_age(str(tmp_path), sid, time.time())
    assert age is not None and 98 <= age <= 102


# --- engine events ----------------------------------------------------------

def _cfg(tmp_path):
    return types.SimpleNamespace(data_dir=tmp_path / "data", projects={})


def test_emit_records_last_counts_history(tmp_path):
    cfg = _cfg(tmp_path)
    sid = "S-u-w-p5"
    assert L.emit(cfg, "bot-squad", sid, L.SESSION_TIMEOUT, now=1000.0,
                  reason="idle_window") is True
    assert L.emit(cfg, "bot-squad", sid, L.SESSION_RECYCLED, now=1100.0,
                  cause="idle_timeout") is True
    assert L.emit(cfg, "bot-squad", sid, L.SESSION_RECYCLED, now=1200.0,
                  cause="idle_timeout") is True
    doc = L.read_events(cfg, "bot-squad", sid)
    assert doc["sid"] == sid
    assert doc["counts"] == {L.SESSION_TIMEOUT: 1, L.SESSION_RECYCLED: 2}
    # last carries the newest per kind + the extra fields
    assert doc["last"][L.SESSION_TIMEOUT]["reason"] == "idle_window"
    assert doc["last"][L.SESSION_RECYCLED]["epoch"] == 1200.0
    # history is most-recent-first
    assert [e["event"] for e in doc["history"]] == [
        L.SESSION_RECYCLED, L.SESSION_RECYCLED, L.SESSION_TIMEOUT]


def test_emit_history_capped(tmp_path):
    cfg = _cfg(tmp_path)
    sid = "S-u-w-p5"
    for i in range(L._HISTORY_CAP + 10):
        L.emit(cfg, "bot-squad", sid, L.SESSION_TIMEOUT, now=float(i))
    doc = L.read_events(cfg, "bot-squad", sid)
    assert len(doc["history"]) == L._HISTORY_CAP
    assert doc["counts"][L.SESSION_TIMEOUT] == L._HISTORY_CAP + 10  # tally not capped


def test_emit_noop_on_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert L.emit(cfg, "bot-squad", "", L.SESSION_TIMEOUT) is False
    assert L.emit(cfg, "bot-squad", "S-x", "") is False


def test_summarize_and_reap(tmp_path):
    cfg = _cfg(tmp_path)
    L.emit(cfg, "bot-squad", "S-a", L.SESSION_TIMEOUT, now=1.0)
    L.emit(cfg, "bot-squad", "S-b", L.SESSION_RECYCLED, now=2.0)
    summ = L.summarize(cfg, "bot-squad")
    assert set(summ) == {"S-a", "S-b"}
    assert summ["S-a"]["counts"] == {L.SESSION_TIMEOUT: 1}
    assert summ["S-b"]["last"][L.SESSION_RECYCLED]["epoch"] == 2.0
    # reap removes one doc; summarize then drops it
    removed = L.reap_events(cfg, "bot-squad", "S-a")
    assert removed is not None and not removed.exists()
    assert set(L.summarize(cfg, "bot-squad")) == {"S-b"}
    # reap of a missing doc is a no-op
    assert L.reap_events(cfg, "bot-squad", "S-a") is None


def test_summarize_empty_when_no_dir(tmp_path):
    assert L.summarize(_cfg(tmp_path), "bot-squad") == {}
