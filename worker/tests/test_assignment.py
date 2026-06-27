"""T-0463 / M1-F1.1 — the assignment interface (Tasks + Routines both conform).

Source: vision/INI-XX-process-paradigm-SOURCE-VERBATIM.md Part A — "Each session
should be provided with an assignment ... a) small prompt on how it's expected
to work with an assignment, b) specific assignment ID, c) means to access its
text, d) means to write the result." ; "Tasks: Implement the assignment
interface" ; "Routines: Second thing implementing the assignment interface".

The write-result primitive (d) is backed by the ONE reusable Artifact seam that
the autocompact write-to-artifact flow (T-0467) and the operator state-doc
(T-0473) extend — NOT a Task-only hack.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker.assignment import (
    Artifact,
    Assignment,
    OPERATOR_STATE_SECTIONS,
    RoutineAssignment,
    TaskAssignment,
    for_task,
    operator_state_template,
    role_artifact,
    role_compact_guidance,
)


# --- Artifact: the generic write-result sink ------------------------------

def test_artifact_round_trips_content(tmp_path: Path):
    art = Artifact(tmp_path / "artifacts" / "T-0001.md")
    assert art.exists() is False
    assert art.read() == ""  # absent reads as empty, never raises
    art.write("hello\nworld\n")
    assert art.exists() is True
    assert art.read() == "hello\nworld\n"


def test_artifact_write_is_a_full_replace(tmp_path: Path):
    art = Artifact(tmp_path / "a.md")
    art.write("first")
    art.write("second")
    assert art.read() == "second"


# --- role_artifact: the role-agnostic compact destination (T-0467) --------

def test_role_artifact_for_a_dev_is_the_task_sidecar(tmp_path: Path):
    # A task-bound session (dev) writes its forward-state into the SAME T-0463
    # sidecar that holds its result — no second store.
    data_dir = tmp_path / "data"
    art = role_artifact(data_dir, "bot-squad", role="dev",
                        sid="S-u-dev-p5", task_id="T-0042")
    assert isinstance(art, Artifact)
    assert art.path == data_dir / "bot-squad" / "artifacts" / "T-0042.md"


def test_role_artifact_for_an_operator_is_the_state_doc_seam(tmp_path: Path):
    # The operator has no task; its role artifact is the state-doc the full
    # schema of which T-0473 fills — T-0467 wires the resolver to its path.
    data_dir = tmp_path / "data"
    art = role_artifact(data_dir, "bot-squad", role="operator",
                        sid="S-u-operator-p1", task_id=None)
    assert isinstance(art, Artifact)
    assert art.path == data_dir / "bot-squad" / "artifacts" / "operator-state.md"


def test_role_artifact_task_id_wins_over_role(tmp_path: Path):
    # task-binding is authoritative: even a non-dev role with a task writes the
    # task sidecar (keeps the destination unambiguous).
    data_dir = tmp_path / "data"
    art = role_artifact(data_dir, "bot-squad", role="teamlead",
                        sid="S-u-tl-p2", task_id="T-0099")
    assert art.path == data_dir / "bot-squad" / "artifacts" / "T-0099.md"


def test_role_artifact_generic_role_is_a_per_session_file(tmp_path: Path):
    # Any other transient role with no task still resolves to a stable per-role
    # artifact (role-agnostic — no role is left without a compact destination).
    data_dir = tmp_path / "data"
    art = role_artifact(data_dir, "bot-squad", role="user",
                        sid="S-almdudleer-user-p7", task_id=None)
    assert isinstance(art, Artifact)
    # path is deterministic + filesystem-safe and scoped to the sid tail
    assert art.path.parent == data_dir / "bot-squad" / "artifacts"
    assert art.path.name.startswith("role-user-")
    assert art.path.suffix == ".md"


def test_role_artifact_unknown_role_no_task_is_none(tmp_path: Path):
    # No role + no task → nothing to write into; caller falls back to /compact.
    data_dir = tmp_path / "data"
    assert role_artifact(data_dir, "bot-squad", role="", sid="S-x", task_id=None) is None
    assert role_artifact(data_dir, "bot-squad", role="", sid="S-x", task_id="~") is None


# --- operator state-doc schema + template (T-0473 / M2-F2.1) --------------
# The operator's role artifact is a FUTURE-FOCUSED project-management state
# document (priorities / what's happening / delivered / next / tracked-issues),
# explicitly NOT an event log. T-0467 wired the path (artifacts/operator-state.md);
# T-0473 owns its SCHEMA + the guidance that makes a session write the right shape.

def test_operator_state_sections_are_the_five_future_focused_buckets():
    titles = [t for (t, _desc) in OPERATOR_STATE_SECTIONS]
    # the five buckets named in the T-0473 verbatim, in priority-first order
    assert titles == [
        "Priorities",
        "What's happening now",
        "Delivered",
        "Next",
        "Tracked issues",
    ]


def test_operator_state_template_is_a_fillable_scaffold_with_every_section():
    doc = operator_state_template()
    assert isinstance(doc, str) and doc.strip()
    # every schema section appears as a markdown heading the operator fills
    for title, _desc in OPERATOR_STATE_SECTIONS:
        assert f"## {title}" in doc
    # it is future-focused, not an event log — say so in the scaffold
    assert "event log" in doc.lower()


def test_operator_state_template_embeds_the_slug_when_given():
    assert "acme-app" in operator_state_template("acme-app")


def test_role_compact_guidance_for_operator_carries_the_schema():
    g = role_compact_guidance("operator")
    assert isinstance(g, str) and g.strip()
    # so an operator at autocompact writes the right shape even from a degraded
    # context: the guidance names the schema sections
    for title, _desc in OPERATOR_STATE_SECTIONS:
        assert title in g
    # and reminds it is future-focused, not a chat/event dump
    assert "event log" in g.lower()


def test_role_compact_guidance_is_empty_for_non_operator_roles():
    # dev / teamlead / unknown / none → the generic "write everything" handoff
    # already fits; no extra schema to graft on.
    assert role_compact_guidance("dev") == ""
    assert role_compact_guidance("teamlead") == ""
    assert role_compact_guidance("") == ""
    assert role_compact_guidance(None) == ""


# --- TaskAssignment conforms to the 4 primitives --------------------------

def _make_task(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "data"
    backlog = data_dir / "bot-squad" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "T-0042-demo.md").write_text(
        "---\nid: T-0042\ntitle: Demo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nMake the widget blue.\n"
    )
    return data_dir, "bot-squad"


def test_task_primitive_b_assignment_id(tmp_path: Path):
    data_dir, slug = _make_task(tmp_path)
    a = TaskAssignment(data_dir, slug, "T-0042")
    assert a.assignment_id == "T-0042"
    assert a.kind == "task"


def test_task_primitive_a_how_to_prompt_is_nonempty(tmp_path: Path):
    data_dir, slug = _make_task(tmp_path)
    a = TaskAssignment(data_dir, slug, "T-0042")
    assert isinstance(a.how_to_prompt, str)
    assert a.how_to_prompt.strip()  # a real prompt, not blank


def test_task_primitive_c_read_text_returns_backlog_md(tmp_path: Path):
    data_dir, slug = _make_task(tmp_path)
    a = TaskAssignment(data_dir, slug, "T-0042")
    text = a.read_text()
    assert "Make the widget blue." in text
    assert "id: T-0042" in text


def test_task_read_text_missing_task_raises(tmp_path: Path):
    data_dir, slug = _make_task(tmp_path)
    a = TaskAssignment(data_dir, slug, "T-9999")
    with pytest.raises(FileNotFoundError):
        a.read_text()


def test_task_primitive_d_write_result_persists_artifact(tmp_path: Path):
    data_dir, slug = _make_task(tmp_path)
    a = TaskAssignment(data_dir, slug, "T-0042")
    art = a.write_result("widget is now blue; shipped in abc123", sid="S-x-p1",
                          ts="2026-06-27T00:00:00Z")
    # Returns the Artifact it wrote.
    assert isinstance(art, Artifact)
    # Lives at the conventional per-assignment path.
    assert art.path == data_dir / slug / "artifacts" / "T-0042.md"
    body = art.read()
    assert "widget is now blue; shipped in abc123" in body
    # Stamped frontmatter for traceability (extensible for T-0467/T-0473).
    assert "assignment: T-0042" in body
    assert "kind: task" in body
    assert "sid: S-x-p1" in body
    assert "updated: 2026-06-27T00:00:00Z" in body


def test_task_write_result_does_not_touch_the_backlog_md(tmp_path: Path):
    data_dir, slug = _make_task(tmp_path)
    backlog_md = data_dir / slug / "backlog" / "T-0042-demo.md"
    before = backlog_md.read_text()
    TaskAssignment(data_dir, slug, "T-0042").write_result("done", sid="S-x-p1")
    assert backlog_md.read_text() == before  # result is a sidecar, not the task body


def test_for_task_factory_builds_a_task_assignment(tmp_path: Path):
    data_dir, slug = _make_task(tmp_path)
    a = for_task(data_dir, slug, "T-0042")
    assert isinstance(a, TaskAssignment)
    assert isinstance(a, Assignment)


# --- RoutineAssignment is a conformance STUB (filled by M1-T2) -------------

def test_routine_is_an_assignment_subclass():
    assert issubclass(RoutineAssignment, Assignment)


def test_routine_read_text_is_a_stub_pointing_at_m1t2():
    r = RoutineAssignment("R-0001")
    assert r.assignment_id == "R-0001"
    assert r.kind == "routine"
    with pytest.raises(NotImplementedError, match="M1-T2"):
        r.read_text()


def test_routine_write_result_is_a_stub_pointing_at_m1t2():
    r = RoutineAssignment("R-0001")
    with pytest.raises(NotImplementedError, match="M1-T2"):
        r.write_result("anything")


# --- assignment_write_result worker action --------------------------------

def _make_action_cfg(tmp_path: Path, monkeypatch):
    import bot_squad_worker.actions as A

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
    backlog = data_dir / "bot-squad" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "T-0042-demo.md").write_text(
        "---\nid: T-0042\ntitle: Demo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nMake the widget blue.\n"
    )
    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir)
    monkeypatch.setattr(A, "_get_config", lambda: patched)
    return patched, data_dir


def test_action_write_result_persists_and_returns_path(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _cfg, data_dir = _make_action_cfg(tmp_path, monkeypatch)
    out = A.dispatch("assignment_write_result", {
        "slug": "bot-squad",
        "assignment_id": "T-0042",
        "content": "widget is now blue",
        "sid": "S-x-p1",
    })
    assert out["ok"] is True
    assert out["assignment_id"] == "T-0042"
    art_path = data_dir / "bot-squad" / "artifacts" / "T-0042.md"
    assert out["artifact_path"] == str(art_path)
    assert out["bytes_written"] > 0
    body = art_path.read_text()
    assert "widget is now blue" in body
    assert "assignment: T-0042" in body
    assert "sid: S-x-p1" in body


def test_action_write_result_unknown_slug_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_action_cfg(tmp_path, monkeypatch)
    with pytest.raises(A.ActionError, match="unknown project slug"):
        A.dispatch("assignment_write_result", {
            "slug": "nope", "assignment_id": "T-0042",
            "content": "x", "sid": "S-x-p1",
        })


def test_action_write_result_missing_params_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_action_cfg(tmp_path, monkeypatch)
    with pytest.raises(A.ActionError, match="missing required"):
        A.dispatch("assignment_write_result", {"slug": "bot-squad"})


def test_action_write_result_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_action_cfg(tmp_path, monkeypatch)
    with pytest.raises(A.ActionError, match="unexpected"):
        A.dispatch("assignment_write_result", {
            "slug": "bot-squad", "assignment_id": "T-0042",
            "content": "x", "sid": "S-x-p1", "bogus": 1,
        })


def test_action_write_result_empty_content_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_action_cfg(tmp_path, monkeypatch)
    with pytest.raises(A.ActionError, match="empty"):
        A.dispatch("assignment_write_result", {
            "slug": "bot-squad", "assignment_id": "T-0042",
            "content": "   ", "sid": "S-x-p1",
        })


def test_action_is_registered_with_a_mode(tmp_path):
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY

    assert "assignment_write_result" in ACTION_REGISTRY
    assert "assignment_write_result" in ACTION_MODES
