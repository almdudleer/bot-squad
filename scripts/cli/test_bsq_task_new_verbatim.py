"""``bsq task new --verbatim`` wiring (T-0581 finding 4, optional).

T-0577 gave the worker's ``task_new`` action a ``verbatim`` param (widens the
dedupe-gate query without being stored on the ticket body) but the CLI never
exposed a way to pass it — only ``--force`` existed. This covers the thin
parser + payload wiring; the dedupe-widening BEHAVIOUR is unit-tested against
the worker action in ``worker/tests/test_task_dedupe_gate.py``
(``test_verbatim_widens_the_dedupe_query``).
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_verbatim", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_verbatim", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _run(monkeypatch, argv):
    calls = []

    def _fake_post(action, params, **kw):
        calls.append((action, params))
        return {"ok": True, "id": "T-9999", "file_path": "/tmp/T-9999-x.md"}

    monkeypatch.setattr(bsq, "post", _fake_post)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return calls


def test_task_new_verbatim_flows_into_the_action_payload(monkeypatch, capsys):
    calls = _run(monkeypatch, [
        "task", "new", "Crashes", "--provenance", "T-0577",
        "--verbatim", "Login page crashes on Safari",
    ])
    assert len(calls) == 1
    action, params = calls[0]
    assert action == "task_new"
    assert params["verbatim"] == "Login page crashes on Safari"


def test_task_new_without_verbatim_omits_the_param(monkeypatch, capsys):
    calls = _run(monkeypatch, ["task", "new", "Crashes", "--provenance", "T-0577"])
    assert len(calls) == 1
    _, params = calls[0]
    assert "verbatim" not in params
