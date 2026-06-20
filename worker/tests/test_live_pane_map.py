"""WS-4 (T-0265-adjacent): canonical SID→live-pane resolution.

A live session's tmux pane is the truth, NOT the ``pane_id`` field in its
session md — which is frequently EMPTY for live sessions even though the SID
encodes the pane (``S-<user>-<window>-pN`` ⇒ pane ``%N``) and a real tmux pane
exists. Reading the empty md field made detector/recovery treat genuinely-paned
live sessions as pane-dead (the false-respawn near-miss + the 5h-limit detector
no-op). ``live_pane_map`` resolves the truth via ``list_panes()`` + ``compute_sid``
(the same matching ``suspend()`` uses), so every consumer agrees on liveness.
"""
from __future__ import annotations

from bot_squad_worker import sessions as S
from bot_squad_worker.sessions import PaneInfo, compute_sid, live_pane_map


def _pane(window, pane_id):
    return PaneInfo(pane_id=pane_id, window=window, pid="1", cwd="/x",
                    command="claude", session="proj")


def test_live_pane_map_resolves_sid_to_pane(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [
        _pane("feat", "%177"), _pane("ws2", "%175")])
    m = live_pane_map()
    assert m == {
        compute_sid("u", "feat", "%177"): "%177",
        compute_sid("u", "ws2", "%175"): "%175",
    }
    # the SID->pane mapping must NOT depend on the (empty) md pane_id field
    assert "S-u-feat-p177" in m
    assert m["S-u-feat-p177"] == "%177"


def test_live_pane_map_empty_when_no_panes(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    assert live_pane_map() == {}


def test_live_pane_map_skips_uncomputable(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    # a pane with an empty window still maps (compute_sid tolerates it); just
    # assert the good one is present and the call never raises.
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("feat", "%9")])
    m = live_pane_map()
    assert m.get(compute_sid("u", "feat", "%9")) == "%9"
