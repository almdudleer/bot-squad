"""T-0751 — `bsq docs list` must print the ids the API serves.

There are THREE copies of "a doc filename stem → a doc id": the api's
`artifact_nesting.id_from_stem`, its worker mirror (pinned byte-for-byte by
`test_module_mirrors.MIRRORS`), and `bsq._doc_id_from_stem`. The CLI copy
cannot be a mirror — `scripts/cli` runs on bare `python3` and cannot import the
api or worker package — so it is pinned by BEHAVIOUR here instead: the same
corpus through both implementations must produce the same ids.

Without this, the CLI is the copy that drifts. It already had: it carried the
`stem.split("-", 2)[:2]` rule the api routes carried, so `bsq docs list`
printed `03-docs` for `roadmap/03-docs-artifacts.md` — an id the API rejected
with 400. An agent reading the CLI's listing had no way to open the doc.
"""
from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]

_loader = SourceFileLoader("bsq_mod_docids", str(_HERE / "bsq"))
_spec = importlib.util.spec_from_loader("bsq_mod_docids", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _load_api_artifact_nesting():
    """Load the api copy by PATH — `scripts/cli` has no api package on sys.path."""
    path = _REPO / "api" / "app" / "artifact_nesting.py"
    if not path.exists():  # api tree absent (isolated extract of scripts/ only)
        return None
    loader = SourceFileLoader("api_artifact_nesting_for_docid_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves its own module out of sys.modules while the class
    # body executes, so a by-path load must register first or exec_module dies
    # with `'NoneType' object has no attribute '__dict__'`.
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


#: Every stem shape that exists in `data/bot-squad/docs` today, plus the id
#: shapes the allocator can produce.
CORPUS = [
    "D-0040-knowledge-architecture-framework-skills-vs-project-docs",
    "D-0057",
    "D-10000-past-four-digits",
    "F-2026-04-15-some-feedback",
    "UC-0002",
    "01-sessions-task-manager",
    "03-docs-artifacts",
    "07-multi-server-mothership",
    "verbatim-contract",
    "guidance-corpus",
    "decision-routing-prefs-almdudleer",
    "closed-loops-principle",
    "multi-server-install-report",
    "README",
    "backlog",
    "00-RANKED-BACKLOG",
]


@pytest.mark.parametrize("stem", CORPUS)
def test_cli_derivation_matches_the_api(stem):
    api = _load_api_artifact_nesting()
    if api is None:
        pytest.skip("api tree not mounted")
    assert bsq._doc_id_from_stem(stem) == api.id_from_stem(stem)


def test_non_allocated_stem_is_its_own_id():
    """The concrete regression: no truncation at the second dash."""
    assert bsq._doc_id_from_stem("03-docs-artifacts") == "03-docs-artifacts"
    assert bsq._doc_id_from_stem("01-sessions-task-manager") == "01-sessions-task-manager"


def test_allocated_id_still_strips_its_slug():
    """The behaviour that must NOT change — `bsq docs link D-0040` still works."""
    assert bsq._doc_id_from_stem("D-0040-knowledge-architecture") == "D-0040"
    assert bsq._doc_id_from_stem("D-0057") == "D-0057"
    assert bsq._doc_id_from_stem("D-10000-x") == "D-10000"  # T-0371: ids cross 9999
