"""``bsq ticket update <id> closed`` closes the ticket's dedicated forum topic
(if any) via the worker's ``tg_topic_close_for_ticket`` action (T-0660 Phase
2, "closeForumTopic on ticket -> done" — the actual terminal status in this
codebase's ticket vocabulary is ``closed``, not ``done``).

A no-op (no post at all beyond the status write) for every OTHER status
transition, and best-effort (never blocks/undoes an already-successful status
write) when the worker call fails."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_ticket_close_topic", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_ticket_close_topic", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(path: Path, *, status: str) -> None:
    fm = ["id: T-0001", "title: demo", f"status: {status}", "provenance: T-0577"]
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\n## Verbatim request\n\nx\n")


def test_closing_ticket_calls_close_for_ticket_action(tmp_path, monkeypatch, capsys):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="totest")
    posts: list = []
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")

    def _fake_post(action, params, timeout=35.0, fatal=True):
        posts.append((action, params))
        return {"ok": True, "closed": True, "chat_id": "111", "thread_id": 42}

    monkeypatch.setattr(bsq, "post", _fake_post)
    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", "closed"])
    args.func(args)

    assert posts == [("tg_topic_close_for_ticket", {"ticket_id": "T-0001"})]
    assert bsq.read_frontmatter(p)["status"] == "closed"
    out = capsys.readouterr().out
    assert "closed its forum topic" in out
    assert "chat_id=111" in out and "thread_id=42" in out


def test_closing_ticket_with_no_dedicated_topic_prints_nothing_extra(tmp_path, monkeypatch, capsys):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="totest")
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    monkeypatch.setattr(bsq, "post", lambda *a, **k: {"ok": True, "closed": False})

    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", "closed"])
    args.func(args)

    out = capsys.readouterr().out
    assert "closed its forum topic" not in out
    assert "T-0001: status totest -> closed" in out


def test_non_closing_transition_never_calls_close_for_ticket(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="in_progress")
    posts: list = []
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    monkeypatch.setattr(bsq, "post", lambda action, params, **k: posts.append((action, params)))

    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", "open"])
    args.func(args)

    assert posts == []


def test_close_for_ticket_worker_error_does_not_block_status_write(tmp_path, monkeypatch, capsys):
    """A TG/worker hiccup must never undo the status write that already
    succeeded — best-effort, same posture as every other TG side-effect."""
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="totest")
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")

    def _raise(*a, **k):
        raise bsq.WorkerError("worker unreachable")
    monkeypatch.setattr(bsq, "post", _raise)

    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", "closed"])
    args.func(args)  # must not raise

    assert bsq.read_frontmatter(p)["status"] == "closed"
    out = capsys.readouterr().out
    assert "T-0001: status totest -> closed" in out
