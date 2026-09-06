"""Worker test fixtures."""
from __future__ import annotations

import importlib.util
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
#
# T-0802 rides the SAME directory for `tmux`, for the same reason and after the
# same class of incident: a suite run reached the real tmux and left a live
# `claude` window behind, one per run, until 70 of them had eaten the host's
# RAM and swap. See fake_bin/tmux; test_tmux_hermeticity.py pins it.
_FAKE_BIN = str(Path(__file__).resolve().parent / "fake_bin")
if os.environ.get("PATH", "").split(os.pathsep)[0] != _FAKE_BIN:
    os.environ["PATH"] = _FAKE_BIN + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault(
    "BOT_SQUAD_TEST_FAKE_SYSTEMCTL_LOG",
    os.path.join(tempfile.gettempdir(), f"bot-squad-fake-systemctl-{os.getpid()}.log"),
)
os.environ.setdefault(
    "BOT_SQUAD_TEST_FAKE_TMUX_LOG",
    os.path.join(tempfile.gettempdir(), f"bot-squad-fake-tmux-{os.getpid()}.log"),
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


@pytest.fixture(autouse=True)
def _isolate_actions_config():
    """T-0802: restore ``actions._CONFIG`` after EVERY worker test.

    ``actions._CONFIG`` is the worker's process-global Config singleton, set
    once at startup by ``__main__`` and read by ``_get_config()`` inside every
    action handler. In a test process "once at startup" becomes "whichever test
    set it first", and it stays set for the rest of the run.

    THAT IS THE CONDITION THAT MADE THE T-0802 HOST LEAK REACHABLE, measured on
    the full suite rather than reasoned about:

    * ``tests/test_tg_listener.py::test_private_voice_rejection_replies_to_the_note``
      drives ``TL.handle_update``, which calls ``_ensure_user_conversation`` →
      ``A.dispatch("ensure_user_conversation", …)``. The test stubs the TG
      transport but not that seam.
    * Run ALONE the dispatch dies immediately on ``worker config not
      initialised`` and nothing happens. Run inside the SUITE it finds the
      Config that ``tests/test_actions.py::test_reload_projects_picks_up_new_slug``
      (and its ``…_rejects_params`` neighbour) put there via ``A.set_config``
      and never took back — built from the ``tmp_config_dir`` fixture, whose
      ``projects`` holds the fixture slug ``test-project`` at ``/tmp/test-repo``.
      The slug check then passes and ``sessions.spawn`` runs for real: ``tmux
      new-session -d -s test-project -c /tmp/test-repo`` plus a real ``claude``
      window. The leaked spawn's own ``-c /tmp/test-repo`` is what identifies
      that fixture as the source. One live claude per suite run, 70 of them over
      three days, host RAM + all 8 GB of swap consumed.

    So the same test is inert alone and destructive in a suite, which is why it
    survived every targeted run anyone did. Same shape as the ``DROPS`` and
    deploy-sha globals above: the polluter is any test anywhere, and the victim
    has no reason to know the global exists.

    WHY RESTORE RATHER THAN CLEAR-AND-RESTORE. ``_CONFIG`` starts as ``None`` at
    import, so restoring each test's pre-test value makes ``None`` the state
    every test opens with unless it sets the config itself — which is exactly
    the property needed, and it keeps working for tests that set it in a
    module-scoped fixture (clearing would break those).

    This is the belt; ``fake_bin/tmux`` is the braces — that shim stops the
    mutation whatever leaks it. ``tests/test_actions_config_isolation.py`` pins
    this fixture.
    """
    from bot_squad_worker import actions as _actions

    before = _actions._CONFIG
    yield
    _actions._CONFIG = before


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


# --- T-1002: an incomplete substrate must refuse to be read as a result -------
#
# `worker/tests/` gets run under the bot-squad-api image / a bare api[dev]
# install, because conftest and much of the worker are stdlib-clean and four
# lint-CI steps rely on exactly that. But apscheduler is a worker dependency
# the api tree does not declare, and `ScheduleTrigger.__init__` imports it
# LAZILY — so that substrate produces a plausible 122 passed / 5 failed rather
# than a broken harness, and every schedule-trigger path in it is UNMEASURED
# while the 122 gets cited as coverage. Same shape as T-0824's 22 phantom
# failures from a missing `rsync`.
#
# The hooks below annotate such a run: a banner in the terminal summary and a
# one-line trailer AFTER pytest's own count, so the caveat travels with
# whichever line a reader copies. See tests/substrate.py for the classifier
# (property-conditioned — no name list to drift) and its stated limits.
# tests/test_substrate_refusal.py pins both the fire and the no-fire cases.
_substrate_spec = importlib.util.spec_from_file_location(
    "bot_squad_test_substrate", Path(__file__).resolve().parent / "substrate.py"
)
substrate = importlib.util.module_from_spec(_substrate_spec)
_substrate_spec.loader.exec_module(substrate)


def pytest_exception_interact(node, call, report):
    """Every exception that reaches the reporting layer passes through here."""
    if call.excinfo is not None:
        substrate.record_exception(call.excinfo.value)


# --- T-1019: a run must declare its own provenance ---------------------------
#
# T-1002's substrate guard above asks "is my environment complete"; it cannot
# see a run that measured working-tree files that exist in no commit — the
# defect T-1002 itself paid for twice (see provenance.py's docstring). The
# hooks below run at terminal-summary time, after every test has already
# imported whatever it imports, and ask git which of those files differ from
# HEAD. See tests/provenance.py for the design and its stated limits;
# tests/test_t1019_provenance_gate.py pins both the fire and the no-fire cases.
_provenance_spec = importlib.util.spec_from_file_location(
    "bot_squad_test_provenance", Path(__file__).resolve().parent / "provenance.py"
)
provenance = importlib.util.module_from_spec(_provenance_spec)
_provenance_spec.loader.exec_module(provenance)

_provenance_result = None  # cached here in pytest_terminal_summary; read by pytest_unconfigure


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if substrate.any_gap():
        terminalreporter.write_sep("=", substrate.BANNER_TITLE, red=True, bold=True)
        for line in substrate.banner_lines():
            terminalreporter.write_line(line, red=True)

    global _provenance_result
    _provenance_result = provenance.check()
    if _provenance_result.cannot_check:
        terminalreporter.write_sep("=", provenance.CANNOT_CHECK_TITLE, yellow=True, bold=True)
        for line in provenance.cannot_check_lines():
            terminalreporter.write_line(line, yellow=True)
    elif _provenance_result.differing:
        terminalreporter.write_sep("=", provenance.BANNER_TITLE, red=True, bold=True)
        for line in provenance.banner_lines(_provenance_result.differing):
            terminalreporter.write_line(line, red=True)


def pytest_unconfigure(config):
    if not substrate.any_gap() and (_provenance_result is None or _provenance_result.clean):
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # -p no:terminal, or an embedding harness
        return
    if substrate.any_gap():
        reporter.write_line(substrate.trailer_line(), red=True, bold=True)
    if _provenance_result is not None and not _provenance_result.clean:
        reporter.write_line(provenance.trailer_line(_provenance_result), red=True, bold=True)
