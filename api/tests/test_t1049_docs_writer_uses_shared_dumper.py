"""T-1049: the docs/artifact writers must not bypass the shared frontmatter dumper.

`api/app/routes_docs.py::_write_frontmatter` and the two `artifact_nesting.py`
sites serialize with a bare ``yaml.safe_dump``, bypassing
``app.frontmatter.dump_frontmatter``. They therefore inherit none of what that
module exists to guarantee (T-0075):

* no flow-style list representer, so lists are written in BLOCK form, which
  `scripts/cli/bsq`'s line-based `read_frontmatter` reads as EMPTY;
* no width pin (T-1044), so a long scalar is FOLDED across lines, which the
  same reader truncates at the fold with the opening quote attached.

Measured on the live board: `related_tickets` wrong on 37 of 62 docs, `title`
on 19, and block is the DOMINANT and more recent shape for docs -- every doc
written through the API since 2026-07-30.

THE PART THAT MAKES THIS MORE THAN A DOCS BUG. `_mutate_list_field` calls
`_write_frontmatter` on a BACKLOG TICKET, not only on a doc: linking a doc to a
ticket writes `related_docs` on the ticket side. So this writer rewrites a
ticket's WHOLE frontmatter with the bare dumper -- including `session_history`,
the field `bsq`'s expert discovery reads (T-1047 decision site 1) -- through a
door that is live today.

THE TRAP, and why the obvious fix is not a three-line change. The shared dumper
strips the timestamp resolver, so it writes `created` UNQUOTED where the bare
dumper quotes it. This module's own readers are bare ``yaml.safe_load``
(L100/L378/L446), which resolve the unquoted form to a ``datetime`` -- and
``json.dumps`` then raises. Swapping ONLY the writer therefore 500s the docs
endpoints that serialize `created`. Writer and readers move together or not at
all, which is what `test_created_still_round_trips_through_the_api` pins.

INSTRUMENT: authored and run against the install's api venv
(/home/www/bot-squad/api/.venv, fastapi 0.141.1), NOT the container. The
container remains the release gate; a green here is not a gate result.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app


LONG_TITLE = (
    "the api/worker task_states byte-identity guard can never run in the same "
    "invocation as the pace mirror guard: they derive the worker tree from "
    "parents[3] vs parents[2], so each mount satisfies exactly one"
)


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


def _login(client) -> None:
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})


def _seed_ticket(tmp_bot_squad: Path, tid: str = "T-0001") -> Path:
    """A ticket in the shape the live board actually uses: a long title and a
    FLOW-style session_history, i.e. one the line-based reader can read TODAY.
    Composed from the producer -- this is `_append_task_session_history`'s
    output format, not a hand-invented one."""
    p = tmp_bot_squad / "data" / "test-project" / "backlog" / f"{tid}-thing.md"
    p.write_text(
        f'---\nid: {tid}\ntitle: "{LONG_TITLE}"\nstatus: in_progress\n'
        f"session_history: [S-a-dev-p1, S-a-dev-p2]\n"
        f"updated: 2026-01-01T00:00:00Z\n---\n\n## Context\n\nstate\n",
        encoding="utf-8",
    )
    return p


def _frontmatter_block(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---\n", 2)[1]


def _continuation_lines(path: Path) -> list[str]:
    """Lines a "key: value lines only" reader cannot attribute to a key --
    i.e. folded scalars and block-list items. `session_history_ts` is a
    legitimate nested map; no fixture here has one."""
    return [ln for ln in _frontmatter_block(path).splitlines()
            if ln.startswith((" ", "-", "\t"))]


def _bsq_read_frontmatter(path: Path):
    """`scripts/cli/bsq`'s OWN reader, executed from the CLI file.

    Not reimplemented here: the whole point is that this reader disagrees with
    a pyyaml writer, so only the real thing can show it. Returns None when the
    CLI is absent or a peer has it mid-edit -- it is a live shared file every
    session on this box executes, and that is a fact about the tree rather than
    about the writer under test.
    """
    for parent in Path(__file__).resolve().parents:
        cli = parent / "scripts" / "cli" / "bsq"
        if cli.is_file():
            break
    else:
        return None
    spec = importlib.util.spec_from_loader("_bsq_cli_under_test", None)
    mod = importlib.util.module_from_spec(spec)
    # T-1049: a module executed with `exec` has no `__file__` unless we
    # supply one, and the real CLI needs it: `read_frontmatter` now
    # delegates to a sibling `frontmatter.py` resolved via
    # `Path(__file__).resolve().parent` (T-1047's reader fix). Without this
    # the exec raises NameError and the test fails for a reason that has
    # nothing to do with the code under test. Setting it makes the exec
    # behave like a real import, which is what we are simulating.
    mod.__dict__["__file__"] = str(cli)
    src = cli.read_text().replace('if __name__ == "__main__":\n    main()', "")
    try:
        exec(compile(src, str(cli), "exec"), mod.__dict__)  # noqa: S102
        return mod.read_frontmatter(path)
    except SyntaxError:
        return None  # a peer has the live shared file mid-edit


# ---------------------------------------------------------------------------
# The trap: this must be GREEN before and after the fix
# ---------------------------------------------------------------------------

def test_created_still_round_trips_through_the_api(tmp_bot_squad: Path, monkeypatch):
    """Regression pin on the API surface: every docs read path keeps rendering.

    This one does NOT pin the writer/reader coupling, and saying so matters --
    the mutation matrix showed it stays green when the readers are reverted to
    bare ``yaml.safe_load``, because FastAPI's ``jsonable_encoder`` serializes
    the resulting ``datetime`` on the way out and the endpoint still returns
    200. `test_the_reader_returns_created_as_a_string` is what actually pins
    the coupling; this one guards the surface."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post("/api/projects/test-project/docs",
                        json={"category": "product", "title": "Doc"})
        assert r.status_code == 200, r.text
        doc_id = r.json()["id"]

        # Every read path that serializes the doc must survive it.
        got = client.get(f"/api/projects/test-project/docs/{doc_id}")
        assert got.status_code == 200, got.text
        listing = client.get("/api/projects/test-project/docs")
        assert listing.status_code == 200, listing.text

        # And after a WRITE through the bypassing writer, not just on create.
        _seed_ticket(tmp_bot_squad)
        link = client.post(f"/api/projects/test-project/docs/{doc_id}/link",
                           json={"ticket": "T-0001"})
        assert link.status_code == 200, link.text
        again = client.get(f"/api/projects/test-project/docs/{doc_id}")
        assert again.status_code == 200, again.text


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------

def test_linking_writes_a_doc_list_the_line_reader_can_read(
        tmp_bot_squad: Path, monkeypatch):
    """`related_tickets` must not be written in block form -- 37 of 62 live
    docs are, and the line-based reader returns EMPTY for every one."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc_id = client.post("/api/projects/test-project/docs",
                             json={"category": "product", "title": "Doc"}).json()["id"]
        _seed_ticket(tmp_bot_squad)
        assert client.post(f"/api/projects/test-project/docs/{doc_id}/link",
                           json={"ticket": "T-0001"}).status_code == 200

    docs = list((tmp_bot_squad / "data" / "test-project" / "docs").rglob(f"{doc_id}*.md"))
    assert len(docs) == 1, f"expected one doc md, found {docs}"
    assert _continuation_lines(docs[0]) == [], (
        "doc frontmatter carries lines a line-based reader cannot attribute "
        f"to a key: {_continuation_lines(docs[0])}"
    )

    bsq = _bsq_read_frontmatter(docs[0])
    if bsq is None:
        pytest.skip("scripts/cli/bsq not readable here — the continuation-line "
                    "assertion above already holds the finding")
    assert bsq.get("related_tickets"), (
        "bsq read_frontmatter sees no related_tickets on a doc that has one"
    )


def test_linking_does_not_damage_the_TICKET_it_writes_to(
        tmp_bot_squad: Path, monkeypatch):
    """The cross-class arm, and the reason this is not only a docs bug.

    `_mutate_list_field` calls `_write_frontmatter` on the TICKET to add
    `related_docs`. That rewrites the ticket's WHOLE frontmatter with the bare
    dumper -- so a flow-style `session_history` becomes block (unreadable to
    `bsq`'s expert discovery, T-1047 decision site 1) and a long title folds,
    purely as a side effect of someone linking a doc.
    """
    ticket = _seed_ticket(tmp_bot_squad)
    before_sids = ["S-a-dev-p1", "S-a-dev-p2"]

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc_id = client.post("/api/projects/test-project/docs",
                             json={"category": "product", "title": "Doc"}).json()["id"]
        assert client.post(f"/api/projects/test-project/docs/{doc_id}/link",
                           json={"ticket": "T-0001"}).status_code == 200

    assert _continuation_lines(ticket) == [], (
        "linking a doc reflowed the TICKET's frontmatter into lines a "
        f"line-based reader cannot read: {_continuation_lines(ticket)}"
    )

    bsq = _bsq_read_frontmatter(ticket)
    if bsq is None:
        pytest.skip("scripts/cli/bsq not readable here — the continuation-line "
                    "assertion above already holds the finding")
    sids = sorted(re.findall(r"S-[A-Za-z0-9_.-]+",
                             str(bsq.get("session_history") or "")))
    assert sids == before_sids, (
        f"expert discovery lost SIDs to a doc link: {sids} != {before_sids}"
    )
    assert bsq.get("title") == LONG_TITLE, (
        f"the ticket's title was truncated to {len(str(bsq.get('title')))} "
        f"of {len(LONG_TITLE)} chars by a doc link"
    )


def test_the_bsq_reader_used_above_was_actually_located():
    """A guard against the assertions above passing SILENTLY.

    `_bsq_read_frontmatter` returns None when the CLI is missing or a peer has
    it mid-edit, and the two tests then SKIP rather than fail — correct, because
    the continuation-line assertions hold the finding without it. But a skip is
    invisible in a `-q` run, so a tree where the reader never runs would report
    the same green as one where it did. This test makes that state legible: it
    fails here rather than letting the corroboration quietly stop happening.

    (Learned on T-1044: a mutation control read as "the guard works" when the
    real cause was a test that could not find this same file.)
    """
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as d:
        probe = Path(d) / "T-0001-probe.md"
        probe.write_text("---\nid: T-0001\nstatus: open\n---\n\nbody\n")
        got = _bsq_read_frontmatter(probe)
    assert got is not None, (
        "scripts/cli/bsq could not be located or executed — the bsq-reader "
        "assertions in this module are skipping, so this file is proving less "
        "than its green suggests"
    )
    assert got.get("id") == "T-0001"


def test_the_reader_returns_created_as_a_string(tmp_bot_squad: Path, monkeypatch):
    """THE COUPLING PIN: writer and readers must move together.

    The shared dumper writes `created` UNQUOTED. The shared parser strips the
    timestamp resolver and gives a ``str`` back; a bare ``yaml.safe_load`` gives
    a ``datetime``. Nothing 500s either way -- FastAPI encodes the datetime --
    so the damage is a field that silently changes TYPE for every in-process
    reader, surfacing far from the change (``routes_analytics`` L147 compares
    ``updated >= created``, which a mixed str/datetime comparison breaks).

    This asserts the type at the module's own reader, which is the only place
    the disagreement is visible. Written because the mutation matrix proved the
    API-surface test above could not see it.
    """
    from app import routes_docs as rd

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        doc_id = client.post("/api/projects/test-project/docs",
                             json={"category": "product", "title": "Doc"}).json()["id"]

    path = next((tmp_bot_squad / "data" / "test-project" / "docs").rglob(f"{doc_id}*.md"))
    # `routes_docs._parse` — THE MODULE'S OWN READER, called directly. An
    # earlier cut of this test fell back to `frontmatter.parse_or_none` when a
    # helper name did not resolve, which meant it asserted a property of the
    # SHARED parser no matter what this module did: the mutation matrix showed
    # it staying green with the readers reverted. A fallback that bypasses the
    # code under test cannot fail for the reason the test exists.
    meta = rd._parse(path)
    assert isinstance(meta.get("created"), str), (
        f"created came back as {type(meta.get('created')).__name__}, not str — "
        "a reader is not using the shared parser while the writer is using the "
        "shared dumper"
    )


def test_artifact_nesting_writer_keeps_frontmatter_line_readable(tmp_bot_squad: Path):
    """Cover `artifact_nesting`'s TWO writers directly.

    The mutation matrix found them uncovered: reverting both to a bare
    ``yaml.safe_dump`` reddened nothing, because every other test in this file
    reaches the writer in `routes_docs`. A fix nobody's test can see removed is
    a fix that silently rots.
    """
    from app import artifact_nesting as AN

    meta = {
        "id": "D-0001",
        "title": LONG_TITLE,
        "related_tickets": ["T-0001", "T-0002"],
        "created": "2026-01-01T00:00:00Z",
    }
    out = AN.with_frontmatter(meta, "# body\n")
    block = out.split("---\n", 2)[1]
    folded = [ln for ln in block.splitlines() if ln.startswith((" ", "-", "\t"))]
    assert folded == [], (
        f"artifact_nesting wrote lines a line-based reader cannot read: {folded}"
    )
    assert "related_tickets: [T-0001, T-0002]" in block, (
        f"expected a flow-style list, got:\n{block}"
    )
