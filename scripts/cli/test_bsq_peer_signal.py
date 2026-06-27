"""T-0497 (M6 async-mail VERIFY): lock the `bsq peer send`/`bsq dispatch`
"new mail" notify contract.

The async substrate is two layers:
  1. delivery  — the `peer_send` worker action appends to the recipient inbox
     (covered by worker/tests/test_intersession.py).
  2. notify    — the CLI orchestrates the terminal "check mail" nudge, posting
     an `inject_input` per delivered recipient (NOT the worker action). voice-08
     requires the recipient be "notified via the terminal input that this
     session has new mail", so this client-side wiring is part of the M6 spec
     and must not silently regress.

These tests pin layer 2: send the mail, then signal every live recipient pane
with exactly the `check mail` payload — skipping self and degrading gracefully
when a recipient has no live pane.

`bsq` is extensionless, so it's loaded via SourceFileLoader (mirrors
test_bsq_expert.py).
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
FROM = "S-u-sender-p1"


@pytest.fixture(autouse=True)
def _stub_session_resolution(monkeypatch):
    """No tmux in tests — pin slug + SID resolution to fixed values."""
    monkeypatch.setattr(bsq, "resolve_slug", lambda: SLUG)
    monkeypatch.setattr(bsq, "require_sid", lambda v: v or FROM)


def _record_post(delivered, *, raising_panes=()):
    """Return (post_fn, calls). `peer_send` reports `delivered`; an
    `inject_input` to a sid in `raising_panes` raises WorkerError (no live
    pane), mirroring the worker's behaviour for a suspended/non-tmux target."""
    calls = []

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        if action == "peer_send":
            return {"ok": True, "delivered_to": list(delivered)}
        if action == "inject_input":
            if params["sid"] in raising_panes:
                raise bsq.WorkerError("no live pane")
            return {"ok": True, "lines_sent": 1}
        return {"ok": True}

    return fake_post, calls


def _args(**kw):
    base = {"to": "teamlead", "text": "ping", "from_sid": FROM,
            "user": None, "no_signal": False}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# peer send → nudge contract
# ---------------------------------------------------------------------------
def test_peer_send_nudges_every_delivered_recipient(monkeypatch):
    fake_post, calls = _record_post(["S-u-a-p2", "S-u-b-p3"])
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_peer_send(_args(to="dev"))

    assert calls[0][0] == "peer_send"
    injects = [(p["sid"], p["text"]) for a, p in calls if a == "inject_input"]
    assert injects == [("S-u-a-p2", bsq.MAIL_SIGNAL), ("S-u-b-p3", bsq.MAIL_SIGNAL)]
    # The signal payload is exactly what a recipient greps for.
    assert bsq.MAIL_SIGNAL == "check mail"


def test_peer_send_does_not_nudge_self(monkeypatch):
    # A send that fans back to the sender's own inbox must not self-nudge.
    fake_post, calls = _record_post([FROM, "S-u-a-p2"])
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_peer_send(_args(to="all"))

    nudged = [p["sid"] for a, p in calls if a == "inject_input"]
    assert nudged == ["S-u-a-p2"]
    assert FROM not in nudged


def test_peer_send_no_signal_skips_all_nudges(monkeypatch):
    fake_post, calls = _record_post(["S-u-a-p2"])
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_peer_send(_args(no_signal=True))

    assert [a for a, _ in calls] == ["peer_send"]


def test_peer_send_survives_recipient_without_live_pane(monkeypatch):
    # A WorkerError on inject_input (no live pane) must not abort the loop;
    # the bus write already happened, so the peer reads it on next check.
    fake_post, calls = _record_post(
        ["S-u-a-p2", "S-u-b-p3"], raising_panes={"S-u-a-p2"})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_peer_send(_args(to="all"))  # must not raise

    attempted = [p["sid"] for a, p in calls if a == "inject_input"]
    assert attempted == ["S-u-a-p2", "S-u-b-p3"]  # kept going past the failure


# ---------------------------------------------------------------------------
# dispatch envelope → same nudge contract
# ---------------------------------------------------------------------------
def test_dispatch_nudges_delivered_recipients(monkeypatch):
    fake_post, calls = _record_post(["S-u-a-p2"])
    monkeypatch.setattr(bsq, "post", fake_post)

    d = SimpleNamespace(to="S-u-a-p2", from_sid=FROM, action="assign",
                        json=None, field=["task_id=T-1"], note=None,
                        no_signal=False)
    bsq.cmd_dispatch(d)

    # The envelope rides a normal peer_send, then signals the recipient.
    assert calls[0][0] == "peer_send"
    assert bsq.ENVELOPE_MARKER in calls[0][1]["text"]
    injects = [(p["sid"], p["text"]) for a, p in calls if a == "inject_input"]
    assert injects == [("S-u-a-p2", bsq.MAIL_SIGNAL)]
