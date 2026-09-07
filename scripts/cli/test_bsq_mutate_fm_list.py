"""T-1049 — `bsq doc link` must not damage the TICKET it writes to.

`_mutate_fm_list` is the CLI half of a pair. Its twin,
`api/app/routes_docs.py::_mutate_list_field`, had the identical defect and is
fixed under the same ticket: both parsed with a bare ``yaml.safe_load`` and
serialized with a bare ``yaml.safe_dump``, bypassing the shared frontmatter
module. The bare dumper has no flow-style list representer (lists land in
BLOCK form) and no width pin (T-1044, long scalars FOLD).

WHY THAT IS WORSE THAN THE FIELD IT WRITES. `cmd_doc_link` calls this on the
TICKET path to add ``related_docs``, so it rewrites the ticket's WHOLE
frontmatter. Measured end-to-end on the API twin before the fix: linking a doc
FOLDED the ticket's title and flipped ``session_history`` from flow to BLOCK —
the field `bsq spawn`'s expert auto-resume (T-0150) reads to find the session
that already knows the ticket. A user action with nothing to do with sessions
was destroying the session record, and nothing reported it.

The CLI copy is pinned by BEHAVIOUR rather than as a mirror, the same way
`test_bsq_doc_id_derivation` pins its trio: `scripts/cli` runs on bare
``python3`` and cannot import the api or worker package.
"""
from __future__ import annotations

import importlib.util
import re
from importlib.machinery import SourceFileLoader
from pathlib import Path

_HERE = Path(__file__).resolve().parent

_loader = SourceFileLoader("bsq_mod_mutatefm", str(_HERE / "bsq"))
_spec = importlib.util.spec_from_loader("bsq_mod_mutatefm", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


#: A real 204-char title off the live board (T-1003) — long enough that pyyaml's
#: default 80-column width folds it. Composed from the producer: a hand-written
#: short title would pin nothing, because the defect only appears past the fold.
LONG_TITLE = (
    "the api/worker task_states byte-identity guard can never run in the same "
    "invocation as the pace mirror guard: they derive the worker tree from "
    "parents[3] vs parents[2], so each mount satisfies exactly one"
)

#: The shape `_append_task_session_history` actually writes — a FLOW list on one
#: line, which the board's line-based readers can read today. Starting from the
#: already-broken block form would prove nothing.
SEEDED_SIDS = ["S-a-dev-p1", "S-a-dev-p2"]


def _seed_ticket(tmp_path: Path) -> Path:
    p = tmp_path / "T-0001-thing.md"
    p.write_text(
        f'---\nid: T-0001\ntitle: "{LONG_TITLE}"\nstatus: in_progress\n'
        f"session_history: [{', '.join(SEEDED_SIDS)}]\n"
        f"updated: 2026-01-01T00:00:00Z\n---\n\n## Context\n\nstate\n",
        encoding="utf-8",
    )
    return p


def _continuation_lines(path: Path) -> list[str]:
    """Lines a 'key: value lines only' reader cannot attribute to a key —
    folded scalars and block-list items."""
    block = path.read_text(encoding="utf-8").split("---\n", 2)[1]
    return [ln for ln in block.splitlines() if ln.startswith((" ", "-", "\t"))]


def test_linking_a_doc_leaves_the_ticket_line_readable(tmp_path):
    ticket = _seed_ticket(tmp_path)
    bsq._mutate_fm_list(ticket, "related_docs", "D-0001", add=True)

    assert _continuation_lines(ticket) == [], (
        "adding related_docs reflowed the ticket into lines a line-based "
        f"reader cannot read: {_continuation_lines(ticket)}"
    )


def test_linking_a_doc_does_not_hide_the_tickets_experts(tmp_path):
    """The expensive half: `session_history` must survive as a flow list."""
    ticket = _seed_ticket(tmp_path)
    bsq._mutate_fm_list(ticket, "related_docs", "D-0001", add=True)

    got = bsq.read_frontmatter(ticket)
    assert bsq._load_frontmatter().as_list(got.get("session_history")) == SEEDED_SIDS
    # and the raw line is still the one-line form the board is written in
    block = ticket.read_text().split("---\n", 2)[1]
    assert "session_history: [S-a-dev-p1, S-a-dev-p2]" in block, (
        f"session_history is no longer a flow list:\n{block}"
    )


def test_linking_a_doc_does_not_truncate_the_tickets_title(tmp_path):
    ticket = _seed_ticket(tmp_path)
    bsq._mutate_fm_list(ticket, "related_docs", "D-0001", add=True)

    assert bsq.read_frontmatter(ticket).get("title") == LONG_TITLE


def test_the_value_is_actually_added_and_removal_is_idempotent(tmp_path):
    """HEALTHY CASE: the function still does its job. A guard that preserves
    the frontmatter perfectly while failing to write the field would pass every
    assertion above."""
    ticket = _seed_ticket(tmp_path)
    fm = bsq._load_frontmatter()

    bsq._mutate_fm_list(ticket, "related_docs", "D-0001", add=True)
    assert fm.as_list(bsq.read_frontmatter(ticket).get("related_docs")) == ["D-0001"]

    bsq._mutate_fm_list(ticket, "related_docs", "D-0001", add=True)  # idempotent
    assert fm.as_list(bsq.read_frontmatter(ticket).get("related_docs")) == ["D-0001"]

    bsq._mutate_fm_list(ticket, "related_docs", "D-0001", add=False)
    assert fm.as_list(bsq.read_frontmatter(ticket).get("related_docs")) == []


def test_repeated_links_do_not_drift_the_body(tmp_path):
    """Each link is a full read-modify-write of the file. A round-trip that
    grew a blank line, or re-quoted a scalar differently, would accumulate
    silently across links rather than failing once."""
    ticket = _seed_ticket(tmp_path)
    fm = bsq._load_frontmatter()
    first_body = None
    for i in range(3):
        bsq._mutate_fm_list(ticket, "related_docs", f"D-000{i}", add=True)
        body = fm.parse_or_none(ticket.read_text())[1]
        if first_body is None:
            first_body = body
        assert body == first_body, f"body drifted on link {i + 1}"
    assert _continuation_lines(ticket) == []


# ---------------------------------------------------------------------------
# The constraint that bit this change, pinned explicitly
# ---------------------------------------------------------------------------

def _load_by_path(rel: str):
    """Load a module BY PATH, the way `test_bsq_doc_id_derivation` loads the api
    copy — from `scripts/cli`, where neither `app` nor `bot_squad_worker` is on
    `sys.path` at all."""
    import importlib.util
    import sys
    path = _HERE.parents[1] / rel
    name = "bypath_" + rel.replace("/", "_").replace(".py", "")
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # @dataclass needs the module registered first
    loader.exec_module(mod)
    return mod


def test_artifact_nesting_loads_by_path_from_outside_either_package():
    """A mirrored module may not depend on ITS PACKAGE being importable.

    `artifact_nesting.py` is byte-identical across api and worker AND is loaded
    by path from here (`test_bsq_doc_id_derivation` pins the CLI's doc-id
    derivation against the api copy that way). T-1049 first wired its shared
    frontmatter dependency as a try/except dual import — `app` then
    `bot_squad_worker` — which satisfies byte-identity and fails HERE with
    ModuleNotFoundError on BOTH branches. It cost 16 reds in this suite.

    The fix resolves `frontmatter.py` as a SIBLING FILE, which has no package
    dependency. This test states that requirement outright instead of leaving
    it to be rediscovered through an unrelated test's failure — the way it was
    the first time.
    """
    for rel in ("api/app/artifact_nesting.py",
                "worker/bot_squad_worker/artifact_nesting.py"):
        mod = _load_by_path(rel)
        out = mod.with_frontmatter(
            {"id": "D-0001", "title": LONG_TITLE,
             "related_tickets": ["T-0001", "T-0002"]},
            "# body\n",
        )
        block = out.split("---\n", 2)[1]
        folded = [ln for ln in block.splitlines() if ln.startswith((" ", "-", "\t"))]
        assert folded == [], f"{rel} wrote unreadable lines by-path: {folded}"
        assert "related_tickets: [T-0001, T-0002]" in block, (
            f"{rel} did not use the shared dumper when loaded by path:\n{block}"
        )
