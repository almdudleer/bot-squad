"""T-0779: a monkeypatched ``boot_git_sha`` must WIN over a warm sha cache.

THE DEFECT, as it presented. A full worker run during T-0775 reported
``1 failed, 2802 passed`` at ``tests/test_jobs.py::test_heartbeat_writes_boot_sha``.
The same file passed alone, passed alongside ``test_actions.py``, passed with
every file that sorts at-or-before it, and a second full run of the identical
tree was clean. So the T-0774 recipe — run the file list reversed, find the
earlier file that wrote the shared global — did not converge on it, and it
looked like a shrug.

THE MECHANISM. ``deploy._EFFECTIVE_SHA_CACHE`` was a ``(deployed, effective)``
pair keyed on ``deployed`` ALONE, while the value is derived from ``boot`` too.
``deployed`` comes from a real ``_git_head_sha(_install_root())``, which is the
same sha for every test in a run — so ANY earlier entry was a cache HIT for a
completely different ``boot``, and ``monkeypatch.setattr(D, "boot_git_sha", …)``
was silently ignored. Cache cold → the probe runs, the fake boot survives, green.
Cache warm with a foreign boot → the earlier test's answer is returned, red.
Nothing in ``test_jobs.py`` is at fault; it is the victim (the T-0774 rule).

WHAT A FULL INSTRUMENTED RUN ACTUALLY SHOWED, rather than what was inferred —
a setup/teardown probe over both globals across the forward order:

    tests/test_channels.py::test_deploy_notifications_flow_through_channel
      _BOOT_GIT_SHA: None -> 'c296acf…'          (the real dev-clone HEAD)
    tests/test_jobs.py::test_heartbeat_writes_boot_sha
      _EFFECTIVE_SHA_CACHE: None -> ('c296acf…', 'deadbeefcafe123')

Neither test mentions either global. The second one is the shape the ticket is
about: an entry keyed on a sha every test in the run shares, holding a value
that is true only for one test's fake boot.

WHAT EACH TEST BELOW PINS, because the fix has two independent halves and a
control that only exercises one of them proves half a fix:

* the KEY  — ``test_a_warm_cache_with_a_foreign_boot_loses_to_the_patched_boot``.
  Goes red if ``_EFFECTIVE_SHA_CACHE`` is keyed on ``deployed`` again.
* the FIXTURE — the ``leak`` / ``next test sees it clean`` pairs. They go red if
  ``conftest._isolate_deploy_sha_globals`` is deleted, in the file that explains
  why, instead of as an unattributable flake several thousand tests later.
* that the fixture is what is holding them — ``test_the_leak_is_real_without_the
  _conftest_fixture`` re-runs the same leak in a directory that has no conftest
  and REQUIRES it red. A guard that passes with the guard removed pins nothing
  (T-0740).
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bot_squad_worker import deploy as D

_WORKER_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _head(repo: Path) -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _init_repo(repo: Path, body: str) -> str:
    """A one-commit git repo. Returns its HEAD sha."""
    (repo / "worker" / "bot_squad_worker").mkdir(parents=True)
    (repo / "worker" / "bot_squad_worker" / "sessions.py").write_text(body)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _head(repo)


def _worker_tree_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """base → docs_only (``worker/`` byte-identical) → worker_change.

    Same shape as ``test_deploy.py``'s helper and deliberately a local copy:
    this file must stay runnable on its own, since "run just these two files"
    is how an order-dependency gets attributed.
    """
    repo = tmp_path / "install"
    (repo / "worker" / "bot_squad_worker").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "worker" / "bot_squad_worker" / "sessions.py").write_text("v1\n")
    (repo / "docs" / "roles.md").write_text("roles v1\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _head(repo)
    (repo / "docs" / "roles.md").write_text("roles v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs only")
    docs_only = _head(repo)
    (repo / "worker" / "bot_squad_worker" / "sessions.py").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "worker change")
    worker_change = _head(repo)
    return repo, base, docs_only, worker_change


# --- the KEY: a foreign boot in the cache must not answer for this boot ------


def test_a_warm_cache_with_a_foreign_boot_loses_to_the_patched_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE positive control the ticket asks for, driven entirely through
    production code — no hand-poked global, so it cannot go stale when the
    cache's internal shape changes.

    One ``deployed`` sha, two different ``boot`` shas, and the correct answer
    DIFFERS between them: from ``worker_change`` the ``worker/`` subtree is
    edited, so the report must stay at boot; from ``base`` it is byte-identical,
    so the report must advance to ``deployed``. The first call warms the cache;
    the second must not inherit it.

    RED before the fix — the second call returned ``worker_change``, the first
    call's answer, because the entry was keyed on ``deployed`` which both calls
    share. That is exactly what ``test_heartbeat_writes_boot_sha`` hit.
    """
    repo, base, docs_only, worker_change = _worker_tree_repo(tmp_path)
    monkeypatch.setattr(D, "_install_root", lambda: repo)
    monkeypatch.setattr(D, "_git_head_sha", lambda root: docs_only)

    monkeypatch.setattr(D, "boot_git_sha", lambda: worker_change)
    assert D.effective_worker_git_sha() == worker_change, (
        "precondition: worker/ differs between worker_change and deployed, so "
        "the reported sha must stay at boot")
    assert D._EFFECTIVE_SHA_CACHE is not None, (
        "precondition: the first call must WARM the cache, or the second call "
        "below proves nothing")

    monkeypatch.setattr(D, "boot_git_sha", lambda: base)
    assert D.effective_worker_git_sha() == docs_only, (
        "a cache entry belonging to a DIFFERENT boot sha answered for this one")


def test_the_cache_still_answers_for_the_same_boot_and_deployed_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the key change: widening it must not turn the cache OFF.

    The 60s heartbeat and every ``/health`` hit call this; re-probing git each
    time is the cost T-0717 added the cache to avoid. Same ``(boot, deployed)``
    on every call → exactly one ``git diff``.
    """
    repo, base, docs_only, _ = _worker_tree_repo(tmp_path)
    monkeypatch.setattr(D, "_install_root", lambda: repo)
    monkeypatch.setattr(D, "boot_git_sha", lambda: base)
    monkeypatch.setattr(D, "_git_head_sha", lambda root: docs_only)
    probes: list = []
    real = D._worker_subtree_identical
    monkeypatch.setattr(D, "_worker_subtree_identical",
                        lambda *a, **k: (probes.append(a), real(*a, **k))[1])

    assert [D.effective_worker_git_sha() for _ in range(3)] == [docs_only] * 3
    assert len(probes) == 1


# --- the FIXTURE: neither global escapes the test that wrote it --------------
#
# Definition order is what pairs these, and pytest preserves it within a file —
# so the pairs hold under the reverse-FILE-order gate too. The property under
# test is not itself order-dependent.


@pytest.fixture(scope="module")
def _two_repos(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, Path, str]:
    """Two distinct one-commit repos, shared by the leak/check pair below.

    Module-scoped because the pair needs the SAME two shas across two tests;
    a per-test ``tmp_path`` would give each of them its own and the check would
    pass for the wrong reason.
    """
    root = tmp_path_factory.mktemp("t0779")
    a, b = root / "a", root / "b"
    return a, _init_repo(a, "a\n"), b, _init_repo(b, "b\n")


def test_freezing_the_boot_sha_leaks_into_the_rest_of_the_run(
    _two_repos: tuple[Path, str, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freeze on purpose, exactly the way ``test_channels.py`` freezes by
    accident while exercising the deploy notification path.

    ``boot_git_sha`` memoises into a module global that ``monkeypatch`` never
    sees — it patches ``_install_root``, and production code then writes
    ``_BOOT_GIT_SHA`` itself. Nothing restores that at teardown.
    """
    repo_a, sha_a, _, _ = _two_repos
    monkeypatch.setattr(D, "_install_root", lambda: repo_a)

    assert D.boot_git_sha() == sha_a
    assert D._BOOT_GIT_SHA == sha_a


def test_the_next_test_gets_an_unfrozen_boot_sha(
    _two_repos: tuple[Path, str, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion the fixture comes down to.

    Runs straight after a test that froze repo A's HEAD into the global. Points
    at repo B and requires B's answer. Without
    ``conftest._isolate_deploy_sha_globals`` this returns repo A's sha — a
    WRONG VALUE, not an error, which is the hardest kind to attribute.
    """
    _, sha_a, repo_b, sha_b = _two_repos
    monkeypatch.setattr(D, "_install_root", lambda: repo_b)

    assert D._BOOT_GIT_SHA is None, "the previous test's frozen boot sha escaped"
    assert D.boot_git_sha() == sha_b != sha_a


def test_warming_the_effective_sha_cache_leaks_into_the_rest_of_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same pair for the second global. ``monkeypatch`` restores what it set;
    it cannot restore what production code wrote afterwards.
    """
    repo, base, docs_only, _ = _worker_tree_repo(tmp_path)
    monkeypatch.setattr(D, "_install_root", lambda: repo)
    monkeypatch.setattr(D, "boot_git_sha", lambda: base)
    monkeypatch.setattr(D, "_git_head_sha", lambda root: docs_only)

    assert D.effective_worker_git_sha() == docs_only
    assert D._EFFECTIVE_SHA_CACHE is not None


def test_the_next_test_gets_an_empty_effective_sha_cache() -> None:
    """Runs straight after the test above, which left an entry behind.

    The ``(boot, deployed)`` key already makes a stale entry harmless, so this
    is belt-and-braces on purpose: the key defends the VALUE, the fixture
    defends the STATE, and a later change to either one must not quietly leave
    the suite depending on the other.
    """
    assert D._EFFECTIVE_SHA_CACHE is None


# --- proof that it is the fixture holding those, not something else ----------


def _subprocess_env() -> dict[str, str]:
    """Environment for a nested pytest: this tree importable, nothing else new."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_WORKER_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


_LEAK_PAIR = '''
    from pathlib import Path
    import subprocess

    from bot_squad_worker import deploy as D


    def _init(repo, body):
        (repo / "worker" / "bot_squad_worker").mkdir(parents=True)
        (repo / "worker" / "bot_squad_worker" / "sessions.py").write_text(body)
        for a in (["init", "-q"], ["config", "user.email", "t@e.com"],
                  ["config", "user.name", "T"], ["add", "-A"],
                  ["commit", "-q", "-m", "c"]):
            subprocess.run(["git", "-C", str(repo), *a], check=True,
                           capture_output=True)
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()


    REPOS = {}


    def test_leak(tmp_path, monkeypatch):
        repo = tmp_path / "a"
        REPOS["a"] = (repo, _init(repo, "a\\n"))
        monkeypatch.setattr(D, "_install_root", lambda: REPOS["a"][0])
        assert D.boot_git_sha() == REPOS["a"][1]


    def test_victim(tmp_path, monkeypatch):
        repo = tmp_path / "b"
        repo_b, sha_b = repo, _init(repo, "b\\n")
        monkeypatch.setattr(D, "_install_root", lambda: repo_b)
        assert D.boot_git_sha() == sha_b, "inherited the previous test's frozen sha"
'''


def test_the_leak_is_real_without_the_conftest_fixture(tmp_path: Path) -> None:
    """POSITIVE CONTROL (T-0740): the same leak/victim pair, run where
    ``tests/conftest.py`` cannot reach it, MUST go red.

    Without this, the four fixture tests above are indistinguishable from four
    tests that would pass anyway — and that is not a hypothetical worry: the
    ``(boot, deployed)`` key alone already neutralises the cache half, so
    "it's green" was never evidence the fixture does anything.

    A temp directory rather than a flag on the fixture: an off-switch in
    ``conftest.py`` is a way for the real suite to run unguarded, and the thing
    being verified is the fixture's ABSENCE, which a temp dir gives for free.

    Failure here reads as: either the leak is no longer possible (in which case
    delete the fixture tests, deliberately, and say so) or this control has
    stopped exercising it.
    """
    d = tmp_path / "unguarded"
    d.mkdir()
    (d / "test_leak_pair.py").write_text(textwrap.dedent(_LEAK_PAIR), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(d / "test_leak_pair.py")],
        cwd=str(tmp_path), env=_subprocess_env(), capture_output=True, text=True,
        timeout=300,
    )

    assert proc.returncode != 0, (
        "the unguarded leak pair PASSED — this control no longer demonstrates "
        f"anything the conftest fixture is protecting against.\n{proc.stdout[-3000:]}")
    assert "1 failed, 1 passed" in proc.stdout, proc.stdout[-3000:]
    assert "inherited the previous test's frozen sha" in proc.stdout
