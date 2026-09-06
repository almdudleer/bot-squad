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
TL = "S-almdudleer-teamlead-p31"


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

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None,
             debounce=True, route_sid="", delivery=None):  # route_sid: T-0719 raw routing sid
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
        lambda cfg, slug: [
            {"sid": DEV, "role": "dev"},
            {"sid": OP, "role": "operator"},
            {"sid": TL, "role": "teamlead"},
        ],
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

# T-0977 — THE ASK. The marker means "blocked on a reply". A REPORT filed to
# the operator is the opposite direction of information flow, and the old
# trigger could not tell them apart because it read the RECIPIENT'S ROLE, which
# is identical for both. Measured live 2026-09-06: one routine handler logged 28
# mark/clear pairs in 86 minutes without being blocked on anybody, and three
# markers reading "is blocked waiting on your reply" belonged to lanes that had
# just sent reports.
#
# This test is written to FAIL on the pre-fix module: `on_peer_send(DEV, [OP])`
# wrote a marker there. Verified RED against `git show HEAD:` before the fix
# landed — the assertion is the absence, so an untested absence would be a
# green that proves nothing.

def test_peer_send_report_to_operator_marks_NOTHING(tmp_path, fake_roles):
    """A lane reporting to the operator must arm no page — no flag, no marker."""
    cfg = _make_cfg(tmp_path)
    TS.on_peer_send(cfg, "bot-squad", DEV, [OP], text="READY T-0977 — shipped")
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_peer_send_declaring_blocked_marks(tmp_path, fake_roles):
    """POSITIVE CONTROL for the test above: the instrument CAN still write a
    marker, so that absence is about the declaration and not about a module
    that stopped marking altogether."""
    cfg = _make_cfg(tmp_path)
    msg = "need your call: drop the live markers or migrate them?"
    TS.on_peer_send(cfg, "bot-squad", DEV, [OP], blocked=True, text=msg)
    marker = TS._marker_path(cfg, "bot-squad", DEV)
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["origin"] == "declared"
    assert data["blocked_on"] == [OP]
    # The declaring message IS the description of the block. Every live marker
    # read on 2026-09-06 carried only the generic fallback, which told the
    # human nothing about what was blocked or on what.
    assert data["text"] == msg


def test_declared_block_text_is_not_truncated(tmp_path, fake_roles):
    """T-0721 removed a 400-char pre-cut so a long reason reaches the human in
    full (the SSOT splits into numbered parts, it does not truncate). Carrying
    the declaring message into the marker must not re-open that decision one
    layer earlier."""
    cfg = _make_cfg(tmp_path)
    reason = "Нужен твой выбор по деплою. " + "Вот весь контекст решения. " * 40
    TS.on_peer_send(cfg, "bot-squad", DEV, [OP], blocked=True, text=reason)
    stored = json.loads(TS._marker_path(cfg, "bot-squad", DEV).read_text())["text"]
    assert stored == reason.strip()   # only surrounding whitespace is dropped
    assert len(stored) > 1000 and "…" not in stored[-4:]


def test_declared_block_to_a_non_operator_also_marks(tmp_path, fake_roles):
    """Intent, not the recipient's role, is the trigger — so a dev genuinely
    stopped on its TL is marked too. `_route_idle_escalation` then delivers the
    escalation to that TL over the bus rather than paging the stakeholder
    (T-0034), so this widening cannot reach him."""
    cfg = _make_cfg(tmp_path)
    TS.on_peer_send(cfg, "bot-squad", DEV, [DEV], blocked=True, text="stuck")
    assert TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_peer_send_without_declaration_marks_nothing_whoever_the_recipient(
        tmp_path, fake_roles):
    cfg = _make_cfg(tmp_path)
    TS.on_peer_send(cfg, "bot-squad", DEV, [DEV, OP], text="fyi")
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_reply_from_the_party_owing_it_clears_marker(tmp_path, fake_roles):
    cfg = _make_cfg(tmp_path)
    TS.on_peer_send(cfg, "bot-squad", DEV, [OP], blocked=True, text="need prod call")
    TS.on_peer_send(cfg, "bot-squad", OP, [DEV], text="do it")
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


# T-0977 DoD 4 — the clear path carried the same defect in mirror image. It
# fired on "the sender's role is operator", a proxy for "the human answered"
# that ANY unrelated operator broadcast satisfied: a question nobody had read
# was silenced by a message that never looked at it. The block records WHO owes
# the reply, and only that party clears it.

def test_unrelated_operator_broadcast_does_not_clear_a_block_on_someone_else(
        tmp_path, fake_roles):
    cfg = _make_cfg(tmp_path)
    TS.on_peer_send(cfg, "bot-squad", DEV, [TL], blocked=True, text="need your review")
    TS.on_peer_send(cfg, "bot-squad", OP, [DEV], text="broadcast: suite lock lifted")
    assert TS._marker_path(cfg, "bot-squad", DEV).exists()
    # ...and the party actually owing the reply still clears it.
    TS.on_peer_send(cfg, "bot-squad", TL, [DEV], text="reviewed, go")
    assert not TS._marker_path(cfg, "bot-squad", DEV).exists()


def test_stall_sweep_marker_is_not_peer_clearable(tmp_path, fake_roles):
    """A `stall_sweep` marker means "stuck on a TUI modal nobody could
    auto-answer". No peer message unsticks that, so none may clear it — only
    the pane actually resuming does."""
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "stuck on the credits gate",
                    origin="stall_sweep")
    marker = TS._marker_path(cfg, "bot-squad", DEV)
    assert json.loads(marker.read_text())["blocked_on"] == []
    TS.on_peer_send(cfg, "bot-squad", OP, [DEV], text="hi")
    assert marker.exists()
    since = json.loads(marker.read_text())["since"]
    assert TS.clear_if_resumed(cfg, "bot-squad", DEV, since + 31) is True


# ---------------------------------------------------------------------------
# T-0599: never auto-mark one of the human's own sessions blocked on itself —
# journal-evidenced false positives (2026-07-05 11:20Z + 19:01Z), see
# recycle_gate.user_session_exempt (T-0564/T-0616) for the shared signal.
# ---------------------------------------------------------------------------

USERCONV = "S-almdudleer-gu_abc123-user-conversation-p12"
USERSESSION = "S-almdudleer-user-session-p8"


def test_peer_send_user_conversation_role_does_not_mark(tmp_path, monkeypatch):
    """A user-conversation session's peer_send to the operator is its normal
    one-way intake->operator notify (per its role contract), never a question
    awaiting a reply — must not be auto-marked blocked."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(
        S, "list_sessions",
        lambda cfg, slug: [
            {"sid": USERCONV, "role": "user-conversation", "window": "gu_abc123-user-conversation"},
            {"sid": OP, "role": "operator", "window": "operator"},
        ],
    )
    cfg = _make_cfg(tmp_path)
    # T-0977: pass the declaration explicitly. Without it this test would pass
    # vacuously — nothing marks any more — and would stop covering the
    # exemption it exists for.
    TS.on_peer_send(cfg, "bot-squad", USERCONV, [OP], blocked=True, text="q")
    assert not TS._marker_path(cfg, "bot-squad", USERCONV).exists()


def test_peer_send_hand_launched_user_session_window_does_not_mark(tmp_path, monkeypatch):
    """A hand-launched `user-session` window IS the stakeholder — they cannot
    be "blocked on" themselves, even though this window derives role `dev`
    (T-0175 default, D-0053 §4)."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(
        S, "list_sessions",
        lambda cfg, slug: [
            {"sid": USERSESSION, "role": "dev", "window": "user-session"},
            {"sid": OP, "role": "operator", "window": "operator"},
        ],
    )
    cfg = _make_cfg(tmp_path)
    # T-0977: declared explicitly, so the exemption is still what is doing the
    # work here rather than the new default-off trigger.
    TS.on_peer_send(cfg, "bot-squad", USERSESSION, [OP], blocked=True, text="q")
    assert not TS._marker_path(cfg, "bot-squad", USERSESSION).exists()


# ---------------------------------------------------------------------------
# tick escalation (scenario steps 4, 5, 6)
# ---------------------------------------------------------------------------

def _age_marker(cfg, sid, seconds):
    p = TS._marker_path(cfg, "bot-squad", sid)
    d = json.loads(p.read_text())
    d["since"] = time.time() - seconds
    p.write_text(json.dumps(d))


def _stub_pane(monkeypatch, visible: bool, present: bool = True, route=None, archived: bool = False):
    pane = types.SimpleNamespace(pane_id="%7", window="tg-gating", session="bot-squad")
    monkeypatch.setattr(TS, "_pane_for_sid", lambda sid: pane if present else None)
    monkeypatch.setattr(TS, "_window_visible", lambda pid: visible)
    # T-0034: default route=None means "no upstream → TG the stakeholder" (the
    # operator / prod-teamlead case), keeping these the TG-path tests they were.
    # A redirect test passes route=<target SID>.
    monkeypatch.setattr(TS, "_route_idle_escalation", lambda cfg, slug, sid: route)
    # T-0647: default archived=False keeps existing tests exercising the
    # normal escalation path; a suppression test passes archived=True.
    monkeypatch.setattr(TS, "_session_row", lambda cfg, slug, sid: {"archived": archived})


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


def test_tick_long_block_reason_splits_instead_of_being_cut(tmp_path, faketg, monkeypatch):
    """T-0721: the stall escalation used to pre-truncate the blocked session's
    reason at 400 chars (to protect the tmux-attach footer from the SSOT's
    blanket slim). Neither cut exists now — the whole reason AND the footer
    arrive, across numbered parts."""
    cfg = _make_cfg(tmp_path)
    reason = "Нужен твой выбор по деплою. " + "Вот весь контекст решения. " * 300
    TS.mark_blocked(cfg, "bot-squad", DEV, reason)
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)

    assert TS.tick(cfg)["escalated"] == 1
    texts = [s["text"] for s in faketg.sent]
    assert len(texts) > 1 and all(len(t) <= 4096 for t in texts)
    assert "Remote-control" in texts[-1]                 # footer survived
    import re as _re
    bodies = [_re.sub(r"^(\[[^\]]+\] )?(\(\d+/\d+\) )?", "", t) for t in texts]
    assert bodies[0].startswith("🔔")
    joined = "".join("".join(b.split()) for b in bodies)
    assert "".join(reason.split()) in joined            # nothing dropped
    assert "…подробнее" not in joined and "деталисм.задачу/тред" not in joined


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


# T-0977 DoD 5 — what happens to markers that are already on disk when this
# ships. They were written by the role-proxy trigger, so most of them mean "this
# lane filed a report", their 15-minute fuses are already burning, and nothing
# on disk can re-classify them after the fact. The worker's own tick drops
# them: no operator has to remember a sweep, and nothing evaporates when the
# operator compacts, is recycled, is reaped or crashes.

def _write_legacy_marker(cfg, sid, age_sec):
    """The EXACT pre-T-0977 on-disk shape — three of these were read live at
    2026-09-06 15:58Z, all belonging to lanes that had sent reports."""
    p = TS._marker_path(cfg, "bot-squad", sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "sid": sid, "slug": "bot-squad", "since": time.time() - age_sec,
        "text": "is blocked waiting on your reply", "escalated": False,
    }, indent=2))
    return p


def test_tick_drops_pre_t0977_marker_instead_of_escalating(tmp_path, faketg, monkeypatch):
    cfg = _make_cfg(tmp_path)
    marker = _write_legacy_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)

    audit = TS.tick(cfg)
    assert audit["escalated"] == 0
    assert audit["legacy_dropped"] == 1
    assert faketg.sent == []
    assert not marker.exists()


def test_tick_still_escalates_a_declared_marker_of_the_same_age(tmp_path, faketg, monkeypatch):
    """POSITIVE CONTROL for the test above. Same age, same tick, same stubs —
    the only difference is the `origin` key. Without this, that zero could just
    mean the tick no longer escalates anything."""
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "need prod call",
                    origin="declared", blocked_on=[OP])
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False)

    audit = TS.tick(cfg)
    assert audit["escalated"] == 1
    assert audit["legacy_dropped"] == 0
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


def test_tick_archived_session_drops_marker_no_escalation(tmp_path, faketg, monkeypatch):
    """T-0647: a session whose work is already complete (archived — result
    written / ticket closed) must not idle-nag anyone, TL or operator or
    stakeholder. Journal-evidenced: p16 was already
    archive_reason=dead-binding:task-closed when it idle-nagged a TL."""
    cfg = _make_cfg(tmp_path)
    TS.mark_blocked(cfg, "bot-squad", DEV, "x")
    _age_marker(cfg, DEV, 16 * 60)
    _stub_pane(monkeypatch, visible=False, archived=True)  # would otherwise TG-escalate

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


def _stub_rows(monkeypatch, rows):
    """Stub the session list the router consults."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: rows)


def test_route_dev_with_teamlead_parent_returns_tl(tmp_path, monkeypatch):
    """A dev's genuine (non-heuristic) parent_sid pointing at a live TL row
    routes there."""
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev", "parent_sid": TL, "parent_sid_heuristic": False},
         {"sid": TL, "role": "teamlead"},
         {"sid": OP, "role": "operator"}],
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) == TL


def test_route_dev_parent_is_operator_returns_operator(tmp_path, monkeypatch):
    """A dev spawned directly by the operator (parent_sid = the operator's own
    SID) routes straight to the operator."""
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev", "parent_sid": OP, "parent_sid_heuristic": False},
         {"sid": OP, "role": "operator"}],
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) == OP


def test_route_dev_no_parent_recorded_returns_operator(tmp_path, monkeypatch):
    """No parent_sid at all (ad-hoc dev, never backfilled) → operator, not a
    team broadcast."""
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev"}, {"sid": OP, "role": "operator"}],
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) == OP


# --- T-0647 regression: the actual reported bug ----------------------------
# Journal-evidenced 2026-07-18: three operator-parented, already-finished
# devs (S-almdudleer-tg-outage-p11, S-almdudleer-settings-max-fix-p16,
# S-almdudleer-quota-path-fix-p34) each idle-nagged the SAME unrelated TL
# (S-almdudleer-gateway-routing-tl-p23) that had no relationship to any of
# them — because the old routing derived its target from whichever TL was
# newest/live in the project's single shared team roster
# (teams.tl_for_sid), not from each dev's actual parent binding.

def test_route_dev_heuristic_parent_ignored_falls_back_to_operator(tmp_path, monkeypatch):
    """A parent_sid backfill *guessed* (nearest-live-TL heuristic, not a
    genuine spawn-time link) must never be trusted as the actual parent —
    that guess is exactly the mechanism that mis-routed p11/p16/p34's
    idle-nags onto unrelated TL p23."""
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev", "parent_sid": TL, "parent_sid_heuristic": True},
         {"sid": TL, "role": "teamlead"},
         {"sid": OP, "role": "operator"}],
    )
    assert TS._route_idle_escalation(cfg, "bot-squad", DEV) == OP


def test_route_dev_operator_parented_never_broadcasts_to_unrelated_live_tl(tmp_path, monkeypatch):
    """An operator-parented dev with no parent binding at all must go
    straight to the operator even when an unrelated TL happens to be alive in
    the roster — never a role-broadcast / nearest-live-TL fallback."""
    cfg = _make_cfg(tmp_path)
    _stub_rows(
        monkeypatch,
        [{"sid": DEV, "role": "dev"},
         {"sid": TL, "role": "teamlead"},   # a live TL exists, but is unrelated
         {"sid": OP, "role": "operator"}],
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


def test_escalation_tg_primary_single_delivery(tmp_path, monkeypatch):
    """T-0394 → T-0610 inversion: needs-input escalation pages via TG (primary)
    into #team-queries — ONE delivery; MAX stays reserve even when configured."""
    from bot_squad_worker import actions as A, tg_topics
    cfg = _make_cfg(tmp_path)
    # MAX configured — must stay reserve-only.
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
    assert len(tg_calls) == 1                        # personal page via TG
    assert tg_calls[0]["topic_id"] == 3131           # into #team-queries
    assert max_calls == []                           # one page = one delivery (T-0610)


# ---------------------------------------------------------------------------
# T-0300 — remote_control_line is THE single composer of the handoff
#
# The escalation footer and the tg_listener `/remote-control` command both emit
# this. One decision written twice is this repo's #1 bug class, so these pin
# that they cannot drift apart.
# ---------------------------------------------------------------------------

def test_remote_control_line_configured_url_substitutes(tmp_path):
    cfg = _make_cfg(tmp_path, remote_url="https://claude.ai/code?s={sid}&w={session}")
    line = TS.remote_control_line(cfg, DEV, "some-window")
    assert line == f"🖥 Remote-control (Claude app): https://claude.ai/code?s={DEV}&w=some-window"


def test_remote_control_line_falls_back_to_tmux_attach(tmp_path):
    cfg = _make_cfg(tmp_path, remote_url="")
    assert TS.remote_control_line(cfg, DEV, "some-window") == (
        "🖥 Remote-control: tmux attach -t some-window")


def test_remote_control_line_empty_when_nothing_to_offer(tmp_path):
    """No configured URL and no live pane — callers append it only when truthy."""
    cfg = _make_cfg(tmp_path, remote_url="")
    assert TS.remote_control_line(cfg, DEV, "") == ""


def test_remote_control_line_resolves_session_name_when_not_given(tmp_path, monkeypatch):
    """`/remote-control` has no PaneInfo in hand, so None means 'look it up'."""
    cfg = _make_cfg(tmp_path, remote_url="")
    monkeypatch.setattr(TS, "_pane_for_sid",
                        lambda sid: types.SimpleNamespace(session="looked-up"))
    assert TS.remote_control_line(cfg, DEV) == (
        "🖥 Remote-control: tmux attach -t looked-up")


@pytest.mark.parametrize("url", ["", "https://claude.ai/code?session={sid}"])
def test_escalation_footer_is_the_same_line_the_command_emits(tmp_path, url):
    """The drift guard: the footer must CONTAIN the composer's output verbatim."""
    cfg = _make_cfg(tmp_path, remote_url=url)
    body = TS.build_escalation_text(cfg, DEV, "need call", "bot-squad")
    assert TS.remote_control_line(cfg, DEV, "bot-squad") in body


def test_escalation_text_drops_handoff_when_nothing_to_offer(tmp_path):
    """Unchanged pre-T-0300 behaviour: no url + no session name -> no footer."""
    cfg = _make_cfg(tmp_path, remote_url="")
    body = TS.build_escalation_text(cfg, DEV, "need call", "")
    assert "Remote-control" not in body
    assert "#team-queries" in body
