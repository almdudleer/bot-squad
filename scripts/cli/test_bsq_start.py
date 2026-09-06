"""T-0963: `bsq start` — the one command a user runs.

Stakeholder: «когда по проекту их запущено 0, должна быть простая команда юзеру
bsq start, и юзеру должно быть моментально запущено агент … (или команда скажет
юзеру, куда идти, если этот агент уже работает)». Two branches, and the whole
value is that they are the ONLY two — so both are pinned here, along with the
thing that makes the second one safe: it must not spawn.

Also pinned here is T-0964's `role_window`, because its correctness is not
"does it produce a nice string" but a ROUND TRIP: every name it emits has to
derive back to the role it was asked for, through BOTH mirrors — the worker's
python `_derive_role` and the SessionStart hook's bash one. A window that reads
`…_qa` after a `--role dev` spawn hands the session someone else's contract.

`post` is stubbed — `bsq` is not a dry-run surface (every verb hits the live
worker socket), so the param/branch contract is tested here and the live
round-trip is a separate manual walkthrough.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

_REPO = Path(__file__).resolve().parents[2]
SLUG = "proj"


def _args(**kw):
    base = dict(slug=None, user=None, no_attach=True)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: SLUG)
    # Never let a test take over a terminal, whatever the TTY state is.
    monkeypatch.setattr(bsq, "_go_there", lambda target, attach: print(f"GO {target}"))


def _stub_post(monkeypatch, sessions, ensure=None):
    calls: list[tuple[str, dict]] = []
    seq = list(sessions) if isinstance(sessions, list) and sessions and isinstance(
        sessions[0], list) else [sessions, sessions]

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        if action == "list_sessions":
            n = sum(1 for a, _ in calls if a == "list_sessions") - 1
            return {"sessions": seq[min(n, len(seq) - 1)]}
        if action == "ensure_user_conversation":
            return ensure or {"ok": True, "sid": "S-u-universal_bsq_session-p7",
                              "spawned": True}
        return {"ok": True}

    monkeypatch.setattr(bsq, "post", fake_post)
    return calls


def _users(monkeypatch, users):
    monkeypatch.setattr(bsq, "_mothership_users", lambda: users)


ALEX = {"id": "gu_alex", "username": "alexey", "is_super_admin": True}
FLO = {"id": "gu_flo", "username": "flomaster", "is_super_admin": False}
TIM = {"id": "gu_tim", "username": "timpo", "is_super_admin": False}

RUNNING = {
    "sid": "S-u-universal_bsq_session-p7",
    "role": "user-conversation",
    "status": "active",
    "window": "universal_bsq_session",
    "tmux_session": "proj-29",
}


# ---------------------------------------------------------------------------
# Branch 1 — one is already running: SAY WHERE, and spawn nothing.
# ---------------------------------------------------------------------------

def test_running_session_is_reported_and_nothing_is_spawned(monkeypatch, capsys):
    calls = _stub_post(monkeypatch, [RUNNING])
    bsq.cmd_start(_args())
    out = capsys.readouterr().out
    assert "already running" in out
    assert "universal_bsq_session" in out
    assert "GO proj-29:universal_bsq_session" in out
    # The load-bearing half: no launch on this branch.
    assert [a for a, _ in calls] == ["list_sessions"]


def test_a_dev_session_does_not_count_as_the_users_session(monkeypatch, capsys):
    """A board busy with devs and no user session still STARTS one — «когда по
    проекту их запущено 0» counts user-facing sessions, not processes."""
    dev = {"sid": "S-u-dev_thing-p3", "role": "dev", "status": "active",
           "window": "dev_thing", "tmux_session": "proj-29"}
    calls = _stub_post(monkeypatch, [[dev], [dev, RUNNING]])
    _users(monkeypatch, [ALEX])
    bsq.cmd_start(_args())
    assert "ensure_user_conversation" in [a for a, _ in calls]


def test_a_suspended_attendant_does_not_count_as_running(monkeypatch):
    susp = dict(RUNNING, status="suspended")
    calls = _stub_post(monkeypatch, [[susp], [susp, RUNNING]])
    _users(monkeypatch, [ALEX])
    bsq.cmd_start(_args())
    assert "ensure_user_conversation" in [a for a, _ in calls]


def test_several_running_sessions_are_all_listed(monkeypatch, capsys):
    other = dict(RUNNING, sid="S-u-user_session_flomaster-p8",
                 window="user_session_flomaster")
    calls = _stub_post(monkeypatch, [RUNNING, other])
    bsq.cmd_start(_args())
    out = capsys.readouterr().out
    assert "2 sessions are already running" in out
    assert "user_session_flomaster" in out
    assert [a for a, _ in calls] == ["list_sessions"]


# ---------------------------------------------------------------------------
# Branch 2 — none running: launch one, instantly.
# ---------------------------------------------------------------------------

def test_zero_running_launches_one_and_says_where(monkeypatch, capsys):
    calls = _stub_post(monkeypatch, [[], [RUNNING]])
    _users(monkeypatch, [ALEX])
    bsq.cmd_start(_args())
    out = capsys.readouterr().out
    ensure = [p for a, p in calls if a == "ensure_user_conversation"]
    assert ensure == [{"slug": SLUG, "global_user_id": "gu_alex"}]
    assert "GO proj-29:universal_bsq_session" in out


def test_launch_goes_through_ensure_not_a_second_spawner(monkeypatch):
    """The reuse check, the per-(slug,gid) flock and the resume-a-suspended
    path all live in `ensure_user_conversation`; a `spawn_session` here would
    be a second answer to 'is one already running' (the T-0478 fan-out)."""
    calls = _stub_post(monkeypatch, [[], [RUNNING]])
    _users(monkeypatch, [ALEX])
    bsq.cmd_start(_args())
    assert "spawn_session" not in [a for a, _ in calls]


# ---------------------------------------------------------------------------
# Whose session — the guess must be refused when it is a guess.
# ---------------------------------------------------------------------------

def test_sole_user_needs_no_flag(monkeypatch):
    _users(monkeypatch, [FLO])
    assert bsq._resolve_start_user(None)["id"] == "gu_flo"


def test_super_admin_wins_among_several(monkeypatch):
    _users(monkeypatch, [FLO, ALEX, TIM])
    assert bsq._resolve_start_user(None)["id"] == "gu_alex"


def test_explicit_user_flag_is_honoured(monkeypatch):
    _users(monkeypatch, [FLO, ALEX])
    assert bsq._resolve_start_user("FLOMASTER")["id"] == "gu_flo"


def test_ambiguous_user_is_refused_by_name(monkeypatch, capsys):
    """Two members, no super-admin: guessing hands one person's conversation
    thread to another, so it refuses AND names the candidates."""
    _users(monkeypatch, [FLO, TIM])
    with pytest.raises(SystemExit):
        bsq._resolve_start_user(None)
    err = capsys.readouterr().err
    assert "flomaster" in err and "timpo" in err


def test_unknown_user_names_the_known_ones(monkeypatch, capsys):
    _users(monkeypatch, [FLO, ALEX])
    with pytest.raises(SystemExit):
        bsq._resolve_start_user("nobody")
    assert "flomaster" in capsys.readouterr().err


def test_no_user_store_refuses_rather_than_inventing_a_gid(monkeypatch, capsys):
    _users(monkeypatch, [])
    with pytest.raises(SystemExit):
        bsq._resolve_start_user(None)
    assert "no mothership users" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# T-0964 — role_window's ROUND TRIP through both _derive_role mirrors.
# ---------------------------------------------------------------------------

ROLE_CASES = [
    ("dev", "add-ui-button", "dev_add_ui_button", "dev"),
    ("dev", "slim-cli-for-agents", "dev_slim_cli_for_agents", "dev"),
    ("teamlead", "add-ui-button", "add_ui_button_tl", "teamlead"),
    ("tl", "add-ui-button", "add_ui_button_tl", "teamlead"),
    ("prod-teamlead", "ship-it", "ship_it_prod_tl", "prod-teamlead"),
    ("qa", "ship-it", "ship_it_qa", "qa"),
    ("operator", "ship-it", "operator", "operator"),
    ("user-conversation", "ship-it", "user_session", "user-conversation"),
]


@pytest.mark.parametrize("role,feature,window,derived", ROLE_CASES)
def test_role_window_names_the_role(role, feature, window, derived):
    assert bsq.role_window(role, feature, "T-0001") == window


@pytest.mark.parametrize("role,feature,window,derived", ROLE_CASES)
def test_role_window_round_trips_through_python_derive_role(
        role, feature, window, derived):
    sys.path.insert(0, str(_REPO / "worker"))
    try:
        from bot_squad_worker.sessions import _derive_role
    finally:
        sys.path.pop(0)
    assert _derive_role(bsq.role_window(role, feature, "T-0001"), None, None) == derived


@pytest.mark.parametrize("role,feature,window,derived", ROLE_CASES)
def test_role_window_round_trips_through_the_bash_mirror(
        role, feature, window, derived):
    """The hook derives the role in bash, not python — a name that round-trips
    in only one mirror still hands the live session the wrong contract."""
    script = _REPO / "scripts" / "hooks" / "derive_role.sh"
    win = bsq.role_window(role, feature, "T-0001")
    res = subprocess.run(
        ["bash", "-c", f'source "{script}"; bsq_derive_role "{win}"'],
        capture_output=True, text=True, check=True)
    assert res.stdout.strip() == derived


def test_a_feature_slug_ending_in_another_marker_stays_a_dev():
    """`dev_` is authoritative — the reason it is checked FIRST. Without the
    prefix arm, this ticket slug would have spawned a session that read qa.md."""
    win = bsq.role_window("dev", "move-the-qa", "T-0001")
    assert win == "dev_move_the_qa"
    sys.path.insert(0, str(_REPO / "worker"))
    try:
        from bot_squad_worker.sessions import _derive_role
    finally:
        sys.path.pop(0)
    assert _derive_role(win, None, None) == "dev"


def test_role_window_caps_the_whole_name_including_the_marker():
    """The cap is a SID-segment cap, so it must not be able to eat the trailing
    marker — a truncated `…_q` derives as dev and the QA session reads dev.md."""
    long = "a-very-long-feature-slug-" * 5
    for role, expect in (("dev", "dev"), ("qa", "qa"), ("teamlead", "teamlead")):
        win = bsq.role_window(role, long, "T-0001")
        assert len(win) <= 40, (role, win)
        sys.path.insert(0, str(_REPO / "worker"))
        try:
            from bot_squad_worker.sessions import _derive_role
        finally:
            sys.path.pop(0)
        assert _derive_role(win, None, None) == expect, win


def test_empty_feature_falls_back_to_the_ticket_id_not_a_bare_marker():
    assert bsq.role_window("dev", "", "T-0963") == "dev_t_0963"


def test_a_long_title_is_cut_at_a_word_boundary():
    """Reported from a live spawn (operator, 2026-09-06): a hard slice named a
    session `dev_work_state_doc_replaces_operator_sta`. The complaint this
    ticket answers is that names must be legible to a HUMAN, so the cut takes
    whole words."""
    win = bsq.role_window("dev", "work-state-doc-replaces-operator-state-doc", "T-1")
    assert win == "dev_work_state_doc_replaces_operator"
    assert not win.endswith("_")


def test_an_unsplittable_word_is_still_capped():
    """The word-boundary rule must not be able to produce a bare `dev_` that
    names no task, nor a name over the SID-segment cap."""
    win = bsq.role_window("dev", "a" * 90, "T-1")
    assert len(win) == 40 and win.startswith("dev_a")


def test_a_zombie_attendant_does_not_count_as_running(monkeypatch):
    """An md still claiming `status: active` whose pane the user closed. The
    row's `activity` is derived from the pane and forced to "suspended" for
    every md-only row, so it is the field that tells them apart — `status`
    alone says "active" and would send the user to a window that is gone."""
    zombie = dict(RUNNING, activity="suspended")
    calls = _stub_post(monkeypatch, [[zombie], [zombie, dict(RUNNING, activity="active")]])
    _users(monkeypatch, [ALEX])
    bsq.cmd_start(_args())
    assert "ensure_user_conversation" in [a for a, _ in calls]


def test_a_live_attendant_with_no_activity_field_still_counts(monkeypatch):
    """The liveness gate must not turn a legacy row (no `activity`) into a
    reason to spawn a SECOND attendant — absent is not "suspended"."""
    legacy = {k: v for k, v in RUNNING.items()}
    calls = _stub_post(monkeypatch, [legacy])
    bsq.cmd_start(_args())
    assert [a for a, _ in calls] == ["list_sessions"]
