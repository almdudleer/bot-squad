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
    # T-0403 removed autonomous_status/enable/disable (orchestrator cut).
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
        # T-0478 (M2/F2.4): user-conversation intake-session ensure/spawn.
        "ensure_user_conversation",
        "scheduler_state", "inject_input",
        # T-0469 (M1/F1.6): multiplexed queue-backed input channel.
        "send_input",
        # T-0153: autopilot — prompt-driven, time-boxed autonomous runs.
        "autopilot_start", "autopilot_stop", "autopilot_status",
        "peer_send", "peer_inbox_read", "peer_inbox_wait",
        # T-0498 (M6/F6.2): synchronous inter-session channel handshake + send.
        "sync_request", "sync_ack", "sync_enter", "sync_send",
        "sync_exit", "sync_status",
        "task_progress_add",
        # T-0589: on-demand short backlog digest for the TG conversation.
        "task_digest",
        # T-0463: assignment-interface write-result primitive (F1.1-d).
        "assignment_write_result",
        # T-0464: Routines — declare + list (M1-F1.1).
        "routine_declare", "routine_list",
        # T-0604 (D-0048 slice 2): mute a monitor routine (probes, never fires).
        "routine_mute",
        # T-0467: universal-compact role-agnostic forward-state save (F1.4).
        "compact_write_state",
        # T-0473: read-only operator state-doc transparency primitive (M2-F2.1).
        "operator_state_doc",
        # T-0522: user-facing operator re-drive pause toggle (T-0474 follow-up).
        "operator_pause", "operator_resume", "operator_status",
        # T-0630: fleet-default `claude --model` (~/.claude/settings.json).
        "fleet_model_get", "fleet_model_set",
        # T-0042: atomic T-NNNN allocator.
        "task_new",
        # T-0174: generalized atomic allocator across all entity types.
        "doc_new", "uc_new", "flow_new", "initiative_new",
        # Phase 9: bind multi-task-per-dev / multi-initiative-per-TL.
        "bind_task", "bind_initiative",
        # T-0237 Layer-2: operator-invoked reuse-vs-spawn dispatch decision.
        "dispatch_decision",
        # T-0576 (M11/F11.3): instant-tweak vs long-request placement guarantee.
        "placement_decision",
        # T-0184: per-session drift-check off-ramp (bsq drift on/off).
        "set_drift_paused",
        # T-0466: per-session ~1h cache-window recycle postpone (bsq postpone).
        "idle_postpone",
        # T-0509 (M11/F11.2): user-session role morph (user→dev/teamlead/operator).
        "morph_session",
        # Sessions polish batch (2026-05-13): unbind + archive lifecycle.
        "unbind_task", "unbind_initiative",
        # T-0324 (H2): safe primary re-home — the repair bind/unbind can't do.
        "rehome_primary",
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
        # T-0610: temporary page-channel switch (TG-primary / MAX reserve).
        "page_channel",
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

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None, debounce=True) -> bool:
        self.calls.append({
            "chat_id": chat_id, "text": text, "sid": sid, "user": user,
            "urgent": urgent, "topic_id": topic_id, "debounce": debounce,
        })
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


def test_tg_notify_debounce_defaults_true(tmp_config_dir, monkeypatch):
    """Backward-compat: callers that don't pass debounce keep the existing
    debounced-send behavior."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    A.dispatch("tg_notify", {"message": "hi"})
    assert fake.calls[0]["debounce"] is True


def test_tg_notify_debounce_false_forwarded(tmp_config_dir, monkeypatch):
    """T-0569: an interactive relay passes debounce=False so an identical
    payload isn't silently deduped — the flag must reach TgClient.send."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"chat_id": "555", "message": "hi", "debounce": False})
    assert out["ok"] is True
    assert fake.calls[0]["debounce"] is False


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
    # P2-03: MAX is send-only (no ingest) — the footer points at attach/board,
    # NOT a false "reply to this message" promise. build_escalation_text now
    # carries the "To answer:" affordance (locked by test_tg_stall.py).
    assert "To answer:" in text
    assert "Reply to this message" not in text


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


def test_tg_notify_routes_to_tg_even_when_max_configured(tmp_config_dir, monkeypatch):
    """T-0610 inversion: the DEFAULT stakeholder DM goes via TG (primary) even
    with [max].default_chat_id set — MAX is reserve-only now."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_notify", {"message": "stakeholder dm", "sid": "S-x-p1"})
    assert out == {"ok": True, "sent": True, "channel": "tg"}
    assert len(fake_tg.calls) == 1 and len(fake_max.calls) == 0
    assert fake_tg.calls[0]["text"] == "stakeholder dm"


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
    from bot_squad_worker.sessions import _write_session_metadata

    (tmp_config_dir / "auth.toml").write_text(
        '[users]\n'
        'alexey = "hash"\n'
        '\n'
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "404580642"\n'
    )
    (tmp_path / "data" / "test-project" / "_chat").mkdir(parents=True)
    _write_session_metadata(
        tmp_path / "data" / "test-project" / "sessions" / "S-alexey-ui-p0.md",
        {"sid": "S-alexey-ui-p0", "status": "active"},
    )
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
    # T-0644: the TG mirror carries the slug-qualified label, not the bare sid.
    assert call["sid"] == "[test-project] S-almdudleer-operator-p23"
    assert call["user"] == "alexey"


def test_peer_send_skips_tg_mirror_when_user_has_no_chat_id(tmp_path, tmp_config_dir, monkeypatch):
    """User present in user_meta but with no tg_chat_id → no TG call."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    (tmp_config_dir / "auth.toml").write_text(
        '[users]\n'
        'aqice = "hash"\n'
        '\n'
        '[user_meta.aqice]\n'
        'linux_user = "aqice"\n'
    )
    (tmp_path / "data" / "test-project" / "_chat").mkdir(parents=True)
    _write_session_metadata(
        tmp_path / "data" / "test-project" / "sessions" / "S-aqice-ui-p0.md",
        {"sid": "S-aqice-ui-p0", "status": "active"},
    )
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
    from bot_squad_worker.sessions import _write_session_metadata

    (tmp_config_dir / "auth.toml").write_text(
        '[user_meta.alexey]\n'
        'linux_user = "almdudleer"\n'
        'tg_chat_id = "404580642"\n'
    )
    (tmp_path / "data" / "test-project" / "_chat").mkdir(parents=True)
    _write_session_metadata(
        tmp_path / "data" / "test-project" / "sessions" / "S-almdudleer-operator-p23.md",
        {"sid": "S-almdudleer-operator-p23", "status": "active"},
    )
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
    from bot_squad_worker.sessions import _write_session_metadata

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
    _write_session_metadata(
        tmp_path / "data" / "test-project" / "sessions" / "S-alexey-ui-p0.md",
        {"sid": "S-alexey-ui-p0", "status": "active"},
    )
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
# T-0624: peer_send resolves the delivery slug from the RECIPIENT SID's own
# project (session-registry scan across all projects), not the sender's cwd.
# ---------------------------------------------------------------------------


def _two_project_config(tmp_path: Path) -> Config:
    """Two registered projects sharing one config/data root, no TG token needed."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.proj-a]\n'
        'slug = "proj-a"\n'
        'display_name = "Proj A"\n'
        'repo_path = "/tmp/proj-a"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
        '\n'
        '[projects.proj-b]\n'
        'slug = "proj-b"\n'
        'display_name = "Proj B"\n'
        'repo_path = "/tmp/proj-b"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    return Config.load(cfg_dir)


def test_peer_send_cross_project_lands_in_recipient_project(tmp_path, monkeypatch):
    """A send from proj-a to a proj-b SID must land (and be drainable) under
    proj-b's _chat/, not proj-a's — the T-0624 misfile scenario."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    recipient = "S-almdudleer-watchrobot-gate-launch-p62"
    _write_session_metadata(
        cfg.data_dir / "proj-b" / "sessions" / f"{recipient}.md",
        {"sid": recipient, "status": "active"},
    )

    out = A.dispatch("peer_send", {
        "slug": "proj-a",
        "from_sid": "S-almdudleer-operator-p23",
        "to": recipient,
        "text": "hello from the other project",
    })
    assert out["ok"] is True
    assert out["delivered_to"] == [recipient]

    # Landed under the RECIPIENT's project, not the sender's.
    assert (cfg.data_dir / "proj-b" / "_chat" / f"inbox-{recipient}.log").exists()
    assert not (cfg.data_dir / "proj-a" / "_chat" / f"inbox-{recipient}.log").exists()

    # Drainable exactly as the recipient's own `bsq inbox check` would (slug=proj-b).
    drained = A.dispatch("peer_inbox_read", {"slug": "proj-b", "sid": recipient})
    assert drained["count"] == 1
    assert "hello from the other project" in drained["messages"][0]

    # The sender's own project inbox never saw it.
    not_there = A.dispatch("peer_inbox_read", {"slug": "proj-a", "sid": recipient})
    assert not_there["count"] == 0


def test_peer_send_unresolvable_recipient_hard_errors(tmp_path, monkeypatch):
    """A literal-SID target with no registered session anywhere must hard-error,
    not silently misfile into the sender's project."""
    import bot_squad_worker.actions as A

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="no registered session"):
        A.dispatch("peer_send", {
            "slug": "proj-a",
            "from_sid": "S-almdudleer-operator-p23",
            "to": "S-nobody-ghost-p1",
            "text": "into the void",
        })
    assert not (cfg.data_dir / "proj-a" / "_chat" / "inbox-S-nobody-ghost-p1.log").exists()


def test_peer_send_role_fanout_still_scoped_to_sender_slug(tmp_path, monkeypatch):
    """Role-keyword targets (teamlead/dev/all) are an in-project broadcast —
    they keep using the caller's slug, never a per-recipient lookup."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    _write_session_metadata(
        cfg.data_dir / "proj-a" / "sessions" / "S-almdudleer-teamlead-p1.md",
        {"sid": "S-almdudleer-teamlead-p1", "status": "active", "task_id": "~"},
    )

    out = A.dispatch("peer_send", {
        "slug": "proj-a",
        "from_sid": "S-almdudleer-operator-p23",
        "to": "teamlead",
        "text": "status?",
    })
    assert out["delivered_to"] == ["S-almdudleer-teamlead-p1"]
    assert (cfg.data_dir / "proj-a" / "_chat" / "inbox-S-almdudleer-teamlead-p1.log").exists()


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
    # T-0458: the response echoes the to-be-built sha (origin/<branch> tip).
    # This fixture attaches no origin, so the key is present but "" (best-effort).
    assert "target_sha" in out


def test_deploy_action_echoes_target_sha(tmp_path, monkeypatch):
    """T-0458: with an origin attached, the deploy action echoes the to-be-built sha."""
    import subprocess
    import bot_squad_worker.actions as A
    from bot_squad_worker import deploy as _deploy

    cfg, repo = _make_deploy_config(tmp_path)
    # attach a bare origin whose tip == repo HEAD on the deploy branch
    subprocess.run(["git", "branch", "-M", "bot_squad/dev"], cwd=str(repo), check=True)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=str(repo), check=True)
    subprocess.run(["git", "push", "-q", "origin", "bot_squad/dev"], cwd=str(repo), check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    out = A.dispatch("deploy", {
        "slug": "deploy-test",
        "target": "staging",
        "reason": "smoke test",
        "requested_by": "pytest",
    })
    assert out["ok"] is True
    assert out["target_sha"] == head


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


def test_spawn_session_action_threads_model(tmp_path, monkeypatch):
    """T-0623: spawn_session accepts the optional model param and threads it
    to sessions.spawn (allowlist + wiring)."""
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
        "model": "claude-opus-4-8",
    })
    assert result["ok"] is True
    assert captured.get("model") == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# ensure_user_conversation action tests (T-0478, M2/F2.4)
# ---------------------------------------------------------------------------


def test_ensure_user_conversation_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("ensure_user_conversation", {
            "slug": "test-project", "global_user_id": "gu_a1", "evil": "x"})


def test_ensure_user_conversation_missing_required(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("ensure_user_conversation", {"slug": "test-project"})


def test_ensure_user_conversation_spawns_when_none_live(tmp_path, monkeypatch):
    """No live attendant ⟹ spawn one in a gid-keyed user-conversation window
    that derives the user-conversation role, with the boot prompt threaded."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured.update(slug=slug, window=window, initial_prompt=initial_prompt)
        return {"ok": True, "sid": f"S-u-{window}-p3"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "please add dark mode",
    })
    assert result["ok"] is True
    assert result["spawned"] is True
    assert captured["window"] == "gu_a1b2c3-user-conversation"
    # The window must derive the new role (end-to-end with _derive_role).
    assert S._derive_role(captured["window"], None, None) == "user-conversation"
    # Boot prompt orients the session: brief + verbatim mandate + the message.
    assert "bsq brief" in captured["initial_prompt"]
    assert "VERBATIM" in captured["initial_prompt"]
    assert "please add dark mode" in captured["initial_prompt"]
    assert result["sid"] == "S-u-gu_a1b2c3-user-conversation-p3"


def test_ensure_user_conversation_threads_explicit_model(tmp_path, monkeypatch):
    """T-0623: an explicit model param on ensure_user_conversation reaches
    sessions.spawn on the fresh-spawn path (absent → the role default applies
    inside spawn() itself, exercised in test_sessions.py)."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured.update(kw)
        return {"ok": True, "sid": f"S-u-{window}-p4"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "model": "claude-opus-4-8",
    })
    assert result["ok"] is True
    assert captured.get("model") == "claude-opus-4-8"


def test_ensure_user_conversation_reuses_live_attendant(tmp_path, monkeypatch):
    """A live attendant ⟹ route to it (best-effort wake), never spawn a dup."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    existing = "S-u-gu_a1b2c3-user-conversation-p9"

    def boom_spawn(*a, **k):
        raise AssertionError("must NOT spawn when an attendant is already live")

    nudged = {}
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: existing)
    monkeypatch.setattr(S, "spawn", boom_spawn)
    monkeypatch.setattr(A, "_action_inject_input",
                        lambda params: nudged.update(params) or {"ok": True})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "another message",
    })
    assert result == {"ok": True, "sid": existing, "spawned": False}
    assert nudged["sid"] == existing  # the live attendant was nudged


def test_ensure_user_conversation_reuse_survives_nudge_failure(tmp_path, monkeypatch):
    """A pane-timing hiccup on the wake nudge must not fail the ensure (the
    message is already durable in the store)."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    existing = "S-u-gu_x-user-conversation-p1"
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: existing)

    def boom_inject(params):
        raise ActionError("no live pane")

    monkeypatch.setattr(A, "_action_inject_input", boom_inject)
    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_x", "message_ref": "hi"})
    assert result == {"ok": True, "sid": existing, "spawned": False}


def test_ensure_user_conversation_unknown_slug(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unknown project"):
        A.dispatch("ensure_user_conversation", {
            "slug": "nope", "global_user_id": "gu_a1"})


def test_ensure_user_conversation_validates_gid(tmp_path, monkeypatch):
    """A crafted gid can't smuggle a shell/tmux metacharacter into the spawn."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    with pytest.raises(ActionError, match="invalid global_user_id"):
        A.dispatch("ensure_user_conversation", {
            "slug": "test-project", "global_user_id": "gu;rm -rf /"})


def test_ensure_user_conversation_concurrent_no_fanout(tmp_path, monkeypatch):
    """T-0478 (REOPENED) regression: two RAPID/CONCURRENT ensure calls for the
    SAME (slug, gid) must yield EXACTLY ONE spawned attendant + one md — the
    per-(slug,gid) flock serializes check-and-spawn and the rewritten md-scan
    reuse lets the second call find the first's just-spawned session. Before the
    fix this fanned out two sessions (and two duplicate verbatim tickets) for a
    single user's burst. The fake spawn widens the check→spawn window with a
    sleep, so without the flock both threads would spawn and this test fails."""
    import threading
    import time as _time
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    sess_dir = cfg.data_dir / "test-project" / "sessions"
    live: set[str] = set()
    spawn_calls: list[str] = []
    lk = threading.Lock()

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        # Mirror real spawn: write the seed md + mark the pane live so the reuse
        # md-scan can find a JUST-spawned attendant; the sleep widens the race.
        with lk:
            n = len(spawn_calls) + 1
            spawn_calls.append(window)
        _time.sleep(0.05)
        sid = f"S-u-{window}-p{n}"
        (sess_dir / f"{sid}.md").write_text(f"---\nsid: {sid}\n---\n")
        live.add(sid)
        return {"ok": True, "sid": sid}

    monkeypatch.setattr(S, "spawn", fake_spawn)
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set(live))
    monkeypatch.setattr(A, "_action_inject_input", lambda params: {"ok": True})

    results: dict[int, dict] = {}

    def call(idx):
        results[idx] = A.dispatch("ensure_user_conversation", {
            "slug": "test-project", "global_user_id": "gu_race",
            "message_ref": f"msg{idx}"})

    t1 = threading.Thread(target=call, args=(1,))
    t2 = threading.Thread(target=call, args=(2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(spawn_calls) == 1, f"fan-out: spawned {len(spawn_calls)} times"
    sids = {results[1]["sid"], results[2]["sid"]}
    assert len(sids) == 1, f"two different attendants: {sids}"
    assert sorted([results[1]["spawned"], results[2]["spawned"]]) == [False, True]
    mds = [p for p in sess_dir.glob("*.md")
           if S._window_from_sid(p.stem) == "gu_race-user-conversation"]
    assert len(mds) == 1, f"expected 1 user-conversation md, got {len(mds)}"


def _seed_recycled_attendant(cfg, gid: str, *, sid_pane="p4", uuid="uu-att"):
    """A recycle-v2 remembered attendant md for gid (T-0566 stamp shape)."""
    import bot_squad_worker.sessions as S
    sid = f"S-u-{gid}-user-conversation-{sid_pane}"
    S._write_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / f"{sid}.md", {
            "sid": sid, "status": "suspended",
            "window": f"{gid}-user-conversation", "cwd": "/tmp",
            "claude_uuid": uuid, "suspended_at": "2026-07-04T10:00:00Z",
            "resumable": True, "recycled_at": "2026-07-04T10:00:00Z",
            "resume_hint": "idle cache-window recycle (compacted)",
        })
    return sid


def test_ensure_user_conversation_resumes_recycled_attendant(tmp_path, monkeypatch):
    """T-0575: a recycled (compact-terminate-remembered) attendant for this gid
    under the <50k budget is RESUMED with a wake prompt — never a fresh spawn."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    sid = _seed_recycled_attendant(cfg, "gu_r1")

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "recycled_resume_eligible",
                        lambda uuid, user_home=None: (True, 12_000))

    captured = {}

    def fake_resume(cfg, slug, sid, initial_prompt=None, **kw):
        captured.update(slug=slug, sid=sid, initial_prompt=initial_prompt)
        return {"ok": True, "sid": sid.replace("-p4", "-p9")}

    def boom_spawn(*a, **k):
        raise AssertionError("must resume the recycled attendant, not spawn")

    monkeypatch.setattr(S, "resume", fake_resume)
    monkeypatch.setattr(S, "spawn", boom_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_r1",
        "message_ref": "hello again"})
    assert result["ok"] is True
    assert result["spawned"] is False
    assert result["resumed"] is True
    assert captured["sid"] == sid
    assert "RESUMED" in captured["initial_prompt"]
    assert "hello again" in captured["initial_prompt"]


def test_ensure_user_conversation_fat_recycled_attendant_spawns_fresh(
        tmp_path, monkeypatch):
    """T-0575: remembered context ≥50k → the stakeholder rule says fresh spawn."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    _seed_recycled_attendant(cfg, "gu_fat")

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "recycled_resume_eligible",
                        lambda uuid, user_home=None: (False, 120_000))

    def boom_resume(*a, **k):
        raise AssertionError("must NOT resume an over-budget session")

    monkeypatch.setattr(S, "resume", boom_resume)
    monkeypatch.setattr(
        S, "spawn",
        lambda cfg, slug, window, initial_prompt=None, **kw:
            {"ok": True, "sid": f"S-u-{window}-p8"})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_fat",
        "message_ref": "hi"})
    assert result["ok"] is True
    assert result["spawned"] is True


def test_ensure_user_conversation_ignores_recycled_other_user(tmp_path, monkeypatch):
    """A recycled attendant of a DIFFERENT gid (or a recycled dev) never
    matches — the window key scopes the resume preference to the same user."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    _seed_recycled_attendant(cfg, "gu_other")

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)

    def boom_resume(*a, **k):
        raise AssertionError("must NOT resume another user's attendant")

    monkeypatch.setattr(S, "resume", boom_resume)
    monkeypatch.setattr(
        S, "spawn",
        lambda cfg, slug, window, initial_prompt=None, **kw:
            {"ok": True, "sid": f"S-u-{window}-p8"})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_mine",
        "message_ref": "hi"})
    assert result["spawned"] is True


def test_ensure_user_conversation_resume_failure_falls_back_to_spawn(
        tmp_path, monkeypatch):
    """T-0575 DoD: a failing resume (dead uuid, tmux hiccup) that left no live
    attendant falls back to a FRESH spawn — the ensure never errors out."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    _seed_recycled_attendant(cfg, "gu_dead")

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "recycled_resume_eligible",
                        lambda uuid, user_home=None: (True, 1_000))

    def boom_resume(*a, **k):
        raise ActionError("resume: tmux new-window failed")

    monkeypatch.setattr(S, "resume", boom_resume)
    monkeypatch.setattr(
        S, "spawn",
        lambda cfg, slug, window, initial_prompt=None, **kw:
            {"ok": True, "sid": f"S-u-{window}-p8"})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_dead",
        "message_ref": "hi"})
    assert result["ok"] is True
    assert result["spawned"] is True


def test_ensure_user_conversation_late_resume_failure_returns_live_attendant(
        tmp_path, monkeypatch):
    """T-0575: a LATE resume failure (composer-ready timeout — the pane IS up)
    must return the now-live attendant, not spawn a duplicate next to it."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    _seed_recycled_attendant(cfg, "gu_late")
    live_after = "S-u-gu_late-user-conversation-p9"

    calls = {"n": 0}

    def live_sid(cfg, slug, gid):
        calls["n"] += 1
        # First call (reuse check): nothing live; after the resume attempt the
        # resurrected pane IS live even though prompt delivery timed out.
        return None if calls["n"] == 1 else live_after

    monkeypatch.setattr(S, "live_user_conversation_sid", live_sid)
    monkeypatch.setattr(S, "recycled_resume_eligible",
                        lambda uuid, user_home=None: (True, 1_000))

    def late_boom_resume(*a, **k):
        raise ActionError("resume: claude composer never showed ❯")

    def boom_spawn(*a, **k):
        raise AssertionError("must NOT spawn a duplicate next to the live pane")

    monkeypatch.setattr(S, "resume", late_boom_resume)
    monkeypatch.setattr(S, "spawn", boom_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_late",
        "message_ref": "hi"})
    assert result["ok"] is True
    assert result["spawned"] is False
    assert result["resumed"] is True
    assert result["sid"] == live_after


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


def _patch_inject_transport(monkeypatch, tmp_path):
    """Patch the T-0578 mux transport seams; returns the send-keys call log."""
    import subprocess
    import bot_squad_worker.input_mux as M
    import bot_squad_worker.sessions as S

    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(M, "_capture_pane", lambda pane_id: "")  # not typing
    monkeypatch.setattr(M, "_DIRECT_INTERLINE_PAUSE_SEC", 0.0)
    return run_calls


def test_inject_input_happy_path(tmp_path, monkeypatch):
    """Happy path: pane found, delivery routed through the input_mux direct
    lane (T-0578) — send-keys emitted once per line, verbatim."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    from bot_squad_worker.sessions import PaneInfo

    _make_inject_cfg(tmp_path, monkeypatch)

    # Mock list_panes to return a matching pane
    # SID: S-testuser-specwin-p5 → compute_sid("testuser", "specwin", "%5")
    fake_pane = PaneInfo(pane_id="%5", window="specwin", pid="1234", cwd="/tmp", command="claude")
    monkeypatch.setattr(S, "list_panes", lambda: [fake_pane])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    run_calls = _patch_inject_transport(monkeypatch, tmp_path)

    result = A.dispatch("inject_input", {"sid": "S-testuser-specwin-p5", "text": "hello"})
    assert result["ok"] is True
    assert result["pane_id"] == "%5"
    assert result["lines_sent"] == 1
    # Golden keystroke sequence — byte-identical to the pre-T-0578 raw loop:
    # the text (verbatim, uncaptioned) then a SEPARATE Enter.
    send_keys = [c for c in run_calls if "send-keys" in c]
    assert send_keys == [
        ["tmux", "send-keys", "-t", "%5", "--", "hello"],
        ["tmux", "send-keys", "-t", "%5", "Enter"],
    ]


def test_inject_input_multiline(tmp_path, monkeypatch):
    """Multi-line text sends one send-keys per line."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    from bot_squad_worker.sessions import PaneInfo

    _make_inject_cfg(tmp_path, monkeypatch)

    fake_pane = PaneInfo(pane_id="%7", window="win", pid="1111", cwd="/tmp", command="bash")
    monkeypatch.setattr(S, "list_panes", lambda: [fake_pane])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    run_calls = _patch_inject_transport(monkeypatch, tmp_path)

    result = A.dispatch("inject_input", {"sid": "S-testuser-win-p7", "text": "line1\nline2\nline3"})
    assert result["lines_sent"] == 3
    # 2 send-keys calls per line (text + Enter sent separately so Enter submits
    # outside tmux's bracketed-paste — see input_mux.deliver_direct)
    send_keys = [c for c in run_calls if "send-keys" in c]
    assert len(send_keys) == 6


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


def test_task_progress_add_long_note_roundtrips_byte_identical(tmp_path, monkeypatch):
    # Regression for F-2026-07-05-bsq-30844bca41: `bsq ticket note` silently
    # truncated at 240 chars, clipping sacred stakeholder verbatims (T-0566).
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    task_path = backlog / "T-0001-foo.md"
    task_path.write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nI want X.\n"
    )
    note = ("stakeholder said this exact sacred thing " * 8).strip()
    assert len(note) > 300
    out = A.dispatch("task_progress_add", {
        "slug": "test-project",
        "task_id": "T-0001",
        "sid": "S-test-p1",
        "text": note,
    })
    assert out["ok"] is True
    assert out["line_appended"].endswith(f" · {note}")
    content = task_path.read_text()
    assert f"S-test-p1 · {note}" in content


def test_task_progress_add_overflow_raises_and_leaves_file_untouched(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    task_path = backlog / "T-0001-foo.md"
    original = (
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\n"
        "## Verbatim request\n\nI want X.\n"
    )
    task_path.write_text(original)
    with pytest.raises(ActionError, match="cap"):
        A.dispatch("task_progress_add", {
            "slug": "test-project",
            "task_id": "T-0001",
            "sid": "S-test-p1",
            "text": "x" * 5000,
        })
    assert task_path.read_text() == original


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


def test_task_progress_add_skips_tombstone_with_mismatched_id(tmp_path, monkeypatch):
    """T-0231: a renumbered ticket's tombstone shares the numeric filename
    prefix but declares a non-matching id: — the note must land on the real
    ticket even if the tombstone sorts first alphabetically."""
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    (backlog / "T-0030-aaa-tombstone.md").write_text(
        "---\nid: T-0030-DUPLICATE-DO-NOT-USE\ntitle: Wrong\nstatus: closed\n---\n\nbody\n"
    )
    real = backlog / "T-0030-zzz-real.md"
    real.write_text(
        "---\nid: T-0030\ntitle: Real\nstatus: open\n---\n\n"
        "## Verbatim request\n\nI want X.\n"
    )
    out = A.dispatch("task_progress_add", {
        "slug": "test-project",
        "task_id": "T-0030",
        "sid": "S-test-p1",
        "text": "noted",
    })
    assert out["ok"] is True
    assert "noted" in real.read_text()
    assert "noted" not in (backlog / "T-0030-aaa-tombstone.md").read_text()


def test_task_progress_add_genuine_collision_raises(tmp_path, monkeypatch):
    """Two files genuinely declaring the same id: — must fail loudly instead
    of silently writing the note onto whichever sorts first."""
    import bot_squad_worker.actions as A

    cfg, backlog = _make_task_progress_cfg(tmp_path, monkeypatch)
    (backlog / "T-0030-a.md").write_text(
        "---\nid: T-0030\ntitle: A\nstatus: open\n---\n\nbody\n"
    )
    (backlog / "T-0030-b.md").write_text(
        "---\nid: T-0030\ntitle: B\nstatus: open\n---\n\nbody\n"
    )
    with pytest.raises(ActionError, match="T-0030"):
        A.dispatch("task_progress_add", {
            "slug": "test-project",
            "task_id": "T-0030",
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
    out = A.dispatch("task_new", {"slug": "test-project", "title": "first task", "provenance": "T-0519"})
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
    out = A.dispatch("task_new", {"slug": "test-project", "title": "next one", "provenance": "T-0519"})
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
            {"slug": "test-project", "title": f"concurrent task {i}", "provenance": "T-0519"},
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
        "provenance": "T-0519",
        "initiative": "multi-server-installation-process.md",
        "priority": "P1",
        "owner": "alexey",
    })
    body = Path(out["file_path"]).read_text()
    assert 'initiative: "multi-server-installation-process.md"' in body
    assert 'priority: "P1"' in body
    assert 'owner: "alexey"' in body


def test_task_new_canonicalizes_bare_initiative_to_md(tmp_path, tmp_config_dir, monkeypatch):
    """T-0424 canonicalize-on-store: a bare-stem --initiative is persisted in the
    .md FILE form so it matches the initiative file + the FE option.basename (no
    false '(not found)'). 'ui-polish' and 'ui-polish.md' both store 'ui-polish.md'."""
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "bare init", "provenance": "T-0519", "initiative": "ui-polish",
    })
    assert 'initiative: "ui-polish.md"' in Path(out["file_path"]).read_text()


def test_task_new_quotes_titles_with_yaml_specials(tmp_path, tmp_config_dir, monkeypatch):
    """Titles with ':' or '#' must not break the YAML frontmatter."""
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    title = 'Fix: cache miss in #ingest path'
    out = A.dispatch("task_new", {"slug": "test-project", "title": title, "provenance": "T-0519"})
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
        A.dispatch("task_new", {"slug": "no-such", "title": "x", "provenance": "T-0519"})


def test_task_new_uses_shared_counter(tmp_path, tmp_config_dir, monkeypatch):
    """T-0174: task_new now allocates from data/<slug>/_counters/task.txt —
    the same counter the API uses, so the two can't hand out the same id."""
    import bot_squad_worker.actions as A

    _setup_task_new(tmp_path, tmp_config_dir, monkeypatch)
    A.dispatch("task_new", {"slug": "test-project", "title": "one", "provenance": "T-0519"})
    counter = tmp_path / "data" / "test-project" / "_counters" / "task.txt"
    assert counter.read_text().strip() == "1"
    A.dispatch("task_new", {"slug": "test-project", "title": "two", "provenance": "T-0519"})
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


def test_initiative_new_mints_kind_initiative_task(tmp_path, tmp_config_dir, monkeypatch):
    """T-0480 3b-1: initiative_new mints a kind:initiative TASK (in backlog),
    not a legacy vision/initiatives file — initiatives ARE tasks now."""
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("initiative_new", {"slug": "test-project", "name": "Billing Revamp"})
    assert out["id"].startswith("T-")          # a real task id, not INI-NN
    assert out["kind"] == "initiative"
    p = Path(out["file_path"])
    assert p.parent == proj / "backlog"        # lands in the task store
    body = p.read_text()
    assert f"id: {out['id']}" in body
    assert "kind: initiative" in body
    assert "status: open" in body
    assert "provenance:" in body               # post-cutoff task → needs provenance
    # NOT written as a legacy vision file
    assert not (proj / "vision" / "initiatives").exists()
    assert "initiative_kind" not in body   # default (one-shot) omits the field


def test_initiative_new_persistent_sets_initiative_kind(tmp_path, tmp_config_dir, monkeypatch):
    """T-0354: persistent=True stamps initiative_kind: persistent on the stub."""
    import bot_squad_worker.actions as A

    _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("initiative_new", {
        "slug": "test-project", "name": "Prod Support", "persistent": True,
    })
    assert out["initiative_kind"] == "persistent"
    body = Path(out["file_path"]).read_text()
    assert "initiative_kind: persistent" in body


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


def test_provision_project_topics_partial_failure_persists_created(tmp_config_dir, monkeypatch):
    """T-0442: a mid-loop createForumTopic failure must NOT lose the topics already
    created. Persist incrementally + per-topic try (matching gc's resilience at
    :468), so a retry creates only the still-missing classes instead of orphaning
    the created TG threads and duplicating them."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_topics

    class _FlakyForumTg(_FakeForumTg):
        def __init__(self, fail_after: int) -> None:
            super().__init__()
            self._fail_after = fail_after
            self._n = 0

        def create_forum_topic(self, *, chat_id, name) -> int:
            self._n += 1
            if self._n > self._fail_after:
                raise RuntimeError("TG flaked mid-provision")
            return super().create_forum_topic(chat_id=chat_id, name=name)

    # 1st create succeeds, the rest raise.
    fake = _FlakyForumTg(fail_after=1)
    cfg, _ = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)

    # Best-effort: does NOT propagate the create failure...
    out = A.dispatch("provision_project_topics", {"slug": "test-project"})
    persisted = tg_topics.load(cfg, "test-project")
    # ...and the one it managed to create IS persisted (not lost).
    assert len(persisted) == 1 and len(out["created"]) == 1
    first_class = out["created"][0]
    first_tid = persisted[first_class]

    # Retry with a healthy client → creates ONLY the still-missing classes, and
    # never re-creates (duplicates) the one that survived round 1.
    fake2 = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake2)
    out2 = A.dispatch("provision_project_topics", {"slug": "test-project"})
    final = tg_topics.load(cfg, "test-project")
    assert set(final) == {"feedback", "deploy_logs", "team_queries"}
    assert final[first_class] == first_tid          # original id preserved, not recreated
    assert first_class not in out2["created"]        # retry skipped the survivor
    assert len(fake2.created) == 2                    # only the 2 missing were created


def test_provision_project_topics_unknown_slug_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="unknown project"):
        A.dispatch("provision_project_topics", {"slug": "no-such"})


def test_provision_project_topics_no_tg_chat_raises(tmp_config_dir, monkeypatch):
    """T-0386 flag-off-safe: a fresh / DM-only project with no supergroup
    configured raises a CLEAR error (not a malformed empty-chat_id API call) —
    the create_project hook catches it best-effort so provisioning is skipped,
    never blocking project creation."""
    import dataclasses
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    proj = dataclasses.replace(cfg.projects["test-project"], tg_chat="")
    monkeypatch.setattr(A, "_get_config",
                        lambda: dataclasses.replace(cfg, projects={"test-project": proj}))
    with pytest.raises(ActionError, match="no tg_chat supergroup"):
        A.dispatch("provision_project_topics", {"slug": "test-project"})
    assert fake.created == []  # no API call attempted with an empty chat_id


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


# ---------------------------------------------------------------------------
# T-0394 / Audit Item 2 → T-0610 inversion: _send_stakeholder_dm SSOT helper.
# TG-primary / MAX reserve (auto-failover or temporary stakeholder switch),
# one page = one delivery, short-form pages. The personal pagers route here.
# ---------------------------------------------------------------------------

def test_send_stakeholder_dm_tg_primary_even_with_max_configured(tmp_config_dir, monkeypatch):
    """T-0610: with MAX configured, the page STILL goes TG-first — the
    MAX-primary premise (DPI-blocked TG) died 2026-07-04."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A._send_stakeholder_dm(A._get_config(), message="page", sid="S-x-p1", tg_chat_id="-100")
    assert out["channel"] == "tg" and out["sent"] is True
    assert len(fake_tg.calls) == 1 and len(fake_max.calls) == 0


def test_send_stakeholder_dm_prefer_tg_skips_max(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A._send_stakeholder_dm(A._get_config(), message="grp", tg_chat_id="-100", tg_topic_id=5, prefer_tg=True)
    assert out["channel"] == "tg"
    assert len(fake_tg.calls) == 1 and len(fake_max.calls) == 0
    assert fake_tg.calls[0]["topic_id"] == 5


def test_send_stakeholder_dm_tg_when_max_unconfigured(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A._send_stakeholder_dm(A._get_config(), message="hi", tg_chat_id="-100")
    assert out["channel"] == "tg" and len(fake_tg.calls) == 1 and len(fake_max.calls) == 0


def test_send_stakeholder_dm_no_duplicate_group_record(tmp_config_dir, monkeypatch):
    """T-0610 DoD: one page = ONE delivery. group_record is a compat no-op —
    the old MAX-ping + TG-group-record pair was the stakeholder's duplicate
    complaint; the TG-primary delivery already lands in the group/topic."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A._send_stakeholder_dm(A._get_config(), message="needs you", sid="S-x-p1",
                                 tg_chat_id="-100", tg_topic_id=77, group_record=True)
    assert out["channel"] == "tg"
    assert len(fake_tg.calls) == 1 and len(fake_max.calls) == 0
    assert fake_tg.calls[0]["topic_id"] == 77


def test_send_stakeholder_dm_failover_to_max_on_tg_error(tmp_config_dir, monkeypatch):
    """T-0610: TG raising (e.g. DPI-block returns) fails over to the MAX
    reserve instead of losing the page."""
    import bot_squad_worker.actions as A

    class _BoomTg:
        def send(self, **kw):
            raise RuntimeError("tg DPI-block / ConnectTimeout")

    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, _, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: _BoomTg())
    out = A._send_stakeholder_dm(A._get_config(), message="hi", sid="S-x-p1", tg_chat_id="-100")
    assert out["channel"] == "max" and out["sent"] is True
    assert len(fake_max.calls) == 1


def test_send_stakeholder_dm_empty_tg_chat_goes_to_max(tmp_config_dir, monkeypatch):
    """T-0610 sweep: an empty tg_chat_id is not a silent no-op — the page
    delivers via the MAX reserve."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    out = A._send_stakeholder_dm(A._get_config(), message="hi", tg_chat_id="")
    assert out["channel"] == "max" and len(fake_max.calls) == 1 and len(fake_tg.calls) == 0


def test_send_stakeholder_dm_no_channel_is_loud_not_silent(tmp_config_dir, monkeypatch, caplog):
    """T-0610 sweep: neither channel available → ok=False + channel 'none' +
    a WARNING — never a silent drop."""
    import logging as _logging
    import bot_squad_worker.actions as A
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    with caplog.at_level(_logging.WARNING):
        out = A._send_stakeholder_dm(A._get_config(), message="hi", tg_chat_id="")
    assert out == {"ok": False, "sent": False, "channel": "none"}
    assert len(fake_tg.calls) == 0 and len(fake_max.calls) == 0
    assert any("NO channel delivered" in r.message for r in caplog.records)


def test_send_stakeholder_dm_mode_max_temporary_switch(tmp_config_dir, monkeypatch):
    """T-0610: the stakeholder's temporary 'max' mode routes pages via MAX
    (one delivery, no TG copy); 'auto' reverts to TG-primary."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    cfg = A._get_config()
    A._set_page_mode(cfg, "max", by="stakeholder")
    out = A._send_stakeholder_dm(cfg, message="hi", tg_chat_id="-100", group_record=True)
    assert out["channel"] == "max"
    assert len(fake_max.calls) == 1 and len(fake_tg.calls) == 0
    A._set_page_mode(cfg, "auto")
    out2 = A._send_stakeholder_dm(cfg, message="hi again", tg_chat_id="-100")
    assert out2["channel"] == "tg" and len(fake_tg.calls) == 1


def test_send_stakeholder_dm_slims_long_pages(tmp_config_dir, monkeypatch):
    """T-0610: pages are short-form — a verbose work summary is cut at a
    boundary with an explicit continuation pointer.

    T-0635: on a single-project install the pointer is a real staging-web
    link (built from the sole registered project), not the bare words
    "см. задачу/тред"."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    verbose = "Сводка по работе. " + "Сделал шаг и проверил результат. " * 40
    A._send_stakeholder_dm(A._get_config(), message=verbose, tg_chat_id="-100")
    sent_text = fake_tg.calls[0]["text"]
    assert len(sent_text) < len(verbose)
    assert len(sent_text) <= A._PAGE_SLIM_LIMIT + 80
    assert "детали: см. задачу/тред" not in sent_text
    assert "https://staging.example.com/p/test-project/sessions" in sent_text


def test_slim_page_appends_real_link_when_given(tmp_config_dir, monkeypatch):
    """T-0635: _slim_page appends the caller-supplied link verbatim instead of
    the plain-text 'см. задачу/тред' pointer."""
    import bot_squad_worker.actions as A
    long_text = "Заголовок. " + "Много подробностей подряд. " * 40
    out = A._slim_page(long_text, link="https://staging.example.com/p/test-project/t/T-1")
    assert out.endswith("https://staging.example.com/p/test-project/t/T-1")
    assert "см. задачу/тред" not in out


def test_slim_page_falls_back_to_generic_text_without_a_link(tmp_config_dir, monkeypatch):
    """T-0635: an unresolvable link (e.g. multi-project install, no chat
    match) falls back to the old generic pointer rather than a dangling
    'подробнее: ' with nothing after it."""
    import bot_squad_worker.actions as A
    long_text = "Заголовок. " + "Много подробностей подряд. " * 40
    out = A._slim_page(long_text)
    assert out.endswith("(детали: см. задачу/тред)")


def test_send_stakeholder_dm_link_targets_explicit_task_id(tmp_config_dir, monkeypatch):
    """T-0635: when the call site has a task_id on hand (tg_notify's optional
    ``task_id`` param, threaded through to the SSOT), the pointer links
    straight to that task."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    long_text = "Заголовок. " + "Много подробностей подряд. " * 40
    # No explicit chat_id/topic_id — an explicit target is a group/forum
    # address (prefer_tg, never slimmed); slug-based default routing is what
    # actually exercises the slim+link path.
    A.dispatch("tg_notify", {"message": long_text, "slug": "test-project",
                              "task_id": "T-0635"})
    sent = fake_tg.calls[0]["text"]
    assert "https://staging.example.com/p/test-project/t/T-0635" in sent


def test_send_stakeholder_dm_link_scoped_to_real_session_sid(tmp_config_dir, monkeypatch):
    """T-0635: without a task_id, a real session sid (S-...) scopes the
    sessions-page link; a synthetic label sid (e.g. "autopilot") does not."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    long_text = "Заголовок. " + "Много подробностей подряд. " * 40
    A._send_stakeholder_dm(A._get_config(), message=long_text, tg_chat_id="-100",
                            sid="S-almdudleer-dev-p1")
    sent = fake_tg.calls[0]["text"]
    assert "https://staging.example.com/p/test-project/sessions?sid=S-almdudleer-dev-p1" in sent

    A._send_stakeholder_dm(A._get_config(), message=long_text, tg_chat_id="-100",
                            sid="autopilot")
    sent2 = fake_tg.calls[1]["text"]
    assert sent2.endswith("https://staging.example.com/p/test-project/sessions")


def test_send_stakeholder_dm_link_present_via_max_transport_too(tmp_config_dir, monkeypatch):
    """T-0635 DoD: the link must resolve for both TG and MAX transports (the
    T-0610 page-mode switch) — it's baked into ``message`` before the
    tg/max branch split, so a MAX-routed page carries it too."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, _, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: _BoomTg())
    long_text = "Заголовок. " + "Много подробностей подряд. " * 40
    out = A._send_stakeholder_dm(A._get_config(), message=long_text, sid="S-x-p1",
                                  tg_chat_id="-100")
    assert out["channel"] == "max"
    sent = fake_max.calls[0]["text"]
    assert "https://staging.example.com/p/test-project/sessions?sid=S-x-p1" in sent


def test_send_stakeholder_dm_prefer_tg_never_slimmed(tmp_config_dir, monkeypatch):
    """T-0610 review P3: an explicitly-addressed send (prefer_tg — e.g. the
    T-0569 conversation-relay reply to the stakeholder's DM) is conversational
    content, not a page — it must arrive untruncated."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    reply = "Развёрнутый ответ по треду. " + "Деталь и обоснование решения. " * 40
    A._send_stakeholder_dm(A._get_config(), message=reply, tg_chat_id="404", prefer_tg=True)
    assert fake_tg.calls[0]["text"] == reply  # byte-identical, no cap


def test_tg_notify_needs_input_footer_survives_slim(tmp_config_dir, monkeypatch):
    """T-0610 review P2-1: on a needs-input page, the QUESTION is slimmed but
    the tmux-attach escalation footer survives — a blanket slim after
    composition would cut the footer off the end."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_stall as TS
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    monkeypatch.setattr(A, "_resolve_tmux_session", lambda c, slug, sid: "bot-squad")
    monkeypatch.setattr(
        TS, "build_escalation_text",
        lambda cfg, sid, text, session: f"{text}\n\n▶ tmux attach -t {session}")
    long_question = "Нужен твой выбор по деплою. " + "Контекст решения и варианты. " * 40
    out = A.dispatch("tg_notify", {"message": long_question, "sid": "S-x-p1",
                                   "needs_input": True})
    assert out["channel"] == "tg"
    sent = fake_tg.calls[0]["text"]
    assert "tmux attach -t bot-squad" in sent          # footer survived
    assert len(sent) < len(long_question)              # question was slimmed
    assert "детали: см. задачу/тред" in sent           # via _slim_page, not a raw cut


def test_page_channel_action_set_read_persists(tmp_config_dir, monkeypatch):
    """T-0610: page_channel action sets/reads the mode; state survives via the
    data/_worker/page_channel.json file; bad mode rejected."""
    import pytest as _pytest
    import bot_squad_worker.actions as A
    from bot_squad_worker.actions import ActionError as _AE
    _inject_both_channels(monkeypatch, tmp_config_dir)
    assert A.dispatch("page_channel", {})["mode"] == "auto"
    out = A.dispatch("page_channel", {"mode": "max", "by": "stakeholder"})
    assert out["ok"] is True and out["mode"] == "max"
    assert A.dispatch("page_channel", {})["mode"] == "max"
    assert A._page_mode_path(A._get_config()).exists()
    with _pytest.raises(_AE, match="mode must be one of"):
        A.dispatch("page_channel", {"mode": "smoke-signals"})
    assert A.dispatch("page_channel", {"mode": "auto"})["mode"] == "auto"


def test_send_stakeholder_dm_mode_max_falls_back_to_tg_when_max_down(tmp_config_dir, monkeypatch):
    """Even under the temporary 'max' mode, a MAX outage falls back to TG —
    the page always prefers delivery over mode fidelity."""
    import bot_squad_worker.actions as A

    class _BoomMax:
        def send(self, **kw):
            raise RuntimeError("max down")

    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir, max_client=_BoomMax())
    cfg = A._get_config()
    A._set_page_mode(cfg, "max", by="stakeholder")
    out = A._send_stakeholder_dm(cfg, message="hi", tg_chat_id="-100", group_record=True)
    assert out["channel"] == "tg" and len(fake_tg.calls) == 1


# ---------------------------------------------------------------------------
# T-0509 (M11/F11.2): morph_session action — user-session role morph.
# ---------------------------------------------------------------------------

def _seed_user_session(cfg, repo, sid, *, window="claude"):
    from bot_squad_worker.sessions import _write_session_metadata
    md = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    _write_session_metadata(md, {
        "sid": sid, "status": "active", "window": window, "cwd": str(repo),
        "claude_uuid": "u-" + sid[-2:], "task_id": "~", "initiative": "~",
        "started_at": "2026-06-28T00:00:00Z",
    })
    return md


def test_morph_session_action_to_dev(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    cfg, repo = _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    _seed_user_session(cfg, repo, "S-u-claude-p1")

    out = A.dispatch("morph_session", {
        "slug": "test-project", "sid": "S-u-claude-p1",
        "role": "dev", "task_id": "T-0042",
    })
    assert out["ok"] and out["role"] == "dev" and out["task_id"] == "T-0042"


def test_morph_session_action_operator_singleton(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    cfg, repo = _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    _seed_user_session(cfg, repo, "S-u-operator-p9", window="operator")
    _seed_user_session(cfg, repo, "S-u-claude-p1")

    with pytest.raises(ActionError, match="operator already running"):
        A.dispatch("morph_session", {
            "slug": "test-project", "sid": "S-u-claude-p1", "role": "operator",
        })


def test_morph_session_action_rejects_extra_params(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("morph_session", {
            "slug": "test-project", "sid": "S-u-claude-p1", "role": "dev",
            "bogus": 1,
        })


def test_morph_session_action_requires_role(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    _make_sessions_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("morph_session", {"slug": "test-project", "sid": "S-u-claude-p1"})
