"""T-0783a: the `bsq pickup` verb — the human-readable face of the pickup queue.

Pins that each flag sends EXACTLY the right params to ``pickup_queue`` (the
action enforces a strict allowed-param set), that the default view shows only the
takeable band, and that ``--brief`` prints the SAME block the operator re-drive
injects rather than a second rendering that can drift from it.

`post` is stubbed — per the house rule that `bsq` is not a dry-run surface, every
verb hits the live worker socket, so the parse/param contract is tested here and
the live round-trip is a separate manual walkthrough.

`bsq` is extensionless, so it's loaded via SourceFileLoader (mirrors
test_bsq_operator.py).
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
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: SLUG)


def _record_post(returns):
    calls = []

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        return returns.get(action, {"ok": True})

    return fake_post, calls


def _row(tid, **over):
    row = {"id": tid, "title": f"work for {tid}", "status": "reopened",
           "effective_priority": 1, "idle_days": 0.4, "reject": None, "sanity": []}
    row.update(over)
    return row


def _args(**over):
    base = {"triage": False, "all": False, "brief": False}
    base.update(over)
    return SimpleNamespace(**base)


# --- param contract ---------------------------------------------------------

def test_default_asks_for_the_pickup_band_only(monkeypatch, capsys):
    """The excluded band is ~730 rows of closed tickets on a mature board, so the
    default view must not drag it across the socket."""
    fake_post, calls = _record_post({"pickup_queue": {
        "ok": True, "pickup": [_row("T-0719")],
        "counts": {"pickup": 1, "triage": 0, "excluded": 730, "board": 731}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    assert calls == [("pickup_queue", {"slug": SLUG, "band": "pickup"})]


def test_triage_flag_asks_for_the_triage_band(monkeypatch, capsys):
    fake_post, calls = _record_post({"pickup_queue": {
        "ok": True, "triage": [_row("T-0612", sanity=["stale:25d>=14d"])],
        "counts": {"pickup": 0, "triage": 1, "excluded": 0, "board": 1}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args(triage=True))

    assert calls == [("pickup_queue", {"slug": SLUG, "band": "triage"})]


def test_all_flag_omits_the_band_param_entirely(monkeypatch, capsys):
    """Strict allowed-param set: no ``band`` key when every band is wanted."""
    fake_post, calls = _record_post({"pickup_queue": {
        "ok": True, "pickup": [], "triage": [], "excluded": [],
        "counts": {"pickup": 0, "triage": 0, "excluded": 0, "board": 0}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args(all=True))

    assert calls == [("pickup_queue", {"slug": SLUG})]


# --- output -----------------------------------------------------------------

def test_prints_the_takeable_ids_with_status_and_urgency(monkeypatch, capsys):
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [_row("T-0719", title="REGRESSION reply-by-sid routing")],
        "counts": {"pickup": 1, "triage": 4, "excluded": 730, "board": 735}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    out = capsys.readouterr().out
    assert "T-0719" in out
    assert "reopened" in out
    assert "P1" in out
    assert "1 takeable" in out and "4 need triage" in out


def test_prints_why_a_triage_row_is_suspect(monkeypatch, capsys):
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True,
        "triage": [_row("T-0612", status="in_progress", effective_priority=1,
                        idle_days=24.6,
                        sanity=["foreign-project:watchrobot", "stale:24d>=14d"])],
        "counts": {"pickup": 0, "triage": 1, "excluded": 0, "board": 1}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args(triage=True))

    out = capsys.readouterr().out
    assert "T-0612" in out
    assert "foreign-project:watchrobot" in out
    assert "stale:24d>=14d" in out


def test_prints_why_an_excluded_row_was_excluded(monkeypatch, capsys):
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [], "triage": [],
        "excluded": [_row("T-1", status="open", reject="held-by:S-dev-p1")],
        "counts": {"pickup": 0, "triage": 0, "excluded": 1, "board": 1}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args(all=True))

    assert "held-by:S-dev-p1" in capsys.readouterr().out


def test_an_unknown_idle_age_prints_as_unknown_not_zero(monkeypatch, capsys):
    """An absent timestamp must not render as "0d" — that reads as touched today,
    which is the silent-None shape the queue exists to avoid."""
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [_row("T-1", idle_days=None)],
        "counts": {"pickup": 1, "triage": 0, "excluded": 0, "board": 1}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    assert "idle?" in capsys.readouterr().out


def test_brief_prints_the_servers_rendering_verbatim(monkeypatch, capsys):
    """One rendering, two consumers — the CLI must not re-format the brief, or
    what a human reads and what the operator is told will drift."""
    brief = "PICKUP QUEUE (1 takeable, most urgent first) — dispatch from HERE"
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [_row("T-0719")], "brief": brief,
        "counts": {"pickup": 1, "triage": 0, "excluded": 0, "board": 1}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args(brief=True))

    assert capsys.readouterr().out.strip() == brief


def test_an_empty_queue_still_prints_the_counts_line(monkeypatch, capsys):
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [],
        "counts": {"pickup": 0, "triage": 0, "excluded": 12, "board": 12}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    out = capsys.readouterr().out
    assert "0 takeable" in out
    assert "TAKEABLE (most urgent first): 0" in out


# --- parser wiring ----------------------------------------------------------

def test_the_verb_is_wired_into_the_parser():
    args = bsq.build_parser().parse_args(["pickup"])
    assert args.func is bsq.cmd_pickup
    assert args.triage is False and args.all is False and args.brief is False


@pytest.mark.parametrize("flag,attr", [
    ("--triage", "triage"), ("--all", "all"), ("--brief", "brief"),
])
def test_each_flag_parses(flag, attr):
    args = bsq.build_parser().parse_args(["pickup", flag])
    assert getattr(args, attr) is True


def test_the_verb_is_listed_in_the_usage_header():
    """The header is the verb reference a session actually reads; a verb missing
    from it is a verb nobody finds."""
    assert "bsq pickup" in bsq.__doc__
