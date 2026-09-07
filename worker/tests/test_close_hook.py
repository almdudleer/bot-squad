"""Tests for T-0151 session-close guidance harvest (bot_squad_worker.close_hook)."""
from __future__ import annotations

import json
from pathlib import Path

from bot_squad_worker import close_hook, sessions as S
from bot_squad_worker.input_mux import HARNESS_NUDGE_MARKER
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

SID = "S-tester-T-0149-p9"


def _write_jsonl(path: Path, messages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


def _human(text: str) -> dict:
    return {"type": "user", "userType": "external", "isSidechain": False,
            "message": {"role": "user", "content": text}}


def _setup(tmp_path, monkeypatch, *, status="suspended", task_id="T-0149",
           harvested=False, messages=None):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    cwd = "/home/tester/repo"
    uuid = "uuid-xyz"

    backlog = cfg.data_dir / slug / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    ticket = backlog / f"{task_id}-dynamic-context-manager.md"
    ticket.write_text(f"---\nid: {task_id}\ntitle: T\nstatus: in_progress\n---\n\n## DoD\n- x\n")

    sessions_dir = cfg.data_dir / slug / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    md = sessions_dir / f"{SID}.md"
    md.write_text(
        f"---\nsid: {SID}\nstatus: {status}\nwindow: T-0149\npane_id: %9\n"
        f"cwd: {cwd}\nclaude_uuid: {uuid}\ntask_id: {task_id}\n"
        + ("guidance_harvested: true\n" if harvested else "") + "---\n"
    )

    home = tmp_path / "home"
    jsonl = home / ".claude" / "projects" / cwd.replace("/", "-") / f"{uuid}.jsonl"
    _write_jsonl(jsonl, messages if messages is not None else [
        _human("please make sure the drift reminder is enforced, not a polite suggestion, "
               "and comes back every 30 minutes if I keep drifting"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        _human("ok"),  # too short, ignored
    ])

    monkeypatch.setattr(S, "_get_current_user", lambda: "tester")
    monkeypatch.setattr(close_hook.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    return cfg, slug, ticket, md


def test_harvest_appends_stakeholder_comment(tmp_path, monkeypatch):
    cfg, slug, ticket, md = _setup(tmp_path, monkeypatch)
    res = close_hook.harvest_tick(cfg, slug)
    assert len(res["harvested"]) == 1 and res["harvested"][0]["added"] == 1
    body = ticket.read_text()
    assert "Stakeholder comments (harvested" in body
    assert "enforced, not a polite suggestion" in body
    # marker stamped → idempotent on a second pass.
    assert "guidance_harvested: true" in md.read_text()
    before = ticket.read_text()
    res2 = close_hook.harvest_tick(cfg, slug)
    assert res2["harvested"] == [] and ticket.read_text() == before


def test_active_session_not_harvested(tmp_path, monkeypatch):
    cfg, slug, ticket, md = _setup(tmp_path, monkeypatch, status="active")
    res = close_hook.harvest_tick(cfg, slug)
    assert res["harvested"] == [] and "harvested at session close" not in ticket.read_text()


def test_already_harvested_skipped(tmp_path, monkeypatch):
    cfg, slug, ticket, md = _setup(tmp_path, monkeypatch, harvested=True)
    res = close_hook.harvest_tick(cfg, slug)
    assert res["harvested"] == []


def test_no_duplicate_of_existing_ticket_text(tmp_path, monkeypatch):
    # A comment already present in the ticket body is not re-appended.
    dup = "this exact guidance is already written into the ticket body verbatim already"
    cfg, slug, ticket, md = _setup(tmp_path, monkeypatch, messages=[_human(dup)])
    ticket.write_text(ticket.read_text() + f"\n## Context\n{dup}\n")
    res = close_hook.harvest_tick(cfg, slug)
    assert res["harvested"] == []


def test_harness_nudge_marker_excluded_from_harvest(tmp_path, monkeypatch):
    """T-1032: a mint-time-marked harness nudge is filtered even though it is
    otherwise byte-for-byte what a real human-typed message looks like in the
    transcript (type=user, userType=external, isSidechain=False) — the whole
    point of the marker is that this class of message cannot be told apart
    from real typing any other way (see input_mux.HARNESS_NUDGE_MARKER)."""
    nudge = (f"{HARNESS_NUDGE_MARKER} ⏳ FINALIZE — WRITE YOUR "
             "FORWARD-STATE ONTO T-0149. Per the process-paradigm lifecycle "
             "this incarnation is ending now.")
    cfg, slug, ticket, md = _setup(tmp_path, monkeypatch, messages=[_human(nudge)])
    res = close_hook.harvest_tick(cfg, slug)
    assert res["harvested"] == []
    assert "FINALIZE" not in ticket.read_text()


def _real_finalize_prompt(task_id: str = "T-0149") -> str:
    """The REAL composed FINALIZE prompt. A hand-typed stand-in would pin my
    model of it, and the question here is what the actual prompt does to the
    harvest filter."""
    from bot_squad_worker import autocompact
    return autocompact.context_handoff_prompt(task_id, relaunch=True)


def test_a_detached_marker_lets_the_finalize_prompt_be_harvested_as_guidance(
        tmp_path, monkeypatch):
    """T-1038 DoD 3, open question 2 — the value beside the verdict.

    `_NON_STAKEHOLDER_PREFIXES` matching is a PREFIX check, so it only ever
    protected the turn the marker is actually on. Under the pre-T-1038
    transport the prompt arrived as one turn PER LINE, so the marker sat alone
    on turn 1 and the other 20 turns matched nothing: measured 2026-09-07, 9 of
    the 21 fragments passed the filter and were harvested onto the ticket as
    "stakeholder guidance" (the append then caps at _MAX_COMMENTS=6). This test
    documents the gap that produced that population — it is the reason the fix
    had to go in the TRANSPORT, since no prefix filter can protect a body its
    marker has been detached from."""
    prompt = _real_finalize_prompt()
    detached = [_human(line) for line in prompt.split("\n")]
    cfg, slug, ticket, md = _setup(tmp_path, monkeypatch, messages=detached)

    kept = close_hook._stakeholder_comments(
        Path(close_hook.os.path.expanduser("~")) / ".claude" / "projects"
        / "-home-tester-repo" / "uuid-xyz.jsonl")

    assert len(kept) >= 5, kept          # measured 9; not 0, which is the point
    assert not any(k.startswith(HARNESS_NUDGE_MARKER) for k in kept)
    res = close_hook.harvest_tick(cfg, slug)
    assert res["harvested"], "the detached shape reaches the ticket"
    assert "FINALIZE" in ticket.read_text() or "forward-state" in ticket.read_text()


def test_the_whole_finalize_prompt_as_one_turn_is_filtered_out(tmp_path, monkeypatch):
    """The same prompt as the T-1038 transport now delivers it — ONE turn,
    marker at the head. Nothing is harvested: 0 comments, ticket untouched."""
    prompt = _real_finalize_prompt()
    cfg, slug, ticket, md = _setup(tmp_path, monkeypatch, messages=[_human(prompt)])
    before = ticket.read_text()

    res = close_hook.harvest_tick(cfg, slug)

    assert res["harvested"] == []
    assert ticket.read_text() == before
