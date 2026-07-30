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


def _record_post(delivered, *, raising_panes=(), redirected=None):
    """Return (post_fn, calls). `peer_send` reports `delivered`; an
    `inject_input` to a sid in `raising_panes` raises WorkerError (no live
    pane), mirroring the worker's behaviour for a suspended/non-tmux target.

    T-0790: `redirected` is the worker's recycled-target report, echoed back so
    the CLI's handling of it can be pinned."""
    calls = []

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        if action == "peer_send":
            out = {"ok": True, "delivered_to": list(delivered)}
            if redirected is not None:
                out["redirected"] = redirected
            return out
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


# ---------------------------------------------------------------------------
# T-0790: a RECYCLED target. The worker routes past the dead SID; the CLI must
# nudge the SUCCESSOR and tell the sender its SID is stale.
#
# This is also the mechanism by which the 200/400 pairing stops appearing for
# this bug: the nudge now has a live pane to reach. The `inject_input` 400
# itself is untouched and still fires for a genuinely paneless target — pinned
# by test_peer_send_survives_recipient_without_live_pane above.
# ---------------------------------------------------------------------------

_DEAD = "S-almdudleer-operator-p374"
_LIVE = "S-almdudleer-operator-p455"
_REDIRECT = {"from": _DEAD, "to": _LIVE, "reason": "recycled"}


def test_peer_send_nudges_the_successor_not_the_recycled_sid(monkeypatch):
    """GREEN GUARD (passes at 423a058) pinning the MECHANISM: the nudge follows
    `delivered_to`, so redirecting delivery is what gives the nudge a live pane
    to reach. If a future change nudged the requested `to` instead, the 400 would
    come back and the message would be silently unread again."""
    fake_post, calls = _record_post([_LIVE], redirected=_REDIRECT)
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_peer_send(_args(to=_DEAD, text="verbatim relay"))

    nudged = [p["sid"] for a, p in calls if a == "inject_input"]
    assert nudged == [_LIVE]
    assert _DEAD not in nudged


def test_peer_send_prints_the_recycle_redirect(monkeypatch, capsys):
    """The sender has to learn the SID it is carrying is stale — a silent
    redirect is the same class of problem as the silent loss."""
    fake_post, _ = _record_post([_LIVE], redirected=_REDIRECT)
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_peer_send(_args(to=_DEAD, text="verbatim relay"))

    warning = [ln for ln in capsys.readouterr().out.splitlines() if "RECYCLED" in ln]
    assert len(warning) == 1
    assert _DEAD in warning[0]
    assert _LIVE in warning[0]


def test_dispatch_prints_the_recycle_redirect(monkeypatch, capsys):
    """`bsq peer dispatch` rides the same peer_send, so it reports the same way."""
    fake_post, _ = _record_post([_LIVE], redirected=_REDIRECT)
    monkeypatch.setattr(bsq, "post", fake_post)

    d = SimpleNamespace(to=_DEAD, from_sid=FROM, action="assign",
                        json=None, field=["task_id=T-1"], note=None,
                        no_signal=False)
    bsq.cmd_dispatch(d)

    warning = [ln for ln in capsys.readouterr().out.splitlines() if "RECYCLED" in ln]
    assert len(warning) == 1
    assert _LIVE in warning[0]


def test_peer_send_to_a_live_sid_prints_no_redirect_warning(monkeypatch, capsys):
    """GREEN GUARD — no `redirected` in the reply means nothing extra printed."""
    fake_post, _ = _record_post(["S-u-a-p2"])
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_peer_send(_args(to="S-u-a-p2"))

    out = capsys.readouterr().out
    assert "RECYCLED" not in out
    assert "sent to 1 inbox(es)" in out
