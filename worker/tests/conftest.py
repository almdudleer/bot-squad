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
