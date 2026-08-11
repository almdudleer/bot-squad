"""T-0871 (T-0866 worker lane): every claude spawn/resume states its own
``--model`` AND ``--effort``, and the effort can never overshoot the ceiling.

The two holes this pins shut, both of which handed the CHOICE to the vendor:

- ``spawn()`` dropped ``--model`` entirely for any role with no ``[models]``
  entry and no built-in (``qa``, ``prod-teamlead``, a plain dev window), so the
  spawned ``claude`` read the linux user's ``~/.claude/settings.json``;
- nothing anywhere passed ``--effort``, so every turn ran at the CLI's own
  per-model ``default_effort``. That table is vendor-owned and per-model
  (v2.1.227 ships ``claude-opus-4-7 -> xhigh``), so a model swap or a CLI
  update could raise the whole fleet a tier with no change on our side and no
  signal. ``EFFORT_CEILING`` is the guard against exactly that.

The failure mode for most of these is an ABSENT flag, not a wrong one — so
they assert PRESENCE of a concrete value on the assembled shell command,
never just a return code.
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker import agent_provider, fleet_model
from bot_squad_worker.sessions import (
    resume,
    spawn,
    _write_session_metadata,
)


def _make_cfg(tmp_path: Path, repo_path: Path) -> Any:
    """Minimal Config-like object — deliberately a local copy rather than an
    import from test_sessions, so a peer's edits there cannot move this
    file's ground."""
    from bot_squad_worker.config import Config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)

    (cfg_dir / "projects.toml").write_text(
        "[projects.test-project]\n"
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{repo_path}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        "created_at = 2026-05-10\n"
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        config_dir=cfg_dir,
        tg_bot_token=cfg.tg_bot_token,
    )


def _settings(cfg: Any, body: str) -> None:
    """Write a system_settings.toml the worker will fresh-read at spawn."""
    (Path(cfg.config_dir) / "system_settings.toml").write_text(body)


def _stub_tmux(monkeypatch, repo: Path, window: str, captured: list[str]):
    def fake_run(args, **kwargs):
        if "new-window" in args:
            try:
                captured.append(args[args.index("-lc") + 1])
            except (ValueError, IndexError):
                pass
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%6|{window}|123|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(repo.parent))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    monkeypatch.setattr(S, "_wait_for_agent_composer_ready", lambda *_: True)
    monkeypatch.setattr(S, "_deliver_prompt", lambda *a, **k: None)


def _spawn_cmd(tmp_path, monkeypatch, window: str, settings: str = "",
               **spawn_kw) -> str:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    if settings:
        _settings(cfg, settings)
    captured: list[str] = []
    _stub_tmux(monkeypatch, repo, window, captured)
    spawn(cfg, "test-project", window, **spawn_kw)
    assert captured, "expected a tmux new-window call"
    return captured[0]


def _resume_cmd(tmp_path, monkeypatch, meta: dict, settings: str = "") -> str:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    if settings:
        _settings(cfg, settings)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    base = {
        "sid": "S-alice-w-p2", "status": "suspended", "window": "w",
        "cwd": str(repo), "claude_uuid": "u-1", "task_id": "T-0001",
    }
    base.update(meta)
    _write_session_metadata(sessions_dir / f"{base['sid']}.md", base)
    captured: list[str] = []
    _stub_tmux(monkeypatch, repo, base["window"], captured)
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_get_current_user", lambda: "alice")
    resume(cfg, "test-project", base["sid"])
    assert captured, "expected a tmux new-window call"
    return captured[0]


def _session_md(tmp_path) -> dict:
    from bot_squad_worker.sessions import _read_session_metadata

    sessions = list((tmp_path / "data" / "test-project" / "sessions").glob("*.md"))
    assert sessions, "expected a session md"
    return _read_session_metadata(sessions[0]) or {}


# ---------------------------------------------------------------------------
# fleet_model.resolve_effort — the ceiling itself
# ---------------------------------------------------------------------------

def test_effort_levels_are_ordered_low_to_max():
    assert fleet_model.EFFORT_LEVELS == (
        "low", "medium", "high", "xhigh", "max")


def test_effort_ceiling_is_a_recognized_level():
    assert fleet_model.EFFORT_CEILING in fleet_model.EFFORT_LEVELS


@pytest.mark.parametrize("value", ["low", "medium", "high"])
def test_resolve_effort_passes_through_at_or_below_ceiling(value):
    assert fleet_model.resolve_effort(value) == value


@pytest.mark.parametrize("value", ["xhigh", "max"])
def test_resolve_effort_clamps_above_ceiling(value):
    """The anti-overshoot guard: a level ABOVE the ceiling is clamped DOWN to
    it, never passed through. Raising the fleet past the ceiling has to be a
    deliberate one-line edit to EFFORT_CEILING — not something a config typo
    or a vendor default can do."""
    assert fleet_model.resolve_effort(value) == fleet_model.EFFORT_CEILING


def test_resolve_effort_empty_passes_through():
    assert fleet_model.resolve_effort("") == ""
    assert fleet_model.resolve_effort("   ") == ""


def test_resolve_effort_rejects_unrecognized_value():
    """Loud, like resolve_model — a typo must not silently become "no flag",
    which is precisely the vendor-default fallback this lane closes."""
    with pytest.raises(ValueError, match="effort"):
        fleet_model.resolve_effort("ultra")


# ---------------------------------------------------------------------------
# agent_provider — the flag on the command
# ---------------------------------------------------------------------------

def test_claude_launch_command_emits_effort():
    cmd = agent_provider.get("claude").launch_command(model="opus", effort="high")
    assert "--effort high" in cmd


def test_claude_launch_command_omits_effort_when_blank():
    cmd = agent_provider.get("claude").launch_command(model="opus")
    assert "--effort" not in cmd


def test_codex_launch_command_ignores_effort():
    """Codex has no such flag; it accepts and drops the kwarg, same shape as
    its existing `del display_name`."""
    cmd = agent_provider.get("codex").launch_command(
        model="gpt-5.6-terra", effort="high")
    assert "--effort" not in cmd
    assert cmd.startswith("codex ")


# ---------------------------------------------------------------------------
# spawn() — never an implicit model, never an implicit effort
# ---------------------------------------------------------------------------

def test_spawn_role_without_any_model_default_still_names_a_model(
        tmp_path, monkeypatch):
    """The B1 hole: a plain dev window has no [models] entry and no built-in
    ROLE entry, and used to get NO --model flag at all — handing the choice to
    ~/.claude/settings.json. The catch-all must put a concrete value on the
    command."""
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w")
    assert "--model opus" in cmd


def test_the_qa_window_marker_really_derives_the_qa_role():
    """Instrument check for the three tests below — they are only about an
    UNCONFIGURED role if the window actually derives one. `_QA_WINDOW_RE`
    anchors at the END of the window, so "qa-sweep" would silently be a dev."""
    from bot_squad_worker.sessions import _derive_role

    assert _derive_role("sweep-qa", None, None) == "qa"
    assert _derive_role("qa-sweep", None, None) == "dev"


def test_spawn_unconfigured_qa_role_still_names_a_model(tmp_path, monkeypatch):
    """qa has no entry in the live [models] and no built-in either — the
    exact role that used to reach `claude` with no --model at all."""
    cmd = _spawn_cmd(tmp_path, monkeypatch, "sweep-qa",
                     settings='[models]\ndev = "opus"\n')
    assert "--model opus" in cmd


def test_spawn_always_emits_an_effort(tmp_path, monkeypatch):
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w")
    assert "--effort high" in cmd


def test_spawn_configured_effort_is_clamped_to_the_ceiling(tmp_path, monkeypatch):
    """[effort] configured to "max" for a role -> the ceiling reaches the
    command, exercising the clamp rather than merely coding it."""
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w",
                     settings='[effort]\ndev = "max"\n')
    assert f"--effort {fleet_model.EFFORT_CEILING}" in cmd
    assert "--effort max" not in cmd


def test_spawn_explicit_effort_override_is_clamped_identically(
        tmp_path, monkeypatch):
    """The ceiling is not bypassable by an override — an explicit spawn arg
    goes through the same resolve_effort()."""
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w", effort="max")
    assert f"--effort {fleet_model.EFFORT_CEILING}" in cmd
    assert "--effort max" not in cmd


def test_spawn_explicit_effort_below_ceiling_is_honored(tmp_path, monkeypatch):
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w", effort="low")
    assert "--effort low" in cmd


def test_spawn_configured_effort_below_ceiling_is_honored(tmp_path, monkeypatch):
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w",
                     settings='[effort]\ndev = "medium"\n')
    assert "--effort medium" in cmd


def test_spawn_effort_catch_all_applies_to_unconfigured_roles(
        tmp_path, monkeypatch):
    cmd = _spawn_cmd(tmp_path, monkeypatch, "sweep-qa",
                     settings='[effort]\n"*" = "low"\n')
    assert "--effort low" in cmd


def test_spawn_model_catch_all_applies_to_unconfigured_roles(
        tmp_path, monkeypatch):
    cmd = _spawn_cmd(tmp_path, monkeypatch, "sweep-qa",
                     settings='[models]\ndev = "opus"\n"*" = "sonnet"\n')
    assert "--model sonnet" in cmd


def test_spawn_rejects_unrecognized_effort(tmp_path, monkeypatch):
    from bot_squad_worker.actions import ActionError

    with pytest.raises(ActionError, match="effort"):
        _spawn_cmd(tmp_path, monkeypatch, "w", effort="ultra")


def test_spawn_stamps_effort_and_model_source_on_the_session_md(
        tmp_path, monkeypatch):
    """"logged concrete value, not None/unset" — the md has to name what was
    chosen AND where it came from, so an audit can tell a bot-squad choice
    from a vendor fallback."""
    _spawn_cmd(tmp_path, monkeypatch, "w")
    meta = _session_md(tmp_path)
    assert meta.get("effort") == "high"
    assert meta.get("model_source") == "builtin:*"


def test_spawn_model_source_names_the_config_role(tmp_path, monkeypatch):
    _spawn_cmd(tmp_path, monkeypatch, "w", settings='[models]\ndev = "opus"\n')
    meta = _session_md(tmp_path)
    assert meta.get("model_source") == "config:dev"


def test_spawn_logs_the_resolved_model_and_effort(tmp_path, monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="bot_squad_worker.sessions"):
        _spawn_cmd(tmp_path, monkeypatch, "w")
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "opus" in line and "high" in line
    assert "None" not in line


def test_codex_spawn_carries_no_effort_flag(tmp_path, monkeypatch):
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w", model="terra")
    assert cmd.startswith("codex ")
    assert "--effort" not in cmd


# ---------------------------------------------------------------------------
# resume() — a recycle must not silently revert to the vendor default
# ---------------------------------------------------------------------------

def test_resume_without_stamped_model_re_resolves_the_role_default(
        tmp_path, monkeypatch):
    """The B3 hole: `model` is stamped only for an EXPLICIT spawn choice
    (deliberate, T-0678 — a role-defaulted session should follow the role
    default at resume time, not freeze today's value). But resume() then
    passed "" — i.e. NO --model at all — so the recycle dropped to
    settings.json. It must re-resolve the role default instead."""
    cmd = _resume_cmd(tmp_path, monkeypatch, {},
                      settings='[models]\ndev = "sonnet"\n')
    assert "--model sonnet" in cmd


def test_resume_keeps_a_sticky_explicit_model(tmp_path, monkeypatch):
    """T-0678's sticky per-session override is unchanged by the re-resolve."""
    cmd = _resume_cmd(tmp_path, monkeypatch, {"model": "claude-opus-4-8"},
                      settings='[models]\ndev = "sonnet"\n')
    assert "--model claude-opus-4-8" in cmd


def test_resume_carries_effort_forward(tmp_path, monkeypatch):
    cmd = _resume_cmd(tmp_path, monkeypatch, {"effort": "medium"})
    assert "--effort medium" in cmd


def test_resume_without_stamped_effort_still_emits_one(tmp_path, monkeypatch):
    cmd = _resume_cmd(tmp_path, monkeypatch, {})
    assert "--effort high" in cmd


def test_resume_clamps_a_stale_overshooting_stamp(tmp_path, monkeypatch):
    """A session md stamped before the ceiling existed (or edited by hand)
    must not be able to reintroduce the overshoot on a recycle."""
    cmd = _resume_cmd(tmp_path, monkeypatch, {"effort": "max"})
    assert f"--effort {fleet_model.EFFORT_CEILING}" in cmd
    assert "--effort max" not in cmd
