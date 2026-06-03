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

    def send(self, *, chat_id, text, sid="", user="", urgent=False):
        full = f"[{sid}] {text}" if sid else text
        self.sent.append({"chat_id": chat_id, "text": full})
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
    assert "Reply to this message" in msg       # reply-bridge hint
    assert "Remote-control" in msg              # /remote-control footer
    assert json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())["escalated"] is True

    # Second tick must NOT re-flood.
    audit2 = TS.tick(cfg)
    assert audit2["escalated"] == 0
    assert len(faketg.sent) == 1


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
    assert "Reply to this message" in body


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
