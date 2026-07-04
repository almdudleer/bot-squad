"""T-0324 H1: the SessionStart hook must not bind via the shared-cwd marker,
and a constant-team session must never acquire a primary via ANY channel.

Incident: a stale ``.claude/task_id`` marker in the SHARED working tree was
read by an unrelated session's SessionStart hook, cross-wiring a task's
PRIMARY binding onto a constant-team session (p179→p181). T-0525 retired
every WRITER of the marker (spawn/resume use the per-process BOT_SQUAD_TASK_ID
env), but the hook still READ it — so any residue (pre-fix deploy, external
claude) could still poison the next env-less session in that cwd.

Closures under test, executed against the REAL hook script with a fake tmux
on PATH and a throwaway $BOT_SQUAD (hermetic — never touches the live
registry; cf the session_start-hook-clobbers-live-md lesson):
  * the marker read-tier is GONE: residue is ignored AND deleted;
  * the env channel (BOT_SQUAD_TASK_ID) still binds;
  * owner=constant-team drops the task_id whether it arrives via env or is
    persisted in the session md (resumed sessions have no env vars);
  * a previously cross-wired constant-team primary self-heals on next fire;
  * a dev session's existing primary is still preserved.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


def _find_hook(name: str) -> Path | None:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        cand = ancestor / "scripts" / "hooks" / name
        if cand.is_file():
            return cand
    return None


FAKE_TMUX = """#!/usr/bin/env bash
# Hermetic tmux stand-in for session_start.sh tests.
# Window name comes from $FAKE_WINDOW so tests can vary it.
args="$*"
case "$args" in
  *"#W"*)            echo "${FAKE_WINDOW:-somework}" ;;
  *"#{pane_id}"*)    echo "%7" ;;
  *"#{pane_pid}"*)   echo "99999" ;;
  *"#{window_id}"*)  echo "@1" ;;
  *"#S"*)            echo "fake-tmux-session" ;;
  *list-panes*)      echo "onepane" ;;   # cnt=1 -> break-pane block no-ops
  *)                 exit 0 ;;
esac
"""


@pytest.fixture()
def hook_env(tmp_path):
    """Throwaway $BOT_SQUAD + repo + fake tmux; returns (run_hook, repo, sessions_dir)."""
    real_hook = _find_hook("session_start.sh")
    derive = _find_hook("derive_role.sh")
    my_sid = _find_hook("hook_my_sid.sh")
    if not (real_hook and derive and my_sid):
        pytest.skip("hook scripts not reachable from test cwd")

    bs = tmp_path / "bs"
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (bs / "scripts" / "hooks").mkdir(parents=True)
    (bs / "config").mkdir(parents=True)
    os.symlink(derive, bs / "scripts" / "hooks" / "derive_role.sh")
    os.symlink(my_sid, bs / "scripts" / "hooks" / "hook_my_sid.sh")
    (bs / "config" / "projects.toml").write_text(
        '[projects.test-project]\nrepo_path = "%s"\n' % repo)
    sessions_dir = bs / "data" / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True)
    (bs / "data" / "test-project" / "vision").mkdir(parents=True)

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(FAKE_TMUX)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    def run_hook(*, window="somework", extra_env=None):
        env = dict(os.environ)
        env["BOT_SQUAD"] = str(bs)
        env["PATH"] = f"{shim_dir}:{env['PATH']}"
        env["TMUX"] = "/tmp/fake-tmux-sock,123,0"
        env["TMUX_PANE"] = "%7"
        env["FAKE_WINDOW"] = window
        for k in ("BOT_SQUAD_TASK_ID", "BOT_SQUAD_INITIATIVE",
                  "BOT_SQUAD_OWNER", "BOT_SQUAD_OWNER_USER"):
            env.pop(k, None)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(real_hook)],
            input='{"session_id":"11111111-2222-3333-4444-555555555555",'
                  '"source":"startup"}',
            capture_output=True, text=True, cwd=str(repo), env=env, timeout=30,
        )

    return run_hook, repo, sessions_dir


def _user() -> str:
    import getpass
    u = getpass.getuser()
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in u)


def _md(sessions_dir: Path, window: str) -> Path:
    return sessions_dir / f"S-{_user()}-{window}-p7.md"


def _fm(md_path: Path) -> dict:
    text = md_path.read_text()
    fm = text.split("---", 2)[1]
    out = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def test_stale_marker_is_ignored_and_deleted(hook_env):
    """The marker read-tier is retired: residue must not bind, and must be
    cleaned up so no later flow can be poisoned either."""
    run_hook, repo, sessions_dir = hook_env
    marker = repo / ".claude" / "task_id"
    marker.write_text("T-9999\n")

    res = run_hook(window="somework")
    assert res.returncode == 0, res.stderr

    md = _md(sessions_dir, "somework")
    assert md.exists(), (res.stdout, res.stderr)
    assert _fm(md).get("task_id") == "~", _fm(md)
    assert not marker.exists(), "stale marker residue must be deleted"


def test_env_channel_still_binds(hook_env):
    run_hook, repo, sessions_dir = hook_env
    res = run_hook(window="somework", extra_env={"BOT_SQUAD_TASK_ID": "T-1234"})
    assert res.returncode == 0, res.stderr
    assert _fm(_md(sessions_dir, "somework")).get("task_id") == "T-1234"


def test_window_name_tier_still_binds_for_dev(hook_env):
    run_hook, repo, sessions_dir = hook_env
    res = run_hook(window="T-0123-fix-things")
    assert res.returncode == 0, res.stderr
    assert _fm(_md(sessions_dir, "T-0123-fix-things")).get("task_id") == "T-0123"


def test_env_constant_team_drops_env_task(hook_env):
    """Regression pin: the pre-existing env-level guard."""
    run_hook, repo, sessions_dir = hook_env
    res = run_hook(window="user-feedback", extra_env={
        "BOT_SQUAD_TASK_ID": "T-0200", "BOT_SQUAD_OWNER": "constant-team"})
    assert res.returncode == 0, res.stderr
    assert _fm(_md(sessions_dir, "user-feedback")).get("task_id") == "~"


def test_md_owner_constant_team_drops_window_task(hook_env):
    """A RESUMED constant-team session carries no env vars — the md's
    persisted ``owner: constant-team`` must trigger the same drop for a
    window-derived task_id."""
    run_hook, repo, sessions_dir = hook_env
    md = _md(sessions_dir, "T-0123-triage")
    md.write_text(
        "---\n"
        f"sid: S-{_user()}-T-0123-triage-p7\n"
        "status: active\n"
        "window: T-0123-triage\n"
        f"cwd: {repo}\n"
        "claude_uuid: old-uuid\n"
        "task_id: ~\n"
        "initiative: ~\n"
        "extra_task_ids: []\n"
        "extra_initiatives: []\n"
        "started_at: 2026-07-01T00:00:00Z\n"
        "owner: constant-team\n"
        "owner_user: ~\n"
        "tmux_session: ~\n"
        "linux_user: ~\n"
        "---\n"
    )
    res = run_hook(window="T-0123-triage")
    assert res.returncode == 0, res.stderr
    assert _fm(md).get("task_id") == "~", _fm(md)


def test_md_owner_constant_team_self_heals_crosswired_primary(hook_env):
    """A primary that already cross-wired onto a constant-team session md
    (the p181 state) is stripped on the next hook fire instead of being
    preserved forever by the existing-wins fallback."""
    run_hook, repo, sessions_dir = hook_env
    md = _md(sessions_dir, "user-feedback")
    md.write_text(
        "---\n"
        f"sid: S-{_user()}-user-feedback-p7\n"
        "status: active\n"
        "window: user-feedback\n"
        f"cwd: {repo}\n"
        "claude_uuid: old-uuid\n"
        "task_id: T-0253\n"
        "initiative: ~\n"
        "extra_task_ids: []\n"
        "extra_initiatives: []\n"
        "started_at: 2026-07-01T00:00:00Z\n"
        "owner: constant-team\n"
        "owner_user: ~\n"
        "tmux_session: ~\n"
        "linux_user: ~\n"
        "---\n"
    )
    res = run_hook(window="user-feedback")
    assert res.returncode == 0, res.stderr
    assert _fm(md).get("task_id") == "~", _fm(md)


def test_dev_md_primary_still_preserved(hook_env):
    """The existing-wins preservation for normal dev sessions must survive
    the guard change."""
    run_hook, repo, sessions_dir = hook_env
    md = _md(sessions_dir, "somework")
    md.write_text(
        "---\n"
        f"sid: S-{_user()}-somework-p7\n"
        "status: active\n"
        "window: somework\n"
        f"cwd: {repo}\n"
        "claude_uuid: old-uuid\n"
        "task_id: T-0500\n"
        "initiative: ~\n"
        "extra_task_ids: []\n"
        "extra_initiatives: []\n"
        "started_at: 2026-07-01T00:00:00Z\n"
        "owner: alexey\n"
        "owner_user: ~\n"
        "tmux_session: ~\n"
        "linux_user: ~\n"
        "---\n"
    )
    res = run_hook(window="somework")
    assert res.returncode == 0, res.stderr
    assert _fm(md).get("task_id") == "T-0500", _fm(md)
