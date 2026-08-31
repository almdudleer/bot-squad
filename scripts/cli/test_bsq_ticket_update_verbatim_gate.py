"""``bsq ticket update <id> in_progress`` refuses a stakeholder-sourced
ticket whose ``## Verbatim request`` is still empty / the task_new stub
(T-0591, D-0047 F3.3: "provenance capture is discipline-based, not
enforced" — this closes the enforcement gap at the moment real build work
starts)."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_verbatim_gate", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_verbatim_gate", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(path: Path, *, provenance: str, verbatim: str, status: str = "planned") -> None:
    fm = [
        "id: T-0001",
        "title: demo",
        f"status: {status}",
        f"provenance: {provenance}",
    ]
    body = f"## Verbatim request\n\n{verbatim}\n" if verbatim else ""
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body)


def _run_update(tmp_path, monkeypatch, status="in_progress"):
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", status])
    args.func(args)


def test_stakeholder_ticket_with_placeholder_verbatim_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, provenance="stakeholder:2026-07-05", verbatim="(filed via task_new)")
    with pytest.raises(SystemExit):
        _run_update(tmp_path, monkeypatch)
    assert bsq.read_frontmatter(p)["status"] == "planned"  # untouched


def test_stakeholder_ticket_with_empty_verbatim_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, provenance="stakeholder:2026-07-05", verbatim="")
    with pytest.raises(SystemExit):
        _run_update(tmp_path, monkeypatch)


def test_stakeholder_ticket_with_real_verbatim_proceeds(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, provenance="stakeholder:2026-07-05",
           verbatim="Please add dark mode to the settings page.")
    _run_update(tmp_path, monkeypatch)
    assert bsq.read_frontmatter(p)["status"] == "in_progress"


def test_non_stakeholder_provenance_is_never_gated(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, provenance="T-0577", verbatim="")
    _run_update(tmp_path, monkeypatch)
    assert bsq.read_frontmatter(p)["status"] == "in_progress"


def test_gate_only_applies_to_the_in_progress_transition(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, provenance="stakeholder:2026-07-05", verbatim="(filed via task_new)",
           status="in_progress")
    # T-0944 removed in_progress -> totest; to_accept is now the delivery edge
    # out of in_progress, so it's the transition this test needs.
    _run_update(tmp_path, monkeypatch, status="to_accept")
    assert bsq.read_frontmatter(p)["status"] == "to_accept"


def test_mixed_provenance_token_list_is_detected():
    assert bsq._is_stakeholder_provenance("T-0587,stakeholder:2026-07-05")
    assert not bsq._is_stakeholder_provenance("T-0587,corpus:onboarding")


def test_verbatim_placeholder_detection():
    assert bsq._verbatim_is_placeholder("")
    assert bsq._verbatim_is_placeholder("   ")
    assert bsq._verbatim_is_placeholder("(filed via task_new)")
    assert bsq._verbatim_is_placeholder("(filed via uc_new — attach user flows)")
    assert not bsq._verbatim_is_placeholder("Please add dark mode to settings.")
