"""Tests for worker.actions."""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import pytest

from bot_squad_worker.actions import ACTION_REGISTRY, ActionError, dispatch
from bot_squad_worker.config import Config


def _make_tg_payload(bot_token: str, tg_id: int = 12345, **extra) -> dict:
    """Construct a TG Login Widget payload with valid HMAC."""
    base: dict = {
        "id": tg_id,
        "first_name": "Alexey",
        "username": "alexeysdk",
        "auth_date": int(time.time()),
        **extra,
    }
    secret = hashlib.sha256(bot_token.encode()).digest()
    data_check_string = "\n".join(f"{k}={base[k]}" for k in sorted(base.keys()))
    h = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {**base, "hash": h}


def test_noop_returns_ok():
    result = dispatch("noop", {})
    assert result["ok"] is True
    assert "ts" in result


def test_unknown_action_raises():
    with pytest.raises(ActionError) as excinfo:
        dispatch("rm_rf_root", {})
    assert "unknown action" in str(excinfo.value)


def test_registry_lists_only_allowed_actions():
    # Closed allowlist — spec #3 phase 3 adds deploy + kick_stuck_now.
    # spec #5 adds list_sessions, pause_session, resume_session, spawn_session.
    assert set(ACTION_REGISTRY.keys()) == {
        "noop", "tg_verify_login", "tg_notify", "deploy", "kick_stuck_now",
        "list_sessions", "pause_session", "resume_session", "spawn_session",
    }


def test_noop_rejects_extra_params():
    # Typed contract — noop takes no params; extras are an error.
    with pytest.raises(ActionError) as excinfo:
        dispatch("noop", {"unexpected": 1})
    assert "unexpected" in str(excinfo.value)


def test_tg_verify_login_dispatches(tmp_config_dir: Path, monkeypatch):
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    payload = _make_tg_payload("TESTBOT:TOKEN", tg_id=12345)
    out = A.dispatch("tg_verify_login", {"payload": payload})
    assert out["ok"] is True
    assert out["user"]["id"] == 12345


def test_tg_verify_login_rejects_empty_payload(tmp_config_dir: Path, monkeypatch):
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    out = A.dispatch("tg_verify_login", {"payload": {}})
    assert out["ok"] is False
    assert "error" in out


def test_tg_verify_login_rejects_extra_params(tmp_config_dir: Path, monkeypatch):
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError):
        A.dispatch("tg_verify_login", {"payload": {}, "extra": "bad"})


# ---------------------------------------------------------------------------
# tg_notify tests
# ---------------------------------------------------------------------------

class _FakeTgClient:
    """Records calls instead of hitting the real TG API."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._suppress = False  # when True, send() returns False (debounce sim)

    def send(self, *, chat_id, text, sid="", user="") -> bool:
        self.calls.append({"chat_id": chat_id, "text": text, "sid": sid, "user": user})
        return not self._suppress


def _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=None):
    """Common setup: load config, inject it + a fake TgClient."""
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    if fake_client is None:
        fake_client = _FakeTgClient()
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_client)
    return cfg, fake_client


def test_tg_notify_sends_message(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"message": "hello"})
    assert out == {"ok": True, "sent": True}
    assert len(fake.calls) == 1
    assert fake.calls[0]["text"] == "hello"


def test_tg_notify_missing_message_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="missing required param"):
        A.dispatch("tg_notify", {})


def test_tg_notify_rejects_extra_params(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("tg_notify", {"message": "hi", "evil_extra": "x"})


def test_tg_notify_resolves_slug(tmp_config_dir, monkeypatch):
    """slug lookup: test-project → tg_chat '0'."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"slug": "test-project", "message": "slug test"})
    assert out["ok"] is True
    assert fake.calls[0]["chat_id"] == "0"


def test_tg_notify_explicit_chat_id_wins(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"chat_id": "999", "slug": "test-project", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "999"


def test_tg_notify_unknown_slug_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("tg_notify", {"slug": "no-such-slug", "message": "hi"})


def test_tg_notify_debounce_returns_sent_false(tmp_config_dir, monkeypatch):
    """When TgClient.send returns False (debounced), tg_notify reports sent=False."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    fake._suppress = True
    out = A.dispatch("tg_notify", {"message": "same"})
    assert out == {"ok": True, "sent": False}


def test_tg_notify_sid_and_user_forwarded(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {"message": "hi", "sid": "S-x-p1", "user": "alexey"})
    call = fake.calls[0]
    assert call["sid"] == "S-x-p1"
    assert call["user"] == "alexey"


# ---------------------------------------------------------------------------
# deploy action tests
# ---------------------------------------------------------------------------


def _make_deploy_config(tmp_path: Path) -> "tuple[Config, Path]":
    """Create a config with a project that has a real repo dir for deploy tests."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        f'[projects.deploy-test]\n'
        f'slug = "deploy-test"\n'
        f'display_name = "Deploy Test"\n'
        f'repo_path = "{repo}"\n'
        f'deploy_branch = "agent_team/dev"\n'
        f'master_branch = "master"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return cfg, repo


def test_deploy_action_enqueues(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, repo = _make_deploy_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    out = A.dispatch("deploy", {
        "slug": "deploy-test",
        "target": "staging",
        "reason": "smoke test",
        "requested_by": "pytest",
    })
    assert out["ok"] is True
    assert "queue_id" in out
    assert "queued_at" in out


def test_deploy_action_rejects_bad_target(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, repo = _make_deploy_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="unknown target"):
        A.dispatch("deploy", {
            "slug": "deploy-test",
            "target": "prod",
            "reason": "bad",
            "requested_by": "pytest",
        })


def test_deploy_action_rejects_unknown_slug(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, repo = _make_deploy_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="unknown project"):
        A.dispatch("deploy", {
            "slug": "no-such-project",
            "target": "staging",
            "reason": "bad",
            "requested_by": "pytest",
        })


def test_deploy_action_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, repo = _make_deploy_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("deploy", {
            "slug": "deploy-test",
            "target": "staging",
            "reason": "r",
            "requested_by": "u",
            "evil": "extra",
        })


def test_deploy_action_requires_all_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, repo = _make_deploy_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("deploy", {"slug": "deploy-test", "target": "staging"})


# ---------------------------------------------------------------------------
# kick_stuck_now action tests
# ---------------------------------------------------------------------------


def test_kick_stuck_now_returns_ok(tmp_config_dir, monkeypatch):
    """kick_stuck_now with no params runs and returns {ok: True, ran: True}."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import jobs as J

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    # Patch kick_stuck so it doesn't actually try to send TG messages
    monkeypatch.setattr(J, "kick_stuck", lambda _cfg: None)

    out = A.dispatch("kick_stuck_now", {})
    assert out == {"ok": True, "ran": True}


def test_kick_stuck_now_rejects_params(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="takes no params"):
        A.dispatch("kick_stuck_now", {"extra": "bad"})


# ---------------------------------------------------------------------------
# Session actions tests (spec #5)
# ---------------------------------------------------------------------------


def _make_sessions_cfg(tmp_path: Path, monkeypatch):
    """Create a minimal config with test-project and inject it into actions."""
    import types
    import bot_squad_worker.actions as A

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        f'[projects.test-project]\n'
        f'slug = "test-project"\n'
        f'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        f'deploy_branch = "agent_team/dev"\n'
        f'master_branch = "master"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "sessions").mkdir(parents=True)
    (data_dir / "test-project" / "backlog").mkdir(parents=True)

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        tg_bot_token=cfg.tg_bot_token,
    )
    monkeypatch.setattr(A, "_get_config", lambda: patched)
    return patched, repo


def test_list_sessions_action_dispatches(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, repo = _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "_run", lambda args, **kw: __import__("subprocess").CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    result = A.dispatch("list_sessions", {"slug": "test-project"})
    assert isinstance(result, list)


def test_list_sessions_action_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("list_sessions", {"slug": "test-project", "evil": "extra"})


def test_list_sessions_action_requires_slug(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("list_sessions", {})


def test_list_sessions_action_rejects_unknown_slug(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "_run", lambda args, **kw: __import__("subprocess").CompletedProcess(args, 0, "", ""))

    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("list_sessions", {"slug": "no-such-project"})


def test_pause_session_action_rejects_missing_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("pause_session", {"slug": "test-project"})  # missing sid


def test_pause_session_action_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("pause_session", {"slug": "test-project", "sid": "S-x-p1", "evil": "x"})


def test_resume_session_action_rejects_missing_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("resume_session", {"slug": "test-project"})  # missing sid


def test_spawn_session_action_accepts_optional_prompt(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, repo = _make_sessions_cfg(tmp_path, monkeypatch)

    call_counts = {"list": 0}

    def fake_run(args, **kw):
        import subprocess as sp
        if "list-panes" in args:
            call_counts["list"] += 1
            if call_counts["list"] > 1:
                return sp.CompletedProcess(args, 0, f"%9|testwin|1111|{repo}|claude\n", "")
            return sp.CompletedProcess(args, 0, "", "")
        return sp.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = A.dispatch("spawn_session", {
        "slug": "test-project",
        "window": "testwin",
        "initial_prompt": "hello from test",
    })
    assert result["ok"] is True
    assert "sid" in result


def test_spawn_session_action_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("spawn_session", {"slug": "test-project", "window": "w", "evil": "x"})
