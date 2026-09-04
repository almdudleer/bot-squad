"""T-0960 — a --resume'd session's context reading is frozen on the ANCESTOR.

Measured live 2026-09-03: the operator pane's cmdline carried
``--resume 29018a66-…`` while the session was writing ``0c671a5d-….jsonl``.
``_pane_claude_uuid_from_proc`` treats ``--resume`` and ``--session-id`` as the
same thing, and ``list_sessions`` prefers that /proc uuid over the md-recorded
one, so telemetry tailed a transcript nobody had written to in 97h. With
``offset == size`` every tick the sampler carried the previous reading forward
unchanged and the context number froze at **410082** — 68.3% of a 600k ceiling
the session was nowhere near. That is the number the stakeholder saw on every
single warning.

The asymmetry is the whole bug: ``--session-id <uuid>`` FORCES the uuid, so it
IS the live transcript. ``--resume <uuid>`` names the transcript the session was
forked FROM; Claude Code then writes a NEW file under a NEW uuid.

Over-reporting is the visible failure. UNDER-reporting is the dangerous one — a
resumed session whose ancestor was small would sit below every threshold forever
and never auto-compact at all.
"""

from __future__ import annotations

import subprocess

import pytest

from bot_squad_worker.sessions import _write_session_metadata, list_sessions
from tests.test_sessions import _make_cfg


def _one_pane_project(tmp_path, monkeypatch, *, proc_uuid, md_uuid, cmdline):
    """One live pane whose md records ``md_uuid`` while /proc says ``proc_uuid``."""
    import bot_squad_worker.sessions as S

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-operator-p513.md", {
        "sid": "S-testuser-operator-p513", "status": "active",
        "window": "operator", "cwd": str(repo), "claude_uuid": md_uuid,
        "task_id": "~", "started_at": "2026-09-03T10:00:00Z",
    })

    fake_panes = f"%513|operator|3001|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_panes, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S, "_pane_claude_uuid_from_proc",
                        lambda pid, home, children=None: proc_uuid)
    # the real resume-vs-session-id probe, driven through its cmdline seam
    monkeypatch.setattr(S, "_proc_children_map", lambda: {3001: []})
    monkeypatch.setattr(S, "_proc_cmdline", lambda pid: list(cmdline))

    rows = list_sessions(cfg, "test-project")
    return {r["sid"]: r for r in rows if r["status"] == "active"}


ANCESTOR = "29018a66-4948-4f2b-9d1b-6e087ae5140e"
LIVE = "0c671a5d-6a8d-43d2-b3cb-1030196a02c3"


def test_a_resumed_pane_reports_the_md_uuid_not_the_ancestor(tmp_path, monkeypatch):
    """DoD 2, first half: cmdline says --resume A, md says B → B wins."""
    by_sid = _one_pane_project(
        tmp_path, monkeypatch, proc_uuid=ANCESTOR, md_uuid=LIVE,
        cmdline=["claude", "--dangerously-skip-permissions",
                 "--resume", ANCESTOR, "--model", "opus"],
    )
    row = by_sid["S-testuser-operator-p513"]
    assert row["claude_uuid"] == LIVE, (
        "a --resume uuid names the ancestor transcript; telemetry tailing it "
        "is what froze the reading at 410082"
    )


def test_a_session_id_pane_still_reports_the_forced_uuid(tmp_path, monkeypatch):
    """DoD 2, second half: --session-id FORCES the uuid, so it stays authoritative
    even when an md disagrees. Without this the fix would break every spawn."""
    by_sid = _one_pane_project(
        tmp_path, monkeypatch, proc_uuid=ANCESTOR, md_uuid=LIVE,
        cmdline=["claude", "--dangerously-skip-permissions",
                 "--session-id", ANCESTOR],
    )
    row = by_sid["S-testuser-operator-p513"]
    assert row["claude_uuid"] == ANCESTOR


def test_agreement_never_consults_the_probe(tmp_path, monkeypatch):
    """The common case must not pay for the rare one: when /proc and the md
    agree there is nothing to decide, so the extra walk never runs."""
    import bot_squad_worker.sessions as S

    calls: list = []
    real = S._proc_uuid_is_resume_only
    monkeypatch.setattr(S, "_proc_uuid_is_resume_only",
                        lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    by_sid = _one_pane_project(
        tmp_path, monkeypatch, proc_uuid=LIVE, md_uuid=LIVE,
        cmdline=["claude", "--resume", ANCESTOR],
    )
    assert by_sid["S-testuser-operator-p513"]["claude_uuid"] == LIVE
    assert calls == []


@pytest.mark.parametrize("cmdline,expected", [
    (["claude", "--resume", ANCESTOR], True),
    (["claude", "--session-id", ANCESTOR], False),
    (["claude", "--dangerously-skip-permissions"], False),
    (["claude", "--resume", LIVE], False),          # a different uuid entirely
    (["bash", "--resume", ANCESTOR], False),        # not a claude process
])
def test_the_resume_probe_reads_the_flag_not_just_the_uuid(monkeypatch, cmdline,
                                                           expected):
    """The probe itself, as a table — the asymmetry it exists to encode."""
    import bot_squad_worker.sessions as S

    monkeypatch.setattr(S, "_proc_cmdline", lambda pid: list(cmdline))
    assert S._proc_uuid_is_resume_only("3001", ANCESTOR, {3001: []}) is expected
