"""Tests for the synchronous inter-session channel (T-0498, M6/F6.2).

The sync channel is a thin coordination layer ON TOP of the verified M6 async
substrate (D-0037): the handshake (request -> ack -> enter) and the in-channel
messages are all delivered through ``intersession.send`` (durable per-SID
inbox), so they ride the same ``bsq inbox check`` / ``check mail`` bus. This
module adds only the handshake state machine + close-on-exit/timeout; it never
touches ``intersession.py`` delivery primitives.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import intersession as I
from bot_squad_worker import sync_channel as SC


A = "S-u-alice-p1"
B = "S-u-bob-p2"
C = "S-u-carol-p3"


def _cfg(tmp_path: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(data_dir=tmp_path / "data")


def _drain(cfg, sid) -> list[str]:
    return I.inbox_read(cfg, "p", sid)["messages"]


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------
def test_request_creates_channel_in_requested_state(tmp_path):
    cfg = _cfg(tmp_path)
    out = SC.request(cfg, "p", A, B)
    assert out["ok"] is True
    assert out["channel_id"] == "SC-1"
    assert out["status"] == "requested"
    assert out["requester"] == A
    assert out["responder"] == B


def test_request_ids_are_monotonic(tmp_path):
    cfg = _cfg(tmp_path)
    assert SC.request(cfg, "p", A, B)["channel_id"] == "SC-1"
    assert SC.request(cfg, "p", A, C)["channel_id"] == "SC-2"


def test_request_notifies_responder_inbox(tmp_path):
    """The responder is told (durably) to ack — rides the async inbox bus."""
    cfg = _cfg(tmp_path)
    out = SC.request(cfg, "p", A, B)
    assert out["notify"]["sid"] == B
    assert "SC-1" in out["notify"]["text"]
    msgs = _drain(cfg, B)
    assert any("SC-1" in m and "ack" in m.lower() for m in msgs)


# --------------------------------------------------------------------------
# ack
# --------------------------------------------------------------------------
def test_ack_by_responder_moves_to_acked_and_notifies_requester(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    _drain(cfg, B)  # clear request notice
    out = SC.ack(cfg, "p", B, "SC-1")
    assert out["status"] == "acked"
    assert out["notify"]["sid"] == A
    msgs = _drain(cfg, A)
    assert any("SC-1" in m and "enter" in m.lower() for m in msgs)


def test_ack_by_non_responder_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    with pytest.raises(SC.SyncError, match="responder"):
        SC.ack(cfg, "p", C, "SC-1")


def test_ack_unknown_channel_errors(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(SC.SyncError, match="unknown channel"):
        SC.ack(cfg, "p", B, "SC-99")


# --------------------------------------------------------------------------
# enter
# --------------------------------------------------------------------------
def test_enter_requires_ack_first(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    with pytest.raises(SC.SyncError, match="not acked"):
        SC.enter(cfg, "p", A, "SC-1")


def test_enter_by_both_opens_channel(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    SC.ack(cfg, "p", B, "SC-1")
    first = SC.enter(cfg, "p", A, "SC-1")
    assert first["status"] == "acked"
    assert first["both_in"] is False
    assert first["peer"] == B
    second = SC.enter(cfg, "p", B, "SC-1")
    assert second["status"] == "open"
    assert second["both_in"] is True


def test_enter_by_nonmember_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    SC.ack(cfg, "p", B, "SC-1")
    with pytest.raises(SC.SyncError, match="not a member"):
        SC.enter(cfg, "p", C, "SC-1")


# --------------------------------------------------------------------------
# send — the live 2-way channel (the DoD's bidirectional exchange)
# --------------------------------------------------------------------------
def _open_channel(cfg) -> None:
    SC.request(cfg, "p", A, B)
    SC.ack(cfg, "p", B, "SC-1")
    SC.enter(cfg, "p", A, "SC-1")
    SC.enter(cfg, "p", B, "SC-1")
    _drain(cfg, A)
    _drain(cfg, B)


def test_send_before_open_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    SC.ack(cfg, "p", B, "SC-1")
    SC.enter(cfg, "p", A, "SC-1")  # only one side in
    with pytest.raises(SC.SyncError, match="not open"):
        SC.send(cfg, "p", A, "SC-1", "too early")


def test_bidirectional_exchange(tmp_path):
    cfg = _cfg(tmp_path)
    _open_channel(cfg)

    # A -> B
    out = SC.send(cfg, "p", A, "SC-1", "hi bob")
    assert out["peer"] == B
    assert out["notify"]["sid"] == B
    b_msgs = _drain(cfg, B)
    assert any("hi bob" in m and "[from %s]" % A in m and "SC-1" in m for m in b_msgs)

    # B -> A
    SC.send(cfg, "p", B, "SC-1", "hey alice")
    a_msgs = _drain(cfg, A)
    assert any("hey alice" in m and "[from %s]" % B in m and "SC-1" in m for m in a_msgs)


def test_send_over_cap_raises_instead_of_truncating(tmp_path):
    """T-0827: sync is the one INTERNAL caller carrying agent-authored text, so
    it is the one that can actually hit the bus cap. A refusal must reach the
    sender as a SyncError like every other failure in this module — a live
    channel message that arrives cut mid-word is the "delivered, but not what
    you sent" outcome the bus must not produce, and the channel's own last-touch
    is deliberately NOT bumped for a message that never went.
    """
    cfg = _cfg(tmp_path)
    _open_channel(cfg)
    with pytest.raises(SC.SyncError, match="refusing to truncate"):
        SC.send(cfg, "p", A, "SC-1", "w" * 4100)
    assert _drain(cfg, B) == []


def test_send_by_nonmember_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    _open_channel(cfg)
    with pytest.raises(SC.SyncError, match="not a member"):
        SC.send(cfg, "p", C, "SC-1", "intrude")


# --------------------------------------------------------------------------
# close on exit / timeout
# --------------------------------------------------------------------------
def test_exit_closes_channel_and_blocks_further_send(tmp_path):
    cfg = _cfg(tmp_path)
    _open_channel(cfg)
    out = SC.exit_channel(cfg, "p", A, "SC-1")
    assert out["status"] == "closed"
    assert out["closed_reason"] == "exit"
    # peer is notified the channel closed
    assert out["notify"]["sid"] == B
    with pytest.raises(SC.SyncError, match="closed"):
        SC.send(cfg, "p", B, "SC-1", "still there?")


def test_idle_channel_times_out(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B, ttl=5, now=1000.0)
    # status checked well past the TTL with no activity -> auto-closed.
    st = SC.status(cfg, "p", channel_id="SC-1", now=2000.0)
    assert st["status"] == "closed"
    assert st["closed_reason"] == "timeout"


def test_activity_refreshes_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B, ttl=100, now=0.0)
    SC.ack(cfg, "p", B, "SC-1", now=50.0)
    SC.enter(cfg, "p", A, "SC-1", now=90.0)
    SC.enter(cfg, "p", B, "SC-1", now=120.0)
    # last activity at 120 with ttl=100 -> still open at 200.
    out = SC.send(cfg, "p", A, "SC-1", "still alive", now=200.0)
    assert out["ok"] is True


def test_send_after_timeout_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    _open_channel(cfg)  # opened at now=None (wall clock); use explicit ttl path
    SC.request(cfg, "p", A, C, ttl=5, now=0.0)
    SC.ack(cfg, "p", C, "SC-2", now=1.0)
    SC.enter(cfg, "p", A, "SC-2", now=2.0)
    SC.enter(cfg, "p", C, "SC-2", now=3.0)
    with pytest.raises(SC.SyncError, match="timed out|closed"):
        SC.send(cfg, "p", A, "SC-2", "late", now=999.0)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def test_status_reports_single_channel(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    st = SC.status(cfg, "p", channel_id="SC-1")
    assert st["channel_id"] == "SC-1"
    assert st["status"] == "requested"
    assert st["requester"] == A
    assert st["responder"] == B


def test_status_lists_channels_for_a_sid(tmp_path):
    cfg = _cfg(tmp_path)
    SC.request(cfg, "p", A, B)
    SC.request(cfg, "p", C, A)  # A is responder here
    st = SC.status(cfg, "p", sid=A)
    ids = {c["channel_id"] for c in st["channels"]}
    assert ids == {"SC-1", "SC-2"}
