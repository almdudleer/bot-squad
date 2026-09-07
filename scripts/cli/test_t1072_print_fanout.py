"""T-1072, the printing half: each reason gets its own sentence, and only the
genuinely-nobody case keeps the relay advice.

The worker now sends `will_notify_why` beside `will_notify` (see
worker/tests/test_t1072_fanout_preview.py for the reasons themselves). This
file pins what the human actually reads, because the defect was never in the
recipient list — it was in the sentence printed about it.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_t1072", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_t1072", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _line(capsys, **res) -> str:
    bsq._print_fanout({"will_notify": [], **res}, "T-1082")
    return capsys.readouterr().out


def test_the_specimen_no_longer_claims_nobody_is_bound(capsys):
    """THE 14:12:26Z SENTENCE. It said "nobody is bound to T-1082 and no live
    operator" while that ticket's session_history named the writer and a live
    operator was on the board. It must now say neither of those things."""
    out = _line(capsys, will_notify_why="author_only")

    assert "you are the only session bound to T-1082" in out
    assert "nobody else to tell" in out
    assert "nobody is bound" not in out
    assert "no live operator" not in out
    assert "do NOT relay" in out          # relaying is the wrong answer here


def test_the_genuinely_empty_board_keeps_the_relay_advice(capsys):
    """The positive control: the original sentence was RIGHT for this state and
    must survive. A fix that silences it everywhere loses the one case the line
    exists for."""
    out = _line(capsys, will_notify_why="nobody")

    assert "nobody is bound to T-1082" in out
    assert "relay it only if it fits no ticket" in out


def test_a_disabled_watch_blames_the_switch_not_the_board(capsys):
    out = _line(capsys, will_notify_why="watch_disabled")

    assert "ticket-watch is OFF" in out
    assert "nobody is bound" not in out


def test_a_failed_lookup_refuses_to_say_nobody(capsys):
    """An error is not a negative fact."""
    out = _line(capsys, will_notify_why="unknown")

    assert "NOT 'nobody'" in out
    assert "nobody is bound" not in out


def test_recipients_are_still_named_when_there_are_any(capsys):
    bsq._print_fanout({"will_notify": ["S-a", "S-b"], "will_notify_why": "bound"},
                      "T-1082")

    assert "fan-out will nudge: S-a, S-b" in capsys.readouterr().out


def test_an_older_worker_that_sends_no_reason_admits_it(capsys):
    """A pre-T-1072 worker's empty list is ambiguous BY CONSTRUCTION. Printing
    the old sentence would re-assert the false claim this ticket is about; the
    honest line is that the reason is not recoverable here."""
    out = _line(capsys)

    assert "did not say why" in out
    assert "nobody is bound" not in out


def test_a_response_without_will_notify_prints_nothing(capsys):
    """Actions that do not preview the fan-out must not grow a line."""
    bsq._print_fanout({"ok": True}, "T-1082")

    assert capsys.readouterr().out == ""
