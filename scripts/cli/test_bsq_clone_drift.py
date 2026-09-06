"""T-0959: the commit-time clone-drift advisory.

The shared dev clone diverged from `origin/bot_squad/dev` on 2026-08-18 and was
still diverged on 2026-09-06 — 75 commits each way. Nothing told the sessions
working in it. Every deploy shipped origin and omitted whatever sat only in the
clone, and a session that committed, ran green tests and reported READY was
telling the truth about its clone and something false about the install.

The advisory fires where the belief is formed: at the commit. What it must NOT
be is a check a stale clone can pass — `origin/<branch>` is a LOCAL cache, so a
clone that never fetches reports "0 behind" forever, which is precisely the
state being guarded against. Hence `test_a_stale_remote_ref_does_not_satisfy_it`
below: it is the whole point of the design, and it is the test that goes red if
the fetch is removed.
"""
from __future__ import annotations

import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_drift", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_drift", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


# ---------------------------------------------------------------------------
# real-git fixtures: an origin, and two clones of it that drift independently
# ---------------------------------------------------------------------------
def _git(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {p.stderr}")
    return p


def _commit(repo: Path, name: str, body: str = "x"):
    (repo / name).write_text(body)
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def world(tmp_path):
    """origin (bare) + `clone` on branch `work`, one shared base commit."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "work")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    _commit(seed, "base.txt")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    _git(clone, "checkout", "-q", "work")
    return {"origin": origin, "seed": seed, "clone": clone, "tmp": tmp_path}


def _advance_origin(world_, n=1, prefix="up"):
    """Push n commits to origin from a SEPARATE clone, so `world['clone']`
    learns nothing about them until it fetches — the real-life shape."""
    other = world_["tmp"] / f"other-{prefix}"
    if not other.exists():
        _git(world_["tmp"], "clone", "-q", str(world_["origin"]), str(other))
        _git(other, "config", "user.email", "t@t")
        _git(other, "config", "user.name", "t")
        _git(other, "checkout", "-q", "work")
    else:
        _git(other, "fetch", "-q", "origin")
        _git(other, "reset", "-q", "--hard", "origin/work")
    for i in range(n):
        _commit(other, f"{prefix}-{i}.txt")
    _git(other, "push", "-q", "origin", "work")


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
def test_an_in_sync_clone_says_nothing(world):
    st = bsq._drift_state(str(world["clone"]))
    assert st["status"] == "ok"
    assert (st["behind"], st["ahead"], st["unshipped"]) == (0, 0, 0)
    assert bsq._drift_message(st) is None


def test_a_small_drift_is_not_worth_a_message(world):
    # The bar is "more than a small number" — routine peer traffic must not
    # train sessions to read the advisory past.
    _advance_origin(world, n=bsq._DRIFT_BEHIND_LIMIT)
    st = bsq._drift_state(str(world["clone"]))
    assert st["behind"] == bsq._DRIFT_BEHIND_LIMIT
    assert bsq._drift_message(st) is None


def test_a_real_drift_is_named_with_its_size(world):
    _advance_origin(world, n=bsq._DRIFT_BEHIND_LIMIT + 3)
    st = bsq._drift_state(str(world["clone"]))
    assert st["behind"] == bsq._DRIFT_BEHIND_LIMIT + 3
    msg = bsq._drift_message(st)
    assert msg is not None
    assert f"{bsq._DRIFT_BEHIND_LIMIT + 3} commit(s) BEHIND" in msg


def test_a_stale_remote_ref_does_not_satisfy_it(world):
    """THE load-bearing test. origin moves; the clone's cached `origin/work`
    still points at the old tip. A check that reads the cache reports a clone
    in sync while it is eight commits out of date — the exact silence that let
    the real clone drift for nineteen days.

    Remove the `git fetch` from `_drift_state` and this test goes red (measured:
    behind becomes 0 and `_drift_message` returns None)."""
    _advance_origin(world, n=8)
    cached = _git(world["clone"], "rev-parse", "origin/work").stdout.strip()
    real = _git(world["origin"], "rev-parse", "work").stdout.strip()
    assert cached != real, "fixture is broken — the clone already knows the new tip"

    st = bsq._drift_state(str(world["clone"]))
    assert st["status"] == "ok"
    assert st["behind"] == 8
    assert "8 commit(s) BEHIND" in bsq._drift_message(st)


def test_unreachable_origin_reports_UNKNOWN_never_in_sync(world):
    """A guard that cannot measure must not report a pass. An absent answer and
    a clean answer are the same bytes to a reader who is skimming."""
    _git(world["clone"], "remote", "set-url", "origin",
         str(world["tmp"] / "does-not-exist.git"))
    st = bsq._drift_state(str(world["clone"]), timeout=20)
    assert st["status"] == "unknown"
    msg = bsq._drift_message(st)
    assert "UNKNOWN" in msg
    assert "NOT 'in sync'" in msg


def test_a_detached_head_is_UNKNOWN_not_clean(world):
    sha = _git(world["clone"], "rev-parse", "HEAD").stdout.strip()
    _git(world["clone"], "checkout", "-q", sha)
    st = bsq._drift_state(str(world["clone"]))
    assert st["status"] == "unknown"
    assert "detached" in st["reason"]


# ---------------------------------------------------------------------------
# the unit that matters: CONTENT, not SHAs
# ---------------------------------------------------------------------------
def test_a_sha_replayed_upstream_is_not_counted_as_unshipped(world):
    """The measured shape of the real divergence: 70 of 75 local-only SHAs were
    byte-identical to a commit already on origin, replayed there by a session
    that cherry-picked onto a branch cut from origin. Counting SHAs turns the
    advisory into 94% noise, which is why the deploy's own version of it was
    read past for nineteen days."""
    clone = world["clone"]
    local = _commit(clone, "feature.txt", "the work")

    # Someone replays that same content onto origin under a different SHA.
    other = world["tmp"] / "replayer"
    _git(world["tmp"], "clone", "-q", str(world["origin"]), str(other))
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    _git(other, "checkout", "-q", "work")
    _git(other, "fetch", "-q", str(clone), "work")
    # `-x` keeps the patch-id and changes the message, so the SHA is guaranteed
    # to differ. A same-second cherry-pick otherwise reproduces the IDENTICAL
    # SHA and the fixture stops testing what it says it tests.
    _git(other, "cherry-pick", "-x", local)
    replayed = _git(other, "rev-parse", "HEAD").stdout.strip()
    _git(other, "push", "-q", "origin", "work")
    assert replayed != local, "fixture is broken — the replay kept the SHA"

    st = bsq._drift_state(str(clone))
    assert st["ahead"] == 1, "the SHA really is local-only"
    assert st["unshipped"] == 0, "...but its CONTENT is on origin, so it ships"
    assert bsq._drift_message(st) is None


def test_content_that_is_nowhere_upstream_is_reported_even_below_the_limit(world):
    """The behind-count is about a stale base; the unshipped-count is about
    work that will not reach the install. The second fires at ONE, because one
    is already the whole failure."""
    _commit(world["clone"], "stranded.txt", "never pushed")
    st = bsq._drift_state(str(world["clone"]))
    assert (st["behind"], st["unshipped"]) == (0, 1)
    msg = bsq._drift_message(st)
    assert "1 commit(s) here have NO equivalent on origin" in msg


def test_the_advisory_names_the_practice_that_caused_the_divergence(world):
    _commit(world["clone"], "stranded.txt", "never pushed")
    msg = bsq._drift_message(bsq._drift_state(str(world["clone"])))
    assert "Do NOT cherry-pick onto a branch cut from origin" in msg
    assert "merge origin/work" in msg


# ---------------------------------------------------------------------------
# the advisory is an advisory — it never costs a commit
# ---------------------------------------------------------------------------
def test_warn_never_raises_when_git_is_broken(tmp_path, capsys):
    """Called right after a successful commit. An exception here would abort
    `bsq commit` AFTER the commit landed, which reads as a failed commit."""
    bsq._warn_clone_drift(str(tmp_path / "not-a-repo"))
    err = capsys.readouterr().err
    assert "UNKNOWN" in err


def test_warn_is_silent_on_a_healthy_clone(world, capsys):
    bsq._warn_clone_drift(str(world["clone"]))
    assert capsys.readouterr().err == ""


def test_warn_prints_to_stderr_on_drift(world, capsys):
    _advance_origin(world, n=20)
    bsq._warn_clone_drift(str(world["clone"]))
    assert "CLONE DRIFT (T-0959)" in capsys.readouterr().err


def test_the_escape_hatch_is_opt_in_only(world, capsys, monkeypatch):
    _advance_origin(world, n=20)
    monkeypatch.setenv("BOT_SQUAD_SKIP_DRIFT_CHECK", "1")
    bsq._warn_clone_drift(str(world["clone"]))
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# wiring: the advisory has to be REACHED, or none of the above matters
# ---------------------------------------------------------------------------
def test_both_commit_paths_call_the_advisory():
    """A correct function nobody calls is not a guard (T-0959 exists because a
    correct WARNING nobody read was not one either)."""
    src = _BSQ_PATH.read_text()
    assert src.count("_warn_clone_drift(repo_root)") == 2, (
        "expected the plain and the --hunks commit paths to both call it"
    )
    # ...and next to the other post-commit check, not somewhere unreachable.
    # (`[1:]` would also catch the `def` line, which is a declaration, not a
    # call site — split on the call form only.)
    calls = src.split("_verify_commit_scope(repo_root,")[1:]
    assert len(calls) == 2, "expected exactly the two post-commit call sites"
    for tail in calls:
        assert "_warn_clone_drift(repo_root)" in tail[:200]


def test_the_on_demand_verb_is_registered_and_wired():
    """T-0959, operator field report 2026-09-06: the commit-time advisory helps
    the session WRITING. The session READING a diverged tree gets confident,
    well-formed false negatives out of a grep — twice in ten minutes, on a
    delivered P1's test citations. `bsq clone-drift` is the answer it can ask
    for before trusting the tree."""
    parser = bsq.build_parser()
    args = parser.parse_args(["clone-drift"])
    assert args.func is bsq.cmd_drift
    # it must MEASURE, not read the cached ref
    src = _BSQ_PATH.read_text()
    body = src.split("def cmd_drift(")[1].split("\ndef ")[0]
    assert "_drift_state(" in body


def test_the_verb_name_does_not_collide_with_the_existing_drift_verb():
    """`bsq drift` already exists (the stall-watchdog pause). Registering a
    second subparser under that name raises at build_parser() time and breaks
    EVERY bsq verb for every live session — measured here on 2026-09-06."""
    parser = bsq.build_parser()          # would raise on a collision
    assert parser.parse_args(["drift", "status"]).func is not bsq.cmd_drift
