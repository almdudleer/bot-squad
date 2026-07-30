"""Tests for the cross-session message bus (bot_squad_worker.intersession)."""
from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import intersession as I


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(data_dir=tmp_path / "data")


def _write_session(
    tmp_path: Path, slug: str, sid: str, task_id: str = "",
    *, status: str = "active", archived: bool = False, window: str = "",
) -> None:
    sess_dir = tmp_path / "data" / slug / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"sid: {sid}",
        f"status: {status}",
        f"task_id: {task_id or '~'}",
    ]
    # T-0790: the window is what `sessions._derive_role` reads, so any test
    # exercising role identity (the `operator` keyword) has to carry it — a real
    # session md always does.
    if window:
        lines.append(f"window: {window}")
    if archived:
        lines.append("archived: true")
    lines += ["---", ""]
    (sess_dir / f"{sid}.md").write_text("\n".join(lines))


def test_send_then_read_roundtrip(tmp_path):
    cfg = _make_cfg(tmp_path)
    out = I.send(cfg, "p", "S-from", "S-to", "hello world")
    assert out["ok"] is True
    assert out["delivered_to"] == ["S-to"]
    read = I.inbox_read(cfg, "p", "S-to")
    assert read["count"] == 1
    assert "hello world" in read["messages"][0]
    assert "[from S-from]" in read["messages"][0]


def test_read_idempotent_after_drain(tmp_path):
    cfg = _make_cfg(tmp_path)
    I.send(cfg, "p", "S-from", "S-to", "one")
    I.send(cfg, "p", "S-from", "S-to", "two")
    first = I.inbox_read(cfg, "p", "S-to")
    assert first["count"] == 2
    second = I.inbox_read(cfg, "p", "S-to")
    assert second["count"] == 0


def test_send_to_unknown_sid_still_writes_inbox(tmp_path):
    """An SID with no sessions/*.md entry should still get its own inbox file."""
    cfg = _make_cfg(tmp_path)
    out = I.send(cfg, "p", "S-from", "S-ghost", "boo")
    assert out["delivered_to"] == ["S-ghost"]
    inbox = tmp_path / "data" / "p" / "_chat" / "inbox-S-ghost.log"
    assert inbox.exists()
    read = I.inbox_read(cfg, "p", "S-ghost")
    assert read["count"] == 1


def test_text_sanitisation(tmp_path):
    cfg = _make_cfg(tmp_path)
    long = "x" * 5000
    I.send(cfg, "p", "S-from", "S-to", "line1\nline2\rline3\r\nline4")
    I.send(cfg, "p", "S-from", "S-to", long)
    read = I.inbox_read(cfg, "p", "S-to")
    assert len(read["messages"]) == 2
    # Newlines collapsed to spaces — message line has no embedded \n
    assert "\n" not in read["messages"][0]
    assert "line1 line2 line3 line4" in read["messages"][0]
    # 4000-char cap
    assert read["messages"][1].endswith("x" * 100)
    # Total length budget = ts + "[from S-from]\t" + body; body <= 4000
    assert read["messages"][1].count("x") == 4000


def test_role_fanout_teamlead_and_dev(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-tl1-p0")           # teamlead (no task_id)
    _write_session(tmp_path, "p", "S-u-tl2-p1")           # teamlead
    _write_session(tmp_path, "p", "S-u-w1-p2", "T-0001")  # dev
    _write_session(tmp_path, "p", "S-u-w2-p3", "T-0002")  # dev

    tl = I.send(cfg, "p", "S-orchestrator", "teamlead", "hi TLs")
    assert sorted(tl["delivered_to"]) == ["S-u-tl1-p0", "S-u-tl2-p1"]

    dv = I.send(cfg, "p", "S-orchestrator", "dev", "hi devs")
    assert sorted(dv["delivered_to"]) == ["S-u-w1-p2", "S-u-w2-p3"]

    al = I.send(cfg, "p", "S-orchestrator", "all", "hi everyone")
    assert sorted(al["delivered_to"]) == sorted([
        "S-u-tl1-p0", "S-u-tl2-p1", "S-u-w1-p2", "S-u-w2-p3",
    ])


def test_role_fanout_excludes_dead_and_archived_sessions(tmp_path):
    """T-0683: a role broadcast (teamlead/dev/all) must only reach LIVE
    sessions (status active/paused, not archived) — not every session md
    ever written for the project. Regression for the inject_input 400 storm
    (~431 calls in <3min) caused by broadcasting to a large historical fleet.
    """
    cfg = _make_cfg(tmp_path)
    # Live roster: one live TL, one live dev.
    _write_session(tmp_path, "p", "S-u-tl-live-p0", status="active")
    _write_session(tmp_path, "p", "S-u-dev-live-p1", "T-0001", status="paused")
    # Dead roster: suspended (no live pane, never archived), and explicitly
    # archived — both should be excluded from every role-keyword fan-out.
    _write_session(tmp_path, "p", "S-u-tl-suspended-p2", status="suspended")
    _write_session(
        tmp_path, "p", "S-u-dev-archived-p3", "T-0002",
        status="suspended", archived=True,
    )

    tl = I.send(cfg, "p", "S-orchestrator", "teamlead", "hi TLs")
    assert tl["delivered_to"] == ["S-u-tl-live-p0"]

    dv = I.send(cfg, "p", "S-orchestrator", "dev", "hi devs")
    assert dv["delivered_to"] == ["S-u-dev-live-p1"]

    al = I.send(cfg, "p", "S-orchestrator", "all", "hi everyone")
    assert sorted(al["delivered_to"]) == ["S-u-dev-live-p1", "S-u-tl-live-p0"]


def test_wait_timeout_returns_not_ready(tmp_path):
    cfg = _make_cfg(tmp_path)
    t0 = time.monotonic()
    out = I.inbox_wait(cfg, "p", "S-to", timeout=1.5)
    elapsed = time.monotonic() - t0
    assert out["ok"] is True
    assert out["ready"] is False
    assert 1.0 <= elapsed <= 3.5
    assert out["elapsed_sec"] >= 1.0


def test_wait_returns_ready_when_inbox_grows(tmp_path):
    cfg = _make_cfg(tmp_path)

    def writer():
        time.sleep(0.4)
        I.send(cfg, "p", "S-from", "S-to", "ping")

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    out = I.inbox_wait(cfg, "p", "S-to", timeout=5)
    t.join(timeout=2)
    assert out["ready"] is True
    # And a follow-up read drains the message.
    read = I.inbox_read(cfg, "p", "S-to")
    assert read["count"] == 1


def test_wait_caps_timeout(tmp_path, monkeypatch):
    """Timeout > 7200 (the T-0091 cap) should be clamped."""
    cfg = _make_cfg(tmp_path)
    # Patch time.sleep + monotonic to verify clamp without burning 7200s.
    calls: list[float] = []
    real_monotonic = time.monotonic
    start = real_monotonic()
    fake_now = [start]

    def fake_monotonic():
        return fake_now[0]

    def fake_sleep(s):
        calls.append(s)
        fake_now[0] += s

    monkeypatch.setattr(I.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(I.time, "sleep", fake_sleep)
    out = I.inbox_wait(cfg, "p", "S-to", timeout=99999)
    assert out["ready"] is False
    # Should have slept its way to roughly 7200s, not 99999s.
    assert sum(calls) <= I._MAX_WAIT_TIMEOUT + 1
    assert out["elapsed_sec"] <= I._MAX_WAIT_TIMEOUT + 1


def test_wait_returns_early_on_shutdown_event_arg(tmp_path):
    """T-0119: explicit shutdown_event arg trips inbox_wait within one poll."""
    cfg = _make_cfg(tmp_path)
    ev = threading.Event()
    ev.set()  # already set — should return on the first loop iteration

    t0 = time.monotonic()
    out = I.inbox_wait(cfg, "p", "S-to", timeout=30, shutdown_event=ev)
    elapsed = time.monotonic() - t0

    assert out["ok"] is True
    assert out["ready"] is False
    assert out["reason"] == "shutdown"
    assert elapsed < 2.0  # must NOT wait the 30s timeout


def test_wait_returns_early_on_shutdown_event_global(tmp_path):
    """T-0119: process-wide event set via set_shutdown_event also works."""
    cfg = _make_cfg(tmp_path)
    ev = threading.Event()
    I.set_shutdown_event(ev)
    try:
        def trip():
            time.sleep(0.3)
            ev.set()
        threading.Thread(target=trip, daemon=True).start()

        t0 = time.monotonic()
        out = I.inbox_wait(cfg, "p", "S-to", timeout=30)
        elapsed = time.monotonic() - t0

        assert out["ready"] is False
        assert out["reason"] == "shutdown"
        assert elapsed < 3.0
    finally:
        I.set_shutdown_event(None)


def test_chat_dir_created_with_group_write(tmp_path):
    cfg = _make_cfg(tmp_path)
    I.send(cfg, "p", "S-from", "S-to", "hi")
    chat = tmp_path / "data" / "p" / "_chat"
    assert chat.is_dir()
    mode = chat.stat().st_mode & 0o777
    # Some tmpfs / CI filesystems strip group-write; assert at least the
    # owner+group can write. The chmod is best-effort.
    assert mode & 0o600


# ---------------------------------------------------------------------------
# T-0157: multi-user peer boundaries — role-keyword fan-out is scoped to one
# linux user by default; an explicit `user` widens it; literal SIDs always pass.
# ---------------------------------------------------------------------------

def _write_user_session(tmp_path, slug, sid, *, task_id="", linux_user=None):
    """Write a session md; linux_user defaults to the SID prefix (omitted field)."""
    sess_dir = tmp_path / "data" / slug / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"sid: {sid}", "status: active", f"task_id: {task_id or '~'}"]
    if linux_user is not None:
        lines.append(f"linux_user: {linux_user}")
    lines += ["---", ""]
    (sess_dir / f"{sid}.md").write_text("\n".join(lines))


def test_resolve_recipients_scopes_to_sender_user(tmp_path):
    cfg = _make_cfg(tmp_path)
    # alice TL + alice dev; bob TL + bob dev. linux_user derived from SID prefix.
    _write_user_session(tmp_path, "p", "S-alice-tl-p1", task_id="")
    _write_user_session(tmp_path, "p", "S-alice-dev-p2", task_id="T-1")
    _write_user_session(tmp_path, "p", "S-bob-tl-p3", task_id="")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")

    # alice sends to "all" → only alice's sessions.
    got = I._resolve_recipients(cfg, "p", "all", from_sid="S-alice-tl-p1")
    assert set(got) == {"S-alice-tl-p1", "S-alice-dev-p2"}

    # alice → "dev" → only alice's dev.
    assert I._resolve_recipients(cfg, "p", "dev", from_sid="S-alice-tl-p1") == ["S-alice-dev-p2"]


def test_resolve_recipients_user_override_crosses_boundary(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-alice-tl-p1", task_id="")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")

    # alice explicitly targets bob's user.
    got = I._resolve_recipients(cfg, "p", "all", from_sid="S-alice-tl-p1", user="bob")
    assert got == ["S-bob-dev-p4"]


def test_resolve_recipients_legacy_sender_unscoped(tmp_path):
    """A non-SID sender (e.g. 'stakeholder') resolves to no scope → unfiltered."""
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-alice-tl-p1", task_id="")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    got = I._resolve_recipients(cfg, "p", "all", from_sid="stakeholder")
    assert set(got) == {"S-alice-tl-p1", "S-bob-dev-p4"}


def test_resolve_recipients_explicit_field_wins_over_sid(tmp_path):
    """An explicit linux_user field overrides the SID prefix for scoping."""
    cfg = _make_cfg(tmp_path)
    # SID says 'svc' but the session is really owned by alice (field wins).
    _write_user_session(tmp_path, "p", "S-svc-tl-p9", task_id="", linux_user="alice")
    _write_user_session(tmp_path, "p", "S-alice-dev-p2", task_id="T-1")
    # A bob session makes the project genuinely multi-user so scoping engages.
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    got = I._resolve_recipients(cfg, "p", "all", from_sid="S-alice-dev-p2")
    # alice-scoped → the svc-named-but-alice-owned TL is included; bob excluded.
    assert set(got) == {"S-svc-tl-p9", "S-alice-dev-p2"}


def test_resolve_recipients_literal_sid_never_scoped(tmp_path):
    """Addressing a specific cross-user SID is always allowed (explicit)."""
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    assert I._resolve_recipients(cfg, "p", "S-bob-dev-p4", from_sid="S-alice-tl-p1") == ["S-bob-dev-p4"]


def test_send_scopes_role_fanout_to_sender_user(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_user_session(tmp_path, "p", "S-alice-dev-p2", task_id="T-1")
    _write_user_session(tmp_path, "p", "S-bob-dev-p4", task_id="T-2")
    out = I.send(cfg, "p", "S-alice-tl-p1", "dev", "ping")
    assert out["delivered_to"] == ["S-alice-dev-p2"]


# ── T-0447 (#4): cascade-reap the per-SID peer-bus _chat triple on archive ────

def _chat(tmp_path, slug="p"):
    return tmp_path / "data" / slug / "_chat"


def test_reap_chat_sidecars_removes_triple(tmp_path):
    cfg = _make_cfg(tmp_path)
    chat = _chat(tmp_path)
    chat.mkdir(parents=True)
    sid = "S-to"
    (chat / f"inbox-{sid}.log").write_text("hi\n")
    (chat / f"seen-{sid}").write_text("0")
    (chat / f"heartbeat-{sid}").write_text("")
    removed = I.reap_chat_sidecars(cfg, "p", sid)
    assert len(removed) == 3
    assert not (chat / f"inbox-{sid}.log").exists()
    assert not (chat / f"seen-{sid}").exists()
    assert not (chat / f"heartbeat-{sid}").exists()


def test_reap_chat_sidecars_partial_missing_and_idempotent(tmp_path):
    cfg = _make_cfg(tmp_path)
    # No _chat dir at all → no error, nothing removed.
    assert I.reap_chat_sidecars(cfg, "p", "S-none") == []
    # Only the inbox present (lazy-created triple).
    chat = _chat(tmp_path)
    chat.mkdir(parents=True)
    (chat / "inbox-S-solo.log").write_text("x")
    removed = I.reap_chat_sidecars(cfg, "p", "S-solo")
    assert len(removed) == 1
    # Re-run is a no-op (idempotent).
    assert I.reap_chat_sidecars(cfg, "p", "S-solo") == []


def test_reap_chat_sidecars_empty_sid_noop(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert I.reap_chat_sidecars(cfg, "p", "") == []


# ---------------------------------------------------------------------------
# T-0790: peer_send to a RECYCLED session wrote into the dead session's inbox
# and returned success. Measured on the live install 2026-07-30: operator
# `…-p374` was replaced by `…-p455` at 16:20Z; two verbatim stakeholder relays
# sent at 20:08:49Z / 20:12:06Z landed in inbox-…-p374.log and the operator on
# duty never learned they existed.
#
# A recycle is not a resume: it mints a BRAND-NEW SID in the SAME window and
# leaves the predecessor's md as `status: suspended`, so `rebind_sid` (which
# only fires on resume()) never moves the inbox.
#
# The next four tests are RED PINS — each fails at 423a058.
# ---------------------------------------------------------------------------

_DEAD = "S-almdudleer-operator-p374"
_LIVE = "S-almdudleer-operator-p455"


def _recycled_pair(tmp_path: Path, slug: str = "p") -> None:
    """The measured live shape: a suspended predecessor + its live successor,
    same linux user, same window stem, different pane."""
    _write_session(tmp_path, slug, _DEAD, status="suspended", window="operator")
    _write_session(tmp_path, slug, _LIVE, status="active", window="operator")


def test_send_to_recycled_sid_routes_to_live_successor(tmp_path):
    """RED PIN — the reported bug. A send addressed at the recycled SID must
    reach the live holder of its window, not the dead inbox."""
    cfg = _make_cfg(tmp_path)
    _recycled_pair(tmp_path)

    out = I.send(cfg, "p", "S-almdudleer-uc-p5", _DEAD, "verbatim stakeholder relay")

    assert out["delivered_to"] == [_LIVE]
    assert I.inbox_read(cfg, "p", _LIVE)["count"] == 1
    assert not (_chat(tmp_path) / f"inbox-{_DEAD}.log").exists()


def test_send_to_recycled_sid_reports_the_redirect(tmp_path):
    """RED PIN — a SILENT redirect is the same class of problem as the silent
    loss, so the sender is told the SID it is carrying is stale."""
    cfg = _make_cfg(tmp_path)
    _recycled_pair(tmp_path)

    out = I.send(cfg, "p", "S-almdudleer-uc-p5", _DEAD, "hi")

    assert out["redirected"] == {"from": _DEAD, "to": _LIVE, "reason": "recycled"}


def test_operator_is_a_role_keyword_resolving_to_live_operators(tmp_path):
    """RED PIN — `operator` was NOT a role keyword, so it fell through to the
    literal-SID branch and resolved to the string "operator"."""
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _LIVE, status="active", window="operator")

    assert I._resolve_recipients(cfg, "p", "operator") == [_LIVE]
    assert "operator" in I._ROLE_KEYWORDS


def test_send_to_operator_never_writes_the_ownerless_inbox(tmp_path):
    """RED PIN — `to="operator"` wrote _chat/inbox-operator.log, a file no
    session owns or drains. Three internal escalation callers address it that
    way (autocompact, uc_redrive, recovery); the live install had 27 undrained
    lines there, 22 of them uc_redrive "needs a human look"."""
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _LIVE, status="active", window="operator")

    out = I.send(cfg, "p", "S-uc-redrive", "operator", "needs a human look")

    assert out["delivered_to"] == [_LIVE]
    assert not (_chat(tmp_path) / "inbox-operator.log").exists()
    assert I.inbox_read(cfg, "p", _LIVE)["count"] == 1


# --- GREEN GUARDS: pass at 423a058 too. These fence the ways this fix could
# --- regress the bus, which the ticket rates worse than the bug itself.


def test_live_target_is_never_redirected(tmp_path):
    """A live holder reads its own inbox — nothing to redirect, no field set."""
    cfg = _make_cfg(tmp_path)
    _recycled_pair(tmp_path)

    out = I.send(cfg, "p", "S-almdudleer-uc-p5", _LIVE, "direct")

    assert out["delivered_to"] == [_LIVE]
    assert "redirected" not in out


def test_suspended_target_with_no_successor_still_gets_the_durable_write(tmp_path):
    """The (b)-as-written regression, refused deliberately and pinned here.

    "No live pane" is a legitimate SUCCESS on this bus: resume() rotates the SID
    and rebind_sid MOVES the triple, so a suspended-pending-resume peer reads its
    mail on the way back. Treating a failed inject as a delivery failure would
    break every such send.
    """
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _DEAD, status="suspended", window="operator")

    out = I.send(cfg, "p", "S-almdudleer-uc-p5", _DEAD, "read this on resume")

    assert out["delivered_to"] == [_DEAD]
    assert "redirected" not in out
    assert I.inbox_read(cfg, "p", _DEAD)["count"] == 1


def test_ambiguous_successor_is_never_guessed(tmp_path):
    """Two live sessions in one window → no redirect. A wrong redirect on this
    bus is worse than the loss it would prevent."""
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _DEAD, status="suspended", window="operator")
    _write_session(tmp_path, "p", _LIVE, status="active", window="operator")
    _write_session(tmp_path, "p", "S-almdudleer-operator-p456", status="active", window="operator")

    out = I.send(cfg, "p", "S-almdudleer-uc-p5", _DEAD, "ambiguous")

    assert out["delivered_to"] == [_DEAD]
    assert "redirected" not in out


def test_live_session_in_another_window_is_not_a_successor(tmp_path):
    """A live session in ANOTHER window is not a successor — only the same seat
    (same linux user + window stem) counts. Driven through `send` so this fences
    the delivered behaviour at 423a058 too, not just the new helper."""
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _DEAD, status="suspended", window="operator")
    _write_session(tmp_path, "p", "S-almdudleer-teamlead-p9", status="active", window="teamlead")

    out = I.send(cfg, "p", "S-almdudleer-uc-p5", _DEAD, "not for the TL")

    assert out["delivered_to"] == [_DEAD]
    assert "redirected" not in out
    assert I.inbox_read(cfg, "p", "S-almdudleer-teamlead-p9")["count"] == 0


def test_same_window_under_another_linux_user_is_not_a_successor(tmp_path):
    """Same window stem under a DIFFERENT linux user is a different seat — the
    T-0157 multi-user boundary must hold for the redirect too."""
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _DEAD, status="suspended", window="operator")
    _write_session(tmp_path, "p", "S-otheruser-operator-p455", status="active", window="operator")

    out = I.send(cfg, "p", "S-almdudleer-uc-p5", _DEAD, "stays on this user")

    assert out["delivered_to"] == [_DEAD]
    assert "redirected" not in out
    assert I.inbox_read(cfg, "p", "S-otheruser-operator-p455")["count"] == 0


def test_role_keywords_reach_operator_sessions_as_before(tmp_path):
    """Adding the `operator` keyword must not narrow teamlead/all: an operator
    session (no task_id) is still in both fan-outs."""
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _LIVE, status="active", window="operator")

    assert I._resolve_recipients(cfg, "p", "teamlead") == [_LIVE]
    assert I._resolve_recipients(cfg, "p", "all") == [_LIVE]


def test_role_fanout_sets_no_redirect_field(tmp_path):
    """A fan-out delivering to a SID other than the literal `to` is not a
    redirect — the field must stay absent."""
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", _LIVE, status="active", window="operator")

    for role in ("teamlead", "dev", "all", "operator"):
        out = I.send(cfg, "p", "S-almdudleer-uc-p5", role, "fanout")
        assert "redirected" not in out, role
