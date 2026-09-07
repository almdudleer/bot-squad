"""T-0465 / M1-F1.2 — work-done → graceful exit (uniform across roles).

Source: vision/initiatives/process-paradigm.md (SOURCE-VERBATIM Part A) — "If the
work the session was created to do is done, the session should exit" +
clarification-01 "ALL the sessions from operator to dev to user session have same
lifecycle."

The missing lifecycle path: a session whose ASSIGNMENT IS DONE suspends itself
(no relaunch — the deliverable already exists), uniformly across roles. The
done-signal differs ONLY by role: a task-bound role (dev/TL) is done when its
bound task is terminal (totest/closed); an operator is done when its backlog is
empty. A NOT-done session is left to the timeout recyclers, never to graceful_exit.

Pairs with test_idle_timeout (recycle-on-timeout) — together they cover the two
"never block indefinitely" outcomes (done→exit / waiting→recycle).
"""
from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import autocompact as A
from bot_squad_worker import graceful_exit as GE
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import sessions as S


# --- env knobs --------------------------------------------------------------

def test_enabled_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_GRACEFUL_EXIT", raising=False)
    assert GE.graceful_exit_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT", "0")
    assert GE.graceful_exit_enabled() is False


def test_grace_default_and_override(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC", raising=False)
    assert GE.exit_grace_sec() == GE.DEFAULT_EXIT_GRACE_SEC
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC", "60")
    assert GE.exit_grace_sec() == 60
    # garbage / non-positive falls back to the default
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC", "-5")
    assert GE.exit_grace_sec() == GE.DEFAULT_EXIT_GRACE_SEC
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC", "nope")
    assert GE.exit_grace_sec() == GE.DEFAULT_EXIT_GRACE_SEC


# --- pure decision helpers --------------------------------------------------

def test_work_done_operator_keyed_on_empty_backlog():
    assert GE.work_done("operator", None, "", pending_backlog=0) is True
    assert GE.work_done("operator", None, "", pending_backlog=3) is False


def test_work_done_task_bound_keyed_on_terminal_status():
    for st in ("totest", "closed"):
        assert GE.work_done("dev", "T-0042", st, pending_backlog=99) is True
    for st in ("in_progress", "open", "reopened", ""):
        assert GE.work_done("dev", "T-0042", st, pending_backlog=0) is False


def test_work_done_no_signal_session_never_done():
    # role-only / user-conversation (no task, not operator) has no auto-done signal
    assert GE.work_done("dev", None, "", pending_backlog=0) is False
    assert GE.work_done("teamlead", "~", "", pending_backlog=0) is False


def test_work_done_dev_not_special_cased_among_task_bound_roles():
    """T-0477 / F2.3: dev rides the SAME universal lifecycle — its graceful-exit
    done-signal is the GENERIC task-bound path (terminal task status), with NO
    dev-specific branch. For identical (task_id, status) inputs, 'dev' must yield
    the SAME work_done verdict as any other task-bound role. Operator is excluded
    on purpose: it is legitimately backlog-keyed (role-routing, not exemption).
    If a future edit re-introduced an `if role == 'dev'` exemption, this goes red."""
    for st in ("totest", "closed", "in_progress", "open", "reopened", ""):
        dev = GE.work_done("dev", "T-0477", st, pending_backlog=7)
        for other in ("teamlead", "qa", "prod-teamlead", "some-future-role"):
            assert GE.work_done(other, "T-0477", st, pending_backlog=7) is dev


# --- T-0930: task-less TL — "nothing open" in its initiative --------------

def test_work_done_teamlead_no_task_keyed_on_initiative_pending():
    assert GE.work_done("teamlead", None, "", pending_backlog=0,
                        initiative_pending=0) is True
    assert GE.work_done("teamlead", None, "", pending_backlog=0,
                        initiative_pending=3) is False


def test_work_done_teamlead_initiative_pending_none_means_not_computed():
    # None (no initiative to scope by) is NOT the same as zero — stays False,
    # same "no auto-done signal" posture as any other role-only session.
    assert GE.work_done("teamlead", None, "", pending_backlog=0,
                        initiative_pending=None) is False


def test_work_done_teamlead_with_a_real_task_ignores_initiative_pending():
    # A task-bound TL still rides the generic task-bound path — initiative
    # scoping only kicks in when there is NO primary task_id.
    assert GE.work_done("teamlead", "T-0042", "totest", pending_backlog=0,
                        initiative_pending=5) is True
    assert GE.work_done("teamlead", "T-0042", "open", pending_backlog=0,
                        initiative_pending=0) is False


def test_count_pending_initiative_tasks(tmp_path):
    cfg, data_dir = _make_cfg(tmp_path, sid="S-x", window="demo", task_id=None)
    backlog = data_dir / "bot-squad" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / "T-0001-a.md").write_text(
        "---\nid: T-0001\ntitle: A\nstatus: open\ninitiative: my-init.md\n---\n\nx\n")
    (backlog / "T-0002-b.md").write_text(
        "---\nid: T-0002\ntitle: B\nstatus: in_progress\ninitiative: my-init\n---\n\nx\n")
    (backlog / "T-0003-c.md").write_text(
        "---\nid: T-0003\ntitle: C\nstatus: closed\ninitiative: my-init.md\n---\n\nx\n")
    (backlog / "T-0004-d.md").write_text(
        "---\nid: T-0004\ntitle: D\nstatus: open\ninitiative: other-init.md\n---\n\nx\n")
    (backlog / "T-0005-e.md").write_text(
        "---\nid: T-0005\ntitle: E\nstatus: open\ninitiative: my-init.md\n"
        "archived: true\n---\n\nx\n")
    # T-0948: DELIVERED and PARKED subtasks are no longer the TL's pending
    # work. This block used to count `totest` (the pin below asserted 2 with a
    # totest ticket in the set) on the T-0930 rationale that a totest task was
    # "still the TL's to review/close" — T-0944 made totest the HUMAN's queue
    # and to_accept the OPERATOR's, and leaving the counter behind is what kept
    # a task-less TL nudged «продолжай» every 40 min all weekend over work it
    # had already delivered and could not legally move.
    (backlog / "T-0006-f.md").write_text(
        "---\nid: T-0006\ntitle: F\nstatus: totest\ninitiative: my-init.md\n---\n\nx\n")
    (backlog / "T-0007-g.md").write_text(
        "---\nid: T-0007\ntitle: G\nstatus: to_accept\ninitiative: my-init.md\n---\n\nx\n")
    (backlog / "T-0008-h.md").write_text(
        "---\nid: T-0008\ntitle: H\nstatus: blocked_on_user\ninitiative: my-init.md\n---\n\nx\n")
    (backlog / "T-0009-i.md").write_text(
        "---\nid: T-0009\ntitle: I\nstatus: paused\ninitiative: my-init.md\n---\n\nx\n")
    # ...but `planned` DOES count: queueing and dispatching an unstarted
    # subtask is the coordination work itself, which is why this predicate is
    # deliberately not the same one `task_alive` uses for a BOUND ticket.
    (backlog / "T-0010-j.md").write_text(
        "---\nid: T-0010\ntitle: J\nstatus: planned\ninitiative: my-init.md\n---\n\nx\n")
    # T-0001 (open), T-0002 (in_progress, bare-stem match) and T-0010 (planned)
    # count; T-0003 (closed), T-0004 (other initiative), T-0005 (archived),
    # T-0006 (totest), T-0007 (to_accept), T-0008 (blocked_on_user) and
    # T-0009 (paused) don't.
    assert GE.count_pending_initiative_tasks(cfg, "bot-squad", "my-init.md") == 3
    assert GE.count_pending_initiative_tasks(cfg, "bot-squad", "my-init") == 3
    assert GE.count_pending_initiative_tasks(cfg, "bot-squad", "other-init") == 1
    assert GE.count_pending_initiative_tasks(cfg, "bot-squad", None) == 0
    assert GE.count_pending_initiative_tasks(cfg, "bot-squad", "~") == 0
    assert GE.count_pending_initiative_tasks(cfg, "bot-squad", "no-such-init") == 0


def test_taskless_tl_exits_when_initiative_backlog_is_empty(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-tl-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="tl", task_id=None,
                          extra_md={"role": "teamlead"})
    backlog = data / "bot-squad" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / "T-0001-a.md").write_text(
        "---\nid: T-0001\ntitle: A\nstatus: closed\ninitiative: my-init.md\n---\n\nx\n")
    row = _row(sid, role="teamlead", cwd_repo=data.parent / "repo",
              task_id=None, initiative="my-init.md")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]


def test_taskless_tl_not_exited_when_initiative_has_open_work(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-tl-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="tl", task_id=None,
                          extra_md={"role": "teamlead"})
    backlog = data / "bot-squad" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / "T-0001-a.md").write_text(
        "---\nid: T-0001\ntitle: A\nstatus: open\ninitiative: my-init.md\n---\n\nx\n")
    row = _row(sid, role="teamlead", cwd_repo=data.parent / "repo",
              task_id=None, initiative="my-init.md")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_taskless_tl_with_no_initiative_never_exits(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-tl-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="tl", task_id=None,
                          extra_md={"role": "teamlead"})
    row = _row(sid, role="teamlead", cwd_repo=data.parent / "repo",
              task_id=None, initiative=None)
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_exit_due_grace():
    assert GE.exit_due(180, 180) is True
    assert GE.exit_due(181, 180) is True
    assert GE.exit_due(179, 180) is False
    # unknowable idle age is conservative — never due (don't race a finishing dev)
    assert GE.exit_due(None, 180) is False


# --- cfg + session/backlog md harness (mirrors test_idle_timeout) -----------

def _make_cfg(tmp_path: Path, *, sid: str, window: str, task_id: str | None,
              task_status: str | None = None, extra_md: dict | None = None):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True)
    fm = {"sid": sid, "status": "active", "window": window,
          "cwd": str(repo), "claude_uuid": "uuid-" + sid}
    if task_id:
        fm["task_id"] = task_id
    if extra_md:
        fm.update(extra_md)
    S._write_session_metadata(sess / f"{sid}.md", fm)

    if task_id and task_status is not None:
        _write_task(data_dir, task_id, task_status)

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir)
    return patched, data_dir


def _write_task(data_dir: Path, task_id: str, status: str, *, title="Demo task"):
    backlog = data_dir / "bot-squad" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / f"{task_id}-demo.md").write_text(
        f"---\nid: {task_id}\ntitle: {title}\nstatus: {status}\n---\n\nbody\n")


def _row(sid: str, *, role="dev", window="demo", task_id="T-0042",
         cwd_repo: Path, status="active", initiative=None):
    return {"sid": sid, "status": status, "window": window, "task_id": task_id,
            "role": role, "cwd": str(cwd_repo), "claude_uuid": "uuid-" + sid,
            "linux_user": "", "initiative": initiative}


@pytest.fixture
def seams(monkeypatch):
    """Stub every tmux/suspend seam so no real session is touched."""
    calls = {"suspend": [], "ctx_handoff": [], "compact": []}
    state = {"pane": "%9", "buf": "❯ \n", "idle_age": 5000.0}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(GE, "_suspend",
                        lambda cfg, slug, sid: calls["suspend"].append(sid))
    # T-0945: the pre-exit handoff ask (never a /compact — `compact` is wired so
    # a regression that reintroduces one on this path fails loudly).
    monkeypatch.setattr(A, "_inject_context_handoff",
                        lambda sid, task_id, *, relaunch=True, stay=False:
                        calls["ctx_handoff"].append((sid, task_id, relaunch)))
    monkeypatch.setattr(A, "_send_compact",
                        lambda sid: calls["compact"].append(sid))
    # T-0945: this path now consults recycle_gate; no human is attached and
    # nothing is pinned in these tests unless a test says otherwise.
    monkeypatch.setattr(GE.recycle_gate, "is_attached", lambda target, **kw: False)
    monkeypatch.delenv("BOT_SQUAD_EXIT_HANDOFF", raising=False)
    # idle clock: jsonl mtime = now - idle_age
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - state["idle_age"])
    monkeypatch.delenv("BOT_SQUAD_GRACEFUL_EXIT", raising=False)
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC", "180")
    return {"calls": calls, "state": state}


# --- T-0945: the pre-exit handoff (handoff + exit, NEVER a compact) ---------

def _write_context(data, task_id: str, text: str) -> None:
    """Stand in for the session answering the handoff ask — the ONLY thing
    `_maybe_arm_exit_handoff` watches is the ticket's `## Context` digest."""
    md = data / "bot-squad" / "backlog" / f"{task_id}-demo.md"
    body = md.read_text()
    head, _, _tail = body.partition("## Context")
    md.write_text(head + "## Context\n\n" + text + "\n")


def _exit_after_handoff(cfg, data, row, seams, *, task_id="T-0042"):
    """Two ticks: the first ARMS the handoff, the second (after the session has
    written) exits. Returns the second tick's result."""
    now = time.time()
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now, user_home="/home/x") is True
    assert seams["calls"]["suspend"] == []       # not yet — the ticket first
    _write_context(data, task_id, "what is true now")
    return GE.maybe_exit(cfg, "bot-squad", row, now=now + 1, user_home="/home/x")


# --- DEV: work-done → graceful exit -----------------------------------------

def test_dev_done_suspends_no_relaunch(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert _exit_after_handoff(cfg, data, row, seams) is True
    assert seams["calls"]["suspend"] == [sid]
    # T-0945: the ask goes to the TICKET, and no compact is ever spent here —
    # «когда to test для меня … компакты делать не надо».
    assert seams["calls"]["ctx_handoff"] == [(sid, "T-0042", False)]
    assert seams["calls"]["compact"] == []


def test_dev_closed_also_exits(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="closed")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert _exit_after_handoff(cfg, data, row, seams) is True
    assert seams["calls"]["suspend"] == [sid]
    assert seams["calls"]["compact"] == []


def test_dev_not_done_is_left_alone(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="in_progress")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_dev_done_but_fresh_waits_for_grace(tmp_path, seams):
    """A dev that JUST set totest (still committing/pinging) is not cut off."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    seams["state"]["idle_age"] = 10.0  # finishing its READY sequence
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_dev_done_mid_turn_not_cut(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    seams["state"]["buf"] = "working… esc to interrupt\n"
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_done_but_pane_gone_is_noop(tmp_path, seams):
    """No live pane → the session already exited; nothing to suspend."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    seams["state"]["pane"] = None
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


# --- OPERATOR: work-done → graceful exit (empty backlog) ---------------------

def test_operator_done_on_empty_backlog_exits(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    # no backlog dir / no non-closed tasks → operator's work is done
    row = _row(sid, role="operator", window="operator", task_id=None,
               cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]


def test_operator_busy_backlog_is_left_alone(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    _write_task(data, "T-0099", "open")  # pending work → operator stays
    row = _row(sid, role="operator", window="operator", task_id=None,
               cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_operator_totest_backlog_does_not_block_exit(tmp_path, seams):
    """T-0951: ``totest`` is the HUMAN's queue (T-0944 — "to test это для меня
    уже, человека"), not the operator's to close. Before the fix this counted
    as operator-pending, which combined with ``operator_redrive`` to respawn a
    fresh operator over a totest-only board forever: each boot found nothing
    takeable, went idle/drive-off, and was replaced — zero state transitions,
    ever, since only the HUMAN can move a totest ticket."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    _write_task(data, "T-0099", "totest")
    row = _row(sid, role="operator", window="operator", task_id=None,
               cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]


def test_operator_to_accept_task_still_pending(tmp_path, seams):
    """The twin of the test above: ``to_accept`` IS the operator's own queue
    (T-0944) — accepting it into ``totest`` is operator work, so a to_accept
    backlog still blocks exit."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    _write_task(data, "T-0099", "to_accept")
    row = _row(sid, role="operator", window="operator", task_id=None,
               cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


# --- T-0468: exit-resume hint (whether/when resume beats a fresh start) ------

def test_compute_resume_hint_dev_done_discourages_resume():
    """A documented-done dev exit: resume NOT recommended (deliverable is the
    source of truth + history searchable), with a reopen-only `when`."""
    h = GE.compute_resume_hint("dev", "T-0042", "totest", "dev on T-0042 — reached totest")
    assert h["resume_recommended"] is False
    assert "discouraged" in h["reason"]
    assert "REOPENED" in h["when"]
    assert h["last_work_summary"] == "dev on T-0042 — reached totest"


def test_compute_resume_hint_operator_discourages_resume():
    """An operator empty-backlog exit: re-driven fresh from the backlog
    (operator_redrive respawns, not resumes) → resume not recommended."""
    h = GE.compute_resume_hint("operator", None, "", "operator — backlog cleared")
    assert h["resume_recommended"] is False
    assert "respawn" in h["reason"].lower()


def test_compute_resume_hint_in_flight_recommends_resume():
    """A NON-terminal status (work interrupted, deliverable doesn't capture it):
    resume DOES beat fresh — the policy is genuinely two-valued."""
    for st in ("in_progress", "open", "reopened"):
        h = GE.compute_resume_hint("dev", "T-0042", st, "s")
        assert h["resume_recommended"] is True, st
        assert "resume now" in h["when"].lower()


def test_last_work_summary_by_role():
    assert "backlog cleared" in GE._last_work_summary("operator", None, "")
    assert GE._last_work_summary("dev", "T-0042", "totest") == "dev on T-0042 — reached totest"
    assert GE._last_work_summary("dev", "~", "") == "dev — work done"


def test_maybe_exit_stamps_resume_hint_on_md(tmp_path, seams):
    """On a graceful exit the four DoD hint fields are WRITTEN to the session md
    (read back from disk — the suspend seam is stubbed, so this proves the stamp
    is a distinct step, not a side effect of suspend)."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert _exit_after_handoff(cfg, data, row, seams) is True
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["resume_recommended"] is False
    assert meta["resume_hint_reason"]
    assert meta["resume_hint_when"]
    assert meta["last_work_summary"] == "dev on T-0042 — reached totest"


def test_maybe_exit_stamp_failure_does_not_undo_exit(tmp_path, seams, monkeypatch):
    """The stamp is best-effort: if it raises, the session is still reported
    suspended (it already exited — a stamp failure must not flip the result)."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    monkeypatch.setattr(GE, "_stamp_resume_hint",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert _exit_after_handoff(cfg, data, row, seams) is True
    assert seams["calls"]["suspend"] == [sid]


# --- gates ------------------------------------------------------------------

def test_kill_switch_disables_exit(tmp_path, seams, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT", "0")
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_non_active_row_skipped(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo", status="suspended")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


# --- tick: per-project sweep ------------------------------------------------

def test_tick_exits_done_active_skips_suspended(tmp_path, seams, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    active = _row(sid, role="dev", cwd_repo=data.parent / "repo", status="active")
    dead = _row("S-almdudleer-bot-squad-old-p9", role="dev",
                cwd_repo=data.parent / "repo", status="suspended")
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: [active, dead])
    monkeypatch.setattr(S, "_get_user_home", lambda: "/home/x")
    monkeypatch.setattr(S, "_get_current_user", lambda: "almdudleer")
    GE.tick(cfg)                      # tick 1 — arms the pre-exit handoff
    assert seams["calls"]["suspend"] == []
    _write_context(data, "T-0042", "what is true now")
    GE.tick(cfg)                      # tick 2 — the write landed, exit
    assert seams["calls"]["suspend"] == [sid]


# --- T-0945: pin/attach hold the exit; the handoff never wedges -------------

def test_attached_done_session_is_not_exited(tmp_path, seams, monkeypatch):
    """Same rule for a live tmux client — the exit waits out the attachment."""
    monkeypatch.setattr(GE.recycle_gate, "is_attached", lambda target, **kw: True)
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []


def test_exit_handoff_waits_for_the_write_then_exits(tmp_path, seams):
    """The wait is real: an unchanged `## Context` inside the window keeps the
    session alive, and the mark is what distinguishes the two."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    now = time.time()
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now, user_home="/home/x") is True
    md = data / "bot-squad" / "sessions" / f"{sid}.md"
    assert S._read_session_metadata(md)["exit_handoff_phase"] == "writing"
    # nothing written yet, still inside the window → hold
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now + 60,
                         user_home="/home/x") is False
    assert seams["calls"]["suspend"] == []
    _write_context(data, "T-0042", "what is true now")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now + 61,
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]
    assert "exit_handoff_phase" not in S._read_session_metadata(md)


def test_exit_handoff_never_wedges_on_a_session_that_ignores_it(tmp_path, seams):
    """Past the bounded window with nothing written, the session exits anyway —
    the same never-wedge posture every other handoff in the system has."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    now = time.time()
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now, user_home="/home/x") is True
    late = now + A.handoff_timeout_sec() + 60
    assert GE.maybe_exit(cfg, "bot-squad", row, now=late,
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]
    assert seams["calls"]["compact"] == []   # never, on this path


def test_exit_handoff_timeout_notes_the_missing_handoff_on_the_ticket(tmp_path, seams):
    """T-1055: a handoff that timed out used to leave ONLY a `log.warning` line —
    a worker-process log nobody reads — as the record that ## Context was never
    actually written. This suspend looks identical to a clean graceful-exit
    (same `_suspend` call, same source) unless the ticket itself says otherwise."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    now = time.time()
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now, user_home="/home/x") is True
    late = now + A.handoff_timeout_sec() + 60
    assert GE.maybe_exit(cfg, "bot-squad", row, now=late,
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]
    body = (data / "bot-squad" / "backlog" / "T-0042-demo.md").read_text()
    assert "## Progress" in body
    assert sid in body
    assert "never finished writing" in body


def test_exit_handoff_write_in_time_leaves_no_death_note(tmp_path, seams):
    """The twin negative: when the handoff DOES land in time, nothing about a
    missing handoff is written to the ticket — the note is specific to the
    timeout case, not stamped on every graceful exit."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert _exit_after_handoff(cfg, data, row, seams) is True
    body = (data / "bot-squad" / "backlog" / "T-0042-demo.md").read_text()
    assert "never finished writing" not in body


def test_exit_handoff_kill_switch_restores_the_record_free_exit(tmp_path, seams,
                                                                monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_EXIT_HANDOFF", "0")
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]      # one tick, as before T-0945
    assert seams["calls"]["ctx_handoff"] == []


def test_taskless_operator_exit_asks_for_nothing(tmp_path, seams, monkeypatch):
    """The handoff is scoped to a session with a TICKET to write onto. An
    operator on an empty backlog has none — it exits in one tick, unchanged."""
    from bot_squad_worker import operator_redrive
    monkeypatch.setattr(operator_redrive, "count_pending_backlog",
                        lambda cfg, slug: 0)
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    row = _row(sid, role="operator", cwd_repo=data.parent / "repo", task_id=None)
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]
    assert seams["calls"]["ctx_handoff"] == []


# --- NO ROLE EXEMPT from recycle-on-timeout (DoD audit) ---------------------

def test_no_role_is_exempt_from_idle_recycle(tmp_path, monkeypatch):
    """idle_timeout must sweep EVERY role EXCEPT the human's own live chat (and,
    per T-0655, a drive=on operator — see below). We drive a TEAMLEAD row
    through maybe_recycle and confirm it recycles (not skipped on role).
    Guards against re-introducing a general role exemption (the old per-role
    drift.py-style scoping the uniform lifecycle removes).

    T-0564 (2026-07-04, superseding this test's original "…user-conv alike"
    claim): ``user-conversation`` IS now a deliberate, narrow exemption — the
    human's own live chat is never auto-recycled — see
    test_idle_timeout.py::test_user_conversation_role_never_recycled.

    T-0655 (2026-07-21, superseding this test's ORIGINAL operator-row subject):
    an operator with ``drive=on`` (the default) is now ALSO a deliberate,
    narrow exception — it gets a keep-alive nudge instead of being recycled;
    see test_idle_timeout.py::test_drive_on_operator_gets_keepalive_nudge_not_recycled
    and recycle_gate.operator_drive_on. This test switches its subject to a
    TEAMLEAD row (a role T-0655 does not touch at all) so it keeps guarding
    against a general/accidental role exemption without colliding with the
    now-deliberate operator carve-out. dev/TL stay non-exempt, which is what
    this test still asserts.

    T-0945 (2026-08-31) narrows the claim once more and this test now pins the
    NARROWED version: a TL is swept, but it only reaches the terminate path when
    none of its tasks is alive. This row is bound to no task at all and to no
    initiative, so it has no live work — which is exactly the shape that still
    recycles. A TL with an open ticket is nudged instead; see
    test_idle_timeout.py::test_teamlead_with_a_live_task_is_nudged_not_recycled.
    """
    compacted = []
    asked = []
    monkeypatch.setattr(A, "_pane_for", lambda sid, **kw: "%9")
    monkeypatch.setattr(A, "_capture_pane", lambda pane, **kw: "❯ \n")
    monkeypatch.setattr(A, "_send_compact", lambda sid: compacted.append(sid))
    # T-0863: the recycle ARM asks a task-LESS role (this TL) to write its
    # forward-state to its role artifact instead of sending /compact. What this
    # test guards is unchanged — that the TL is SWEPT, not exempted — so it
    # asserts on whichever action the recycle took, and still fails if the
    # retired /compact comes back.
    monkeypatch.setattr(A, "_inject_handoff",
                        lambda sid, art, role=None, *, relaunch=True, resume=False:
                        asked.append(sid))
    monkeypatch.setattr(A, "_inject_context_handoff",
                        lambda sid, task_id, *, relaunch=True, resume=False:
                        asked.append(sid))
    monkeypatch.setattr(IT, "_context_tokens", lambda cfg, slug, sid: 25000)
    monkeypatch.setattr(IT.recycle_gate, "is_attached", lambda target, **kw: False)
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - 5000.0)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    sid = "S-almdudleer-bot-squad-TL-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="TL", task_id=None)
    row = _row(sid, role="teamlead", window="TL", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert asked == [sid]  # teamlead recycled → not exempt
    assert compacted == []  # ...and never via the retired /compact (T-0863)


# --- T-0944/T-0945: to_accept is a DELIVERED status for the lifecycle -------

def test_the_three_done_sets_are_one_set():
    """graceful_exit's own comment claims these three are the same set. A
    comment is not a control — this is (T-0945). They govern three different
    decisions about the same fact ("the deliverable exists"): whether to exit
    the session, whether to respawn onto the task, whether to nag the dev about
    its DoD. A status added to one and missed in another is a session that
    exits and is immediately respawned, or one that is kept alive and nagged
    about work it already delivered."""
    from bot_squad_worker import drift, recovery
    assert set(GE.DONE_STATUSES) == set(recovery.DONE_STATUSES)
    assert set(GE.DONE_STATUSES) == set(drift._TERMINAL_TICKET_STATUSES)


def test_done_set_matches_the_task_state_machine_after_in_progress():
    """The set is pinned to the STATE MACHINE, not hand-listed: every status
    reachable from in_progress that means "the dev is finished" must be in it.
    T-0944 inserted to_accept there, and missing it would leave a delivered dev
    nudged «продолжай» forever (idle_timeout.task_alive reads this set)."""
    from bot_squad_worker import task_states
    assert "to_accept" in GE.DONE_STATUSES
    # to_accept really is a state of this graph, and really is where in_progress
    # delivers to — read from the state machine, not asserted as a literal here
    assert "to_accept" in task_states.TICKET_STATUSES
    assert "to_accept" in task_states.TRANSITIONS["in_progress"]
    # every done status is a real state of the machine
    assert set(GE.DONE_STATUSES) <= set(task_states.TICKET_STATUSES)
    # NOT asserted: disjointness from task_states.ACTIVE_STATES. That set is a
    # different axis — "the TICKET is still open on the board" — and it
    # deliberately contains to_accept and totest, both of which are delivered
    # work whose SESSION is done. Conflating the two is what would put a
    # delivered dev back on the nudge path.
    assert {"to_accept", "totest"} <= set(task_states.ACTIVE_STATES)
    # ...and the statuses that mean work is STILL LIVE stay out of DONE_STATUSES
    for still_live in ("open", "in_progress", "reopened", "paused",
                       "planned", "blocked_on_user"):
        assert still_live not in GE.DONE_STATUSES, still_live


def test_dev_at_to_accept_exits_and_is_not_nudged(tmp_path, seams):
    """The end-to-end of the coupling: a dev whose ticket is to_accept has
    delivered — graceful_exit takes it (after the ticket handoff, no compact),
    and idle_timeout stops treating its task as live work."""
    from bot_squad_worker import idle_timeout as _IT
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="to_accept")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert _IT.task_alive(cfg, "bot-squad", "T-0042") is False
    assert _exit_after_handoff(cfg, data, row, seams) is True
    assert seams["calls"]["suspend"] == [sid]
    assert seams["calls"]["compact"] == []
