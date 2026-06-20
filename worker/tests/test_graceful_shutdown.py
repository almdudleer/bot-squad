"""T-0284 (WS-4): graceful worker shutdown within systemd's TimeoutStopSec.

Bug: ``uvicorn.run()`` installs its OWN SIGTERM/SIGINT handlers, overriding the
worker's ``_shutdown`` — so the T-0119 ``shutdown_event`` was never set, the
``peer_inbox_wait`` long-polls ran their full timeout, uvicorn's graceful
shutdown blocked on them, and systemd SIGKILLed the worker at TimeoutStopSec=10s
(status=9/KILL), risking in-flight action corruption + hard-killing every TL's
wake channel.

Fix: wrap the uvicorn ``Server.handle_exit`` so the signal ALSO flips our
shutdown event (long-polls return within ~1s) + stops the scheduler, BEFORE
uvicorn starts draining — so the worker exits cleanly inside the window.
"""
from __future__ import annotations

import signal
import threading

from bot_squad_worker.__main__ import _install_graceful_shutdown


class _FakeServer:
    def __init__(self):
        self.exited_with = None

    def handle_exit(self, sig, frame):  # uvicorn's original
        self.exited_with = (sig, frame)


class _FakeSched:
    def __init__(self):
        self.shutdown_calls = []

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)


def test_wrapper_sets_event_and_stops_sched_then_calls_original():
    server = _FakeServer()
    ev = threading.Event()
    sched = _FakeSched()

    _install_graceful_shutdown(server, ev, sched)
    server.handle_exit(signal.SIGTERM, None)

    assert ev.is_set() is True                 # long-polls will drain within ~1s
    assert sched.shutdown_calls == [False]     # sched.shutdown(wait=False)
    assert server.exited_with == (signal.SIGTERM, None)  # uvicorn drain still runs


def test_wrapper_tolerates_no_scheduler():
    server = _FakeServer()
    ev = threading.Event()

    _install_graceful_shutdown(server, ev, None)  # user-worker mode: no sched
    server.handle_exit(signal.SIGINT, None)

    assert ev.is_set() is True
    assert server.exited_with == (signal.SIGINT, None)


def test_wrapper_sets_event_even_if_original_raises():
    """Our shutdown signalling must not be lost if uvicorn's handler throws."""
    server = _FakeServer()
    ev = threading.Event()

    def boom(sig, frame):
        raise RuntimeError("uvicorn boom")
    server.handle_exit = boom

    _install_graceful_shutdown(server, ev, None)
    try:
        server.handle_exit(signal.SIGTERM, None)
    except RuntimeError:
        pass
    assert ev.is_set() is True  # event flipped before the original ran
