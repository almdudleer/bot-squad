"""T-0522 (Process Paradigm M2, follow-up to T-0474): worker actions that expose
the operator re-drive pause flag as a user-facing toggle.

Promotes the manual walkthrough in
``data/bot-squad/scenarios/T-0522-...md`` (written + walked first, per T-0158):
``operator_pause`` writes the flag, ``operator_resume`` clears it (both
idempotent), and ``operator_status`` reports paused-vs-driving — all thin
wrappers over the EXISTING ``operator_redrive`` helpers (no reimplementation).

Reuses the ``cfg_slug`` fixture machinery from ``test_operator_redrive`` style
(temp Config + project), and mocks ``actions._get_config`` so the dispatcher
reads the temp config. ``dispatch.live_operator_sids`` is mocked to control the
driving-vs-pending state.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bot_squad_worker.actions as A
from bot_squad_worker import operator_redrive as ord_
from bot_squad_worker import dispatch
from bot_squad_worker.actions import ActionError, dispatch as act_dispatch
from tests.test_jobs import _make_config_with_project, _make_project_with_repo


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug / "backlog").mkdir(parents=True, exist_ok=True)
    # The dispatcher resolves config via actions._get_config.
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    # Default: no operator currently live (so status reports pending/idle, not
    # driving). The driving test overrides this.
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: [])
    return cfg, slug


def _write_task(cfg, slug, tid="T-1", *, status="open"):
    (cfg.data_dir / slug / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: x\nstatus: {status}\n---\n\nbody\n"
    )


# --- DoD: action toggles the flag -------------------------------------------

def test_operator_pause_writes_flag(cfg_slug):
    cfg, slug = cfg_slug
    assert ord_.is_paused(cfg, slug) is False

    out = act_dispatch("operator_pause", {"slug": slug, "reason": "away"})

    assert out["ok"] is True
    assert out["was_already_paused"] is False
    assert out["paused"]["reason"] == "away"
    # The REAL flag is on disk — re-drive will see it.
    assert ord_.is_paused(cfg, slug) is True


def test_operator_pause_is_idempotent(cfg_slug):
    cfg, slug = cfg_slug
    act_dispatch("operator_pause", {"slug": slug})
    out = act_dispatch("operator_pause", {"slug": slug})
    assert out["was_already_paused"] is True
    assert ord_.is_paused(cfg, slug) is True


def test_operator_resume_clears_flag(cfg_slug):
    cfg, slug = cfg_slug
    act_dispatch("operator_pause", {"slug": slug})
    out = act_dispatch("operator_resume", {"slug": slug})
    assert out["ok"] is True
    assert out["was_paused"] is True
    assert ord_.is_paused(cfg, slug) is False


def test_operator_resume_noop_when_not_paused(cfg_slug):
    cfg, slug = cfg_slug
    out = act_dispatch("operator_resume", {"slug": slug})
    assert out["was_paused"] is False


# --- DoD: status shows paused-vs-driving ------------------------------------

def test_operator_status_paused(cfg_slug):
    cfg, slug = cfg_slug
    act_dispatch("operator_pause", {"slug": slug})
    out = act_dispatch("operator_status", {"slug": slug})
    assert out["paused"] is True
    assert out["state"] == "paused"


def test_operator_status_driving_when_operator_live(cfg_slug, monkeypatch):
    cfg, slug = cfg_slug
    _write_task(cfg, slug)
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: ["S-op-1"])
    out = act_dispatch("operator_status", {"slug": slug})
    assert out["paused"] is False
    assert out["state"] == "driving"
    assert out["live_operators"] == ["S-op-1"]


def test_operator_status_pending_redrive_when_backlog_no_operator(cfg_slug):
    cfg, slug = cfg_slug
    _write_task(cfg, slug)
    out = act_dispatch("operator_status", {"slug": slug})
    assert out["paused"] is False
    assert out["state"] == "pending-redrive"
    assert out["pending_backlog"] == 1


def test_operator_status_idle_empty_backlog(cfg_slug):
    cfg, slug = cfg_slug  # no tasks written
    out = act_dispatch("operator_status", {"slug": slug})
    assert out["state"] == "idle-empty-backlog"
    assert out["pending_backlog"] == 0


# --- Strict param contract + unknown-slug guard -----------------------------

def test_operator_pause_rejects_unexpected_params(cfg_slug):
    cfg, slug = cfg_slug
    with pytest.raises(ActionError, match="unexpected"):
        act_dispatch("operator_pause", {"slug": slug, "bogus": 1})


def test_operator_status_unknown_slug(cfg_slug):
    with pytest.raises(ActionError, match="unknown project slug"):
        act_dispatch("operator_status", {"slug": "no-such-proj"})


# --- T-0855: "no operator yet" vs "no operator on purpose" -------------------

def _write_attendant(cfg, slug, sid="S-u-gu_x-user-conversation-p9"):
    from bot_squad_worker import sessions as S
    (cfg.data_dir / slug / "sessions").mkdir(parents=True, exist_ok=True)
    S._write_session_metadata(
        cfg.data_dir / slug / "sessions" / f"{sid}.md",
        {"sid": sid, "status": "active", "window": "gu_x-user-conversation",
         "cwd": "/tmp", "claude_uuid": "uuid-uc", "task_id": "~",
         "initiative": "~", "started_at": "2026-08-11T00:00:00Z"},
    )


def test_operator_status_direct_tier_is_not_reported_as_pending(cfg_slug):
    """Same board, same empty operator roster, opposite meaning: with a user
    session driving a small flow the absence of an operator is the design, and
    calling it `pending-redrive` reads as a project stuck waiting on a spawn."""
    cfg, slug = cfg_slug
    _write_task(cfg, slug)
    _write_attendant(cfg, slug)

    out = act_dispatch("operator_status", {"slug": slug})

    assert out["state"] == "direct-tier"
    assert out["pending_backlog"] == 1
    assert out["live_operators"] == []
    assert out["topology"]["operator_needed"] is False
    assert out["topology"]["attending_user_session_sids"] == [
        "S-u-gu_x-user-conversation-p9"]


def test_operator_status_still_says_pending_when_nobody_is_driving(cfg_slug):
    """The negative control for the arm above — remove the attendant and the
    pre-T-0855 answer must come back, or the new state would mask a real stall."""
    cfg, slug = cfg_slug
    _write_task(cfg, slug)

    out = act_dispatch("operator_status", {"slug": slug})

    assert out["state"] == "pending-redrive"
    assert out["topology"]["operator_needed"] is True


def test_operator_status_survives_a_broken_topology_read(cfg_slug, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("gate exploded")

    cfg, slug = cfg_slug
    _write_task(cfg, slug)
    _write_attendant(cfg, slug)
    monkeypatch.setattr(dispatch, "decide_topology", _boom)

    out = act_dispatch("operator_status", {"slug": slug})

    assert out["state"] == "pending-redrive"      # the pre-T-0855 answer
    assert "topology" not in out


# --- T-0943: "driving" means WHOEVER HOLDS the role, not only a dedicated one -

def _write_session_md(cfg, slug, sid, *, window, roles=None, status="active"):
    d = cfg.data_dir / slug / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"sid: {sid}", f"status: {status}", "task_id: ~",
             f"window: {window}"]
    if roles is not None:
        lines.append("roles: [" + ", ".join(roles) + "]")
    lines += ["---", ""]
    (d / f"{sid}.md").write_text("\n".join(lines))


def test_operator_status_reports_driving_for_a_multi_role_operator(cfg_slug):
    """T-0943: this read is what a HUMAN uses to decide whether to spawn an
    operator, so reporting "no live operator" about a session that is driving
    the board produces a correct-looking human action — a second dispatcher.

    Note the fixture still stubs `live_operator_sids` to [], which is the whole
    point: the DEDICATED-operator singleton genuinely does not see this session,
    and the status action must not be asking it.
    """
    cfg, slug = cfg_slug
    _write_task(cfg, slug)
    _write_session_md(cfg, slug, "S-u-operator-p1", window="operator",
                      roles=["operator", "user-conversation"])
    out = act_dispatch("operator_status", {"slug": slug})
    assert out["state"] == "driving"
    assert out["live_operators"] == ["S-u-operator-p1"]


def test_operator_status_still_reports_pending_redrive_with_no_role_holder(
        cfg_slug):
    """NEGATIVE CONTROL: a board whose only session is a dev still reports
    pending-redrive. The widening is to WHOEVER HOLDS THE OPERATOR ROLE, not to
    'any live session', and without this arm the test above would also pass if
    the report had been widened into meaninglessness."""
    cfg, slug = cfg_slug
    _write_task(cfg, slug)
    _write_session_md(cfg, slug, "S-u-dev_thing-p9", window="dev_thing")
    out = act_dispatch("operator_status", {"slug": slug})
    assert out["state"] == "pending-redrive"
    assert out["live_operators"] == []
