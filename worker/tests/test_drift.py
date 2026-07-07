"""Tests for T-0149 drift enforcement (bot_squad_worker.drift)."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from bot_squad_worker import drift, sessions as S
from tests.test_jobs import _make_config_with_project, _make_project_with_repo


@dataclass
class _FakePane:
    window: str = "dynamic-context-manager"
    pane_id: str = "%9"
    cwd: str = "/repo"
    pid: str = "111"
    session: str = "bot-squad"


SID = "S-tester-dynamic-context-manager-p9"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _setup(tmp_path: Path, *, updated_ago_min: int, activity_ago_sec: int,
           role: str = "dev", status: str = "active", task_id: str = "T-0149",
           drift_enforcement: bool = True, owner: str = "",
           session_initiative: str = "", ticket_initiative: str = "",
           drift_paused: bool = False, ticket_status: str = "in_progress"):
    """Build cfg + a backlog ticket + a session md, return (cfg, slug, now, ticket).

    drift_enforcement defaults True so the legacy fire-tests keep firing under
    the T-0184 per-project opt-in; opt-out tests pass drift_enforcement=False.
    """
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    # T-0184: the loaded project defaults drift_enforcement=False; flip it per-test.
    cfg.projects[slug] = replace(cfg.projects[slug], drift_enforcement=drift_enforcement)
    now = time.time()

    backlog = cfg.data_dir / slug / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    ticket = backlog / f"{task_id}-dynamic-context-manager.md"
    init_line = f"initiative: {ticket_initiative}\n" if ticket_initiative else ""
    ticket.write_text(
        f"---\nid: {task_id}\ntitle: Dynamic context manager\n"
        f"status: {ticket_status}\n{init_line}updated: {_iso(now - updated_ago_min * 60)}\n---\n\n"
        "## DoD\n- do the thing\n"
    )

    sessions_dir = cfg.data_dir / slug / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    md_owner = f"owner: {owner}\n" if owner else ""
    md_init = f"initiative: {session_initiative}\n" if session_initiative else ""
    md_paused = "drift_paused: true\n" if drift_paused else ""
    (sessions_dir / f"{SID}.md").write_text(
        f"---\nsid: {SID}\nstatus: {status}\nwindow: dynamic-context-manager\n"
        f"pane_id: %9\nclaude_uuid: uuid-1\ntask_id: {task_id}\n"
        f"{md_owner}{md_init}{md_paused}---\n"
    )

    row = {
        "sid": SID, "status": status, "role": role, "task_id": task_id,
        "activity_at": now - activity_ago_sec, "cwd": "/repo",
        "claude_uuid": "uuid-1", "started_at": _iso(now - 3 * 3600),
        "owner": owner, "initiative": session_initiative, "extra_initiatives": [],
    }

    def patch(monkeypatch):
        monkeypatch.setattr(S, "list_sessions", lambda c, s: [row])
        monkeypatch.setattr(S, "_get_current_user", lambda: "tester")
        monkeypatch.setattr(S, "list_panes", lambda: [_FakePane()])
        monkeypatch.setattr(S, "compute_sid", lambda u, w, p: SID)

    return cfg, slug, now, ticket, patch


def test_stale_session_gets_nudged(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=90, activity_ago_sec=30)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setenv("BOT_SQUAD_DRIFT_COOLDOWN_MINUTES", "30")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))

    res = drift.drift_check(cfg, slug)
    assert res["ok"] and len(res["nudged"]) == 1
    assert res["nudged"][0]["signal"] == "stale"
    assert delivered and "DRIFT CHECK" in delivered[0][1] and "T-0149" in delivered[0][1]


def test_disabled_by_env(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=600, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "0")
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res.get("disabled") is True and not delivered


def test_idle_session_not_nudged(tmp_path, monkeypatch):
    # Recent activity is older than the active window → waiting at a prompt.
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=600, activity_ago_sec=99999)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_non_dev_role_not_nudged(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=600, activity_ago_sec=10, role="tl")
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_superpowers_signal(tmp_path, monkeypatch):
    # Not stale (recent update) but writing to superpowers → superpowers signal.
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=1, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets",
                        lambda *_a, **_k: ["/home/u/.claude/superpowers/skills/foo.md"])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "superpowers"
    assert "T-0152" in delivered[0][1] and "superpowers" in delivered[0][1]


def test_automation_signal_only_without_scenario(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=1, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets",
                        lambda *_a, **_k: ["/repo/tests/e2e/foo.mjs"])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "automation"


def test_automation_signal_suppressed_when_scenario_exists(tmp_path, monkeypatch):
    # Manual scenario already written → premature-automation signal is suppressed,
    # and the ticket was just updated so there's no stale signal either.
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=1, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets",
                        lambda *_a, **_k: ["/repo/tests/e2e/foo.mjs"])
    scen = cfg.data_dir / slug / "scenarios"
    scen.mkdir(parents=True, exist_ok=True)
    (scen / "T-0149-dynamic-context-manager.md").write_text("# scenario\n")
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_recent_write_targets_ignores_bash_commands(tmp_path):
    # Regression (T-0158 manual walkthrough): a Bash command that merely
    # MENTIONS a superpowers path / .mjs must not be treated as a write target
    # — only Edit/Write file_paths count. Otherwise sessions working on this
    # very feature false-flag themselves.
    jsonl = tmp_path / "t.jsonl"
    import json as _j
    with open(jsonl, "w") as fh:
        fh.write(_j.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "grep -r superpowers ~/.claude/superpowers/foo.mjs"}}]}}) + "\n")
        fh.write(_j.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "/repo/src/real.py"}}]}}) + "\n")
    targets = drift._recent_write_targets(jsonl)
    assert targets == ["/repo/src/real.py"]


def test_cooldown_suppresses_second_nudge(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=90, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setenv("BOT_SQUAD_DRIFT_COOLDOWN_MINUTES", "30")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))

    first = drift.drift_check(cfg, slug)
    assert len(first["nudged"]) == 1
    second = drift.drift_check(cfg, slug)  # within cooldown
    assert second["nudged"] == [] and len(delivered) == 1


# --- T-0184: per-project opt-in + per-session off-ramp + footer ---

def test_skipped_when_project_not_opted_in(tmp_path, monkeypatch):
    # A stale, actively-working dev — but the project did NOT opt into drift
    # enforcement (e.g. signal-tracker). The whole project is skipped.
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=90, activity_ago_sec=30, drift_enforcement=False)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res.get("skipped") == "drift_enforcement_off"
    assert res["nudged"] == [] and not delivered


def test_per_session_pause_suppresses_nudge(tmp_path, monkeypatch):
    # Stale dev in an opted-in project, but the session set drift_paused: true
    # (via `bsq drift off`) → no nudge.
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=90, activity_ago_sec=30, drift_paused=True)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_drift_message_carries_off_ramp_footer(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=90, activity_ago_sec=30)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1
    assert "bsq drift off" in delivered[0][1]


# --- T-0185: constant-team skip + initiative-match guard ---

def test_constant_team_session_not_nudged(tmp_path, monkeypatch):
    # owner=constant-team → queue consumer, no single-ticket DoD → never nagged,
    # even when the (mis-bound) ticket looks stale.
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=120, activity_ago_sec=30, owner="constant-team")
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_initiative_mismatch_not_nudged(tmp_path, monkeypatch):
    # Session is bound to feedback initiative; the (mis-bound) ticket belongs to
    # a different initiative → the cross-initiative nag is suppressed (p38 case).
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=120, activity_ago_sec=30,
        session_initiative="user-feedback.md",
        ticket_initiative="operator-ux-and-session-mgmt.md")
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_initiative_match_still_nudges(tmp_path, monkeypatch):
    # Same initiative on both → the guard does NOT suppress a genuine stale nag.
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=120, activity_ago_sec=30,
        session_initiative="operator-ux-and-session-mgmt.md",
        ticket_initiative="operator-ux-and-session-mgmt.md")
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "stale"


# --- T-0190: skip a dev whose bound ticket is terminal (totest/closed) ---

@pytest.mark.parametrize("terminal_status", ["totest", "closed"])
def test_terminal_ticket_status_not_nudged(tmp_path, monkeypatch, terminal_status):
    # A dev that reported READY and set its ticket totest/closed is done &
    # awaiting TL review — stale + actively-working, yet must NOT be nagged
    # (the dogfood incident: drift dev nagged about its own ticket 89 min after
    # setting it totest).
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=90, activity_ago_sec=30, ticket_status=terminal_status)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_in_progress_ticket_still_nudges(tmp_path, monkeypatch):
    # in_progress is NOT terminal → a stale, actively-working dev is still nagged.
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=90, activity_ago_sec=30, ticket_status="in_progress")
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "stale"


def test_reopened_ticket_still_nudges(tmp_path, monkeypatch):
    # reopened is NOT terminal — the work is live again, so a stale dev is nagged.
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=90, activity_ago_sec=30, ticket_status="reopened")
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "stale"


def test_ticket_id_collision_resolves_by_frontmatter_not_alpha_sort(tmp_path, monkeypatch):
    """T-0231: a tombstone left behind after a renumber (or a genuine id
    collision) can share the numeric filename prefix with the session's real
    bound ticket. If it sorts alphabetically BEFORE the real ticket, the old
    ``sorted(glob())[0]`` resolution would nag using the WRONG ticket's title
    (or wrongly skip the nudge if the tombstone reads as terminal/closed).
    resolve_id_file must still find the real, in-progress ticket."""
    cfg, slug, now, ticket, patch = _setup(
        tmp_path, updated_ago_min=90, activity_ago_sec=30, task_id="T-0030",
        ticket_status="in_progress")
    # ticket file from _setup is "T-0030-dynamic-context-manager.md" — add a
    # tombstone that sorts alphabetically BEFORE it and looks terminal/closed
    # (mirroring the real T-0030 tombstone convention: mismatched id: value).
    backlog = cfg.data_dir / slug / "backlog"
    tombstone = backlog / "T-0030-aaa-tombstone.md"
    tombstone.write_text(
        "---\nid: T-0030-DUPLICATE-DO-NOT-USE\n"
        "title: WRONG TICKET SHOULD NOT BE NAGGED ABOUT\nstatus: closed\n---\n\nbody\n"
    )
    assert sorted(backlog.glob("T-0030-*.md"))[0] == tombstone  # sanity

    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(
        S, "_deliver_prompt",
        lambda pane, text, **_kw: delivered.append((pane, text)))

    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "stale"
    assert "Dynamic context manager" in delivered[0][1]
    assert "WRONG TICKET" not in delivered[0][1]


# ── T-0449 (#6): _initiative_stem DRY-delegates the .md-strip to normalize_id ──

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("foo.md", "foo"),       # strip exactly one trailing .md
        ("foo", "foo"),          # bare stem unchanged
        ("~", ""),               # YAML null sentinel → empty
        ("", ""),                # empty → empty
        (None, ""),              # None → empty
        ("  bar.md  ", "bar"),   # surrounding whitespace trimmed, then stripped
        ("x.md.md", "x.md"),     # not greedy (one suffix only)
        ("a.MD", "a.MD"),        # case-sensitive: .MD is NOT stripped
    ],
)
def test_initiative_stem_behaviour_unchanged(raw, expected):
    assert drift._initiative_stem(raw) == expected


def test_initiative_stem_delegates_to_normalize_id(monkeypatch):
    """The .md-strip must route through the shared normalize_id (no inline dup)."""
    calls = []

    def _spy(value):
        calls.append(value)
        return "SENTINEL"

    monkeypatch.setattr(drift, "normalize_id", _spy)
    out = drift._initiative_stem("anything.md")
    assert out == "SENTINEL"
    assert calls == ["anything.md"]
