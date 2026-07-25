"""Tests for `bsq session alias {set,remove,resolve,list}` (T-0662).

`bsq` is an extensionless script loaded via SourceFileLoader. The commands are
thin wrappers over `post()` to the worker's session_alias_* actions (tested
directly in worker/tests/test_actions.py) — these tests only check the CLI's
own formatting/plumbing, so `post` is monkeypatched to a fake worker.
"""
from __future__ import annotations

import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_session_alias", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_session_alias", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


class _FakeWorker:
    def __init__(self):
        self.calls = []
        self.aliases = {}

    def post(self, action, params, **kw):
        self.calls.append((action, params))
        if action == "session_alias_set":
            prev = self.aliases.get(params["label"])
            self.aliases[params["label"]] = params["sid"]
            return {"ok": True, "label": params["label"], "sid": params["sid"],
                     "previous_sid": prev}
        if action == "session_alias_remove":
            existed = params["label"] in self.aliases
            self.aliases.pop(params["label"], None)
            return {"ok": True, "removed": existed}
        if action == "session_alias_resolve":
            return {"ok": True, "sid": self.aliases.get(params["label"])}
        if action == "session_alias_list":
            return {"ok": True, "aliases": dict(self.aliases)}
        raise AssertionError(f"unexpected action {action!r}")


def _install(monkeypatch, fake):
    monkeypatch.setattr(bsq, "post", fake.post)


def test_alias_set_prints_mapping(monkeypatch, capsys):
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    bsq.cmd_session_alias_set(argparse.Namespace(label="gateway-tl", sid="S-x-p1"))
    out = capsys.readouterr().out
    assert "gateway-tl -> S-x-p1" in out
    assert fake.calls == [("session_alias_set", {"label": "gateway-tl", "sid": "S-x-p1"})]


def test_alias_set_repoint_shows_previous(monkeypatch, capsys):
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    bsq.cmd_session_alias_set(argparse.Namespace(label="alpha", sid="S-x-p1"))
    capsys.readouterr()
    bsq.cmd_session_alias_set(argparse.Namespace(label="alpha", sid="S-y-p2"))
    out = capsys.readouterr().out
    assert "alpha -> S-y-p2" in out
    assert "S-x-p1" in out


def test_alias_resolve_prints_sid(monkeypatch, capsys):
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    bsq.cmd_session_alias_set(argparse.Namespace(label="alpha", sid="S-x-p1"))
    capsys.readouterr()
    bsq.cmd_session_alias_resolve(argparse.Namespace(label="alpha"))
    out = capsys.readouterr().out.strip()
    assert out == "S-x-p1"


def test_alias_resolve_unknown_label_dies(monkeypatch):
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    with pytest.raises(SystemExit):
        bsq.cmd_session_alias_resolve(argparse.Namespace(label="nope"))


def test_alias_remove_reports_result(monkeypatch, capsys):
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    bsq.cmd_session_alias_set(argparse.Namespace(label="alpha", sid="S-x-p1"))
    capsys.readouterr()

    bsq.cmd_session_alias_remove(argparse.Namespace(label="alpha"))
    assert "removed alpha" in capsys.readouterr().out

    bsq.cmd_session_alias_remove(argparse.Namespace(label="alpha"))
    assert "no such label" in capsys.readouterr().out


def test_alias_list_json(monkeypatch, capsys):
    import json
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    bsq.cmd_session_alias_set(argparse.Namespace(label="alpha", sid="S-x-p1"))
    bsq.cmd_session_alias_set(argparse.Namespace(label="beta", sid="S-y-p2"))
    capsys.readouterr()

    bsq.cmd_session_alias_list(argparse.Namespace(json=True))
    out = capsys.readouterr().out
    assert json.loads(out) == {"alpha": "S-x-p1", "beta": "S-y-p2"}


def test_alias_list_empty(monkeypatch, capsys):
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    bsq.cmd_session_alias_list(argparse.Namespace(json=False))
    assert "no session labels set" in capsys.readouterr().out


def test_alias_list_text_format(monkeypatch, capsys):
    fake = _FakeWorker()
    _install(monkeypatch, fake)
    bsq.cmd_session_alias_set(argparse.Namespace(label="alpha", sid="S-x-p1"))
    capsys.readouterr()
    bsq.cmd_session_alias_list(argparse.Namespace(json=False))
    out = capsys.readouterr().out
    assert "alpha  ->  S-x-p1" in out


def test_argparse_wires_session_alias_subcommands():
    """The `session alias set/remove/resolve/list` subparsers parse and route
    to the right cmd_* function (argparse wiring, not the fake-worker
    plumbing covered above)."""
    parser = bsq.build_parser()

    args = parser.parse_args(["session", "alias", "set", "gateway-tl", "S-x-p1"])
    assert args.func is bsq.cmd_session_alias_set
    assert args.label == "gateway-tl"
    assert args.sid == "S-x-p1"

    args = parser.parse_args(["session", "alias", "remove", "gateway-tl"])
    assert args.func is bsq.cmd_session_alias_remove
    assert args.label == "gateway-tl"

    args = parser.parse_args(["session", "alias", "resolve", "gateway-tl"])
    assert args.func is bsq.cmd_session_alias_resolve
    assert args.label == "gateway-tl"

    args = parser.parse_args(["session", "alias", "list", "--json"])
    assert args.func is bsq.cmd_session_alias_list
    assert args.json is True
