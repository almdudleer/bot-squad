"""T-0574: the suite must NEVER reach the real user systemd.

Deploy/apply tests execute GENERATED scripts end-to-end (run_next launches
recipes, _restart_worker_detached / _schedule_worker_restart fire detached
``systemctl --user restart bot-squad-worker`` children). Those subprocesses
resolve ``systemctl`` and ``systemd-run`` via PATH — Python-level
monkeypatching cannot intercept them. conftest.py prepends
``worker/tests/fake_bin`` (committed recording shims) to PATH for the whole
suite; these tests pin that guarantee so its removal fails loudly instead of
silently restarting the LIVE worker service again.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FAKE_BIN = Path(__file__).resolve().parent / "fake_bin"
WORKER_PKG = Path(__file__).resolve().parents[1] / "bot_squad_worker"


def test_fake_bin_is_first_on_path() -> None:
    assert os.environ["PATH"].split(os.pathsep)[0] == str(FAKE_BIN)


def test_systemd_binaries_resolve_to_fakes() -> None:
    for name in ("systemctl", "systemd-run"):
        resolved = shutil.which(name)
        assert resolved is not None
        assert Path(resolved).parent == FAKE_BIN, (
            f"{name} resolves to {resolved} — a test subprocess would reach "
            f"the REAL user systemd"
        )


def test_generated_scripts_resolve_fakes_through_bash() -> None:
    # Generated deploy/apply scripts run under bash and resolve via the
    # inherited PATH — assert that resolution path, not just shutil.which.
    out = subprocess.run(
        ["bash", "-c", "command -v systemctl && command -v systemd-run"],
        capture_output=True, text=True, check=True,
    )
    lines = out.stdout.strip().splitlines()
    assert [str(Path(p).parent) for p in lines] == [str(FAKE_BIN)] * 2


def test_fake_systemctl_records_and_swallows_restart(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    env = {**os.environ, "BOT_SQUAD_TEST_FAKE_SYSTEMCTL_LOG": str(log)}
    proc = subprocess.run(
        ["bash", "-c", "systemctl --user restart bot-squad-worker.service"],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "systemctl --user restart bot-squad-worker.service" in log.read_text()


def test_fake_systemd_run_execs_wrapped_command(tmp_path: Path) -> None:
    # Production wraps children as `systemd-run --user --scope ... -- <argv>`
    # and relies on --scope exec semantics (proc.pid IS the child). The fake
    # must exec the wrapped argv so scope-path executor tests stay real.
    log = tmp_path / "calls.log"
    env = {**os.environ, "BOT_SQUAD_TEST_FAKE_SYSTEMCTL_LOG": str(log)}
    proc = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet",
         "--unit=t-0574-guard", "--collect", "--", "echo", "hermetic"],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "hermetic"
    assert "systemd-run" in log.read_text()


def test_no_absolute_path_systemd_invocations_in_worker_source() -> None:
    # An absolute-path invocation would bypass the PATH shim entirely.
    offenders = []
    for py in WORKER_PKG.rglob("*.py"):
        text = py.read_text()
        for needle in ("/usr/bin/systemctl", "/bin/systemctl",
                       "/usr/bin/systemd-run", "/bin/systemd-run"):
            if needle in text:
                offenders.append(f"{py.name}: {needle}")
    assert offenders == [], offenders
