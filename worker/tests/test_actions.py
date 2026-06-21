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
        # T-0296: per-project clone health read-model + ff-only pull-master.
        "clone_status", "pull_master",
        # T-0155: stall-watchdog marker clear (UserPromptSubmit hook).
        "tg_stall_clear",
        "pause_deploys", "resume_deploys",
        "list_sessions", "telemetry_get",
        "pause_session", "suspend_session", "resume_session",
        "spawn_session",
        "scheduler_state", "inject_input",
        "autonomous_status", "autonomous_enable", "autonomous_disable",
        # T-0153: autopilot — prompt-driven, time-boxed autonomous runs.
        "autopilot_start", "autopilot_stop", "autopilot_status",
        "peer_send", "peer_inbox_read", "peer_inbox_wait",
        "task_progress_add",
        # T-0042: atomic T-NNNN allocator.
        "task_new",
        # T-0174: generalized atomic allocator across all entity types.
        "doc_new", "uc_new", "flow_new", "initiative_new",
        # Phase 9: bind multi-task-per-dev / multi-initiative-per-TL.
        "bind_task", "bind_initiative",
        # T-0237 Layer-2: operator-invoked reuse-vs-spawn dispatch decision.
        "dispatch_decision",
        # T-0184: per-session drift-check off-ramp (bsq drift on/off).
        "set_drift_paused",
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
        # T-0176 #5/#6: one-shot duplicate-SessionMd dedup migration (gated).
        "dedup_sessions",
        # T-0177: gated prune of stale per-initiative team files post-regroup.
        "prune_orphan_teams",
        # T-0142/0144: session-lifecycle reconcilers + Team entity + rename sync.
        "gc_dead_bindings", "archive_dead_teammates", "reconcile_teams",
        "list_teams", "archive_team", "resurrect_team", "sync_session_name",
        # T-0247: MAX (max.ru) DM channel — mirrors tg_notify.
        "max_notify",
        # T-0386: per-project forum-topic lifecycle (create-on-project / GC).
        "provision_project_topics", "gc_project_topics",
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

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None) -> bool:
        self.calls.append({"chat_id": chat_id, "text": text, "sid": sid, "user": user, "urgent": urgent, "topic_id": topic_id})
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
    assert out == {"ok": True, "sent": True, "channel": "tg"}
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


def test_tg_notify_uses_system_default_chat_over_first_project(tmp_config_dir, monkeypatch):
    """T-0171: with a system default_chat_id, a notify carrying neither chat_id
    nor slug resolves to that default (the detached local-bot path) instead of
    guessing the first registered project's chat."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "system_settings.toml").write_text(
        '[tg]\ndefault_chat_id = "DEFAULT_999"\n'
    )
    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    assert cfg.tg_default_chat_id == "DEFAULT_999"
    out = A.dispatch("tg_notify", {"message": "detached hello"})
    assert out["ok"] is True
    assert fake.calls[0]["chat_id"] == "DEFAULT_999"


def test_tg_notify_slug_still_wins_over_default_chat(tmp_config_dir, monkeypatch):
    """A project-bound notify still targets the project chat — the system
    default only fills the no-slug/no-chat gap."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "system_settings.toml").write_text(
        '[tg]\ndefault_chat_id = "DEFAULT_999"\n'
    )
    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {"slug": "test-project", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "0"  # test-project tg_chat, not the default


def test_tg_notify_debounce_returns_sent_false(tmp_config_dir, monkeypatch):
    """When TgClient.send returns False (debounced), tg_notify reports sent=False."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    fake._suppress = True
    out = A.dispatch("tg_notify", {"message": "same"})
    assert out == {"ok": True, "sent": False, "channel": "tg"}


def test_tg_notify_sid_and_user_forwarded(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {"message": "hi", "sid": "S-x-p1", "user": "alexey"})
    call = fake.calls[0]
    assert call["sid"] == "S-x-p1"
    assert call["user"] == "alexey"


# --- T-0241: needs-input enrichment (tmux-attach command + reply hint) ---

def test_tg_notify_needs_input_appends_tmux_attach(tmp_config_dir, monkeypatch):
    """needs_input=True + an explicit tmux_session → the DM carries the exact
    `tmux attach -t <session>` command AND keeps the message text."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {
        "message": "Which DB should I use?",
        "needs_input": True,
        "tmux_session": "bot-squad-roles",
    })
    text = fake.calls[0]["text"]
    assert "Which DB should I use?" in text
    assert "tmux attach -t bot-squad-roles" in text
    assert "Reply" in text  # replying in TG must still work


def test_tg_notify_needs_input_forces_urgent(tmp_config_dir, monkeypatch):
    """A blocked process's input request must clear the quiet-hours gate, so
    needs_input forces urgent even when the caller didn't pass it."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {"message": "need a decision", "needs_input": True})
    assert fake.calls[0]["urgent"] is True


def test_tg_notify_needs_input_resolves_session_from_sid(tmp_config_dir, monkeypatch):
    """With no explicit tmux_session, needs_input resolves the session's
    tmux_session from its SessionMd (sid + slug)."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    sdir = cfg.data_dir / "test-project" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sdir / "S-u-roles-dev-p5.md", {
        "sid": "S-u-roles-dev-p5", "status": "active", "window": "roles-dev",
        "task_id": "~", "initiative": "~", "tmux_session": "bot-squad-roles",
    })
    A.dispatch("tg_notify", {
        "message": "stuck", "needs_input": True,
        "slug": "test-project", "sid": "S-u-roles-dev-p5",
    })
    assert "tmux attach -t bot-squad-roles" in fake.calls[0]["text"]


def test_tg_notify_without_needs_input_unchanged(tmp_config_dir, monkeypatch):
    """Back-compat: no needs_input → text is the bare message, no footer, and
    urgent is honored exactly as passed."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {"message": "plain", "tmux_session": "bot-squad-x"})
    assert fake.calls[0]["text"] == "plain"
    assert fake.calls[0]["urgent"] is False


# --- T-0156: project-bound forum topic resolution ---

def _config_dir_with_topic(tmp_path: Path) -> Path:
    """A config dir whose project binds a group chat + forum topic."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.toml").write_text(
        '[projects.group-project]\n'
        'slug = "group-project"\n'
        'display_name = "Group Project"\n'
        'repo_path = "/tmp/group-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "-1001234567890"\n'
        'tg_topic_id = 99\n'
    )
    (cfg / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\nauth_age_max = 86400\n'
    )
    return cfg


def test_tg_notify_inherits_project_topic(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg_dir = _config_dir_with_topic(tmp_path)
    _, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "-1001234567890"
    assert fake.calls[0]["topic_id"] == 99


def test_tg_notify_explicit_topic_id_overrides_project(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg_dir = _config_dir_with_topic(tmp_path)
    _, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi", "topic_id": 7})
    assert fake.calls[0]["topic_id"] == 7


def test_tg_notify_topic_none_when_project_has_no_topic(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {"slug": "test-project", "message": "hi"})
    assert fake.calls[0]["topic_id"] is None


def test_tg_notify_rejects_non_integer_topic(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="topic_id must be an integer"):
        A.dispatch("tg_notify", {"message": "hi", "topic_id": "abc"})


# ---------------------------------------------------------------------------
# max_notify tests (T-0247) — MAX (max.ru) DM channel, mirrors tg_notify
# ---------------------------------------------------------------------------

class _FakeMaxClient:
    """Records calls instead of hitting the real MAX API."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._suppress = False

    def send(self, *, chat_id, text, sid="", user="", urgent=False, recipient_kind=None) -> bool:
        self.calls.append({
            "chat_id": chat_id, "text": text, "sid": sid, "user": user,
            "urgent": urgent, "recipient_kind": recipient_kind,
        })
        return not self._suppress


def _inject_fake_max(monkeypatch, tmp_config_dir, fake_client=None):
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    if fake_client is None:
        fake_client = _FakeMaxClient()
    monkeypatch.setattr(A, "_get_max_client", lambda _cfg: fake_client)
    return cfg, fake_client


def _config_dir_with_max_default(tmp_config_dir: Path, chat_id: str = "MAXCHAT99") -> None:
    (tmp_config_dir / "system_settings.toml").write_text(
        f'[max]\ndefault_chat_id = "{chat_id}"\n'
    )


def test_max_notify_sends_to_explicit_chat_id(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _, fake = _inject_fake_max(monkeypatch, tmp_config_dir)
    out = A.dispatch("max_notify", {"chat_id": "555", "message": "hello max"})
    assert out == {"ok": True, "sent": True}
    assert fake.calls[0]["chat_id"] == "555"
    assert fake.calls[0]["text"] == "hello max"


def test_max_notify_uses_default_chat_when_no_chat_id(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake = _inject_fake_max(monkeypatch, tmp_config_dir)
    A.dispatch("max_notify", {"message": "to the stakeholder"})
    assert fake.calls[0]["chat_id"] == "MAXCHAT99"


def test_max_notify_no_chat_and_no_default_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_max(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="no chat_id"):
        A.dispatch("max_notify", {"message": "nowhere to go"})


# ---------------------------------------------------------------------------
# T-0247: channel-aware stakeholder DM — tg_notify routes via MAX when the
# install has [max].default_chat_id (TG is DPI-blocked here); TG is the fallback.
# ---------------------------------------------------------------------------

def _inject_both_channels(monkeypatch, tmp_config_dir, max_client=None):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    fake_tg = _FakeTgClient()
    fake_max = max_client or _FakeMaxClient()
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: fake_tg)
    monkeypatch.setattr(A, "_get_max_client", lambda _c: fake_max)
    return cfg, fake_tg, fake_max


def test_tg_notify_routes_to_max_when_configured(tmp_config_dir, monkeypatch):
    """The DEFAULT stakeholder DM goes via MAX (primary) when [max] is set."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"message": "stakeholder dm", "sid": "S-x-p1"})
    assert out == {"ok": True, "sent": True, "channel": "max"}
    assert len(fake_max.calls) == 1 and len(fake_tg.calls) == 0
    assert fake_max.calls[0]["chat_id"] == "MAXCHAT99"
    assert fake_max.calls[0]["text"] == "stakeholder dm"


def test_tg_notify_uses_tg_when_max_unconfigured(tmp_config_dir, monkeypatch):
    """No [max].default_chat_id → the existing TG behavior is unchanged."""
    import bot_squad_worker.actions as A
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"message": "hi"})
    assert out["channel"] == "tg"
    assert len(fake_tg.calls) == 1 and len(fake_max.calls) == 0


def test_tg_notify_falls_back_to_tg_when_max_errors(tmp_config_dir, monkeypatch):
    """MAX configured but its send raises → fall back to TG (loop still closes)."""
    import bot_squad_worker.actions as A

    class _BoomMax:
        def send(self, **kwargs):
            raise RuntimeError("max api down")

    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir, max_client=_BoomMax())
    out = A.dispatch("tg_notify", {"message": "hi"})
    assert out == {"ok": True, "sent": True, "channel": "tg"}
    assert len(fake_tg.calls) == 1


def test_tg_notify_explicit_chat_id_stays_on_tg(tmp_config_dir, monkeypatch):
    """An explicit chat_id/topic_id is a TG group/forum target — never MAX."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"message": "to a group", "chat_id": "GROUP-7"})
    assert out["channel"] == "tg"
    assert len(fake_tg.calls) == 1 and len(fake_max.calls) == 0
    assert fake_tg.calls[0]["chat_id"] == "GROUP-7"


def test_max_notify_missing_message_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_max(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="missing required param"):
        A.dispatch("max_notify", {"chat_id": "1"})


def test_max_notify_rejects_extra_params(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_max(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("max_notify", {"chat_id": "1", "message": "hi", "evil": "x"})


def test_max_notify_debounce_returns_sent_false(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _, fake = _inject_fake_max(monkeypatch, tmp_config_dir)
    fake._suppress = True
    out = A.dispatch("max_notify", {"chat_id": "1", "message": "same"})
    assert out == {"ok": True, "sent": False}


def test_max_notify_sid_user_and_recipient_kind_forwarded(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _, fake = _inject_fake_max(monkeypatch, tmp_config_dir)
    A.dispatch("max_notify", {
        "chat_id": "1", "message": "hi", "sid": "S-x-p1", "user": "alexey",
        "urgent": True, "recipient_kind": "user_id",
    })
    call = fake.calls[0]
    assert call["sid"] == "S-x-p1"
    assert call["user"] == "alexey"
    assert call["urgent"] is True
    assert call["recipient_kind"] == "user_id"


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
# T-0218: per-user notify precedence (project -> server -> global)
# ---------------------------------------------------------------------------


def _seed_migrated_attachment(
    tmp_path, *, gu_id="GU-1", server_id="srv-self",
    tg_chat_id="", project_tg_chat_ids=None,
):
    """Write a self-server registry + an Attachment JSON on disk, mirroring the
    API's mothership store layout so the worker resolver can read it."""
    import json

    mship = tmp_path / "data" / "_mothership"
    (mship).mkdir(parents=True, exist_ok=True)
    (mship / "servers.json").write_text(
        json.dumps({"version": 1, "servers": [{"id": server_id, "is_self": True}]})
    )
    att_dir = mship / "attachments" / gu_id
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / f"{server_id}.json").write_text(
        json.dumps({
            "global_user_id": gu_id,
            "server_id": server_id,
            "server_username": "alexey",
            "attached_at": "2026-06-19T00:00:00Z",
            "tg_chat_id": tg_chat_id,
            "seen_steps": [],
            "last_seen_at": None,
            "project_tg_chat_ids": project_tg_chat_ids or {},
        })
    )


def test_resolve_user_tg_unmigrated_project_over_global(tmp_path, tmp_config_dir):
    """Un-migrated user: a per-project override beats the global tg_chat_id;
    an unmapped slug falls back to global."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "auth.toml").write_text(
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "111"\n'
        'project_tg_chat_ids = { "proj-x" = "333" }\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert A._resolve_user_tg_chat_id(cfg, "alexey", slug="proj-x") == "333"
    assert A._resolve_user_tg_chat_id(cfg, "alexey", slug="other") == "111"
    assert A._resolve_user_tg_chat_id(cfg, "alexey") == "111"


def test_resolve_user_tg_migrated_precedence(tmp_path, tmp_config_dir):
    """Migrated user: project > server > global, each level falling through."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "auth.toml").write_text(
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "111"\n'
        'attached_to_global_user = "GU-1"\n'
    )
    _seed_migrated_attachment(
        tmp_path, tg_chat_id="222", project_tg_chat_ids={"proj-x": "333"}
    )
    cfg = Config.load(tmp_config_dir)
    # project override wins
    assert A._resolve_user_tg_chat_id(cfg, "alexey", slug="proj-x") == "333"
    # no project override → server level
    assert A._resolve_user_tg_chat_id(cfg, "alexey", slug="other") == "222"
    # no slug → server level (still beats global)
    assert A._resolve_user_tg_chat_id(cfg, "alexey") == "222"


def test_resolve_user_tg_migrated_server_empty_falls_to_global(
    tmp_path, tmp_config_dir
):
    """Migrated user with no per-server override → global tg_chat_id."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "auth.toml").write_text(
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "111"\n'
        'attached_to_global_user = "GU-1"\n'
    )
    _seed_migrated_attachment(tmp_path, tg_chat_id="", project_tg_chat_ids={})
    cfg = Config.load(tmp_config_dir)
    assert A._resolve_user_tg_chat_id(cfg, "alexey", slug="proj-x") == "111"


def test_peer_send_tg_mirror_uses_project_override(
    tmp_path, tmp_config_dir, monkeypatch
):
    """peer_send tg-mirror fires to the project-override chat for the slug it
    was sent under, not the global binding."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "auth.toml").write_text(
        '[users]\n'
        'alexey = "hash"\n'
        '\n'
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "111"\n'
        'attached_to_global_user = "GU-1"\n'
    )
    _seed_migrated_attachment(
        tmp_path, tg_chat_id="222", project_tg_chat_ids={"test-project": "999"}
    )
    (tmp_path / "data" / "test-project" / "_chat").mkdir(parents=True)
    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)

    out = A.dispatch("peer_send", {
        "slug": "test-project",
        "from_sid": "S-almdudleer-operator-p23",
        "to": "S-alexey-ui-p0",
        "text": "project-scoped ping",
    })
    assert out["ok"] is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["chat_id"] == "999"


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
    pasted = [False]
    entered = [False]

    def fake_run(args, **kw):
        import subprocess as sp
        if "list-panes" in args:
            call_counts["list"] += 1
            if call_counts["list"] > 1:
                return sp.CompletedProcess(args, 0, f"%9|testwin|1111|{repo}|claude\n", "")
            return sp.CompletedProcess(args, 0, "", "")
        if "paste-buffer" in args:
            pasted[0] = True
            return sp.CompletedProcess(args, 0, "", "")
        if "send-keys" in args and args[-1] == "Enter":
            entered[0] = True
            return sp.CompletedProcess(args, 0, "", "")
        if "capture-pane" in args:
            # T-0126: spawn() polls capture-pane for the ❯ composer rune
            # before delivery. T-0201: _deliver_prompt then confirms the paste
            # landed (composer non-empty) and cleared (empty after Enter).
            if pasted[0] and not entered[0]:
                return sp.CompletedProcess(args, 0, "❯ [Pasted text #1 +1 lines]\n", "")
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


def test_spawn_session_action_threads_parent_sid(tmp_path, monkeypatch):
    """T-0128: spawn_session accepts the optional parent_sid param and threads
    it to sessions.spawn (allowlist + wiring)."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured.update(kw)
        return {"ok": True, "sid": "S-x-w-p1"}

    monkeypatch.setattr(S, "spawn", fake_spawn)
    result = A.dispatch("spawn_session", {
        "slug": "test-project", "window": "w",
        "parent_sid": "S-x-tl-p0",
    })
    assert result["ok"] is True
    assert captured.get("parent_sid") == "S-x-tl-p0"


def test_resume_session_action_accepts_initial_prompt(tmp_path, monkeypatch):
    """T-0150: resume_session must accept the optional initial_prompt param and
    pass it through to sessions.resume (so a resumed expert gets a delta brief)."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    captured = {}

    def fake_resume(cfg, slug, sid, initial_prompt=None, task_id=None):
        captured["args"] = (slug, sid, initial_prompt)
        captured["task_id"] = task_id
        return {"ok": True, "sid": sid}

    _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "resume", fake_resume)

    result = A.dispatch("resume_session", {
        "slug": "test-project", "sid": "S-u-w-p1",
        "initial_prompt": "delta brief here",
    })
    assert result["ok"] is True
    assert captured["args"] == ("test-project", "S-u-w-p1", "delta brief here")


def test_resume_session_action_passes_task_id(tmp_path, monkeypatch):
    """T-0166: resume_session must accept the optional task_id param and pass it
    to sessions.resume so an expert with no primary adopts the new ticket."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    captured = {}

    def fake_resume(cfg, slug, sid, initial_prompt=None, task_id=None):
        captured["task_id"] = task_id
        return {"ok": True, "sid": sid}

    _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "resume", fake_resume)

    result = A.dispatch("resume_session", {
        "slug": "test-project", "sid": "S-u-w-p1", "task_id": "T-0002",
    })
    assert result["ok"] is True
    assert captured["task_id"] == "T-0002"


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


def test_task_progress_add_concurrent_no_lost_notes(tmp_path, monkeypatch):
    """T-0373: N concurrent progress/comment adds all survive — was a shared-tmp +
    unlocked read-modify-write that lost writes (and 500'd) under contention."""
    import threading
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    task_path = backlog / "T-0001-foo.md"
    task_path.write_text("---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\n## Progress\n")
    n = 12
    barrier = threading.Barrier(n)
    errors: list = []

    def _w(i: int) -> None:
        barrier.wait()
        try:
            A.dispatch("task_progress_add", {
                "slug": "test-project", "task_id": "T-0001",
                "sid": f"S-p{i}", "text": f"concurrent-note-{i}",
            })
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_w, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"writers raised: {errors!r}"
    content = task_path.read_text()
    missing = [i for i in range(n) if f"concurrent-note-{i}" not in content]
    assert not missing, f"LOST notes: {missing}"


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


def test_task_new_uses_shared_counter(tmp_path, tmp_config_dir, monkeypatch):
    """T-0174: task_new now allocates from data/<slug>/_counters/task.txt —
    the same counter the API uses, so the two can't hand out the same id."""
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    A.dispatch("task_new", {"slug": "test-project", "title": "one"})
    counter = tmp_path / "data" / "test-project" / "_counters" / "task.txt"
    assert counter.read_text().strip() == "1"
    A.dispatch("task_new", {"slug": "test-project", "title": "two"})
    assert counter.read_text().strip() == "2"


# ---------------------------------------------------------------------------
# T-0174 — generalized entity_new actions (doc / uc / flow / initiative)
# ---------------------------------------------------------------------------


def _setup_entity_new(tmp_path: Path, tmp_config_dir: Path, monkeypatch) -> Path:
    """Wire config; return the project data dir for the test-project slug."""
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    proj = tmp_path / "data" / "test-project"
    proj.mkdir(parents=True)
    return proj


def test_doc_new_allocates_and_writes_stub(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Edge cache design",
    })
    assert out["id"] == "D-0001"
    assert out["category"] == "architecture"
    p = Path(out["file_path"])
    assert p == proj / "docs" / "architecture" / "D-0001-edge-cache-design.md"
    body = p.read_text()
    assert "id: D-0001" in body
    assert "category: architecture" in body
    assert "status: draft" in body
    assert (proj / "_counters" / "doc.txt").read_text().strip() == "1"


def test_doc_new_rejects_bad_category(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="invalid category"):
        A.dispatch("doc_new", {
            "slug": "test-project", "category": "../etc", "title": "x",
        })


def test_uc_new_stem_is_id_and_ignores_legacy_slug_ucs(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    # A legacy slug-named UC is non-numeric and must NOT bump the counter.
    (proj / "use_cases").mkdir(parents=True)
    (proj / "use_cases" / "UC-autopilot-popover.md").write_text("---\nid: UC-autopilot-popover\n---\n")
    out = A.dispatch("uc_new", {"slug": "test-project", "title": "Probe flow"})
    assert out["id"] == "UC-0001"
    p = Path(out["file_path"])
    # filename stem IS the id (what routes_usecases keys on) — no -slug suffix.
    assert p == proj / "use_cases" / "UC-0001.md"
    assert "id: UC-0001" in p.read_text()


def test_flow_new_requires_existing_uc(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="unknown use case"):
        A.dispatch("flow_new", {
            "slug": "test-project", "uc_id": "UC-0001", "title": "x",
        })
    # Create the UC, then the flow lands under it.
    A.dispatch("uc_new", {"slug": "test-project", "title": "parent"})
    out = A.dispatch("flow_new", {
        "slug": "test-project", "uc_id": "UC-0001", "title": "Happy path",
    })
    assert out["id"] == "UF-0001"  # T-0180: user-flow prefix, distinct from feedback F-
    assert out["uc_id"] == "UC-0001"
    p = Path(out["file_path"])
    assert p == proj / "use_cases" / "UC-0001" / "flows" / "UF-0001-happy-path.md"
    body = p.read_text()
    assert "uc_id: UC-0001" in body
    assert "```mermaid" in body


def test_flow_new_rejects_bad_uc_id(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="invalid uc_id"):
        A.dispatch("flow_new", {
            "slug": "test-project", "uc_id": "../evil", "title": "x",
        })


def test_initiative_new_two_digit_pad(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("initiative_new", {"slug": "test-project", "name": "Billing Revamp"})
    assert out["id"] == "INI-01"
    p = Path(out["file_path"])
    assert p == proj / "vision" / "initiatives" / "INI-01-billing-revamp.md"
    body = p.read_text()
    assert "id: INI-01" in body
    assert "name: " in body


def test_doc_new_self_heals_against_manual_file(tmp_path, tmp_config_dir, monkeypatch):
    """A hand-created higher id must not collide — allocator takes max(counter, scan)+1."""
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    arch = proj / "docs" / "architecture"
    arch.mkdir(parents=True)
    (arch / "D-0099-manual.md").write_text("x")
    out = A.dispatch("doc_new", {
        "slug": "test-project", "category": "design", "title": "next",
    })
    assert out["id"] == "D-0100"


def test_doc_new_concurrent_no_collisions(tmp_path, tmp_config_dir, monkeypatch):
    """The DoD's core check: spawn parallel doc_new, verify zero collisions."""
    from concurrent.futures import ThreadPoolExecutor
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)

    def _alloc(i: int) -> dict:
        return A.dispatch("doc_new", {
            "slug": "test-project", "category": "design", "title": f"race {i}",
        })

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_alloc, range(8)))

    ids = [r["id"] for r in results]
    assert len(set(ids)) == 8, f"ids must be distinct, got {ids}"
    assert set(ids) == {f"D-{i:04d}" for i in range(1, 9)}
    files = sorted((proj / "docs" / "design").glob("D-*.md"))
    assert len(files) == 8
    assert (proj / "_counters" / "doc.txt").read_text().strip() == "8"


def test_entity_new_unknown_slug_raises(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("doc_new", {"slug": "no-such", "category": "design", "title": "x"})


# ---------------------------------------------------------------------------
# T-0386 / INI-04 Phase 1: per-project forum-topic routing + provisioning
# ---------------------------------------------------------------------------

class _FakeForumTg(_FakeTgClient):
    """Fake TG client that also records forum-topic CRUD."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[dict] = []
        self.closed: list[dict] = []
        self._next_tid = 100

    def create_forum_topic(self, *, chat_id, name) -> int:
        self._next_tid += 1
        self.created.append({"chat_id": chat_id, "name": name, "id": self._next_tid})
        return self._next_tid

    def close_forum_topic(self, *, chat_id, thread_id) -> None:
        self.closed.append({"chat_id": chat_id, "thread_id": thread_id})


def test_tg_notify_topic_class_resolves_to_thread_id(tmp_config_dir, monkeypatch):
    """A `topic` class param routes the TG send into that class's forum thread."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_topics

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    tg_topics.save(cfg, "test-project", {"deploy_logs": 4242})
    out = A.dispatch("tg_notify", {"slug": "test-project", "message": "hi", "topic": "deploy_logs"})
    assert out["ok"] is True
    assert fake.calls[0]["topic_id"] == 4242


def test_provision_project_topics_creates_the_standard_set(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_topics

    fake = _FakeForumTg()
    cfg, _ = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("provision_project_topics", {"slug": "test-project"})
    assert out["ok"] is True
    assert set(out["topics"]) == {"feedback", "deploy_logs", "team_queries"}
    assert set(out["created"]) == {"feedback", "deploy_logs", "team_queries"}
    # Persisted so sends can resolve later.
    assert tg_topics.load(cfg, "test-project") == out["topics"]
    # Created in the project's supergroup.
    assert all(c["chat_id"] == "0" for c in fake.created)


def test_provision_project_topics_is_idempotent(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    cfg, _ = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    first = A.dispatch("provision_project_topics", {"slug": "test-project"})
    again = A.dispatch("provision_project_topics", {"slug": "test-project"})
    assert again["created"] == []                       # nothing new created
    assert again["topics"] == first["topics"]           # same ids
    assert len(fake.created) == 3                        # only the first round called the API


def test_provision_project_topics_unknown_slug_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="unknown project"):
        A.dispatch("provision_project_topics", {"slug": "no-such"})


def test_gc_project_topics_closes_all(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_topics

    fake = _FakeForumTg()
    cfg, _ = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    tg_topics.save(cfg, "test-project", {"feedback": 11, "deploy_logs": 22})
    out = A.dispatch("gc_project_topics", {"slug": "test-project"})
    assert out["ok"] is True
    assert set(out["closed"]) == {"feedback", "deploy_logs"}
    assert {c["thread_id"] for c in fake.closed} == {11, 22}
