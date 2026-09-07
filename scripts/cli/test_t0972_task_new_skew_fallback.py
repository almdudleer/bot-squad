"""T-0972: `bsq task new` degrades on the worker's EVIDENCE, not on a guess.

## What this pins, and what it cost

`scripts/cli/bsq` is instant-live for the whole fleet (``~/.local/bin/bsq``
symlinks into the shared working tree) while the worker only picks a matching
change up at the next deploy. `cmd_task_new` therefore sends its params
optimistically and degrades if the worker does not know one of them.

The degrade path used to retry with a FIXED LEAN DICT — the pre-T-0519 param
shape, from before the worker had a provenance gate at all. Every worker has
had that gate for months, so that retry could no longer succeed under any
circumstances: it could only convert *"I do not know param X"* into
*"you did not give me provenance"*.

Measured 2026-09-07: T-1052 added `filed_by`; the install worker rejected it;
`bsq task new` was down fleet-wide behind a refusal naming **provenance**, a
param the first call had no problem with. Two wrong hypotheses and two worker
restarts were spent chasing the named param. `test_the_old_lean_retry_is_what_broke`
below is the control that reproduces exactly that, so the modelled worker here
is known to be capable of the outage rather than merely of passing.

The fix: drop ONLY the params the worker NAMED as unexpected.
"""
from __future__ import annotations

import ast
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_CLI_DIR = Path(__file__).resolve().parent
_BSQ_PATH = _CLI_DIR / "bsq"
_ACTIONS_PATH = _CLI_DIR.parents[1] / "worker" / "bot_squad_worker" / "actions.py"

_loader = SourceFileLoader("bsq_mod_t0972", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_t0972", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


# --------------------------------------------------------------------------
# The refusal string is composed from ITS PRODUCER, not hand-typed here.
# A hand-typed literal would keep passing after the worker reworded the
# message, which is the one change that would silently break the parser this
# file exists to pin.
# --------------------------------------------------------------------------
def _worker_unexpected_msg(names: list[str]) -> str:
    src = _ACTIONS_PATH.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.JoinedStr):
            expr = ast.unparse(node)
            if "task_new got unexpected params" in expr:
                return eval(expr, {"sorted": sorted}, {"extra": set(names)})
    raise AssertionError(
        f"no 'task_new got unexpected params' f-string found in {_ACTIONS_PATH} "
        "— if the worker stopped naming the params it rejected, the CLI can no "
        "longer degrade by evidence and this whole mechanism is dead")


def test_the_worker_still_names_the_params_it_rejects():
    """The load-bearing precondition: degrading by evidence needs evidence."""
    msg = _worker_unexpected_msg(["filed_by"])
    assert bsq._unexpected_params(msg) == {"filed_by"}
    assert bsq._unexpected_params(_worker_unexpected_msg(["a", "b"])) == {"a", "b"}


def test_unexpected_params_returns_empty_on_an_unparseable_message():
    assert bsq._unexpected_params("worker rejected task_new: boom") == set()


# --------------------------------------------------------------------------
# A modelled worker: an allowlist gate + the provenance gate, in that order —
# the same order `_action_task_new` applies them.
# --------------------------------------------------------------------------
class _FakeWorker:
    def __init__(self, tmp_path, allowed=None):
        # allowed=None models a worker that accepts every param sent.
        self.allowed = None if allowed is None else set(allowed)
        self.calls: list[dict] = []
        self.tmp_path = tmp_path
        self.ticket = tmp_path / "T-9999-probe.md"
        self.ticket.write_text(
            "---\nid: T-9999\ntitle: probe\n---\n\n## Verbatim request\n\n(stub)\n",
            encoding="utf-8")

    def __call__(self, action, params, **kw):
        self.calls.append(dict(params))
        extra = set() if self.allowed is None else set(params) - self.allowed
        if extra:
            raise bsq.WorkerError(
                f"worker rejected {action}: {_worker_unexpected_msg(sorted(extra))}")
        # The gate exists only on a worker that KNOWS the param. A genuine
        # pre-T-0519 worker rejects `provenance` as unexpected and has no gate
        # to fail — modelling both at once would be a worker that cannot exist.
        gated = self.allowed is None or "provenance" in self.allowed
        if gated and not str(params.get("provenance") or "").strip():
            raise bsq.WorkerError(
                f"worker rejected {action}: task_new requires provenance "
                "(cite the source) — allowed: corpus:<token> | F-NNNN | "
                "T-NNNN | stakeholder:YYYY-MM-DD")
        return {"ok": True, "id": "T-9999", "file_path": str(self.ticket)}


def _run(monkeypatch, worker, argv, sid="S-almdudleer-dev_probe-p734"):
    monkeypatch.setattr(bsq, "post", worker)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    monkeypatch.setattr(bsq, "my_sid", lambda *a, **k: sid)
    parser = bsq.build_parser()
    args = parser.parse_args(argv)
    args.func(args)


_ARGV = ["task", "new", "Something broke", "--provenance", "T-0972"]


# --------------------------------------------------------------------------
# 1. Healthy case FIRST. A worker that accepts everything must see the
#    optimistic call, once, untouched.
# --------------------------------------------------------------------------
def test_healthy_worker_takes_the_optimistic_call_unchanged(tmp_path, monkeypatch, capsys):
    w = _FakeWorker(tmp_path)  # accepts everything
    _run(monkeypatch, w, _ARGV)

    assert len(w.calls) == 1, "a worker that accepts everything must not be retried"
    assert w.calls[0]["provenance"] == "T-0972"
    assert w.calls[0]["filed_by"] == "S-almdudleer-dev_probe-p734"
    assert "does not accept" not in capsys.readouterr().err
    # Nothing was dropped, so nothing is stamped post-hoc: the ticket the
    # worker wrote is left exactly as the worker wrote it.
    assert "filed_by:" not in w.ticket.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 2. The measured outage: the worker knows provenance but not filed_by.
# --------------------------------------------------------------------------
def test_unknown_filed_by_is_dropped_by_name_and_provenance_survives(
        tmp_path, monkeypatch, capsys):
    w = _FakeWorker(tmp_path, {"slug", "title", "provenance", "priority",
                               "owner", "initiative", "verbatim", "force"})
    _run(monkeypatch, w, _ARGV)

    assert len(w.calls) == 2
    first, retry = w.calls
    assert first["filed_by"] == "S-almdudleer-dev_probe-p734"
    assert "filed_by" not in retry, "the named param is what gets dropped"
    assert retry["provenance"] == "T-0972", (
        "provenance was NEVER the problem — dropping it is the defect this "
        "test exists for")
    assert retry["title"] == "Something broke"

    err = capsys.readouterr().err
    assert "filed_by" in err and "does not accept" in err, (
        "the skew must be named on stderr, or the next reader chases the "
        "wrong param again")
    # what the worker could not take is stamped onto the ticket instead
    assert "filed_by: S-almdudleer-dev_probe-p734" in w.ticket.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 3. The operator's ask: a DIFFERENT unknown param bites the same way.
#    Nothing here is special-cased to filed_by.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("unknown", ["verbatim", "priority", "force"])
def test_a_different_unknown_param_is_dropped_and_the_rest_survives(
        tmp_path, monkeypatch, capsys, unknown):
    allowed = {"slug", "title", "provenance", "filed_by", "priority", "owner",
               "initiative", "verbatim", "force"} - {unknown}
    w = _FakeWorker(tmp_path, allowed)
    _run(monkeypatch, w, _ARGV + ["--verbatim", "he said it broke",
                                  "--priority", "p1", "--force"])

    assert len(w.calls) == 2
    retry = w.calls[1]
    assert unknown not in retry
    assert retry["provenance"] == "T-0972", "provenance survives every skew"
    assert retry["filed_by"] == "S-almdudleer-dev_probe-p734"
    for keep in {"verbatim", "priority", "force"} - {unknown}:
        assert keep in retry, f"{keep} was not rejected and must not be dropped"
    assert unknown in capsys.readouterr().err


# --------------------------------------------------------------------------
# 4. The case the old shim was actually written for still works: a genuine
#    pre-T-0519 worker that does not know `provenance` gets the post-hoc stamp.
# --------------------------------------------------------------------------
def test_a_genuine_pre_gate_worker_still_gets_the_post_hoc_stamp(
        tmp_path, monkeypatch, capsys):
    w = _FakeWorker(tmp_path, {"slug", "title", "priority", "owner", "initiative"})
    _run(monkeypatch, w, _ARGV)

    assert len(w.calls) == 2
    assert "provenance" not in w.calls[1] and "filed_by" not in w.calls[1]
    body = w.ticket.read_text(encoding="utf-8")
    assert "provenance: T-0972" in body
    assert "filed_by: S-almdudleer-dev_probe-p734" in body


# --------------------------------------------------------------------------
# 5. THE CONTROL. Reproduce the old behaviour against this same modelled
#    worker and watch it produce the outage. Without this, everything above
#    could be passing against a worker incapable of the failure.
# --------------------------------------------------------------------------
def test_the_old_lean_retry_is_what_broke(tmp_path):
    w = _FakeWorker(tmp_path, {"slug", "title", "provenance", "priority",
                               "owner", "initiative", "verbatim", "force"})
    optimistic = {"slug": "demo", "title": "Something broke",
                  "provenance": "T-0972", "filed_by": "S-x"}
    with pytest.raises(bsq.WorkerError) as first:
        w("task_new", optimistic)
    assert "filed_by" in str(first.value)

    # what the pre-T-0972 code did next: retry with the lean dict, which
    # omitted provenance as well as the param the worker actually named.
    lean = {"slug": "demo", "title": "Something broke"}
    with pytest.raises(bsq.WorkerError) as second:
        w("task_new", lean)
    assert "requires provenance" in str(second.value), (
        "the modelled worker must be able to produce the observed refusal")
    assert "filed_by" not in str(second.value), (
        "and the refusal names the WRONG param — which is why it cost "
        "two wrong hypotheses to diagnose")


# --------------------------------------------------------------------------
# 6. Refuse to retry blind. A rejection naming nothing this call sent is not
#    a skew we understand, and guessing is what got us here.
# --------------------------------------------------------------------------
def test_refuses_to_retry_when_the_worker_names_nothing_this_call_sent(
        tmp_path, monkeypatch, capsys):
    class _Weird(_FakeWorker):
        def __call__(self, action, params, **kw):
            self.calls.append(dict(params))
            raise bsq.WorkerError(
                f"worker rejected {action}: "
                f"{_worker_unexpected_msg(['some_param_we_never_sent'])}")

    w = _Weird(tmp_path, set())
    with pytest.raises(SystemExit):
        _run(monkeypatch, w, _ARGV)
    assert len(w.calls) == 1, "no blind retry"
    assert "not retrying blind" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 7. A non-skew failure is still fatal on the FIRST call — the degrade path
#    must not swallow a real refusal.
# --------------------------------------------------------------------------
def test_a_non_skew_refusal_is_not_retried(tmp_path, monkeypatch, capsys):
    class _Dupe(_FakeWorker):
        def __call__(self, action, params, **kw):
            self.calls.append(dict(params))
            raise bsq.WorkerError(
                "worker rejected task_new: near-duplicate of T-0100 "
                "(retry with force: true)")

    w = _Dupe(tmp_path, set())
    with pytest.raises(SystemExit):
        _run(monkeypatch, w, _ARGV)
    assert len(w.calls) == 1
    assert "near-duplicate" in capsys.readouterr().err
