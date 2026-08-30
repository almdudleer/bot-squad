"""T-0895 — `on_breach: spawn` actually attaches an agent, and a spawn that
fails is never silent.

Two defects, one ticket:

1. ``routines._spawn_for_routine`` spawns with ``owner=routine:R-NNNN``, and
   ``sessions.spawn``'s owner validator (a colon-free character class, because
   owner lands in ``BOT_SQUAD_OWNER=…``, a shell assignment) rejected every
   such value. So NO routine had ever spawned: watchrobot's whole
   ``events.ndjson`` held 88 ``notify`` + 10 ``recover`` and zero ``fire``.
2. The rejection was swallowed as capacity backpressure, so the breach produced
   no spawn, no alert, no event and no cooldown — `spawn` was strictly worse
   than `notify`.

WHY THIS FILE EXISTS SEPARATELY from test_monitors.py: that suite stubs
``sessions.spawn`` wholesale, so it was green THROUGHOUT the defect and could
not have caught it. Every test here drives the REAL validator — the tmux
subprocess seam is the only thing stubbed — and the negative controls are
written on REJECTED inputs, so they fail if the fix is widened into a hole.
"""
from __future__ import annotations

import subprocess
import types
from datetime import timedelta
from pathlib import Path

import pytest

from bot_squad_worker import routines as R
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from tests.test_jobs import _make_config_with_project, _make_project_with_repo
from tests.test_monitors import T0, _events, _spec

ROUTINE_OWNER = "routine:R-0018"


# ---------------------------------------------------------------------------
# The real-spawn harness: stub tmux (and the two pane-readiness waits), keep
# EVERYTHING inside sessions.spawn — owner validation included — real.
# ---------------------------------------------------------------------------

def _stub_tmux(monkeypatch, tmp_path: Path, repo: Path, window: str,
               captured: list[str]) -> None:
    # T-0933: the handler spawn asks for window "routine-handler", not "dev",
    # so list-panes must echo back whatever window the LAST new-window
    # requested — a hardcoded name silently strands every non-"dev" spawn in
    # "no pane appeared" (measured: the handler test failed exactly there).
    live = {"window": window}

    def fake_run(args, **kwargs):
        if "new-window" in args:
            try:
                live["window"] = args[args.index("-n") + 1]
            except (ValueError, IndexError):
                pass
            try:
                captured.append(args[args.index("-lc") + 1])
            except (ValueError, IndexError):
                pass
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%7|{live['window']}|4321|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    monkeypatch.setattr(S, "_wait_for_agent_composer_ready", lambda *_: True)
    monkeypatch.setattr(S, "_deliver_prompt", lambda *a, **k: None)


@pytest.fixture
def real_spawn_cfg(tmp_path: Path, monkeypatch):
    """(cfg, slug, captured_shell_cmds) with the REAL sessions.spawn wired."""
    project = _make_project_with_repo(tmp_path, slug="routine-owner-proj")
    cfg = _make_config_with_project(tmp_path, project)
    (cfg.data_dir / project.slug).mkdir(parents=True, exist_ok=True)
    captured: list[str] = []
    _stub_tmux(monkeypatch, tmp_path, project.repo_path, "dev", captured)
    return cfg, project.slug, captured


# ---------------------------------------------------------------------------
# DoD 2 — the routine owner passes; the boundary is NOT widened for anyone else
# ---------------------------------------------------------------------------

def test_spawn_accepts_routine_owner_and_stamps_the_env_var(real_spawn_cfg):
    cfg, slug, captured = real_spawn_cfg
    res = S.spawn(cfg, slug, "dev", owner=ROUTINE_OWNER)
    assert res.get("sid")
    assert captured, "expected a tmux new-window"
    # the colon survives into the env assignment, quoted by shlex
    assert f"BOT_SQUAD_OWNER={ROUTINE_OWNER}" in captured[0]
    # ...and the routine binding derives NO human owner_user (it is not a
    # person). Before this was fixed, _derive_owner_user echoed the owner back
    # and the colon-free owner_user validator raised on it instead — the same
    # spawn refusal one line further down.
    assert "BOT_SQUAD_OWNER_USER" not in captured[0]


@pytest.mark.parametrize("bad", [
    # DoD 3 — real injections, written on the REJECTED input
    "routine:R-1; rm -rf /",
    "a b",
    "$(id)",
    "x'y",
    "routine:R-0018; id",
    "routine:$(id)",
    "routine:R-0018 && curl evil",
    "routine:R-0018\nBOT_SQUAD_OWNER=root",
    'routine:R-0018"',
    "routine:`id`",
    # the allowance is WHOLE-STRING, not a prefix/substring match
    "xroutine:R-0018",
    "routine:R-0018:extra",
    "please routine:R-0018",
    "routine:T-0018",
    "routine:R-",
    "routine:",
    # a colon is still illegal for every NON-routine owner, incl. JWT-sourced
    "alexey:admin",
    "S-almdudleer-operator-p1:x",
    "constant-team:x",
])
def test_spawn_rejects_injection_and_near_miss_owners(real_spawn_cfg, bad):
    cfg, slug, captured = real_spawn_cfg
    with pytest.raises(ActionError, match="invalid owner"):
        S.spawn(cfg, slug, "dev", owner=bad)
    assert captured == [], "a rejected owner must not reach tmux at all"


def test_plain_owners_still_accepted(real_spawn_cfg):
    """Positive control for the general class — the fix must not have narrowed
    it either (these are the JWT-username / sentinel / TL-SID owners)."""
    cfg, slug, captured = real_spawn_cfg
    for good in ("dev", "operator", "alexey", "constant-team",
                 "S-almdudleer-operator-p322", "a.b-c_d"):
        captured.clear()
        S.spawn(cfg, slug, "dev", owner=good)
        assert f"BOT_SQUAD_OWNER={good}" in captured[0]


def test_derive_owner_user_returns_none_for_a_routine_owner(real_spawn_cfg):
    cfg, slug, _ = real_spawn_cfg
    assert S._derive_owner_user(cfg, slug, ROUTINE_OWNER) is None
    assert S._derive_owner_user(cfg, slug, "alexey") == "alexey"


# ---------------------------------------------------------------------------
# DoD 1 — a confirmed breach on `on_breach: spawn` really attaches a session
# bound to routine:R-NNNN, and events.ndjson gets a `fire` with its SID.
# Same path as production: monitor_sweep -> _handle_fire -> _spawn_for_routine
# -> the REAL sessions.spawn.
# ---------------------------------------------------------------------------

def _declare_spawn_monitor(cfg, slug, metric: Path, **over) -> str:
    kw = {"threshold": 10, "persist_s": 0, "cooldown_s": 0,
          "on_breach": "spawn", **over}
    spec = _spec(cmd=f"cat {metric}", **kw)
    return R.declare(cfg, slug, instruction="fix the breach per runbook",
                     trigger="monitor", monitor=spec,
                     provenance="T-0895", now=T0)["id"]


def test_breach_attaches_a_real_session_and_records_fire(real_spawn_cfg,
                                                         tmp_path, monkeypatch):
    cfg, slug, captured = real_spawn_cfg
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [])
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_spawn_monitor(cfg, slug, metric)

    res = R.monitor_sweep(cfg, slug, now=T0)

    assert res["fired"] == [rid]
    evs = _events(cfg, slug)
    assert [e["kind"] for e in evs] == ["fire"]
    assert evs[0]["sid"] and evs[0]["sid"].startswith("S-")
    assert evs[0]["value"] == 42.0 and evs[0]["threshold"] == 10
    # T-0933: the session that was actually launched is the SHARED handler —
    # one session triages every routine's breaches, not one per routine.
    assert f"BOT_SQUAD_OWNER={R.ROUTINE_HANDLER_OWNER}" in captured[0]
    # cooldown stamped only because the attach really happened
    st = R.load_state(cfg, slug, rid)
    assert st["fired"] is True and st["last_fired_at"] == R._iso(T0)


# ---------------------------------------------------------------------------
# DoD 4 — a spawn that cannot attach degrades to `notify`, never to silence
# ---------------------------------------------------------------------------

@pytest.fixture
def notify_cfg(tmp_path: Path, monkeypatch):
    """(cfg, slug, dms, metric, rid) with spawn FAILING and the DM SSOT stubbed."""
    project = _make_project_with_repo(tmp_path, slug="routine-degrade-proj")
    cfg = _make_config_with_project(tmp_path, project)
    (cfg.data_dir / project.slug).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [])
    dms: list[dict] = []

    from bot_squad_worker import actions

    def _fake_dm(cfg_, *, message, **kw):
        dms.append({"message": message, **kw})
        return {"ok": True, "sent": True, "channel": "test"}

    monkeypatch.setattr(actions, "_send_stakeholder_dm", _fake_dm)
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_spawn_monitor(cfg, project.slug, metric, cooldown_s=1800)
    return types.SimpleNamespace(cfg=cfg, slug=project.slug, dms=dms,
                                 metric=metric, rid=rid)


def _spawn_raises(msg: str):
    def _boom(*a, **kw):
        raise ActionError(msg)
    return _boom


def test_non_capacity_spawn_failure_pages_and_records_notify(notify_cfg,
                                                             monkeypatch):
    """The exact shape of the shipped defect: spawn refuses (here: the owner
    validation that used to refuse EVERY routine spawn) -> the stakeholder is
    told, events.ndjson gets a notify, cooldown is stamped so it alerts once
    per cooldown instead of looping mutely."""
    n = notify_cfg
    monkeypatch.setattr(S, "spawn",
                        _spawn_raises("spawn: invalid owner 'routine:R-0018'"))

    res = R.monitor_sweep(n.cfg, n.slug, now=T0)

    assert res["fired"] == [n.rid]
    assert len(n.dms) == 1
    dm = n.dms[0]
    assert dm["urgent"] is True and dm["sid"] == f"routine:{n.rid}"
    assert "NO AI COULD BE ATTACHED" in dm["message"]
    assert "invalid owner" in dm["message"]      # the reason, not just a shrug
    assert "42" in dm["message"] and "10" in dm["message"]
    evs = _events(n.cfg, n.slug)
    assert [e["kind"] for e in evs] == ["notify"]
    assert "attach failed" in evs[0]["note"]
    st = R.load_state(n.cfg, n.slug, n.rid)
    assert st["fired"] is True and st["last_fired_at"] == R._iso(T0)


def test_spawn_returning_no_sid_also_degrades_to_notify(notify_cfg, monkeypatch):
    """Not every failure raises — a spawn that returns without a SID left the
    breach equally unattended and equally silent."""
    n = notify_cfg
    monkeypatch.setattr(S, "spawn", lambda *a, **kw: {"ok": False})
    R.monitor_sweep(n.cfg, n.slug, now=T0)
    assert len(n.dms) == 1 and "no sid" in n.dms[0]["message"]
    assert [e["kind"] for e in _events(n.cfg, n.slug)] == ["notify"]


def test_undelivered_degraded_alert_retries_next_tick(notify_cfg, monkeypatch):
    """Mirrors _notify_breach: an undeliverable page stamps nothing, so the
    next sweep tries again rather than dropping the breach on the floor."""
    n = notify_cfg
    monkeypatch.setattr(S, "spawn", _spawn_raises("spawn: no pane"))
    from bot_squad_worker import actions
    monkeypatch.setattr(actions, "_send_stakeholder_dm",
                        lambda c, *, message, **kw: {"ok": False, "channel": "none"})

    res = R.monitor_sweep(n.cfg, n.slug, now=T0)
    assert res["fired"] == []
    assert _events(n.cfg, n.slug) == []
    st = R.load_state(n.cfg, n.slug, n.rid)
    assert st["fired"] is False and st["last_fired_at"] is None

    def _up(c, *, message, **kw):
        n.dms.append({"message": message, **kw})
        return {"ok": True, "channel": "test"}

    monkeypatch.setattr(actions, "_send_stakeholder_dm", _up)
    res = R.monitor_sweep(n.cfg, n.slug, now=T0 + timedelta(seconds=5))
    assert res["fired"] == [n.rid] and len(n.dms) == 1


# --- the ⚠ carve-out: capacity backpressure stays quiet, but not forever ----

def test_capacity_deferral_is_quiet_then_alerts_once_per_window(notify_cfg,
                                                                monkeypatch):
    n = notify_cfg
    monkeypatch.setattr(S, "spawn",
                        _spawn_raises("spawn: capacity reached (3/3 sessions)"))
    monkeypatch.setattr(R, "SPAWN_DEFER_QUIET_S", 900.0)

    # first sighting + everything inside the quiet window: silent retries,
    # cooldown never stamped (a slot may free up on any tick)
    for offset in (0, 30, 600, 890):
        res = R.monitor_sweep(n.cfg, n.slug, now=T0 + timedelta(seconds=offset))
        assert res["fired"] == []
    assert n.dms == []
    assert _events(n.cfg, n.slug) == []
    st = R.load_state(n.cfg, n.slug, n.rid)
    assert st["fired"] is False
    assert st["spawn_defer_count"] == 4

    # past the window: the silence breaks
    res = R.monitor_sweep(n.cfg, n.slug, now=T0 + timedelta(seconds=901))
    assert res["fired"] == []          # still retrying — capacity may return
    assert len(n.dms) == 1
    assert "STILL has no AI attached" in n.dms[0]["message"]
    assert "5 spawn attempts" in n.dms[0]["message"]
    assert [e["kind"] for e in _events(n.cfg, n.slug)] == ["notify"]

    # and only ONE alert per window, not one per tick
    R.monitor_sweep(n.cfg, n.slug, now=T0 + timedelta(seconds=1000))
    assert len(n.dms) == 1
    R.monitor_sweep(n.cfg, n.slug, now=T0 + timedelta(seconds=1810))
    assert len(n.dms) == 2


def test_capacity_deferral_bookkeeping_cleared_when_the_spawn_lands(notify_cfg,
                                                                    monkeypatch):
    n = notify_cfg
    monkeypatch.setattr(S, "spawn", _spawn_raises("spawn: capacity reached"))
    R.monitor_sweep(n.cfg, n.slug, now=T0)
    assert R.load_state(n.cfg, n.slug, n.rid)["spawn_defer_count"] == 1

    monkeypatch.setattr(S, "spawn", lambda *a, **kw: {"ok": True, "sid": "S-x-p1"})
    res = R.monitor_sweep(n.cfg, n.slug, now=T0 + timedelta(seconds=10))
    assert res["fired"] == [n.rid]
    st = R.load_state(n.cfg, n.slug, n.rid)
    assert "spawn_defer_count" not in st and "spawn_defer_since" not in st
    assert [e["kind"] for e in _events(n.cfg, n.slug)] == ["fire"]


def test_capacity_deferral_bookkeeping_cleared_on_recovery(notify_cfg,
                                                           monkeypatch):
    """A stale defer-since would make the NEXT breach's first capacity
    deferral alert immediately instead of waiting out its quiet window."""
    n = notify_cfg
    monkeypatch.setattr(S, "spawn", _spawn_raises("spawn: capacity reached"))
    R.monitor_sweep(n.cfg, n.slug, now=T0)
    n.metric.write_text("1")        # back under threshold
    R.monitor_sweep(n.cfg, n.slug, now=T0 + timedelta(seconds=10))
    st = R.load_state(n.cfg, n.slug, n.rid)
    assert "spawn_defer_since" not in st and "spawn_defer_count" not in st


# ---------------------------------------------------------------------------
# The schedule-trigger twin: same swallowed failure, same silence.
# ---------------------------------------------------------------------------

def test_schedule_routine_spawn_failure_pages_and_advances(tmp_path,
                                                           monkeypatch):
    project = _make_project_with_repo(tmp_path, slug="routine-sched-proj")
    cfg = _make_config_with_project(tmp_path, project)
    (cfg.data_dir / project.slug).mkdir(parents=True, exist_ok=True)
    dms: list[dict] = []
    from bot_squad_worker import actions
    monkeypatch.setattr(
        actions, "_send_stakeholder_dm",
        lambda c, *, message, **kw: (dms.append({"message": message, **kw}),
                                     {"ok": True, "channel": "test"})[1])
    monkeypatch.setattr(S, "spawn", _spawn_raises("spawn: invalid owner 'x'"))
    rid = R.declare(cfg, project.slug, instruction="daily sweep",
                    schedule="0 9 * * *", provenance="T-0895", now=T0)["id"]

    due = T0.replace(hour=9, minute=0, second=0) + timedelta(days=1)
    res = R.tick(cfg, project.slug, now=due)

    assert res["fired"] == []
    assert len(dms) == 1 and "NO session could be attached" in dms[0]["message"]
    assert [e["kind"] for e in _events(cfg, project.slug)] == ["notify"]
    # the schedule advanced: a permanent failure must not re-fire every 60s
    nxt = R.load(cfg, project.slug, rid).next_run_at
    assert nxt is not None and nxt > R._iso(due)


def test_schedule_routine_capacity_deferral_stays_quiet(tmp_path, monkeypatch):
    """Positive control for the carve-out on the schedule path: backpressure
    still leaves next_run_at untouched so a later tick retries."""
    project = _make_project_with_repo(tmp_path, slug="routine-sched-cap-proj")
    cfg = _make_config_with_project(tmp_path, project)
    (cfg.data_dir / project.slug).mkdir(parents=True, exist_ok=True)
    dms: list[dict] = []
    from bot_squad_worker import actions
    monkeypatch.setattr(
        actions, "_send_stakeholder_dm",
        lambda c, *, message, **kw: (dms.append(message),
                                     {"ok": True, "channel": "test"})[1])
    monkeypatch.setattr(S, "spawn", _spawn_raises("spawn: capacity reached"))
    rid = R.declare(cfg, project.slug, instruction="daily sweep",
                    schedule="0 9 * * *", provenance="T-0895", now=T0)["id"]
    before = R.load(cfg, project.slug, rid).next_run_at

    due = T0.replace(hour=9, minute=0, second=0) + timedelta(days=1)
    assert R.tick(cfg, project.slug, now=due)["fired"] == []

    assert dms == []
    assert _events(cfg, project.slug) == []
    assert R.load(cfg, project.slug, rid).next_run_at == before


# ---------------------------------------------------------------------------
# The validator must hold WITHOUT the caller's .strip() (operator p322 review
# of the T-0895 fix). In Python `$` also matches BEFORE a trailing newline, so
# `^…$` accepted "routine:R-0001\n" / "dev\n" — measured True on both classes.
# spawn() strips first, so nothing reachable today was wrong; but these patterns
# are declared the PRIMARY boundary, not a backstop to shlex.quote, so they are
# tested DIRECTLY, unstripped. Anchors are now \Z.
#
# Written on the inputs that PASSED before the anchor change — each of these
# would have gone green against the previous commit, i.e. there was no arm here.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "routine:R-0001\n",          # the reviewed case
    "dev\n",                     # the same hole in the GENERAL class
    "alexey\n",
    "constant-team\n",
    "S-almdudleer-operator-p322\n",
    "routine:R-0001\r",
    "routine:R-0001\n\n",
    "routine:R-0001 ",
    "\nroutine:R-0001",
])
def test_valid_owner_rejects_trailing_whitespace_unstripped(bad):
    assert S._valid_owner(bad) is False, (
        f"{bad!r} must fail the validator itself, not only after .strip()")


def test_valid_owner_still_accepts_the_clean_forms():
    """Positive control: tightening the anchor must not reject anything real."""
    for good in ("routine:R-0001", "routine:R-10000", "dev", "operator",
                 "alexey", "constant-team", "S-almdudleer-operator-p322",
                 "a.b-c_d"):
        assert S._valid_owner(good) is True, good


def test_owner_user_shares_the_owner_class(real_spawn_cfg):
    """owner_user kept a SECOND copy of the same literal, which is how the hole
    survives a fix applied to one of them. It now reuses _OWNER_RE."""
    cfg, slug, captured = real_spawn_cfg
    with pytest.raises(ActionError, match="invalid owner_user"):
        S.spawn(cfg, slug, "dev", owner="dev", owner_user="alexey:admin")
    assert captured == []
    assert S._OWNER_RE.match("alexey\n") is None


def test_global_user_id_guard_rejects_a_trailing_newline():
    """Third copy of the same anchor bug — its own docstring calls it the guard
    against smuggling a shell/tmux metacharacter into the spawn command."""
    assert S._GLOBAL_USER_ID_RE.match("gu_abc\n") is None
    assert S.user_conversation_window("gu_abc") == "gu_abc-user-conversation"
    with pytest.raises(ActionError, match="invalid global_user_id"):
        S.user_conversation_window("gu_abc\nx")


# ---------------------------------------------------------------------------
# T-0933 — ONE shared routine-handler session for every breach
# ---------------------------------------------------------------------------

def _handler_row(sid="S-u-routine-handler-p7"):
    return {"sid": sid, "owner": R.ROUTINE_HANDLER_OWNER,
            "status": "active", "archived": False}


def test_second_routine_breach_routes_into_the_live_handler(real_spawn_cfg,
                                                            tmp_path,
                                                            monkeypatch):
    """A live handler absorbs EVERY routine's breach — no second spawn, the
    breach lands in the handler's input queue instead."""
    from bot_squad_worker import input_mux
    cfg, slug, captured = real_spawn_cfg
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [_handler_row()])
    metric = tmp_path / "metric2.txt"
    metric.write_text("42")
    rid = _declare_spawn_monitor(cfg, slug, metric)

    res = R.monitor_sweep(cfg, slug, now=T0)

    assert res["fired"] == [rid]
    assert captured == [], "no new session may be spawned while a handler lives"
    queued = input_mux.read_queue(cfg.data_dir, "S-u-routine-handler-p7")
    assert len(queued) == 1
    assert f"[ROUTINE BREACH {rid}]" in queued[0]["text"]
    assert queued[0]["author"] == f"routine:{rid}"
    evs = _events(cfg, slug)
    assert evs[-1]["kind"] == "fire" and evs[-1]["sid"] == "S-u-routine-handler-p7"
    st = R.load_state(cfg, slug, rid)
    assert st["fired"] is True  # cooldown stamped: the fire WAS delivered


def test_legacy_per_routine_owner_still_takes_the_breach_first(real_spawn_cfg,
                                                               tmp_path,
                                                               monkeypatch):
    """A session already bound to THIS routine (owner routine:R-NNNN — the
    pre-handler shape, or a hand-spawned owner) outranks the shared handler:
    one brain per breach, and that brain already holds the context."""
    from bot_squad_worker import input_mux
    cfg, slug, captured = real_spawn_cfg
    metric = tmp_path / "metric3.txt"
    metric.write_text("42")
    rid = _declare_spawn_monitor(cfg, slug, metric)
    owner_row = {"sid": "S-u-owner-p3", "owner": f"routine:{rid}",
                 "status": "active", "archived": False}
    monkeypatch.setattr(S, "list_sessions",
                        lambda c, s: [owner_row, _handler_row()])

    res = R.monitor_sweep(cfg, slug, now=T0)

    assert res["fired"] == [rid]
    assert captured == []
    assert len(input_mux.read_queue(cfg.data_dir, "S-u-owner-p3")) == 1
    assert input_mux.read_queue(cfg.data_dir, "S-u-routine-handler-p7") == []


def test_handler_brief_names_the_standing_role(real_spawn_cfg, tmp_path):
    """The fresh handler must know it is THE handler (later breaches arrive as
    messages), not a one-breach session that exits when its routine clears."""
    cfg, slug, _ = real_spawn_cfg
    metric = tmp_path / "metric4.txt"
    metric.write_text("42")
    rid = _declare_spawn_monitor(cfg, slug, metric)
    routine = R.load(cfg, slug, rid)
    ev = R.FireEvent(kind="fire", value=42.0, threshold=10,
                     judge="gt", breach_first_seen="2026-08-30T00:00:00Z")
    brief = R._handler_brief(cfg, slug, routine, ev, now=T0)
    assert "SINGLE shared ROUTINE-HANDLER" in brief
    assert "[ROUTINE BREACH R-NNNN]" in brief
    assert "Stay resident" in brief
    assert rid in brief  # the triggering breach rides along
