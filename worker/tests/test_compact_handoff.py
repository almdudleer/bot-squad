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


def test_compact_write_state_refuses_a_task_bound_session(tmp_path, monkeypatch):
    """T-0863: a session that owns a task hands off through that ticket's
    `## Context` — one task, one artifact. Writing `artifacts/<task_id>.md`
    would mint a second, board-invisible copy of the same forward-state."""
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    _cfg, data_dir = _make_cfg(tmp_path, monkeypatch, sid=sid, window="demo",
                               task_id="T-0042")
    with pytest.raises(ACT.ActionError) as e:
        ACT.dispatch("compact_write_state", {
            "slug": "bot-squad", "sid": sid,
            "content": "half the widget is blue; next: paint the rest",
        })
    # refuses LOUDLY and names the replacement — a silent no-op here is the
    # "reported success, stored nothing" failure T-0858 measured
    assert "T-0042" in str(e.value)
    assert "bsq ticket context" in str(e.value)
    assert not (data_dir / "bot-squad" / "artifacts" / "T-0042.md").exists()


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
    sid = "S-almdudleer-bot-squad-operator-p1"
    _make_cfg(tmp_path, monkeypatch, sid=sid, window="operator", task_id=None)
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


# --- operator_state_doc action: read-only transparency (T-0473 / DoD-3) -----

def test_operator_state_doc_reports_not_yet_created(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    _cfg, data_dir = _make_cfg(tmp_path, monkeypatch, sid="S-x-p1",
                               window="operator", task_id=None)
    out = ACT.dispatch("operator_state_doc", {"slug": "bot-squad"})
    art = data_dir / "bot-squad" / "artifacts" / "operator-state.md"
    assert out["exists"] is False
    assert out["path"] == str(art)
    assert out["content"] == ""
    # a fillable scaffold is always offered so a fresh operator can seed it
    assert "## Priorities" in out["template"]


def test_operator_state_doc_reads_the_written_doc(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-operator-p1"
    _cfg, _data = _make_cfg(tmp_path, monkeypatch, sid=sid, window="operator",
                            task_id=None)
    ACT.dispatch("compact_write_state", {
        "slug": "bot-squad", "sid": sid,
        "content": "priorities: ship M2; happening: T-0473 in flight"})
    out = ACT.dispatch("operator_state_doc", {"slug": "bot-squad"})
    assert out["exists"] is True
    assert "T-0473 in flight" in out["content"]


def test_operator_state_doc_unknown_slug_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    _make_cfg(tmp_path, monkeypatch, sid="S-x-p1", window="operator", task_id=None)
    with pytest.raises(ACT.ActionError, match="unknown project"):
        ACT.dispatch("operator_state_doc", {"slug": "ghost"})


def test_operator_state_doc_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    _make_cfg(tmp_path, monkeypatch, sid="S-x-p1", window="operator", task_id=None)
    with pytest.raises(ACT.ActionError, match="unexpected"):
        ACT.dispatch("operator_state_doc", {"slug": "bot-squad", "nope": 1})


def test_operator_state_doc_registered_read_mode():
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY
    assert "operator_state_doc" in ACTION_REGISTRY
    assert ACTION_MODES["operator_state_doc"] == "coordinator_only"


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


# --- handoff_prompt role-aware shape guidance (T-0473) ----------------------

def test_handoff_prompt_appends_operator_state_doc_schema():
    from bot_squad_worker.assignment import OPERATOR_STATE_SECTIONS
    p = A.handoff_prompt("/data/bot-squad/artifacts/operator-state.md", "operator")
    # the operator handoff carries the future-focused schema so it writes the
    # right shape even from a degraded context
    for title, _hint in OPERATOR_STATE_SECTIONS:
        assert title in p
    assert "event log" in p.lower()
    # still the universal handoff underneath
    assert "compact-save" in p


def test_handoff_prompt_is_byte_identical_for_non_operator_roles():
    # backward-compat guard: the T-0467 dev/task handoff must not shift a byte.
    base = A.handoff_prompt("/art/T-0042.md")
    assert A.handoff_prompt("/art/T-0042.md", None) == base
    assert A.handoff_prompt("/art/T-0042.md", "dev") == base
    assert A.handoff_prompt("/art/T-0042.md", "teamlead") == base


# --- the 2-phase compact state machine --------------------------------------

@pytest.fixture
def harness(monkeypatch):
    """Stub every tmux/spawn seam so no real session is touched.

    Defaults to the T-0863 TASK-BOUND destination (the ticket's ``## Context``),
    since that is what an ordinary dev/TL session takes; the task-LESS artifact
    handoff is selected per-test via ``taskless()``.
    """
    calls = {"handoff": [], "ctx_handoff": [], "compact": [], "suspend": [],
             "spawn": [], "spawn_ticket": []}
    state = {"pane": "%9", "buf": "❯ ready\n", "artifact_mtime": 100.0,
             "ctx_digest": "digest-at-arm",
             "target": {"kind": "context", "task_id": "T-0042",
                        "task_md": "/backlog/T-0042-demo.md", "role": "dev",
                        "assignment_id": "T-0042"}}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: calls["compact"].append(sid))
    monkeypatch.setattr(A, "_inject_handoff", lambda sid, art_path, role=None, *, relaunch=True: calls["handoff"].append((sid, art_path, role, relaunch)))
    monkeypatch.setattr(A, "_inject_context_handoff", lambda sid, task_id, *, relaunch=True: calls["ctx_handoff"].append((sid, task_id, relaunch)))
    monkeypatch.setattr(A, "_suspend_session", lambda cfg, slug, sid: calls["suspend"].append(sid))
    # `**kw` deliberately (T-0909) — see test_recovery.py for why a hand-mirrored
    # stub signature is the wrong thing to pin.
    monkeypatch.setattr(A, "_relaunch_from_artifact",
                        lambda cfg, slug, rec, art_path, **kw:
                        calls["spawn"].append((rec["sid"], art_path)))
    monkeypatch.setattr(A, "_relaunch_from_ticket",
                        lambda cfg, slug, rec, task_md, **kw:
                        calls["spawn_ticket"].append((rec["sid"], task_md)))
    monkeypatch.setattr(A, "_resolve_compact_target",
                        lambda cfg, slug, rec: state["target"])
    monkeypatch.setattr(A, "_artifact_mtime", lambda path: state["artifact_mtime"])
    monkeypatch.setattr(A, "context_digest", lambda path: state["ctx_digest"])
    monkeypatch.setattr(A, "autocompact_enabled", lambda: True)
    monkeypatch.setattr(A, "compact_mode", lambda: "handoff")

    def taskless():
        state["target"] = {"kind": "artifact", "artifact_path": "/art/operator-state.md",
                           "role": "operator", "assignment_id": "operator"}
    return {"calls": calls, "state": state, "taskless": taskless}


def _rec(sid="S-almdudleer-bot-squad-demo-p5", activity="idle", fired=None,
         compact=None, slug="bot-squad", role="dev", task_id="T-0042"):
    r = {"sid": sid, "activity": activity, "alert_fired_at": dict(fired or {}),
         "role": role, "task_id": task_id, "slug": slug}
    if compact is not None:
        r["compact"] = compact
    return r


def test_arm_asks_a_task_bound_session_for_its_ticket_context(harness):
    """T-0863: the default handoff for a session that owns a task is the
    ticket's `## Context` — no artifact file, no /compact."""
    rec = _rec()
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is True
    # relaunch=True: the ceiling path boots a successor from what it writes
    assert harness["calls"]["ctx_handoff"] == [(rec["sid"], "T-0042", True)]
    assert harness["calls"]["handoff"] == []   # no artifact handoff
    assert harness["calls"]["compact"] == []   # not Claude's /compact
    assert rec["compact"]["phase"] == "writing"
    assert rec["compact"]["kind"] == "context"
    assert rec["compact"]["armed_at"] == 1000.0
    assert rec["compact"]["task_id"] == "T-0042"
    # the pre-write Context digest is recorded so FINALIZE can detect the write
    assert rec["compact"]["arm_digest"] == "digest-at-arm"


def test_arm_injects_artifact_handoff_for_a_taskless_session(harness):
    """The task-LESS half is unchanged (T-0467/T-0473): an operator has no
    ticket to write a Context onto, so the role artifact is still its home.
    The resolved role rides along so the handoff can shape the state-doc."""
    harness["taskless"]()
    rec = _rec(role="operator", task_id=None)
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is True
    assert harness["calls"]["handoff"] == [
        (rec["sid"], "/art/operator-state.md", "operator", True)]
    assert harness["calls"]["ctx_handoff"] == []
    assert harness["calls"]["compact"] == []
    assert rec["compact"]["kind"] == "artifact"
    assert rec["compact"]["arm_mtime"] == 100.0


def test_arm_falls_back_to_claude_compact_when_no_destination(harness, monkeypatch):
    monkeypatch.setattr(A, "_resolve_compact_target",
                        lambda cfg, slug, rec: {"kind": "none", "role": "",
                                                "assignment_id": None})
    rec = _rec(role="", task_id=None)
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is True
    assert harness["calls"]["compact"] == [rec["sid"]]  # legacy path
    assert harness["calls"]["handoff"] == []
    assert harness["calls"]["ctx_handoff"] == []
    assert "compact" not in rec or rec.get("compact", {}).get("phase") != "writing"


def test_arm_respects_claude_mode_override(harness, monkeypatch):
    monkeypatch.setattr(A, "compact_mode", lambda: "claude")
    rec = _rec()
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is True
    assert harness["calls"]["compact"] == [rec["sid"]]
    assert harness["calls"]["handoff"] == []


def test_writing_phase_waits_until_artifact_written(harness):
    # phase=writing but the artifact mtime hasn't advanced past arm → keep waiting
    rec = _rec(compact={"phase": "writing", "kind": "artifact", "armed_at": 1000.0,
                        "arm_mtime": 100.0})
    harness["state"]["artifact_mtime"] = 100.0  # unchanged
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is False
    assert harness["calls"]["suspend"] == []
    assert harness["calls"]["spawn"] == []
    assert rec["compact"]["phase"] == "writing"  # still waiting


def test_writing_phase_waits_until_the_ticket_context_changes(harness):
    """T-0863: an unchanged `## Context` means the session has not handed off
    yet — terminating here is precisely the loss this path prevents."""
    rec = _rec(compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-at-arm"  # unchanged
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is False
    assert harness["calls"]["suspend"] == []
    assert harness["calls"]["spawn_ticket"] == []
    assert rec["compact"]["phase"] == "writing"


def test_finalize_clears_and_relaunches_from_the_ticket(harness):
    rec = _rec(compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-after-write"  # the session wrote it
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is True
    # cleared the old pane THEN relaunched a fresh incarnation from the TICKET
    assert harness["calls"]["suspend"] == [rec["sid"]]
    assert harness["calls"]["spawn_ticket"] == [(rec["sid"], "/backlog/T-0042-demo.md")]
    assert harness["calls"]["spawn"] == []  # never the artifact reload
    # phase is cleared so the fresh session starts a clean lifecycle
    assert rec.get("compact", {}).get("phase") is None


def test_finalize_clears_and_relaunches_from_artifact(harness):
    harness["taskless"]()
    rec = _rec(role="operator", task_id=None,
               compact={"phase": "writing", "kind": "artifact", "armed_at": 1000.0,
                        "arm_mtime": 100.0, "artifact_path": "/art/operator-state.md"})
    harness["state"]["artifact_mtime"] = 150.0  # the session wrote it
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is True
    assert harness["calls"]["suspend"] == [rec["sid"]]
    assert harness["calls"]["spawn"] == [(rec["sid"], "/art/operator-state.md")]
    assert harness["calls"]["spawn_ticket"] == []
    assert rec.get("compact", {}).get("phase") is None


def test_finalize_treats_a_kindless_inflight_record_as_an_artifact(harness):
    """A handoff armed by the PREVIOUS worker build carries no `kind`. It must
    still converge — a worker restart mid-handoff otherwise leaves the session
    armed forever, over-ceiling, with nothing driving it."""
    rec = _rec(compact={"phase": "writing", "armed_at": 1000.0, "arm_mtime": 100.0,
                        "artifact_path": "/art/T-0042.md"})
    harness["state"]["artifact_mtime"] = 150.0
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is True
    assert harness["calls"]["spawn"] == [(rec["sid"], "/art/T-0042.md")]


def test_finalize_relaunch_failure_alerts_operator_not_silent(harness, monkeypatch):
    # If the old pane is cleared but the relaunch throws (e.g. transient tmux
    # error), telemetry won't re-sample the paneless session — so don't orphan
    # the task silently: alert the operator (mirrors recovery.py park-notify).
    alerts = []
    monkeypatch.setattr(A, "_alert_orphaned_handoff",
                        lambda cfg, slug, sid, reason: alerts.append((sid, reason)))

    def _boom(cfg, slug, rec, task_md, **kw):
        raise RuntimeError("tmux exploded")
    monkeypatch.setattr(A, "_relaunch_from_ticket", _boom)

    rec = _rec(compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-after-write"
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is False
    assert harness["calls"]["suspend"] == [rec["sid"]]  # old pane was cleared
    assert len(alerts) == 1 and alerts[0][0] == rec["sid"]  # operator told


def test_finalize_only_when_pane_idle(harness):
    rec = _rec(activity="running",
               compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-after-write"
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1030.0) is False
    assert harness["calls"]["suspend"] == []


def test_finalize_timeout_falls_back_to_claude_compact(harness):
    # the session never wrote its state within the deadline → don't wedge,
    # fall back to Claude's /compact and drop the handoff phase.
    rec = _rec(compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-at-arm"  # never written
    late = 1000.0 + A.handoff_timeout_sec() + 1
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late) is True
    assert harness["calls"]["compact"] == [rec["sid"]]
    assert rec.get("compact", {}).get("phase") is None


def test_kill_switch_disables_the_whole_loop(harness, monkeypatch):
    monkeypatch.setattr(A, "autocompact_enabled", lambda: False)
    rec = _rec()
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=1000.0) is False
    assert harness["calls"]["handoff"] == []
    assert harness["calls"]["ctx_handoff"] == []
    assert harness["calls"]["compact"] == []


# --- T-0863: the ticket-Context handoff -------------------------------------

def test_context_handoff_prompt_names_the_one_verb_and_forbids_the_old_ones():
    p = A.context_handoff_prompt("T-0042", relaunch=True)
    assert "bsq ticket context T-0042" in p
    # the retired mechanisms are named as forbidden, not merely omitted: a
    # dev's accumulated habit (and every skill doc it read before this change)
    # points at them, so silence would read as "either is fine"
    assert "compact-save" not in p
    assert "artifacts/" not in p
    low = p.lower()
    assert "do not write a handoff file" in low
    assert "progress note" in low
    # Context REPLACES, so the session must be told to write current state
    assert "REPLACES" in p
    # his words are off-limits even to a degraded, context-full session
    assert "## Stakeholder notes" in p


def test_context_handoff_prompt_asks_for_the_executive_summary_too(): 
    """T-0863: finalize is where the status paragraph gets written, so the
    prompt has to ask for it — and has to say what it is FOR.

    The distinguishing sentence is the reader: a prompt that asked for "a
    summary" without saying he reads it produces a second, shorter Context,
    which is the duplication the section was designed not to be.
    """
    p = A.context_handoff_prompt("T-0042", relaunch=True)
    assert "bsq ticket summary T-0042" in p
    assert "`## Executive summary`" in p
    assert "STAKEHOLDER reads this one" in p
    assert "ONE paragraph" in p
    # what it must NOT contain: he was explicit that restating the ask is the
    # one thing this section is not for
    assert "Do NOT restate" in p
    # both writes are named, and the completion signal waits for BOTH — a
    # session that reports done after one of them defeats the point of asking
    assert "bsq ticket context T-0042" in p
    assert "After both return ok" in p


def test_context_handoff_prompt_tells_the_truth_about_what_happens_next():
    """The ceiling path relaunches, the idle path stops. A session told it will
    be relaunched when it is about to be ended writes for a successor that
    never boots."""
    ceiling = A.context_handoff_prompt("T-0042", relaunch=True)
    idle = A.context_handoff_prompt("T-0042", relaunch=False)
    assert "relaunches you FRESH" in ceiling
    assert "relaunch" not in idle.lower()
    assert "ENDS this session" in idle
    assert "re-drives T-0042" in idle


def test_handoff_prompt_idle_variant_does_not_promise_a_relaunch():
    ceiling = A.handoff_prompt("/art/operator-state.md")
    idle = A.handoff_prompt("/art/operator-state.md", relaunch=False)
    assert "relaunches you fresh" in ceiling
    assert "relaunches you fresh" not in idle
    assert "ends this session" in idle
    # both still name the one verb a task-less session uses
    assert "compact-save" in ceiling and "compact-save" in idle


def test_boot_prompt_from_ticket_points_at_the_context_not_a_file():
    p = A.boot_prompt_from_ticket(role="dev", task_id="T-0042",
                                  task_md_path="/backlog/T-0042-demo.md")
    assert "T-0042" in p
    assert "/backlog/T-0042-demo.md" in p
    assert "## Context" in p
    assert "## Stakeholder notes" in p   # the anchor, per T-0483
    assert "artifacts/" not in p
    low = p.lower()
    assert "only" in low and "memory" in low


def _ticket(tmp_path, body: str, *, task_id="T-0042") -> Path:
    backlog = tmp_path / "bot-squad" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    p = backlog / f"{task_id}-demo.md"
    p.write_text(f"---\nid: {task_id}\ntitle: Demo\nstatus: open\n---\n\n{body}")
    return p


def test_context_digest_moves_when_the_context_is_rewritten(tmp_path):
    """Positive control for the FINALIZE signal — without this the negative
    below would pass just as well against a digest function that never moves."""
    p = _ticket(tmp_path, "## Stakeholder notes\n\nDo it.\n\n## Context\n\nbefore\n")
    before = A.context_digest(str(p))
    p.write_text(p.read_text().replace("before", "after the handoff"))
    assert A.context_digest(str(p)) != before
    assert before  # a readable ticket never digests to the empty sentinel


def test_context_digest_ignores_writes_to_other_sections(tmp_path):
    """THE reason FINALIZE reads the section and not the file's mtime: the
    ticket md is shared. A peer filing a progress note, or the stakeholder
    adding a quote, must not read as "this session handed off" — that would
    terminate it with nothing recorded."""
    p = _ticket(tmp_path, "## Stakeholder notes\n\nDo it.\n\n## Context\n\n"
                          "state\n\n## Progress\n\n- old note\n")
    before = A.context_digest(str(p))
    p.write_text(p.read_text()
                 .replace("- old note", "- old note\n- 2026-08-11 · S-peer · new note")
                 .replace("Do it.", "Do it.\n- 2026-08-11 · tg · and hurry"))
    assert A.context_digest(str(p)) == before


def test_context_digest_of_a_missing_ticket_is_the_empty_sentinel(tmp_path):
    # "" never satisfies handoff_written, so a vanished ticket falls to the
    # bounded timeout rather than looking like a completed write
    assert A.context_digest(str(tmp_path / "nope.md")) == ""
    assert A.context_digest(None) == ""
    assert A.handoff_written({"kind": "context", "task_md": None,
                              "arm_digest": ""}) is False


def test_context_digest_survives_a_title_containing_a_dash_run(tmp_path):
    """`split("---", 2)` reads such a ticket's body as a fragment and its
    Context as empty — which would make every digest equal and FINALIZE blind."""
    backlog = tmp_path / "bot-squad" / "backlog"
    backlog.mkdir(parents=True)
    p = backlog / "T-0042-demo.md"
    p.write_text('---\nid: T-0042\ntitle: "a --- b"\nstatus: open\n---\n\n'
                 "## Context\n\nreal state\n")
    d1 = A.context_digest(str(p))
    p.write_text(p.read_text().replace("real state", "different state"))
    assert A.context_digest(str(p)) != d1


def _target_cfg(tmp_path):
    return types.SimpleNamespace(data_dir=tmp_path)


def test_resolve_target_routes_a_task_bound_session_to_its_ticket(tmp_path):
    p = _ticket(tmp_path, "## Context\n\nstate\n")
    t = A._resolve_compact_target(_target_cfg(tmp_path), "bot-squad",
                                  {"sid": "S-x-p1", "role": "dev", "task_id": "T-0042"})
    assert t["kind"] == "context"
    assert t["task_id"] == "T-0042"
    assert t["task_md"] == str(p)


def test_resolve_target_routes_a_taskless_session_to_its_role_artifact(tmp_path):
    t = A._resolve_compact_target(_target_cfg(tmp_path), "bot-squad",
                                  {"sid": "S-x-p1", "role": "operator", "task_id": "~"})
    assert t["kind"] == "artifact"
    assert t["artifact_path"].endswith("/artifacts/operator-state.md")


def test_resolve_target_refuses_to_resurrect_the_task_sidecar(tmp_path):
    """A session bound to an id whose md is gone must NOT fall through to
    `role_artifact`, which would resolve it right back to the retired
    `artifacts/<task_id>.md`."""
    t = A._resolve_compact_target(_target_cfg(tmp_path), "bot-squad",
                                  {"sid": "S-x-p1", "role": "dev", "task_id": "T-9999"})
    assert t["kind"] == "none"


# --- T-0905: the timeout fallback, split by WHETHER ANYTHING WAS WRITTEN -----
#
# Measured on the live worker journal (2026-08-12..18, 6 days):
#   74 completed handoffs, ARM→complete max 719s — so 900s is a fine deadline
#      for the question "did this session write anything at all".
#   4 timed-out arms. Two of them are S-almdudleer-operator-p355 on 2026-08-18,
#      and in BOTH the forward-state was already on disk (operator-state.md
#      rewritten at 07:48/07:50/07:55/07:58 after a 07:39 ARM) — the pane was
#      simply mid-generation for the whole window. Each fallback bought a
#      compact_boundary of preTokens 389,980 and 396,913 respectively, fired
#      294s and 54s past the deadline and within seconds of the pane first
#      going composer-ready — i.e. at the exact moment the clean relaunch
#      became possible.

def test_finalize_does_not_fall_back_when_the_forward_state_is_already_written(harness):
    """The core T-0905 claim: past the deadline with the state ON DISK and a
    busy pane, NOTHING happens — no /compact, no clear, the handoff stays armed.

    ``_do_claude_compact`` is gated on the same composer-ready pane the finalize
    is, so the fallback can never take context down sooner than the relaunch
    would have; it can only spend a summarization instead of doing the relaunch.
    """
    rec = _rec(activity="running",
               compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-after-write"   # it DID write
    late = 1000.0 + A.handoff_timeout_sec() + 60
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late) is False
    assert harness["calls"]["compact"] == []      # the whole point
    assert harness["calls"]["suspend"] == []
    assert rec["compact"]["phase"] == "writing"   # still armed, not dropped


def test_finalize_relaunches_when_the_pane_frees_after_the_deadline(harness):
    """p355 replayed: the pane went composer-ready 294s PAST the deadline with
    the state long since written. That tick must produce the clean
    clear+relaunch, not the ~390k-token /compact it produced live."""
    rec = _rec(activity="idle",
               compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-after-write"
    late = 1000.0 + A.handoff_timeout_sec() + 294
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late) is True
    assert harness["calls"]["compact"] == []
    assert harness["calls"]["suspend"] == [rec["sid"]]
    assert harness["calls"]["spawn_ticket"] == [(rec["sid"], "/backlog/T-0042-demo.md")]
    assert rec["compact"] == {}


def test_the_operator_shape_does_not_fall_back_either(harness):
    """p355 is task-LESS (role artifact, not a ticket Context), and the
    artifact half must take the same branch — that is the session shape the
    whole ticket was filed about."""
    harness["taskless"]()
    rec = _rec(activity="running", role="operator", task_id=None,
               compact={"phase": "writing", "kind": "artifact", "armed_at": 1000.0,
                        "artifact_path": "/art/operator-state.md",
                        "arm_mtime": 100.0, "role": "operator"})
    harness["state"]["artifact_mtime"] = 200.0   # its drive cycle rewrote it
    late = 1000.0 + A.handoff_timeout_sec() + 60
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late) is False
    assert harness["calls"]["compact"] == []
    assert rec["compact"]["phase"] == "writing"


def test_finalize_is_forced_at_the_hard_cap_against_a_busy_pane(harness):
    """Never wedge, the other way round: waiting for an idle pane is bounded
    too. Past the hard cap the state is on disk, so clearing the pane costs
    only the in-flight turn — cheaper than staying over the ceiling forever."""
    rec = _rec(activity="running",
               compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-after-write"
    way_late = 1000.0 + A.handoff_hard_timeout_sec() + 1
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=way_late) is True
    assert harness["calls"]["compact"] == []
    assert harness["calls"]["suspend"] == [rec["sid"]]
    assert harness["calls"]["spawn_ticket"] == [(rec["sid"], "/backlog/T-0042-demo.md")]


def test_the_hard_cap_does_not_apply_to_a_handoff_that_wrote_nothing(harness):
    """The hard cap is a licence to relaunch, and it is earned by having
    written. A session that wrote nothing must never be relaunched at any age —
    there is nothing for the successor to boot from."""
    rec = _rec(activity="running",
               compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-at-arm"        # never written
    harness["state"]["buf"] = "working… esc to interrupt"   # pane busy
    way_late = 1000.0 + A.handoff_hard_timeout_sec() + 1
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=way_late) is False
    assert harness["calls"]["suspend"] == []
    assert harness["calls"]["spawn_ticket"] == []


def test_a_failed_fallback_send_leaves_the_handoff_armed(harness):
    """The old code cleared ``rec['compact']`` BEFORE knowing the /compact send
    landed. Telemetry only persists the record when an action was taken, so on
    the failed-send path that clear was reverted and the same WARNING re-fired
    every tick — 6 of them for one timeout on p355. Keeping the phase also lets
    a LATE write finalize cleanly instead of being locked out."""
    rec = _rec(activity="idle",
               compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-at-arm"        # never written
    harness["state"]["buf"] = "working… esc to interrupt"   # send can't land
    late = 1000.0 + A.handoff_timeout_sec() + 1
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late) is False
    assert harness["calls"]["compact"] == []
    assert rec["compact"]["phase"] == "writing"


def test_a_late_write_still_finalizes_instead_of_being_locked_out(harness):
    """Consequence of the line above, and the one that closes the repeat loop:
    a session that misses the deadline while its pane is busy and writes
    afterwards gets the clean relaunch on the next tick."""
    rec = _rec(activity="idle",
               compact={"phase": "writing", "kind": "context", "armed_at": 1000.0,
                        "task_id": "T-0042", "task_md": "/backlog/T-0042-demo.md",
                        "arm_digest": "digest-at-arm"})
    harness["state"]["ctx_digest"] = "digest-at-arm"
    harness["state"]["buf"] = "working… esc to interrupt"
    late = 1000.0 + A.handoff_timeout_sec() + 1
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late) is False

    harness["state"]["ctx_digest"] = "digest-after-write"   # it wrote, late
    harness["state"]["buf"] = "❯ ready\n"
    assert A.maybe_compact(None, "bot-squad", rec, "urgent", now=late + 60) is True
    assert harness["calls"]["compact"] == []
    assert harness["calls"]["spawn_ticket"] == [(rec["sid"], "/backlog/T-0042-demo.md")]


def test_hard_timeout_can_never_undercut_the_soft_one(monkeypatch):
    """A hard cap below the soft deadline would invert the two halves — a
    handoff would be force-finalized before the "wrote nothing" branch it
    belongs to could be evaluated."""
    monkeypatch.setenv("BOT_SQUAD_HANDOFF_HARD_TIMEOUT_SEC", "5")
    assert A.handoff_hard_timeout_sec() == A.handoff_timeout_sec()
    monkeypatch.setenv("BOT_SQUAD_HANDOFF_HARD_TIMEOUT_SEC", "9999")
    assert A.handoff_hard_timeout_sec() == 9999
    monkeypatch.setenv("BOT_SQUAD_HANDOFF_HARD_TIMEOUT_SEC", "nonsense")
    assert A.handoff_hard_timeout_sec() == A.DEFAULT_HANDOFF_HARD_TIMEOUT_SEC
    monkeypatch.delenv("BOT_SQUAD_HANDOFF_HARD_TIMEOUT_SEC")
    assert A.handoff_hard_timeout_sec() == A.DEFAULT_HANDOFF_HARD_TIMEOUT_SEC


def test_the_soft_deadline_still_covers_every_completed_handoff_measured():
    """The soft deadline is now only "did it write anything" — and it is sized
    off a real distribution: over 2026-08-12..18 the slowest of 74 completed
    handoffs had its forward-state on disk 719s after ARM. If someone lowers
    this below that, sessions that WOULD have written get the /compact branch."""
    assert A.DEFAULT_HANDOFF_TIMEOUT_SEC >= 900
    # and the hard cap has to clear the worst measured busy stretch (p355:
    # 1194s from ARM to the pane first going composer-ready)
    assert A.DEFAULT_HANDOFF_HARD_TIMEOUT_SEC >= 1194
