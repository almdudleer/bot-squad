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
        # T-0639: runtime (chat_id,thread_id)->project topic-binding surface.
        "tg_topic_bind", "tg_topic_unbind", "tg_topic_list",
        # T-0660: create-and-bind a forum topic in one step + rename General
        # + close-on-done.
        "tg_topic_create", "tg_topic_rename_general", "tg_topic_rename",
        "tg_topic_close_for_ticket",
        # T-0799: per-message-TYPE outbound destination map — which chat/topic
        # each automated message class is delivered to (default: unchanged).
        "msg_route_list", "msg_route_set", "msg_route_clear",
        "pause_deploys", "resume_deploys",
        "list_sessions", "telemetry_get",
        "pause_session", "suspend_session", "resume_session",
        "spawn_session",
        # T-0909: the model-dispatch compliance number, off the shared ledger
        # every spawn/resume appends to (`bsq model compliance`).
        "model_compliance",
        # T-0478 (M2/F2.4): user-conversation intake-session ensure/spawn.
        "ensure_user_conversation",
        "scheduler_state", "inject_input",
        # T-0924: `bsq compact` — arms the handoff instead of a bare /compact.
        "compact",
        # T-0770: the BLOCK sibling of inject_input — one composer submission.
        "inject_prompt",
        # T-0759: read-only liveness of the outbound log (ok/idle/decayed/blind).
        "outbound_liveness",
        # T-0469 (M1/F1.6): multiplexed queue-backed input channel.
        "send_input",
        # T-0153: autopilot — prompt-driven, time-boxed autonomous runs.
        "autopilot_start", "autopilot_stop", "autopilot_status",
        # T-0929: the project drive state + the "is anything automatic running"
        # read — the ultimate off-switch's surface.
        "automation_status", "automation_set_state",
        "peer_send", "peer_inbox_read", "peer_inbox_wait",
        # T-0498 (M6/F6.2): synchronous inter-session channel handshake + send.
        "sync_request", "sync_ack", "sync_enter", "sync_send",
        "sync_exit", "sync_status",
        "task_progress_add",
        # T-0767: the working area + stakeholder-quote writers, so a session
        # has somewhere to put durable detail other than the progress feed.
        # T-0863 added the third: the one-paragraph status HE reads.
        "task_context_set", "task_summary_set", "task_stakeholder_note_add",
        # T-0938: attribution-only companion to the four writers above — it
        # tells the ticket-update fan-out who made a change that never reached
        # a worker action (`bsq ticket update`'s client-side status patch).
        "ticket_author_note",
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
        # T-0942: the doc is the PROJECT's, not the operator's, and it now has
        # a WRITE half (locked + CAS). The old name stays registered so a `bsq`
        # or api from before the deploy keeps resolving.
        "work_state_doc", "work_state_write", "operator_state_doc",
        # T-0522: user-facing operator re-drive pause toggle (T-0474 follow-up).
        "operator_pause", "operator_resume", "operator_status",
        # T-0783a: which board tickets are takeable / need triage / are excluded.
        "pickup_queue",
        # T-0630: fleet-default `claude --model` (~/.claude/settings.json).
        "fleet_model_get", "fleet_model_set",
        # T-0042: atomic T-NNNN allocator.
        "task_new",
        # T-0174: generalized atomic allocator across all entity types.
        "doc_new", "initiative_new",
        # Phase 9: bind multi-task-per-dev / multi-initiative-per-TL.
        "bind_task", "bind_initiative",
        # T-0237 Layer-2: operator-invoked reuse-vs-spawn dispatch decision.
        "dispatch_decision",
        # T-0576 (M11/F11.3): instant-tweak vs long-request placement guarantee.
        "placement_decision",
        # T-0855: direct (user-session drives devs) vs operator tier — the
        # scaling ladder's first rung. Backs `bsq route`.
        "topology_decision",
        # T-0932: gradual budding — the WRITE side of that same ladder. The
        # read (which rung, should this session bud) and the one deliberate
        # spawn a session performs on the operator rung. Back `bsq bud`.
        "budding_decision", "bud_operator",
        # T-0937: the operator SEAT — the ladder's OTHER move on that rung, a
        # live root driving the board itself. Back `bsq operator seat`.
        "operator_seat_claim", "operator_seat_release",
        # T-0184: per-session drift-check off-ramp (bsq drift on/off).
        "set_drift_paused",
        # T-0926 follow-up: per-session pin against every automatic action
        # (bsq pin on/off).
        # T-0655: operator's own drive=on/off continuity toggle (bsq drive on/off).
        "set_drive",
        # T-0678: durable per-session `claude --model` override (bsq model set/status).
        "set_model",
        # T-0466: per-session ~1h cache-window recycle postpone (bsq postpone).
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
        # T-0662: human-readable label -> session SID aliases.
        "session_alias_set", "session_alias_remove",
        "session_alias_resolve", "session_alias_list",
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

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None,
             debounce=True, route_sid="", delivery=None) -> bool:
        # T-0719: `route_sid` is the RAW routing sid behind the display `sid`
        # label — recorded so tests can pin that reply-routing gets the real
        # session, not the (compact, sid-less) label.
        self.calls.append({
            "chat_id": chat_id, "text": text, "sid": sid, "user": user,
            "urgent": urgent, "topic_id": topic_id, "debounce": debounce,
            "route_sid": route_sid,
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


# --- T-0667: slug-resolved sends (bsq tg ping / stall escalations) follow
# the conversation LOCUS ahead of the project's static tg_chat/tg_topic_id ---

def test_tg_notify_slug_prefers_locus_over_static_tg_chat(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 42)

    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "999888777"
    assert fake.calls[0]["topic_id"] == 42


def test_tg_notify_slug_falls_back_to_static_when_no_locus(tmp_path, monkeypatch):
    """No locus recorded yet for this slug -> unchanged pre-T-0667 behavior:
    the project's static tg_chat/tg_topic_id."""
    import bot_squad_worker.actions as A

    cfg_dir = _config_dir_with_topic(tmp_path)
    _, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "-1001234567890"
    assert fake.calls[0]["topic_id"] == 99


def test_tg_notify_explicit_topic_id_overrides_locus(tmp_path, monkeypatch):
    """An explicit topic_id param still wins over the locus's thread_id."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 42)

    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi", "topic_id": 7})
    assert fake.calls[0]["chat_id"] == "999888777"
    assert fake.calls[0]["topic_id"] == 7


def test_tg_notify_slug_locus_scoped_to_slug(tmp_path, monkeypatch):
    """A locus recorded for a DIFFERENT slug must not leak into this one."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "some-other-project", "gu_1", "111", 1)

    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "-1001234567890"


def test_tg_notify_explicit_chat_id_overrides_locus(tmp_path, monkeypatch):
    """An explicit chat_id param bypasses slug resolution (and locus) entirely."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 42)

    A.dispatch("tg_notify", {"chat_id": "555", "slug": "group-project", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "555"
    assert fake.calls[0]["topic_id"] is None


# --- T-0660 Phase 2: direct-write into a task's topic (`bsq topic say`) ---
# resolves chat_id/topic_id from the ticket's bound topic.

def test_tg_notify_ticket_id_resolves_task_topic(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1003761939853", 42, "group-project",
                            ticket_id="T-0700", session_id="S-dev-p9")

    A.dispatch("tg_notify", {"ticket_id": "T-0700", "message": "on it", "sid": "S-dev-p9"})
    assert fake.calls[0]["chat_id"] == "-1003761939853"
    assert fake.calls[0]["topic_id"] == 42
    # T-0676 item 4: no `slug` param was given, but the ticket's bound topic
    # names one — the sender label now carries it (compact '<slug> <role>'
    # style, item 5), instead of degrading to the bare unattributed sid.
    assert fake.calls[0]["sid"] == "group-project dev"


def test_tg_notify_ticket_id_unbound_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="no topic bound to ticket"):
        A.dispatch("tg_notify", {"ticket_id": "T-9999", "message": "hi"})


def test_tg_notify_explicit_topic_id_overrides_ticket_binding(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1003761939853", 42, "group-project", ticket_id="T-0700")

    A.dispatch("tg_notify", {"ticket_id": "T-0700", "message": "hi", "topic_id": 7})
    assert fake.calls[0]["topic_id"] == 7


def test_tg_notify_explicit_chat_id_overrides_ticket_id(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1003761939853", 42, "group-project", ticket_id="T-0700")

    A.dispatch("tg_notify", {"ticket_id": "T-0700", "chat_id": "555", "message": "hi"})
    assert fake.calls[0]["chat_id"] == "555"
    assert fake.calls[0]["topic_id"] is None


# --- T-0660 Phase 2 mechanic #3: a task-topic direct-write ALSO records a
# passive FYI append into the project's attendant thread, so the attendant
# keeps context without treating it as its own actionable inbox item. ---

def test_tg_notify_ticket_id_direct_write_records_fyi(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings, conversation_locus, tg_listener as TL

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1003761939853", 42, "group-project",
                            ticket_id="T-0700", session_id="S-dev-p9")
    # The attendant thread this task's project has been talking to (T-0667 locus).
    conversation_locus.set_locus(cfg, "group-project", "gu_stake", "999", None)

    fyi_calls = []
    monkeypatch.setattr(
        TL, "append_conversation_fyi",
        lambda cfg, slug, gid, *, author, text: fyi_calls.append(
            {"slug": slug, "gid": gid, "author": author, "text": text}
        ),
    )

    A.dispatch("tg_notify", {"ticket_id": "T-0700", "message": "on it, fixing now",
                             "sid": "S-dev-p9"})

    assert len(fyi_calls) == 1
    call = fyi_calls[0]
    assert call["slug"] == "group-project"
    assert call["gid"] == "gu_stake"
    assert call["author"] == "session:S-dev-p9"
    assert "on it, fixing now" in call["text"]
    assert "T-0700" in call["text"]


def test_tg_notify_ticket_id_direct_write_no_locus_skips_fyi(tmp_path, monkeypatch):
    """No locus recorded yet for this project (never talked to it via TG) ->
    no gid to record under — best-effort skip, the send itself still succeeds."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings, tg_listener as TL

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1003761939853", 42, "group-project", ticket_id="T-0700")

    fyi_calls = []
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda *a, **k: fyi_calls.append((a, k)))

    out = A.dispatch("tg_notify", {"ticket_id": "T-0700", "message": "hi"})
    assert out["sent"] is True
    assert fyi_calls == []


def test_tg_notify_non_ticket_send_never_records_fyi(tmp_config_dir, monkeypatch):
    """A plain slug/chat_id send (no ticket_id) is NOT a task-topic
    direct-write — must never trigger the FYI mechanic."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_listener as TL

    _inject_fake_tg(monkeypatch, tmp_config_dir)
    fyi_calls = []
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda *a, **k: fyi_calls.append((a, k)))

    A.dispatch("tg_notify", {"slug": "test-project", "message": "hi"})
    assert fyi_calls == []


def test_tg_notify_rejects_non_integer_topic(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir)
    with pytest.raises(ActionError, match="topic_id must be an integer"):
        A.dispatch("tg_notify", {"message": "hi", "topic_id": "abc"})


# ---------------------------------------------------------------------------
# T-0723: a session that OWNS a topic posts THERE, not into whatever topic the
# stakeholder last wrote in. The destination precedence ladder (see
# _action_tg_notify's "DESTINATION PRECEDENCE" comment — this block IS its
# encoding, so the order can't quietly drift back into branch order):
#
#   explicit chat_id/topic_id > ticket_id param > SENDER'S OWN topic >
#   topic class > conversation locus > project static default
# ---------------------------------------------------------------------------

def _dev_session(cfg, sid: str, *, slug: str = "group-project",
                 task_id: str = "~", extra_task_ids=None) -> None:
    """Write a dev SessionMd so `sid` resolves to its task binding(s)."""
    from bot_squad_worker.sessions import _write_session_metadata
    sdir = cfg.data_dir / slug / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sdir / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "dev-window",
        "task_id": task_id, "initiative": "~",
        "extra_task_ids": extra_task_ids or [],
    })


def test_tg_notify_task_bound_sender_beats_locus(tmp_path, monkeypatch):
    """THE T-0723 regression: the locus points at topic A ([BS] General, where
    the stakeholder last typed) while the sending dev owns topic B (its T-0660
    per-task topic). The message must land in B."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "-1001234567890", 5)   # topic A
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project",
                            ticket_id="T-0723")                                       # topic B
    _dev_session(cfg, "S-u-dev-p9", task_id="T-0723")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "progress",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["topic_id"] == 77
    assert fake.calls[0]["chat_id"] == "-1001234567890"


def test_tg_notify_own_topic_resolved_from_extra_task_ids(tmp_path, monkeypatch):
    """A bundled/BIND_TASK dev owns its extra tasks' topics too."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "-1001234567890", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", 88, "group-project",
                            ticket_id="T-0999")
    _dev_session(cfg, "S-u-dev-p9", task_id="T-0723", extra_task_ids=["T-0999"])

    A.dispatch("tg_notify", {"slug": "group-project", "message": "progress",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["topic_id"] == 88


def test_tg_notify_direct_mode_pinned_topic_beats_locus(tmp_path, monkeypatch):
    """T-0677 direct mode: a topic pinned to this session is its own topic even
    with no ticket bound to it — no SessionMd task binding needed."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "-1001234567890", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", 61, "group-project")
    tg_bindings.set_direct_session(cfg, "-1001234567890", 61, "S-u-oper-p2",
                                   pinned_message_id=444)

    A.dispatch("tg_notify", {"slug": "group-project", "message": "status",
                             "sid": "S-u-oper-p2"})

    assert fake.calls[0]["topic_id"] == 61


def test_tg_notify_topicless_sender_still_follows_locus(tmp_path, monkeypatch):
    """Do NOT regress T-0667: a sender with no topic of its own (`bsq tg ping`
    from a plain session, a project-level escalation) keeps following the
    conversation — including when OTHER sessions own topics."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project",
                            ticket_id="T-0723", session_id="S-u-other-p1")
    _dev_session(cfg, "S-u-dev-p9", task_id="~")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "need you",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["chat_id"] == "999888777"
    assert fake.calls[0]["topic_id"] == 5


def test_tg_notify_sidless_send_still_follows_locus(tmp_path, monkeypatch):
    """No sid at all (the admin test-ping, an API-initiated project send) has no
    identity to resolve a topic from — locus, unchanged."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project",
                            ticket_id="T-0723")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "test ping"})

    assert fake.calls[0]["chat_id"] == "999888777"
    assert fake.calls[0]["topic_id"] == 5


def test_tg_notify_explicit_topic_id_beats_own_topic(tmp_path, monkeypatch):
    """Rung 1 > rung 3: a caller that spelled the thread out means it."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project",
                            session_id="S-u-dev-p9")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi",
                             "sid": "S-u-dev-p9", "topic_id": 7})

    assert fake.calls[0]["topic_id"] == 7


def test_tg_notify_explicit_chat_id_beats_own_topic(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project",
                            session_id="S-u-dev-p9")

    A.dispatch("tg_notify", {"slug": "group-project", "chat_id": "555",
                             "message": "hi", "sid": "S-u-dev-p9"})

    assert fake.calls[0]["chat_id"] == "555"
    assert fake.calls[0]["topic_id"] is None


def test_tg_notify_own_topic_beats_topic_class(tmp_path, monkeypatch):
    """Rung 3 > rung 4: the sender's own topic outranks a class thread."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings, tg_topics

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_topics.save(cfg, "group-project", {"feedback": 12})
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project",
                            session_id="S-u-dev-p9")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "hi",
                             "sid": "S-u-dev-p9", "topic": "feedback"})

    assert fake.calls[0]["topic_id"] == 77


def test_tg_notify_topic_class_beats_locus(tmp_path, monkeypatch):
    """Rung 4 > rung 5: a class-routed send belongs in the project's thread for
    that class, in the project's OWN chat — not wherever the human last wrote
    (which used to preempt the class entirely)."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_topics

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_topics.save(cfg, "group-project", {"deploy_logs": 12})
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 5)

    A.dispatch("tg_notify", {"slug": "group-project", "message": "deploy ok",
                             "topic": "deploy_logs"})

    assert fake.calls[0]["chat_id"] == "-1001234567890"
    assert fake.calls[0]["topic_id"] == 12


def test_tg_notify_own_topic_beats_static_default(tmp_path, monkeypatch):
    """Rung 3 > rung 6: with no locus recorded at all, the owned topic still
    wins over the project's static tg_chat/tg_topic_id."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    tg_bindings.set_binding(cfg, "-1009999999999", 77, "group-project",
                            ticket_id="T-0723")
    _dev_session(cfg, "S-u-dev-p9", task_id="T-0723")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "progress",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["chat_id"] == "-1009999999999"
    assert fake.calls[0]["topic_id"] == 77


def test_tg_notify_own_topic_never_crosses_projects(tmp_path, monkeypatch):
    """The slug is a GUARD on rung 3: a topic bound under a DIFFERENT project is
    never a valid destination for this project's send — it falls through to the
    locus instead."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "some-other-project",
                            session_id="S-u-dev-p9")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "progress",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["chat_id"] == "999888777"
    assert fake.calls[0]["topic_id"] == 5


def test_tg_notify_general_feed_binding_is_not_an_own_topic(tmp_path, monkeypatch):
    """A (chat_id, None) General-feed binding naming this session is not a topic
    of its own — such a sender keeps the locus behaviour."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", None, "group-project",
                            session_id="S-u-dev-p9")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "progress",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["chat_id"] == "999888777"
    assert fake.calls[0]["topic_id"] == 5


def test_tg_notify_own_task_topic_records_the_fyi(tmp_path, monkeypatch):
    """Landing in a task's topic is a task-topic direct-write however it was
    resolved, so it earns the same T-0660 mechanic #3 FYI append — carrying the
    ticket the BINDING named (no `ticket_id` param was passed)."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings, tg_listener as TL

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_stake", "999888777", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project",
                            ticket_id="T-0723")
    _dev_session(cfg, "S-u-dev-p9", task_id="T-0723")

    fyi_calls = []
    monkeypatch.setattr(
        TL, "append_conversation_fyi",
        lambda cfg, slug, gid, *, author, text: fyi_calls.append(
            {"gid": gid, "author": author, "text": text}),
    )

    A.dispatch("tg_notify", {"slug": "group-project", "message": "progress",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["topic_id"] == 77
    assert len(fyi_calls) == 1
    assert fyi_calls[0]["author"] == "session:S-u-dev-p9"
    assert "T-0723" in fyi_calls[0]["text"]


def test_tg_notify_own_topic_survives_an_unreadable_session_md(tmp_path, monkeypatch):
    """Routing must never fail on session state: a garbage SessionMd degrades to
    "no topic of my own" (locus), not to an exception."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "999888777", 5)
    sdir = cfg.data_dir / "group-project" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "S-u-dev-p9.md").write_text("not: [valid, frontmatter\n")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "progress",
                             "sid": "S-u-dev-p9"})

    assert fake.calls[0]["chat_id"] == "999888777"


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

class _RecordingBoomTg:
    """A TG client that RECORDS each send and then fails the way a DPI-block
    does — the double for every "TG is down, fail over to MAX" test.

    The record is the point. ``_send_stakeholder_dm`` swallows exceptions from
    the tg client BY DESIGN, so ``out["channel"] == "max"`` is equally true when
    the double was never reached at all: T-0789 found
    ``test_send_stakeholder_dm_link_present_via_max_transport_too`` passing
    because its monkeypatch lambda referenced an out-of-scope name, and the
    resulting NameError was caught as the TG failure the test wanted. A tripwire
    that only RAISES cannot discriminate inside that try/except; ``calls`` can.
    Assert on it, not on the channel alone.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._exc = exc or RuntimeError("tg DPI-block / ConnectTimeout")

    def send(self, **kw):
        self.calls.append(kw)
        raise self._exc


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
    # T-0644: the TG mirror carries the slug-qualified label, not the bare
    # sid. T-0676 item 5: compact '<slug> <role>' style.
    assert call["sid"] == "test-project operator"
    assert call["user"] == "alexey"


def test_peer_send_over_cap_raises_and_mirrors_nothing(tmp_path, tmp_config_dir, monkeypatch):
    """T-0827: the AGENT-facing layer must fail loudly, not return a 200.

    The old path returned ok/delivered_to for a message it had cut mid-word and
    the CLI printed "sent to 1 inbox(es)" — indistinguishable from a whole
    send. It also fired the TG mirror with the sender's FULL text, so the
    stakeholder saw content the recipient never got. Both are asserted here:
    the call raises, and the mirror never fires.
    """
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

    with pytest.raises(A.ActionError) as exc:
        A.dispatch("peer_send", {
            "slug": "test-project",
            "from_sid": "S-almdudleer-operator-p23",
            "to": "S-alexey-ui-p0",
            "text": "q" * 4200,
        })
    assert "4200" in str(exc.value) and "refusing to truncate" in str(exc.value)
    assert fake.calls == []
    inbox = tmp_path / "data" / "test-project" / "_chat" / "inbox-S-alexey-ui-p0.log"
    assert not inbox.exists() or inbox.read_bytes() == b""


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
# T-0790: a target SID superseded by a RECYCLE passed the T-0624 guard (the
# predecessor's md still exists) and returned 200 having written into an inbox
# nobody drains. Same silent-success signature, different resolution path:
# T-0624 was cross-PROJECT misfiling by slug, this is same-project misfiling by
# STALE SESSION.
# ---------------------------------------------------------------------------


def test_peer_send_to_recycled_sid_delivers_to_the_live_successor(tmp_path, monkeypatch):
    """RED PIN, end-to-end through the action: the two lost Ponytail relays.

    Sent at operator `…-p374` (recycled at 16:20Z), they must reach `…-p455`,
    the operator actually on duty — and be drainable by that session's own
    `bsq inbox check`.
    """
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    dead, live = "S-almdudleer-operator-p374", "S-almdudleer-operator-p455"
    for sid, status in ((dead, "suspended"), (live, "active")):
        _write_session_metadata(
            cfg.data_dir / "proj-a" / "sessions" / f"{sid}.md",
            {"sid": sid, "status": status, "window": "operator", "task_id": "~"},
        )

    out = A.dispatch("peer_send", {
        "slug": "proj-a",
        "from_sid": "S-almdudleer-gu_x-user-conversation-p5",
        "to": dead,
        "text": "Ponytail findings, verbatim",
    })

    assert out["delivered_to"] == [live]
    assert out["redirected"] == {"from": dead, "to": live, "reason": "recycled"}
    assert not (cfg.data_dir / "proj-a" / "_chat" / f"inbox-{dead}.log").exists()
    drained = A.dispatch("peer_inbox_read", {"slug": "proj-a", "sid": live})
    assert drained["count"] == 1
    assert "Ponytail findings, verbatim" in drained["messages"][0]


def test_peer_send_to_archived_sid_with_no_successor_refuses(tmp_path, monkeypatch):
    """RED PIN — an ARCHIVED target's sidecars were already reaped by
    `archive_session`, so a write mints a fresh file nobody owns and returns 200.
    No routing can save it, so the action refuses instead."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    gone = "S-almdudleer-worker-old-ticket-p12"
    _write_session_metadata(
        cfg.data_dir / "proj-a" / "sessions" / f"{gone}.md",
        {"sid": gone, "status": "suspended", "archived": "true",
         "window": "worker-old-ticket", "task_id": "T-0001"},
    )

    with pytest.raises(ActionError, match="ARCHIVED"):
        A.dispatch("peer_send", {
            "slug": "proj-a",
            "from_sid": "S-almdudleer-operator-p23",
            "to": gone,
            "text": "into the void",
        })
    assert not (cfg.data_dir / "proj-a" / "_chat" / f"inbox-{gone}.log").exists()


def test_peer_send_operator_keyword_reaches_the_live_operator(tmp_path, monkeypatch):
    """RED PIN — `operator` is a role keyword, so it must skip the T-0624
    per-recipient slug lookup (which hard-errored on it) and fan out in-project."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    live = "S-almdudleer-operator-p455"
    _write_session_metadata(
        cfg.data_dir / "proj-a" / "sessions" / f"{live}.md",
        {"sid": live, "status": "active", "window": "operator", "task_id": "~"},
    )

    out = A.dispatch("peer_send", {
        "slug": "proj-a",
        "from_sid": "S-almdudleer-worker-x-p1",
        "to": "operator",
        "text": "needs a human look",
    })

    assert out["delivered_to"] == [live]
    assert not (cfg.data_dir / "proj-a" / "_chat" / "inbox-operator.log").exists()


def test_peer_send_archived_sid_with_a_successor_is_redirected_not_refused(
    tmp_path, monkeypatch,
):
    """RED PIN (it asserts the redirect, so it fails at 423a058) whose job is to
    fence the refusal's PRECEDENCE: refusing is the last resort, not the first,
    so an archived target whose window has a live holder still gets delivered."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    dead, live = "S-almdudleer-operator-p374", "S-almdudleer-operator-p455"
    _write_session_metadata(
        cfg.data_dir / "proj-a" / "sessions" / f"{dead}.md",
        {"sid": dead, "status": "suspended", "archived": "true",
         "window": "operator", "task_id": "~"},
    )
    _write_session_metadata(
        cfg.data_dir / "proj-a" / "sessions" / f"{live}.md",
        {"sid": live, "status": "active", "window": "operator", "task_id": "~"},
    )

    out = A.dispatch("peer_send", {
        "slug": "proj-a",
        "from_sid": "S-almdudleer-worker-x-p1",
        "to": dead,
        "text": "still lands",
    })

    assert out["delivered_to"] == [live]


def test_peer_send_to_a_live_sid_is_unchanged(tmp_path, monkeypatch):
    """GREEN GUARD — the common case must be byte-identical: delivered to the
    SID addressed, no redirect field, no refusal."""
    import bot_squad_worker.actions as A
    from bot_squad_worker.sessions import _write_session_metadata

    cfg = _two_project_config(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    live = "S-almdudleer-worker-some-ticket-p7"
    _write_session_metadata(
        cfg.data_dir / "proj-a" / "sessions" / f"{live}.md",
        {"sid": live, "status": "active", "window": "worker-some-ticket",
         "task_id": "T-0002"},
    )

    out = A.dispatch("peer_send", {
        "slug": "proj-a",
        "from_sid": "S-almdudleer-operator-p23",
        "to": live,
        "text": "READY T-0002",
    })

    assert out["delivered_to"] == [live]
    assert "redirected" not in out


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

    # T-0754: the SAME value is what got persisted. The echo and the payload are
    # one resolution — resolving twice around a push landing in between would let
    # /api/health interpret drift against a commit the requester never saw.
    import json
    queue_dir = cfg.data_dir / "deploy-test" / "_jobs" / "deploy" / "queue"
    payload = json.loads(next(queue_dir.glob("*.json")).read_text())
    assert payload["target_sha"] == out["target_sha"] == head


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
    """No live attendant ⟹ spawn one under the USER-FACING window (T-0964)
    that derives the user-conversation role, with the gid on the md field and
    the boot prompt threaded."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured.update(slug=slug, window=window, initial_prompt=initial_prompt,
                        **kw)
        return {"ok": True, "sid": f"S-u-{window}-p3"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "please add dark mode",
    })
    assert result["ok"] is True
    assert result["spawned"] is True
    # T-0964: the name the USER reads — never the gu_… id.
    assert captured["window"] == S.UNIVERSAL_WINDOW == "universal_bsq_session"
    assert "gu_a1b2c3" not in captured["window"]
    # …and the gid it used to carry now rides the md field instead.
    assert captured["global_user_id"] == "gu_a1b2c3"
    # The window must derive the new role (end-to-end with _derive_role).
    assert S._derive_role(captured["window"], None, None) == "user-conversation"
    # Boot prompt orients the session: brief + verbatim mandate + the message.
    assert "bsq brief" in captured["initial_prompt"]
    assert "VERBATIM" in captured["initial_prompt"]
    assert "please add dark mode" in captured["initial_prompt"]
    assert result["sid"] == "S-u-universal_bsq_session-p3"


def test_ensure_user_conversation_injects_group_prompt(tmp_path, monkeypatch):
    """T-0591 (F5.10): a user bound to a project group gets that group's
    prompt folded into the boot prompt — the consumption seam
    project_groups_store.group_for_user (T-0496) was built for but never had
    a caller."""
    import json

    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _ = _make_sessions_cfg(tmp_path, monkeypatch)
    (cfg.data_dir / "test-project" / "groups.json").write_text(json.dumps({
        "version": 1,
        "groups": [{"id": "grp_1", "name": "support", "role": "support",
                    "access_scope": "scoped help", "prompt": "Only answer FAQ."}],
        "memberships": {"gu_a1b2c3": "grp_1"},
    }))
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured["initial_prompt"] = initial_prompt
        return {"ok": True, "sid": f"S-u-{window}-p3"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
    })
    assert result["ok"] is True
    assert "support" in captured["initial_prompt"]
    assert "Only answer FAQ." in captured["initial_prompt"]


def test_ensure_user_conversation_no_group_omits_group_block(tmp_path, monkeypatch):
    """No groups.json / no membership ⟹ boot prompt is unchanged (default
    treatment, matching pre-T-0591 behaviour)."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured["initial_prompt"] = initial_prompt
        return {"ok": True, "sid": f"S-u-{window}-p3"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_nogroup",
    })
    assert result["ok"] is True
    assert "Group-specific instructions" not in captured["initial_prompt"]


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


def test_ensure_user_conversation_thread_id_in_fresh_spawn_boot_prompt(tmp_path, monkeypatch):
    """T-0676 items 3/6: a thread_id on the ensure call tells the (still
    single, per-(slug,gid)) attendant to read/reply into THAT bound topic's
    isolated thread instead of the whole mixed project history."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured["initial_prompt"] = initial_prompt
        return {"ok": True, "sid": f"S-u-{window}-p3"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "hi", "thread_id": 7,
    })
    assert result["ok"] is True
    # T-0795: the id is now HIGHLIGHTED on its own line rather than named
    # mid-prose as "(thread_id 7)". This assertion used to read
    # `"thread_id 7" in ...`, which a buried id satisfies — the exact rendered
    # line is pinned in test_ping_ids_highlight.py.
    assert "▶ TOPIC ID: 7" in captured["initial_prompt"]
    assert "thread_id=7" in captured["initial_prompt"]


def test_ensure_user_conversation_no_thread_id_boot_prompt_unaffected(tmp_path, monkeypatch):
    """Absent thread_id (DM / non-topic message) -> the boot prompt carries
    no thread-scoped block at all — byte-identical to before this change."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured["initial_prompt"] = initial_prompt
        return {"ok": True, "sid": f"S-u-{window}-p3"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3", "message_ref": "hi",
    })
    assert result["ok"] is True
    assert "BOUND FORUM TOPIC" not in captured["initial_prompt"]
    assert "thread_id" not in captured["initial_prompt"]


def test_ensure_user_conversation_thread_id_in_nudge_for_live_attendant(tmp_path, monkeypatch):
    """A thread_id on the ensure call, routed to an ALREADY-live attendant,
    must be named in the nudge text so the SAME session knows which topic's
    isolated thread the new message belongs to."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    existing = "S-u-gu_a1b2c3-user-conversation-p9"
    nudged = {}
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: existing)
    monkeypatch.setattr(A, "_action_inject_input",
                        lambda params: nudged.update(params) or {"ok": True})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "another message", "thread_id": 7,
    })
    assert result == {"ok": True, "sid": existing, "spawned": False}
    # T-0795, same substitution as the boot-prompt test above: highlighted, not
    # buried. The full nudge string is pinned in test_ping_ids_highlight.py.
    assert "▶ TOPIC ID: 7" in nudged["text"]
    assert "thread_id=7" in nudged["text"]


def test_ensure_user_conversation_no_thread_id_nudge_unchanged(tmp_path, monkeypatch):
    """Absent thread_id -> the exact pre-T-0676 generic nudge text."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    existing = "S-u-gu_a1b2c3-user-conversation-p9"
    nudged = {}
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: existing)
    monkeypatch.setattr(A, "_action_inject_input",
                        lambda params: nudged.update(params) or {"ok": True})

    A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "another message",
    })
    assert nudged["text"] == (
        "A new message arrived in your user-conversation thread — read it and respond."
    )


# ---------------------------------------------------------------------------
# T-0775: the conversation-read URL these prompts hand the attendant
#
# All three sites named `GET /api/conversations/<slug>/<gid>/messages`, which is
# mounted nowhere — routes_conversations is included with prefix `/api/m`, so
# an attendant that followed the prompt got a hard 404 (measured on the live
# install with a valid worker token). The T-0676 tests above pass either way:
# they assert `thread_id 7` and `thread_id=7`, never the route, which is how a
# broken URL sat in the boot prompt of the system's highest-traffic session.
#
# Two wrong answers are adjacent and both look quiet from inside the session:
# the bare prefix 404s, and the session-auth `/api/m/conversations/...` UI
# surface answers a worker Bearer with 401 — so half-correcting the prefix
# trades a loud failure for a silent one. These pin the delivered STRING; the
# route it names is resolved against a real build_app() by
# api/tests/test_worker_api_paths_mounted.py.
# ---------------------------------------------------------------------------

_WORKER_CONV_ROUTE = "/api/m/worker/conversations/test-project/gu_a1b2c3/messages"


def _boot_prompt_via_dispatch(monkeypatch, **params) -> str:
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    captured = {}

    def fake_spawn(cfg, slug, window, initial_prompt=None, **kw):
        captured["initial_prompt"] = initial_prompt
        return {"ok": True, "sid": f"S-u-{window}-p3"}

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)
    monkeypatch.setattr(S, "spawn", fake_spawn)
    assert A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3", **params,
    })["ok"] is True
    return captured["initial_prompt"]


def test_boot_prompt_names_the_worker_token_conversation_route(tmp_path, monkeypatch):
    _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.delenv("WORKER_API_BASE_URL", raising=False)
    monkeypatch.delenv("MOTHERSHIP_BASE_URL", raising=False)
    prompt = _boot_prompt_via_dispatch(monkeypatch, message_ref="hi")

    assert f"GET {_WORKER_CONV_ROUTE}" in prompt
    # The two wrong answers, by construction: the bare prefix (404) and the
    # session-auth UI surface (401 for a worker Bearer). Neither may appear.
    assert "/api/conversations/" not in prompt
    assert "/api/m/conversations/" not in prompt
    # The route needs a Bearer, so a correct path with no auth hint is the
    # 401 again one step later.
    assert "WORKER_API_TOKEN" in prompt


def test_boot_prompt_uses_the_configured_api_base_not_a_hardcoded_host(tmp_path, monkeypatch):
    """The base comes from the same accessor the worker's working callers use
    (tg_listener._api_base_url), so a moved API host moves this prompt with it."""
    _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKER_API_BASE_URL", "http://api.internal:9999")
    prompt = _boot_prompt_via_dispatch(monkeypatch, message_ref="hi")
    assert f"GET http://api.internal:9999{_WORKER_CONV_ROUTE}" in prompt


def test_boot_prompt_degrades_to_a_relative_path_when_base_unset(tmp_path, monkeypatch):
    """Unset env yields "" — the prompt must fall back to the correct RELATIVE
    path, never to a wrong absolute one."""
    _make_sessions_cfg(tmp_path, monkeypatch)
    monkeypatch.delenv("WORKER_API_BASE_URL", raising=False)
    monkeypatch.delenv("MOTHERSHIP_BASE_URL", raising=False)
    prompt = _boot_prompt_via_dispatch(monkeypatch, message_ref="hi")
    assert f"GET {_WORKER_CONV_ROUTE}\n" in prompt


def test_thread_scoped_block_names_the_worker_token_route(tmp_path, monkeypatch):
    """T-0676's topic-scoped read — same defect, plus the ?thread_id= parameter
    the mounted GET actually accepts (measured 200 on the live install)."""
    _make_sessions_cfg(tmp_path, monkeypatch)
    prompt = _boot_prompt_via_dispatch(monkeypatch, message_ref="hi", thread_id=7)
    assert f"GET {_WORKER_CONV_ROUTE}?thread_id=7" in prompt
    assert "/api/conversations/" not in prompt


def test_reuse_path_nudge_names_the_worker_token_route(tmp_path, monkeypatch):
    """The third site: a live attendant nudged about a new topic message. It
    already holds its contract, so this one carries the route only."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_sessions_cfg(tmp_path, monkeypatch)
    existing = "S-u-gu_a1b2c3-user-conversation-p9"
    nudged = {}
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: existing)
    monkeypatch.setattr(A, "_action_inject_input",
                        lambda params: nudged.update(params) or {"ok": True})

    A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "another message", "thread_id": 7,
    })
    assert f"GET {_WORKER_CONV_ROUTE}?thread_id=7" in nudged["text"]
    assert "/api/conversations/" not in nudged["text"]


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
        # T-0964: the seed md carries `global_user_id` — that field, not the
        # window, is what the reuse scan matches now, so a fake that omits it
        # models a producer that no longer exists and fans out here.
        with lk:
            n = len(spawn_calls) + 1
            spawn_calls.append(window)
        _time.sleep(0.05)
        sid = f"S-u-{window}-p{n}"
        gid_line = (f"global_user_id: {kw['global_user_id']}\n"
                    if kw.get("global_user_id") else "")
        (sess_dir / f"{sid}.md").write_text(
            f"---\nsid: {sid}\nwindow: {window}\n{gid_line}---\n")
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
           if S.session_global_user_id(S._read_session_metadata(p)) == "gu_race"]
    assert len(mds) == 1, f"expected 1 user-conversation md, got {len(mds)}"


def test_ensure_user_conversation_never_resumes_a_recycled_attendant(
        tmp_path, monkeypatch):
    """T-0720 ruling (operator, 2026-07-26): ensure_user_conversation ALWAYS
    fresh-spawns — there is no resume-the-recycled-attendant preference, and
    re-adding one is a deliberate decision, not a bugfix.

    T-0575 shipped that preference at 74eef0f; T-0564 exempts every
    ``user-conversation`` session from all three recycle paths, so
    ``idle_timeout``'s terminate-and-remember (the only writer of
    ``resumable: true``) never runs for the role and no attendant md can ever
    carry the stamp. This test seeds the impossible md ANYWAY and asserts the
    ensure still spawns fresh, so a reader can't mistake "unreachable" for
    "broken" and wire it back up without narrowing recycle_gate first.
    """
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    gid = "gu_t720"
    sid = f"S-u-{gid}-user-conversation-p4"
    S._write_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / f"{sid}.md", {
            "sid": sid, "status": "suspended",
            "window": f"{gid}-user-conversation", "cwd": "/tmp",
            "claude_uuid": "uu-att", "suspended_at": "2026-07-26T10:00:00Z",
            "resumable": True, "recycled_at": "2026-07-26T10:00:00Z",
            "resume_hint": "idle cache-window recycle (compacted)",
        })

    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: None)

    def boom_resume(*a, **k):
        raise AssertionError(
            "ensure_user_conversation must not resume — see T-0720")

    monkeypatch.setattr(S, "resume", boom_resume)
    monkeypatch.setattr(
        S, "spawn",
        lambda cfg, slug, window, initial_prompt=None, **kw:
            {"ok": True, "sid": f"S-u-{window}-p8"})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": gid,
        "message_ref": "hello again"})
    assert result["ok"] is True
    assert result["spawned"] is True
    assert "resumed" not in result


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
# inject_prompt (T-0770) — the BLOCK sibling of inject_input
# ---------------------------------------------------------------------------
#
# The measurement that made this action necessary is the pair of tests above:
# `test_inject_input_multiline` PINS one Enter per line, which is three
# composer submissions for a three-line payload. The stakeholder's own messages
# on the direct-mode topic path already arrive that way, and a multi-line
# provenance envelope on that transport would have been worse than the bare
# text it replaces. These tests are that comparison, made explicit.


def _patch_prompt_transport(monkeypatch):
    """Patch the paste-primitive seams; returns the tmux call log."""
    import subprocess
    import bot_squad_worker.input_mux as M
    import bot_squad_worker.sessions as S

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(S, "_run", fake_run)
    # The composer: non-empty right after the paste (it landed), empty after
    # the Enter (it submitted) — the two states _deliver_prompt polls for.
    states = iter(["pasted", ""] * 8)
    monkeypatch.setattr(S, "_composer_content", lambda pane: next(states))
    # Small but NON-zero: both are divisors of their timeout when the poll
    # budget is computed (sessions.py), so a 0.0 here is a ZeroDivisionError.
    monkeypatch.setattr(S, "_SUBMIT_CONFIRM_POLL_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(S, "_PASTE_LANDED_POLL_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(M, "_capture_pane", lambda pane_id: "")
    return calls


def test_inject_prompt_delivers_a_multiline_block_as_ONE_submission(tmp_path, monkeypatch):
    """★ The contrast with test_inject_input_multiline above: same three lines,
    ONE Enter. A four-line envelope submitted line-by-line would have the
    session answering the header before it had read his words."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    from bot_squad_worker.sessions import PaneInfo

    _make_inject_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [
        PaneInfo(pane_id="%7", window="win", pid="1", cwd="/tmp", command="claude")])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    calls = _patch_prompt_transport(monkeypatch)

    result = A.dispatch("inject_prompt",
                        {"sid": "S-testuser-win-p7", "text": "line1\nline2\nline3"})
    assert result["ok"] is True and result["pane_id"] == "%7"
    assert [c for c in calls if "send-keys" in c] == [
        ["tmux", "send-keys", "-t", "%7", "Enter"],
    ]
    # …and the body went in as a bracketed PASTE, so the newlines stayed
    # newlines instead of submitting.
    assert any(c[:2] == ["tmux", "load-buffer"] for c in calls)
    assert any("paste-buffer" in c for c in calls)


def test_inject_prompt_raises_when_the_session_is_gone(tmp_path, monkeypatch):
    """Same contract as inject_input, and it is load-bearing: tg_listener's
    T-0746 fallback (don't drop an undeliverable message — hand it to the
    project attendant) is driven by this exception. `send_input` would have
    DEFERRED into a queue instead, which is the silence this ticket is about."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    _make_inject_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")

    with pytest.raises(ActionError, match="no live pane"):
        A.dispatch("inject_prompt", {"sid": "S-testuser-gone-p9", "text": "hi"})


def test_inject_prompt_param_gate(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A

    _make_inject_cfg(tmp_path, monkeypatch)
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("inject_prompt", {"sid": "S-x-y-p1", "text": "hi", "evil": "x"})
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("inject_prompt", {"sid": "S-x-y-p1"})
    with pytest.raises(ActionError, match="empty text"):
        A.dispatch("inject_prompt", {"sid": "S-x-y-p1", "text": "  \n "})


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
    note = ("stakeholder said this sacred thing " * 6).strip()
    assert 200 < len(note) <= 240
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
    # T-0877 canonicalize-on-store (same shape as the --initiative case below):
    # priority is now validated against the vocabulary the queue RANKS by and
    # stored canonical, so `P1` lands as `p1` and `high` lands as `p1` too. It
    # used to be stored verbatim, whatever it was — which is how `high` became
    # 86 tickets the queue could not see. See test_priority_vocabulary.py.
    assert 'priority: "p1"' in body
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


def test_doc_new_nests_under_an_existing_parent(tmp_path, tmp_config_dir, monkeypatch):
    """T-0290 (a): `bsq doc new` can author the nested tree the web API could.

    The gap was one missing key: `_DOC_NEW_ALLOWED` omitted `parent_doc_id`, so
    the agent path could only ever create ROOT docs while `POST /docs` accepted
    a mother. Both paths now write the same frontmatter field.
    """
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    mother = A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Mother doc",
    })
    assert mother["parent_doc_id"] is None                      # a root doc stays root
    assert "parent_doc_id" not in Path(mother["file_path"]).read_text()

    child = A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Child doc",
        "parent_doc_id": mother["id"],
    })
    assert child["parent_doc_id"] == mother["id"]
    assert f"parent_doc_id: {mother['id']}" in Path(child["file_path"]).read_text()


def test_doc_new_child_is_visible_to_the_web_apis_nesting_view(
    tmp_path, tmp_config_dir, monkeypatch
):
    """The edge an agent writes is the edge the web docs tree reads.

    Both sides resolve parent/child through `artifact_nesting`, which is a
    declared MIRROR pair (worker ⇄ api, see test_module_mirrors.MIRRORS) — so
    this asserts the point of the mirror: a CLI-created child shows up under
    its mother in exactly the view `GET /docs/<id>/children` serves.
    """
    import bot_squad_worker.actions as A
    from bot_squad_worker import artifact_nesting as AN

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    mother = A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Mother doc",
    })
    child = A.dispatch("doc_new", {
        "slug": "test-project", "category": "design", "title": "Child doc",
        "parent_doc_id": mother["id"],
    })

    kids = AN.children_of(proj, mother["id"])
    assert [k["id"] for k in kids] == [child["id"]]
    assert kids[0]["parent_doc_id"] == mother["id"]


def test_doc_new_rejects_an_unknown_parent_without_burning_an_id(
    tmp_path, tmp_config_dir, monkeypatch
):
    """Same rule as routes_docs.create_doc: the mother must already exist.

    The id allocation happens AFTER the check, so a rejected create leaves the
    monotonic counter where it was — a typo'd parent doesn't punch a hole in
    the D-NNNN sequence.
    """
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Mother doc",
    })
    counter = proj / "_counters" / "doc.txt"
    before = counter.read_text().strip()

    with pytest.raises(ActionError, match="parent artifact not found"):
        A.dispatch("doc_new", {
            "slug": "test-project", "category": "architecture", "title": "Orphan",
            "parent_doc_id": "D-9999",
        })
    assert counter.read_text().strip() == before


def test_doc_new_cannot_self_parent_or_close_a_cycle(tmp_path, tmp_config_dir, monkeypatch):
    """The DoD's cycle clause, and why no cycle WALK is needed on create.

    A doc being created has no id yet, so naming the id it is about to receive
    (or any id that doesn't exist) is rejected by the same existence check. A
    fresh child cannot be an ancestor of anything, so no loop can be closed.
    """
    import bot_squad_worker.actions as A

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Mother doc",
    })
    next_id = "D-%04d" % (int((proj / "_counters" / "doc.txt").read_text().strip()) + 1)

    with pytest.raises(ActionError, match="parent artifact not found"):
        A.dispatch("doc_new", {
            "slug": "test-project", "category": "architecture", "title": "Self parent",
            "parent_doc_id": next_id,
        })


def test_doc_new_parent_may_live_in_another_store(tmp_path, tmp_config_dir, monkeypatch):
    """T-0283 cross-store nesting: a doc may hang off a FEEDBACK artifact."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import artifact_nesting as AN

    proj = _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    fb_dir = proj / "feedback"
    fb_dir.mkdir(parents=True)
    (fb_dir / "F-2026-07-27-docs-tree.md").write_text(
        "---\nid: F-2026-07-27-docs-tree\ntitle: \"tree gripe\"\n---\n\nbody\n"
    )

    child = A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Answer doc",
        "parent_doc_id": "F-2026-07-27-docs-tree",
    })
    assert child["parent_doc_id"] == "F-2026-07-27-docs-tree"
    kids = AN.children_of(proj, "F-2026-07-27-docs-tree")
    assert [k["id"] for k in kids] == [child["id"]]


def test_doc_new_blank_parent_is_the_same_as_none(tmp_path, tmp_config_dir, monkeypatch):
    """A caller passing an empty/whitespace parent gets a ROOT doc, not a 404.

    Mirrors the web API, where `(body.parent_doc_id or "").strip() or None`
    makes an empty string mean "no mother" — a CLI flag defaulting to "" must
    not become an unfindable parent id.
    """
    import bot_squad_worker.actions as A

    _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    out = A.dispatch("doc_new", {
        "slug": "test-project", "category": "architecture", "title": "Root doc",
        "parent_doc_id": "   ",
    })
    assert out["parent_doc_id"] is None
    assert "parent_doc_id" not in Path(out["file_path"]).read_text()


def test_doc_new_rejects_bad_category(tmp_path, tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _setup_entity_new(tmp_path, tmp_config_dir, monkeypatch)
    with pytest.raises(ActionError, match="invalid category"):
        A.dispatch("doc_new", {
            "slug": "test-project", "category": "../etc", "title": "x",
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
        self.renamed_general: list[dict] = []
        self.renamed: list[dict] = []
        self._next_tid = 100

    def create_forum_topic(self, *, chat_id, name) -> int:
        self._next_tid += 1
        self.created.append({"chat_id": chat_id, "name": name, "id": self._next_tid})
        return self._next_tid

    def close_forum_topic(self, *, chat_id, thread_id) -> None:
        self.closed.append({"chat_id": chat_id, "thread_id": thread_id})

    def rename_general_forum_topic(self, *, chat_id, name) -> None:
        self.renamed_general.append({"chat_id": chat_id, "name": name})

    def edit_forum_topic(self, *, chat_id, thread_id, name) -> None:
        self.renamed.append({"chat_id": chat_id, "thread_id": thread_id, "name": name})


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
# T-0660: create-and-bind a forum topic in ONE step + rename a forum's
# General topic (Phase 1 — the runtime capability the stakeholder's live
# group setup is executed against once deployed).
# ---------------------------------------------------------------------------


def test_tg_topic_create_creates_and_binds(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_create", {
        "chat_id": "-1003761939853", "name": "[watchrobot] General", "slug": "test-project",
    })
    assert out["ok"] is True
    assert out["chat_id"] == "-1003761939853"
    assert out["name"] == "[watchrobot] General"
    assert out["slug"] == "test-project"
    thread_id = out["thread_id"]
    assert fake.created == [{"chat_id": "-1003761939853", "name": "[watchrobot] General", "id": thread_id}]

    rec = tg_bindings.resolve(Config.load(tmp_config_dir), "-1003761939853", thread_id)
    assert rec == {"slug": "test-project", "ticket_id": None, "session_id": None,
                   "pinned_message_id": None}


def _write_ticket(cfg, slug: str, ticket_id: str, title: str) -> None:
    backlog = cfg.data_dir / slug / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / f"{ticket_id}-{title.lower().replace(' ', '-')}.md").write_text(
        f"---\nid: {ticket_id}\ntitle: {title}\nstatus: open\n---\n"
    )


def test_tg_topic_create_with_ticket_id_binds_task_topic(tmp_config_dir, monkeypatch):
    """T-0660 per-task topic: an optional ticket_id binds {slug, ticket_id}
    instead of just {slug}."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    fake = _FakeForumTg()
    cfg = Config.load(tmp_config_dir)
    _write_ticket(cfg, "test-project", "T-0700", "Add user panel")
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "ticket_id": "T-0700",
    })
    rec = tg_bindings.resolve(cfg, "111", out["thread_id"])
    assert rec == {"slug": "test-project", "ticket_id": "T-0700", "session_id": None,
                   "pinned_message_id": None}


def test_tg_topic_create_with_session_id_binds_originating_session(tmp_config_dir, monkeypatch):
    """T-0660 Phase 2: an optional session_id routes an inbound topic message
    straight to that originating session (tg_listener._handle_topic_bound),
    not the project's user-conversation attendant."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    fake = _FakeForumTg()
    cfg = Config.load(tmp_config_dir)
    _write_ticket(cfg, "test-project", "T-0700", "Add user panel")
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "ticket_id": "T-0700", "session_id": "S-dev-p9",
    })
    rec = tg_bindings.resolve(cfg, "111", out["thread_id"])
    assert rec == {"slug": "test-project", "ticket_id": "T-0700", "session_id": "S-dev-p9",
                   "pinned_message_id": None}


def test_tg_topic_create_unknown_slug_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    with pytest.raises(ActionError, match="unknown project slug"):
        A.dispatch("tg_topic_create", {"chat_id": "111", "name": "X", "slug": "no-such"})
    assert fake.created == []  # no API call attempted for an invalid bind target


def test_tg_topic_create_missing_required_param_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="missing required params"):
        A.dispatch("tg_topic_create", {"chat_id": "111", "name": "X"})  # no slug


def test_tg_topic_create_rejects_unexpected_param(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("tg_topic_create", {
            "chat_id": "111", "name": "X", "slug": "test-project", "bogus": "y",
        })


# --- T-0660 field note (TL p23): a documented TG API failure (bad chat_id,
# missing can_manage_topics admin right, …) must surface as a legible
# ActionError, not an opaque 500 — tg.py's _call already puts the API's
# `description` into the exception message; these actions must not let it
# bubble up unwrapped. ---

def test_tg_topic_create_surfaces_tg_api_error_as_action_error(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    class _FailingForumTg(_FakeForumTg):
        def create_forum_topic(self, *, chat_id, name):
            raise RuntimeError("Telegram API error (createForumTopic): CHAT_ADMIN_REQUIRED")

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FailingForumTg())
    with pytest.raises(ActionError, match="CHAT_ADMIN_REQUIRED"):
        A.dispatch("tg_topic_create", {"chat_id": "111", "name": "X", "slug": "test-project"})


def test_tg_topic_rename_general_surfaces_tg_api_error_as_action_error(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    class _FailingForumTg(_FakeForumTg):
        def rename_general_forum_topic(self, *, chat_id, name):
            raise RuntimeError("Telegram API error (editGeneralForumTopic): CHAT_ADMIN_REQUIRED")

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FailingForumTg())
    with pytest.raises(ActionError, match="CHAT_ADMIN_REQUIRED"):
        A.dispatch("tg_topic_rename_general", {"chat_id": "111", "name": "X"})


def test_tg_topic_close_for_ticket_surfaces_tg_api_error_as_action_error(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    class _FailingForumTg(_FakeForumTg):
        def close_forum_topic(self, *, chat_id, thread_id):
            raise RuntimeError("Telegram API error (closeForumTopic): TOPIC_NOT_FOUND")

    cfg, _ = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FailingForumTg())
    tg_bindings.set_binding(cfg, "111", 42, "test-project", ticket_id="T-0700")
    with pytest.raises(ActionError, match="TOPIC_NOT_FOUND"):
        A.dispatch("tg_topic_close_for_ticket", {"ticket_id": "T-0700"})


def test_tg_topic_create_surfaces_httpx_status_error_as_action_error(tmp_config_dir, monkeypatch):
    import httpx
    import bot_squad_worker.actions as A

    class _FailingForumTg(_FakeForumTg):
        def create_forum_topic(self, *, chat_id, name):
            raise httpx.HTTPStatusError("502 Bad Gateway", request=None, response=None)

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FailingForumTg())
    with pytest.raises(ActionError, match="502 Bad Gateway"):
        A.dispatch("tg_topic_create", {"chat_id": "111", "name": "X", "slug": "test-project"})


def test_tg_topic_rename_general_calls_edit_general_forum_topic(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_rename_general", {
        "chat_id": "-1003761939853", "name": "[bot-squad] General",
    })
    assert out == {"ok": True, "chat_id": "-1003761939853", "name": "[bot-squad] General"}
    assert fake.renamed_general == [
        {"chat_id": "-1003761939853", "name": "[bot-squad] General"},
    ]


def test_tg_topic_rename_general_missing_required_param_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="missing required params"):
        A.dispatch("tg_topic_rename_general", {"chat_id": "111"})  # no name


def test_tg_topic_rename_general_rejects_unexpected_param(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("tg_topic_rename_general", {"chat_id": "111", "name": "X", "bogus": "y"})


# ---------------------------------------------------------------------------
# T-0669: harden tg_topic_create — a per-task topic's name is DERIVED from
# the ticket's own title (never the caller), and a project-level topic name
# that IS a raw session SID is rejected. Root cause of the T-0270 phantom-SID
# topic name bug.
# ---------------------------------------------------------------------------


def test_tg_topic_create_with_ticket_id_derives_name_from_ticket_title(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    backlog = cfg.data_dir / "test-project" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "T-0700-add-user-panel.md").write_text(
        "---\nid: T-0700\ntitle: Add user panel\nstatus: open\n---\n"
    )
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "ticket_id": "T-0700",
    })
    assert out["name"] == "[test-project] Add user panel"
    assert fake.created == [{"chat_id": "111", "name": "[test-project] Add user panel", "id": out["thread_id"]}]


def test_tg_topic_create_with_ticket_id_uses_topic_abbrev(tmp_config_dir, monkeypatch):
    """T-0680 (stakeholder: rename [watchrobot] topics to WR, [bot-squad] to
    BS): when the project has a configured topic_abbrev, the task-topic name
    uses it in place of the full slug."""
    import bot_squad_worker.actions as A

    (tmp_config_dir / "projects.toml").write_text(
        (tmp_config_dir / "projects.toml").read_text()
        + '\ntopic_abbrev = "WR"\n'
    )
    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    backlog = cfg.data_dir / "test-project" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "T-0700-add-user-panel.md").write_text(
        "---\nid: T-0700\ntitle: Add user panel\nstatus: open\n---\n"
    )
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "ticket_id": "T-0700",
    })
    assert out["name"] == "[WR] Add user panel"


def test_tg_topic_create_with_ticket_id_ignores_caller_name(tmp_config_dir, monkeypatch):
    """T-0669: a caller-supplied `name` is accepted (so an old caller isn't
    broken by the param becoming non-required) but IGNORED for a per-task
    topic — the ticket's own title always wins, even a SID-shaped one."""
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    backlog = cfg.data_dir / "test-project" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "T-0700-add-user-panel.md").write_text(
        "---\nid: T-0700\ntitle: Add user panel\nstatus: open\n---\n"
    )
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "ticket_id": "T-0700",
        "name": "S-almdudleer-watchrobot-user-conversation-p70",
    })
    assert out["name"] == "[test-project] Add user panel"


def test_tg_topic_create_with_ticket_id_missing_ticket_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    (cfg.data_dir / "test-project" / "backlog").mkdir(parents=True)
    with pytest.raises(ActionError, match="not found"):
        A.dispatch("tg_topic_create", {
            "chat_id": "111", "slug": "test-project", "ticket_id": "T-9999",
        })
    assert fake.created == []


def test_tg_topic_create_without_ticket_id_requires_name(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="name.*required"):
        A.dispatch("tg_topic_create", {"chat_id": "111", "slug": "test-project"})


@pytest.mark.parametrize("bad_name", [
    "S-almdudleer-watchrobot-operator-p160",
    "[watchrobot] S-almdudleer-watchrobot-operator-p160",
])
def test_tg_topic_create_without_ticket_id_rejects_sid_shaped_name(tmp_config_dir, monkeypatch, bad_name):
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    with pytest.raises(ActionError, match="looks like a raw session SID"):
        A.dispatch("tg_topic_create", {"chat_id": "111", "slug": "test-project", "name": bad_name})
    assert fake.created == []


def test_tg_topic_create_without_ticket_id_accepts_real_title(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "name": "[test-project] General",
    })
    assert out["name"] == "[test-project] General"


# ---------------------------------------------------------------------------
# T-0701: T-0669's SID-shape guard (_SID_NAME_RE) only recognises the OLD
# bracket+raw-SID label. T-0676 item 5 introduced a NEW compact label
# ("<slug> <role>", e.g. "watchrobot operator") for every TG-facing path — a
# session repeating T-0669's original mistake under the new label regime
# produces a name that the shape regex does NOT match. Fix: when the caller
# threads its own sid through as `caller_sid`, reject a name that equals that
# sid's OWN rendered label (compact or bracket form) — a literal self-match,
# not a generic "looks like <slug> <role>" heuristic (which would false-
# positive on legitimate short titles).
# ---------------------------------------------------------------------------


def test_tg_topic_create_rejects_own_compact_label_when_caller_sid_given(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    caller_sid = "S-almdudleer-operator-p160"
    with pytest.raises(ActionError, match="own display label"):
        A.dispatch("tg_topic_create", {
            "chat_id": "111", "slug": "test-project", "name": "test-project operator",
            "caller_sid": caller_sid,
        })
    assert fake.created == []


def test_tg_topic_create_rejects_own_bracket_label_when_caller_sid_given(tmp_config_dir, monkeypatch):
    """The caller might still be on the OLD bracket-form label — the
    shape-based guard already catches this (any bracket+raw-SID name, not
    just the caller's own), and passing caller_sid alongside it must not
    regress that existing coverage."""
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    caller_sid = "S-almdudleer-operator-p160"
    with pytest.raises(ActionError, match="looks like a raw session SID"):
        A.dispatch("tg_topic_create", {
            "chat_id": "111", "slug": "test-project",
            "name": f"[test-project] {caller_sid}", "caller_sid": caller_sid,
        })
    assert fake.created == []


def test_tg_topic_create_compact_label_of_a_DIFFERENT_sid_is_not_rejected(tmp_config_dir, monkeypatch):
    """The guard is a literal self-match, not a generic "<slug> <role>"
    pattern — a name that happens to render as another session's compact
    label (different role) is a real candidate title, not a repeat of the
    T-0669 mistake, and must still be accepted."""
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "name": "test-project operator",
        "caller_sid": "S-almdudleer-dev-p9",
    })
    assert out["name"] == "test-project operator"


def test_tg_topic_create_compact_label_name_accepted_without_caller_sid(tmp_config_dir, monkeypatch):
    """No caller_sid ⇒ no self-match check is possible, so an old/other
    caller that doesn't pass it is unaffected (falls back to the shape-only
    guard, which a compact label never matches)."""
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_create", {
        "chat_id": "111", "slug": "test-project", "name": "test-project operator",
    })
    assert out["name"] == "test-project operator"


# ---------------------------------------------------------------------------
# T-0669/T-0676 item 1: rename a REGULAR forum topic (editForumTopic) — the
# capability the TL uses to relabel the live phantom-SID-named T-0270 topic.
# ---------------------------------------------------------------------------


def test_tg_topic_rename_calls_edit_forum_topic(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_rename", {
        "chat_id": "-1003761939853", "thread_id": 45, "name": "[watchrobot] T-0270 title",
    })
    assert out == {
        "ok": True, "chat_id": "-1003761939853", "thread_id": 45,
        "name": "[watchrobot] T-0270 title",
    }
    assert fake.renamed == [
        {"chat_id": "-1003761939853", "thread_id": 45, "name": "[watchrobot] T-0270 title"},
    ]


def test_tg_topic_rename_does_not_touch_binding(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg, fake = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    tg_bindings.set_binding(cfg, "111", 45, "test-project", ticket_id="T-0700")
    A.dispatch("tg_topic_rename", {"chat_id": "111", "thread_id": 45, "name": "new title"})
    rec = tg_bindings.resolve(cfg, "111", 45)
    assert rec == {"slug": "test-project", "ticket_id": "T-0700", "session_id": None,
                   "pinned_message_id": None}


def test_tg_topic_rename_missing_required_param_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="missing required params"):
        A.dispatch("tg_topic_rename", {"chat_id": "111", "thread_id": 45})  # no name


def test_tg_topic_rename_rejects_unexpected_param(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("tg_topic_rename", {
            "chat_id": "111", "thread_id": 45, "name": "X", "bogus": "y",
        })


def test_tg_topic_rename_rejects_non_integer_thread_id(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="must be an integer"):
        A.dispatch("tg_topic_rename", {"chat_id": "111", "thread_id": "not-a-number", "name": "X"})


def test_tg_topic_rename_surfaces_tg_api_error_as_action_error(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    class _FailingForumTg(_FakeForumTg):
        def edit_forum_topic(self, *, chat_id, thread_id, name):
            raise RuntimeError("Telegram API error (editForumTopic): CHAT_ADMIN_REQUIRED")

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FailingForumTg())
    with pytest.raises(ActionError, match="CHAT_ADMIN_REQUIRED"):
        A.dispatch("tg_topic_rename", {"chat_id": "111", "thread_id": 45, "name": "X"})


# ---------------------------------------------------------------------------
# T-0660 Phase 2: close a ticket's dedicated forum topic (if any) when the
# ticket reaches its terminal status — `bsq ticket update <id> closed`.
# ---------------------------------------------------------------------------


def test_tg_topic_close_for_ticket_closes_and_clears_binding(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    fake = _FakeForumTg()
    cfg, _ = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    tg_bindings.set_binding(cfg, "111", 42, "test-project", ticket_id="T-0700")

    out = A.dispatch("tg_topic_close_for_ticket", {"ticket_id": "T-0700"})
    assert out == {"ok": True, "closed": True, "chat_id": "111", "thread_id": 42}
    assert fake.closed == [{"chat_id": "111", "thread_id": 42}]
    assert tg_bindings.resolve(cfg, "111", 42) is None  # binding cleared


def test_tg_topic_close_for_ticket_no_dedicated_topic_is_noop(tmp_config_dir, monkeypatch):
    """The common case (T-0660 Addendum 2): most tasks stay in the project's
    General room, no dedicated topic exists — no-op, not an error."""
    import bot_squad_worker.actions as A

    fake = _FakeForumTg()
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    out = A.dispatch("tg_topic_close_for_ticket", {"ticket_id": "T-9999"})
    assert out == {"ok": True, "closed": False}
    assert fake.closed == []


def test_tg_topic_close_for_ticket_general_binding_not_closed(tmp_config_dir, monkeypatch):
    """A ticket_id degenerately bound to a chat's General feed (thread_id=None)
    has nothing closeForumTopic applies to — skipped, not an API error."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    fake = _FakeForumTg()
    cfg, _ = _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    tg_bindings.set_binding(cfg, "111", None, "test-project", ticket_id="T-0700")

    out = A.dispatch("tg_topic_close_for_ticket", {"ticket_id": "T-0700"})
    assert out == {"ok": True, "closed": False}
    assert fake.closed == []


def test_tg_topic_close_for_ticket_missing_required_param_raises(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="missing required params"):
        A.dispatch("tg_topic_close_for_ticket", {})


def test_tg_topic_close_for_ticket_rejects_unexpected_param(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=_FakeForumTg())
    with pytest.raises(ActionError, match="unexpected params"):
        A.dispatch("tg_topic_close_for_ticket", {"ticket_id": "T-0700", "bogus": "y"})


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

    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, _, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    # T-0789: was a `_BoomTg` class defined locally here, which
    # test_send_stakeholder_dm_link_present_via_max_transport_too further down
    # this file referenced out of scope. Hoisted to _RecordingBoomTg so there is
    # one failing-TG double and no shadowing name to reference by accident.
    boom_tg = _RecordingBoomTg()
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: boom_tg)
    out = A._send_stakeholder_dm(A._get_config(), message="hi", sid="S-x-p1", tg_chat_id="-100")
    assert out["channel"] == "max" and out["sent"] is True
    assert len(boom_tg.calls) == 1  # the failover was driven by THIS double
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


# T-0721: a page long enough to need more than one TG message (the chunk
# budget is ~3800 chars, TG's hard API cap 4096).
def _oversize_page(head: str = "Сводка по работе. ") -> str:
    return head + "Сделал шаг и проверил результат. " * 300


def _joined(calls: list[dict]) -> str:
    """Every delivered part, markers stripped, in send order."""
    import re as _re
    return " ".join(_re.sub(r"^\(\d+/\d+\)\s*", "", c["text"]) for c in calls)


def test_send_stakeholder_dm_splits_long_pages_instead_of_truncating(
        tmp_config_dir, monkeypatch):
    """T-0721 (stakeholder override of T-0610): «long ones should just split,
    that's it». A verbose page arrives COMPLETE as N sequential numbered
    messages — no ellipsis, no dropped tail, every part under TG's 4096 cap."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    verbose = _oversize_page()
    A._send_stakeholder_dm(A._get_config(), message=verbose, tg_chat_id="-100")

    assert len(fake_tg.calls) > 1                       # it split
    texts = [c["text"] for c in fake_tg.calls]
    assert all(len(t) <= 4096 for t in texts)           # TG's hard API cap
    # Parts are identifiable as parts, in order.
    total = len(texts)
    for n, t in enumerate(texts, 1):
        assert t.startswith(f"({n}/{total}) ")
    # Nothing was lost: every sentence of the original is somewhere in the set.
    assert _joined(fake_tg.calls).startswith(verbose[:200].strip())
    assert "".join(verbose.split()) in "".join(_joined(fake_tg.calls).split())
    assert "…" not in "".join(texts[:-1])               # no truncation ellipsis


def test_send_stakeholder_dm_first_part_failure_still_fails_over(
        tmp_config_dir, monkeypatch):
    """T-0721 must not weaken the T-0394 failover: when the FIRST part fails,
    nothing was delivered on TG, so the whole page goes to the MAX reserve."""
    import bot_squad_worker.actions as A

    class _BoomOnFirst:
        def send(self, **kw):
            raise RuntimeError("tg ConnectTimeout")

    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, _, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: _BoomOnFirst())
    out = A._send_stakeholder_dm(A._get_config(), message=_oversize_page(),
                                  sid="S-x-p1", tg_chat_id="-100")
    assert out["channel"] == "max" and out["sent"] is True
    assert "".join(_oversize_page().split()) in "".join(_joined(fake_max.calls).split())


def test_send_stakeholder_dm_mid_page_failure_does_not_redeliver_on_max(
        tmp_config_dir, monkeypatch):
    """A failure AFTER some parts landed must not fail over — that would
    re-deliver the earlier parts on the reserve channel. The page is reported
    partial (and logged) instead."""
    import bot_squad_worker.actions as A

    class _BoomOnSecond:
        def __init__(self):
            self.n = 0

        def send(self, **kw):
            self.n += 1
            if self.n > 1:
                raise RuntimeError("tg 502 mid-page")
            return True

    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, _, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: _BoomOnSecond())
    out = A._send_stakeholder_dm(A._get_config(), message=_oversize_page(),
                                  sid="S-x-p1", tg_chat_id="-100")
    assert out["channel"] == "tg" and out["sent"] is True and out["partial"] is True
    assert fake_max.calls == []


def test_split_page_appends_link_on_the_final_part_only(tmp_config_dir, monkeypatch):
    """T-0635 pointer survives T-0721 — but as an AFFORDANCE on the last part
    of a split page, never as a replacement for the content."""
    import bot_squad_worker.actions as A
    link = "https://staging.example.com/p/test-project/t/T-1"
    parts = A._split_page(_oversize_page("Заголовок. "), link=link)
    assert len(parts) > 1
    assert parts[-1].endswith(link)
    assert not any(link in p for p in parts[:-1])
    assert "см. задачу/тред" not in "".join(parts)


def test_split_page_short_text_is_one_unchanged_part(tmp_config_dir, monkeypatch):
    """A page that fits in one message is sent byte-identical: no part marker,
    and no "подробнее" pointer (there is no continuation to point at)."""
    import bot_squad_worker.actions as A
    short = "Готово: T-0721 в totest."
    assert A._split_page(short, link="https://example.com/x") == [short]


def test_send_stakeholder_dm_link_targets_explicit_task_id(tmp_config_dir, monkeypatch):
    """T-0635: when the call site has a task_id on hand (threaded through to
    the SSOT), the pointer links straight to that task.

    T-0665: exercised directly against ``_send_stakeholder_dm`` (do_slim=True)
    rather than via the ``tg_notify`` action dispatch — the action itself is
    never treated as a page (every ``tg_notify`` caller is an
    explicitly-addressed conversational send), but automated pages that call
    the SSOT directly still carry a working link. T-0721: the link now rides
    the final part of a SPLIT page."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    A._send_stakeholder_dm(A._get_config(), message=_oversize_page("Заголовок. "),
                            tg_chat_id="-100", slug="test-project", task_id="T-0635")
    assert "https://staging.example.com/p/test-project/t/T-0635" in fake_tg.calls[-1]["text"]


def test_send_stakeholder_dm_link_scoped_to_real_session_sid(tmp_config_dir, monkeypatch):
    """T-0635: without a task_id, a real session sid (S-...) scopes the
    sessions-page link; a synthetic label sid (e.g. "autopilot") does not."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    long_text = _oversize_page("Заголовок. ")
    A._send_stakeholder_dm(A._get_config(), message=long_text, tg_chat_id="-100",
                            sid="S-almdudleer-dev-p1")
    assert ("https://staging.example.com/p/test-project/sessions?sid=S-almdudleer-dev-p1"
            in fake_tg.calls[-1]["text"])

    fake_tg.calls.clear()
    A._send_stakeholder_dm(A._get_config(), message=long_text, tg_chat_id="-100",
                            sid="autopilot")
    assert fake_tg.calls[-1]["text"].endswith(
        "https://staging.example.com/p/test-project/sessions")


def test_page_detail_link_uses_mothership_host_for_other_project(tmp_path):
    """T-0657: on a multi-project install, a page-detail link for a
    NON-mothership project (e.g. watchrobot) must use the mothership
    project's own staging_url as the dashboard host — the ``/p/<slug>/...``
    route lives only in bot-squad's own web dashboard, never in the target
    project's own product deployment (which 404s / is garbage). Only the
    slug/sid in the path identify the target project's session."""
    import bot_squad_worker.actions as A
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        'repo_path = "/tmp/bot-squad-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://botsquad.dev"\n'
        'staging_url = "https://staging.botsquad.dev"\n'
        'dev_url = "https://dev.botsquad.dev"\n'
        'deploy_targets = ["staging", "prod"]\n'
        'mothership = true\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
        '\n'
        '[projects.watchrobot]\n'
        'slug = "watchrobot"\n'
        'display_name = "Watchrobot"\n'
        'repo_path = "/tmp/watchrobot-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://signal-tracker.dev.uzinvestapi.com"\n'
        'staging_url = "https://signal-staging.dev.uzinvestapi.com"\n'
        'dev_url = "https://signal-dev.dev.uzinvestapi.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "404580642"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text(
        '[telegram]\n'
        'bot_token = "TESTBOT:TOKEN"\n'
        'auth_age_max = 86400\n'
    )
    cfg = Config.load(cfg_dir)
    link = A._page_detail_link(cfg, slug="watchrobot", sid="S-almdudleer-x-p1")
    assert link == "https://staging.botsquad.dev/p/watchrobot/sessions?sid=S-almdudleer-x-p1"
    assert "signal-staging.dev.uzinvestapi.com" not in link


def test_send_stakeholder_dm_link_present_via_max_transport_too(tmp_config_dir, monkeypatch):
    """T-0635 DoD: the link must resolve for both TG and MAX transports (the
    T-0610 page-mode switch) — it's baked into ``message`` before the
    tg/max branch split, so a MAX-routed page carries it too.

    T-0721: the MAX reserve splits the same way TG does (parts, then the link
    on the last one) — a failover must not reintroduce truncation."""
    import bot_squad_worker.actions as A
    _config_dir_with_max_default(tmp_config_dir, "MAXCHAT99")
    _, _, fake_max = _inject_both_channels(monkeypatch, tmp_config_dir)
    boom_tg = _RecordingBoomTg()
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: boom_tg)
    out = A._send_stakeholder_dm(A._get_config(), message=_oversize_page("Заголовок. "),
                                  sid="S-x-p1", tg_chat_id="-100")
    assert out["channel"] == "max"
    # T-0789: `_BoomTg` was undefined at this line — a class local to the
    # T-0610 test above — so the lambda raised NameError, _send_stakeholder_dm
    # caught it as a TG failure, and `channel == "max"` passed for the wrong
    # reason. This asserts the failover was driven by the tg double actually
    # being invoked and actually failing, which the channel alone cannot show.
    assert len(boom_tg.calls) == 1
    assert len(fake_max.calls) > 1
    assert ("https://staging.example.com/p/test-project/sessions?sid=S-x-p1"
            in fake_max.calls[-1]["text"])


def test_send_stakeholder_dm_prefer_tg_never_slimmed(tmp_config_dir, monkeypatch):
    """T-0610 review P3: an explicitly-addressed send (prefer_tg — e.g. the
    T-0569 conversation-relay reply to the stakeholder's DM) is conversational
    content, not a page — it must arrive untruncated."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    reply = "Развёрнутый ответ по треду. " + "Деталь и обоснование решения. " * 40
    A._send_stakeholder_dm(A._get_config(), message=reply, tg_chat_id="404", prefer_tg=True)
    assert fake_tg.calls[0]["text"] == reply  # byte-identical, no cap


def test_tg_notify_slug_only_never_slimmed(tmp_config_dir, monkeypatch):
    """T-0665: `bsq tg ping` dispatches ``tg_notify`` with only slug/message/
    sid/urgent — no chat_id/topic_id, so it used to fall through to the
    alert-page ``_slim_page`` cut at 400 chars even though it's a live,
    explicitly-addressed conversational send. A long message must arrive
    byte-identical: no cut, no dangling "… подробнее" continuation tail."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    long_reply = ("Статус деплоя. Вариант 1: откатить. Вариант 2: катить дальше. "
                  + "Ещё немного контекста по решению. " * 20)
    A.dispatch("tg_notify", {"slug": "test-project", "message": long_reply,
                              "sid": "S-x-p1", "urgent": True})
    assert len(fake_tg.calls) == 1
    sent = fake_tg.calls[0]["text"]
    assert sent == long_reply
    assert "подробнее" not in sent


def test_tg_notify_over_tg_cap_splits_without_a_pointer(tmp_config_dir, monkeypatch):
    """T-0721: the T-0665 exemption is about never TRUNCATING, not about
    ignoring TG's 4096-char hard cap — a `bsq tg ping` longer than one message
    is split into numbered parts (it used to be an API 400), still with no
    "подробнее" pointer, since it isn't an automated page."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    huge = "Разбор ситуации. " + "Ещё немного контекста по решению. " * 300
    A.dispatch("tg_notify", {"slug": "test-project", "message": huge,
                              "sid": "S-x-p1", "urgent": True})
    texts = [c["text"] for c in fake_tg.calls]
    assert len(texts) > 1 and all(len(t) <= 4096 for t in texts)
    assert texts[0].startswith(f"(1/{len(texts)}) ")
    assert "подробнее" not in "".join(texts)
    assert "".join(huge.split()) in "".join(_joined(fake_tg.calls).split())


def test_tg_notify_needs_input_question_and_footer_both_survive(tmp_config_dir, monkeypatch):
    """T-0610 review P2-1 pre-slimmed the QUESTION so the tmux-attach footer
    couldn't be cut off the end. T-0721 removed truncation entirely: a long
    needs-input page keeps the FULL question AND the footer, split across
    parts — the footer landing on the last one."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_stall as TS
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)
    monkeypatch.setattr(A, "_resolve_tmux_session", lambda c, slug, sid: "bot-squad")
    monkeypatch.setattr(
        TS, "build_escalation_text",
        lambda cfg, sid, text, session: f"{text}\n\n▶ tmux attach -t {session}")
    long_question = "Нужен твой выбор по деплою. " + "Контекст решения и варианты. " * 300
    out = A.dispatch("tg_notify", {"message": long_question, "sid": "S-x-p1",
                                   "needs_input": True})
    assert out["channel"] == "tg"
    texts = [c["text"] for c in fake_tg.calls]
    assert len(texts) > 1
    assert "tmux attach -t bot-squad" in texts[-1]     # footer survived, on the tail
    # …and so did the whole question (whitespace-insensitive: parts are
    # rstripped at their split boundary).
    assert "".join(long_question.split()) in "".join(_joined(fake_tg.calls).split())
    assert "детали: см. задачу/тред" not in "".join(texts)


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


# ---------------------------------------------------------------------------
# T-0655: set_drive action — operator's own drive=on/off continuity toggle.
# ---------------------------------------------------------------------------

def test_set_drive_action_off_and_on(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    cfg, repo = _make_sessions_cfg(tmp_path, monkeypatch)
    _seed_user_session(cfg, repo, "S-u-operator-p1", window="operator")

    out = A.dispatch("set_drive", {
        "slug": "test-project", "sid": "S-u-operator-p1", "on": False,
    })
    assert out["ok"] and out["drive"] == "off"

    out = A.dispatch("set_drive", {
        "slug": "test-project", "sid": "S-u-operator-p1", "on": True,
    })
    assert out["drive"] == "on"


def test_set_drive_action_refuses_non_operator(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    cfg, repo = _make_sessions_cfg(tmp_path, monkeypatch)
    _seed_user_session(cfg, repo, "S-u-claude-p1")  # derives role dev

    with pytest.raises(ActionError, match="not operator"):
        A.dispatch("set_drive", {
            "slug": "test-project", "sid": "S-u-claude-p1", "on": False,
        })


def test_set_drive_action_rejects_extra_and_bad_type(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    cfg, repo = _make_sessions_cfg(tmp_path, monkeypatch)
    _seed_user_session(cfg, repo, "S-u-operator-p1", window="operator")

    with pytest.raises(ActionError, match="unexpected"):
        A.dispatch("set_drive", {
            "slug": "test-project", "sid": "S-u-operator-p1", "on": False,
            "bogus": 1,
        })
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("set_drive", {"slug": "test-project", "sid": "S-u-operator-p1"})
    with pytest.raises(ActionError, match="boolean"):
        A.dispatch("set_drive", {
            "slug": "test-project", "sid": "S-u-operator-p1", "on": "off",
        })


def test_set_drive_registered_with_mode():
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY
    assert "set_drive" in ACTION_REGISTRY
    assert ACTION_MODES["set_drive"] == "tmux_only"


# ---------------------------------------------------------------------------
# T-0662: human-readable label -> session SID aliases.
# ---------------------------------------------------------------------------


def test_session_alias_set_and_resolve(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    out = A.dispatch("session_alias_set", {
        "label": "gateway-tl", "sid": "S-almdudleer-gateway-routing-tl-p23",
    })
    assert out == {
        "ok": True, "label": "gateway-tl",
        "sid": "S-almdudleer-gateway-routing-tl-p23", "previous_sid": None,
    }
    resolved = A.dispatch("session_alias_resolve", {"label": "gateway-tl"})
    assert resolved == {"ok": True, "sid": "S-almdudleer-gateway-routing-tl-p23"}


def test_session_alias_resolve_unknown_label(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    out = A.dispatch("session_alias_resolve", {"label": "nope"})
    assert out == {"ok": True, "sid": None}


def test_session_alias_set_reports_previous_sid_on_repoint(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    A.dispatch("session_alias_set", {"label": "alpha", "sid": "S-x-p1"})
    out = A.dispatch("session_alias_set", {"label": "alpha", "sid": "S-y-p2"})
    assert out == {"ok": True, "label": "alpha", "sid": "S-y-p2", "previous_sid": "S-x-p1"}


def test_session_alias_set_rejects_invalid_label(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError):
        A.dispatch("session_alias_set", {"label": "S-looks-like-a-sid", "sid": "S-x-p1"})


def test_session_alias_set_rejects_extra_and_missing_params(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="unexpected"):
        A.dispatch("session_alias_set", {"label": "alpha", "sid": "S-x-p1", "bogus": 1})
    with pytest.raises(ActionError, match="missing required"):
        A.dispatch("session_alias_set", {"label": "alpha"})


def test_session_alias_remove(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    A.dispatch("session_alias_set", {"label": "alpha", "sid": "S-x-p1"})
    out = A.dispatch("session_alias_remove", {"label": "alpha"})
    assert out == {"ok": True, "removed": True}
    # Idempotent — removing again is a no-op, not an error.
    out2 = A.dispatch("session_alias_remove", {"label": "alpha"})
    assert out2 == {"ok": True, "removed": False}
    assert A.dispatch("session_alias_resolve", {"label": "alpha"}) == {"ok": True, "sid": None}


def test_session_alias_list(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    A.dispatch("session_alias_set", {"label": "alpha", "sid": "S-x-p1"})
    A.dispatch("session_alias_set", {"label": "beta", "sid": "S-y-p2"})
    out = A.dispatch("session_alias_list", {})
    assert out == {"ok": True, "aliases": {"alpha": "S-x-p1", "beta": "S-y-p2"}}


def test_session_alias_list_rejects_params(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    with pytest.raises(ActionError, match="unexpected"):
        A.dispatch("session_alias_list", {"slug": "test-project"})


def test_session_alias_actions_registered_coordinator_only():
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY
    for name in (
        "session_alias_set", "session_alias_remove",
        "session_alias_resolve", "session_alias_list",
    ):
        assert name in ACTION_REGISTRY
        assert ACTION_MODES[name] == "coordinator_only"


# ---------------------------------------------------------------------------
# T-0719 REGRESSION — the page funnel must hand the send the RAW routing sid
#
# `_send_stakeholder_dm` is the SSOT every tg_notify / tg_ping / topic-say /
# relay / needs-input page funnels through. Since T-0676 item 5 the `sid` it
# puts on the wire is a compact DISPLAY label with no SID in it, so the raw
# routing sid has to travel separately (`route_sid`) or replies cannot come
# back. These pin exactly that split.
# ---------------------------------------------------------------------------

def test_send_stakeholder_dm_forwards_raw_route_sid_beside_compact_label(
        tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)

    A._send_stakeholder_dm(
        A._get_config(), message="operator here", tg_chat_id="-100",
        sid="S-almdudleer-operator-p241", slug="test-project", do_slim=False,
    )
    call = fake_tg.calls[0]
    # Display label stays compact (T-0676 item 5 is NOT reverted) …
    assert call["sid"] == "test-project operator"
    # … and the routing key rides alongside it.
    assert call["route_sid"] == "S-almdudleer-operator-p241"


@pytest.mark.parametrize("sid,role", [
    ("S-almdudleer-t-0719-dev-p260", "dev"),
    ("S-almdudleer-gateway-routing-tl-p23", "teamlead"),
    ("S-almdudleer-operator-p241", "operator"),
    ("S-almdudleer-gu_dc8262b6-user-conversation-p5", "user-conversation"),
])
def test_send_stakeholder_dm_route_sid_for_every_sender_kind(
        tmp_config_dir, monkeypatch, sid, role):
    """The break was system-wide — every sender kind derives a role and so gets
    the SID-less compact label."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)

    A._send_stakeholder_dm(
        A._get_config(), message="paging you", tg_chat_id="-100",
        sid=sid, slug="test-project", do_slim=False,
    )
    call = fake_tg.calls[0]
    assert call["sid"] == f"test-project {role}"   # no raw SID in the label
    assert call["route_sid"] == sid


def test_send_stakeholder_dm_omits_route_sid_for_synthetic_senders(
        tmp_config_dir, monkeypatch):
    """`deploy_monitor` / `autopilot` / `oauth_refresh` are not sessions: there
    is no pane to inject into, so nothing is routed and no kwarg is forwarded
    (which also keeps fixed-signature transports working)."""
    import bot_squad_worker.actions as A
    _, fake_tg, _ = _inject_both_channels(monkeypatch, tmp_config_dir)

    for synthetic in ("deploy_monitor", "autopilot", "oauth_refresh"):
        A._send_stakeholder_dm(
            A._get_config(), message=f"from {synthetic}", tg_chat_id="-100",
            sid=synthetic, slug="test-project", do_slim=False,
        )
    assert [c["route_sid"] for c in fake_tg.calls] == ["", "", ""]


def test_peer_send_tg_mirror_forwards_route_sid(tmp_path, tmp_config_dir, monkeypatch):
    """A mirrored peer message is also a page the stakeholder can reply to —
    the reply must reach the SENDING session, not the attendant."""
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
    _, fake = _inject_fake_tg(monkeypatch, tmp_config_dir)

    A.dispatch("peer_send", {
        "slug": "test-project",
        "from_sid": "S-almdudleer-operator-p23",
        "to": "S-alexey-ui-p0",
        "text": "ack — got your ping",
    })
    call = fake.calls[0]
    assert call["sid"] == "test-project operator"          # compact label
    assert call["route_sid"] == "S-almdudleer-operator-p23"


# ---------------------------------------------------------------------------
# T-0771: `tg_topic_bind` could not express the record shape `tg_topic_create`
# routinely produces, so every rebind of a per-task topic silently stripped it
# back to a bare project binding — and there was no supported way to put the
# association back short of creating a NEW topic, which is what a rebind
# exists to avoid.
# ---------------------------------------------------------------------------


def _bind_cfg(monkeypatch, tmp_config_dir):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    return cfg


def test_tg_topic_bind_writes_ticket_id_and_session_id(tmp_config_dir, monkeypatch):
    """The fields tg_topic_create sets are now reachable from the bind verb."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg = _bind_cfg(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_topic_bind", {
        "chat_id": "111", "thread_id": 517, "slug": "test-project",
        "ticket_id": "T-0314", "session_id": "S-x-p70",
    })

    assert out["ok"] is True
    assert out["binding"] == {"slug": "test-project", "ticket_id": "T-0314",
                              "session_id": "S-x-p70", "pinned_message_id": None}
    assert tg_bindings.resolve(cfg, "111", 517) == out["binding"]


def test_tg_topic_bind_refuses_a_lossy_rebind(tmp_config_dir, monkeypatch):
    """THE REPRODUCTION, at the surface the operator actually used."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg = _bind_cfg(monkeypatch, tmp_config_dir)
    tg_bindings.set_binding(cfg, "111", 517, "test-project",
                            ticket_id="T-0314", session_id="S-x-p70")

    with pytest.raises(A.ActionError) as e:
        A.dispatch("tg_topic_bind", {
            "chat_id": "111", "thread_id": 517, "slug": "test-project",
        })

    assert "T-0314" in str(e.value) and "S-x-p70" in str(e.value)
    assert tg_bindings.resolve(cfg, "111", 517)["ticket_id"] == "T-0314"


def test_tg_topic_bind_refusal_names_both_surfaces_and_both_operations(
        tmp_config_dir, monkeypatch):
    """The refusal has to be actionable where the caller is standing: an agent
    reads action params, a human reads CLI flags, and BOTH need to be told how
    to keep AND how to drop — a message that only offers "keep" would push the
    operator who genuinely meant to clear straight back to a workaround."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg = _bind_cfg(monkeypatch, tmp_config_dir)
    tg_bindings.set_binding(cfg, "111", 517, "test-project", session_id="S-x-p70")

    with pytest.raises(A.ActionError) as e:
        A.dispatch("tg_topic_bind", {
            "chat_id": "111", "thread_id": 517, "slug": "test-project",
        })

    msg = str(e.value)
    for expected in ("session_id='S-x-p70'", "--session S-x-p70",
                     "clear_session_id=true", "--clear-session",
                     "Nothing was written"):
        assert expected in msg, msg
    # ... and never advertises a field that is not at risk.
    assert "ticket" not in msg


def test_tg_topic_bind_clears_a_field_on_purpose_and_says_so(tmp_config_dir, monkeypatch):
    """The negative of the refusal: the operation p366 legitimately wanted
    (demote a direct-mode topic to the attendant) stays expressible AND is
    reported — under a silent merge it would have believed it had cleared
    session_id while the direct-mode routing branch stayed live."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg = _bind_cfg(monkeypatch, tmp_config_dir)
    tg_bindings.set_binding(cfg, "111", 517, "test-project",
                            ticket_id="T-0314", session_id="S-x-p70")

    out = A.dispatch("tg_topic_bind", {
        "chat_id": "111", "thread_id": 517, "slug": "test-project",
        "ticket_id": "T-0314", "clear_session_id": True,
    })

    assert out["cleared"] == ["session_id"]
    assert out["changes"] == {"session_id": {"from": "S-x-p70", "to": None}}
    assert out["binding"]["ticket_id"] == "T-0314"
    assert tg_bindings.resolve(cfg, "111", 517)["session_id"] is None


def test_tg_topic_bind_reports_a_write_that_changed_nothing(tmp_config_dir, monkeypatch):
    """A lossy write and a faithful one used to print the same line. `changes`
    is the difference — and an empty one is a real answer, not a failure."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg = _bind_cfg(monkeypatch, tmp_config_dir)
    tg_bindings.set_binding(cfg, "111", 517, "test-project", ticket_id="T-0314")

    out = A.dispatch("tg_topic_bind", {
        "chat_id": "111", "thread_id": 517, "slug": "test-project",
        "ticket_id": "T-0314", "clear_session_id": True,
    })

    assert out["changes"] == {}
    # Still answers the question it was ASKED — "clear session_id" was
    # requested and honoured, it just moved nothing.
    assert out["cleared"] == ["session_id"]


def test_tg_topic_bind_restores_a_ticket_id_that_was_already_lost(
        tmp_config_dir, monkeypatch):
    """The half the ticket calls "there is NO supported way to restore
    ticket_id": tonight's stripped topics get their association back WITHOUT a
    new topic, and the ticket-resolution consumers find them again."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg = _bind_cfg(monkeypatch, tmp_config_dir)
    tg_bindings.set_binding(cfg, "111", 220, "test-project")  # stripped, bare
    assert tg_bindings.find_by_ticket(cfg, "T-0314") is None

    A.dispatch("tg_topic_bind", {
        "chat_id": "111", "thread_id": 220, "slug": "test-project",
        "ticket_id": "T-0314",
    })

    found = tg_bindings.find_by_ticket(cfg, "T-0314")
    assert found["chat_id"] == "111" and found["thread_id"] == 220


def test_tg_topic_bind_contradiction_is_a_400_not_a_500(tmp_config_dir, monkeypatch):
    """The store raises a plain ValueError for a contradictory call; the action
    owes the caller an ActionError (HTTP 400 + the reason), never a traceback."""
    import bot_squad_worker.actions as A

    _bind_cfg(monkeypatch, tmp_config_dir)
    with pytest.raises(A.ActionError, match="contradict"):
        A.dispatch("tg_topic_bind", {
            "chat_id": "111", "thread_id": 517, "slug": "test-project",
            "ticket_id": "T-0314", "clear_ticket_id": True,
        })


def test_tg_topic_bind_still_binds_a_bare_project_topic(tmp_config_dir, monkeypatch):
    """NEGATIVE GUARD — the T-0639 project-binding flow (8 of the 11 live
    bindings) is unchanged. The risk of this change is over-refusing."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    cfg = _bind_cfg(monkeypatch, tmp_config_dir)
    out = A.dispatch("tg_topic_bind", {
        "chat_id": "111", "thread_id": 7, "slug": "test-project",
    })

    assert out["binding"] == {"slug": "test-project", "ticket_id": None,
                              "session_id": None, "pinned_message_id": None}
    assert out["cleared"] == []
    assert out["changes"] == {"slug": {"from": None, "to": "test-project"}}
    assert tg_bindings.resolve(cfg, "111", 7) == out["binding"]


def test_tg_topic_bind_still_rejects_a_genuinely_unknown_param(
        tmp_config_dir, monkeypatch):
    """NEGATIVE GUARD — widening the allowed set by four names must not turn
    the param gate off."""
    import bot_squad_worker.actions as A

    _bind_cfg(monkeypatch, tmp_config_dir)
    with pytest.raises(A.ActionError, match="unexpected params"):
        A.dispatch("tg_topic_bind", {
            "chat_id": "111", "thread_id": 7, "slug": "test-project",
            "pinned_message_id": 555,
        })


def test_tg_topic_create_reports_a_stale_key_instead_of_overwriting_it(
        tmp_config_dir, monkeypatch):
    """Unreachable in practice — a freshly created forum topic has an id
    nothing is bound to. But if the map IS stale for that key, overwriting is
    exactly the silent strip this guard exists to stop, and a 500 would leave a
    created topic nobody can explain. Degrade to the alarm, naming the topic."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings

    fake = _FakeForumTg()
    stale_thread_id = fake._next_tid + 1  # the id its next create_forum_topic returns
    cfg = Config.load(tmp_config_dir)
    _inject_fake_tg(monkeypatch, tmp_config_dir, fake_client=fake)
    tg_bindings.set_binding(cfg, "111", stale_thread_id, "test-project",
                            ticket_id="T-0314")

    with pytest.raises(A.ActionError) as e:
        A.dispatch("tg_topic_create", {
            "chat_id": "111", "slug": "test-project", "name": "[wr] General",
        })

    msg = str(e.value)
    assert "T-0314" in msg and "NOT bound" in msg
    assert str(stale_thread_id) in msg
    assert tg_bindings.resolve(cfg, "111", stale_thread_id)["ticket_id"] == "T-0314"


def test_restoring_a_stripped_ticket_id_puts_the_sends_back_in_the_topic(
        tmp_path, monkeypatch):
    """THE RECORDED CONSEQUENCE, end to end (T-0771 ★ "the sharp edge").

    A dev working T-0723 has a per-task topic. Strip its ticket_id — what a
    slug-only rebind used to do silently — and `_own_topic_binding` rung 2
    goes empty, so the session's sends fall back to the project's General
    room: from the stakeholder's seat, silence in the topic he is watching.
    Restoring the ticket_id through the bind verb (no NEW topic) puts them
    back, which is what unblocks repairing tonight's bindings by hand.
    """
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_bindings

    cfg_dir = _config_dir_with_topic(tmp_path)
    cfg, fake = _inject_fake_tg(monkeypatch, cfg_dir)
    conversation_locus.set_locus(cfg, "group-project", "gu_1", "-1001234567890", 5)
    tg_bindings.set_binding(cfg, "-1001234567890", 77, "group-project")  # STRIPPED
    _dev_session(cfg, "S-u-dev-p9", task_id="T-0723")

    A.dispatch("tg_notify", {"slug": "group-project", "message": "before",
                             "sid": "S-u-dev-p9"})
    # Names WHERE it lands instead, not just "not 77": the locus (topic 5,
    # wherever the human last wrote) — a send that failed outright would also
    # satisfy a bare inequality.
    assert fake.calls[-1]["topic_id"] == 5

    A.dispatch("tg_topic_bind", {
        "chat_id": "-1001234567890", "thread_id": 77, "slug": "group-project",
        "ticket_id": "T-0723",
    })
    A.dispatch("tg_notify", {"slug": "group-project", "message": "after",
                             "sid": "S-u-dev-p9", "debounce": False})

    assert fake.calls[-1]["topic_id"] == 77
    assert fake.calls[-1]["chat_id"] == "-1001234567890"


# --- T-0930: resume a suspended attendant before spawning a fresh one --------
#
# The stakeholder's 2026-08-31 answer reversed T-0720: idle_timeout now EXITS
# a user-conversation session past ~3h (resumable, claude_uuid kept), so the
# once-unreachable state exists — and the revive half must prefer
# `claude --resume` (full in-session history) over a memory-less fresh spawn.

def _write_uc_md(cfg, window, sid, *, status="suspended", claude_uuid="u-77",
                 suspended_at="2026-08-31T09:00:00Z", archived=None):
    import bot_squad_worker.sessions as S
    d = cfg.data_dir / "test-project" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"sid": sid, "status": status, "window": window,
            "cwd": "/x", "claude_uuid": claude_uuid,
            "suspended_at": suspended_at, "role": "user-conversation"}
    if archived is not None:
        meta["archived"] = archived
    S._write_session_metadata(d / f"{sid}.md", meta)


def test_ensure_user_conversation_resumes_suspended_attendant(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    window = "gu_a1b2c3-user-conversation"
    _write_uc_md(cfg, window, "S-u-gu_a1b2c3-user-conversation-p3")
    resumed, spawned = [], []
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda c, s, g: None)
    monkeypatch.setattr(S, "resume",
                        lambda c, s, sid, initial_prompt=None, **kw:
                        (resumed.append((sid, initial_prompt)) or
                         {"ok": True, "sid": "S-u-gu_a1b2c3-user-conversation-p9"}))
    monkeypatch.setattr(S, "spawn",
                        lambda *a, **k: spawned.append(a) or {"ok": True, "sid": "S-fresh"})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "are you there?",
    })
    assert result["ok"] is True and result.get("resumed") is True
    assert result["spawned"] is False
    assert result["sid"] == "S-u-gu_a1b2c3-user-conversation-p9"
    assert spawned == [], "resume must preempt the fresh spawn"
    assert resumed and "resumed" in (resumed[0][1] or "")


def test_ensure_user_conversation_resume_failure_falls_back_to_spawn(tmp_path,
                                                                     monkeypatch):
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    window = "gu_a1b2c3-user-conversation"
    _write_uc_md(cfg, window, "S-u-gu_a1b2c3-user-conversation-p3")
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda c, s, g: None)

    def _boom(*a, **k):
        raise RuntimeError("pane refused")

    monkeypatch.setattr(S, "resume", _boom)
    monkeypatch.setattr(S, "spawn",
                        lambda *a, **k: {"ok": True, "sid": "S-fresh-p4"})

    result = A.dispatch("ensure_user_conversation", {
        "slug": "test-project", "global_user_id": "gu_a1b2c3",
        "message_ref": "are you there?",
    })
    assert result["ok"] is True and result["spawned"] is True
    assert result["sid"] == "S-fresh-p4"


def test_find_suspended_uc_skips_archived_uuidless_and_other_windows(tmp_path,
                                                                     monkeypatch):
    import bot_squad_worker.actions as A

    cfg, _repo = _make_sessions_cfg(tmp_path, monkeypatch)
    window = "gu_a1b2c3-user-conversation"
    # not eligible: archived / no uuid / wrong window / still active
    _write_uc_md(cfg, window, "S-arch-p1", archived="true")
    _write_uc_md(cfg, window, "S-nouuid-p2", claude_uuid="~")
    _write_uc_md(cfg, "gu_OTHER-user-conversation", "S-other-p3")
    _write_uc_md(cfg, window, "S-live-p4", status="active")
    assert A._find_suspended_user_conversation(cfg, "test-project", window) is None
    # eligible, and the LATEST suspended one wins
    _write_uc_md(cfg, window, "S-old-p5", suspended_at="2026-08-30T01:00:00Z")
    _write_uc_md(cfg, window, "S-new-p6", suspended_at="2026-08-31T02:00:00Z")
    assert A._find_suspended_user_conversation(
        cfg, "test-project", window) == "S-new-p6"
