"""T-0467 / M1-F1.4 — universal compact = write-everything-to-role-artifact
→ clear → fresh incarnation reads it.

Source: vision/initiatives/process-paradigm.md clarification-01 — "all the
sessions from operator to dev to user session have same lifecycle with
timeout/context full, with out custom autocompact procedure: the session is
asked to write down everything an in-system artifact + clear the context".

This is OUR custom compact, NOT Claude's ``/compact`` (an in-context squeeze).
The handoff is a 2-phase, cross-tick state machine in ``autocompact``:

  ARM      over-ceiling + idle + composer-ready & a role artifact resolves
           → inject the HANDOFF prompt + stamp ``compact.phase = writing``.
  FINALIZE phase==writing & the artifact was written & pane idle
           → clear (suspend the old pane) + relaunch a FRESH incarnation that
             boots from the artifact, re-bound to the SAME assignment.

Fallbacks (never wedge): no artifact resolvable OR the write times out → the
legacy Claude ``/compact``.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import autocompact as A


# --- compact_write_state action: the role-agnostic "write everything" save ---

def _make_cfg(tmp_path: Path, monkeypatch, *, sid: str, window: str,
              task_id: str | None):
    """A worker config with one project + a session md for ``sid``."""
    import bot_squad_worker.actions as ACT

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        f'[projects.bot-squad]\n'
        f'slug = "bot-squad"\n'
        f'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        f'deploy_branch = "bot_squad/dev"\n'
        f'master_branch = "master"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True)
    fm = [f"sid: {sid}", f"window: {window}"]
    if task_id:
        fm.append(f"task_id: {task_id}")
        backlog = data_dir / "bot-squad" / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        (backlog / f"{task_id}-demo.md").write_text(
            f"---\nid: {task_id}\ntitle: Demo\nstatus: open\n---\n\nDo the thing.\n"
        )
    (sess / f"{sid}.md").write_text("---\n" + "\n".join(fm) + "\n---\n")

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir)
    monkeypatch.setattr(ACT, "_get_config", lambda: patched)
    return patched, data_dir


def test_compact_write_state_dev_writes_the_task_sidecar(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    _cfg, data_dir = _make_cfg(tmp_path, monkeypatch, sid=sid, window="demo",
                               task_id="T-0042")
    out = ACT.dispatch("compact_write_state", {
        "slug": "bot-squad", "sid": sid,
        "content": "## forward-state\nhalf the widget is blue; next: paint the rest",
    })
    assert out["ok"] is True
    assert out["role"] == "dev"
    assert out["assignment_id"] == "T-0042"
    art = data_dir / "bot-squad" / "artifacts" / "T-0042.md"
    assert out["artifact_path"] == str(art)
    body = art.read_text()
    assert "paint the rest" in body
    assert "assignment: T-0042" in body
    assert f"sid: {sid}" in body


def test_compact_write_state_operator_writes_the_state_doc(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-operator-p1"
    _cfg, data_dir = _make_cfg(tmp_path, monkeypatch, sid=sid,
                               window="operator", task_id=None)
    out = ACT.dispatch("compact_write_state", {
        "slug": "bot-squad", "sid": sid,
        "content": "priorities: ship M1; happening: T-0467 in flight",
    })
    assert out["ok"] is True
    assert out["role"] == "operator"
    art = data_dir / "bot-squad" / "artifacts" / "operator-state.md"
    assert out["artifact_path"] == str(art)
    assert "T-0467 in flight" in art.read_text()


def test_compact_write_state_is_a_full_replace(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    _make_cfg(tmp_path, monkeypatch, sid=sid, window="demo", task_id="T-0042")
    ACT.dispatch("compact_write_state", {"slug": "bot-squad", "sid": sid,
                                         "content": "first state"})
    out = ACT.dispatch("compact_write_state", {"slug": "bot-squad", "sid": sid,
                                               "content": "second state"})
    body = Path(out["artifact_path"]).read_text()
    assert "second state" in body
    assert "first state" not in body


def test_compact_write_state_unknown_sid_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    _make_cfg(tmp_path, monkeypatch, sid="S-known-p1", window="demo", task_id=None)
    with pytest.raises(ACT.ActionError, match="no session"):
        ACT.dispatch("compact_write_state", {
            "slug": "bot-squad", "sid": "S-ghost-p9", "content": "x"})


def test_compact_write_state_empty_content_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    _make_cfg(tmp_path, monkeypatch, sid=sid, window="demo", task_id="T-0042")
    with pytest.raises(ACT.ActionError, match="empty"):
        ACT.dispatch("compact_write_state", {"slug": "bot-squad", "sid": sid,
                                             "content": "   "})


def test_compact_write_state_missing_params_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    _make_cfg(tmp_path, monkeypatch, sid="S-x-p1", window="demo", task_id=None)
    with pytest.raises(ACT.ActionError, match="missing required"):
        ACT.dispatch("compact_write_state", {"slug": "bot-squad"})


def test_compact_write_state_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    _make_cfg(tmp_path, monkeypatch, sid=sid, window="demo", task_id="T-0042")
    with pytest.raises(ACT.ActionError, match="unexpected"):
        ACT.dispatch("compact_write_state", {"slug": "bot-squad", "sid": sid,
                                             "content": "x", "bogus": 1})


def test_compact_write_state_registered_with_a_mode():
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY
    assert "compact_write_state" in ACTION_REGISTRY
    assert "compact_write_state" in ACTION_MODES


# --- boot_prompt_from_artifact: the reusable reload path (shared w/ T-0471) --

def test_boot_prompt_names_the_artifact_as_only_memory():
    prompt = A.boot_prompt_from_artifact(
        role="dev", assignment_id="T-0042",
        artifact_path="/data/bot-squad/artifacts/T-0042.md")
    assert "T-0042" in prompt
    assert "/data/bot-squad/artifacts/T-0042.md" in prompt
    # the successor is told the artifact is its ONLY memory (no retained context)
    low = prompt.lower()
    assert "only" in low and "memory" in low
    assert "read" in low


def test_boot_prompt_handles_a_taskless_role():
    prompt = A.boot_prompt_from_artifact(
        role="operator", assignment_id=None,
        artifact_path="/data/bot-squad/artifacts/operator-state.md")
    assert "operator-state.md" in prompt
    assert "operator" in prompt.lower()


# --- the 2-phase compact state machine --------------------------------------

@pytest.fixture
def harness(monkeypatch):
    """Stub every tmux/spawn seam so no real session is touched."""
    calls = {"handoff": [], "compact": [], "suspend": [], "spawn": []}
    state = {"pane": "%9", "buf": "❯ ready\n", "artifact_mtime": 100.0,
             "artifact": A.assignment.Artifact}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: calls["compact"].append(sid))
    monkeypatch.setattr(A, "_inject_handoff", lambda sid, art_path: calls["handoff"].append((sid, art_path)))
    monkeypatch.setattr(A, "_suspend_session", lambda cfg, slug, sid: calls["suspend"].append(sid))
    monkeypatch.setattr(A, "_relaunch_from_artifact",
                        lambda cfg, slug, rec, art_path: calls["spawn"].append((rec["sid"], art_path)))
    monkeypatch.setattr(A, "_resolve_role_artifact",
                        lambda cfg, slug, rec: ("/art/T-0042.md", "dev", "T-0042"))
    monkeypatch.setattr(A, "_artifact_mtime", lambda path: state["artifact_mtime"])
    monkeypatch.setattr(A, "autocompact_enabled", lambda: True)
    monkeypatch.setattr(A, "compact_mode", lambda: "handoff")
    return {"calls": calls, "state": state}


def _rec(sid="S-almdudleer-bot-squad-demo-p5", activity="idle", fired=None,
         compact=None, slug="bot-squad", role="dev", task_id="T-0042"):
    r = {"sid": sid, "activity": activity, "alert_fired_at": dict(fired or {}),
         "role": role, "task_id": task_id, "slug": slug}
    if compact is not None:
        r["compact"] = compact
    return r


def test_arm_injects_handoff_and_stamps_phase_not_compact(harness):
    rec = _rec()
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is True
    # asked the session to write everything down — NOT Claude's /compact
    assert harness["calls"]["handoff"] == [(rec["sid"], "/art/T-0042.md")]
    assert harness["calls"]["compact"] == []
    assert rec["compact"]["phase"] == "writing"
    assert rec["compact"]["armed_at"] == 1000.0
    # the pre-write mtime is recorded so FINALIZE can detect a NEW write
    assert rec["compact"]["arm_mtime"] == 100.0


def test_arm_falls_back_to_claude_compact_when_no_artifact(harness, monkeypatch):
    monkeypatch.setattr(A, "_resolve_role_artifact", lambda cfg, slug, rec: (None, "", None))
    rec = _rec(role="", task_id=None)
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is True
    assert harness["calls"]["compact"] == [rec["sid"]]  # legacy path
    assert harness["calls"]["handoff"] == []
    assert "compact" not in rec or rec.get("compact", {}).get("phase") != "writing"


def test_arm_respects_claude_mode_override(harness, monkeypatch):
    monkeypatch.setattr(A, "compact_mode", lambda: "claude")
    rec = _rec()
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is True
    assert harness["calls"]["compact"] == [rec["sid"]]
    assert harness["calls"]["handoff"] == []


def test_writing_phase_waits_until_artifact_written(harness):
    # phase=writing but the artifact mtime hasn't advanced past arm → keep waiting
    rec = _rec(compact={"phase": "writing", "armed_at": 1000.0, "arm_mtime": 100.0})
    harness["state"]["artifact_mtime"] = 100.0  # unchanged
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is False
    assert harness["calls"]["suspend"] == []
    assert harness["calls"]["spawn"] == []
    assert rec["compact"]["phase"] == "writing"  # still waiting


def test_finalize_clears_and_relaunches_from_artifact(harness):
    rec = _rec(compact={"phase": "writing", "armed_at": 1000.0, "arm_mtime": 100.0,
                        "artifact_path": "/art/T-0042.md"})
    harness["state"]["artifact_mtime"] = 150.0  # the session wrote it
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is True
    # cleared the old pane THEN relaunched a fresh incarnation from the artifact
    assert harness["calls"]["suspend"] == [rec["sid"]]
    assert harness["calls"]["spawn"] == [(rec["sid"], "/art/T-0042.md")]
    # phase is cleared so the fresh session starts a clean lifecycle
    assert rec.get("compact", {}).get("phase") is None


def test_finalize_relaunch_failure_alerts_operator_not_silent(harness, monkeypatch):
    # If the old pane is cleared but the relaunch throws (e.g. transient tmux
    # error), telemetry won't re-sample the paneless session — so don't orphan
    # the task silently: alert the operator (mirrors recovery.py park-notify).
    alerts = []
    monkeypatch.setattr(A, "_alert_orphaned_handoff",
                        lambda cfg, slug, sid, reason: alerts.append((sid, reason)))

    def _boom(cfg, slug, rec, art_path):
        raise RuntimeError("tmux exploded")
    monkeypatch.setattr(A, "_relaunch_from_artifact", _boom)

    rec = _rec(compact={"phase": "writing", "armed_at": 1000.0, "arm_mtime": 100.0,
                        "artifact_path": "/art/T-0042.md"})
    harness["state"]["artifact_mtime"] = 150.0
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is False
    assert harness["calls"]["suspend"] == [rec["sid"]]  # old pane was cleared
    assert len(alerts) == 1 and alerts[0][0] == rec["sid"]  # operator told


def test_finalize_only_when_pane_idle(harness):
    rec = _rec(activity="running",
               compact={"phase": "writing", "armed_at": 1000.0, "arm_mtime": 100.0})
    harness["state"]["artifact_mtime"] = 150.0
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is False
    assert harness["calls"]["suspend"] == []


def test_finalize_timeout_falls_back_to_claude_compact(harness):
    # the session never wrote its state within the deadline → don't wedge,
    # fall back to Claude's /compact and drop the handoff phase.
    rec = _rec(compact={"phase": "writing", "armed_at": 1000.0, "arm_mtime": 100.0})
    harness["state"]["artifact_mtime"] = 100.0  # never written
    late = 1000.0 + A.handoff_timeout_sec() + 1
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late) is True
    assert harness["calls"]["compact"] == [rec["sid"]]
    assert rec.get("compact", {}).get("phase") is None


def test_kill_switch_disables_the_whole_loop(harness, monkeypatch):
    monkeypatch.setattr(A, "autocompact_enabled", lambda: False)
    rec = _rec()
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is False
    assert harness["calls"]["handoff"] == []
    assert harness["calls"]["compact"] == []
