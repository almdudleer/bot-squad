"""Tests for bsq's expert-resumption (T-0150) + bundle-bind (T-0160) logic.

`bsq` is an extensionless script, so it's loaded via SourceFileLoader. The
expert-scoring functions are pure over an injected `sessions` list + a tmp
backlog dir (pointed at by the module's BOT_SQUAD global), so no worker needed.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
SLUG = "proj"


def _backlog(tmp_path: Path) -> Path:
    d = tmp_path / "data" / SLUG / "backlog"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_ticket(tmp_path: Path, tid: str, *, title: str = "", body: str = "",
                  session_history: list[str] | None = None,
                  initiative: str = "") -> Path:
    d = _backlog(tmp_path)
    sh = "[" + ", ".join(session_history or []) + "]"
    fm = [f"id: {tid}", f"title: {title or tid}", "status: closed",
          f"session_history: {sh}"]
    if initiative:
        fm.append(f"initiative: {initiative}")
    p = d / f"{tid}-{(title or tid).lower().replace(' ', '-')[:30]}.md"
    p.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body + "\n")
    return p


def _session(sid: str, *, status: str = "suspended", uuid: str = "u-1",
             task_id=None, extras=None, initiative="", window="",
             archived=False) -> dict:
    return {"sid": sid, "status": status, "claude_uuid": uuid,
            "task_id": task_id, "extra_task_ids": extras or [],
            "initiative": initiative, "window": window, "archived": archived,
            "suspended_at": "2026-05-30T00:00:00Z"}


@pytest.fixture(autouse=True)
def _point_bsq_at_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(bsq, "BOT_SQUAD", str(tmp_path))
    # Never let the file-touched pass hit the real ~/.claude during tests.
    monkeypatch.setattr(bsq, "_jsonl_for_uuid", lambda uuid: None)


def _find(tmp_path, tickets: list[str], sessions: list[dict], my_sid=None):
    paths, fms = {}, {}
    for t in tickets:
        matches = sorted(_backlog(tmp_path).glob(f"{t}-*.md"))
        paths[t] = matches[0]
        fms[t] = bsq.read_frontmatter(matches[0])
    return bsq._find_expert(SLUG, sessions, tickets, fms, paths, my_sid)


# ---------------------------------------------------------------------------
# _find_expert
# ---------------------------------------------------------------------------
def test_xref_session_history_is_high_confidence(tmp_path):
    # T-100 was shipped by S-A; the new T-200 links to it via [[T-100]].
    _write_ticket(tmp_path, "T-100", title="prior work", session_history=["S-A"])
    _write_ticket(tmp_path, "T-200", title="follow up", body="Builds on it. Related: [[T-100]].")
    ranked = _find(tmp_path, ["T-200"], [_session("S-A")])
    assert len(ranked) == 1
    assert ranked[0]["sid"] == "S-A"
    assert ranked[0]["confidence"] == "high"
    assert "T-100" in ranked[0]["worked"]


def test_no_overlap_yields_no_candidate(tmp_path):
    _write_ticket(tmp_path, "T-100", title="alpha bravo", session_history=["S-A"])
    _write_ticket(tmp_path, "T-200", title="zulu yankee xray", body="unrelated work")
    ranked = _find(tmp_path, ["T-200"], [_session("S-A")])
    assert ranked == []


def test_active_session_is_excluded(tmp_path):
    _write_ticket(tmp_path, "T-100", title="prior", session_history=["S-A"])
    _write_ticket(tmp_path, "T-200", body="Related: [[T-100]].")
    ranked = _find(tmp_path, ["T-200"], [_session("S-A", status="active")])
    assert ranked == []  # only suspended/archived are resumable candidates


def test_archived_session_is_a_candidate(tmp_path):
    _write_ticket(tmp_path, "T-100", title="prior", session_history=["S-A"])
    _write_ticket(tmp_path, "T-200", body="Related: [[T-100]].")
    ranked = _find(tmp_path, ["T-200"],
                   [_session("S-A", status="suspended", archived=True)])
    assert [c["sid"] for c in ranked] == ["S-A"]
    assert ranked[0]["archived"] is True


def test_plain_mention_is_medium_not_high(tmp_path):
    # New ticket mentions T-100 in prose (no [[ ]]) → medium, never auto-resumed.
    _write_ticket(tmp_path, "T-100", title="prior", session_history=["S-A"])
    _write_ticket(tmp_path, "T-200", title="follow",
                  body="Bug is in the path (see T-100 assembly).")
    ranked = _find(tmp_path, ["T-200"], [_session("S-A")])
    assert len(ranked) == 1
    assert ranked[0]["confidence"] == "medium"


def test_self_is_never_a_candidate(tmp_path):
    _write_ticket(tmp_path, "T-100", title="prior", session_history=["S-ME"])
    _write_ticket(tmp_path, "T-200", body="Related: [[T-100]].")
    ranked = _find(tmp_path, ["T-200"], [_session("S-ME")], my_sid="S-ME")
    assert ranked == []


def test_higher_overlap_ranks_first(tmp_path):
    # S-A worked the directly-linked T-100; S-B only shares the initiative.
    _write_ticket(tmp_path, "T-100", title="prior", session_history=["S-A"])
    _write_ticket(tmp_path, "T-200", body="Related: [[T-100]].", initiative="init.md")
    ranked = _find(tmp_path, ["T-200"], [
        _session("S-B", initiative="init.md"),
        _session("S-A"),
    ])
    assert ranked[0]["sid"] == "S-A"
    assert ranked[0]["confidence"] == "high"


# ---------------------------------------------------------------------------
# _related_tickets
# ---------------------------------------------------------------------------
def test_related_tickets_reverse_xref(tmp_path):
    # T-300's body links to the new T-200 → T-300 is a related (high) ticket.
    _write_ticket(tmp_path, "T-300", title="other", body="depends on [[T-200]] heavily")
    _write_ticket(tmp_path, "T-200", title="new")
    rel = bsq._related_tickets(SLUG, {"T-200"}, set(), ["no links here"])
    assert "T-300" in rel
    assert rel["T-300"][2] is True  # high flag


def test_related_tickets_mention_is_medium(tmp_path):
    _write_ticket(tmp_path, "T-200", title="new")
    rel = bsq._related_tickets(SLUG, {"T-200"}, set(), ["touches T-099 and T-150 too"])
    assert rel["T-099"][2] is False and rel["T-150"][2] is False  # mentions = medium


# ---------------------------------------------------------------------------
# _bind_extras (T-0160) — calls bind_task per extra, after the hook settles
# ---------------------------------------------------------------------------
def test_bind_extras_binds_each_extra(monkeypatch):
    calls = []
    state = {"extras": []}

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        if action == "bind_task":
            state["extras"].append(params["task_id"])
            return {"ok": True, "extras": list(state["extras"])}
        if action == "list_sessions":
            return {"sessions": [{"sid": "S-NEW", "task_id": "T-1",
                                  "extra_task_ids": list(state["extras"])}]}
        return {"ok": True}

    monkeypatch.setattr(bsq, "post", fake_post)
    monkeypatch.setattr(bsq.time, "sleep", lambda x: None)
    monkeypatch.setattr(bsq.time, "monotonic", lambda: 0.0)

    bsq._bind_extras("proj", "S-NEW", ["T-2", "T-3"], wait_primary="T-1")

    bind_calls = [p["task_id"] for a, p in calls if a == "bind_task"]
    assert bind_calls[:2] == ["T-2", "T-3"]
    assert all(p["sid"] == "S-NEW" for a, p in calls if a == "bind_task")


def test_bind_extras_noop_without_extras(monkeypatch):
    calls = []
    monkeypatch.setattr(bsq, "post", lambda *a, **k: calls.append(a) or {"ok": True})
    bsq._bind_extras("proj", "S-NEW", [])
    assert calls == []


def test_bind_extras_rebinds_when_hook_races(monkeypatch):
    # First verify read shows the extra missing (hook clobbered) → must re-bind.
    reads = {"n": 0}

    def fake_post(action, params, timeout=35.0, fatal=True):
        if action == "list_sessions":
            reads["n"] += 1
            extras = [] if reads["n"] == 1 else ["T-2"]
            return {"sessions": [{"sid": "S-NEW", "task_id": "T-1",
                                  "extra_task_ids": extras}]}
        return {"ok": True, "extras": ["T-2"]}

    bind_calls = []
    orig = fake_post

    def tracking(action, params, timeout=35.0, fatal=True):
        if action == "bind_task":
            bind_calls.append(params["task_id"])
        return orig(action, params, timeout, fatal)

    monkeypatch.setattr(bsq, "post", tracking)
    monkeypatch.setattr(bsq.time, "sleep", lambda x: None)

    bsq._bind_extras("proj", "S-NEW", ["T-2"])  # no wait_primary
    # Bound once initially, then re-bound after the racing read showed it gone.
    assert bind_calls.count("T-2") >= 2
