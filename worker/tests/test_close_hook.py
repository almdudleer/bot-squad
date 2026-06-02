"""Tests for T-0151 session-close guidance harvest (bot_squad_worker.close_hook)."""
from __future__ import annotations

import json
from pathlib import Path

from bot_squad_worker import close_hook, sessions as S
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
