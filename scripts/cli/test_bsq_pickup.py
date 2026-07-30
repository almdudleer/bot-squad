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


# --- T-0829: the DEFAULT table names the active drive scope ------------------
#
# `--brief` got this for free (the server renders it). The default table is the
# one a human types, and a scope that narrows it silently is «нужно более чёткое
# понимание для меня, какой режим драйва щас стоит» reproduced on his own
# surface. Both directions are pinned: it must appear when a scope IS set, and
# must NOT be manufactured when none is.

def _scope(**over):
    ds = {"scope": "open_reopened", "statuses": ["open", "reopened"],
          "source": "config", "configured": True, "problem": None,
          "set_by": "stakeholder", "set_at": "2026-07-30T14:52:00Z",
          "source_text": "закончить всё что в опен",
          "triage_in_scope": 4, "out_of_scope": 55}
    ds.update(over)
    return ds


def test_the_table_names_the_scope_that_narrowed_it(monkeypatch, capsys):
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [_row("T-0719")], "drive_scope": _scope(),
        "counts": {"pickup": 1, "triage": 4, "excluded": 782, "board": 787}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    out = capsys.readouterr().out
    assert "drive scope: open_reopened (open, reopened)" in out
    assert "закончить всё что в опен" in out
    assert "55 ticket(s) outside this scope are not listed" in out
    assert "4 in scope need triage" in out
    assert "NOT mean the scope is done" in out


def test_an_unset_scope_prints_no_scope_line_at_all(monkeypatch, capsys):
    """`configured` tells "never set" from "deliberately widest". A default view
    that announces a mode he never chose invents a setting instead of reporting
    one — and this is also what keeps the table byte-identical to today until he
    sets something."""
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [_row("T-0719")],
        "drive_scope": _scope(scope="all", statuses=["in_progress", "open", "planned",
                                                     "reopened"],
                              configured=False, set_by=None, set_at=None,
                              source_text=None, triage_in_scope=8, out_of_scope=0),
        "counts": {"pickup": 1, "triage": 8, "excluded": 730, "board": 739}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    out = capsys.readouterr().out
    assert "drive scope" not in out
    assert "T-0719" in out and "1 takeable" in out


def test_a_rejected_scope_value_is_shown_raw_not_as_the_fallback(monkeypatch, capsys):
    """Never report the fallback as if it were the setting — he has to see the
    typo he made."""
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [],
        "drive_scope": _scope(scope="all", statuses=["in_progress", "open", "planned",
                                                     "reopened"],
                              problem="invalid-scope:'opne'", source_text=None,
                              triage_in_scope=0, out_of_scope=0),
        "counts": {"pickup": 0, "triage": 0, "excluded": 1, "board": 1}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    out = capsys.readouterr().out
    assert "DID NOT take effect" in out
    assert "opne" in out


def test_a_response_without_a_scope_block_still_renders(monkeypatch, capsys):
    """An older worker, or any caller that does not send the block: the table
    must not blow up in a session's face."""
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [_row("T-1")],
        "counts": {"pickup": 1, "triage": 0, "excluded": 0, "board": 1}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args())

    assert "T-1" in capsys.readouterr().out


def test_brief_does_not_get_the_table_header_too(monkeypatch, capsys):
    """One rendering per surface. `--brief` prints the server's block verbatim,
    so the CLI header must not be prepended to it as a second, drifting copy."""
    brief = "DRIVE SCOPE: open_reopened (statuses in play: open, reopened) — set."
    fake_post, _ = _record_post({"pickup_queue": {
        "ok": True, "pickup": [], "brief": brief, "drive_scope": _scope(),
        "counts": {"pickup": 0, "triage": 4, "excluded": 782, "board": 787}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_pickup(_args(brief=True))

    assert capsys.readouterr().out.strip() == brief


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
