"""Per-worker-user provider defaults stay isolated on a shared install."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_squad_worker import fleet_model


@pytest.fixture(autouse=True)
def _worker_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(fleet_model._sessions, "_get_user_home", lambda: str(home))
    return home


def _config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    return path


def test_per_user_provider_overrides_global_without_changing_it(tmp_path):
    config_dir = _config_dir(tmp_path)

    fleet_model.set_model("opus", config_dir)
    fleet_model.set_model("codex", config_dir, linux_user="flomaster")

    assert fleet_model.get_provider(config_dir) == "claude"
    assert fleet_model.get_provider(config_dir, linux_user="flomaster") == "codex"
    assert fleet_model.get_model(config_dir, linux_user="flomaster") == "codex"
    assert fleet_model.get_provider(config_dir, linux_user="another-user") == "claude"

    global_state = json.loads(
        (tmp_path / "data" / "_state" / "agent_provider.json").read_text()
    )
    user_state = json.loads(
        (
            tmp_path
            / "data"
            / "_state"
            / "agent_providers"
            / "flomaster.json"
        ).read_text()
    )
    assert global_state == {"provider": "claude"}
    assert user_state == {"provider": "codex"}


def test_missing_user_state_falls_back_to_legacy_global(tmp_path):
    config_dir = _config_dir(tmp_path)
    fleet_model.set_model("codex", config_dir)

    assert fleet_model.get_provider(config_dir, linux_user="flomaster") == "codex"


@pytest.mark.parametrize("linux_user", ["../other", "a/b", "a\\b", ".", ".."])
def test_per_user_provider_rejects_unsafe_linux_user(tmp_path, linux_user):
    config_dir = _config_dir(tmp_path)

    with pytest.raises(ValueError, match="invalid linux_user"):
        fleet_model.set_model("codex", config_dir, linux_user=linux_user)


def _spawn_capturing_launch_cmd(tmp_path, monkeypatch, worker_user: str) -> str:
    """Run `sessions.spawn` with the tmux seam stubbed, as `worker_user`.

    Deliberately a LOCAL harness rather than importing test_sessions'
    equivalent: that one stubs `_get_current_user` to a fixed "u", and the
    account spawn resolves the provider for is precisely what this file is
    testing — a test must own the stub whose value it is asserting about. It
    also keeps this file independent of test_sessions.py, which another ticket
    is actively editing.
    """
    import subprocess

    from bot_squad_worker.config import Config
    import bot_squad_worker.sessions as S

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        f'[projects.test-project]\n'
        f'slug = "test-project"\n'
        f'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        f'deploy_branch = "bot_squad/dev"\n'
        f'master_branch = "master"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    loaded = Config.load(cfg_dir)
    import types
    cfg = types.SimpleNamespace(
        projects=loaded.projects, data_dir=data_dir,
        tg_bot_token=loaded.tg_bot_token,
    )

    captured: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            try:
                captured.append(args[args.index("-lc") + 1])
            except (ValueError, IndexError):
                pass
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%6|w|123|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: worker_user)
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda _x: None)
    monkeypatch.setattr(S, "_wait_for_agent_composer_ready", lambda *_a: True)
    monkeypatch.setattr(S, "_deliver_prompt", lambda *a, **k: None)

    S.spawn(cfg, "test-project", "w")
    assert captured, "expected a new-window call"
    return captured[0]


def test_spawn_resolves_the_provider_for_its_OWN_worker_user(tmp_path, monkeypatch):
    """The consumer, not just the store.

    `get_provider(..., linux_user=...)` is inert unless something passes the
    argument, and exactly one caller does: `sessions.spawn`, with
    `user = _get_current_user()` — the OS account the WORKER PROCESS runs as.
    That is the whole point on a shared install: the coordinator resolves its
    own default, flomaster's per-user worker resolves flomaster's, and one
    project can run Codex without the fleet switching to it. (Which is the
    still-unmet half of T-0883: «эта сессия должна быть codex».)

    Asserted through the launch COMMAND rather than by watching for a kwarg,
    because the claim is "the spawned agent is Codex". A mutation dropping
    `linux_user=user` leaves every other spawn test in the suite green.
    """
    import bot_squad_worker.sessions as S

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)

    # Fleet default stays Claude; only THIS worker's account opts into Codex.
    fleet_model.set_model("", config_dir)
    fleet_model.set_model("codex", config_dir, linux_user="flomaster")
    assert fleet_model.get_provider(config_dir) == "claude"

    cmd = _spawn_capturing_launch_cmd(tmp_path, monkeypatch, "flomaster")

    assert cmd.startswith("codex --dangerously-bypass-approvals-and-sandbox"), cmd


def test_a_different_worker_user_still_gets_the_fleet_default(tmp_path, monkeypatch):
    """The other direction, so the test above cannot pass by always-Codex."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    fleet_model.set_model("", config_dir)
    fleet_model.set_model("codex", config_dir, linux_user="flomaster")

    cmd = _spawn_capturing_launch_cmd(tmp_path, monkeypatch, "almdudleer")

    assert not cmd.startswith("codex "), cmd
