"""T-0265: read_telemetry liveness filter (kill ghost/stale sessions).

read_telemetry's docstring claimed it filters stale records to active SIDs, but
the body appended EVERY record on disk → ~33 ghosts vs ~6 live on the board.
The filter now keeps only records whose SID is in ``list_sessions`` — the board's
canonical live set (active+paused, derived from real tmux panes per T-0326), so
suspended/archived/ended ghosts and paneless 'status:active' zombies are dropped.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker import telemetry as T


def _cfg(tmp_path):
    data = tmp_path / "data"
    (data / "p1" / "_worker" / "telemetry").mkdir(parents=True)
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=data)


def _rec(cfg, sid):
    p = cfg.data_dir / "p1" / "_worker" / "telemetry" / f"{sid}.json"
    p.write_text(json.dumps({"sid": sid, "rate_limited": False,
                             "sampled_at": "2026-06-20T00:00:00Z"}))


def test_read_telemetry_filters_to_list_sessions_live(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    # records on disk for 4 sids; only 2 are live per list_sessions
    for sid in ("S-u-live-p1", "S-u-paused-p2", "S-u-ghost-p3", "S-u-gone-p9"):
        _rec(cfg, sid)
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: [
        {"sid": "S-u-live-p1", "status": "active"},
        {"sid": "S-u-paused-p2", "status": "paused"},
    ])
    out = T.read_telemetry(cfg, "p1")
    sids = {r["sid"] for r in out["sessions"]}
    assert sids == {"S-u-live-p1", "S-u-paused-p2"}
    assert "S-u-ghost-p3" not in sids   # not live → ghost dropped
    assert "S-u-gone-p9" not in sids


def test_read_telemetry_empty_when_nothing_live(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    _rec(cfg, "S-u-zombie-p7")
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: [])  # board shows none
    out = T.read_telemetry(cfg, "p1")
    assert out["sessions"] == []


def test_live_sids_tolerates_list_sessions_error(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    def _boom(cfg, slug):
        raise RuntimeError("tmux down")
    monkeypatch.setattr(S, "list_sessions", _boom)
    assert T._live_sids(cfg, "p1") == set()  # never raises into the read path
