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
    monkeypatch.setattr(G, "is_attached", lambda t, **kw: False)
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


# --- T-0864: is_attached is PANE/WINDOW-scoped, not tmux-SESSION-scoped -------
#
# THE FAKE TMUX BELOW IS THE INSTRUMENT, so its behaviour is not invented — every
# response shape it produces was MEASURED against real tmux 3.4 on a throwaway
# server (`tmux -L t0864`) on 2026-08-11, with a real client attached via
# `tmux -L outer new-session -d 'tmux -L t0864 attach -t proj'`:
#
#   # the bug itself — client viewing w1 (@0), %1 is a pane in w2 (@1):
#   $ tmux list-clients -t %1
#   /dev/pts/21: proj [200x50 tmux-256color] (attached,focused,UTF-8)   rc=0
#     ^ the OLD implementation read this as "someone is watching %1". They are not.
#
#   # per-client current window — tracks the client, verified by switching it:
#   $ tmux list-clients -F '#{client_session} #{window_id}'
#   proj @0        (client in w1)   →   proj @1   after `select-window -t proj:w2`
#
#   # target → window resolution, and the unknown-target shape:
#   $ tmux display-message -p -t %3 -F '#{window_id}'   → "@1"   rc=0
#   $ tmux display-message -p -t %99 -F '#{window_id}'  → ""     rc=0  (!)
#   $ tmux display-message -p -t nosuchsess -F '#{window_id}' → "" rc=0
#   $ tmux -L nosuchserver list-clients -F '#{window_id}'
#     error connecting to /tmp/tmux-1000/nosuchserver ...              rc=1
#
# Note the rc=0-with-empty-output form for an unknown target: tmux 3.4 does NOT
# exit non-zero there, so "target gone" has to be read off empty stdout.

class _FakeTmux:
    """A tmux server model: panes live in windows, windows live in sessions, and
    each attached client displays exactly ONE window of its session.

    Serves the two read-only verbs :func:`G.is_attached` uses AND the
    ``list-clients -t <target>`` the pre-T-0864 implementation used, with tmux's
    real session-widening semantics for the latter — so the same fake can run
    both implementations and the regression test genuinely discriminates.
    """

    def __init__(self, panes, clients):
        # panes: {pane_id: (session_name, window_id)}
        # clients: [(client_name, session_name, current_window_id)]
        self.panes = dict(panes)
        self.clients = list(clients)
        self.calls: list[list[str]] = []

    def _window_of(self, target):
        if target in self.panes:
            return self.panes[target][1]
        # a session-name target resolves to that session's current window, i.e.
        # the window its client displays (or its first window when detached)
        for _name, sess, win in self.clients:
            if sess == target:
                return win
        for _pid, (sess, win) in self.panes.items():
            if sess == target:
                return win
        return ""  # unknown target → rc 0, empty stdout (measured, tmux 3.4)

    def _session_of(self, target):
        if target in self.panes:
            return self.panes[target][0]
        return target

    def run(self, cmd, **kw):
        assert cmd[0] == "tmux"
        self.calls.append(cmd)
        verb = cmd[1]
        if verb == "display-message":
            return types.SimpleNamespace(
                returncode=0, stdout=self._window_of(cmd[cmd.index("-t") + 1]))
        if verb == "list-clients":
            if "-t" in cmd:  # the OLD, session-widening call
                sess = self._session_of(cmd[cmd.index("-t") + 1])
                rows = [f"{n}: {s} [80x24] (attached)"
                        for n, s, _w in self.clients if s == sess]
                return types.SimpleNamespace(returncode=0, stdout="".join(
                    r + "\n" for r in rows))
            return types.SimpleNamespace(returncode=0, stdout="".join(
                f"{w}\n" for _n, _s, w in self.clients))
        raise AssertionError(f"unexpected tmux verb: {verb}")


def _one_project_two_sessions(client_window="@1"):
    """The exact live shape: ONE tmux session per project, one window per
    bot-squad session, a human attached to one of them."""
    fake = _FakeTmux(
        panes={"%1": ("watchrobot", "@1"),   # pane A — the human is here
               "%2": ("watchrobot", "@2"),   # pane B — nobody is watching
               "%3": ("watchrobot", "@3")},
        clients=[("/dev/pts/21", "watchrobot", client_window)],
    )
    return fake


def test_is_attached_no_target_is_false():
    assert G.is_attached(None) is False
    assert G.is_attached("") is False


def test_is_attached_true_for_the_window_the_client_is_viewing(monkeypatch):
    fake = _one_project_two_sessions()
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert G.is_attached("%1") is True


def test_is_attached_false_for_a_sibling_pane_in_the_same_tmux_session(monkeypatch):
    """T-0864, THE regression: this is the case that silently passed before.

    Pane A (%1) and pane B (%2) are two bot-squad sessions of ONE project, so
    they share one tmux session. A human is attached and viewing A's window.
    The old `tmux list-clients -t %2` returned A's client and froze
    autocompact/idle_timeout/recovery for B — and for every other session of
    the project — for as long as the human sat there.
    """
    fake = _one_project_two_sessions()
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert G.is_attached("%2") is False
    assert G.is_attached("%3") is False
    # and the fix is not "always False": A is still correctly protected
    assert G.is_attached("%1") is True
    # it must not have asked the session-widening question at all
    assert not any("list-clients" in c and "-t" in c for c in fake.calls)


def test_is_attached_follows_the_client_when_it_switches_window(monkeypatch):
    """Same fleet, human now looking at B instead of A — the verdicts swap."""
    fake = _one_project_two_sessions(client_window="@2")
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert G.is_attached("%1") is False
    assert G.is_attached("%2") is True


def test_is_attached_true_for_a_non_active_pane_of_a_displayed_window(monkeypatch):
    """WINDOW-level on purpose: a split window shows all of its panes at once,
    so the human really is watching the inactive pane too."""
    fake = _FakeTmux(panes={"%1": ("watchrobot", "@1"),
                            "%4": ("watchrobot", "@1")},  # split of the same window
                     clients=[("/dev/pts/21", "watchrobot", "@1")])
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert G.is_attached("%4") is True


def test_is_attached_ignores_clients_on_other_tmux_sessions(monkeypatch):
    fake = _FakeTmux(panes={"%1": ("bot-squad", "@1")},
                     clients=[("/dev/pts/9", "watchrobot", "@7")])
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert G.is_attached("%1") is False


def test_is_attached_false_when_no_client(monkeypatch):
    fake = _FakeTmux(panes={"%1": ("watchrobot", "@1")}, clients=[])
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert G.is_attached("%1") is False


def test_is_attached_false_when_target_gone(monkeypatch):
    # tmux 3.4 exits 0 and prints NOTHING for an unknown pane id. A dead pane
    # can have no one watching it, and failing closed here would wedge the
    # recovery path whose whole job is cleaning dead panes up.
    fake = _one_project_two_sessions()
    monkeypatch.setattr(subprocess, "run", fake.run)
    assert G.is_attached("%99") is False


def test_is_attached_false_on_nonzero_exit(monkeypatch):
    def _fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert G.is_attached("%9") is False


def test_is_attached_false_when_no_tmux_server(monkeypatch):
    def _fake_run(cmd, **kw):
        if cmd[1] == "display-message":
            return types.SimpleNamespace(returncode=0, stdout="@1\n")
        return types.SimpleNamespace(returncode=1, stdout="")  # no server
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


def test_is_attached_fails_closed_when_only_list_clients_dies(monkeypatch):
    """The window resolved fine, then tmux hiccuped — still fail closed."""
    def _half_boom(cmd, **kw):
        if cmd[1] == "display-message":
            return types.SimpleNamespace(returncode=0, stdout="@1\n")
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(subprocess, "run", _half_boom)
    assert G.is_attached("%9") is True


# --- T-0864 DoD 3: debounced skip logging on both silent gates ----------------

def test_is_attached_logs_a_debounced_skip_line(monkeypatch, caplog):
    G._last_skip_log.clear()
    fake = _one_project_two_sessions()
    monkeypatch.setattr(subprocess, "run", fake.run)
    caplog.set_level("INFO")
    assert G.is_attached("%1", sid="S-alpha", now=1000.0) is True
    assert G.is_attached("%1", sid="S-alpha", now=1000.1) is True  # same tick
    assert G.is_attached("%1", sid="S-alpha", now=1035.0) is True  # window elapsed
    lines = [r.getMessage() for r in caplog.records
             if "S-alpha" in r.getMessage()]
    assert len(lines) == 2
    assert "@1" in lines[0]  # names the window a client is holding


def test_attach_skip_log_does_not_suppress_the_allowlist_line(monkeypatch, caplog):
    """Namespaced debounce keys: the three gates must not silence each other, or
    the log stops answering "which gate held this session up"."""
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    G._last_skip_log.clear()
    fake = _one_project_two_sessions()
    monkeypatch.setattr(subprocess, "run", fake.run)
    caplog.set_level("INFO")
    assert G.is_attached("%1", sid="other-project", now=1000.0) is True
    assert G.project_allowed(_cfg(), "other-project", now=1000.0) is False
    assert len([r for r in caplog.records if "non-allowlisted" in r.message]) == 1


def test_should_log_skip_prunes_stale_keys():
    G._last_skip_log.clear()
    for i in range(G._SKIP_LOG_MAX_KEYS + 5):
        G.should_log_skip(f"attached:S-{i}", now=1000.0 + i * 60)  # each past the window
    assert len(G._last_skip_log) < G._SKIP_LOG_MAX_KEYS + 5


# --- combined gate ------------------------------------------------------------

def test_recycle_allowed_combines_all_three(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    monkeypatch.setattr(G, "is_attached", lambda t, **kw: False)
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
    monkeypatch.setattr(G, "is_attached", lambda t, **kw: True)
    cfg = _cfg()
    assert G.recycle_allowed(cfg, slug="bot-squad", role="dev",
                            tmux_target="%1", now=1000.0) is False
