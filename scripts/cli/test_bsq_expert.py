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
                  initiative: str = "", filed_by: str = "") -> Path:
    d = _backlog(tmp_path)
    sh = "[" + ", ".join(session_history or []) + "]"
    fm = [f"id: {tid}", f"title: {title or tid}", "status: closed",
          f"session_history: {sh}"]
    if initiative:
        fm.append(f"initiative: {initiative}")
    if filed_by:
        fm.append(f"filed_by: {filed_by}")
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
# T-1052 — `filed_by`: the filer is a distinct, separate signal from
# `session_history` ("worked it"), not an overload of it.
# ---------------------------------------------------------------------------
def test_filer_is_refused_without_filed_by_stamp(tmp_path):
    # Live specimen, "before": a session filed this ticket (even measured it)
    # but the ticket carries no session_history and no filed_by stamp — the
    # pre-T-1052 task_new shape. Expert discovery has no signal to go on.
    _write_ticket(tmp_path, "T-1049", title="load-bearing trap")
    ranked = _find(tmp_path, ["T-1049"], [_session("S-p714")])
    assert ranked == []


def test_filer_is_offered_via_filed_by_stamp(tmp_path):
    # Live specimen, "after": task_new stamps `filed_by` on the ticket it
    # mints. The filing session, though it never had session_history
    # written, now clears the expert scan and is resumable.
    _write_ticket(tmp_path, "T-1049", title="load-bearing trap", filed_by="S-p714")
    ranked = _find(tmp_path, ["T-1049"], [_session("S-p714")])
    assert [c["sid"] for c in ranked] == ["S-p714"]
    assert "filed T-1049" in ranked[0]["reasons"]
    # Filing alone is not the same claim as having worked it (DoD #1) —
    # never auto-resume-eligible on this signal by itself.
    assert ranked[0]["confidence"] == "medium"


def test_healthy_case_unrelated_filer_still_refused(tmp_path):
    # DoD #3: a session with no relationship to the ticket — including not
    # having filed it — must still be refused. `filed_by` must not widen
    # into "offer everyone".
    _write_ticket(tmp_path, "T-1049", title="load-bearing trap",
                  filed_by="S-someone-else")
    ranked = _find(tmp_path, ["T-1049"], [_session("S-p714")])
    assert ranked == []


def test_filed_by_does_not_duplicate_a_worked_reason(tmp_path):
    # A session that both worked (session_history) AND filed a ticket keeps
    # the HIGH "worked" reason unchanged — filing the same ticket you already
    # worked adds no second, weaker reason for it.
    _write_ticket(tmp_path, "T-100", title="prior", session_history=["S-A"],
                  filed_by="S-A")
    _write_ticket(tmp_path, "T-200", body="Related: [[T-100]].")
    ranked = _find(tmp_path, ["T-200"], [_session("S-A")])
    assert ranked[0]["confidence"] == "high"
    assert not any(r.startswith("filed ") for r in ranked[0]["reasons"])


# ---------------------------------------------------------------------------
# T-1047 decision site 1 — expert discovery must survive block/folded
# session_history, not just the flow-style `_write_ticket` fixtures above.
# ---------------------------------------------------------------------------
def _write_raw_ticket(tmp_path: Path, tid: str, fm_block: str, body: str = "") -> Path:
    d = _backlog(tmp_path)
    p = d / f"{tid}-x.md"
    p.write_text(f"---\n{fm_block}\n---\n\n{body}\n")
    return p


def test_block_format_session_history_still_finds_expert(tmp_path):
    # T-0003's real shape, copied byte-for-byte off the live board: block-style
    # session_history, which the old flat reader returned as completely empty
    # (T-1047 arm 3) — the expert it names must still surface.
    _write_raw_ticket(
        tmp_path, "T-0003",
        "id: T-0003\n"
        'title: Active operator session is displayed as "suspended" in the UI\n'
        "status: closed\n"
        "session_history:\n"
        "- S-almdudleer-operator-shown-active-p35\n",
    )
    _write_ticket(tmp_path, "T-0200", body="Related: [[T-0003]].")
    ranked = _find(tmp_path, ["T-0200"], [_session("S-almdudleer-operator-shown-active-p35")])
    assert [c["sid"] for c in ranked] == ["S-almdudleer-operator-shown-active-p35"]
    assert ranked[0]["confidence"] == "high"


def test_folded_flow_session_history_still_finds_every_expert(tmp_path):
    # T-0207's real shape, copied byte-for-byte off the live board: a flow
    # list folded past pyyaml's 80-column width. The ticket's own measurement
    # found the OLD reader recovered 2 of the 4 real SIDs here — all 4 must
    # come back now (T-1047 arm 1).
    sids = [
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p87",
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p106",
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p109",
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p120",
    ]
    _write_raw_ticket(
        tmp_path, "T-0207",
        "id: T-0207\n"
        "title: Route ALL backlog-ticket creation through the bsq task_new allocator — operator\n"
        "  dispatch templating direct-writes T-NNNN files and collides\n"
        "status: closed\n"
        f"session_history: [{sids[0]}, {sids[1]},\n"
        f"  {sids[2]}, {sids[3]}]\n",
    )
    _write_ticket(tmp_path, "T-0300", body="Related: [[T-0207]].")
    ranked = _find(tmp_path, ["T-0300"], [_session(s) for s in sids])
    assert {c["sid"] for c in ranked} == set(sids)
    assert all(c["confidence"] == "high" for c in ranked)


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
