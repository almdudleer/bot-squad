"""T-0563 (per-project allowlist) + T-0564 (user/attached exemption) — the
shared gate consulted by all three recycle paths (autocompact / idle_timeout /
recovery). See bot_squad_worker.recycle_gate.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from bot_squad_worker import recycle_gate as G


def _cfg(**kw):
    return types.SimpleNamespace(**kw)


# --- T-0563: per-project allowlist -------------------------------------------

def test_default_allowlist_is_bot_squad_and_watchrobot(monkeypatch):
    # T-0613: watchrobot rides recycle-v2 by default (the T-0612 gate wants it
    # covered BEFORE its operator program spawns); any OTHER project still
    # needs an explicit opt-in.
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    cfg = _cfg()  # no recycle_projects attr at all
    assert G.recycle_allowlist(cfg) == ("bot-squad", "watchrobot")
    assert G.project_allowed(cfg, "bot-squad", now=1000.0) is True
    assert G.project_allowed(cfg, "watchrobot", now=1000.0) is True
    assert G.project_allowed(cfg, "some-new-project", now=1000.0) is False


def test_config_projects_extend_the_allowlist(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    cfg = _cfg(recycle_projects=("bot-squad", "watchrobot"))
    assert G.project_allowed(cfg, "watchrobot", now=1000.0) is True


def test_env_override_wins_over_config(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "watchrobot, signal-tracker")
    cfg = _cfg(recycle_projects=("bot-squad",))  # config would exclude watchrobot
    assert G.recycle_allowlist(cfg) == ("watchrobot", "signal-tracker")
    assert G.project_allowed(cfg, "watchrobot", now=1000.0) is True
    assert G.project_allowed(cfg, "bot-squad", now=1000.0) is False  # env wins, excludes it


def test_env_blank_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "   ")
    cfg = _cfg(recycle_projects=("bot-squad",))
    assert G.recycle_allowlist(cfg) == ("bot-squad",)


def test_skip_log_debounced_per_slug(monkeypatch, caplog):
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    G._last_skip_log.clear()
    cfg = _cfg()
    caplog.set_level("INFO")
    assert G.project_allowed(cfg, "other-project", now=1000.0) is False
    assert G.project_allowed(cfg, "other-project", now=1000.1) is False  # same tick, no re-log
    assert G.project_allowed(cfg, "other-project", now=1035.0) is False  # window elapsed, re-logs
    lines = [r.message for r in caplog.records if "other-project" in r.message]
    assert len(lines) == 2


# --- T-0564: role + attached exemption ---------------------------------------

def test_role_exempt_only_user_conversation():
    assert G.role_exempt("user-conversation") is True
    assert G.role_exempt("dev") is False
    assert G.role_exempt("operator") is False
    assert G.role_exempt(None) is False
    assert G.role_exempt("") is False


# --- T-0616: hand-launched user-session exemption -----------------------------

def test_user_session_exempt_role_signal_unchanged():
    assert G.user_session_exempt(role="user-conversation") is True
    assert G.user_session_exempt(role="dev") is False
    assert G.user_session_exempt() is False


def test_user_session_exempt_window_marker():
    # p8's exact shape: window `user-session` derives role dev (T-0175
    # default) — the D-0053 §4 hole. The window segment must exempt it.
    assert G.user_session_exempt(role="dev", window="user-session") is True
    assert G.user_session_exempt(role="dev", window="user-session-2") is True
    assert G.user_session_exempt(role="dev", window="gu_12ab-user-session") is True
    assert G.user_session_exempt(role="dev", window="USER-SESSION") is True
    # segment-anchored: no partial-word / lookalike matches
    assert G.user_session_exempt(role="dev", window="user-sessions") is False
    assert G.user_session_exempt(role="dev", window="user-feedback") is False  # constant-team window
    assert G.user_session_exempt(role="dev", window="somework") is False
    assert G.user_session_exempt(role="dev", window="") is False


def test_user_session_exempt_md_marker():
    # ad-hoc window names can't be recognised — the explicit md stamp covers
    # them (the SessionStart hook preserves it since T-0616).
    assert G.user_session_exempt(role="dev", window="adhoc",
                                 meta={"recycle_exempt": True}) is True
    assert G.user_session_exempt(role="dev", window="adhoc",
                                 meta={"recycle_exempt": "true"}) is True
    assert G.user_session_exempt(role="dev", window="adhoc",
                                 meta={"recycle_exempt": False}) is False
    assert G.user_session_exempt(role="dev", window="adhoc",
                                 meta={"recycle_exempt": "~"}) is False
    assert G.user_session_exempt(role="dev", window="adhoc", meta={}) is False
    assert G.user_session_exempt(role="dev", window="adhoc", meta=None) is False


def test_recycle_allowed_blocks_hand_launched_user_session(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    monkeypatch.setattr(G, "is_attached", lambda t: False)
    cfg = _cfg()
    # window signal — even though the derived role is dev
    assert G.recycle_allowed(cfg, slug="bot-squad", role="dev",
                             tmux_target="%1", now=1000.0,
                             window="user-session") is False
    # md marker signal
    assert G.recycle_allowed(cfg, slug="bot-squad", role="dev",
                             tmux_target="%1", now=1000.0, window="adhoc",
                             meta={"recycle_exempt": True}) is False
    # control: a plain dev window without the marker is still recyclable —
    # the exemption is precise, not a blanket recycle-off
    assert G.recycle_allowed(cfg, slug="bot-squad", role="dev",
                             tmux_target="%1", now=1000.0, window="adhoc",
                             meta={}) is True


# --- T-0655: operator drive=on/off predicate ---------------------------------

def test_operator_drive_on_defaults_true_when_unset():
    # Absent drive field on an operator session reads as ON — "runs non-stop
    # while work is on" is the default, not an opt-in.
    assert G.operator_drive_on(role="operator") is True
    assert G.operator_drive_on(role="operator", meta={}) is True
    assert G.operator_drive_on(role="operator", meta={"drive": "on"}) is True


def test_operator_drive_off_only_when_explicit():
    assert G.operator_drive_on(role="operator", meta={"drive": "off"}) is False
    assert G.operator_drive_on(role="operator", meta={"drive": "OFF"}) is False
    assert G.operator_drive_on(role="operator", meta={"drive": " Off "}) is False
    # garbage/unexpected values fail SAFE toward "keep it alive", not toward
    # "recycle it" — only the literal off turns it off.
    assert G.operator_drive_on(role="operator", meta={"drive": "banana"}) is True


def test_operator_drive_on_never_true_for_other_roles():
    # The predicate only ever applies to the operator role — dev/TL/
    # user-conversation sessions are untouched by this mechanism regardless
    # of what a `drive` field might (incorrectly) carry.
    assert G.operator_drive_on(role="dev", meta={"drive": "on"}) is False
    assert G.operator_drive_on(role="teamlead", meta={"drive": "on"}) is False
    assert G.operator_drive_on(role="user-conversation", meta={"drive": "on"}) is False
    assert G.operator_drive_on(role=None) is False
    assert G.operator_drive_on() is False


def test_is_attached_no_target_is_false():
    assert G.is_attached(None) is False
    assert G.is_attached("") is False


def test_is_attached_true_when_client_present(monkeypatch):
    def _fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout="/dev/pts/3: main [80x24]\n")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert G.is_attached("%9") is True


def test_is_attached_false_when_no_client(monkeypatch):
    def _fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert G.is_attached("%9") is False


def test_is_attached_false_when_target_gone(monkeypatch):
    def _fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert G.is_attached("%9") is False


def test_is_attached_fails_closed_on_subprocess_error(monkeypatch):
    def _boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert G.is_attached("%9") is True  # fail-closed: safer to skip than to kill


def test_is_attached_fails_closed_on_oserror(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("tmux not found")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert G.is_attached("%9") is True


# --- combined gate ------------------------------------------------------------

def test_recycle_allowed_combines_all_three(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    monkeypatch.setattr(G, "is_attached", lambda t: False)
    cfg = _cfg()
    assert G.recycle_allowed(cfg, slug="bot-squad", role="dev",
                            tmux_target="%1", now=1000.0) is True
    # watchrobot joined the default allowlist (T-0613)
    assert G.recycle_allowed(cfg, slug="watchrobot", role="dev",
                            tmux_target="%1", now=1000.0) is True
    # non-allowlisted project
    assert G.recycle_allowed(cfg, slug="some-new-project", role="dev",
                            tmux_target="%1", now=1000.0) is False
    # user-conversation role, even in an allowlisted project
    assert G.recycle_allowed(cfg, slug="bot-squad", role="user-conversation",
                            tmux_target="%1", now=1000.0) is False


def test_recycle_allowed_false_when_attached(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    monkeypatch.setattr(G, "is_attached", lambda t: True)
    cfg = _cfg()
    assert G.recycle_allowed(cfg, slug="bot-squad", role="dev",
                            tmux_target="%1", now=1000.0) is False
