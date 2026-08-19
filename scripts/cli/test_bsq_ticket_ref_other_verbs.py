"""T-0892: the ticket-id verbs OUTSIDE `bsq ticket` must not resolve by `$PWD`.

T-0891 closed six verbs — `bsq ticket` status/update/note/context/summary/quote
— because they REPLACE authored text and `data/` is outside git. It left the
rest, which take a ticket id and picked the project from the working directory:
`spawn`, `expert find`, `scenario new`, `docs link`, `cluster-launch`,
`topic bind/create`'s `--ticket`, and — absent from this ticket's list of seven,
because its argument is spelled `--id` rather than `ticket` —
`assignment-write-result`.

A miss here does not overwrite anything. It spawns a PAID session onto another
project's ticket, writes a manual-test scenario into the wrong project's tree,
cross-links a doc to a stranger's ticket, or hangs a Telegram topic off one.
That is why the refusal has to name candidates rather than merely say no: the
operator's alternative is a wrong spawn nobody notices until the session
reports on work that was never asked for.

Two of the seven write files with no worker socket in the way — `scenario new`
and `doc link` — so they are driven END TO END and measured on the filesystem.
The rest reach the socket, which does not exist in this fixture; they are
measured on WHERE THEY STOP: refused at resolution, or past it (the
announcement `-> project <slug>` on stderr, which is printed before any write).
Both arms are present for every verb, because a check that has only ever been
watched pass is a check nobody has tested.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_t0892", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_t0892", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

TICKET_BODY = (
    "---\nid: {tid}\ntitle: {title}\nstatus: {status}\n---\n\n"
    "## Verbatim request\n\nhis words\n\n## Context\n\nstate of {slug}\n"
)
DOC_BODY = "---\nid: {did}\ntitle: {title}\ncategory: architecture\n---\n\nbody\n"


@pytest.fixture()
def world(tmp_path: Path):
    """Two projects, `alpha` and `beta`, that both own a T-0655 and a D-0001.

    A fake BOT_SQUAD root: nothing here may reach the live install, whose
    backlog these verbs write to and whose spawns cost money.
    """
    root = tmp_path / "bot-squad"
    (root / "config").mkdir(parents=True)
    for slug in ("alpha", "beta"):
        (root / "data" / slug / "backlog").mkdir(parents=True)
        (root / "data" / slug / "sessions").mkdir(parents=True)
        (root / "data" / slug / "docs" / "architecture").mkdir(parents=True)
        (tmp_path / "repo" / slug).mkdir(parents=True)
    (root / "config" / "projects.toml").write_text(
        f'[projects.alpha]\nslug = "alpha"\nrepo_path = "{tmp_path / "repo" / "alpha"}"\n\n'
        f'[projects.beta]\nslug = "beta"\nrepo_path = "{tmp_path / "repo" / "beta"}"\n'
    )

    def ticket(slug: str, tid: str, title: str, status: str = "open") -> Path:
        p = root / "data" / slug / "backlog" / f"{tid}-{slug}-side.md"
        p.write_text(TICKET_BODY.format(tid=tid, title=title, status=status, slug=slug))
        return p

    def doc(slug: str, did: str, title: str) -> Path:
        p = root / "data" / slug / "docs" / "architecture" / f"{did}-{slug}-doc.md"
        p.write_text(DOC_BODY.format(did=did, title=title))
        return p

    w = {
        "root": root,
        "repo": tmp_path / "repo",
        "alpha": ticket("alpha", "T-0655", "alpha keep-alive nudge"),
        "beta": ticket("beta", "T-0655", "beta prod-rollback-selftest"),
        "unique": ticket("beta", "T-0999", "beta only"),
        "doc_alpha": doc("alpha", "D-0001", "alpha doc"),
        "doc_beta": doc("beta", "D-0001", "beta doc"),
    }
    (root / "data" / "alpha" / "sessions" / "S-t-p1.md").write_text("---\nsid: S-t-p1\n---\n")
    (root / "data" / "beta" / "sessions" / "S-t-p2.md").write_text("---\nsid: S-t-p2\n---\n")
    return w


def run(world, *argv, cwd_slug: str, sid: str | None = None):
    """Drive the REAL bsq as a subprocess; rc is the process's own."""
    env = {"BOT_SQUAD": str(world["root"]), "PATH": "/usr/bin:/bin",
           "HOME": str(world["root"].parent)}
    if sid:
        env["BSQ_SID"] = sid
    return subprocess.run(
        [sys.executable, str(_BSQ_PATH), *argv],
        cwd=str(world["repo"] / cwd_slug), env=env,
        capture_output=True, text=True, timeout=120,
    )


def scenarios(world, slug: str) -> list[str]:
    d = world["root"] / "data" / slug / "scenarios"
    return sorted(p.name for p in d.glob("*.md")) if d.is_dir() else []


REFUSAL = "refusing to guess which project"


# ---------------------------------------------------------------------------
# 1. the inputs that MUST be refused — replaying the T-0891 incident shape
#    against each of the seven verbs
# ---------------------------------------------------------------------------

def test_scenario_new_refuses_and_writes_nothing_from_a_foreign_repo(world):
    """A beta session standing in alpha's repo — the systematic case: the code
    it is fixing lives in the other project's clone, so it MUST `cd` there."""
    r = run(world, "scenario", "new", "T-0655", cwd_slug="alpha", sid="S-t-p2")

    assert r.returncode != 0
    assert REFUSAL in r.stderr
    # measured on the tree, not on the message
    assert scenarios(world, "alpha") == []
    assert scenarios(world, "beta") == []
    # the refusal is USEFUL: it names both candidates with their titles
    assert "alpha keep-alive nudge" in r.stderr
    assert "beta prod-rollback-selftest" in r.stderr


def test_doc_link_refuses_and_leaves_both_projects_byte_identical(world):
    before = {k: world[k].read_bytes()
              for k in ("alpha", "beta", "doc_alpha", "doc_beta")}

    r = run(world, "docs", "link", "D-0001", "T-0655", cwd_slug="alpha", sid="S-t-p2")

    assert r.returncode != 0
    assert REFUSAL in r.stderr
    for k, b in before.items():
        assert world[k].read_bytes() == b, f"{k} was modified"


def test_spawn_refuses_before_it_can_cost_a_session(world):
    r = run(world, "spawn", "T-0655", "--dry-run", cwd_slug="alpha", sid="S-t-p2")
    assert r.returncode != 0
    assert REFUSAL in r.stderr
    assert "alpha keep-alive nudge" in r.stderr


def test_expert_find_refuses(world):
    r = run(world, "expert", "find", "T-0655", cwd_slug="alpha", sid="S-t-p2")
    assert r.returncode != 0
    assert REFUSAL in r.stderr


def test_cluster_launch_refuses_on_its_seed(world):
    r = run(world, "cluster-launch", "T-0655", "--dry-run",
            cwd_slug="alpha", sid="S-t-p2")
    assert r.returncode != 0
    assert REFUSAL in r.stderr


def test_cwd_alone_never_decides_a_colliding_id(world):
    """No session binding at all: `$PWD` is not a second signal, it is the one
    that moved. Same rule as T-0891, now on this ticket's verbs."""
    r = run(world, "scenario", "new", "T-0655", cwd_slug="beta")
    assert r.returncode != 0
    assert scenarios(world, "beta") == []


def test_id_absent_from_your_own_project_is_refused(world):
    """An alpha session naming T-0999, which only beta has."""
    r = run(world, "scenario", "new", "T-0999", cwd_slug="beta", sid="S-t-p1")
    assert r.returncode != 0
    assert scenarios(world, "beta") == []


@pytest.mark.parametrize("argv,verb", [
    (["scenario", "new", "T-0655"], "scenario new"),
    (["spawn", "T-0655", "--dry-run"], "spawn"),
    (["docs", "link", "D-0001", "T-0655"], "docs link"),
])
def test_the_refusal_names_the_verb_that_was_typed(world, argv, verb):
    """A refusal whose remedy names a DIFFERENT command costs the reader another
    round trip, which is the tax that gets a guard removed.

    This arm exists because the manual walkthrough caught what the suite did
    not: the message was inherited from T-0891 with `bsq ticket <verb>` baked
    in — right while only `bsq ticket` refused, wrong the moment these verbs
    did. Every assertion here was green through that whole period; they check
    that candidates are listed, not that the remedy is runnable.
    """
    r = run(world, *argv, cwd_slug="alpha", sid="S-t-p2")

    assert REFUSAL in r.stderr
    assert f"re-run `bsq {verb}`" in r.stderr
    assert "bsq ticket <verb>" not in r.stderr
    # and the two ways out are both spelled out
    assert "T-0655@alpha" in r.stderr or "T-0655@beta" in r.stderr
    assert "--slug " in r.stderr


def test_a_bundle_id_from_another_board_is_refused(world):
    """One session is bound to ONE project. A bundle that names another
    project's board is a mistake with no correct reading — spelling it out
    beats binding half the bundle to tickets that do not exist here."""
    r = run(world, "spawn", "T-0999", "--bundle", "T-0655@alpha", "--dry-run",
            cwd_slug="beta", sid="S-t-p2")
    assert r.returncode != 0
    assert "alpha" in r.stderr


def test_topic_bind_refuses_a_ticket_that_is_not_on_the_named_board(world):
    """`topic bind` takes the slug explicitly, so it never guessed — but it
    passed `--ticket` through unverified, so a topic could be hung off an id
    the named project does not have (and, in the collision case, off the
    same-numbered ticket of a different one)."""
    r = run(world, "topic", "bind", "-100", "7", "alpha", "--ticket", "T-0999",
            cwd_slug="alpha", sid="S-t-p1")
    assert r.returncode != 0
    assert "T-0999" in r.stderr
    # it stopped at the id, not at the socket
    assert "socket" not in r.stderr.lower() and "connect" not in r.stderr.lower()


def test_assignment_write_result_refuses_a_cwd_picked_board(world):
    """The eighth verb, absent from this ticket's list of seven: its ticket id
    is spelled `--id`, so an enumeration keyed on the word `ticket` missed it.
    A miss files this session's work product under another project's
    `artifacts/`."""
    r = run(world, "assignment-write-result", "some result", "--id", "T-0655",
            cwd_slug="alpha", sid="S-t-p2")
    assert r.returncode != 0
    assert REFUSAL in r.stderr


def test_assignment_write_result_leaves_routine_ids_alone(world):
    """An `R-NNNN` is on no backlog — routing it through a ticket resolver would
    refuse every routine write. It must reach the worker instead."""
    r = run(world, "assignment-write-result", "some result", "--id", "R-0001",
            "--kind", "routine", cwd_slug="beta", sid="S-t-p2")
    assert REFUSAL not in r.stderr
    assert "worker socket" in r.stderr  # got all the way to the socket


def test_topic_create_refuses_a_ticket_that_is_not_on_the_named_board(world):
    r = run(world, "topic", "create", "-100", "alpha", "--ticket", "T-0999",
            cwd_slug="alpha", sid="S-t-p1")
    assert r.returncode != 0
    assert "T-0999" in r.stderr


# ---------------------------------------------------------------------------
# 2. the inputs that MUST pass — the arm that catches a dead instrument
# ---------------------------------------------------------------------------

def test_scenario_new_in_your_own_project_writes_the_right_tree(world):
    r = run(world, "scenario", "new", "T-0655", cwd_slug="beta", sid="S-t-p2")

    assert r.returncode == 0, r.stderr
    assert scenarios(world, "beta") == ["T-0655-beta-side.md"]
    assert scenarios(world, "alpha") == []
    body = (world["root"] / "data" / "beta" / "scenarios" / "T-0655-beta-side.md").read_text()
    assert "beta prod-rollback-selftest" in body


def test_scenario_new_obeys_the_explicit_at_slug_from_any_directory(world):
    r = run(world, "scenario", "new", "T-0655@alpha", cwd_slug="beta", sid="S-t-p2")
    assert r.returncode == 0, r.stderr
    assert scenarios(world, "alpha") == ["T-0655-alpha-side.md"]
    assert scenarios(world, "beta") == []


def test_scenario_new_obeys_the_slug_flag(world):
    r = run(world, "scenario", "new", "T-0655", "--slug", "alpha",
            cwd_slug="beta", sid="S-t-p2")
    assert r.returncode == 0, r.stderr
    assert scenarios(world, "alpha") == ["T-0655-alpha-side.md"]


def test_doc_link_in_your_own_project_links_only_there(world):
    before_alpha_doc = world["doc_alpha"].read_bytes()
    before_alpha_ticket = world["alpha"].read_bytes()

    r = run(world, "docs", "link", "D-0001", "T-0655", cwd_slug="beta", sid="S-t-p2")

    assert r.returncode == 0, r.stderr
    assert "T-0655" in world["doc_beta"].read_text()
    assert "D-0001" in world["beta"].read_text()
    assert world["doc_alpha"].read_bytes() == before_alpha_doc
    assert world["alpha"].read_bytes() == before_alpha_ticket


def test_a_unique_id_still_needs_no_ceremony(world):
    r = run(world, "scenario", "new", "T-0999", cwd_slug="beta", sid="S-t-p2")
    assert r.returncode == 0, r.stderr
    assert scenarios(world, "beta") == ["T-0999-beta-side.md"]


@pytest.mark.parametrize("argv,verb", [
    (["spawn", "T-0655", "--dry-run"], "spawn"),
    (["expert", "find", "T-0655"], "expert find"),
    (["cluster-launch", "T-0655", "--dry-run"], "cluster-launch"),
])
def test_socket_verbs_get_PAST_resolution_in_their_own_project(world, argv, verb):
    """These three cannot finish without a worker, so the fact under test is
    where they stop: past the guard, on the right board. Without this arm a
    guard that refused EVERYTHING would show all-green above."""
    r = run(world, *argv, cwd_slug="beta", sid="S-t-p2")

    assert REFUSAL not in r.stderr
    assert f"bsq {verb}: T-0655 -> project beta" in r.stderr
    assert str(world["beta"]) in r.stderr


def test_topic_bind_passes_a_ticket_the_named_board_really_has(world):
    r = run(world, "topic", "bind", "-100", "7", "beta", "--ticket", "T-0655",
            cwd_slug="alpha", sid="S-t-p1")
    assert REFUSAL not in r.stderr
    assert "bsq topic bind: T-0655 -> project beta" in r.stderr


# ---------------------------------------------------------------------------
# 3. cluster-launch actually REACHES spawn, on the board its seed resolved to
# ---------------------------------------------------------------------------

def test_cluster_launch_hands_spawn_the_seeds_board(world, monkeypatch, capsys):
    """In-process, because the handoff happens inside one command: the launch
    arm builds a Namespace and calls `cmd_spawn` with it.

    Both inputs are composed by the producers — the argv goes through the real
    parser, and the Namespace `cmd_spawn` receives is the one `cmd_cluster_launch`
    actually built. A hand-typed Namespace here would pin a shape the code no
    longer produces, which is how this defect stayed invisible: the Namespace was
    missing `model`, so every launch raised AttributeError the moment it got past
    the worker call, and no test held the two halves together.

    Driven from ALPHA's repo on a COLLIDING id named explicitly as `@beta`, so
    the resolved board can only reach `spawn` by being handed over: re-resolving
    it there would see session=beta against cwd=alpha and refuse.
    """
    monkeypatch.setattr(bsq, "BOT_SQUAD", str(world["root"]))
    monkeypatch.setattr(bsq, "PROJECTS_TOML",
                        str(world["root"] / "config" / "projects.toml"))
    monkeypatch.setenv("BSQ_SID", "S-t-p2")
    # The expert scan reads transcripts under $HOME; point it at the fixture so
    # this test cannot read (or be slowed by) the real one.
    monkeypatch.setenv("HOME", str(world["root"].parent))
    monkeypatch.chdir(world["repo"] / "alpha")
    # A second beta ticket, so the seed has a cluster to bundle.
    (world["root"] / "data" / "beta" / "backlog" / "T-0998-beta-side.md").write_text(
        TICKET_BODY.format(tid="T-0998", title="beta only sibling",
                           status="open", slug="beta"))
    monkeypatch.setattr(bsq, "post", lambda action, params, **kw: {"sessions": []})
    # The brief assembler greps every transcript store for prior stakeholder
    # guidance (T-0151) — 30s here, and none of it is what this test measures.
    monkeypatch.setattr(bsq, "_collect_guidance", lambda *a, **kw: ([], 0, None))

    # `--model sonnet` because T-0909 made a dev dispatch state its model, and
    # cluster-launch reaches `cmd_spawn` through the same gate. It is passed
    # here rather than exempting the verb: the launch arm carries the flag
    # through the Namespace it builds, so this test also holds THAT handoff
    # together — the same "the Namespace was missing a field" defect this test
    # was written for.
    args = bsq.build_parser().parse_args(
        ["cluster-launch", "T-0655@beta", "--yes", "--spawn-dry-run",
         "--min-score", "0", "--model", "sonnet"])
    args.func(args)

    out = capsys.readouterr().out
    plan = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert plan["slug"] == "beta"
    assert plan["task_id"] == "T-0655"
    # Every bundled id came off beta's board, from a cwd that is alpha's.
    assert set(plan["bind_after"]) == {"T-0998", "T-0999"}
    # ...and the model decision reached the spawn params rather than being
    # dropped on the way through cmd_cluster_launch's Namespace (T-0909).
    assert plan["model"] == "sonnet"


# ---------------------------------------------------------------------------
# 4. ONE implementation of the rule, not seven
# ---------------------------------------------------------------------------

def test_every_ticket_id_verb_routes_through_the_shared_resolver(world):
    """Operator's instruction on this ticket: reuse `resolve_ticket_ref` from
    T-0891 rather than write a second copy — two implementations of one rule is
    the class of defect being fixed here."""
    src = _BSQ_PATH.read_text()
    for fn in ("cmd_spawn", "cmd_expert_find", "cmd_scenario_new",
               "cmd_doc_link", "cmd_cluster_launch",
               "cmd_topic_bind", "cmd_topic_create",
               "cmd_assignment_write_result"):
        body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert "resolve_ticket_ref(" in body, f"{fn} does not use the shared resolver"
        if fn != "cmd_assignment_write_result":
            assert "resolve_slug()" not in body, f"{fn} still resolves the slug by cwd"
    # `assignment-write-result` keeps ONE `resolve_slug()`, on the routine
    # branch — an `R-NNNN` is on no backlog. Pin that it is the only one, so
    # the task branch cannot quietly fall back to it.
    awr = src.split("def cmd_assignment_write_result(", 1)[1].split("\ndef ", 1)[0]
    assert awr.count("resolve_slug()") == 1
    assert 'kind == "task"' in awr
