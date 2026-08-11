"""T-0802 (b): the sweep for tmux sessions no registered project claims.

THE GAP THIS CLOSES, stated as the incident stated it. A tmux session named
``test-project`` — a slug that appears in no ``projects.toml`` and has no data
dir — accumulated one live ``claude`` process per worker-suite run for three
days, 70 of them, until the host's RAM and all 8 GB of swap were gone. Every
reconciler in ``binding_gc_tick`` ran normally throughout. None of them could
have seen it: they are all invoked as ``fn(cfg, slug)`` over
``cfg.projects``, and ``gc_tmux_sessions`` — the one whose job is tmux —
raises ``ActionError`` on an unregistered slug before it looks at anything,
skips any name that is not ``<slug>-``prefixed, and spares any session holding
a live claude pane. All 70 held one.

So the tests below are about the two halves that make the NEXT one visible:
what the sweep is allowed to touch (narrowly — the ownership proof, the
quarantine, the self-pane), and that it SAYS SO on the first tick rather than
only when it kills.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker.sessions import PaneInfo


def _make_cfg(tmp_path: Path, repo_path: Path | None = None) -> Any:
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "real-project" / "sessions").mkdir(parents=True, exist_ok=True)
    rp = str(repo_path or (tmp_path / "repo"))
    (cfg_dir / "projects.toml").write_text(
        f'[projects.real-project]\n'
        f'slug = "real-project"\n'
        f'display_name = "Real"\n'
        f'repo_path = "{rp}"\n'
        f'deploy_branch = ""\nmaster_branch = ""\nprod_url = ""\n'
        f'staging_url = ""\ndev_url = ""\ndeploy_targets = []\ntg_chat = "0"\n'
        f'created_at = 2026-05-10\n')
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    # Config is a frozen dataclass whose data_dir is derived; the suite's
    # convention (tests/test_sessions.py) is a namespace with the same surface
    # pointed at a tmp data dir. `projects` stays the real registry object, so
    # the slug/repo_path lookups under test are the production ones.
    import types
    return types.SimpleNamespace(projects=dict(cfg.projects), data_dir=data_dir,
                                 tg_bot_token=cfg.tg_bot_token)


class _Tmux:
    """A tmux server as data: sessions, their windows, their panes.

    Stubs ``_run`` and ``list_panes`` together, so a test cannot describe a
    session whose windows and panes disagree — the ownership rule under test is
    exactly a statement about those two agreeing.
    """

    def __init__(self, sessions: dict[str, dict]):
        self.sessions = sessions
        self.killed: list[str] = []

    def run(self, args, **kwargs):
        if "list-sessions" in args:
            out = "".join(f"{n}|{s.get('activity', 0)}\n"
                          for n, s in self.sessions.items())
            return subprocess.CompletedProcess(args, 0, out, "")
        if "list-windows" in args:
            out = "".join(f"{n}|{w}\n" for n, s in self.sessions.items()
                          for w in s.get("windows", []))
            return subprocess.CompletedProcess(args, 0, out, "")
        if "kill-session" in args:
            name = args[args.index("-t") + 1]
            self.killed.append(name)
            self.sessions.pop(name, None)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def panes(self) -> list[PaneInfo]:
        out = []
        i = 0
        for name, s in self.sessions.items():
            for p in s.get("panes", []):
                i += 1
                out.append(PaneInfo(pane_id=p.get("id", f"%{i}"),
                                    window=p.get("window", "_init"),
                                    pid=str(i), cwd=p.get("cwd", "/tmp"),
                                    command=p.get("command", "bash"),
                                    session=name))
        return out


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A configured worker with one registered project and a stubbed tmux."""
    import bot_squad_worker.sessions as S

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    def _wire(sessions: dict[str, dict], now: float = 100_000.0):
        tmux = _Tmux(sessions)
        monkeypatch.setattr(S, "_run", tmux.run)
        monkeypatch.setattr(S, "list_panes", tmux.panes)
        monkeypatch.setattr(S.time, "time", lambda: now)
        return tmux

    return cfg, repo, _wire, S


# ---------------------------------------------------------------------------
# what it reaps — and, mostly, what it must not
# ---------------------------------------------------------------------------

def test_first_sighting_never_reaps_it_sounds_the_alarm(wired):
    """The ticket's own session, on the tick it is discovered. It is REPORTED,
    and it is left alone: a reaper whose first act on a new name is to kill it
    has no window in which a human can say "that one is mine"."""
    cfg, repo, wire, S = wired
    tmux = wire({
        "real-project": {"windows": ["_init"],
                         "panes": [{"cwd": str(repo), "command": "bash"}]},
        "test-project": {"windows": ["_init", "gu_1-user-conversation"],
                         "panes": [{"cwd": "/home/almdudleer", "command": "claude"}]},
    })

    res = S.gc_orphan_tmux_sessions(cfg)

    assert tmux.killed == []
    assert [d["session"] for d in res["sighted"]] == ["test-project"]
    assert res["sighted"][0]["claude_panes"] == 1
    assert res["reaped"] == []


def test_it_is_reaped_once_the_quarantine_has_elapsed(wired):
    """Second tick, past the grace: killed — WITH a live claude pane in it.

    That inversion is the ticket. ``gc_tmux_sessions`` spares a session holding
    a live claude pane because there it means a staffed team. Here the session
    has already been proven to be bot-squad's own, for a project that does not
    exist, so the pane is not work — it is the runaway. Sparing it would
    reproduce the bug exactly: all 70 leaked sessions held one.
    """
    cfg, repo, wire, S = wired
    sessions = {
        "test-project": {"windows": ["_init", "gu_1-user-conversation"],
                         "panes": [{"cwd": "/home/almdudleer", "command": "claude"}]},
    }
    tmux = wire(sessions, now=100_000.0)
    S.gc_orphan_tmux_sessions(cfg)          # first sighting
    assert tmux.killed == []

    wire(sessions, now=100_000.0 + S._ORPHAN_TMUX_QUARANTINE_SEC + 1)
    res = S.gc_orphan_tmux_sessions(cfg)

    assert [d["session"] for d in res["reaped"]] == ["test-project"]
    assert res["reaped"][0]["claude_panes"] == 1


def test_a_second_tick_inside_the_grace_still_does_not_reap(wired):
    """The quarantine is a duration, not a "seen it twice" counter — the tick
    runs every 60s, so a two-tick rule would be a two-minute grace.

    THIS TEST EXISTS BECAUSE ITS ABSENCE WAS MEASURED. Collapsing the grace to
    zero in a scratch copy left every other test in this file green except the
    reap-disabled one, which fails for a different reason. The long grace is the
    whole defence against reaping a live project through a transient
    de-registration, so it needs an assertion of its own.
    """
    cfg, repo, wire, S = wired
    sessions = {
        "test-project": {"windows": ["_init"], "panes": [{"cwd": "/tmp",
                                                          "command": "claude"}]},
    }
    wire(sessions, now=100_000.0)
    S.gc_orphan_tmux_sessions(cfg)

    tmux = wire(sessions, now=100_000.0 + 60)          # one tick later
    res = S.gc_orphan_tmux_sessions(cfg)

    assert tmux.killed == [], "reaped inside the quarantine"
    assert [d["session"] for d in res["quarantined"]] == ["test-project"]
    assert res["reaped"] == []


def test_a_session_without_the_init_window_is_never_touched(wired):
    """THE ownership gate. ``_init`` is parked by
    ``_ensure_project_tmux_session`` and by nothing else — so its absence means
    bot-squad did not create this session, and a human's own tmux is off limits
    however long it sits there and whatever it is called."""
    cfg, repo, wire, S = wired
    sessions = {
        "someones-work": {"windows": ["vim", "shell"],
                          "panes": [{"cwd": "/home/almdudleer", "command": "vim"}]},
    }
    tmux = wire(sessions, now=100_000.0)
    first = S.gc_orphan_tmux_sessions(cfg)
    wire(sessions, now=100_000.0 + S._ORPHAN_TMUX_QUARANTINE_SEC * 10)
    later = S.gc_orphan_tmux_sessions(cfg)

    assert first["sighted"] == [] and later["reaped"] == []
    assert tmux.killed == []


def test_a_registered_project_and_its_siblings_are_out_of_scope(wired):
    """The bare slug belongs to its project; ``<slug>-*`` belongs to the T-0200
    reaper, which understands teams, constant-team graces and rooting. This
    sweep must not second-guess either."""
    cfg, repo, wire, S = wired
    sessions = {
        "real-project": {"windows": ["_init"], "panes": [{"cwd": str(repo)}]},
        "real-project-ghost": {"windows": ["_init"], "panes": [{"cwd": str(repo)}]},
    }
    tmux = wire(sessions, now=100_000.0)
    S.gc_orphan_tmux_sessions(cfg)
    wire(sessions, now=100_000.0 + S._ORPHAN_TMUX_QUARANTINE_SEC * 10)
    res = S.gc_orphan_tmux_sessions(cfg)

    assert tmux.killed == [] and res["reaped"] == [] and res["sighted"] == []


def test_a_pane_rooted_in_a_registered_repo_spares_the_session(wired):
    """A session whose name nobody recognises but whose panes are working in a
    registered repo is doing that project's work under another name. Killing it
    would destroy real work, so the cwd evidence outranks the name."""
    cfg, repo, wire, S = wired
    sessions = {
        "renamed-somehow": {"windows": ["_init"],
                            "panes": [{"cwd": str(repo / "sub"), "command": "claude"}]},
    }
    tmux = wire(sessions, now=100_000.0)
    S.gc_orphan_tmux_sessions(cfg)
    wire(sessions, now=100_000.0 + S._ORPHAN_TMUX_QUARANTINE_SEC * 10)
    res = S.gc_orphan_tmux_sessions(cfg)

    assert tmux.killed == [] and res["reaped"] == []


def test_it_never_reaps_the_session_it_is_running_in(wired, monkeypatch):
    """Suicide guard. The sweep also runs from an interactive `bsq` invocation,
    which lives in a pane; an unregistered-looking host session would otherwise
    take its own caller down mid-tick."""
    cfg, repo, wire, S = wired
    sessions = {
        "orphan-host": {"windows": ["_init"],
                        "panes": [{"id": "%77", "cwd": "/tmp", "command": "claude"}]},
    }
    monkeypatch.setenv("TMUX_PANE", "%77")
    wire(sessions, now=100_000.0)
    S.gc_orphan_tmux_sessions(cfg)
    tmux = wire(sessions, now=100_000.0 + S._ORPHAN_TMUX_QUARANTINE_SEC * 10)
    res = S.gc_orphan_tmux_sessions(cfg)

    assert tmux.killed == [] and res["reaped"] == []


def test_reaping_can_be_disabled_and_the_alert_still_fires(wired, monkeypatch):
    """An install that would rather triage by hand keeps the visibility, which
    is the half that does not depend on the kill being right."""
    cfg, repo, wire, S = wired
    sessions = {
        "test-project": {"windows": ["_init"], "panes": [{"cwd": "/tmp",
                                                          "command": "claude"}]},
    }
    wire(sessions, now=100_000.0)
    first = S.gc_orphan_tmux_sessions(cfg)
    monkeypatch.setattr(S, "_ORPHAN_TMUX_REAP", False)
    tmux = wire(sessions, now=100_000.0 + S._ORPHAN_TMUX_QUARANTINE_SEC * 10)
    res = S.gc_orphan_tmux_sessions(cfg)

    assert [d["session"] for d in first["sighted"]] == ["test-project"]
    assert tmux.killed == []
    assert [d["session"] for d in res["quarantined"]] == ["test-project"]


# ---------------------------------------------------------------------------
# the ledger — the clock has to survive the process
# ---------------------------------------------------------------------------

def test_the_quarantine_clock_survives_a_worker_restart(wired):
    """The ledger is on disk, not in a module global, and this is why: deploys
    restart this worker often. An in-memory first_seen would reset every
    candidate's timer on each restart, and the quarantine would never elapse —
    a reaper that looks healthy in every log line and reaps nothing, forever.
    """
    cfg, repo, wire, S = wired
    sessions = {
        "test-project": {"windows": ["_init"], "panes": [{"cwd": "/tmp",
                                                          "command": "claude"}]},
    }
    wire(sessions, now=100_000.0)
    S.gc_orphan_tmux_sessions(cfg)

    ledger = json.loads((cfg.data_dir / "_worker" / "orphan_tmux.json")
                        .read_text(encoding="utf-8"))
    assert ledger["test-project"]["first_seen"] == 100_000.0

    # A fresh process reads the SAME first_seen, so the clock kept running.
    tmux = wire(sessions, now=100_000.0 + S._ORPHAN_TMUX_QUARANTINE_SEC + 1)
    res = S.gc_orphan_tmux_sessions(cfg)
    assert [d["session"] for d in res["reaped"]] == ["test-project"]
    assert tmux.killed == ["test-project"]


def test_a_session_that_stops_being_orphaned_loses_its_record(wired):
    """Registering the project mid-quarantine clears the accrued time, so a
    later de-registration starts a fresh grace instead of an instant kill."""
    cfg, repo, wire, S = wired
    orphan = {"windows": ["_init"], "panes": [{"cwd": "/tmp", "command": "claude"}]}
    wire({"test-project": dict(orphan)}, now=100_000.0)
    S.gc_orphan_tmux_sessions(cfg)

    # …the project gets registered: the name is now a known slug.
    cfg.projects["test-project"] = cfg.projects["real-project"]
    wire({"test-project": dict(orphan)}, now=100_000.0 + 60)
    S.gc_orphan_tmux_sessions(cfg)

    ledger = json.loads((cfg.data_dir / "_worker" / "orphan_tmux.json")
                        .read_text(encoding="utf-8"))
    assert ledger == {}


def test_no_tmux_server_is_not_an_error(wired):
    """The worker runs on hosts with no tmux at all; a failing list-sessions
    must be a quiet no-op, not an exception into the tick."""
    cfg, repo, wire, S = wired
    import bot_squad_worker.sessions as _S

    def failing(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "no server running")

    wire({}, now=100_000.0)
    _S._run = failing
    res = S.gc_orphan_tmux_sessions(cfg)
    assert res == {"ok": True, "reaped": [], "sighted": [], "quarantined": []}


# ---------------------------------------------------------------------------
# the wiring — a pass nobody calls is not a fix
# ---------------------------------------------------------------------------

def test_the_tick_runs_it_once_not_once_per_project(monkeypatch, tmp_path):
    """The per-project loop is the reason the leak was invisible; running this
    sweep inside it would re-inherit that framing (and alert N times)."""
    from bot_squad_worker import jobs as J
    from bot_squad_worker import sessions as S

    cfg = _make_cfg(tmp_path)
    cfg.projects["second-project"] = cfg.projects["real-project"]

    calls: list[Any] = []
    monkeypatch.setattr(S, "gc_orphan_tmux_sessions",
                        lambda c: calls.append(c) or {"ok": True, "reaped": [],
                                                      "sighted": [], "quarantined": []})
    # Neutralise the per-project passes; this test is about the sweep's arity.
    for name in ("gc_sessions", "reconcile_primary_from_history",
                 "reconcile_constant_team_primaries", "gc_dead_bindings",
                 "gc_stale_bindings", "archive_dead_teammates",
                 "reconcile_window_names", "backfill_parent_sid",
                 "gc_tmux_sessions"):
        monkeypatch.setattr(S, name, lambda *a, **k: {"ok": True})

    J.binding_gc_tick(cfg)

    assert len(calls) == 1, (
        f"expected one project-independent sweep, got {len(calls)} for "
        f"{len(cfg.projects)} projects")


def test_a_sighting_reaches_the_operator(monkeypatch, tmp_path):
    """The alert IS the fix for the three-day silence — the reap is hours away
    by design, so if this never fires the ticket is not closed."""
    from bot_squad_worker import jobs as J

    cfg = _make_cfg(tmp_path)
    alerts: list[str] = []
    monkeypatch.setattr(J, "_alert_operators",
                        lambda c, slug, project, text: alerts.append(text))

    J._surface_orphan_tmux(cfg, {
        "ok": True, "sighted": [{"session": "test-project", "claude_panes": 70,
                                 "reap_after_sec": 21600}],
        "reaped": [], "quarantined": []})

    assert len(alerts) == 1
    text = alerts[0]
    assert "test-project" in text
    assert "70" in text, f"the alert hides how much is running: {text!r}"
    assert "6h" in text, f"the alert does not say when it will be killed: {text!r}"


def test_a_reap_is_reported_too(monkeypatch, tmp_path):
    """A silent kill is how a legitimate session disappears and nobody can say
    why — the T-0227 lesson, same shape."""
    from bot_squad_worker import jobs as J

    cfg = _make_cfg(tmp_path)
    alerts: list[str] = []
    monkeypatch.setattr(J, "_alert_operators",
                        lambda c, slug, project, text: alerts.append(text))

    J._surface_orphan_tmux(cfg, {
        "ok": True, "sighted": [],
        "reaped": [{"session": "test-project", "orphaned_for": 25_000,
                    "claude_panes": 3}],
        "quarantined": []})

    assert len(alerts) == 1 and "test-project" in alerts[0]
    assert "reaped" in alerts[0].lower()


def test_an_alert_failure_never_breaks_the_tick(monkeypatch, tmp_path):
    """Every other surfacing helper in this module is best-effort; a TG outage
    must not cost the sweep."""
    from bot_squad_worker import jobs as J

    cfg = _make_cfg(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(J, "_alert_operators", boom)
    J._surface_orphan_tmux(cfg, {
        "ok": True, "sighted": [{"session": "x", "claude_panes": 1,
                                 "reap_after_sec": 21600}],
        "reaped": [], "quarantined": []})   # must not raise
