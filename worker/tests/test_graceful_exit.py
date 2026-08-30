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
         cwd_repo: Path, status="active"):
    return {"sid": sid, "status": status, "window": window, "task_id": task_id,
            "role": role, "cwd": str(cwd_repo), "claude_uuid": "uuid-" + sid,
            "linux_user": ""}


@pytest.fixture
def seams(monkeypatch):
    """Stub every tmux/suspend seam so no real session is touched."""
    calls = {"suspend": []}
    state = {"pane": "%9", "buf": "❯ \n", "idle_age": 5000.0}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(GE, "_suspend",
                        lambda cfg, slug, sid: calls["suspend"].append(sid))
    # idle clock: jsonl mtime = now - idle_age
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - state["idle_age"])
    monkeypatch.delenv("BOT_SQUAD_GRACEFUL_EXIT", raising=False)
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC", "180")
    return {"calls": calls, "state": state}


# --- DEV: work-done → graceful exit -----------------------------------------

def test_dev_done_suspends_no_relaunch(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]


def test_dev_closed_also_exits(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="closed")
    row = _row(sid, role="dev", cwd_repo=data.parent / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
    assert seams["calls"]["suspend"] == [sid]


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


def test_operator_totest_task_still_pending(tmp_path, seams):
    """A totest task is DONE for the dev but still PENDING for the operator
    (it must close it) — so the operator does NOT exit yet."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    _write_task(data, "T-0099", "totest")
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
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
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
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is True
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
    GE.tick(cfg)
    assert seams["calls"]["suspend"] == [sid]


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
                        lambda sid, art, role=None, *, relaunch=True:
                        asked.append(sid))
    monkeypatch.setattr(A, "_inject_context_handoff",
                        lambda sid, task_id, *, relaunch=True: asked.append(sid))
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
