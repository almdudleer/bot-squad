"""T-0891: `bsq ticket` must not resolve a ticket id by `$PWD` alone.

The incident (2026-08-14): a session working **watchrobot's** T-0655 `cd`'d into
the **bot-squad** repo — which it had to, the code it was fixing lives there —
and ran `bsq ticket context/summary/update T-0655`. bot-squad has its own
T-0655, so `## Executive summary` and `## Context` of a closed foreign ticket
were REPLACED, its status was flipped `closed -> totest`, and the output was the
ordinary «context set on T-0655», true of either ticket. `data/` is outside git,
so the authored text is gone.

Every arm below is measured on the IRREVERSIBLE FACT — the bytes of both
ticket files on disk — not on the message or on an exit code read through a
pipe. `ticket update` is the verb driven end-to-end because it writes the md
itself, with no worker socket in the way; the resolution it uses is shared by
all six `bsq ticket` verbs.

Arms, in the order that matters:
  1. the input the system MUST reject — foreign repo, colliding id;
  2. the input it MUST let through unchanged — ordinary work in your own
     project on an id that happens to collide. A guard that taxes the common
     path gets removed within a week, taking its value with it.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_proj", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_proj", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


TICKET_BODY = (
    "---\nid: {tid}\ntitle: {title}\nstatus: {status}\n---\n\n"
    "## Verbatim request\n\nhis words\n\n"
    "## Executive summary\n\nwhere it stands\n\n"
    "## Context\n\nAUTHORED WORKING STATE OF {slug}\n"
)


@pytest.fixture()
def world(tmp_path: Path):
    """Two projects, `alpha` and `beta`, that both own a T-0655.

    A fake BOT_SQUAD root, so nothing here can reach the live backlog: the
    verbs under test REPLACE authored text, and one wrong run destroys another
    session's work with no undo.
    """
    root = tmp_path / "bot-squad"
    (root / "config").mkdir(parents=True)
    for slug in ("alpha", "beta"):
        (root / "data" / slug / "backlog").mkdir(parents=True)
        (root / "data" / slug / "sessions").mkdir(parents=True)
        (tmp_path / "repo" / slug).mkdir(parents=True)
    (root / "config" / "projects.toml").write_text(
        f'[projects.alpha]\nslug = "alpha"\nrepo_path = "{tmp_path / "repo" / "alpha"}"\n\n'
        f'[projects.beta]\nslug = "beta"\nrepo_path = "{tmp_path / "repo" / "beta"}"\n'
    )

    def ticket(slug: str, tid: str, title: str, status: str) -> Path:
        p = root / "data" / slug / "backlog" / f"{tid}-{slug}-side.md"
        p.write_text(TICKET_BODY.format(tid=tid, title=title, status=status, slug=slug))
        return p

    alpha = ticket("alpha", "T-0655", "alpha keep-alive nudge", "closed")
    beta = ticket("beta", "T-0655", "beta prod-rollback-selftest", "in_progress")
    unique = ticket("beta", "T-0999", "beta only", "open")
    # A session registered under each project — the cd-immune signal.
    (root / "data" / "alpha" / "sessions" / "S-t-p1.md").write_text("---\nsid: S-t-p1\n---\n")
    (root / "data" / "beta" / "sessions" / "S-t-p2.md").write_text("---\nsid: S-t-p2\n---\n")
    return {"root": root, "repo": tmp_path / "repo",
            "alpha": alpha, "beta": beta, "unique": unique}


def run(world, *argv, cwd_slug: str, sid: str | None = None):
    """Drive the REAL bsq as a subprocess. rc is the process's own, never a
    pipeline's last stage."""
    env = {"BOT_SQUAD": str(world["root"]), "PATH": "/usr/bin:/bin",
           "HOME": str(world["root"].parent)}
    if sid:
        env["BSQ_SID"] = sid
    return subprocess.run(
        [sys.executable, str(_BSQ_PATH), *argv],
        cwd=str(world["repo"] / cwd_slug), env=env,
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# 1. the input that MUST be rejected
# ---------------------------------------------------------------------------

def test_foreign_repo_colliding_id_is_refused_and_nothing_is_written(world):
    """The 2026-08-14 incident, replayed: a beta session standing in alpha's
    repo, naming the id both boards use."""
    before_alpha = world["alpha"].read_bytes()
    before_beta = world["beta"].read_bytes()

    r = run(world, "ticket", "update", "T-0655", "totest",
            cwd_slug="alpha", sid="S-t-p2")

    assert r.returncode != 0
    # measured on the files, not on the message
    assert world["alpha"].read_bytes() == before_alpha
    assert world["beta"].read_bytes() == before_beta
    # DoD 3: the refusal names the candidates, with their titles
    assert "alpha keep-alive nudge" in r.stderr
    assert "beta prod-rollback-selftest" in r.stderr
    assert "T-0655@alpha" in r.stderr or "T-0655@beta" in r.stderr


def test_cwd_alone_never_decides_a_colliding_id(world):
    """DoD 3 literally: no explicit slug, no session binding — `$PWD` is not
    enough to pick between two boards, however confident it looks."""
    before = world["alpha"].read_bytes(), world["beta"].read_bytes()

    r = run(world, "ticket", "update", "T-0655", "totest", cwd_slug="beta")

    assert r.returncode != 0
    assert (world["alpha"].read_bytes(), world["beta"].read_bytes()) == before


def test_id_absent_from_your_own_project_is_refused(world):
    """An alpha session naming T-0999, which only beta has. Silently writing
    beta's board is the same class of miss, minus the collision."""
    before = world["unique"].read_bytes()

    r = run(world, "ticket", "update", "T-0999", "totest",
            cwd_slug="beta", sid="S-t-p1")

    assert r.returncode != 0
    assert world["unique"].read_bytes() == before


# ---------------------------------------------------------------------------
# 2. the input that MUST pass — ordinary work, not one step harder
# ---------------------------------------------------------------------------

def test_own_project_own_repo_passes_even_on_a_colliding_id(world):
    """The common case, and the one this guard is not allowed to cost
    anything: 480 of this install's 883 ids are shared between projects, so a
    guard that stopped here would stop most work in the fleet."""
    before_alpha = world["alpha"].read_bytes()

    r = run(world, "ticket", "update", "T-0655", "totest",
            cwd_slug="beta", sid="S-t-p2")

    assert r.returncode == 0, r.stderr
    assert "status: totest" in world["beta"].read_text()   # it really wrote
    assert world["alpha"].read_bytes() == before_alpha     # and only there


def test_explicit_at_slug_writes_the_named_board_from_any_directory(world):
    """DoD 2: `T-0655@alpha` is obeyed from beta's repo — the escape hatch the
    refusal points at has to actually work, or the refusal is a dead end."""
    before_beta = world["beta"].read_bytes()

    r = run(world, "ticket", "update", "T-0655@alpha", "reopened",
            cwd_slug="beta", sid="S-t-p2")

    assert r.returncode == 0, r.stderr
    assert "status: reopened" in world["alpha"].read_text()
    assert world["beta"].read_bytes() == before_beta


def test_slug_flag_is_equivalent_to_the_at_suffix(world):
    r = run(world, "ticket", "update", "T-0655", "reopened", "--slug", "alpha",
            cwd_slug="beta", sid="S-t-p2")
    assert r.returncode == 0, r.stderr
    assert "status: reopened" in world["alpha"].read_text()


def test_unique_id_needs_no_ceremony(world):
    # T-0999 starts "open" — in_progress is a valid transition from there
    # (T-0931's state machine); the point of this test is ID resolution
    # ceremony, not which status is targeted.
    r = run(world, "ticket", "update", "T-0999", "in_progress",
            cwd_slug="beta", sid="S-t-p2")
    assert r.returncode == 0, r.stderr
    assert "status: in_progress" in world["unique"].read_text()


# ---------------------------------------------------------------------------
# 3. DoD 1 — the resolution is announced BEFORE the write, on every path
# ---------------------------------------------------------------------------

def test_the_slug_and_the_file_are_named_on_the_passing_path_too(world):
    """«context set on T-0655» was true of both tickets. What distinguishes
    them is the slug and the path, so those are what gets printed — including
    when the command succeeds, which is when nobody is looking."""
    r = run(world, "ticket", "update", "T-0655", "totest",
            cwd_slug="beta", sid="S-t-p2")

    assert r.returncode == 0
    assert "project beta" in r.stderr
    assert str(world["beta"]) in r.stderr
    assert "beta prod-rollback-selftest" in r.stderr


def test_announcement_goes_to_stderr_so_json_output_stays_parseable(world):
    import json
    r = run(world, "ticket", "status", "T-0655", "--json",
            cwd_slug="beta", sid="S-t-p2")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["slug"] == "beta"


# ---------------------------------------------------------------------------
# 4. DoD 6 — every `bsq ticket` verb shares this resolution, not just update
# ---------------------------------------------------------------------------

def test_every_ticket_verb_resolves_through_the_guard(world):
    """note/summary/quote/context reach the board through the worker socket,
    which does not exist here — so they are pinned on the fact that they REFUSE
    before they ever get that far. A verb that only failed at the socket would
    pass a naive assertion while still resolving by cwd."""
    src = _BSQ_PATH.read_text()
    for fn in ("cmd_ticket_status", "cmd_ticket_update", "cmd_ticket_note",
               "cmd_ticket_context", "cmd_ticket_summary", "cmd_ticket_quote"):
        body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert "resolve_ticket_ref(" in body, f"{fn} still resolves by cwd"
        assert "resolve_slug()" not in body, f"{fn} still resolves by cwd"

    for argv in (["ticket", "note", "T-0655", "x"],
                 ["ticket", "summary", "T-0655", "x"],
                 ["ticket", "quote", "T-0655", "x"],
                 ["ticket", "context", "T-0655", "--clear"],
                 ["ticket", "status", "T-0655"]):
        r = run(world, *argv, cwd_slug="alpha", sid="S-t-p2")
        assert r.returncode != 0, argv
        assert "refusing to guess which project" in r.stderr, argv


# ---------------------------------------------------------------------------
# 5. unit-level: the reference grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,expect", [
    ("T-0655", ("T-0655", None)),
    ("T-0655@watchrobot", ("T-0655", "watchrobot")),
    (" T-0655@bot-squad ", ("T-0655", "bot-squad")),
    ("T-0655@", ("T-0655", None)),
])
def test_split_ticket_ref(ref, expect):
    assert bsq.split_ticket_ref(ref) == expect
