"""Tests for T-0215: per-session hunk-isolated commits for co-edited files.

`bsq edit-begin <files>` snapshots each file's current bytes as this session's
baseline; `bsq commit --hunks -- <files>` then commits ONLY the diff since that
baseline (this session's hunks), applied onto a temp index seeded from HEAD so a
peer's concurrent edits in the same file (and the shared .git/index) are never
swept in.

Two layers:
  - pure unit tests over the patch/path helpers (no git, no fs side effects);
  - integration tests that drive `bsq` as a subprocess inside a throwaway git
    repo, exercising the real git-apply / temp-index / commit plumbing — this is
    where the co-edited-file guarantee is actually proven.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_hunks", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_hunks", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


# ---------------------------------------------------------------------------
# pure unit tests
# ---------------------------------------------------------------------------
def test_sanitize_sid_keeps_safe_chars_replaces_rest():
    assert bsq._sanitize_sid("S-almdudleer-multi_server-TL-p67") == \
        "S-almdudleer-multi_server-TL-p67"
    assert bsq._sanitize_sid("S-a/b c:d") == "S-a_b_c_d"


def test_worktree_store_path_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(bsq, "BOT_SQUAD", str(tmp_path))
    store = bsq.worktree_store("bot-squad", "S-x-dev-p1")
    assert store == tmp_path / "data" / "bot-squad" / "_worktree" / "S-x-dev-p1"


def test_snapshot_path_mirrors_relpath(tmp_path):
    store = tmp_path / "store"
    p = bsq._snapshot_path(store, "web/src/api.ts")
    assert p == store / "files" / "web/src/api.ts"


def test_snapshot_path_rejects_traversal(tmp_path):
    store = tmp_path / "store"
    with pytest.raises(ValueError):
        bsq._snapshot_path(store, "../../etc/passwd")
    with pytest.raises(ValueError):
        bsq._snapshot_path(store, "/abs/path")


def test_rewrite_patch_headers_normalizes_ab_paths():
    # git diff --no-index emits the two on-disk paths in the headers; we must
    # rewrite every path-bearing line to the repo-relative a/<rel> b/<rel> so
    # `git apply -p1` lands the hunk at the right place in the repo.
    raw = (
        "diff --git a/tmp/baseline/foo b/work/repo/foo\n"
        "index 1111111..2222222 100644\n"
        "--- a/tmp/baseline/foo\n"
        "+++ b/work/repo/foo\n"
        "@@ -1,2 +1,2 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-edited\n"
    )
    out = bsq._rewrite_patch_headers(raw, "pkg/foo.py")
    assert "diff --git a/pkg/foo.py b/pkg/foo.py\n" in out
    assert "--- a/pkg/foo.py\n" in out
    assert "+++ b/pkg/foo.py\n" in out
    # body untouched
    assert "-line2\n" in out and "+line2-edited\n" in out
    # no leakage of the on-disk scratch paths
    assert "baseline" not in out and "work/repo" not in out


def test_make_hunk_patch_empty_when_identical(tmp_path):
    a = tmp_path / "a"
    a.write_text("same\n")
    assert bsq.make_hunk_patch("f.txt", a, a) == ""


def test_make_hunk_patch_has_relative_headers(tmp_path):
    base = tmp_path / "base"
    cur = tmp_path / "cur"
    base.write_text("l1\nl2\nl3\n")
    cur.write_text("l1\nl2-edited\nl3\n")
    patch = bsq.make_hunk_patch("pkg/x.py", base, cur)
    assert "--- a/pkg/x.py\n" in patch
    assert "+++ b/pkg/x.py\n" in patch
    assert "-l2\n" in patch and "+l2-edited\n" in patch


# ---------------------------------------------------------------------------
# integration tests — real git repo, bsq driven as a subprocess
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _run_bsq(repo: Path, bot_squad: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["BOT_SQUAD"] = bot_squad
    # Deterministic SID, no tmux needed.
    return subprocess.run(
        ["python3", str(_BSQ_PATH), *args],
        cwd=repo, capture_output=True, text=True, env=env,
    )


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repo registered as project 'proj' in a temp BOT_SQUAD."""
    bot_squad = tmp_path / "bs"
    repo = tmp_path / "proj"
    repo.mkdir()
    (bot_squad / "data" / "proj").mkdir(parents=True)
    # config/projects.toml so resolve_slug maps the repo → 'proj'
    (bot_squad / "config").mkdir(parents=True)
    (bot_squad / "config" / "projects.toml").write_text(
        f'[projects.proj]\nrepo_path = "{repo}"\n'
    )
    # bsq resolves safe-commit.sh under $BOT_SQUAD/scripts/cli — mirror it.
    (bot_squad / "scripts" / "cli").mkdir(parents=True)
    (bot_squad / "scripts" / "cli" / "safe-commit.sh").symlink_to(
        _BSQ_PATH.parent / "safe-commit.sh"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    # disable any pre-commit hook noise from the real clone — fresh repo has none
    return {"repo": repo, "bot_squad": str(bot_squad)}


def test_commit_hunks_isolates_my_hunk_from_peer_hunk(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    f = r / "shared.txt"
    f.write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\nL9\nL10\n")
    _git(r, "add", "shared.txt")
    _git(r, "commit", "-q", "-m", "base")

    # PEER edits L2 (uncommitted, dirty in the shared worktree).
    f.write_text("L1\nL2-PEER\nL3\nL4\nL5\nL6\nL7\nL8\nL9\nL10\n")

    # I snapshot my baseline (captures the peer's already-dirty state too).
    eb = _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "shared.txt")
    assert eb.returncode == 0, eb.stderr

    # I edit L8.
    f.write_text("L1\nL2-PEER\nL3\nL4\nL5\nL6\nL7\nL8-MINE\nL9\nL10\n")

    # commit ONLY my hunk.
    cm = _run_bsq(
        r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1",
        "-m", "mine only", "shared.txt",
    )
    assert cm.returncode == 0, cm.stderr

    committed = _git(r, "show", "HEAD:shared.txt")
    assert "L8-MINE" in committed          # my hunk landed
    assert "L2-PEER" not in committed      # peer hunk NOT swept in
    assert "L2\n" in committed             # peer's original line preserved at HEAD

    # peer hunk still dirty in the worktree (theirs to commit).
    diff = _git(r, "diff", "--", "shared.txt")
    assert "L2-PEER" in diff


def test_commit_hunks_requires_prior_edit_begin(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    f = r / "x.txt"
    f.write_text("a\nb\n")
    _git(r, "add", "x.txt")
    _git(r, "commit", "-q", "-m", "base")
    f.write_text("a\nb-edited\n")
    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1", "-m", "x", "x.txt")
    assert cm.returncode != 0
    assert "edit-begin" in (cm.stderr + cm.stdout)


# ---------------------------------------------------------------------------
# T-0732: the POST-HOC path (--patch) gets the same temp-index isolation
# ---------------------------------------------------------------------------
def test_patch_target_rels_reads_plus_headers():
    patch = (
        "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n+hello\n"
    )
    assert bsq._patch_target_rels(patch) == ["pkg/a.py", "new.txt"]


def test_patch_target_rels_reads_the_minus_side_of_a_deletion():
    """T-0753: this used to assert `== []` — the deletion target was skipped, so
    a delete-only patch had no file targets and was rejected outright."""
    patch = "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    assert bsq._patch_target_rels(patch) == ["gone.txt"]


def test_patch_target_rels_keeps_deletion_in_order_among_edits():
    patch = (
        "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        "diff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n+hello\n"
    )
    assert bsq._patch_target_rels(patch) == ["pkg/a.py", "gone.txt", "new.txt"]
    # a creation still reads its path off the `+++` side, not `--- /dev/null`
    assert bsq._patch_hunks_per_rel(patch) == {"pkg/a.py": 1, "gone.txt": 1,
                                               "new.txt": 1}


def test_patch_header_rel_prefers_the_plus_side():
    assert bsq._patch_header_rel("--- a/old.txt", "+++ b/new.txt") == "new.txt"
    assert bsq._patch_header_rel("--- /dev/null", "+++ b/new.txt") == "new.txt"
    assert bsq._patch_header_rel("--- a/gone.txt", "+++ /dev/null") == "gone.txt"
    # both sides absent is not a target at all (nothing to commit under a name)
    assert bsq._patch_header_rel("--- /dev/null", "+++ /dev/null") == ""
    # a `+++` with no `---` before it (patch starts mid-stream) still resolves
    assert bsq._patch_header_rel("", "+++ b/a.py") == "a.py"


def test_commit_hunks_patch_isolates_peer_hunk_and_peer_staging(repo):
    """The 2026-07-27 incident, driven through the fix. No edit-begin was run
    (that's the whole point of the post-hoc path); a peer is dirty in the SAME
    file AND has an unrelated file staged in the shared index. Neither may
    land in my commit."""
    r, bs = repo["repo"], repo["bot_squad"]
    f = r / "shared.txt"
    f.write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\n")
    _git(r, "add", "shared.txt")
    _git(r, "commit", "-q", "-m", "base")

    f.write_text("L1\nL2-PEER\nL3\nL4\nL5\nL6\nL7\nL8-MINE\n")   # both edits, mine + theirs
    (r / "peer_wip.txt").write_text("peer's in-flight work\n")
    _git(r, "add", "peer_wip.txt")                                # peer loads the shared index

    # I hand-build a patch of ONLY my hunk (what a dev does by trimming `@@`s).
    patch = r / "mine.patch"
    patch.write_text(
        "diff --git a/shared.txt b/shared.txt\n"
        "--- a/shared.txt\n"
        "+++ b/shared.txt\n"
        "@@ -8 +8 @@\n"
        "-L8\n"
        "+L8-MINE\n"
    )
    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "mine only")
    assert cm.returncode == 0, cm.stderr + cm.stdout

    committed = _git(r, "show", "HEAD:shared.txt")
    assert "L8-MINE" in committed         # my hunk landed
    assert "L2-PEER" not in committed     # peer's hunk in the same file: not swept
    assert "peer_wip.txt" not in _git(r, "show", "--name-only", "--format=", "HEAD")
    # peer's staging survives, still theirs to commit
    assert "peer_wip.txt" in _git(r, "diff", "--cached", "--name-only")


def test_commit_hunks_patch_rejects_undeclared_files(repo):
    """If you name files, the patch may not reach past them — a mis-trimmed
    patch is caught before it becomes a commit."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "a.txt").write_text("a\n")
    (r / "b.txt").write_text("b\n")
    _git(r, "add", "a.txt", "b.txt")
    _git(r, "commit", "-q", "-m", "base")
    patch = r / "two.patch"
    patch.write_text(
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+a2\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-b\n+b2\n"
    )
    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "only a", "a.txt")
    assert cm.returncode != 0
    assert "b.txt" in (cm.stderr + cm.stdout)


def test_commit_hunks_patch_rejects_a_declared_file_the_patch_does_not_touch(repo):
    """T-1051: the mirror of the stray-file check above. Declaring MORE files
    than the patch touches used to be silently accepted — the commit landed
    only the patch's files and exited 0, reporting success while the extra
    declared files were dropped with no error. Must refuse, naming what was
    declared but not covered, exactly like the stray-file refusal above."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "a.txt").write_text("a\n")
    (r / "b.txt").write_text("b\n")
    (r / "c.txt").write_text("c\n")
    _git(r, "add", "a.txt", "b.txt", "c.txt")
    _git(r, "commit", "-q", "-m", "base")
    patch = r / "one.patch"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+a2\n")
    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "a, b, and c",
                  "a.txt", "b.txt", "c.txt")
    assert cm.returncode != 0
    out = cm.stderr + cm.stdout
    assert "b.txt" in out and "c.txt" in out
    # nothing committed — the failure must be total, not a partial commit of a.txt
    assert _git(r, "log", "--format=%s").strip() == "base"


def test_commit_hunks_patch_with_matching_declared_files_commits_all(repo):
    """Healthy case for the new guard: declaring exactly the files the patch
    touches — multi-file, the caller's actual use case from the T-1051
    incident — must still commit everything and say so."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "a.txt").write_text("a\n")
    (r / "b.txt").write_text("b\n")
    _git(r, "add", "a.txt", "b.txt")
    _git(r, "commit", "-q", "-m", "base")
    patch = r / "two.patch"
    patch.write_text(
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+a2\n"
        "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-b\n+b2\n"
    )
    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "both",
                  "a.txt", "b.txt")
    assert cm.returncode == 0, cm.stderr + cm.stdout
    assert "committed 2 file(s)" in cm.stdout
    assert _git(r, "show", "HEAD:a.txt").strip() == "a2"
    assert _git(r, "show", "HEAD:b.txt").strip() == "b2"


def _peer_commit_shim(shim_dir, repo, trigger, binary="flock"):
    """A PATH shim that lands a PEER commit exactly once, immediately before the
    real `<binary> <trigger>` runs — i.e. inside the window this ticket is about.

    `flock` puts the peer commit just before we acquire the commit lock (the
    realistic case: safe-commit has not locked yet). `git commit` puts it at the
    last possible instant, after any in-lock check, which is what a peer
    committing OUTSIDE safe-commit's lock looks like.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    real = subprocess.run(["bash", "-lc", f"command -v {binary}"],
                          capture_output=True, text=True).stdout.strip()
    guard = ('[ "$1" = "%s" ] && ' % trigger) if trigger else ""
    (shim_dir / binary).write_text(f"""#!/bin/bash
if {guard}[ ! -f "{repo}/.git/PEER_FIRED" ]; then
  touch "{repo}/.git/PEER_FIRED"
  ( cd "{repo}"
    BR=$(git rev-parse --abbrev-ref HEAD); ti=$(mktemp)
    GIT_INDEX_FILE=$ti git read-tree HEAD
    b=$(printf 'peer1\\npeer2\\nPEER-NEW\\n' | git hash-object -w --stdin)
    GIT_INDEX_FILE=$ti git update-index --add --cacheinfo 100644,$b,peer.txt
    t=$(GIT_INDEX_FILE=$ti git write-tree)
    c=$(GIT_INDEX_FILE=$ti git commit-tree $t -p $(git rev-parse HEAD) -m "peer commit in the window")
    git update-ref refs/heads/$BR $c
    printf 'peer1\\npeer2\\nPEER-NEW\\n' > peer.txt
    rm -f $ti ) < /dev/null > /dev/null 2>&1
fi
exec {real} "$@"
""")
    (shim_dir / binary).chmod(0o755)
    return shim_dir


def _repo_with_my_patch(repo):
    """Base commit + my one-file patch, ready to commit. Returns (r, bs, patch)."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "mine.txt").write_text("mine1\nmine2\nmine3\n")
    (r / "peer.txt").write_text("peer1\npeer2\n")
    _git(r, "add", "mine.txt", "peer.txt")
    _git(r, "commit", "-q", "-m", "base")
    (r / "mine.txt").write_text("mine1\nmine2-MINE\nmine3\n")
    patch = r / "mine.patch"
    patch.write_text(_git(r, "diff", "--", "mine.txt"))
    return r, bs, patch


def test_a_peer_commit_before_the_lock_cannot_be_reverted(repo, tmp_path):
    """T-0970, the incident: a peer commit landing between our index seeding and
    our commit used to produce a CORRECT parent pointer over a tree that
    predates it — silently reverting everything they landed, at exit 0.

    The commit must be REFUSED, and nothing of the peer's may move.
    """
    r, bs, patch = _repo_with_my_patch(repo)
    peer_head_before = _git(r, "rev-parse", "HEAD").strip()
    shim = _peer_commit_shim(tmp_path / "shim", r, trigger=None, binary="flock")
    env = dict(os.environ)
    env["BOT_SQUAD"] = bs
    env["PATH"] = f"{shim}:{env['PATH']}"
    cm = subprocess.run(
        ["python3", str(_BSQ_PATH), "commit", "--hunks", "--ack", "--patch",
         str(patch), "--sid", "S-me-dev-p1", "-m", "mine only", "mine.txt"],
        cwd=r, capture_output=True, text=True, env=env, timeout=180,
    )
    assert cm.returncode != 0, "a stale-base commit must be refused, not reported as success"
    out = cm.stdout + cm.stderr
    assert "T-0970" in out
    # The peer's commit is HEAD and its content is intact.
    assert "PEER-NEW" in _git(r, "show", "HEAD:peer.txt")
    # Ours never landed: HEAD is the peer's commit, whose parent is the base.
    assert _git(r, "rev-parse", "HEAD^").strip() == peer_head_before
    assert "mine only" not in _git(r, "log", "--format=%s")


def test_the_refusal_names_both_shas_so_the_retry_is_mechanical(repo, tmp_path):
    """The refusal has to say what to re-extract against, or the reader guesses."""
    r, bs, patch = _repo_with_my_patch(repo)
    base = _git(r, "rev-parse", "HEAD").strip()
    shim = _peer_commit_shim(tmp_path / "shim", r, trigger=None, binary="flock")
    env = dict(os.environ)
    env["BOT_SQUAD"] = bs
    env["PATH"] = f"{shim}:{env['PATH']}"
    cm = subprocess.run(
        ["python3", str(_BSQ_PATH), "commit", "--hunks", "--ack", "--patch",
         str(patch), "--sid", "S-me-dev-p1", "-m", "mine only", "mine.txt"],
        cwd=r, capture_output=True, text=True, env=env, timeout=180,
    )
    out = cm.stdout + cm.stderr
    assert base in out, "the refusal must name the base the index was seeded from"
    assert _git(r, "rev-parse", "HEAD").strip() in out, "and the head to rebase onto"


def test_a_peer_outside_the_commit_lock_is_caught_after_the_fact(repo, tmp_path):
    """A peer that does NOT take safe-commit's lock can still land inside the
    last instant. Prevention is impossible there, so the commit happens — but it
    must exit non-zero and say the tree is stale, never report success."""
    r, bs, patch = _repo_with_my_patch(repo)
    shim = _peer_commit_shim(tmp_path / "shim", r, trigger="commit", binary="git")
    env = dict(os.environ)
    env["BOT_SQUAD"] = bs
    env["PATH"] = f"{shim}:{env['PATH']}"
    cm = subprocess.run(
        ["python3", str(_BSQ_PATH), "commit", "--hunks", "--ack", "--patch",
         str(patch), "--sid", "S-me-dev-p1", "-m", "mine only", "mine.txt"],
        cwd=r, capture_output=True, text=True, env=env, timeout=180,
    )
    assert cm.returncode != 0, "a landed stale-base commit must not exit 0"
    out = cm.stdout + cm.stderr
    assert "STALE BASE" in out
    assert "peer.txt" in out, "the address list must name what was reverted"


def test_a_peer_committing_AFTER_us_is_not_reported_as_a_stale_base(repo, tmp_path):
    """The remedy must not fail in the opposite direction.

    Layer 2 used to read HEAD^ to find our commit's parent. A peer landing in
    the milliseconds AFTER ours moves HEAD, so HEAD^ became OUR commit and a
    perfectly good commit was accused of reverting work — which would send the
    author into a "recovery" that reverts real work. Our commit is identified by
    the TREE we built, which no later commit can move.
    """
    r, bs, patch = _repo_with_my_patch(repo)
    base = _git(r, "rev-parse", "HEAD").strip()
    shim = tmp_path / "shim"
    shim.mkdir()
    real = subprocess.run(["bash", "-lc", "command -v git"],
                          capture_output=True, text=True).stdout.strip()
    # run the real commit FIRST, then land a peer commit in the gap before the
    # post-commit assertion reads the repo.
    (shim / "git").write_text(f"""#!/bin/bash
if [ "$1" = "commit" ] && [ ! -f "{r}/.git/PEER_FIRED" ]; then
  touch "{r}/.git/PEER_FIRED"
  {real} "$@"; rc=$?
  ( cd "{r}"
    BR=$({real} rev-parse --abbrev-ref HEAD); ti=$(mktemp)
    GIT_INDEX_FILE=$ti {real} read-tree HEAD
    b=$(printf 'peer1\\npeer2\\nLATER\\n' | {real} hash-object -w --stdin)
    GIT_INDEX_FILE=$ti {real} update-index --add --cacheinfo 100644,$b,peer.txt
    t=$(GIT_INDEX_FILE=$ti {real} write-tree)
    c=$(GIT_INDEX_FILE=$ti {real} commit-tree $t -p $({real} rev-parse HEAD) -m "peer commit AFTER ours")
    {real} update-ref refs/heads/$BR $c
    rm -f $ti ) < /dev/null > /dev/null 2>&1
  exit $rc
fi
exec {real} "$@"
""")
    (shim / "git").chmod(0o755)
    env = dict(os.environ)
    env["BOT_SQUAD"] = bs
    env["PATH"] = f"{shim}:{env['PATH']}"
    cm = subprocess.run(
        ["python3", str(_BSQ_PATH), "commit", "--hunks", "--ack", "--patch",
         str(patch), "--sid", "S-me-dev-p1", "-m", "mine only", "mine.txt"],
        cwd=r, capture_output=True, text=True, env=env, timeout=180,
    )
    out = cm.stdout + cm.stderr
    assert "STALE BASE" not in out, \
        "our commit sat directly on its base; accusing it would send the author to revert good work"
    assert cm.returncode == 0, out
    # both commits are present and neither reverted the other
    assert "LATER" in _git(r, "show", "HEAD:peer.txt")
    ours = _git(r, "rev-parse", "HEAD^").strip()
    assert _git(r, "rev-parse", f"{ours}^").strip() == base
    assert "mine2-MINE" in _git(r, "show", f"{ours}:mine.txt")


def test_commit_hunks_patch_needs_a_real_patch_file(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "a.txt").write_text("a\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "base")
    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(r / "nope.patch"),
                  "--sid", "S-me-dev-p1", "-m", "x")
    assert cm.returncode != 0
    assert "no such patch file" in (cm.stderr + cm.stdout)


def test_patch_without_hunks_is_rejected(repo):
    """--patch is meaningless outside the hunk-isolated form; say so rather
    than silently doing a plain pathspec commit."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "a.txt").write_text("a\n")
    cm = _run_bsq(r, bs, "commit", "--patch", "x.patch",
                  "--sid", "S-me-dev-p1", "-m", "x", "a.txt")
    assert cm.returncode != 0
    assert "--hunks" in (cm.stderr + cm.stdout)


# ---------------------------------------------------------------------------
# T-0732: the verify step runs itself instead of living in a doc as advice
# ---------------------------------------------------------------------------
def test_verify_commit_scope_is_quiet_when_scope_matches(repo, capsys):
    r = repo["repo"]
    (r / "a.txt").write_text("a\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "just a")
    bsq._verify_commit_scope(str(r), ["a.txt"])
    out = capsys.readouterr()
    assert "verify — what actually landed" in out.out
    assert "ABSORPTION WARNING" not in out.err


def test_verify_commit_scope_shouts_on_an_unintended_file(repo, capsys):
    """The last line of defence that caught the real incident, automated."""
    r = repo["repo"]
    (r / "mine.txt").write_text("mine\n")
    (r / "peer_wip.txt").write_text("peer\n")
    _git(r, "add", "mine.txt", "peer_wip.txt")
    _git(r, "commit", "-q", "-m", "swept a peer's file in")
    bsq._verify_commit_scope(str(r), ["mine.txt"])
    err = capsys.readouterr().err
    assert "ABSORPTION WARNING" in err
    assert "peer_wip.txt" in err
    assert "git apply -R --cached" in err  # names the non-rewriting recovery


def test_verify_commit_scope_accepts_children_of_a_declared_directory(repo, capsys):
    r = repo["repo"]
    (r / "docs").mkdir()
    (r / "docs" / "x.md").write_text("x\n")
    _git(r, "add", "docs/x.md")
    _git(r, "commit", "-q", "-m", "docs")
    bsq._verify_commit_scope(str(r), ["docs"])
    assert "ABSORPTION WARNING" not in capsys.readouterr().err


def test_plain_commit_unchanged_still_pathspec_commits(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    f = r / "y.txt"
    f.write_text("hello\n")
    cm = _run_bsq(r, bs, "commit", "--sid", "S-me-dev-p1", "-m", "add y", "y.txt")
    assert cm.returncode == 0, cm.stderr
    assert "y.txt" in _git(r, "show", "--name-only", "--format=", "HEAD")


# ---------------------------------------------------------------------------
# T-0752 manifestation 1/3: an isolated-index commit must leave the SHARED
# index consistent with what it just committed.
#
# Before the fix the shared index kept the PRE-commit blobs, so `git diff
# --cached` read back as the exact INVERSE of the commit and a peer's ordinary
# `git add <file> && git commit` silently reverted the work that had landed.
# Each of these fails against unfixed sources.
# ---------------------------------------------------------------------------
def _staged_names(repo: Path) -> list[str]:
    return _git(repo, "diff", "--cached", "--name-only").split()


def test_hunks_commit_leaves_no_inverse_in_the_shared_index(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    f = r / "shared.txt"
    f.write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\n")
    _git(r, "add", "shared.txt")
    _git(r, "commit", "-q", "-m", "base")

    assert _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1",
                    "shared.txt").returncode == 0
    f.write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8-MINE\n")
    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1",
                  "-m", "mine", "shared.txt")
    assert cm.returncode == 0, cm.stderr + cm.stdout

    assert "L8-MINE" in _git(r, "show", "HEAD:shared.txt")
    # The whole point: nothing staged, and in particular not the inverse.
    assert _staged_names(r) == []
    assert _git(r, "diff", "--cached") == ""
    assert _git(r, "status", "--short") == ""


def test_peer_add_then_commit_no_longer_reverts_the_landed_hunk(repo):
    """The actual blast radius, driven end to end: `git add <mine> && git
    commit` is pathspec-LESS, so it commits the shared index as-is. With the
    inverse left in it, that reverted a peer's landed work while the commit
    looked entirely normal."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "shared.txt").write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\n")
    _git(r, "add", "shared.txt")
    _git(r, "commit", "-q", "-m", "base")

    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "shared.txt")
    (r / "shared.txt").write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8-MINE\n")
    assert _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1",
                    "-m", "mine", "shared.txt").returncode == 0

    # A peer, entirely innocently, lands an unrelated file with raw git.
    (r / "peer.txt").write_text("peer work\n")
    _git(r, "add", "peer.txt")
    _git(r, "commit", "-q", "-m", "peer work")

    assert "L8-MINE" in _git(r, "show", "HEAD:shared.txt")   # NOT reverted
    assert "peer.txt" in _git(r, "show", "--name-only", "--format=", "HEAD")


def test_reconciliation_is_scoped_and_spares_a_peers_unrelated_staging(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    base = "".join(f"L{i}\n" for i in range(1, 13))
    (r / "shared.txt").write_text(base)
    (r / "other.txt").write_text("other\n")
    _git(r, "add", "shared.txt", "other.txt")
    _git(r, "commit", "-q", "-m", "base")

    peer = base.replace("L1\n", "L1-PEER\n")      # peer already dirty…
    (r / "shared.txt").write_text(peer)
    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "shared.txt")
    # …so my baseline isolates it. Far from their hunk, so no apply conflict.
    (r / "shared.txt").write_text(peer.replace("L12\n", "L12-MINE\n"))
    (r / "other.txt").write_text("peer wip\n")
    _git(r, "add", "other.txt")            # peer loads the shared index

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1",
                  "-m", "mine", "shared.txt")
    assert cm.returncode == 0, cm.stderr + cm.stdout
    # my path reconciled, THEIR staging untouched
    assert _staged_names(r) == ["other.txt"]


def test_reconciliation_covers_a_file_the_commit_deleted(repo):
    """A deletion leaves the opposite lie — the index still holds the file, so
    the peer's commit resurrects it."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "doomed.txt").write_text("bye\n")
    (r / "kept.txt").write_text("L1\n")
    _git(r, "add", "doomed.txt", "kept.txt")
    _git(r, "commit", "-q", "-m", "base")

    (r / "doomed.txt").unlink()
    (r / "kept.txt").write_text("L1-MINE\n")
    patch = r / "del.patch"
    patch.write_text(_git(r, "diff", "--", "doomed.txt", "kept.txt"))
    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "delete it")
    assert cm.returncode == 0, cm.stderr + cm.stdout
    assert "doomed.txt" not in _git(r, "ls-tree", "--name-only", "HEAD")
    assert "kept.txt" in _git(r, "ls-tree", "--name-only", "HEAD")
    assert _staged_names(r) == []
    # the patch file itself is untracked scratch; no TRACKED path is dirty
    assert _git(r, "status", "--short", "--untracked-files=no") == ""
    # T-0753: the deleted path was missing from the patch's target list, so the
    # absorption check saw it land un-intended and cried peer-sweep over a file
    # this very patch removed. Both counts told the same lie ("1 file(s)").
    assert "ABSORPTION WARNING" not in cm.stderr
    assert "committed 2 file(s) from" in cm.stdout


# ---------------------------------------------------------------------------
# T-0753: a patch that ONLY deletes. The path lives on the `--- a/<rel>` side,
# so a target list read off `+++` alone came back empty and the commit was
# rejected before it began.
# ---------------------------------------------------------------------------
def test_delete_only_patch_commits_and_leaves_no_index_entry(repo):
    """The ticket's own repro, end to end: the commit lands, and the SHARED
    index no longer holds the deleted path (the T-0752 invariant — an index
    entry surviving here is what a peer's pathspec-less commit resurrects)."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "doomed.txt").write_text("bye\n")
    (r / "kept.txt").write_text("L1\n")
    _git(r, "add", "doomed.txt", "kept.txt")
    _git(r, "commit", "-q", "-m", "base")

    (r / "doomed.txt").unlink()
    patch = r / "del.patch"
    patch.write_text(_git(r, "diff", "--", "doomed.txt"))
    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "delete it")
    assert cm.returncode == 0, cm.stderr + cm.stdout
    assert "no file targets" not in cm.stderr

    assert "doomed.txt" not in _git(r, "ls-tree", "--name-only", "HEAD")
    assert "kept.txt" in _git(r, "ls-tree", "--name-only", "HEAD")
    # the invariant: gone from the index, not merely gone from HEAD
    assert "doomed.txt" not in _git(r, "ls-files").split()
    assert _staged_names(r) == []
    assert _git(r, "status", "--short", "--untracked-files=no") == ""
    # and the deletion is claimed as intended work, not shouted about
    assert "ABSORPTION WARNING" not in cm.stderr
    assert "committed 1 file(s) from" in cm.stdout


def test_delete_only_patch_survives_a_peers_pathspecless_commit(repo):
    """Why the index entry matters, driven the whole way: with the deleted path
    left in the shared index, a peer's `git add x && git commit` (no pathspec,
    so it commits the index as-is) puts the file straight back."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "doomed.txt").write_text("bye\n")
    _git(r, "add", "doomed.txt")
    _git(r, "commit", "-q", "-m", "base")

    (r / "doomed.txt").unlink()
    patch = r / "del.patch"
    patch.write_text(_git(r, "diff", "--", "doomed.txt"))
    assert _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                    "--sid", "S-me-dev-p1", "-m", "delete it").returncode == 0

    (r / "peer.txt").write_text("peer work\n")
    _git(r, "add", "peer.txt")
    _git(r, "commit", "-q", "-m", "peer work")

    assert "doomed.txt" not in _git(r, "ls-tree", "--name-only", "HEAD")
    assert "peer.txt" in _git(r, "ls-tree", "--name-only", "HEAD")


def test_delete_only_patch_rejects_an_undeclared_deletion(repo):
    """The `--files` declaration gate has to see deletions too — while the
    deleted path was invisible, naming a DIFFERENT file let the patch delete
    one the caller never declared, with no stray to show for it."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "doomed.txt").write_text("bye\n")
    (r / "kept.txt").write_text("L1\n")
    _git(r, "add", "doomed.txt", "kept.txt")
    _git(r, "commit", "-q", "-m", "base")

    (r / "doomed.txt").unlink()
    (r / "kept.txt").write_text("L1-MINE\n")
    patch = r / "mix.patch"
    patch.write_text(_git(r, "diff", "--", "doomed.txt", "kept.txt"))

    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "mixed", "kept.txt")
    assert cm.returncode != 0
    assert "did not declare: doomed.txt" in cm.stderr
    assert _git(r, "log", "--oneline").count("\n") == 1     # nothing committed

    # declaring it is accepted — a deleted path is nameable though it is gone
    ok = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "mixed", "kept.txt", "doomed.txt")
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "doomed.txt" not in _git(r, "ls-files").split()


def test_reconciliation_names_a_clobbered_peer_staging_of_the_same_path(repo):
    """When the repair does drop someone's staged entry, it says so and names
    a blob that still resolves — silence there would be a second silent loss."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "shared.txt").write_text("L1\nL2\n")
    _git(r, "add", "shared.txt")
    _git(r, "commit", "-q", "-m", "base")

    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "shared.txt")
    (r / "shared.txt").write_text("L1\nL2-MINE\n")
    _git(r, "add", "shared.txt")           # a raw `git add` on the same path

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1",
                  "--ack", "-m", "mine", "shared.txt")
    assert cm.returncode == 0, cm.stderr + cm.stdout
    assert "NOTE (T-0752)" in cm.stderr
    assert "shared.txt" in cm.stderr
    assert _staged_names(r) == []
    blob = re.search(r"blob ([0-9a-f]{40})", cm.stderr).group(1)
    assert _git(r, "cat-file", "-t", blob).strip() == "blob"


# ---------------------------------------------------------------------------
# T-0752 manifestation 2: the STALE-BASELINE sweep — `--hunks` isolates you
# only from work already in the file at edit-begin.
# ---------------------------------------------------------------------------
def _peer_store(bot_squad: str, sid: str, rel: str, body: str) -> None:
    p = Path(bot_squad) / "data" / "proj" / "_worktree" / sid / "files" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_hunks_stops_when_baseline_was_clean_and_a_peer_is_mid_edit(repo):
    """The ee7ba15 shape: the file was CLEAN at edit-begin, so the commit
    claims its entire current diff — including whatever a peer appended in the
    meantime, without the source change those lines pin."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "src.py").write_text("def f():\n    return 1\n")
    (r / "test_src.py").write_text("from src import f\n\n\ndef test_f():\n    assert f()\n")
    _git(r, "add", "src.py", "test_src.py")
    _git(r, "commit", "-q", "-m", "base")

    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "test_src.py")
    with (r / "test_src.py").open("a") as fh:              # mine
        fh.write("\n\ndef test_mine():\n    assert f()\n")
    with (r / "test_src.py").open("a") as fh:              # the peer's, pinning g()
        fh.write("\n\ndef test_peer():\n    from src import g\n    assert g()\n")
    with (r / "src.py").open("a") as fh:                   # …which they have NOT committed
        fh.write("\n\ndef g():\n    return 2\n")

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1",
                  "-m", "mine", "test_src.py")
    assert cm.returncode == 3
    assert "co-edit audit (T-0752)" in cm.stderr
    assert "isolates nothing" in cm.stderr
    assert "src.py" in cm.stderr                # names the peer-dirty file
    assert "test_peer" not in _git(r, "show", "HEAD:test_src.py")


def test_the_zero_isolation_refusal_is_not_openable_with_ack(repo):
    """operator p298, 2026-07-27: --ack must NOT open this one. A session in a
    hurry acks uniformly, which is how a warning decays into noise — and here
    there is nothing to weigh, because --hunks is not isolating anything."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "src.py").write_text("x = 1\n")
    (r / "mine.py").write_text("y = 1\n")
    _git(r, "add", "src.py", "mine.py")
    _git(r, "commit", "-q", "-m", "base")

    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "mine.py")
    (r / "mine.py").write_text("y = 2\n")
    (r / "src.py").write_text("x = 2\n")          # a peer, mid-edit, unbaselined

    acked = _run_bsq(r, bs, "commit", "--hunks", "--ack", "--sid", "S-me-dev-p1",
                     "-m", "mine", "mine.py")
    assert acked.returncode == 3
    assert "--ack does" in acked.stderr and "NOT open this one" in acked.stderr
    assert _git(r, "log", "--oneline").count("\n") == 1     # nothing committed

    # …and the escape it names inline actually works: name the paths.
    assert "bsq commit -m MSG mine.py" in acked.stderr
    esc = _run_bsq(r, bs, "commit", "--ack", "--sid", "S-me-dev-p1",
                   "-m", "mine", "mine.py")
    assert esc.returncode == 0, esc.stderr + esc.stdout
    assert "mine.py" in _git(r, "show", "--name-only", "--format=", "HEAD")
    assert "src.py" not in _git(r, "show", "--name-only", "--format=", "HEAD")


def test_the_refusal_also_names_the_trimmed_patch_route(repo):
    """The other escape: commit only your hunks. A refusal that leaves you
    stuck gets worked around, and the workaround would be worse."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "src.py").write_text("x = 1\n")
    (r / "mine.py").write_text("a\nb\n")
    _git(r, "add", "src.py", "mine.py")
    _git(r, "commit", "-q", "-m", "base")
    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "mine.py")
    (r / "mine.py").write_text("a-MINE\nb\n")
    (r / "src.py").write_text("x = 2\n")

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1",
                  "-m", "mine", "mine.py")
    assert cm.returncode == 3
    assert "bsq commit --hunks --patch /tmp/mine.patch -m MSG mine.py" in cm.stderr

    patch = r / "mine.patch"
    patch.write_text(_git(r, "diff", "--", "mine.py"))
    ok = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "mine", "mine.py")
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "a-MINE" in _git(r, "show", "HEAD:mine.py")
    assert "x = 2" not in _git(r, "show", "HEAD:src.py")   # peer's edit untouched


def test_hunks_does_not_nag_when_nobody_else_is_editing_the_tree(repo):
    """The caution must not become background noise: a clean baseline with no
    foreign dirty file is the ordinary case and commits straight through."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "base")
    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "a.txt")
    (r / "a.txt").write_text("two\n")
    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1", "-m", "x", "a.txt")
    assert cm.returncode == 0, cm.stderr + cm.stdout


def test_hunks_stops_when_a_peer_committed_the_file_after_my_baseline(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "f.txt").write_text("L1\nL2\nL3\n")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-q", "-m", "base")

    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "f.txt")
    (r / "f.txt").write_text("L1-PEER\nL2\nL3\n")
    _git(r, "commit", "-q", "-m", "peer landed under me", "--", "f.txt")
    (r / "f.txt").write_text("L1-PEER\nL2\nL3-MINE\n")

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1", "-m", "mine", "f.txt")
    assert cm.returncode != 0
    assert "a peer COMMITTED this file after your baseline" in cm.stderr
    assert "peer landed under me" in cm.stderr
    assert "bsq edit-begin f.txt" in cm.stderr      # names the remedy


def test_hunks_stops_when_a_live_peer_session_holds_a_baseline_on_the_file(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "f.txt").write_text("L1\nL2\n")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-q", "-m", "base")
    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "f.txt")
    _peer_store(bs, "S-peer-dev-p299", "f.txt", "L1\nL2\n")
    (r / "f.txt").write_text("L1\nL2-MINE\n")

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1", "-m", "x", "f.txt")
    assert cm.returncode != 0
    assert "S-peer-dev-p299" in cm.stderr
    assert "live edit-begin baseline" in cm.stderr


def test_a_dead_sessions_month_old_baseline_is_not_a_co_editor(repo):
    """Stores outlive their sessions and are never GC'd — the install still
    holds baselines from June. Age is the liveness proxy; without it every
    commit would block forever."""
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "f.txt").write_text("L1\nL2\n")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-q", "-m", "base")
    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "f.txt")
    _peer_store(bs, "S-dead-dev-p55", "f.txt", "L1\nL2\n")
    corpse = Path(bs) / "data" / "proj" / "_worktree" / "S-dead-dev-p55" / "files" / "f.txt"
    old = time.time() - 30 * 86400
    os.utime(corpse, (old, old))
    (r / "f.txt").write_text("L1\nL2-MINE\n")

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1", "-m", "x", "f.txt")
    assert cm.returncode == 0, cm.stderr + cm.stdout


def test_hunks_stops_when_the_shared_index_holds_staged_content_for_the_file(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "f.txt").write_text("L1\nL2\n")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-q", "-m", "base")
    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "f.txt")
    (r / "f.txt").write_text("L1\nL2-MINE\n")
    _git(r, "add", "f.txt")

    cm = _run_bsq(r, bs, "commit", "--hunks", "--sid", "S-me-dev-p1", "-m", "x", "f.txt")
    assert cm.returncode != 0
    assert "SHARED index holds staged content" in cm.stderr


def test_patch_path_reports_when_it_claims_every_live_hunk(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "f.txt").write_text("L1\nL2\n")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-q", "-m", "base")
    (r / "f.txt").write_text("L1\nL2-MINE\n")
    _peer_store(bs, "S-peer-dev-p299", "elsewhere.txt", "x\n")   # a shared clone
    patch = r / "mine.patch"
    patch.write_text(_git(r, "diff", "--", "f.txt"))

    cm = _run_bsq(r, bs, "commit", "--hunks", "--patch", str(patch),
                  "--sid", "S-me-dev-p1", "-m", "x")
    assert cm.returncode == 0, cm.stderr + cm.stdout   # caution, not a block
    assert "covers ALL 1 hunk(s)" in cm.stderr


def test_patch_hunks_per_rel_counts_per_target_file():
    patch = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1 @@\n-x\n+y\n@@ -9 +9 @@\n-p\n+q\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-m\n+n\n"
    )
    assert bsq._patch_hunks_per_rel(patch) == {"a.py": 2, "b.py": 1}


def test_edit_begin_records_the_head_it_baselined_against(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "f.txt").write_text("x\n")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-q", "-m", "base")
    _run_bsq(r, bs, "edit-begin", "--sid", "S-me-dev-p1", "f.txt")
    meta = json.loads(
        (Path(bs) / "data" / "proj" / "_worktree" / "S-me-dev-p1" / "meta.json").read_text()
    )
    assert meta["f.txt"]["head"] == _git(r, "rev-parse", "HEAD").strip()
    assert meta["f.txt"]["ts"].endswith("Z")


# ---------------------------------------------------------------------------
# T-0752 DoD item 3 — a worktree suite run does not certify a commit.
# ---------------------------------------------------------------------------
def test_verify_isolated_runs_against_head_not_the_dirty_worktree(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "probe.txt").write_text("committed\n")
    _git(r, "add", "probe.txt")
    _git(r, "commit", "-q", "-m", "base")
    (r / "probe.txt").write_text("a peer's uncommitted edit\n")

    cm = _run_bsq(r, bs, "verify-isolated", "--", "cat", "probe.txt")
    assert cm.returncode == 0, cm.stderr
    assert "committed" in cm.stdout
    assert "peer's uncommitted edit" not in cm.stdout


def test_verify_isolated_propagates_the_commands_exit_code(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "a.txt").write_text("x\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "base")
    cm = _run_bsq(r, bs, "verify-isolated", "--", "false")
    assert cm.returncode == 1
