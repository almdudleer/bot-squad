"""T-0802: ``actions._CONFIG`` must not survive the test that set it.

THE DEFECT THIS PINS, measured on a full instrumented worker-suite run rather
than reasoned about. ``actions._CONFIG`` is the worker's process-global Config
singleton. ``tests/test_actions.py::test_reload_projects_picks_up_new_slug``
sets it (legitimately — ``reload_projects`` is *about* that global) and nothing
takes it back, so for the remaining ~2000 tests of the run every
``A.dispatch(...)`` sees a fixture Config whose ``projects`` holds the fixture
slug ``test-project`` rooted at ``/tmp/test-repo``.

Some 1500 tests later ``tests/test_tg_listener.py::
test_private_voice_rejection_replies_to_the_note`` drives ``TL.handle_update``
without stubbing ``_ensure_user_conversation``. Alone, that dispatch dies on
"worker config not initialised" and nothing happens — which is why every
targeted run of that test was clean. In the suite it found the leaked config,
the slug check passed, and ``sessions.spawn`` ran FOR REAL:

    tmux new-session -d -s test-project -c /tmp/test-repo -n _init
    tmux new-window  -d -t test-project: -n gu_1-user-conversation … claude …

That ``-c /tmp/test-repo`` is the fingerprint of the ``tmp_config_dir`` fixture,
and it is how the polluter was identified rather than guessed. One live claude
process per suite run, never reaped: 70 of them by day three, the host's RAM and
all 8 GB of swap consumed, two devs' suites lost to the thrashing.

The guard is ``_isolate_actions_config`` in ``tests/conftest.py``. These tests
exist so removing it fails HERE, in the file that explains why, instead of as a
host outage nobody attributes to the test suite for three days.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import types
import uuid
from pathlib import Path

import bot_squad_worker.actions as A

_WORKER_ROOT = Path(__file__).resolve().parent.parent


def _require_seal() -> None:
    """Fail before doing anything, if ``fake_bin/tmux`` is not in force.

    The test below deliberately drives the exact dispatch that leaked. With the
    seal in place that dispatch dies inside the shim and proves the point;
    WITHOUT it, the same dispatch spawns a real `claude` on the host — the guard
    test would commit the offence it was written to detect, and only afterwards
    report it. So probe first, with a verb whose worst case against the real
    binary is "can't find session", and refuse to continue.
    """
    probe = subprocess.run(
        ["tmux", "kill-session", "-t", f"t0802-probe-{uuid.uuid4().hex[:8]}"],
        capture_output=True, text=True)
    if "T-0802" not in probe.stderr:
        raise AssertionError(
            "refusing to run the leak reproduction: tmux mutations are NOT "
            "being refused, so this test would spawn a real claude process on "
            f"the host. Is worker/tests/fake_bin/tmux present and on PATH? "
            f"probe stderr: {probe.stderr!r}")


def test_a_leaked_config_does_not_escape_the_test_that_set_it() -> None:
    """Pollute on purpose, exactly the way ``test_reload_projects_*`` pollutes
    by accident. Paired with the test BELOW; pytest preserves definition order
    within a file, so the pair holds under reverse file order too."""
    A.set_config(types.SimpleNamespace(projects={"test-project": object()}))
    assert A._get_config() is not None


def test_the_next_test_sees_an_uninitialised_config() -> None:
    """The assertion the whole ticket comes down to."""
    assert A._CONFIG is None, (
        "actions._CONFIG survived the previous test — conftest's "
        "_isolate_actions_config is gone, and a test that reaches "
        "A.dispatch() can now spawn real sessions on the host again")


def test_dispatch_refuses_when_no_test_initialised_the_config() -> None:
    """The behaviour that makes the isolation load-bearing: with the global
    clean, a test that reaches an action by accident is REFUSED rather than
    silently served someone else's project registry."""
    from bot_squad_worker.actions import ActionError

    try:
        A.dispatch("ensure_user_conversation",
                   {"slug": "test-project", "global_user_id": "gu_1"})
    except ActionError as exc:
        assert "not initialised" in str(exc), exc
    else:                                        # pragma: no cover - guard
        raise AssertionError("dispatch succeeded with no config injected")


def test_the_leak_cannot_reach_the_host_even_when_the_config_is_polluted(
        tmp_path: Path) -> None:
    """END-TO-END, the ticket's own scenario: pollute the global the way the
    suite polluted it, drive the action that leaked, and require the host's tmux
    to be untouched.

    This is the belt-and-braces assertion — it passes even if the conftest
    fixture above is removed, because ``fake_bin/tmux`` refuses the mutation.
    Both defences are deliberate: the fixture stops the config from travelling,
    the shim stops any OTHER traveller from reaching the server.
    """
    from bot_squad_worker.config import Config
    from bot_squad_worker.actions import ActionError

    _require_seal()
    slug = "test-project"
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        f'[projects.{slug}]\n'
        f'slug = "{slug}"\n'
        f'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        f'deploy_branch = ""\nmaster_branch = ""\nprod_url = ""\n'
        f'staging_url = ""\ndev_url = ""\ndeploy_targets = []\ntg_chat = ""\n'
        f'created_at = 2026-05-10\n', encoding="utf-8")
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n',
                                          encoding="utf-8")
    real_cfg = Config.load(cfg_dir)
    A.set_config(types.SimpleNamespace(
        projects=real_cfg.projects,
        data_dir=tmp_path / "data",
        tg_bot_token="",
    ))
    (tmp_path / "data" / slug / "sessions").mkdir(parents=True)

    gid = f"gu_{uuid.uuid4().hex[:12]}"
    log = os.environ.get("BOT_SQUAD_TEST_FAKE_TMUX_LOG", "")
    before = Path(log).read_text(encoding="utf-8") if log and Path(log).exists() else ""

    try:
        A.dispatch("ensure_user_conversation",
                   {"slug": slug, "global_user_id": gid,
                    "message_ref": "T-0802 seal check"})
    except ActionError:
        pass                       # the refused tmux call surfaces as this

    # POSITIVE CONTROL — this test is worthless if the spawn path was never
    # reached (a silently-refused dispatch would "pass" while proving nothing).
    # The shim's own log must show the attempt.
    assert log, "conftest did not set BOT_SQUAD_TEST_FAKE_TMUX_LOG"
    added = Path(log).read_text(encoding="utf-8")[len(before):]
    assert f"new-session -d -s {slug}" in added, (
        "the spawn path was never reached, so the assertion below proves "
        f"nothing. tmux calls seen: {added!r}")

    # …and the host must not have gained the session, nor a window for this gid.
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / "tmux" if d else None
        if cand and cand.is_file() and cand.parent.name != "fake_bin":
            probe = subprocess.run([str(cand), "list-windows", "-a", "-F",
                                    "#{session_name}|#{window_name}"],
                                   capture_output=True, text=True)
            assert f"{gid}-user-conversation" not in probe.stdout, (
                "the suite created a REAL claude window on the host — the "
                "T-0802 leak is back")
            break


def test_that_the_seal_check_can_actually_go_red() -> None:
    """Positive control for the shim the test above leans on (T-0740).

    A refusal that is really "tmux is missing" would make every assertion above
    pass for the wrong reason. Drive a mutation directly and require the shim's
    own refusal text.
    """
    name = f"t0802-{uuid.uuid4().hex[:8]}"
    try:
        proc = subprocess.run(["tmux", "new-session", "-d", "-s", name],
                              capture_output=True, text=True)
        assert proc.returncode != 0
        assert "T-0802" in proc.stderr, (
            f"tmux mutations are not being refused by the shim: {proc.stderr!r}")
    finally:
        # Unsealed, the call above SUCCEEDS against the real binary; a guard
        # test must not leave the thing it guards against behind.
        for d in os.environ.get("PATH", "").split(os.pathsep):
            cand = Path(d) / "tmux" if d else None
            if cand and cand.is_file() and cand.parent.name != "fake_bin":
                subprocess.run([str(cand), "kill-session", "-t", name],
                               capture_output=True, text=True)
                break


def _run_child_hard_kill(argv: list, *, cwd, env=None,
                          timeout: float) -> subprocess.CompletedProcess:
    """Run a child pytest with a HARD kill on timeout (T-1024; the T-0212
    idiom from routines.py / deploy.py). A bare ``timeout=`` on
    ``subprocess.run`` only kills the direct child on expiry — a grandchild
    still holding the stdout/stderr pipe leaves ``communicate()`` blocked past
    the stated timeout anyway. Leading its own process group
    (``start_new_session=True``) lets a timed-out run's ``killpg`` reach every
    descendant.
    """
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # own process group -> killpg reaches children
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def test_the_polluter_and_the_victim_run_clean_together() -> None:
    """THE regression test: the ticket's reproduction in two files rather than a
    hundred, in a clean process where file ORDER is the variable.

    A subprocess, not an in-process ``pytest.main`` — a nested in-process run
    would inherit this process's already-isolated module state and prove nothing
    (the T-0774 lesson, same shape).

    IT ASSERTS THE CLAIM, NOT THE EXIT CODE. The property this ticket owns is
    "the run did not mutate the host's tmux", and that is what is checked, via
    the shim's own call log. The subset's pass/fail is deliberately NOT the
    gate: measured at HEAD with the shim dropped in and nothing else changed,
    this same pair reported 22 failed / 492 passed, so a green-suite assertion
    here would encode an unrelated order-dependency and go red for reasons that
    have nothing to do with tmux. A pass COUNT is still required, because a run
    that collected nothing also mutates nothing — that would be a null from a
    broken instrument dressed as a pass.
    """
    tests = _WORKER_ROOT / "tests"
    polluter = tests / "test_actions.py"
    victim = tests / "test_tg_listener.py"
    assert polluter.exists() and victim.exists(), (
        "T-0802's reproduction pair has been renamed; re-point this test at the "
        "file that calls A.set_config and the one that reaches A.dispatch")

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "tmux-calls.log"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_WORKER_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        # Per-run sinks: inheriting THIS process's would make the child append to
        # a log another test asserts on.
        env["BOT_SQUAD_TEST_FAKE_TMUX_LOG"] = str(log)
        env["BOT_SQUAD_TEST_FAKE_SYSTEMCTL_LOG"] = str(Path(td) / "systemctl.log")

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             str(polluter), str(victim)],
            cwd=str(_WORKER_ROOT), env=env, capture_output=True, text=True,
            timeout=1800,
        )
        calls = log.read_text(encoding="utf-8") if log.exists() else ""

    # Execution instrument: a run that collected nothing mutates nothing.
    import re
    passed = re.search(r"(\d+) passed", proc.stdout)
    assert passed and int(passed.group(1)) > 400, (
        "the reproduction run did not execute — its result says nothing about "
        f"tmux.\nstdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-2000:]}")

    # The claim: the run must not have ASKED tmux to create anything. Before
    # this ticket this pair produced the mutating calls quoted in the module
    # docstring — and four real `claude` windows on the host, measured.
    offenders = [ln for ln in calls.splitlines()
                 if ln.startswith("tmux ")
                 and ln.split()[1] not in {
                     "list-sessions", "list-panes", "list-windows", "list-clients",
                     "has-session", "display-message", "display", "show-options",
                     "capture-pane", "info", "server-info", "ls", "lsp", "lsw"}]
    assert offenders == [], (
        "the suite tried to mutate the host's tmux again: " + "; ".join(offenders))
