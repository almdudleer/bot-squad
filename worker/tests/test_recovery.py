"""Crash recovery for ungraceful exits — dead/orphaned session reconcile.

T-0251 (WS-4 S4) shipped the dev-only slice. T-0471 (Process Paradigm M1/F1.8)
generalizes it: EVERY role is recovered, a BOOT-TIME reconcile pass runs on
worker startup, and a crashed role is re-driven from its ROLE ARTIFACT + task
state via the graceful-compact reload path (NOT from lost in-context work).

Crash signature = md ``status: active`` + DEAD pane. A gracefully-suspended
session (``status: suspended``) is intent, not a crash, and is left alone — that
distinction is what makes recovery safe to run alongside the graceful compact.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import recovery as R

ACTIVE = "in_progress"
DONE = "closed"


def _cfg(tmp_path):
    # T-0563: recycle_projects=("p1",) so these pre-existing tests (which all use
    # slug "p1") stay allowlisted under the new per-project gate — the gate
    # itself is covered separately in test_recycle_gate.py.
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=tmp_path / "data",
                                 recycle_projects=("p1",))


# --- kill-switches ---------------------------------------------------------

def test_recovery_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_RECOVERY", raising=False)
    assert R.recovery_enabled() is False  # periodic tick: opt-in for a live run


def test_recovery_enabled_when_set(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_RECOVERY", "1")
    assert R.recovery_enabled() is True


def test_boot_reconcile_enabled_by_default(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_BOOT_RECONCILE", raising=False)
    assert R.boot_reconcile_enabled() is True  # crash recovery on restart = default ON


def test_boot_reconcile_disabled_when_zero(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_BOOT_RECONCILE", "0")
    assert R.boot_reconcile_enabled() is False


# --- pure classify (role-agnostic now) -------------------------------------

def test_classify_live_pane_is_none():
    # live work is never touched here (idle-wedge SIGTERM is a separate slice)
    assert R.classify(pane_live=True, task_status=ACTIVE, has_artifact=True,
                      respawn_count=0, bound=2) == "none"


def test_classify_dead_pane_active_task_respawns():
    assert R.classify(pane_live=False, task_status=ACTIVE, has_artifact=False,
                      respawn_count=0, bound=2) == "respawn"
    assert R.classify(pane_live=False, task_status="reopened", has_artifact=False,
                      respawn_count=1, bound=2) == "respawn"


def test_classify_dead_pane_artifact_no_task_respawns():
    # an operator/TL with no task but a written role artifact still recovers
    assert R.classify(pane_live=False, task_status="", has_artifact=True,
                      respawn_count=0, bound=2) == "respawn"


def test_classify_dead_pane_respawn_bound_parks():
    assert R.classify(pane_live=False, task_status=ACTIVE, has_artifact=False,
                      respawn_count=2, bound=2) == "park"


def test_classify_dead_pane_done_task_is_none():
    # deliverable exists → leave to stale-archive, even with an artifact present
    assert R.classify(pane_live=False, task_status=DONE, has_artifact=True,
                      respawn_count=0, bound=2) == "none"
    assert R.classify(pane_live=False, task_status="totest", has_artifact=False,
                      respawn_count=0, bound=2) == "none"


def test_classify_dead_pane_no_state_is_none():
    # never started + nothing written → nothing in-flight to recover
    assert R.classify(pane_live=False, task_status="planned", has_artifact=False,
                      respawn_count=0, bound=2) == "none"
    assert R.classify(pane_live=False, task_status="", has_artifact=False,
                      respawn_count=0, bound=2) == "none"


# --- gather: crash signature + artifact resolution -------------------------

def _seed(tmp_path, sid, *, status, role, task_id="~", pane_id="%1"):
    from bot_squad_worker import sessions as S
    sess = tmp_path / "data" / "p1" / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    S._write_session_metadata(sess / f"{sid}.md", {
        "sid": sid, "status": status, "window": role, "task_id": task_id,
        "initiative": "~", "role": role, "pane_id": pane_id})


def test_gather_selects_crashed_skips_graceful_and_live(monkeypatch, tmp_path):
    """_gather picks only the crash signature (md active + dead pane), across all
    roles; a suspended (graceful) or live session is excluded."""
    from bot_squad_worker import sessions as S
    cfg = _cfg(tmp_path)
    backlog = tmp_path / "data" / "p1" / "backlog"; backlog.mkdir(parents=True)
    artifacts = tmp_path / "data" / "p1" / "artifacts"; artifacts.mkdir(parents=True)

    _seed(tmp_path, "S-u-crashed-p1", status="active", role="dev", task_id="T-1")
    (backlog / "T-1.md").write_text("---\nid: T-1\nstatus: in_progress\n---\n# f\n")
    (artifacts / "T-1.md").write_text("# forward-state\n")
    _seed(tmp_path, "S-u-op-p2", status="active", role="operator", pane_id="%2")
    (artifacts / "operator-state.md").write_text("# op state\n")
    _seed(tmp_path, "S-u-graceful-p3", status="suspended", role="dev",
          task_id="T-1", pane_id="%3")
    _seed(tmp_path, "S-u-live-p4", status="active", role="dev", task_id="T-1",
          pane_id="%4")

    # only the live session has a live pane
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {"S-u-live-p4": "%4"})

    rows = {r["sid"]: r for r in R._gather(cfg)}
    # graceful (suspended) is excluded outright; the live one is gathered but
    # flagged pane_live (classify then leaves it alone) — gather is the single
    # status filter, classify is the single liveness gate.
    assert "S-u-graceful-p3" not in rows
    assert set(rows) == {"S-u-crashed-p1", "S-u-op-p2", "S-u-live-p4"}
    assert rows["S-u-live-p4"]["pane_live"] is True
    assert rows["S-u-crashed-p1"]["pane_live"] is False
    assert rows["S-u-crashed-p1"]["task_status"] == "in_progress"
    assert rows["S-u-crashed-p1"]["has_artifact"] is True
    assert rows["S-u-crashed-p1"]["artifact_path"].endswith("/artifacts/T-1.md")
    # operator (no task) resolves to its state-doc artifact
    assert rows["S-u-op-p2"]["has_artifact"] is True
    assert rows["S-u-op-p2"]["artifact_path"].endswith("/artifacts/operator-state.md")


def test_gather_no_artifact_when_file_absent(monkeypatch, tmp_path):
    from bot_squad_worker import sessions as S
    cfg = _cfg(tmp_path)
    backlog = tmp_path / "data" / "p1" / "backlog"; backlog.mkdir(parents=True)
    _seed(tmp_path, "S-u-c-p1", status="active", role="dev", task_id="T-9")
    (backlog / "T-9.md").write_text("---\nid: T-9\nstatus: in_progress\n---\n# f\n")
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    rows = R._gather(cfg)
    assert len(rows) == 1
    assert rows[0]["has_artifact"] is False  # no artifact written yet


# --- T-0563/T-0564: recycle-v2 gates in _gather -----------------------------

def test_gather_skips_non_allowlisted_project(monkeypatch, tmp_path):
    """T-0563: a crashed session in a non-allowlisted project is never
    gathered — never respawned (watchrobot, the 2026-06-29 incident's project,
    joined the default allowlist in T-0613, so the example is another slug)."""
    from bot_squad_worker import sessions as S
    import types as _types
    # default allowlist — this cfg has NO recycle_projects override, unlike
    # the module's shared _cfg() helper.
    cfg = _types.SimpleNamespace(projects={"lim-finance": object()},
                                 data_dir=tmp_path / "data")
    sess = tmp_path / "data" / "lim-finance" / "sessions"; sess.mkdir(parents=True)
    S._write_session_metadata(sess / "S-u-wr-p1.md", {
        "sid": "S-u-wr-p1", "status": "active", "window": "dev", "task_id": "~",
        "initiative": "~", "role": "dev"})
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    assert R._gather(cfg) == []


def test_gather_skips_user_conversation_role(monkeypatch, tmp_path):
    """T-0564: a crashed user-conversation session is never gathered/respawned
    — the human's own chat is never touched by any recycle path."""
    cfg = _cfg(tmp_path)
    from bot_squad_worker import sessions as S
    _seed(tmp_path, "S-u-userconv-p1", status="active", role="user-conversation")
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    rows = R._gather(cfg)
    assert "S-u-userconv-p1" not in {r["sid"] for r in rows}


# --- T-0618: standing-need + stale-age guards (the 14:14:46 incident) --------

def test_classify_stale_dead_pane_archives_not_respawns():
    """T-0618: a crashed md whose last sign of life predates the cutoff is
    ABANDONED, not a restart casualty — archived, never respawned (and never
    parked: parking a weeks-dead md just pages the operator about garbage)."""
    assert R.classify(pane_live=False, task_status=ACTIVE, has_artifact=False,
                      respawn_count=0, bound=2, stale=True) == "archive"
    # the artifact-only path (p361's shape — operator, no task) too
    assert R.classify(pane_live=False, task_status="", has_artifact=True,
                      respawn_count=0, bound=2, stale=True) == "archive"
    # stale overrides the park branch as well
    assert R.classify(pane_live=False, task_status=ACTIVE, has_artifact=False,
                      respawn_count=2, bound=2, stale=True) == "archive"


def test_classify_stale_leaves_non_recoverable_alone():
    """stale only redirects a would-be respawn/park; a row recovery would
    never have acted on stays none (no new write surface)."""
    assert R.classify(pane_live=False, task_status=DONE, has_artifact=True,
                      respawn_count=0, bound=2, stale=True) == "none"
    assert R.classify(pane_live=False, task_status="", has_artifact=False,
                      respawn_count=0, bound=2, stale=True) == "none"
    assert R.classify(pane_live=True, task_status=ACTIVE, has_artifact=True,
                      respawn_count=0, bound=2, stale=True) == "none"


def test_gather_skips_operator_paused_project(monkeypatch, tmp_path):
    """T-0618 standing-need gate: a project whose operator re-drive is user-
    PAUSED has no standing need — recovery skips it wholesale (no gather, no
    respawn, no archive; a paused project is never written to). watchrobot was
    paused at incident time; only the recycle allowlist was consulted."""
    from bot_squad_worker import operator_redrive as OR
    from bot_squad_worker import sessions as S
    cfg = _cfg(tmp_path)
    backlog = tmp_path / "data" / "p1" / "backlog"; backlog.mkdir(parents=True)
    _seed(tmp_path, "S-u-crashed-p1", status="active", role="dev", task_id="T-1")
    (backlog / "T-1.md").write_text("---\nid: T-1\nstatus: in_progress\n---\n# f\n")
    OR.pause(cfg, "p1", by="user", reason="T-0618 test")
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    assert R._gather(cfg) == []


def _age_md(tmp_path, sid, *, days):
    import os as _os, time as _time
    md = tmp_path / "data" / "p1" / "sessions" / f"{sid}.md"
    old = _time.time() - days * 86400
    _os.utime(md, (old, old))
    return md


def test_boot_reconcile_incident_repro_stale_md_archived_not_respawned(
        monkeypatch, tmp_path):
    """THE incident shape (T-0618 DoD): stale `active` md (dead for days) on an
    allowlisted project + worker boot -> NO spawn; the md is archived instead.
    p361's exact shape: operator, no task, role artifact present."""
    from bot_squad_worker import sessions as S
    from bot_squad_worker import autocompact as A
    monkeypatch.delenv("BOT_SQUAD_RECOVERY_STALE_SEC", raising=False)
    cfg = _cfg(tmp_path)
    artifacts = tmp_path / "data" / "p1" / "artifacts"; artifacts.mkdir(parents=True)
    _seed(tmp_path, "S-u-op-p361", status="active", role="operator")
    (artifacts / "operator-state.md").write_text("# op forward-state\n")
    _age_md(tmp_path, "S-u-op-p361", days=5)

    spawns, archives = [], []
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    monkeypatch.setattr(S, "spawn", lambda *a, **k: spawns.append(a) or {"ok": True})
    monkeypatch.setattr(A, "_relaunch_from_artifact",
                        lambda *a, **k: spawns.append(a) or {"ok": True})
    monkeypatch.setattr(S, "archive_session",
                        lambda cfg, slug, sid: archives.append(sid) or {"ok": True})

    out = R.boot_reconcile(cfg)

    assert spawns == []                                   # NO spawn — the DoD
    assert out["acted"] == [("stale-archive", "S-u-op-p361")]
    assert archives == ["S-u-op-p361"]                    # defused, not re-driven


def test_gather_fresh_hook_marker_beats_stale_md_mtime(monkeypatch, tmp_path):
    """The stale clock reads the NEWEST sign of life: an old md mtime with a
    fresh T-0470 Stop marker (long-running session that never re-fired its
    SessionStart hook) is NOT stale — legit crash recovery must still run."""
    from bot_squad_worker import sessions as S
    from bot_squad_worker import lifecycle_events as LE
    monkeypatch.delenv("BOT_SQUAD_RECOVERY_STALE_SEC", raising=False)
    cfg = _cfg(tmp_path)
    backlog = tmp_path / "data" / "p1" / "backlog"; backlog.mkdir(parents=True)
    (backlog / "T-1.md").write_text("---\nid: T-1\nstatus: in_progress\n---\n# f\n")
    cwd = tmp_path / "cwd"; cwd.mkdir()
    sess = tmp_path / "data" / "p1" / "sessions"; sess.mkdir(parents=True, exist_ok=True)
    S._write_session_metadata(sess / "S-u-c-p1.md", {
        "sid": "S-u-c-p1", "status": "active", "window": "dev", "task_id": "T-1",
        "initiative": "~", "role": "dev", "cwd": str(cwd)})
    _age_md(tmp_path, "S-u-c-p1", days=5)
    LE.touch_marker(str(cwd), "S-u-c-p1", LE.MARKER_STOP)  # fresh sign of life

    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    rows = R._gather(cfg)
    assert len(rows) == 1
    assert rows[0]["stale"] is False


def test_gather_stale_cutoff_disabled_by_env(monkeypatch, tmp_path):
    """BOT_SQUAD_RECOVERY_STALE_SEC<=0 disables the cutoff (operator opt-out)."""
    from bot_squad_worker import sessions as S
    monkeypatch.setenv("BOT_SQUAD_RECOVERY_STALE_SEC", "0")
    cfg = _cfg(tmp_path)
    backlog = tmp_path / "data" / "p1" / "backlog"; backlog.mkdir(parents=True)
    (backlog / "T-1.md").write_text("---\nid: T-1\nstatus: in_progress\n---\n# f\n")
    _seed(tmp_path, "S-u-c-p1", status="active", role="dev", task_id="T-1")
    _age_md(tmp_path, "S-u-c-p1", days=30)
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    rows = R._gather(cfg)
    assert len(rows) == 1
    assert rows[0]["stale"] is False


def test_boot_reconcile_fresh_crash_on_unpaused_project_still_respawns(
        monkeypatch, tmp_path):
    """Regression guard: the T-0471 first-class guarantee is intact — a FRESH
    crash (md touched just before the restart) on an un-paused allowlisted
    project is still re-driven."""
    from bot_squad_worker import sessions as S
    from bot_squad_worker import autocompact as A
    monkeypatch.delenv("BOT_SQUAD_RECOVERY_STALE_SEC", raising=False)
    cfg = _cfg(tmp_path)
    artifacts = tmp_path / "data" / "p1" / "artifacts"; artifacts.mkdir(parents=True)
    _seed(tmp_path, "S-u-op-p2", status="active", role="operator")
    (artifacts / "operator-state.md").write_text("# op forward-state\n")

    relaunches = []
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})
    monkeypatch.setattr(A, "_relaunch_from_artifact",
                        lambda cfg, slug, row, art: relaunches.append(row["sid"]) or {"ok": True})
    monkeypatch.setattr(S, "archive_session", lambda *a, **k: {"ok": True})

    out = R.boot_reconcile(cfg)
    assert out["acted"] == [("respawn", "S-u-op-p2")]
    assert relaunches == ["S-u-op-p2"]


# --- tick dispatch ---------------------------------------------------------

def test_tick_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_SQUAD_RECOVERY", raising=False)
    called = []
    monkeypatch.setattr(R, "_gather", lambda cfg, now=None: [{"sid": "S-d-p1", "slug": "p1",
        "role": "dev", "pane_live": False, "task_id": "T-1", "task_status": ACTIVE,
        "has_artifact": False}])
    monkeypatch.setattr(R, "_do_respawn", lambda *a, **k: called.append("respawn"))
    out = R.recovery_tick(_cfg(tmp_path))
    assert called == []
    assert out["enabled"] is False


def test_tick_routes_respawn_then_park(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_SQUAD_RECOVERY", "1")
    monkeypatch.setenv("BOT_SQUAD_RESPAWN_MAX", "1")
    cfg = _cfg(tmp_path)
    rows = [{"sid": "S-d-p1", "slug": "p1", "role": "dev", "pane_live": False,
             "task_id": "T-1", "task_status": ACTIVE, "has_artifact": False}]
    monkeypatch.setattr(R, "_gather", lambda c, now=None: rows)
    actions = []
    monkeypatch.setattr(R, "_do_respawn", lambda c, row: actions.append(("respawn", row["sid"])))
    monkeypatch.setattr(R, "_do_park", lambda c, row, reason: actions.append(("park", row["sid"])))

    R.recovery_tick(cfg)  # count 0 < bound 1 -> respawn (count -> 1)
    R.recovery_tick(cfg)  # count 1 >= bound 1 -> park
    assert actions == [("respawn", "S-d-p1"), ("park", "S-d-p1")]


def test_run_counts_failed_respawn_toward_bound(monkeypatch, tmp_path):
    """A respawn that THROWS still counts toward the bound (so a perpetually
    failing recovery eventually parks instead of looping)."""
    monkeypatch.setenv("BOT_SQUAD_RECOVERY", "1")
    monkeypatch.setenv("BOT_SQUAD_RESPAWN_MAX", "1")
    cfg = _cfg(tmp_path)
    rows = [{"sid": "S-d-p1", "slug": "p1", "role": "dev", "pane_live": False,
             "task_id": "T-1", "task_status": ACTIVE, "has_artifact": False}]
    monkeypatch.setattr(R, "_gather", lambda c, now=None: rows)
    parks = []
    def _boom(c, row):
        raise RuntimeError("spawn failed")
    monkeypatch.setattr(R, "_do_respawn", _boom)
    monkeypatch.setattr(R, "_do_park", lambda c, row, reason: parks.append(row["sid"]))
    out1 = R.recovery_tick(cfg)
    assert out1["acted"] == [("respawn-failed", "S-d-p1")]
    out2 = R.recovery_tick(cfg)  # count now 1 >= bound 1 -> park
    assert parks == ["S-d-p1"]


# --- boot reconcile: the ungraceful-death + boot integration (DoD) ----------

def test_boot_reconcile_redrives_crash_from_artifact_and_archives(monkeypatch, tmp_path):
    """DoD: simulate an ungraceful death (md active + dead pane) + a worker boot.
    boot_reconcile must run EVEN with the periodic BOT_SQUAD_RECOVERY switch OFF,
    re-drive the crashed session from its role artifact (via the graceful-compact
    reload path), re-bound to the same task, and archive the dead predecessor.
    A gracefully-suspended sibling must be untouched."""
    from bot_squad_worker import sessions as S
    monkeypatch.delenv("BOT_SQUAD_RECOVERY", raising=False)       # periodic OFF
    monkeypatch.delenv("BOT_SQUAD_BOOT_RECONCILE", raising=False)  # boot default ON
    cfg = _cfg(tmp_path)
    backlog = tmp_path / "data" / "p1" / "backlog"; backlog.mkdir(parents=True)
    artifacts = tmp_path / "data" / "p1" / "artifacts"; artifacts.mkdir(parents=True)

    _seed(tmp_path, "S-u-crashed-p1", status="active", role="dev", task_id="T-1")
    (backlog / "T-1.md").write_text("---\nid: T-1\nstatus: in_progress\n---\n# f\n")
    (artifacts / "T-1.md").write_text("# forward-state\nDONE x NEXT y\n")
    _seed(tmp_path, "S-u-graceful-p3", status="suspended", role="dev",
          task_id="T-1", pane_id="%3")

    spawns, archives = [], []
    monkeypatch.setattr(S, "live_pane_map", lambda *a, **k: {})  # all panes dead
    monkeypatch.setattr(S, "spawn",
        lambda cfg, slug, window, prompt, **kw: spawns.append(
            {"window": window, "prompt": prompt, "kw": kw}) or {"ok": True})
    monkeypatch.setattr(S, "archive_session",
        lambda cfg, slug, sid: archives.append(sid) or {"ok": True})

    out = R.boot_reconcile(cfg)

    assert out["enabled"] is True and out["source"] == "boot" and out["boot"] is True
    assert out["acted"] == [("respawn", "S-u-crashed-p1")]  # graceful NOT recovered
    # re-driven from the artifact (boot prompt names the artifact path), same task
    assert len(spawns) == 1
    assert "artifacts/T-1.md" in spawns[0]["prompt"]
    assert spawns[0]["kw"].get("task_id") == "T-1"
    # dead predecessor retired so the next boot won't recover it again
    assert archives == ["S-u-crashed-p1"]


def test_boot_reconcile_disabled_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_SQUAD_BOOT_RECONCILE", "0")
    called = []
    monkeypatch.setattr(R, "_gather", lambda cfg, now=None: called.append("gathered") or [])
    out = R.boot_reconcile(_cfg(tmp_path))
    assert out["enabled"] is False
    assert called == []  # short-circuits before touching any session


# --- T-0470: a crash respawn records a session_recycled lifecycle event ------

def test_do_respawn_emits_session_recycled(monkeypatch, tmp_path):
    """recovery feeds the unified lifecycle surface: re-driving a crashed
    session records a session_recycled event for operator measurement."""
    from bot_squad_worker import autocompact as A
    from bot_squad_worker import lifecycle_events as LE
    cfg = _cfg(tmp_path)
    row = {"sid": "S-u-crashed-p1", "slug": "p1", "role": "dev",
           "task_id": "T-1", "window": "w", "initiative": None,
           "has_artifact": True, "artifact_path": "/art/T-1.md"}
    monkeypatch.setattr(A, "_relaunch_from_artifact", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(R, "_retire_dead", lambda *a, **k: None)
    R._do_respawn(cfg, row)
    doc = LE.read_events(cfg, "p1", "S-u-crashed-p1")
    assert doc.get("counts", {}).get(LE.SESSION_RECYCLED) == 1
    assert doc["last"][LE.SESSION_RECYCLED]["cause"] == "recovery"
