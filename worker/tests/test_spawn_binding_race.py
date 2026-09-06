"""T-0525: concurrency-safe spawn binding + deterministic primary reconciler.

ROOT CAUSE the fix addresses: spawn wrote the assigned task_id into a SINGLE
shared ``<repo>/.claude/task_id`` marker; concurrent (cross-cluster) spawns
clobbered it before each new claude's SessionStart hook read it → cross-wired
PRIMARY bindings. The fix carries the task_id to the new claude via a
PER-PROCESS env var (``BOT_SQUAD_TASK_ID`` — like BOT_SQUAD_INITIATIVE/OWNER),
so concurrent spawns share no mutable binding state, and adds a deterministic
reconciler that repairs a cross-wired/unbound primary from the authoritative
ticket ``session_history``.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import bot_squad_worker.sessions as S
from bot_squad_worker.sessions import (
    PaneInfo,
    compute_sid,
    reconcile_primary_from_history,
    spawn,
    _read_session_metadata,
    _write_session_metadata,
)


# ---------------------------------------------------------------------------
# Config + fixtures
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "backlog").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "vision" / "initiatives").mkdir(parents=True, exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(
        projects=cfg.projects, data_dir=data_dir,
        config_dir=cfg_dir, tg_bot_token="",
    )


class _FakeTmux:
    """Minimal stateful tmux backend: records each launched shell command and
    fabricates a discoverable pane per ``new-window`` so spawn() can compute a
    real SID and stamp seed metadata."""

    def __init__(self) -> None:
        self.panes: list[PaneInfo] = []
        self.launched: list[str] = []
        self._n = 0

    def run(self, args, **kwargs):
        import subprocess
        if args[:2] == ["tmux", "new-window"]:
            win = cwd = ""
            for i, a in enumerate(args):
                if a == "-n" and i + 1 < len(args):
                    win = args[i + 1]
                if a == "-c" and i + 1 < len(args):
                    cwd = args[i + 1]
            shell_cmd = args[-1]
            self.launched.append(shell_cmd)
            self._n += 1
            self.panes.append(PaneInfo(
                pane_id=f"%{self._n}", window=win, pid=str(1000 + self._n),
                cwd=cwd, command="claude",
            ))
        return subprocess.CompletedProcess(args, 0, "", "")

    def list_panes(self):
        return list(self.panes)


@pytest.fixture
def tmux(monkeypatch):
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_run", fake.run)
    monkeypatch.setattr(S, "list_panes", fake.list_panes)
    monkeypatch.setattr(S, "_ensure_project_tmux_session", lambda *a, **k: None)
    monkeypatch.setattr(S, "_enforce_parallel_cap", lambda cfg, slug=None: None)
    monkeypatch.setattr(S, "_enforce_token_cap", lambda cfg: None)
    return fake


def _seed_task(cfg, task_id, status="open", history=None) -> None:
    hist = ""
    if history:
        hist = "session_history: [" + ", ".join(history) + "]\n"
    p = cfg.data_dir / "test-project" / "backlog" / f"{task_id}-thing.md"
    p.write_text(f"---\nid: {task_id}\nstatus: {status}\n{hist}---\nbody\n")


def _seed_session(cfg, sid, **fields) -> Path:
    meta = {"sid": sid, "status": "active", "window": "dev"}
    meta.update(fields)
    p = S._session_file(cfg.data_dir, "test-project", sid)
    _write_session_metadata(p, meta)
    return p


# ---------------------------------------------------------------------------
# Part 1 — per-process env channel (no shared marker race)
# ---------------------------------------------------------------------------

def test_spawn_passes_task_id_via_per_process_env(tmp_path, tmux):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001")
    spawn(cfg, "test-project", "dev", task_id="T-0001")
    assert len(tmux.launched) == 1
    assert "BOT_SQUAD_TASK_ID=T-0001" in tmux.launched[0]


def test_spawn_without_task_omits_env(tmp_path, tmux):
    cfg = _make_cfg(tmp_path)
    spawn(cfg, "test-project", "dev")
    assert "BOT_SQUAD_TASK_ID" not in tmux.launched[0]


def test_spawn_does_not_write_shared_marker(tmp_path, tmux):
    """The shared ``<repo>/.claude/task_id`` marker WAS the race source — spawn
    must no longer write it (the per-process env carries the binding)."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001")
    spawn(cfg, "test-project", "dev", task_id="T-0001")
    marker = tmp_path / "repo" / ".claude" / "task_id"
    assert not marker.exists()


def test_spawn_clears_stale_shared_marker(tmp_path, tmux):
    """A stale marker left by a prior (pre-fix) deploy must not poison an
    env-less reader — spawn best-effort removes it."""
    cfg = _make_cfg(tmp_path)
    marker = tmp_path / "repo" / ".claude" / "task_id"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("T-9999")  # stale residue
    _seed_task(cfg, "T-0001")
    spawn(cfg, "test-project", "dev", task_id="T-0001")
    assert not marker.exists()


def test_concurrent_spawns_carry_disjoint_task_id_env(tmp_path, tmux):
    """Two back-to-back spawns each carry their OWN task_id in a per-process env
    and each session md gets the CORRECT primary — no shared mutable state to
    clobber, so no cross-wire (the live incident's failure mode)."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001")
    _seed_task(cfg, "T-0002")
    r1 = spawn(cfg, "test-project", "alpha", task_id="T-0001")
    r2 = spawn(cfg, "test-project", "beta", task_id="T-0002")

    assert "BOT_SQUAD_TASK_ID=T-0001" in tmux.launched[0]
    assert "BOT_SQUAD_TASK_ID=T-0002" in tmux.launched[1]

    m1 = _read_session_metadata(S._session_file(cfg.data_dir, "test-project", r1["sid"]))
    m2 = _read_session_metadata(S._session_file(cfg.data_dir, "test-project", r2["sid"]))
    assert m1["task_id"] == "T-0001"
    assert m2["task_id"] == "T-0002"
    # And each spawn appended its OWN SID to the correct ticket's session_history.
    from bot_squad_worker import frontmatter as fm
    h1 = fm.parse(( cfg.data_dir / "test-project" / "backlog" / "T-0001-thing.md").read_text())[0]
    h2 = fm.parse(( cfg.data_dir / "test-project" / "backlog" / "T-0002-thing.md").read_text())[0]
    assert r1["sid"] in fm.as_list(h1.get("session_history"))
    assert r2["sid"] in fm.as_list(h2.get("session_history"))


# ---------------------------------------------------------------------------
# Part 2 — deterministic reconciler: rewrite primary from session_history
# ---------------------------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    """Mark a set of SIDs as having a live pane (so the reconciler treats them
    as live sessions). Returns a set the test mutates."""
    sids: set[str] = set()
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")

    def _panes():
        out = []
        for sid in sids:
            # SID = S-u-<window>-p<n>
            body = sid[len("S-u-"):]
            window, _, pno = body.rpartition("-p")
            out.append(PaneInfo(pane_id=f"%{pno}", window=window, pid="1",
                                cwd="/r", command="claude"))
        return out

    monkeypatch.setattr(S, "list_panes", _panes)
    return sids


def test_reconcile_rewrites_crosswired_primary(tmp_path, live):
    cfg = _make_cfg(tmp_path)
    # A was spawned for T-0001 (its history lists A) but got cross-wired to a
    # primary of T-0002 (whose history lists B, not A).
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])
    _seed_task(cfg, "T-0002", history=["S-u-beta-p2"])
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="T-0002")
    _seed_session(cfg, "S-u-beta-p2", window="beta", task_id="T-0002")
    live.update({"S-u-alpha-p1", "S-u-beta-p2"})

    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 1
    assert _read_session_metadata(a)["task_id"] == "T-0001"
    # B's correct primary untouched.
    b = S._session_file(cfg.data_dir, "test-project", "S-u-beta-p2")
    assert _read_session_metadata(b)["task_id"] == "T-0002"


def test_reconcile_is_idempotent_and_converges(tmp_path, live):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])
    _seed_task(cfg, "T-0002", history=["S-u-beta-p2"])
    _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="T-0002")
    _seed_session(cfg, "S-u-beta-p2", window="beta", task_id="T-0002")
    live.update({"S-u-alpha-p1", "S-u-beta-p2"})

    reconcile_primary_from_history(cfg, "test-project")
    res2 = reconcile_primary_from_history(cfg, "test-project")
    assert res2["rewritten"] == 0  # no flip-flop on a second pass


def test_reconcile_adopts_empty_primary_from_history(tmp_path, live):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="~")
    live.add("S-u-alpha-p1")
    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 1
    assert _read_session_metadata(a)["task_id"] == "T-0001"


def test_reconcile_skips_ambiguous_multi_home(tmp_path, live):
    """SID in TWO non-extra tickets' history → ambiguous; leave for other passes."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])
    _seed_task(cfg, "T-0002", history=["S-u-alpha-p1"])
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="~")
    live.add("S-u-alpha-p1")
    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 0
    assert _read_session_metadata(a)["task_id"] is None


def test_reconcile_excludes_extra_bound_tickets(tmp_path, live):
    """A ticket the SID is BOUND to as an extra is not a primary candidate."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])  # the spawn-primary home
    _seed_task(cfg, "T-0003", history=["S-u-alpha-p1"])  # bound as an extra
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="~",
                      extra_task_ids=["T-0003"])
    live.add("S-u-alpha-p1")
    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 1
    assert _read_session_metadata(a)["task_id"] == "T-0001"


def test_reconcile_leaves_consistent_primary(tmp_path, live):
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="T-0001")
    live.add("S-u-alpha-p1")
    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 0
    assert _read_session_metadata(a)["task_id"] == "T-0001"


def test_reconcile_only_touches_live_sessions(tmp_path, live):
    """A suspended/no-pane session is history — gc_dead_bindings owns it, not us."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="T-0002",
                      status="suspended")
    # no live pane added
    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 0
    assert _read_session_metadata(a)["task_id"] == "T-0002"


def test_reconcile_skips_closed_candidate(tmp_path, live):
    """If the only home ticket is closed, don't rewrite — leave the dead binding
    for gc_dead_bindings to strip."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", status="closed", history=["S-u-alpha-p1"])
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="~")
    live.add("S-u-alpha-p1")
    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 0


def test_reconcile_clears_false_last_task_id_residue(tmp_path, live):
    """A last_task_id pointing at a ticket whose history does NOT list this SID
    is false residue that feeds reconciler oscillation — clear it."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-alpha-p1"])
    _seed_task(cfg, "T-0002", status="closed", history=["S-u-beta-p2"])  # not A's
    a = _seed_session(cfg, "S-u-alpha-p1", window="alpha", task_id="T-0001",
                      last_task_id="T-0002")
    live.add("S-u-alpha-p1")
    reconcile_primary_from_history(cfg, "test-project")
    assert _read_session_metadata(a).get("last_task_id") in (None, "~")


def test_reconcile_never_gives_tl_a_primary(tmp_path, live):
    """A teamlead window must never adopt a task primary from history."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", history=["S-u-multi_server-TL-p1"])
    a = _seed_session(cfg, "S-u-multi_server-TL-p1", window="multi_server-TL",
                      task_id="~", initiative="multi-server.md")
    live.add("S-u-multi_server-TL-p1")
    res = reconcile_primary_from_history(cfg, "test-project")
    assert res["rewritten"] == 0
    assert _read_session_metadata(a)["task_id"] is None
