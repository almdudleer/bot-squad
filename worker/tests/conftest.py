"""Worker test fixtures."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Quiet-hours dropping is time-dependent; tests should always exercise the
# send path regardless of wall-clock.
os.environ.setdefault("BOT_SQUAD_DISABLE_QUIET_HOURS", "1")

# T-0574 hermeticity: deploy/apply tests execute GENERATED scripts end-to-end;
# those scripts (and their detached `sleep && systemctl` children) resolve
# `systemctl` / `systemd-run` via the inherited PATH, out of reach of Python
# monkeypatching. Prepend the committed fake_bin recording shims so no test
# can restart the LIVE bot-squad-worker service or spawn real systemd scopes.
# test_suite_hermeticity.py pins this guarantee.
_FAKE_BIN = str(Path(__file__).resolve().parent / "fake_bin")
if os.environ.get("PATH", "").split(os.pathsep)[0] != _FAKE_BIN:
    os.environ["PATH"] = _FAKE_BIN + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault(
    "BOT_SQUAD_TEST_FAKE_SYSTEMCTL_LOG",
    os.path.join(tempfile.gettempdir(), f"bot-squad-fake-systemctl-{os.getpid()}.log"),
)


@pytest.fixture(autouse=True)
def _isolate_outbound_drops():
    """T-0774: zero ``outbound_log.DROPS`` around EVERY worker test.

    ``DROPS`` is process-global by design ("Reset only by process restart" —
    outbound_log.py) and production code bumps ``DROPS["record"]`` from the
    ``except`` arms of :func:`record` / :func:`record_response` whenever a
    record is swallowed. So ANY test that makes the transport swallow a record
    leaks a count into every test that runs after it, in the same process, for
    the rest of the run.

    That is not hypothetical and it is not confined to tests that know the
    global exists: ``tests/test_tg.py`` leaks 17 purely as a side effect of
    exercising the transport, and the string ``DROPS`` appears zero times in
    it. Its author had no reason to know.

    The counter is an input to ``outbound_liveness.check``, which reports
    DECAYED on a non-zero count — so the damage presents as a WRONG VERDICT in
    an unrelated module, not as an error, and it lands on whichever file
    happens to sort later. Before this fixture the suite was green in
    alphabetical order and 5-red in reverse, at the same commit.

    WHY THIS LIVES HERE AND NOT IN THE READERS. The previous defence was a
    per-file autouse fixture in each module that READS the counter — three of
    them by the time this was written. Every one defends the victim; none can
    reach the source, because the source is any test anywhere. A fourth would
    have been the same mistake.

    WHY ZERO RATHER THAN SNAPSHOT-AND-RESTORE. Restoring the pre-test value
    faithfully preserves a count that leaked before this fixture ever ran —
    from a module-scoped fixture, or at import time during collection. Zeroing
    gives every test the same known starting state no matter what preceded it,
    which is the property the suite actually needs. Tests that assert on a
    DELTA (``before = DROPS["record"]`` … ``== before + 1``) are unaffected.

    This touches test state only; ``outbound_log`` runtime behaviour is
    deliberately unchanged (making the counter non-global is production design,
    out of scope here). ``tests/test_drops_isolation.py`` pins this fixture.
    """
    from bot_squad_worker import outbound_log as _outbound_log

    for key in _outbound_log.DROPS:
        _outbound_log.DROPS[key] = 0
    yield
    for key in _outbound_log.DROPS:
        _outbound_log.DROPS[key] = 0


@pytest.fixture(autouse=True)
def _isolate_deploy_sha_globals():
    """T-0779: clear ``deploy._BOOT_GIT_SHA`` and ``deploy._EFFECTIVE_SHA_CACHE``
    around EVERY worker test.

    Both are process-global memoisations that are CORRECT for the real worker
    and wrong for a test process. ``boot_git_sha()`` freezes the install tree's
    HEAD on first call and never recomputes — that is the whole point (T-0461):
    it is how the restart gate knows the running process is on stale code.
    ``effective_worker_git_sha()`` then caches the answer it derives from it.
    In a suite, "once per process" means "once per RUN of ~2800 tests", so the
    first test to touch either one decides what every later test sees.

    MEASURED, not inferred (T-0779, full forward run instrumented with a
    setup/teardown probe over both globals):

    * ``tests/test_channels.py::test_deploy_notifications_flow_through_channel``
      freezes ``_BOOT_GIT_SHA`` to the real dev-clone HEAD as a side effect of
      exercising the deploy notification path. It never mentions the global —
      same shape as ``test_tg.py`` leaking 17 into ``outbound_log.DROPS``.
    * ``tests/test_jobs.py::test_heartbeat_writes_boot_sha`` leaves
      ``(<real deployed sha>, "deadbeefcafe123")`` in the cache — an entry keyed,
      before the fix, on a sha shared by every test in the run.

    That second one is why the ticket exists: with the old ``deployed``-only key
    an earlier entry was a cache HIT for a foreign ``boot``, so a
    ``monkeypatch.setattr(D, "boot_git_sha", ...)`` was silently ignored and the
    test failed — but only when something happened to warm the cache first,
    which is why it presented as an unreproducible flake rather than an order
    dependency the T-0774 reverse-order gate could converge on.

    WHY THIS LIVES HERE AND NOT IN ``test_jobs.py``. That file is the victim,
    not the fault (the T-0774 rule in AGENT_INSTRUCTIONS). The polluter is any
    test anywhere that calls a heartbeat, a ``/health`` handler, or a deploy
    path with a patched boot sha, and it has no reason to know these globals
    exist. A per-file fixture defends one reader; this defends all ~100 files.

    WHY CLEAR RATHER THAN SNAPSHOT-AND-RESTORE. Restoring the pre-test value
    faithfully preserves whatever leaked before this fixture first ran — from a
    module-scoped fixture, or at import time during collection. Clearing gives
    every test the same known starting state regardless of what preceded it,
    which is the property the suite needs. (Same call, same reasons, as
    ``_isolate_outbound_drops`` above.)

    Production behaviour is deliberately unchanged: the real worker calls
    ``freeze_boot_git_sha()`` once at startup and keeps its memoised value for
    the life of the process. ``tests/test_effective_sha_cache_isolation.py``
    pins this fixture and the key it depends on.
    """
    from bot_squad_worker import deploy as _deploy

    _deploy._BOOT_GIT_SHA = None
    _deploy._EFFECTIVE_SHA_CACHE = None
    yield
    _deploy._BOOT_GIT_SHA = None
    _deploy._EFFECTIVE_SHA_CACHE = None


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Provide a writable bot-squad data dir for a single test."""
    (tmp_path / "_sock").mkdir()
    (tmp_path / "_worker").mkdir()
    return tmp_path


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Provide a config dir with a minimal projects.toml and secrets.toml."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        'repo_path = "/tmp/test-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg / "secrets.toml").write_text(
        '[telegram]\n'
        'bot_token = "TESTBOT:TOKEN"\n'
        'auth_age_max = 86400\n'
    )
    return cfg
