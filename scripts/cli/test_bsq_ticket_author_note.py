"""T-0938: `bsq ticket update` reports its authorship, and NEVER fails over it.

The status write happens in this process — it never reaches a worker action —
which is why the ticket-update fan-out polls the md rather than hooking writers.
The attribution post exists only so the session that moved a status isn't nudged
about its own move.

That makes it strictly best-effort, and the case that proves it matters is a
DEPLOY WINDOW: `bsq` ships by git and starts running before the worker restarts,
so for a few minutes the new CLI talks to a worker that has never heard of
`ticket_author_note`. If that could abort the verb, every status update on the
fleet would break during every deploy — trading a nudge nobody would miss for
the one verb the lifecycle runs on.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_ticket_author_note", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_ticket_author_note", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(path: Path, *, status: str) -> None:
    path.write_text(
        "---\nid: T-0001\ntitle: demo\n"
        f"status: {status}\nprovenance: T-0577\n---\n\n"
        "## Verbatim request\n\nx\n"
    )


def _run(tmp_path, monkeypatch, post, *, frm="planned", to="open"):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status=frm)
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    monkeypatch.setattr(bsq, "post", post)
    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", to, "--sid", "S-t-p1"])
    args.func(args)
    return p


def test_a_status_move_reports_who_moved_it(tmp_path, monkeypatch):
    posts: list = []
    _run(tmp_path, monkeypatch,
         lambda action, params, **k: posts.append((action, params)))
    assert ("ticket_author_note", {
        "slug": "demo", "task_id": "T-0001", "sid": "S-t-p1", "keys": ["status"],
    }) in [(a, dict(p)) for a, p in posts] or [
        p for a, p in posts if a == "ticket_author_note"
    ], f"no authorship reported: {posts}"


def test_a_no_op_transition_reports_nothing(tmp_path, monkeypatch):
    """Re-setting the same status changes no bytes, so there is nothing for the
    fan-out to see and nothing to attribute."""
    posts: list = []
    _run(tmp_path, monkeypatch,
         lambda action, params, **k: posts.append((action, params)),
         frm="open", to="open")
    assert [a for a, _ in posts] == []


def test_an_old_worker_that_rejects_the_action_does_not_break_the_verb(
        tmp_path, monkeypatch, capsys):
    """The deploy-window case: the new CLI, the not-yet-restarted worker."""
    def _reject(action, params, **k):
        if action == "ticket_author_note":
            raise bsq.WorkerError("worker rejected ticket_author_note: "
                                  "unknown action: 'ticket_author_note'")
        return {"ok": True}

    p = _run(tmp_path, monkeypatch, _reject)
    assert bsq.read_frontmatter(p)["status"] == "open"      # the write LANDED
    out = capsys.readouterr()
    assert "T-0001: status planned -> open" in out.out
    assert "ticket_author_note" not in out.out + out.err     # and stayed quiet


def test_an_unreachable_worker_does_not_break_the_verb(tmp_path, monkeypatch):
    def _down(action, params, **k):
        raise bsq.WorkerError("connection refused")
    p = _run(tmp_path, monkeypatch, _down)
    assert bsq.read_frontmatter(p)["status"] == "open"
