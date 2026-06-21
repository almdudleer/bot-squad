"""Tests for worker.tg_stall — T-0155 stall-watchdog.

Mirrors the manual walkthrough in
data/bot-squad/scenarios/T-0155-tg-bot-gating-escalation.md (written + walked
through before this automation, per T-0158). All tmux/pane/role lookups and TG
sends are stubbed — no network, no live tmux.
"""
from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

import bot_squad_worker.tg_stall as TS


DEV = "S-almdudleer-tg-gating-p18"
OP = "S-almdudleer-operator-p23"


def _make_cfg(tmp_path: Path, *, stall_minutes: int = 15, remote_url: str = ""):
    data = tmp_path / "data"
    (data / "bot-squad").mkdir(parents=True)
    proj = types.SimpleNamespace(tg_chat="404580642")
    return types.SimpleNamespace(
        data_dir=data,
        projects={"bot-squad": proj},
        tg_stall_minutes=stall_minutes,
        tg_remote_control_url=remote_url,
        tg_bot_token="FAKE:TOKEN",
    )


class _FakeTg:
    def __init__(self):
        self.sent = []

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None):
        full = f"[{sid}] {text}" if sid else text
        self.sent.append({"chat_id": chat_id, "text": full, "topic_id": topic_id})
        return True


@pytest.fixture
def faketg(monkeypatch):
    from bot_squad_worker import actions as A
    tg = _FakeTg()
    monkeypatch.setattr(A, "_TG", tg)
    return tg


@pytest.fixture
def fake_roles(monkeypatch):
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(
        S, "list_sessions",
        lambda cfg, slug: [{"sid": DEV, "role": "dev"}, {"sid": OP, "role": "operator"}],
    )


# ---------------------------------------------------------------------------
# Marker lifecycle
# ---------------------------------------------------------------------------

def test_mark_blocked_writes_marker(tmp_path):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "need prod call")
    data = json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())
    assert data["sid"] == DEV
    assert data["text"] == "need prod call"
    assert data["escalated"] is False
    assert data["since"] <= time.time()


def test_mark_blocked_preserves_since_while_pending(tmp_path):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "first")
    first = json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())["since"]
    time.sleep(0.01)
    TS.mark_blocked(cfg, "bot-squad", DEV, "second")
    again = json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())
    assert again["since"] == first  # clock started at the first ask
    assert again["text"] == "second"


def test_clear_blocked(tmp_path):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    assert TS.clear_blocked(cfg, "bot-squad", DEV) is True
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()
    assert TS.clear_blocked(cfg, "bot-squad", DEV) is False  # idempotent


# ---------------------------------------------------------------------------
# P2-05-BE: close-on-attach reconcile — clear a marker when the agent resumes
# crunching after the block (operator attached-and-typed, which the peer_send /
# user_prompt_submit clear paths miss on the DPI/MAX host).
# ---------------------------------------------------------------------------

def _marker_since(cfg, sid):
    return json.loads(TS._marker_path(cfg, "bot-squad", sid).read_text())["since"]


def test_clear_if_resumed_clears_on_new_activity_after_block(tmp_path):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "need prod call")
    since = _marker_since(cfg, DEV)
    # Genuinely-new jsonl activity well after the block → the agent resumed.
    assert TS.clear_if_resumed(cfg, "bot-squad", DEV, since + 600) is True
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_clear_if_resumed_keeps_marker_within_grace(tmp_path):
    """The agent's OWN peer_send write lands at ~since; it must not self-clear
    the marker it just set."""
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    since = _marker_since(cfg, DEV)
    assert TS.clear_if_resumed(cfg, "bot-squad", DEV, since + 5) is False
    assert TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_clear_if_resumed_no_marker_returns_false(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert TS.clear_if_resumed(cfg, "bot-squad", DEV, time.time()) is False


def test_clear_if_resumed_none_activity_leaves_marker(tmp_path):
    """No measurable activity timestamp → can't tell resume from idle; the
    marker is left untouched (keeps the existing-tests-green None contract)."""
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    assert TS.clear_if_resumed(cfg, "bot-squad", DEV, None) is False
    assert TS._marker_path(cfg, "bot-squad", DEV).exists()


# ---------------------------------------------------------------------------
# on_peer_send mark/clear (scenario steps 3, 8)
# ---------------------------------------------------------------------------

def test_peer_send_to_operator_marks_blocked(tmp_path, fake_roles):
    cfg = _make_cfg(tmp_path)
    TS.on_peer_send(cfg, "bot-squad", DEV, [OP])
    assert TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_peer_send_to_non_operator_does_not_mark(tmp_path, fake_roles):
    cfg = _make_cfg(tmp_path)
    TS.on_peer_send(cfg, "bot-squad", DEV, [DEV])  # dev→dev
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_operator_reply_clears_marker(tmp_path, fake_roles):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "need prod call")
    TS.on_peer_send(cfg, "bot-squad", OP, [DEV])  # operator replies to dev
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


# ---------------------------------------------------------------------------
# tick escalation (scenario steps 4, 5, 6)
# ---------------------------------------------------------------------------

def _age_marker(cfg, sid, seconds):
    p = TS._marker_path(cfg, "bot-squad", sid)
    d = json.loads(p.read_text())
    d["since"] = time.time() - seconds
    p.write_text(json.dumps(d))


def _stub_pane(monkeypatch, visible: bool, present: bool = True, route=None):
    pane = types.SimpleNamespace(pane_id="%7", window="tg-gating", session="bot-squad")
    monkeypatch.setattr(TS, "_pane_for_sid", lambda sid: pane if present else None)
    monkeypatch.setattr(TS, "_window_visible", lambda pid: visible)
    # T-0034: default route=None means "no upstream → TG the stakeholder" (the
    # operator / prod-teamlead case), keeping these the TG-path tests they were.
    # A redirect test passes route=<target SID>.
    monkeypatch.setattr(TS, "_route_idle_escalation", lambda cfg, slug, sid: route)


def test_tick_too_early_no_tg(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _stub_pane(monkeypatch, visible=False)
    audit = TS.tick(cfg)
    assert audit["escalated"] == 0
    assert faketg.sent == []


def test_tick_window_visible_no_tg(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=True)  # stakeholder is watching
    audit = TS.tick(cfg)
    assert audit["escalated"] == 0
    assert faketg.sent == []
    assert json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())["escalated"] is False


def test_tick_window_hidden_escalates_once(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "need prod call")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)

    audit = TS.tick(cfg)
    assert audit["escalated"] == 1
    assert len(faketg.sent) == 1
    msg = faketg.sent[0]["text"]
    assert msg.startswith(f"[{DEV}]")          # reply-routing prefix
    assert "need prod call" in msg
    # P2-03: MAX is send-only (no ingest) — no false "reply to this" promise.
    assert "Reply to this message" not in msg
    assert "#team-queries" in msg               # points to a REAL answer channel
    assert "Remote-control" in msg              # /remote-control footer
    assert json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())["escalated"] is True

    # Second tick must NOT re-flood.
    audit2 = TS.tick(cfg)
    assert audit2["escalated"] == 0
    assert len(faketg.sent) == 1


def test_escalation_routes_to_team_queries_topic(tmp_path, faketg, monkeypatch):
    """T-0386: a needs-input escalation lands in the #team-queries forum topic."""
    from bot_squad_worker import tg_topics
    cfg = _make_cfg(tmp_path)
    tg_topics.save(cfg, "bot-squad", {"team_queries": 3131})
    TS.mark_blocked(cfg, "bot-squad", DEV, "need prod call")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)

    TS.tick(cfg)
    assert len(faketg.sent) == 1
    assert faketg.sent[0]["topic_id"] == 3131


def test_tick_pane_gone_drops_marker(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False, present=False)  # session ended
    audit = TS.tick(cfg)
    assert audit["escalated"] == 0
    assert faketg.sent == []
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_tick_suppressed_send_defers_marker(tmp_path, monkeypatch):
    # T-0155: a token is configured but send() returns False (quiet hours) →
    # leave the marker pending so it pings when the stakeholder wakes.
    from bot_squad_worker import actions as A

    class _QuietTg:
        def send(self, **kw):
            return False  # suppressed (e.g. quiet hours)

    monkeypatch.setattr(A, "_TG", _QuietTg())
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)

    audit = TS.tick(cfg)
    assert audit["escalated"] == 0
    marker = TS._marker_path(cfg, "bot-squad", DEV)
    assert marker.exists()
    assert json.loads(marker.read_text())["escalated"] is False  # still pending


def test_tick_disabled_when_stall_minutes_zero(tmp_path, faketg):
    cfg = _make_cfg(tmp_path, stall_minutes=0)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    audit = TS.tick(cfg)
    assert audit["disabled"] is True
    assert faketg.sent == []


def test_tick_gc_ancient_marker(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, TS._MARKER_TTL_SEC + 100)
    audit = TS.tick(cfg)
    assert audit["gc"] == 1
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


# ---------------------------------------------------------------------------
# Escalation message / remote-control (scenario step 10)
# ---------------------------------------------------------------------------

def test_escalation_text_tmux_fallback(tmp_path):
    cfg = _make_cfg(tmp_path, remote_url="")
    body = TS.build_escalation_text(cfg, DEV, "need call", "bot-squad")
    assert "tmux attach -t bot-squad" in body
    # P2-03: no false reply promise — point to real answer channels instead.
    assert "Reply to this message" not in body
    assert "#team-queries" in body


def test_escalation_text_configured_url_substitutes_sid(tmp_path):
    cfg = _make_cfg(tmp_path, remote_url="https://claude.ai/code?session={sid}")
    body = TS.build_escalation_text(cfg, DEV, "need call", "bot-squad")
    assert f"https://claude.ai/code?session={DEV}" in body
    assert "tmux attach" not in body


# ---------------------------------------------------------------------------
# tmux visibility parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("out,expected", [
    ("1|1", True),     # attached + active window
    ("0|1", False),    # detached
    ("1|0", False),    # background window
    ("0|0", False),
    ("", False),
    ("garbage", False),
])
def test_window_visible_parsing(monkeypatch, out, expected):
    def fake_run(*a, **k):
        return types.SimpleNamespace(returncode=0, stdout=out)
    monkeypatch.setattr(TS.subprocess, "run", fake_run)
    assert TS._window_visible("%7") is expected


def test_window_visible_tmux_error(monkeypatch):
    def fake_run(*a, **k):
        return types.SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(TS.subprocess, "run", fake_run)
    assert TS._window_visible("%7") is False


# ---------------------------------------------------------------------------
# T-0034: idle-notify routing by role + TL parent
# ---------------------------------------------------------------------------

TL = "S-almdudleer-multi_server-TL-p30"


def _stub_rows(monkeypatch, rows, tl_of=None):
    """Stub the session list + team-projection lookup the router consults."""
    import bot_squad_worker.sessions as S
    import bot_squad_worker.teams as T
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: rows)
    monkeypatch.setattr(T, "tl_for_sid", lambda cfg, slug, sid: (tl_of or {}).get(sid))


def test_route_dev_with_teamlead_parent_returns_tl(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev"}, {"sid": TL, "role": "teamlead"},
         {"sid": OP, "role": "operator"}],
        tl_of={DEV: TL},
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) == TL


def test_route_dev_without_tl_returns_operator(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # team lead slot is the operator (or no team at all) → ad-hoc dev → operator
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev"}, {"sid": OP, "role": "operator"}],
        tl_of={DEV: OP},   # tl resolves to an operator-role session → not a TL
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) == OP


def test_route_dev_no_team_returns_operator(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev"}, {"sid": OP, "role": "operator"}],
        tl_of={},          # no team owns the dev
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) == OP


def test_route_teamlead_returns_operator(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": TL, "role": "teamlead"}, {"sid": OP, "role": "operator"}],
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", TL) == OP


def test_route_operator_returns_none_tg(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _stub_rows(monkeypatch, [{"sid": OP, "role": "operator"}])
    assert TS._route_idle_escalation(cfg, "bot-squad", OP) is None


def test_route_prod_teamlead_no_operator_returns_none_tg(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # prod contour: a teamlead with no operator session above it → TG.
    _stub_rows(monkeypatch, [{"sid": TL, "role": "teamlead"}])
    assert TS._route_idle_escalation(cfg, "bot-squad", TL) is None


def test_route_session_list_failure_falls_back_to_tg(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    import bot_squad_worker.sessions as S

    def boom(cfg, slug):
        raise RuntimeError("no tmux")

    monkeypatch.setattr(S, "list_sessions", boom)
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) is None


# ---------------------------------------------------------------------------
# Redirect path through tick: dev under a TL escalates to the TL, NOT the
# stakeholder (the core T-0034 DoD).
# ---------------------------------------------------------------------------

def _capture_bus(monkeypatch):
    sent = []
    nudged = []
    import bot_squad_worker.intersession as IS

    monkeypatch.setattr(
        IS, "send",
        lambda cfg, slug, from_sid, to, text, user=None: (
            sent.append({"from": from_sid, "to": to, "text": text})
            or {"ok": True, "delivered_to": [to]}
        ),
    )
    from bot_squad_worker import actions as A
    monkeypatch.setattr(
        A, "_action_inject_input",
        lambda params: nudged.append(params) or {"ok": True},
    )
    return sent, nudged


def test_tick_dev_under_tl_redirects_to_tl_not_stakeholder(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "stuck on the failing migration")
    _age_marker(cfg, DEV, 16 * 60)
    # Window visibility is irrelevant for a peer-bus redirect.
    _stub_pane(monkeypatch, visible=True, route=TL)
    sent, nudged = _capture_bus(monkeypatch)

    audit = TS.tick(cfg)

    assert audit["escalated"] == 1
    assert faketg.sent == []                      # stakeholder NOT paged
    assert len(sent) == 1 and sent[0]["to"] == TL
    assert "stuck on the failing migration" in sent[0]["text"]
    assert nudged and nudged[0]["sid"] == TL      # check-mail nudge to the TL
    marker = json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())
    assert marker["escalated"] is True
    assert marker["routed_to"] == TL

    # One-shot: a second tick must not re-deliver.
    audit2 = TS.tick(cfg)
    assert audit2["escalated"] == 0
    assert len(sent) == 1


def test_tick_redirect_bus_failure_leaves_marker_pending(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False, route=TL)
    import bot_squad_worker.intersession as IS

    def boom(*a, **k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(IS, "send", boom)

    audit = TS.tick(cfg)
    assert audit["escalated"] == 0
    assert faketg.sent == []
    # Marker stays pending (un-escalated) so a later tick retries the redirect.
    assert json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())["escalated"] is False


# ---------------------------------------------------------------------------
# T-0285: blocked_sids — the set surfaced as a per-session awaiting_input flag.
# ---------------------------------------------------------------------------

def test_blocked_sids_empty_when_no_markers(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert TS.blocked_sids(cfg, "bot-squad") == set()


def test_blocked_sids_includes_active_markers(tmp_path):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "need your call")
    TS.mark_blocked(cfg, "bot-squad", OP, "blocked too")
    assert TS.blocked_sids(cfg, "bot-squad") == {DEV, OP}


def test_blocked_sids_still_reports_an_escalated_marker(tmp_path, faketg, monkeypatch):
    """An escalated (one-shot TG-pinged) marker still means the agent is
    waiting — it should remain in the awaiting-input set until cleared."""
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)  # not watched → escalates via TG
    TS.tick(cfg)
    assert json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())["escalated"] is True
    assert DEV in TS.blocked_sids(cfg, "bot-squad")


def test_blocked_sids_drops_stale_marker_past_ttl(tmp_path):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, TS._MARKER_TTL_SEC + 60)
    assert TS.blocked_sids(cfg, "bot-squad") == set()


def test_blocked_sids_cleared_marker_drops_out(tmp_path):
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    assert DEV in TS.blocked_sids(cfg, "bot-squad")
    TS.clear_blocked(cfg, "bot-squad", DEV)
    assert TS.blocked_sids(cfg, "bot-squad") == set()


def test_escalation_max_primary_with_group_record(tmp_path, monkeypatch):
    """T-0394: needs-input escalation pages via MAX (primary) + leaves a
    best-effort #team-queries group-record in TG (D1)."""
    from bot_squad_worker import actions as A, tg_topics
    cfg = _make_cfg(tmp_path)
    # Configure MAX as the primary channel.
    cfg = types.SimpleNamespace(**{**cfg.__dict__, "max_default_chat_id": "MAXID",
                                   "max_recipient_kind": "chat_id"})
    tg_topics.save(cfg, "bot-squad", {"team_queries": 3131})

    max_calls, tg_calls = [], []
    monkeypatch.setattr(A, "_MAX", types.SimpleNamespace(
        send=lambda **k: (max_calls.append(k) or True)))
    monkeypatch.setattr(A, "_TG", types.SimpleNamespace(
        send=lambda **k: (tg_calls.append(k) or True)))

    TS.mark_blocked(cfg, "bot-squad", DEV, "need prod call")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)

    TS.tick(cfg)
    assert len(max_calls) == 1                       # personal page via MAX
    assert max_calls[0]["chat_id"] == "MAXID"
    assert len(tg_calls) == 1                        # best-effort group-record
    assert tg_calls[0]["topic_id"] == 3131           # into #team-queries
