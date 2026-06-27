"""T-0237 Layer-2 v1: operator-invoked reuse-vs-spawn dispatch decision.

decide_dispatch(cfg, slug, task_id) returns whether to REUSE an idle live dev
session that has useful context (same initiative + context headroom) or SPAWN a
fresh one. Pure decision — it reads session mds + telemetry records, no tmux.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker import telemetry as T
from bot_squad_worker.actions import ActionError
from bot_squad_worker.config import Config
from bot_squad_worker.dispatch import decide_dispatch
from bot_squad_worker.sessions import _write_session_metadata


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True)
    (data_dir / "test-project" / "sessions").mkdir(parents=True)
    (data_dir / "test-project" / "vision" / "initiatives").mkdir(parents=True)
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir, tg_bot_token="")


def _make_task(cfg, task_id: str, *, initiative: str = "~") -> None:
    p = cfg.data_dir / "test-project" / "backlog" / f"{task_id}-foo.md"
    p.write_text(
        f"---\nid: {task_id}\ntitle: T\nstatus: open\ninitiative: {initiative}\n---\nbody\n"
    )


def _make_session(cfg, sid, *, window, task_id="~", initiative="~",
                  status="active", archived=None) -> Path:
    meta = {
        "sid": sid, "status": status, "window": window, "cwd": "/tmp",
        "claude_uuid": "uuid-" + sid[-3:], "task_id": task_id,
        "initiative": initiative, "started_at": "2026-05-12T00:00:00Z",
    }
    if archived is not None:
        meta["archived"] = archived
    p = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    _write_session_metadata(p, meta)
    return p


def _set_context_pct(cfg, sid, pct: float) -> None:
    T._write_json(T._record_path(cfg, "test-project", sid), {"context": {"pct": pct}})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: "/home/u")
    # default: idle (no recent transcript activity)
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: None)
    # default: no live tmux panes — keeps the live_operator_sids tmux scan
    # (T-0523) hermetic; tests that exercise it override list_panes explicitly.
    monkeypatch.setattr(S, "list_panes", lambda: [])


# --- the heuristic table ---

def test_reuse_idle_same_initiative_dev_with_headroom(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 12.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "reuse"
    assert res["target_sid"] == "S-u-d1-dev-p1"


def test_spawn_when_task_has_no_initiative(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="~")  # no initiative to match on
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"
    assert "initiative" in res["reason"]


def test_spawn_when_candidate_over_context_threshold(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 92.0)  # above the 80% warn ratio

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_spawn_when_candidate_busy(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 10.0)
    # fresh transcript activity -> busy, not idle
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: 10 ** 12)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_spawn_when_only_different_initiative(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="beta.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 10.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_interface_session_is_not_reused(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    # a TL bound to the initiative — idle, headroom — but it's an interface
    _make_session(cfg, "S-u-tl-p1", window="tl", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-tl-p1", 5.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_suspended_session_is_not_reused(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev",
                  initiative="alpha.md", status="suspended")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 10.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"


def test_decide_dispatch_skips_context_read_for_dead_sessions(tmp_path, monkeypatch):
    """T-0430: only a LIVE dev can be a reuse target, so the expensive
    _session_context_pct (disk JSON read) must be skipped for dead/non-live
    candidates. A project with N dead + 1 live session does EXACTLY 1 context
    read — not N+1 — and still reuses the live dev (behaviour unchanged)."""
    import bot_squad_worker.dispatch as D
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-live-dev-p1", window="live-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-live-dev-p1", 10.0)
    for i in range(5):
        _make_session(cfg, f"S-u-dead-p{i}", window=f"dead{i}",
                      initiative="alpha.md", status="suspended")
        # lower pct (more "headroom") but dead → must be ignored, never reused.
        _set_context_pct(cfg, f"S-u-dead-p{i}", 5.0)

    reads: list[str] = []
    real = D._session_context_pct
    monkeypatch.setattr(
        D, "_session_context_pct",
        lambda c, s, sid: (reads.append(sid), real(c, s, sid))[1],
    )
    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "reuse"
    assert res["target_sid"] == "S-u-live-dev-p1"
    assert reads == ["S-u-live-dev-p1"]  # the 5 dead sessions never get a context read


def test_tie_break_prefers_lowest_context_pct(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _make_session(cfg, "S-u-d2-dev-p2", window="d2-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 60.0)
    _set_context_pct(cfg, "S-u-d2-dev-p2", 20.0)  # more headroom -> winner

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "reuse"
    assert res["target_sid"] == "S-u-d2-dev-p2"


def test_threshold_is_env_tunable(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 50.0)
    monkeypatch.setenv("BOT_SQUAD_REUSE_MAX_CONTEXT_PCT", "40")  # 50 now over cap
    assert decide_dispatch(cfg, "test-project", "T-0009")["decision"] == "spawn"
    monkeypatch.setenv("BOT_SQUAD_REUSE_MAX_CONTEXT_PCT", "70")  # 50 now under cap
    assert decide_dispatch(cfg, "test-project", "T-0009")["decision"] == "reuse"


def test_unknown_task_raises(tmp_path):
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ActionError, match="task not found"):
        decide_dispatch(cfg, "test-project", "T-9999")


def test_missing_telemetry_record_treated_as_headroom(tmp_path):
    """A session with no telemetry record yet (brand-new) is assumed to have
    headroom (pct=0) — a fresh idle session is a fine reuse target."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", initiative="alpha.md")
    # no _set_context_pct -> no record

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "reuse"
    assert res["target_sid"] == "S-u-d1-dev-p1"


# ---------------------------------------------------------------------------
# T-0468 — the reuse-vs-spawn decision CONSUMES the exit-resume hint
# ---------------------------------------------------------------------------

def _stamp_hint(cfg, sid, *, recommended=False, reason="r", when="w", summary="s"):
    """Add a T-0468 exit-resume hint (flat keys) to an existing session md."""
    p = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    meta = S._read_session_metadata(p)
    meta["resume_recommended"] = recommended
    meta["resume_hint_reason"] = reason
    meta["resume_hint_when"] = when
    meta["last_work_summary"] = summary
    _write_session_metadata(p, meta)


def test_decide_dispatch_surfaces_resume_hint_same_initiative(tmp_path):
    """An EXITED same-initiative dev with a hint is surfaced in resume_hints, so
    the operator can state resume-vs-fresh — the DoD 'consumes the hint'."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev",
                  initiative="alpha.md", status="suspended")
    _stamp_hint(cfg, "S-u-d1-dev-p1", summary="dev on T-0001 — reached totest")

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert len(res["resume_hints"]) == 1
    h = res["resume_hints"][0]
    assert h["sid"] == "S-u-d1-dev-p1"
    assert h["initiative_match"] is True
    assert h["resume_recommended"] is False
    assert h["last_work_summary"] == "dev on T-0001 — reached totest"


def test_decide_dispatch_hint_held_task_surfaced_even_when_skipped_as_candidate(tmp_path):
    """A suspended session that RAN this exact task (e.g. it was reopened) is the
    most relevant resume target — its hint must surface even though the session
    is skipped as a reuse candidate (it 'holds' the task)."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0009",
                  initiative="alpha.md", status="suspended")
    _stamp_hint(cfg, "S-u-d1-dev-p1")

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert [h["sid"] for h in res["resume_hints"]] == ["S-u-d1-dev-p1"]
    assert res["resume_hints"][0]["held_task"] is True


def test_decide_dispatch_recommended_resume_hint_steers_spawn_reason(tmp_path):
    """When no live reuse target exists but an exited session RECOMMENDS resume
    (work in flight), the spawn reason points the operator at --resume."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev",
                  initiative="alpha.md", status="suspended")
    _stamp_hint(cfg, "S-u-d1-dev-p1", recommended=True)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["decision"] == "spawn"
    assert "S-u-d1-dev-p1" in res["reason"]
    assert "--resume" in res["reason"]


def test_decide_dispatch_ignores_hint_on_different_initiative(tmp_path):
    """An exited session on a DIFFERENT initiative is not relevant to this task —
    its hint is not surfaced."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev",
                  initiative="beta.md", status="suspended")
    _stamp_hint(cfg, "S-u-d1-dev-p1")

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["resume_hints"] == []


def test_decide_dispatch_ignores_hint_on_live_session(tmp_path):
    """A LIVE session is handled by the reuse heuristic, not the resume-hint path
    (the hint is about EXITED sessions). A stray hint on a live md is ignored."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev",
                  initiative="alpha.md", status="active")
    _stamp_hint(cfg, "S-u-d1-dev-p1")
    _set_context_pct(cfg, "S-u-d1-dev-p1", 10.0)

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["resume_hints"] == []
    assert res["decision"] == "reuse"  # still a valid live reuse target


def test_decide_dispatch_no_hint_field_absent(tmp_path):
    """A session md with no hint stamped contributes nothing to resume_hints."""
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0009", initiative="alpha.md")
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev",
                  initiative="alpha.md", status="suspended")

    res = decide_dispatch(cfg, "test-project", "T-0009")
    assert res["resume_hints"] == []


# ---------------------------------------------------------------------------
# T-0472 — operator as transient dispatcher: standing task + one-per-project
# ---------------------------------------------------------------------------

from bot_squad_worker.dispatch import live_operator_sids, operator_standing_task


def test_operator_standing_task_is_backlog_clearing_directive():
    """The standing task (clarification-03) must be a backlog-clearing directive
    that does NOT rely on user input, and must name the orchestration
    constraints (parallelism + token/quota)."""
    txt = operator_standing_task().lower()
    assert "backlog" in txt
    assert "parallel" in txt
    assert "token" in txt or "quota" in txt
    # user-facing but NOT user-reliant
    assert "wait" in txt or "not" in txt


def test_live_operator_sids_finds_live_operator(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-operator-p1", window="operator")
    assert live_operator_sids(cfg, "test-project") == ["S-u-operator-p1"]


def test_live_operator_sids_window_suffix_match(tmp_path):
    """A ``<x>-operator`` window also derives the operator role."""
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-demo-operator-p2", window="demo-operator")
    assert live_operator_sids(cfg, "test-project") == ["S-u-demo-operator-p2"]


def test_live_operator_sids_excludes_dev_and_tl(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-d1-dev-p1", window="d1-dev")
    _make_session(cfg, "S-u-x-tl-p2", window="x-tl")
    assert live_operator_sids(cfg, "test-project") == []


def test_live_operator_sids_excludes_archived_and_suspended(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-op-archived-p1", window="operator",
                  status="active", archived="true")
    _make_session(cfg, "S-u-op-suspended-p2", window="operator",
                  status="suspended")
    assert live_operator_sids(cfg, "test-project") == []


def test_live_operator_sids_includes_paused(tmp_path):
    """A paused operator is still a live holder — it counts toward the
    one-per-project invariant (a re-drive should continue it, not duplicate)."""
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-operator-p1", window="operator", status="paused")
    assert live_operator_sids(cfg, "test-project") == ["S-u-operator-p1"]


def test_live_operator_sids_empty_when_no_sessions_dir(tmp_path):
    cfg = _make_cfg(tmp_path)
    import shutil
    shutil.rmtree(cfg.data_dir / "test-project" / "sessions")
    assert live_operator_sids(cfg, "test-project") == []


# --- T-0523: the canonical/unregistered operator (live pane, NO session md) --
# The canonical operator p5 runs in a live `bot-squad-operator` window but has no
# session md (predates spawn-registration). live_operator_sids must ALSO scan
# live claude panes (scoped to the project's tmux session) via the same
# `_derive_role` SSOT, else the re-drive gate misfires and spawns a duplicate.

def _pane(window, *, pane_id="%5", pid="999", session="test-project"):
    return S.PaneInfo(pane_id=pane_id, window=window, pid=pid, cwd="/tmp",
                      command="claude", session=session)


def test_live_operator_sids_finds_unregistered_canonical_operator_via_tmux(tmp_path, monkeypatch):
    """A live `bot-squad-operator` window with NO session md is recognized."""
    cfg = _make_cfg(tmp_path)  # no session mds written
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("bot-squad-operator")])
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda *a, **k: True)
    monkeypatch.setattr(S, "_proc_children_map", lambda: {})
    assert live_operator_sids(cfg, "test-project") == ["S-u-bot-squad-operator-p5"]


def test_live_operator_sids_tmux_scan_both_window_namings(tmp_path, monkeypatch):
    """Both the canonical `bot-squad-operator` and the newer `operator` naming
    derive the operator role via the one identity SSOT."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("operator", pane_id="%9")])
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda *a, **k: True)
    monkeypatch.setattr(S, "_proc_children_map", lambda: {})
    assert live_operator_sids(cfg, "test-project") == ["S-u-operator-p9"]


def test_live_operator_sids_tmux_scan_scoped_to_project_session(tmp_path, monkeypatch):
    """An operator pane in ANOTHER project's tmux session must not leak in."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes",
                        lambda: [_pane("operator", session="other-project")])
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda *a, **k: True)
    monkeypatch.setattr(S, "_proc_children_map", lambda: {})
    assert live_operator_sids(cfg, "test-project") == []


def test_live_operator_sids_tmux_scan_ignores_dead_claude_pane(tmp_path, monkeypatch):
    """A pane whose claude process has exited (fell back to a shell) is not a
    live operator."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("operator")])
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda *a, **k: False)
    monkeypatch.setattr(S, "_proc_children_map", lambda: {})
    assert live_operator_sids(cfg, "test-project") == []


def test_live_operator_sids_tmux_scan_ignores_non_operator_window(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("feature-x")])
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda *a, **k: True)
    monkeypatch.setattr(S, "_proc_children_map", lambda: {})
    assert live_operator_sids(cfg, "test-project") == []


def test_live_operator_sids_dedups_md_and_tmux(tmp_path, monkeypatch):
    """A registered operator that ALSO has a live pane is returned exactly once."""
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-operator-p1", window="operator")
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("operator", pane_id="%1")])
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda *a, **k: True)
    monkeypatch.setattr(S, "_proc_children_map", lambda: {})
    assert live_operator_sids(cfg, "test-project") == ["S-u-operator-p1"]


# --- spawn-time enforcement of exactly-one-operator (guard in actions) ------

def test_spawn_session_blocks_second_operator(tmp_path, monkeypatch):
    """``spawn_session`` for an operator window is refused when a live operator
    already holds the project — exactly one operator per project (T-0472)."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-operator-p1", window="operator")

    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    called = {"spawn": False}
    monkeypatch.setattr(S, "spawn", lambda *a, **k: called.__setitem__("spawn", True))

    with pytest.raises(ActionError, match="operator already running"):
        A._action_spawn_session({"slug": "test-project", "window": "operator"})
    assert called["spawn"] is False  # guard fired BEFORE reaching the spawn


def test_spawn_session_allows_first_operator(tmp_path, monkeypatch):
    """With no live operator, an operator spawn proceeds to ``sessions.spawn``."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)

    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "spawn", lambda *a, **k: {"ok": True, "sid": "S-new"})

    res = A._action_spawn_session({"slug": "test-project", "window": "operator"})
    assert res == {"ok": True, "sid": "S-new"}


def test_spawn_session_dev_unaffected_by_operator_guard(tmp_path, monkeypatch):
    """A dev spawn is never blocked by the operator guard, even with a live
    operator present."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)
    _make_session(cfg, "S-u-operator-p1", window="operator")

    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "spawn", lambda *a, **k: {"ok": True, "sid": "S-dev"})

    res = A._action_spawn_session({"slug": "test-project", "window": "feature-x"})
    assert res == {"ok": True, "sid": "S-dev"}


# --- T-0523: operator-dispatches-only — an operator spawn must not bind a dev
# ticket (the operator orchestrates and spawns devs; voice-03). -------------

def test_spawn_session_operator_rejects_dev_task_bind(tmp_path, monkeypatch):
    """An operator-window spawn carrying a dev ``task_id`` is refused at the
    spawn seam — the operator orchestrates, it never self-claims a dev ticket."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    called = {"spawn": False}
    monkeypatch.setattr(S, "spawn", lambda *a, **k: called.__setitem__("spawn", True))

    with pytest.raises(ActionError, match="must not bind a dev task"):
        A._action_spawn_session(
            {"slug": "test-project", "window": "operator", "task_id": "T-0465"})
    assert called["spawn"] is False  # guard fired BEFORE reaching the spawn


def test_spawn_session_operator_placeholder_task_id_allowed(tmp_path, monkeypatch):
    """The ``~`` unset sentinel is not a real bind — an operator spawn carrying
    it proceeds (it is just the registry's absent marker)."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "spawn", lambda *a, **k: {"ok": True, "sid": "S-op"})

    res = A._action_spawn_session(
        {"slug": "test-project", "window": "operator", "task_id": "~"})
    assert res == {"ok": True, "sid": "S-op"}


def test_spawn_session_dev_with_task_id_unaffected(tmp_path, monkeypatch):
    """A dev spawn WITH a ``task_id`` is normal — the operator-only guard must
    not touch it."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "spawn", lambda *a, **k: {"ok": True, "sid": "S-dev"})

    res = A._action_spawn_session(
        {"slug": "test-project", "window": "feature-x", "task_id": "T-0465"})
    assert res == {"ok": True, "sid": "S-dev"}
