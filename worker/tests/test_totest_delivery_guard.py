"""T-0947 — to_accept discipline second sweep: guard against any prompt/doc
generator instructing the removed ``in_progress -> totest`` delivery move.

T-0944 removed that edge from TRANSITIONS (task_states.py) — a dev's delivery
now lands in ``to_accept``, one step before the human's ``totest`` queue,
which only the TL/operator promotes it into. The 2026-08-31
lifecycle-loop-audit (fable workflow) found FIVE live generators a dev/session
actually reads at delivery time that still said the opposite: two `bsq`
brief-assembly functions, the autocompact idle-deadline prompt, the
bot-squad-lifecycle skill, and qa.md's queue description (plus two smaller
doc echoes — the bot-squad-dev skill and an idle_timeout.py docstring). This
test pins those fixes and guards every such surface against the same
regression recurring, rather than trusting the fix to hold by inspection.
"""
from __future__ import annotations

import importlib.util
import re
from importlib.machinery import SourceFileLoader
from pathlib import Path

from bot_squad_worker import autocompact as A

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BSQ_PATH = _REPO_ROOT / "scripts" / "cli" / "bsq"

_loader = SourceFileLoader("bsq_mod_totest_guard", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_totest_guard", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

# The stale instruction showed up in exactly three shapes, all seen live
# pre-fix (see the ticket's Context for the audit citations):
#   "bsq ticket update <id> totest" / "ticket update {task_id} totest"
#   "frontmatter to totest"
#   "`totest` when delivered" / "totest` if it is delivered"
FORBIDDEN = re.compile(
    r"ticket\s+update\s+\S+\s+`?totest`?\b"
    r"|frontmatter\s+to\s+totest"
    r"|`?totest`?\s*(?:when|if)\s+(?:it\s+is\s+)?delivered",
    re.IGNORECASE,
)


def _assert_clean(text: str, label: str) -> None:
    m = FORBIDDEN.search(text)
    assert not m, f"{label} still instructs direct totest delivery: {m.group(0)!r}"


def test_forbidden_pattern_actually_catches_the_original_defects():
    """Positive control — prove the regex can go red before trusting it to
    guard anything (a guard nobody has watched fail is not a guard)."""
    assert FORBIDDEN.search("bsq ticket update <id> totest")
    assert FORBIDDEN.search("bsq ticket update {task_id} totest` if it is delivered")
    assert FORBIDDEN.search("set the ticket status frontmatter to totest")
    assert FORBIDDEN.search("the ticket status: `totest` when delivered")
    # legitimate mentions of totest as a real, LATER status must not trip it
    assert not FORBIDDEN.search("You verify tickets that have reached `totest`.")
    assert not FORBIDDEN.search("`bsq ticket update <id> to_accept`")
    assert not FORBIDDEN.search("moves the ticket to `totest` once accepted")


def _ticket_fixture(tmp_path: Path, ticket: str = "T-9999") -> dict:
    md = tmp_path / f"{ticket}-fixture.md"
    md.write_text(
        "---\n"
        f"id: {ticket}\n"
        "title: guard fixture\n"
        "---\n\n"
        "## Verbatim request\n\n"
        "> fixture request\n\n"
        "## DoD\n\n- ship it\n",
        encoding="utf-8",
    )
    return {"paths": {ticket: md}, "fms": {ticket: {"id": ticket, "title": "guard fixture"}}}


def test_assemble_prompt_never_instructs_direct_totest(tmp_path, monkeypatch):
    f = _ticket_fixture(tmp_path)
    monkeypatch.setattr(bsq, "_collect_guidance", lambda *a, **k: ([], 0, None))
    brief = bsq._assemble_prompt("bot-squad", ["T-9999"], f["paths"], f["fms"],
                                 "dev", "S-tl")
    _assert_clean(brief, "bsq._assemble_prompt (fresh dev spawn)")
    assert "to_accept" in brief


def test_assemble_delta_brief_never_instructs_direct_totest(tmp_path):
    f = _ticket_fixture(tmp_path)
    expert = {"tickets": ["T-0001"], "reasons": ["overlap"]}
    brief = bsq._assemble_delta_brief("bot-squad", ["T-9999"], f["fms"], f["paths"],
                                      expert, "S-tl")
    _assert_clean(brief, "bsq._assemble_delta_brief (resumed expert)")
    assert "to_accept" in brief


def test_default_brief_never_instructs_direct_totest(tmp_path, monkeypatch):
    monkeypatch.setattr(bsq, "my_sid", lambda: "S-tl")
    f = _ticket_fixture(tmp_path)
    ticket = "T-9999"
    brief = bsq._default_brief("bot-squad", ticket, f["paths"][ticket],
                               f["fms"][ticket], [])
    _assert_clean(brief, "bsq._default_brief")
    assert "to_accept" in brief


def test_autocompact_deadline_prompt_never_instructs_direct_totest():
    # The stale text lived on the idle-window branch (relaunch=False,
    # stay=False, resume=False) — the one that names the ticket-status verb.
    p = A.context_handoff_prompt("T-0042", relaunch=False, resume=False)
    _assert_clean(p, "autocompact.context_handoff_prompt (idle deadline, no resume)")
    assert "to_accept" in p


_DOC_SURFACES = [
    _REPO_ROOT / "framework-skills" / "bot-squad-lifecycle" / "SKILL.md",
    _REPO_ROOT / "framework-skills" / "bot-squad-dev" / "SKILL.md",
    _REPO_ROOT / "api" / "app" / "resources" / "roles" / "qa.md",
    _REPO_ROOT / "api" / "app" / "resources" / "roles" / "dev.md",
    _REPO_ROOT / "api" / "app" / "resources" / "roles" / "teamlead.md",
]


def test_doc_surfaces_never_instruct_direct_totest():
    for path in _DOC_SURFACES:
        assert path.exists(), f"expected doc surface missing: {path}"
        _assert_clean(path.read_text(encoding="utf-8"), str(path))
