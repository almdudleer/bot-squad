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


def test_pure_behind_with_no_local_work_gets_the_ff_only_remedy(world):
    """T-1042: `ahead == 0` means there is nothing local to lose, so the
    remedy is a fast-forward that REFUSES instead of merging if that stops
    being true by the time a human runs it — the self-checking property `git
    merge` alone does not have."""
    _advance_origin(world, n=bsq._DRIFT_BEHIND_LIMIT + 3)
    st = bsq._drift_state(str(world["clone"]))
    assert st["ahead"] == 0
    msg = bsq._drift_message(st)
    assert "git merge --ff-only" in msg
    assert "Do NOT run that merge yourself" not in msg  # nothing to escalate


def test_behind_with_only_already_replayed_local_work_gets_the_safe_merge_remedy(world):
    """T-1042: `unshipped == 0` while `behind > 0` means every local-only
    commit's CONTENT already exists on origin (a twin under another SHA) — a
    real merge is still needed (this is not a fast-forward), but there is
    nothing local for it to lose, so it keeps the merge remedy rather than
    escalating."""
    local = _commit(world["clone"], "feature.txt", "the work")
    other = world["tmp"] / "replayer2"
    _git(world["tmp"], "clone", "-q", str(world["origin"]), str(other))
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    _git(other, "checkout", "-q", "work")
    _git(other, "fetch", "-q", str(world["clone"]), "work")
    _git(other, "cherry-pick", "-x", local)
    for i in range(bsq._DRIFT_BEHIND_LIMIT + 2):
        _commit(other, f"filler-{i}.txt")
    _git(other, "push", "-q", "origin", "work")

    st = bsq._drift_state(str(world["clone"]))
    assert st["ahead"] == 1
    assert st["behind"] == bsq._DRIFT_BEHIND_LIMIT + 3
    assert st["unshipped"] == 0
    msg = bsq._drift_message(st)
    assert msg is not None
    assert f"git merge origin/{st['branch']}` reconciles this cleanly" in msg
    assert "Do NOT run that merge yourself" not in msg
    assert "--ff-only" not in msg


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


def test_a_stranded_commit_alone_is_not_reported_while_behind_is_zero(world):
    """T-1042: `behind == 0` means origin is a strict ancestor of HEAD — a
    push ships this commit unconditionally, whatever `unshipped` says. This is
    the shared tree's NORMAL steady state between TL pushes (every commit from
    every lane sits exactly like this until the next push), not drift — firing
    here is what made the advisory indistinguishable from "always on"."""
    _commit(world["clone"], "stranded.txt", "never pushed")
    st = bsq._drift_state(str(world["clone"]))
    assert (st["behind"], st["unshipped"]) == (0, 1)
    assert bsq._drift_message(st) is None


def test_content_that_is_nowhere_upstream_is_reported_once_behind_too(world):
    """The behind-count is about a stale base; the unshipped-count is about
    work that will not reach the install by a plain push. Reported ONLY once
    origin has ALSO moved — a real two-sided divergence — because that is the
    only situation reconciling needs an actual merge commit at all (T-1042)."""
    _commit(world["clone"], "stranded.txt", "never pushed")
    _advance_origin(world, n=1)
    st = bsq._drift_state(str(world["clone"]))
    assert (st["behind"], st["unshipped"]) == (1, 1)
    msg = bsq._drift_message(st)
    assert "1 commit(s) here have NO equivalent on origin" in msg


def test_the_advisory_names_the_practice_that_caused_the_divergence(world):
    _commit(world["clone"], "stranded.txt", "never pushed")
    _advance_origin(world, n=1)
    msg = bsq._drift_message(bsq._drift_state(str(world["clone"])))
    assert "Do NOT cherry-pick onto a branch cut from origin" in msg
    assert "merge origin/work" in msg
    # T-1042: naming the remedy is no longer the same as recommending it.
    assert "Do NOT run that merge yourself" in msg


# ---------------------------------------------------------------------------
# T-1042 positive control: the OLD remedy really does duplicate content
# ---------------------------------------------------------------------------
def test_the_old_remedy_really_does_silently_duplicate_content(tmp_path):
    """Not a claim about git in the abstract — the exact scenario `_drift_state`
    flags as `unshipped`, reproduced end to end. One base file; the clone adds a
    line inside `build()`; a release branch cut fresh from the SAME base (the
    T-0959-documented cherry-pick-onto-a-fresh-branch practice) independently
    adds the byte-identical line inside `Deploy`, at a different anchor, and is
    pushed to origin. The clone is now ahead=1/behind=1 with unshipped=1 —
    exactly the state this ticket's redesigned advisory now escalates instead
    of handing back `git merge origin/<branch>`.

    Run that OLD remedy verbatim: it exits 0, prints "Auto-merging", raises no
    conflict — and the added line is in the file TWICE. That is the proof this
    ticket's DoD asked for: the action the advisory used to hand out, followed
    exactly as printed, ends the reader in a WORSE state than before."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "work")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "deploy.py").write_text("def build():\n    do_a()\n\n\nclass Deploy:\n    stage = \"build\"\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    _git(clone, "checkout", "-q", "work")
    (clone / "deploy.py").write_text(
        "def build():\n    do_a()\n    sha = fetch_sha()\n\n\nclass Deploy:\n    stage = \"build\"\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "T-0919: capture sha in build()")

    replayer = tmp_path / "replayer"
    _git(tmp_path, "clone", "-q", str(origin), str(replayer))
    _git(replayer, "config", "user.email", "t@t")
    _git(replayer, "config", "user.name", "t")
    _git(replayer, "checkout", "-q", "work")
    (replayer / "deploy.py").write_text(
        "def build():\n    do_a()\n\n\nclass Deploy:\n    stage = \"build\"\n    sha = fetch_sha()\n"
    )
    _git(replayer, "add", "-A")
    _git(replayer, "commit", "-q", "-m", "T-0919: capture sha (replayed on release branch)")
    _git(replayer, "push", "-q", "origin", "work")

    st = bsq._drift_state(str(clone))
    assert (st["ahead"], st["behind"], st["unshipped"]) == (1, 1, 1), (
        "fixture is broken — expected exactly the state the advisory escalates"
    )
    msg = bsq._drift_message(st)
    assert "Do NOT run that merge yourself" in msg

    merge = _git(clone, "merge", "origin/work", "-m", "merge origin/work", check=False)
    assert merge.returncode == 0, "the OLD remedy must exit 0 for this to be SILENT"
    assert (clone / "deploy.py.orig").exists() is False  # no conflict artifacts
    status = _git(clone, "status", "--short").stdout
    assert status == "", f"a conflict would have refused the merge; got: {status!r}"

    content = (clone / "deploy.py").read_text()
    assert content.count("fetch_sha()") == 2, (
        "expected the OLD remedy to duplicate the line silently; got:\n" + content
    )


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


def test_the_on_demand_form_does_not_claim_you_just_committed(world):
    """`bsq clone-drift` is asked by a session that is READING the tree. Telling
    it "you just committed onto a base the deploy does not ship" is a false
    statement inside the one message whose entire job is to be believed — and
    the read-time case is the expensive one (operator, 2026-09-06: nearly
    recorded a fabricated-citation finding against a delivered p1 because a grep
    of this tree returned a well-formed false negative)."""
    _advance_origin(world, n=20)
    _commit(world["clone"], "stranded.txt", "never pushed")
    st = bsq._drift_state(str(world["clone"]))

    at_commit = bsq._drift_message(st)
    on_demand = bsq._drift_message(st, just_committed=False)
    assert "You just committed" in at_commit
    assert "the one\nyou just made" in at_commit

    assert "just committed" not in on_demand
    assert "you just made" not in on_demand
    # ...and it names what a stale tree actually does to a reader
    assert "FALSE NEGATIVES" in on_demand
    # both still carry the size and the remedy
    for msg in (at_commit, on_demand):
        assert "20 commit(s) BEHIND" in msg
        assert "1 commit(s) here have NO equivalent on origin" in msg
        assert "merge origin/work" in msg


def test_the_verb_asks_for_the_reader_wording():
    src = _BSQ_PATH.read_text()
    body = src.split("def cmd_drift(")[1].split("\ndef ")[0]
    assert "_drift_message(st, just_committed=False)" in body


def test_a_failed_content_check_is_UNKNOWN_not_zero_while_actually_behind():
    """`git cherry` can fail on its own while the behind-count succeeds. The
    naive quiet path is `if behind <= LIMIT and not unshipped: return None`, and
    `not None` is TRUE — so a broken content check would have produced silence
    that reads exactly like "this clone ships everything". That is the failure
    this whole ticket is about, reproduced inside its own guard.

    T-1042: gated on `behind > 0` — with `behind == 0` a plain push ships
    everything regardless of what `cherry` says, so there is nothing for a
    broken content check to hide in that state (see the `behind == 0` sibling
    test below)."""
    st = {"status": "ok", "branch": "work", "upstream": "origin/work",
          "ahead": 3, "behind": 1, "unshipped": None}
    msg = bsq._drift_message(st)
    assert msg is not None, "a failed content check must never be silent"
    assert "UNKNOWN — not zero" in msg

    # and the same state with a real zero stays quiet, so the UNKNOWN branch is
    # not just "always shout"
    st_zero = dict(st, unshipped=0)
    assert bsq._drift_message(st_zero) is None


def test_a_failed_content_check_is_moot_when_behind_is_zero():
    """T-1042: `behind == 0` means a plain push ships every byte here
    regardless of `cherry`'s answer — a broken content check has nothing to
    hide when there is nothing that could be lost."""
    st = {"status": "ok", "branch": "work", "upstream": "origin/work",
          "ahead": 3, "behind": 0, "unshipped": None}
    assert bsq._drift_message(st) is None


def test_the_content_check_really_can_return_None(world, monkeypatch):
    """The line above is only worth pinning if `_drift_state` can actually
    produce it — a test over a hand-built dict alone would be a claim about my
    own model, not about the code. Advances origin first (T-1042: with
    `behind == 0` a broken content check is moot, see the sibling test) so this
    exercises the branch where a broken `cherry` actually matters."""
    _advance_origin(world, n=1)
    real = bsq.subprocess.run

    def fake(args, **kw):
        if len(args) > 1 and args[1] == "cherry":
            class R:
                returncode, stdout, stderr = 1, "", "fatal: bad revision"
            return R()
        return real(args, **kw)

    monkeypatch.setattr(bsq.subprocess, "run", fake)
    st = bsq._drift_state(str(world["clone"]))
    assert st["status"] == "ok"
    assert st["behind"] == 1
    assert st["unshipped"] is None
    assert "UNKNOWN — not zero" in bsq._drift_message(st)
