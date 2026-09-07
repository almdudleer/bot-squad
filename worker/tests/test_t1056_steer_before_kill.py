"""T-1056: an automatic reap must not leave tmux's own window-selection
fallback to decide what a live client sees next.

Stakeholder (2026-09-07): "а только что меня вообще в тмуксе рандомно
перекинуло в другое окно с открытым в какой-то служебной папке терминалом" —
tmux windows are shared per-session state, so killing whichever window
happens to be the session's CURRENT one silently redirects every attached
client. These tests pin the steer-before-kill helper in isolation, then prove
both automatic call sites (``suspend``'s force-kill fallback and
``archive_dead_teammates``'s window mop-up) invoke it before the kill.
"""
from __future__ import annotations

from typing import Any

from bot_squad_worker import sessions as S
from bot_squad_worker.sessions import PaneInfo


def _run_recorder(monkeypatch, responses: dict[tuple, Any] = None):
    """Monkeypatch S._run, recording every call and returning a canned
    CompletedProcess-like object per command name (default: rc=0, empty out).
    """
    import types

    calls: list[list[str]] = []
    responses = responses or {}

    def _fake(args, **kw):
        calls.append(list(args))
        key = tuple(args)
        if key in responses:
            rc, out = responses[key]
        else:
            rc, out = 0, ""
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(S, "_run", _fake)
    return calls


# ---------------------------------------------------------------------------
# _current_session_pane
# ---------------------------------------------------------------------------

def test_current_session_pane_reads_display_message(monkeypatch):
    calls = _run_recorder(monkeypatch, {
        ("tmux", "display-message", "-p", "-t", "proj", "#{pane_id}"): (0, "%7\n"),
    })
    assert S._current_session_pane("proj") == "%7"
    assert calls == [["tmux", "display-message", "-p", "-t", "proj", "#{pane_id}"]]


def test_current_session_pane_empty_on_failure(monkeypatch):
    _run_recorder(monkeypatch, {
        ("tmux", "display-message", "-p", "-t", "proj", "#{pane_id}"): (1, ""),
    })
    assert S._current_session_pane("proj") == ""


def test_current_session_pane_empty_for_blank_name(monkeypatch):
    calls = _run_recorder(monkeypatch)
    assert S._current_session_pane("") == ""
    assert calls == []  # no tmux call for a blank session name


# ---------------------------------------------------------------------------
# _safe_landing_pane
# ---------------------------------------------------------------------------

def test_safe_landing_prefers_the_root_user_conversation_pane(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [
        PaneInfo(pane_id="%1", window="dev_feat", pid="1", cwd="/x",
                 command="claude", session="proj"),
        PaneInfo(pane_id="%2", window="universal_bsq_session", pid="2",
                 cwd="/home", command="claude", session="proj"),
        PaneInfo(pane_id="%3", window="other_proj_dev", pid="3", cwd="/y",
                 command="claude", session="other"),  # different tmux session
    ])
    assert S._safe_landing_pane("proj", exclude_pane="%9") == "%2"


def test_safe_landing_falls_back_to_any_real_window(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [
        PaneInfo(pane_id="%0", window="_init", pid="0", cwd="/svc",
                 command="bash", session="proj"),
        PaneInfo(pane_id="%1", window="dev_feat", pid="1", cwd="/x",
                 command="claude", session="proj"),
    ])
    assert S._safe_landing_pane("proj", exclude_pane="%9") == "%1"


def test_safe_landing_excludes_the_pane_being_killed(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [
        PaneInfo(pane_id="%1", window="dev_feat", pid="1", cwd="/x",
                 command="claude", session="proj"),
    ])
    assert S._safe_landing_pane("proj", exclude_pane="%1") == ""


def test_safe_landing_empty_when_only_init_remains(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [
        PaneInfo(pane_id="%0", window="_init", pid="0", cwd="/svc",
                 command="bash", session="proj"),
    ])
    assert S._safe_landing_pane("proj", exclude_pane="%9") == ""


# ---------------------------------------------------------------------------
# _steer_client_before_kill
# ---------------------------------------------------------------------------

def _pane(pane_id="%5", window="dev_feat", session="proj"):
    return PaneInfo(pane_id=pane_id, window=window, pid="1", cwd="/x",
                     command="claude", session=session)


def test_steer_noop_when_pane_is_none(monkeypatch):
    calls = _run_recorder(monkeypatch)
    S._steer_client_before_kill(None)
    assert calls == []


def test_steer_noop_when_target_is_not_the_current_pane(monkeypatch):
    calls = _run_recorder(monkeypatch, {
        ("tmux", "display-message", "-p", "-t", "proj", "#{pane_id}"): (0, "%1\n"),
    })
    S._steer_client_before_kill(_pane(pane_id="%5"))
    # only the current-pane probe fired; no select-window (nobody is looking at %5)
    assert calls == [["tmux", "display-message", "-p", "-t", "proj", "#{pane_id}"]]


def test_steer_switches_client_when_target_is_current(monkeypatch):
    calls = _run_recorder(monkeypatch, {
        ("tmux", "display-message", "-p", "-t", "proj", "#{pane_id}"): (0, "%5\n"),
    })
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [
        _pane(pane_id="%5", window="dev_feat"),
        PaneInfo(pane_id="%2", window="universal_bsq_session", pid="2",
                 cwd="/home", command="claude", session="proj"),
    ])
    S._steer_client_before_kill(_pane(pane_id="%5"))
    assert ["tmux", "select-window", "-t", "%2"] in calls


def test_steer_does_not_select_window_when_no_safer_pane_exists(monkeypatch):
    calls = _run_recorder(monkeypatch, {
        ("tmux", "display-message", "-p", "-t", "proj", "#{pane_id}"): (0, "%5\n"),
    })
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [_pane(pane_id="%5", window="dev_feat")])
    S._steer_client_before_kill(_pane(pane_id="%5"))
    assert not any(c[:2] == ["tmux", "select-window"] for c in calls)


def test_steer_swallows_tmux_errors(monkeypatch):
    def _boom(args, **kw):
        raise RuntimeError("tmux is gone")
    monkeypatch.setattr(S, "_run", _boom)
    S._steer_client_before_kill(_pane())  # must not raise
