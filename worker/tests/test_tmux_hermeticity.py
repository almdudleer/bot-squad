"""T-0802: the suite must NEVER mutate the host's tmux.

A full worker-suite run created a REAL tmux session named for the fixture slug
(`test-project`) holding a REAL `claude` window (`gu_1-user-conversation`) —
one per run, never reaped, until 70 live claude processes had consumed the
host's RAM and all 8 GB of swap. Two devs' suites were lost to the resulting
thrashing and an operator spent an hour attributing it to his own dispatching.

conftest.py prepends ``worker/tests/fake_bin`` to PATH for the whole suite; the
committed ``fake_bin/tmux`` there passes read-only verbs through to the real
tmux and refuses everything else. These tests pin that guarantee so its removal
fails loudly instead of silently leaking claude processes onto the host again.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

FAKE_BIN = Path(__file__).resolve().parent / "fake_bin"
WORKER_PKG = Path(__file__).resolve().parents[1] / "bot_squad_worker"


def _real_tmux() -> str | None:
    """The real tmux, resolved the way the shim itself resolves it."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or Path(d) == FAKE_BIN:
            continue
        cand = Path(d) / "tmux"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def test_tmux_resolves_to_the_fake() -> None:
    resolved = shutil.which("tmux")
    assert resolved is not None
    assert Path(resolved).parent == FAKE_BIN, (
        f"tmux resolves to {resolved} — a test subprocess could mutate the "
        f"host's REAL tmux server"
    )


def test_bash_resolves_the_fake_too() -> None:
    # Generated scripts and scripts/hooks/session_start.sh reach tmux through
    # bash and the inherited PATH — assert that resolution path, not just
    # shutil.which.
    out = subprocess.run(["bash", "-c", "command -v tmux"],
                         capture_output=True, text=True, check=True)
    assert Path(out.stdout.strip()).parent == FAKE_BIN


def test_new_session_is_refused_and_creates_nothing() -> None:
    """The exact call that leaked, with a unique name so the assertion is about
    THIS invocation and cannot pass on a stale absence.

    The ``finally`` is not tidiness: when the seal is absent this call SUCCEEDS
    against the real binary, and a guard test must not leave behind the very
    thing it exists to prevent (see test_unknown_verbs_fail_closed for the
    general rule).
    """
    name = f"t0802-guard-{uuid.uuid4().hex[:8]}"
    real = _real_tmux()
    try:
        proc = subprocess.run(["tmux", "new-session", "-d", "-s", name],
                              capture_output=True, text=True)
        assert proc.returncode != 0
        assert "T-0802" in proc.stderr, proc.stderr

        if real is None:      # CI without tmux — nothing could have been created
            return
        check = subprocess.run([real, "has-session", "-t", name],
                               capture_output=True, text=True)
        assert check.returncode != 0, (
            f"session {name} EXISTS on the host — the shim let a mutation through")
    finally:
        if real is not None:
            subprocess.run([real, "kill-session", "-t", name],
                           capture_output=True, text=True)


def test_spawn_style_new_window_is_refused() -> None:
    """The second half of the leaked pair: the window that carried `claude`.

    Same shape as the leaked call — detached, named for a user conversation,
    carrying a `bash -lc` payload — but aimed at a session name minted here, so
    that an unsealed run can only produce "can't find session" instead of
    attaching a window to whatever real session the leak happens to have left
    lying around.
    """
    proc = subprocess.run(
        ["tmux", "new-window", "-d", "-t",
         f"t0802-absent-{uuid.uuid4().hex[:8]}:", "-n",
         "gu_1-user-conversation", "-c", "/tmp", "bash", "-lc", "true"],
        capture_output=True, text=True)
    assert proc.returncode != 0
    assert "T-0802" in proc.stderr, proc.stderr


def test_unknown_verbs_fail_closed() -> None:
    """The shim allowlists reads rather than blocklisting mutations: tmux has
    far too many mutating verbs for a blocklist to stay complete. Pinned with a
    verb that IS a mutation and that no worker code calls today, so a future
    rewrite into a blocklist fails here.

    THE VERB IS CHOSEN TO BE HARMLESS WHEN THE SEAL IS ABSENT, and that is not
    a detail. A guard test runs precisely in the state it is guarding against:
    if ``fake_bin/tmux`` is missing, or PATH never got prepended, every command
    in this file reaches the REAL tmux binary. This test was first written
    against ``kill-server``, which in that state does not fail — it succeeds,
    destroying every session on the host (including the sessions of whoever is
    running the suite) and only THEN going red on ``returncode != 0``. Measured
    the hard way during this ticket's own guard-mutation run, where the shim was
    deleted on purpose.

    ``kill-session`` against a freshly-minted name is the same shape of verb —
    a mutation, unknown to the allowlist — but its worst case against the real
    binary is "can't find session", which is exactly the red we want and
    nothing else. Never pin this file with a verb whose SUCCESS is destructive.
    """
    absent = f"t0802-never-created-{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(["tmux", "kill-session", "-t", absent],
                          capture_output=True, text=True)
    assert proc.returncode != 0
    assert "T-0802" in proc.stderr, (
        "an unknown verb was not refused by the shim — if the shim has been "
        f"rewritten as a blocklist, mutations it has not heard of get through: "
        f"{proc.stderr!r}")


def test_read_only_verbs_still_work() -> None:
    """The seal must not change what reads return, or it would break the tests
    that legitimately query tmux state."""
    real = _real_tmux()
    if real is None:
        return
    via_shim = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                              capture_output=True, text=True)
    direct = subprocess.run([real, "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True)
    assert via_shim.returncode == direct.returncode
    assert via_shim.stdout == direct.stdout


def test_every_invocation_is_logged_so_a_run_can_be_audited() -> None:
    """The shim records EVERY call, refused or not, to
    ``$BOT_SQUAD_TEST_FAKE_TMUX_LOG``. That log is not a debugging nicety, it is
    the instrument — and it has already earned its place.

    A refusal protects the host but does not always redden the test that caused
    it: ``test_sessions.py::test_fleet_codex_default_rejects_accidental_sonnet_override``
    called ``spawn()`` directly, ``spawn`` ignores the tmux return code and went
    on to raise the ``ActionError`` the test was asserting, so the test passed
    either way. It was creating a real ``test-project`` session on every suite
    run and nothing said so. Grepping this log after a full run is what found
    it — the root-cause analysis had not.
    """
    log = os.environ.get("BOT_SQUAD_TEST_FAKE_TMUX_LOG", "")
    assert log, "conftest no longer sets BOT_SQUAD_TEST_FAKE_TMUX_LOG"

    marker = f"t0802-audit-{uuid.uuid4().hex[:8]}"
    before = Path(log).read_text(encoding="utf-8") if Path(log).exists() else ""
    subprocess.run(["tmux", "kill-session", "-t", marker],
                   capture_output=True, text=True)
    added = Path(log).read_text(encoding="utf-8")[len(before):]
    assert marker in added, (
        "a refused tmux call left no trace — a suite run can no longer be "
        f"audited for what it TRIED to do. log tail: {added!r}")


def test_worker_source_has_no_absolute_path_tmux_invocations() -> None:
    """An absolute-path invocation would bypass the PATH shim entirely — the
    same gate test_suite_hermeticity.py keeps over systemctl."""
    offenders = []
    for py in WORKER_PKG.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for needle in ("/usr/bin/tmux", "/bin/tmux", "/usr/local/bin/tmux"):
            if needle in text:
                offenders.append(f"{py.name}: {needle}")
    assert offenders == [], offenders
