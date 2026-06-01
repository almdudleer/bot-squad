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
    # Closed allowlist — spec #3 phase 3 adds deploy.
    # spec #5 adds list_sessions, pause_session, resume_session, spawn_session.
    # spec #6 adds scheduler_state.
    # spec #7 adds inject_input.
    # spec #8 adds autonomous_status, autonomous_enable, autonomous_disable.
    # Phase 1 message bus adds peer_send, peer_inbox_read, peer_inbox_wait.
    assert set(ACTION_REGISTRY.keys()) == {
        "noop", "tg_verify_login", "tg_notify", "deploy",
        "pause_deploys", "resume_deploys",
        "list_sessions", "pause_session", "suspend_session", "resume_session",
        "spawn_session",
        "scheduler_state", "inject_input",
        "autonomous_status", "autonomous_enable", "autonomous_disable",
        "peer_send", "peer_inbox_read", "peer_inbox_wait",
        "task_progress_add",
        # T-0042: atomic T-NNNN allocator.
        "task_new",
        # Phase 9: bind multi-task-per-dev / multi-initiative-per-TL.
        "bind_task", "bind_initiative",
        # Sessions polish batch (2026-05-13): unbind + archive lifecycle.
        "unbind_task", "unbind_initiative",
        "archive_session", "unarchive_session",
        # T-0085: operator handoff levers for autoupdate failures.
        "autoupdate_retry", "autoupdate_force",
        # T-0089: trigger an out-of-cadence poller tick from the consumer UI.
        "autoupdate_check_now",
        # T-0054: hot-reload projects.toml after POST /api/projects.
        "reload_projects",
        # T-0072/0073/0077: binding-graph reconcilers + peer-bus rebind.
        "gc_sessions", "gc_stale_bindings", "peer_rebind_sid",
        # T-0142/0144: session-lifecycle reconcilers + Team entity + rename sync.
        "gc_dead_bindings", "archive_dead_teammates", "reconcile_teams",
        "list_teams", "archive_team", "resurrect_team", "sync_session_name",
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

    def send(self, *, chat_id, text, sid="", user="", urgent=False) -> bool:
        self.calls.append({"chat_id": chat_id, "text": text, "sid": sid, "user": user, "urgent": urgent})
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
# peer_send tg-mirror tests (T-0035 lean Option B)
# ---------------------------------------------------------------------------


def test_peer_send_mirrors_to_telegram_for_ui_sid(tmp_path, tmp_config_dir, monkeypatch):
    """peer_send addressed to S-<user>-ui-p0 fires tg.send for the user's bound chat."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "auth.toml").write_text(
        '[users]\n'
        'alexey = "hash"\n'
        '\n'
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "404580642"\n'
    )
    (tmp_path / "data" / "test-project" / "_chat").mkdir(parents=True)
    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)

    out = A.dispatch("peer_send", {
        "slug": "test-project",
        "from_sid": "S-almdudleer-operator-p23",
        "to": "S-alexey-ui-p0",
        "text": "ack — got your ping",
    })
    assert out["ok"] is True
    assert out["delivered_to"] == ["S-alexey-ui-p0"]
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["chat_id"] == "404580642"
    assert call["text"] == "ack — got your ping"
    assert call["sid"] == "S-almdudleer-operator-p23"
    assert call["user"] == "alexey"


def test_peer_send_skips_tg_mirror_when_user_has_no_chat_id(tmp_path, tmp_config_dir, monkeypatch):
    """User present in user_meta but with no tg_chat_id → no TG call."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "auth.toml").write_text(
        '[users]\n'
        'aqice = "hash"\n'
        '\n'
        '[user_meta.aqice]\n'
        'linux_user = "aqice"\n'
    )
    (tmp_path / "data" / "test-project" / "_chat").mkdir(parents=True)
    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)

    out = A.dispatch("peer_send", {
        "slug": "test-project",
        "from_sid": "S-x-p1",
        "to": "S-aqice-ui-p0",
        "text": "hi",
    })
    assert out["ok"] is True
    assert fake.calls == []


def test_peer_send_no_mirror_for_non_ui_sid(tmp_path, tmp_config_dir, monkeypatch):
    """A dev/TL/operator SID should never trigger the TG mirror."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "auth.toml").write_text(
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "404580642"\n'
    )
    (tmp_path / "data" / "test-project" / "_chat").mkdir(parents=True)
    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)

    out = A.dispatch("peer_send", {
        "slug": "test-project",
        "from_sid": "S-alexey-ui-p0",
        "to": "S-almdudleer-operator-p23",
        "text": "hi",
    })
    assert out["ok"] is True
    assert fake.calls == []


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
    assert isinstance(result, dict)
    assert "sessions" in result
    assert isinstance(result["sessions"], list)


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
        if "capture-pane" in args:
            # T-0126: spawn() polls capture-pane for the ❯ composer rune
            # before send-keys; emit it so the readiness check passes.
            return sp.CompletedProcess(args, 0, "❯ \n", "")
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


# ---------------------------------------------------------------------------
# scheduler_state action tests (spec #6)
# ---------------------------------------------------------------------------


class _FakeJob:
    """Minimal stub for an APScheduler job."""

    def __init__(self, jid: str, next_run=None, trigger_str="interval[60s]") -> None:
        self.id = jid
        self.next_run_time = next_run
        self._trigger_str = trigger_str

    @property
    def trigger(self):
        class _T:
            def __str__(self_inner):
                return self._trigger_str
        return _T()


class _FakeSched:
    def __init__(self, jobs=None) -> None:
        self._jobs = jobs or []

    def get_jobs(self):
        return self._jobs


def _make_scheduler_cfg(tmp_path: Path, monkeypatch):
    """Create config + heartbeat file + inject fake scheduler."""
    import types
    import bot_squad_worker.actions as A

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.tp]\nslug = "tp"\ndisplay_name = "TP"\n'
        'repo_path = "/tmp/tp"\ndeploy_branch = "dev"\nmaster_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\ntg_chat = "0"\ncreated_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')

    data_dir = tmp_path / "data"
    worker_dir = data_dir / "_worker"
    worker_dir.mkdir(parents=True)
    hb_path = worker_dir / "heartbeat"
    hb_path.write_text("ok")

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        heartbeat_path=hb_path,
        tg_bot_token="",
    )
    monkeypatch.setattr(A, "_get_config", lambda: patched)
    return patched


def test_scheduler_state_returns_expected_shape(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from datetime import datetime, timezone

    cfg = _make_scheduler_cfg(tmp_path, monkeypatch)

    fake_sched = _FakeSched(jobs=[
        _FakeJob("heartbeat", datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)),
        _FakeJob("deploy_monitor", None),
    ])
    monkeypatch.setattr(A, "_SCHED", fake_sched)

    result = A.dispatch("scheduler_state", {})
    assert "jobs" in result
    assert "worker_started_at" in result
    assert "last_heartbeat_age_seconds" in result
    assert len(result["jobs"]) == 2
    assert result["jobs"][0]["id"] == "heartbeat"
    assert result["jobs"][0]["next_run"] is not None
    assert result["jobs"][1]["next_run"] is None


def test_scheduler_state_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_scheduler_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "_SCHED", _FakeSched())
    with pytest.raises(ActionError, match="takes no params"):
        A.dispatch("scheduler_state", {"evil": "x"})


# ---------------------------------------------------------------------------
# inject_input action tests (spec #7)
# ---------------------------------------------------------------------------


def _make_inject_cfg(tmp_path: Path, monkeypatch):
    """Create config + inject it into actions module."""
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
    cfg = Config.load(cfg_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    return cfg


def test_inject_input_happy_path(tmp_path, monkeypatch):
    """Happy path: pane found, subprocess called once per line."""
    import subprocess
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    from bot_squad_worker.sessions import PaneInfo

    _make_inject_cfg(tmp_path, monkeypatch)

    # Mock list_panes to return a matching pane
    # SID: S-testuser-specwin-p5 → compute_sid("testuser", "specwin", "%5")
    fake_pane = PaneInfo(pane_id="%5", window="specwin", pid="1234", cwd="/tmp", command="claude")
    monkeypatch.setattr(S, "list_panes", lambda: [fake_pane])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")

    run_calls = []

    def fake_run(args, check=False):
        run_calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("subprocess.run", fake_run)

    result = A.dispatch("inject_input", {"sid": "S-testuser-specwin-p5", "text": "hello"})
    assert result["ok"] is True
    assert result["pane_id"] == "%5"
    assert result["lines_sent"] == 1
    assert any("send-keys" in str(c) for c in run_calls)


def test_inject_input_multiline(tmp_path, monkeypatch):
    """Multi-line text sends one send-keys per line."""
    import subprocess
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    from bot_squad_worker.sessions import PaneInfo

    _make_inject_cfg(tmp_path, monkeypatch)

    fake_pane = PaneInfo(pane_id="%7", window="win", pid="1111", cwd="/tmp", command="bash")
    monkeypatch.setattr(S, "list_panes", lambda: [fake_pane])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")

    run_calls = []
    monkeypatch.setattr("subprocess.run", lambda args, check=False: run_calls.append(args) or subprocess.CompletedProcess(args, 0))

    result = A.dispatch("inject_input", {"sid": "S-testuser-win-p7", "text": "line1\nline2\nline3"})
    assert result["lines_sent"] == 3
    # 2 send-keys calls per line (text + Enter sent separately so Enter submits
    # outside tmux's bracketed-paste — see _action_inject_input)
    assert len(run_calls) == 6


def test_inject_input_unknown_sid(tmp_path, monkeypatch):
    """Unknown SID raises ActionError."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_inject_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")

    with pytest.raises(ActionError, match="no live pane"):
        A.dispatch("inject_input", {"sid": "S-testuser-nosuchwin-p99", "text": "hello"})


def test_inject_input_empty_text(tmp_path, monkeypatch):
    """Empty text raises ActionError."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_inject_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")

    with pytest.raises(ActionError, match="empty text"):
        A.dispatch("inject_input", {"sid": "S-testuser-win-p1", "text": "   "})


def test_inject_input_extra_params(tmp_path, monkeypatch):
    """Extra params raise ActionError."""
    import bot_squad_worker.actions as A

    _make_inject_cfg(tmp_path, monkeypatch)

    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("inject_input", {"sid": "S-x-y-p1", "text": "hi", "evil": "x"})


def test_inject_input_missing_params(tmp_path, monkeypatch):
    """Missing required params raise ActionError."""
    import bot_squad_worker.actions as A

    _make_inject_cfg(tmp_path, monkeypatch)

    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("inject_input", {"sid": "S-x-y-p1"})


# ---------------------------------------------------------------------------
# autonomous_status / autonomous_enable / autonomous_disable tests (spec #8)
# ---------------------------------------------------------------------------


def _make_auto_cfg(tmp_path: Path, monkeypatch):
    """Create config + data dirs for autonomous action tests."""
    import types
    import bot_squad_worker.actions as A

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True)
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
    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        tg_bot_token="",
    )
    monkeypatch.setattr(A, "_get_config", lambda: patched)
    return patched


def test_autonomous_status_returns_disabled_by_default(tmp_path, monkeypatch):
    """autonomous_status for a new project returns enabled=False."""
    import bot_squad_worker.actions as A

    _make_auto_cfg(tmp_path, monkeypatch)
    result = A.dispatch("autonomous_status", {"slug": "test-project"})
    assert result["ok"] is True
    assert result["enabled"] is False
    assert result["status"] == "idle"


def test_autonomous_status_unknown_slug_raises(tmp_path, monkeypatch):
    """autonomous_status with unknown slug raises ActionError."""
    import bot_squad_worker.actions as A

    _make_auto_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("autonomous_status", {"slug": "no-such-project"})


def test_autonomous_status_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_auto_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("autonomous_status", {"slug": "test-project", "evil": "x"})


def test_autonomous_enable_sets_enabled_true(tmp_path, monkeypatch):
    """autonomous_enable persists enabled=True."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import autonomous as _auto

    cfg = _make_auto_cfg(tmp_path, monkeypatch)
    result = A.dispatch("autonomous_enable", {"slug": "test-project"})
    assert result["ok"] is True
    assert result["enabled"] is True

    state = _auto.load_state(cfg, "test-project")
    assert state.enabled is True


def test_autonomous_enable_accepts_sleep_hours(tmp_path, monkeypatch):
    """autonomous_enable accepts optional sleep_start_hour / sleep_end_hour."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import autonomous as _auto

    cfg = _make_auto_cfg(tmp_path, monkeypatch)
    A.dispatch("autonomous_enable", {
        "slug": "test-project",
        "sleep_start_hour": 23,
        "sleep_end_hour": 7,
    })
    state = _auto.load_state(cfg, "test-project")
    assert state.sleep_start_hour == 23
    assert state.sleep_end_hour == 7


def test_autonomous_enable_unknown_slug_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_auto_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("autonomous_enable", {"slug": "no-such-project"})


def test_autonomous_enable_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_auto_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("autonomous_enable", {"slug": "test-project", "evil": "x"})


def test_autonomous_disable_sets_enabled_false(tmp_path, monkeypatch):
    """autonomous_disable persists enabled=False even if previously enabled."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import autonomous as _auto

    cfg = _make_auto_cfg(tmp_path, monkeypatch)
    # Enable first
    A.dispatch("autonomous_enable", {"slug": "test-project"})
    state = _auto.load_state(cfg, "test-project")
    assert state.enabled is True

    # Now disable
    result = A.dispatch("autonomous_disable", {"slug": "test-project"})
    assert result["ok"] is True
    assert result["enabled"] is False

    state2 = _auto.load_state(cfg, "test-project")
    assert state2.enabled is False


def test_autonomous_disable_unknown_slug_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_auto_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("autonomous_disable", {"slug": "no-such-project"})


def test_autonomous_disable_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_auto_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("autonomous_disable", {"slug": "test-project", "evil": "x"})


# ---------------------------------------------------------------------------
# task_progress_add action tests (Phase 7)
# ---------------------------------------------------------------------------


def _make_task_progress_cfg(tmp_path: Path, monkeypatch):
    """Config + a single test task md, with config injected into actions."""
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
    data_dir = tmp_path / "data"
    backlog = data_dir / "test-project" / "backlog"
    backlog.mkdir(parents=True)

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        tg_bot_token="",
    )
    monkeypatch.setattr(A, "_get_config", lambda: patched)
    return patched, backlog


def test_task_progress_add_appends_line(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    task_path = backlog / "T-0001-foo.md"
    task_path.write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nI want X.\n"
    )
    out = A.dispatch("task_progress_add", {
        "slug": "test-project",
        "task_id": "T-0001",
        "sid": "S-test-p1",
        "text": "shipped the thing",
    })
    assert out["ok"] is True
    assert out["task_id"] == "T-0001"
    assert "S-test-p1" in out["line_appended"]
    assert "shipped the thing" in out["line_appended"]

    content = task_path.read_text()
    assert "## Verbatim request" in content
    assert "I want X." in content
    assert "## Progress" in content
    assert "S-test-p1 · shipped the thing" in content


def test_task_progress_add_unknown_task_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_task_progress_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="task not found"):
        A.dispatch("task_progress_add", {
            "slug": "test-project",
            "task_id": "T-9999",
            "sid": "S-x",
            "text": "x",
        })


def test_task_progress_add_unknown_slug_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_task_progress_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("task_progress_add", {
            "slug": "no-such",
            "task_id": "T-0001",
            "sid": "S-x",
            "text": "x",
        })


def test_task_progress_add_empty_text_raises(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    with pytest.raises(ActionError, match="empty text"):
        A.dispatch("task_progress_add", {
            "slug": "test-project",
            "task_id": "T-0001",
            "sid": "S-x",
            "text": "   ",
        })


def test_task_progress_add_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_task_progress_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("task_progress_add", {
            "slug": "test-project", "task_id": "T-0001",
            "sid": "S-x", "text": "x", "evil": "x",
        })


def test_task_progress_add_missing_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_task_progress_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("task_progress_add", {"slug": "test-project", "task_id": "T-0001"})


def test_task_progress_add_preserves_verbatim_section(tmp_path, monkeypatch):
    """The verbatim section must not be altered when appending progress."""
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    task_path = backlog / "T-0001-foo.md"
    task_path.write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nDO NOT REWRITE.\n\n"
        "## Context\n\nctx text\n"
    )
    A.dispatch("task_progress_add", {
        "slug": "test-project", "task_id": "T-0001",
        "sid": "S-x", "text": "first",
    })
    A.dispatch("task_progress_add", {
        "slug": "test-project", "task_id": "T-0001",
        "sid": "S-y", "text": "second",
    })
    content = task_path.read_text()
    assert "DO NOT REWRITE." in content
    assert "ctx text" in content
    assert content.count("S-x · first") == 1
    assert content.count("S-y · second") == 1


# ---------------------------------------------------------------------------
# reload_projects tests (T-0054)
# ---------------------------------------------------------------------------


def test_reload_projects_picks_up_new_slug(tmp_config_dir: Path, monkeypatch):
    """After projects.toml gains a new block, reload_projects rebuilds the
    in-memory config so cfg.projects sees the new slug without restart."""
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    A.set_config(cfg)
    assert set(A._get_config().projects.keys()) == {"test-project"}

    # Append a second project block to projects.toml.
    projects_toml = tmp_config_dir / "projects.toml"
    projects_toml.write_text(
        projects_toml.read_text()
        + "\n[projects.fresh]\n"
        'slug = "fresh"\n'
        'display_name = "Fresh"\n'
        'repo_path = "/tmp/fresh-repo"\n'
        'deploy_branch = ""\n'
        'master_branch = ""\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = []\n'
        'tg_chat = ""\n'
    )

    out = A.dispatch("reload_projects", {})
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["projects"] == ["fresh", "test-project"]
    # Module-level config was actually swapped in — subsequent action lookups
    # see the new slug.
    assert "fresh" in A._get_config().projects


def test_reload_projects_rejects_params(tmp_config_dir: Path, monkeypatch):
    import bot_squad_worker.actions as A

    A.set_config(Config.load(tmp_config_dir))
    with pytest.raises(ActionError, match="unexpected"):
        A.dispatch("reload_projects", {"slug": "test-project"})


# ---------------------------------------------------------------------------
# task_new tests (T-0042 — atomic T-NNNN allocator)
# ---------------------------------------------------------------------------


def _setup_task_new(tmp_path: Path, tmp_config_dir: Path, monkeypatch) -> Path:
    """Wire the config + return the backlog dir for the test-project slug."""
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    backlog = tmp_path / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True)
    return backlog


def test_task_new_empty_backlog_allocates_t0001(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    backlog = _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("task_new", {"slug": "test-project", "title": "first task"})
    assert out["ok"] is True
    assert out["id"] == "T-0001"
    p = Path(out["file_path"])
    assert p.parent == backlog
    assert p.name == "T-0001-first-task.md"
    body = p.read_text()
    assert "id: T-0001" in body
    assert "status: planned" in body
    assert "## Verbatim request" in body
    assert "(filed via task_new)" in body
    assert "## DoD" in body


def test_task_new_returns_t0134_for_existing_133(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    backlog = _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    # Seed T-0001..T-0133 as empty stubs — only the filename matters for max-id.
    for i in range(1, 134):
        (backlog / f"T-{i:04d}-seed.md").write_text("---\nid: T-XXXX\n---\n")
    out = A.dispatch("task_new", {"slug": "test-project", "title": "next one"})
    assert out["id"] == "T-0134"
    assert Path(out["file_path"]).name == "T-0134-next-one.md"


def test_task_new_concurrent_threads_get_distinct_ids(tmp_path, tmp_config_dir, monkeypatch):
    """4 threads racing on task_new must each get a distinct T-NNNN id."""
    from concurrent.futures import ThreadPoolExecutor
    import bot_squad_worker.actions as A

    backlog = _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)

    def _alloc(i: int) -> dict:
        return A.dispatch(
            "task_new",
            {"slug": "test-project", "title": f"concurrent task {i}"},
        )

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(_alloc, range(4)))

    ids = [r["id"] for r in results]
    assert len(set(ids)) == 4, f"ids must be distinct, got {ids}"
    assert set(ids) == {"T-0001", "T-0002", "T-0003", "T-0004"}
    # Files all exist with non-overlapping ids.
    for r in results:
        p = Path(r["file_path"])
        assert p.exists()
        assert p.parent == backlog
        assert p.name.startswith(r["id"] + "-")
    # Filenames on disk match the four returned ids exactly (plus the lock).
    on_disk_ids = sorted(
        p.name.split("-", 2)[0] + "-" + p.name.split("-", 2)[1]
        for p in backlog.glob("T-*.md")
    )
    assert on_disk_ids == sorted(ids)


def test_task_new_writes_optional_frontmatter_fields(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("task_new", {
        "slug": "test-project",
        "title": "with extras",
        "initiative": "multi-server-installation-process.md",
        "priority": "P1",
        "owner": "alexey",
    })
    body = Path(out["file_path"]).read_text()
    assert 'initiative: "multi-server-installation-process.md"' in body
    assert 'priority: "P1"' in body
    assert 'owner: "alexey"' in body


def test_task_new_quotes_titles_with_yaml_specials(tmp_path, tmp_config_dir, monkeypatch):
    """Titles with ':' or '#' must not break the YAML frontmatter."""
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    title = 'Fix: cache miss in #ingest path'
    out = A.dispatch("task_new", {"slug": "test-project", "title": title})
    body = Path(out["file_path"]).read_text()
    # Pull the title line out, parse with tomllib? No — frontmatter is YAML;
    # just confirm the quoted form is what we wrote.
    assert f'title: "Fix: cache miss in #ingest path"' in body


def test_task_new_rejects_extra_params(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="unexpected"):
        A.dispatch("task_new", {
            "slug": "test-project", "title": "x", "evil": 1,
        })


def test_task_new_rejects_empty_title(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="empty title"):
        A.dispatch("task_new", {"slug": "test-project", "title": "   "})


def test_task_new_rejects_multiline_title(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="single-line"):
        A.dispatch("task_new", {"slug": "test-project", "title": "a\nb"})


def test_task_new_unknown_slug_raises(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("task_new", {"slug": "no-such", "title": "x"})
