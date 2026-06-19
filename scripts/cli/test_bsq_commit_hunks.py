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
import os
import subprocess
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


def test_plain_commit_unchanged_still_pathspec_commits(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    f = r / "y.txt"
    f.write_text("hello\n")
    cm = _run_bsq(r, bs, "commit", "--sid", "S-me-dev-p1", "-m", "add y", "y.txt")
    assert cm.returncode == 0, cm.stderr
    assert "y.txt" in _git(r, "show", "--name-only", "--format=", "HEAD")
