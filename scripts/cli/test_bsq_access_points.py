"""T-0508 (M11/F11.1): three points of access — the two NEW CLI verbs.

The stakeholder named three access points (clarification-03): async mail,
attach-and-write to any session (most often the operator), and launching a
dedicated user session. AP#1 (async mail) is already live (M5 tg_listener ->
ensure_user_conversation). This pins the two NEW first-class verbs that wrap
ALREADY-LIVE worker primitives:

  AP#2  ``bsq write <sid> <text>``         -> inject_input {sid, text}
  AP#3  ``bsq user-session <slug> <gid>``  -> ensure_user_conversation

The worker actions enforce a STRICT allowed-param set (reject unexpected keys),
so the client must send EXACTLY the right params — these tests lock that, plus
the idempotent-reuse reporting that proves T-0478 single-attendant to a human.

`bsq` is extensionless, so it's loaded via SourceFileLoader (mirrors
test_bsq_peer_signal.py).
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

SLUG = "proj"


@pytest.fixture(autouse=True)
def _stub_slug(monkeypatch):
    """No tmux/cwd resolution in tests — pin slug."""
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: SLUG)


def _record_post(returns):
    """Return (post_fn, calls); post_fn replies per-action from `returns`."""
    calls = []

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        return returns.get(action, {"ok": True})

    return fake_post, calls


# ---------------------------------------------------------------------------
# AP#2 — bsq write <sid> <text>  ->  inject_input
# ---------------------------------------------------------------------------
def test_write_injects_input_to_target_sid(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"inject_input": {"ok": True, "pane_id": "%4", "lines_sent": 1}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_write(SimpleNamespace(sid="S-u-op-p2", text="please ship it"))

    # Exactly the strict-allowed params — no slug, no extras.
    assert calls == [("inject_input", {"sid": "S-u-op-p2", "text": "please ship it"})]
    assert "S-u-op-p2" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# AP#3 — bsq user-session <slug> <gid> [--message]  ->  ensure_user_conversation
# ---------------------------------------------------------------------------
def test_user_session_launches_attendant(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"ensure_user_conversation":
            {"ok": True, "sid": "S-u-gid-uc-p9", "spawned": True}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_user_session(
        SimpleNamespace(slug=SLUG, global_user_id="gu_abc", message=None))

    # No message_ref key when --message is absent (strict allowed-set).
    assert calls == [("ensure_user_conversation",
                      {"slug": SLUG, "global_user_id": "gu_abc"})]
    out = capsys.readouterr().out
    assert "launched" in out and "S-u-gid-uc-p9" in out


def test_user_session_reuse_reported_not_relaunched(monkeypatch, capsys):
    # T-0478: a 2nd call for the same (slug,gid) REUSES the attendant. The verb
    # must report the reuse, never claim it launched a new one.
    fake_post, _ = _record_post(
        {"ensure_user_conversation":
            {"ok": True, "sid": "S-u-gid-uc-p9", "spawned": False}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_user_session(
        SimpleNamespace(slug=SLUG, global_user_id="gu_abc", message=None))

    out = capsys.readouterr().out
    assert "reused" in out
    assert "launched" not in out


def test_user_session_forwards_message_ref(monkeypatch):
    fake_post, calls = _record_post(
        {"ensure_user_conversation":
            {"ok": True, "sid": "S-x", "spawned": True}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_user_session(
        SimpleNamespace(slug=SLUG, global_user_id="gu_abc", message="F-0042"))

    assert calls[0][1] == {
        "slug": SLUG, "global_user_id": "gu_abc", "message_ref": "F-0042"}
