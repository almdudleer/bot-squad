"""T-0751 — the list endpoint and the detail endpoint must agree what a doc id IS.

They didn't. ``list_docs`` derived an id from the filename stem with
``stem.split("-", 2)[:2]``, which assumes every doc file is ``D-NNNN-<slug>``;
``get_doc`` validated against ``^D-\\d{4,}$``. So
``docs/roadmap/03-docs-artifacts.md`` was ADVERTISED in the tree as ``03-docs``
and then 400'd when opened. Measured on the live install before the fix: 19 of
74 docs (26%) — roadmap 13, design 3, qa 2, operator 1 — listed but unopenable.

Two invariants are pinned here, and they are the whole ticket:

1. **Agreement.** Every id the list endpoint emits resolves on the detail
   endpoint. Asserted as a property over the seeded tree, not as a hand-listed
   set, so a new filename shape can't quietly reopen the gap.
2. **Traversal safety.** The old ``^D-\\d{4,}$`` was doing double duty as the
   path-traversal guard, because ``doc_id`` was interpolated into
   ``docs/*/{doc_id}-*.md``. Widening the id shape without moving that guard is
   the one mistake on this ticket that would be worse than the bug. Resolution
   now enumerates the tree and compares derived ids — no caller string reaches
   a path — and the battery below demonstrates it rather than documenting it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import artifact_nesting as AN
from app.main import build_app


def _client(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


def _login(client) -> None:
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})


def _docs_root(tmp_bot_squad: Path) -> Path:
    return tmp_bot_squad / "data" / "test-project" / "docs"


#: The real shapes that live in ``data/bot-squad/docs`` today. Each entry is
#: ``(category, filename stem, expected id)``. The non-``D-NNNN`` ones are the
#: 19 the tree advertised and could not open.
LIVE_SHAPES = [
    # allocated ids — unchanged by this ticket, kept as the regression floor
    ("architecture", "D-0040-knowledge-architecture-framework-skills-vs-project-docs", "D-0040"),
    ("architecture", "D-10000-past-four-digits", "D-10000"),  # T-0371
    ("design", "D-0057", "D-0057"),  # bare id, no slug suffix
    # numeric-prefixed roadmap chapters — `split("-", 2)[:2]` gave `01-sessions`
    ("roadmap", "01-sessions-task-manager", "01-sessions-task-manager"),
    ("roadmap", "03-docs-artifacts", "03-docs-artifacts"),
    ("roadmap", "07-multi-server-mothership", "07-multi-server-mothership"),
    # two-word stems — mangled to themselves, but still 400'd by the id regex
    ("roadmap", "verbatim-contract", "verbatim-contract"),
    ("roadmap", "guidance-corpus", "guidance-corpus"),
    # single-token stems
    ("roadmap", "README", "README"),
    ("roadmap", "backlog", "backlog"),
    # not roadmap-only: the same disease in three other categories
    ("design", "closed-loops-principle", "closed-loops-principle"),
    ("design", "session-lifecycle-state-machine", "session-lifecycle-state-machine"),
    ("qa", "multi-server-install-report", "multi-server-install-report"),
    ("operator", "decision-routing-prefs-almdudleer", "decision-routing-prefs-almdudleer"),
]


def _seed_live_shapes(tmp_bot_squad: Path) -> None:
    """Write one file per LIVE_SHAPES entry, mirroring how they exist on disk.

    Frontmatter presence is varied on purpose: the roadmap chapters carry
    frontmatter WITHOUT an ``id:`` field (so the id is derived), and
    ``closed-loops-principle`` has no frontmatter at all — both real cases.
    """
    root = _docs_root(tmp_bot_squad)
    for category, stem, _ in LIVE_SHAPES:
        d = root / category
        d.mkdir(parents=True, exist_ok=True)
        if stem == "closed-loops-principle":
            body = f"# {stem}\n\nno frontmatter at all, like the real file\n"
        elif stem.startswith("D-"):
            body = (
                f"---\nid: {stem.split('-')[0]}-{stem.split('-')[1]}\n"
                f"title: {stem}\nstatus: draft\n---\n\n# {stem}\n"
            )
        else:
            # frontmatter, but no `id:` — the roadmap/qa/operator shape
            body = f"---\ntitle: {stem} title\nticket: T-0244\n---\n\n# {stem}\n\nbody of {stem}\n"
        (d / f"{stem}.md").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Agreement — everything listed opens
# ---------------------------------------------------------------------------
def test_every_listed_doc_opens(tmp_bot_squad: Path, monkeypatch):
    """The invariant, stated as a property: list → GET each → 200.

    This is the test that was RED before the fix, with 11 of 15 ids 400ing.
    """
    _seed_live_shapes(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        listed = client.get("/api/projects/test-project/docs").json()
        assert len(listed) == len(LIVE_SHAPES)

        unopenable = []
        for item in listed:
            r = client.get(f"/api/projects/test-project/docs/{item['id']}")
            if r.status_code != 200:
                unopenable.append((item["id"], item["category"], r.status_code))
        assert unopenable == [], f"listed but not openable: {unopenable}"


def test_listed_id_matches_the_shared_derivation(tmp_bot_squad: Path, monkeypatch):
    """The list endpoint emits exactly ``AN.id_from_stem`` — no second rule.

    ``01-sessions-task-manager`` listing as ``01-sessions`` is the concrete
    defect: an id that names no file, in a tree that offered it as a link.
    """
    _seed_live_shapes(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        listed = {d["id"] for d in client.get("/api/projects/test-project/docs").json()}
    assert listed == {expected for _, _, expected in LIVE_SHAPES}
    for _, stem, expected in LIVE_SHAPES:
        assert AN.id_from_stem(stem) == expected


def test_opened_doc_serves_the_right_file(tmp_bot_squad: Path, monkeypatch):
    """Agreement is not enough — the id must resolve to ITS OWN file.

    ``03-docs-artifacts`` and ``04-...`` share a numeric prefix style, and
    ``D-0057`` is a bare stem sitting next to slug-suffixed siblings; a
    prefix-matching resolver can serve the wrong one and still return 200.
    """
    _seed_live_shapes(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        for category, stem, doc_id in LIVE_SHAPES:
            r = client.get(f"/api/projects/test-project/docs/{doc_id}")
            assert r.status_code == 200, (doc_id, r.text)
            out = r.json()
            assert out["id"] == doc_id
            assert out["category"] == category
            assert Path(out["path"]).name == f"{stem}.md"


def test_non_allocated_doc_can_mother_children(tmp_bot_squad: Path, monkeypatch):
    """Nesting keys on the SAME id, so a roadmap chapter can be a mother page.

    ``parent_doc_id`` is matched against ``artifact_nesting``'s derivation. When
    the docs routes carried their own, a child pointing at ``03-docs-artifacts``
    was invisible to a tree built from the list endpoint's ``03-docs``.
    """
    _seed_live_shapes(tmp_bot_squad)
    child = _docs_root(tmp_bot_squad) / "roadmap" / "gap-notes.md"
    child.write_text(
        "---\ntitle: gap notes\nparent_doc_id: 03-docs-artifacts\n---\n\n# gap\n",
        encoding="utf-8",
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/test-project/docs/03-docs-artifacts")
        assert r.status_code == 200, r.text
        assert r.json()["child_artifact_ids"] == ["gap-notes"]
        kids = client.get("/api/projects/test-project/docs/03-docs-artifacts/children")
        assert [k["id"] for k in kids.json()] == ["gap-notes"]


# ---------------------------------------------------------------------------
# 2. Traversal safety — the guard the old regex was doubling as
# ---------------------------------------------------------------------------
#: Ids that must never resolve to anything. Some are rejected by shape (400),
#: some simply match no enumerated doc (404); the assertion is that NONE of them
#: ever returns content, and that no file outside the docs tree is reachable.
HOSTILE_IDS = [
    "..",
    ".",
    "../..",
    "../../etc/passwd",
    "/etc/passwd",
    "roadmap/../../secret",
    "..%2f..%2fsecret",
    "\\..\\secret",
    "D-0040/../../secret",
    "secret\x00.md",
    "**/*",  # a glob AND a separator — belongs with the traversal shapes
    "x" * 4096,
]

#: Glob metacharacters. These are legal filename characters, so the shape gate
#: deliberately does NOT reject them — the guarantee is that resolution never
#: expands them, because the id is compared, never globbed.
GLOB_IDS = ["*", "?", "D-*", "D-00??", "[a-z]*", "D-0040*"]


def _as_path_segment(raw: str) -> str:
    """Percent-encode so the string arrives at the handler as ONE path segment.

    Sent raw, `..` / `.` / `?` never reach ``get_doc`` at all — the HTTP client
    and the router resolve them first, and the request lands on a different
    endpoint entirely (the project detail, the docs LIST, a query string). Those
    are 200s that carry no doc and prove nothing either way. Encoding is what
    puts the hostile string in front of the code under test.

    `urllib.parse.quote(safe="")` is NOT enough: `.` is an unreserved character
    so `..` survives verbatim and gets normalised away again. Everything except
    the unreserved set is encoded here, dots included.
    """
    return "".join(
        c if (c.isascii() and (c.isalnum() or c in "-_~"))
        else "".join(f"%{b:02X}" for b in c.encode("utf-8"))
        for c in raw
    )


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_doc_id_never_resolves(tmp_bot_squad: Path, monkeypatch, hostile):
    _seed_live_shapes(tmp_bot_squad)
    # a file the traversal would be aiming at, one level above the docs tree
    secret = _docs_root(tmp_bot_squad).parent / "secret.md"
    secret.write_text("---\nid: secret\n---\n\nTOP SECRET\n", encoding="utf-8")

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get(f"/api/projects/test-project/docs/{_as_path_segment(hostile)}")
        assert r.status_code in (400, 404), (hostile, r.status_code, r.text[:400])
        assert "TOP SECRET" not in r.text


@pytest.mark.parametrize("pattern", GLOB_IDS)
def test_glob_metacharacters_do_not_expand(tmp_bot_squad: Path, monkeypatch, pattern):
    """A pattern must not be able to stand in for a real doc.

    The SECURITY property held before the fix too — the old ``^D-\\d{4,}$``
    rejected these outright — and the point of the test is that it still holds
    once that regex is gone and glob metacharacters are syntactically legal
    ids. It goes red on the unfixed sources for a status-code reason only (400,
    not 404), never because a pattern matched a file.
    """
    _seed_live_shapes(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get(f"/api/projects/test-project/docs/{_as_path_segment(pattern)}")
        assert r.status_code == 404, (pattern, r.status_code, r.text[:200])
        # The real assertion: a not-found error, not a doc payload. (Checking
        # for the absence of `D-0040` in the text would be wrong — the 404
        # detail echoes the pattern, and `D-0040*` contains it.)
        body = r.json()
        assert set(body) == {"detail"} and body["detail"].startswith("doc not found")


def test_write_verbs_share_the_same_gate(tmp_bot_squad: Path, monkeypatch):
    """PUT / DELETE / link / parent resolve through the same resolver.

    They all called the old glob too, so widening only ``get_doc`` would have
    left the mutating verbs on the interpolating path.
    """
    _seed_live_shapes(tmp_bot_squad)
    secret = _docs_root(tmp_bot_squad).parent / "secret.md"
    secret.write_text("---\nid: secret\n---\n\nTOP SECRET\n", encoding="utf-8")

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        hostile = _as_path_segment("../secret")
        assert client.put(
            f"/api/projects/test-project/docs/{hostile}",
            json={"content": "pwned"},
        ).status_code != 200
        assert client.delete(f"/api/projects/test-project/docs/{hostile}").status_code != 200
        assert client.post(
            f"/api/projects/test-project/docs/{hostile}/link", json={"ticket": "T-0751"},
        ).status_code != 200
        assert client.put(
            f"/api/projects/test-project/docs/{hostile}/parent",
            json={"parent_doc_id": None},
        ).status_code != 200
    assert secret.read_text(encoding="utf-8").endswith("TOP SECRET\n")


def test_find_doc_stays_inside_the_docs_tree(tmp_path: Path):
    """Module-level proof, independent of the HTTP layer's own decoding.

    ``AN.find_doc`` can only ever return a path it enumerated from
    ``<project>/docs/<category>/*.md``, so escape is not a matter of pattern
    strength.
    """
    docs = tmp_path / "docs" / "roadmap"
    docs.mkdir(parents=True)
    (docs / "03-docs-artifacts.md").write_text("---\ntitle: x\n---\n\nbody\n")
    (tmp_path / "secret.md").write_text("---\nid: secret\n---\n\nTOP SECRET\n")

    assert AN.find_doc(tmp_path, "03-docs-artifacts").path == docs / "03-docs-artifacts.md"
    for hostile in HOSTILE_IDS + GLOB_IDS + ["secret", "../secret"]:
        assert AN.find_doc(tmp_path, hostile) is None, hostile


@pytest.mark.parametrize(
    "candidate,ok",
    [
        ("D-0040", True),
        ("03-docs-artifacts", True),
        ("verbatim-contract", True),
        ("README", True),
        ("a.b", True),          # a dot is a legal filename character
        ("F-2026-04-15-x", True),
        ("*", True),            # legal in a filename; resolution, not shape, rejects it
        ("", False),
        (".", False),
        ("..", False),
        ("a/b", False),
        ("a\\b", False),
        ("a\x00b", False),
        ("x" * 256, False),
    ],
)
def test_artifact_id_shape_gate(candidate, ok):
    """The gate excludes only what can never BE a filename component.

    Not a charset whitelist, on purpose: ids are derived FROM filenames, so any
    narrower charset re-creates this ticket for the next unusual filename.
    """
    assert AN.is_valid_artifact_id(candidate) is ok


# ---------------------------------------------------------------------------
# 4. T-0756 — the list endpoint ships the STEM the id was derived FROM
# ---------------------------------------------------------------------------
def test_listed_doc_carries_its_filename_stem(tmp_bot_squad: Path, monkeypatch):
    """Doc bodies cross-link by relative FILENAME; ids are what the app routes on.

    Something has to bridge the two, and `id_from_stem` is a python function
    the web bundle cannot call. Re-implementing it in TypeScript would be the
    second copy this whole module exists to prevent (T-0751 deleted three), so
    the list endpoint ships the stem instead and the renderer resolves by
    LOOKUP. That makes this field load-bearing for T-0756, not decoration:
    without it a link to `D-0057-t-0637-….md` cannot find `D-0057`.
    """
    _seed_live_shapes(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        listed = client.get("/api/projects/test-project/docs").json()

    by_stem = {d["stem"]: d for d in listed}
    assert len(by_stem) == len(listed), "stems must be unique per listing"
    for category, stem, expected_id in LIVE_SHAPES:
        row = by_stem[stem]
        assert row["category"] == category
        # The pair is the contract: this id came from THIS stem, by the one
        # shared derivation. A consumer joining on stem lands on the same doc
        # the detail endpoint would serve.
        assert row["id"] == expected_id == AN.id_from_stem(stem)


def test_every_listed_stem_resolves_through_its_id(tmp_bot_squad: Path, monkeypatch):
    """Stated as a property: stem -> id -> GET is a 200 for every doc.

    This is the round trip the renderer performs on every relative `.md` link.
    """
    _seed_live_shapes(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        listed = client.get("/api/projects/test-project/docs").json()
        for row in listed:
            resolved = AN.id_from_stem(row["stem"])
            assert resolved == row["id"]
            assert client.get(f"/api/projects/test-project/docs/{resolved}").status_code == 200
