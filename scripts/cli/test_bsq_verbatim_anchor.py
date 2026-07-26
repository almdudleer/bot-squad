"""Tests for the tasks-as-glue verbatim anchor in spawn briefs (T-0483, M3/F3.6).

A request is recorded once (verbatim) in a task; every downstream session reads
the TASK rather than a relayed paraphrase — defeating broken-telephone + target
drift. The spawn-brief assembly must (a) hand the session the ticket's
`## Verbatim request` BYTE-FOR-BYTE (no paraphrase), and (b) explicitly instruct
it to ANCHOR on that string at start. This guards both the fresh-spawn brief
(`_assemble_prompt`) and the resumed-expert delta brief (`_assemble_delta_brief`).

`bsq` is an extensionless script loaded via SourceFileLoader.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_anchor", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_anchor", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

# A distinctive verbatim string a relay/paraphrase would mangle.
VERBATIM = ('"user request processed conversationally then recorded in a task; '
            'downstream sessions read the task" — the EXACT stakeholder words.')
TICKET = "T-9999"


def _write_ticket(tmp_path: Path) -> dict:
    """Drop a synthetic ticket md with a known verbatim; return (paths, fms)."""
    md = tmp_path / f"{TICKET}-fixture.md"
    md.write_text(
        "---\n"
        f"id: {TICKET}\n"
        "title: anchor fixture\n"
        "---\n\n"
        "## Verbatim request\n\n"
        f"> {VERBATIM}\n\n"
        "## DoD\n\n- ship it\n",
        encoding="utf-8",
    )
    return {"paths": {TICKET: md}, "fms": {TICKET: {"id": TICKET, "title": "anchor fixture"}}}


def test_fresh_spawn_brief_hands_verbatim_and_anchors(tmp_path, monkeypatch):
    f = _write_ticket(tmp_path)
    # Neutralize the heavy/IO-bound guidance search; role file is read from the
    # live tree (exists) so leave it. _assemble_prompt(slug, tickets, paths, fms,
    # role, report_to).
    monkeypatch.setattr(bsq, "_collect_guidance", lambda *a, **k: ([], 0, None))
    brief = bsq._assemble_prompt("bot-squad", [TICKET], f["paths"], f["fms"],
                                 "dev", "S-tl")
    # (a) verbatim handed byte-for-byte — no paraphrase loss.
    assert VERBATIM in brief
    # (b) explicit START anchor + STAY-ON-TASK both point at the verbatim ask.
    assert "START HERE" in brief and "ANCHOR ON THE ASK" in brief
    assert f"{TICKET}'s `## Verbatim request`" in brief          # START anchor
    assert "STAY ON TASK" in brief
    stay_on_task = brief[brief.index("STAY ON TASK"):]
    assert f"{TICKET}'s `## Verbatim request`" in stay_on_task   # rule #4 anchor
    assert "{primary}" not in brief                              # f-string resolved


def test_delta_brief_anchors_resumed_expert_on_verbatim(tmp_path):
    f = _write_ticket(tmp_path)
    expert = {"tickets": ["T-0001"], "reasons": ["overlap"]}
    brief = bsq._assemble_delta_brief("bot-squad", [TICKET], f["fms"], f["paths"],
                                      expert, "S-tl")
    assert VERBATIM in brief                                     # handed verbatim
    assert "ANCHOR ON THE ASK" in brief
    assert "`## Verbatim request`" in brief
