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
