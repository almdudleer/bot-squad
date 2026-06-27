"""Phase 9: bind_task / bind_initiative — multi-binding semantics.

A dev session can hold N tasks (primary + extras); a TL session N
initiatives. Constraint: a single task is bound to at most one dev, and a
single initiative to at most one TL — enforced by collision scan at bind time.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker.actions import ActionError
from bot_squad_worker.config import Config
from bot_squad_worker.sessions import (
    _read_session_metadata,
    _write_session_metadata,
    bind_initiative,
    bind_task,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _all_sessions_live(tmp_path, monkeypatch):
    """T-0402: ``_live_task_owner`` now intersects holders with live claude
    agents (``_live_agent_sids``). These binding tests model every written
    session as a genuinely live agent (their pre-T-0402 assumption), so stub
    ``_live_agent_sids`` to return every session-md SID under the test data dir.
    ``_is_live_holder`` still filters suspended/archived holders, so the
    rebind-when-suspended/archived cases keep passing."""
    import bot_squad_worker.sessions as S

    def _live() -> set[str]:
        out: set[str] = set()
        for md in (tmp_path / "data").glob("*/sessions/*.md"):
            meta = S._read_session_metadata(md)
            if meta:
                out.add(meta.get("sid", md.stem))
        return out

    monkeypatch.setattr(S, "_live_agent_sids", _live)


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True)
    (data_dir / "test-project" / "sessions").mkdir(parents=True)
    (data_dir / "test-project" / "vision" / "initiatives").mkdir(parents=True)
    (data_dir / "test-project" / "_chat").mkdir(parents=True)
    (cfg_dir / "projects.toml").write_text(
        f'[projects.test-project]\n'
        f'slug = "test-project"\n'
        f'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        f'deploy_branch = "bot_squad/dev"\n'
        f'master_branch = "master"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        tg_bot_token="",
    )


def _make_dev_session(cfg, sid: str, task_id: str) -> Path:
    p = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    _write_session_metadata(p, {
        "sid": sid,
        "status": "active",
        "window": "dev1",
        "cwd": "/tmp",
        "claude_uuid": "uuid-1",
        "task_id": task_id,
        "initiative": "~",
        "started_at": "2026-05-12T00:00:00Z",
    })
    return p


def _make_tl_session(cfg, sid: str, initiative: str = "~") -> Path:
    p = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    _write_session_metadata(p, {
        "sid": sid,
        "status": "active",
        "window": "tl",
        "cwd": "/tmp",
        "claude_uuid": "uuid-2",
        "task_id": "~",
        "initiative": initiative,
        "started_at": "2026-05-12T00:00:00Z",
    })
    return p


def _make_task(cfg, task_id: str, title: str = "Some Task") -> Path:
    p = cfg.data_dir / "test-project" / "backlog" / f"{task_id}-foo.md"
    p.write_text(
        f"---\nid: {task_id}\ntitle: {title}\nstatus: open\n---\n\nbody\n"
    )
    return p


def _make_initiative(cfg, name: str) -> Path:
    p = cfg.data_dir / "test-project" / "vision" / "initiatives" / name
    p.write_text(f"# {name}\n\nbody\n")
    return p


# ---------------------------------------------------------------------------
# bind_task
# ---------------------------------------------------------------------------

def test_bind_task_appends_to_extras(tmp_path):
    cfg = _make_cfg(tmp_path)
    sess_md = _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")

    result = bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")
    assert result["ok"] is True
    assert result["sid"] == "S-u-w-p1"
    assert result["task_id"] == "T-0002"
    assert result["extras"] == ["T-0002"]

    meta = _read_session_metadata(sess_md)
    assert meta["task_id"] == "T-0001"
    assert meta["extra_task_ids"] == ["T-0002"]


def test_bind_task_propagates_session_initiative_to_task_md(tmp_path):
    """T-0038: bind_task stamps the dev's initiative onto the bound task md
    if absent. Existing-wins."""
    cfg = _make_cfg(tmp_path)
    sess_md = cfg.data_dir / "test-project" / "sessions" / "S-u-w-p1.md"
    _write_session_metadata(sess_md, {
        "sid": "S-u-w-p1",
        "status": "active",
        "window": "dev1",
        "cwd": "/tmp",
        "claude_uuid": "uuid-1",
        "task_id": "T-0001",
        "initiative": "multi-server-installation-process.md",
        "started_at": "2026-05-12T00:00:00Z",
    })
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")

    bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")

    task_md = cfg.data_dir / "test-project" / "backlog" / "T-0002-foo.md"
    txt = task_md.read_text()
    assert "initiative: multi-server-installation-process.md" in txt


def test_bind_task_does_not_clobber_existing_task_initiative(tmp_path):
    cfg = _make_cfg(tmp_path)
    sess_md = cfg.data_dir / "test-project" / "sessions" / "S-u-w-p1.md"
    _write_session_metadata(sess_md, {
        "sid": "S-u-w-p1",
        "status": "active",
        "window": "dev1",
        "cwd": "/tmp",
        "claude_uuid": "uuid-1",
        "task_id": "T-0001",
        "initiative": "alpha.md",
        "started_at": "2026-05-12T00:00:00Z",
    })
    _make_task(cfg, "T-0001")
    # The bound task already has an initiative set; bind must not overwrite.
    task_md = cfg.data_dir / "test-project" / "backlog" / "T-0002-foo.md"
    task_md.write_text(
        "---\nid: T-0002\ntitle: Some Task\nstatus: open\n"
        "initiative: beta.md\n---\n\nbody\n"
    )

    bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")

    txt = task_md.read_text()
    assert "initiative: beta.md" in txt
    assert "initiative: alpha.md" not in txt


def test_bind_task_no_session_initiative_leaves_task_md_alone(tmp_path):
    """When the dev session has no initiative, no stamping happens."""
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")  # initiative: ~
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")

    bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")

    task_md = cfg.data_dir / "test-project" / "backlog" / "T-0002-foo.md"
    txt = task_md.read_text()
    assert "initiative:" not in txt


def test_bind_task_sends_peer_notification(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002", title="Build Heatmaps")

    bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")

    inbox = cfg.data_dir / "test-project" / "_chat" / "inbox-S-u-w-p1.log"
    assert inbox.exists()
    text = inbox.read_text()
    assert "[BIND_TASK from stakeholder]" in text
    assert "T-0002" in text


def test_bind_task_rejects_tl_session(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_tl_session(cfg, "S-u-tl-p0")
    _make_task(cfg, "T-0001")

    with pytest.raises(ActionError, match="not a dev session"):
        bind_task(cfg, "test-project", "S-u-tl-p0", "T-0001")


def test_bind_task_adopts_empty_primary_for_dev(tmp_path):
    """T-0525: an unbound DEV session (primary stripped/cross-wired to empty by
    the spawn-marker race) is repaired in place — bind_task SETS the primary
    instead of refusing or appending to extras (the old append-only path couldn't
    fix a corrupted/empty primary)."""
    cfg = _make_cfg(tmp_path)
    sess_md = _make_dev_session(cfg, "S-u-w-p1", task_id="~")  # unbound dev
    _make_task(cfg, "T-0001")

    result = bind_task(cfg, "test-project", "S-u-w-p1", "T-0001")
    assert result["ok"] is True
    assert result["primary_set"] is True
    meta = _read_session_metadata(sess_md)
    assert meta["task_id"] == "T-0001"
    assert meta.get("extra_task_ids") in (None, [], )


def test_bind_task_empty_primary_respects_cap(tmp_path):
    """Adopting an empty primary still honours the 1-live-holder cap — if the
    task is already held by a live session, the adopt refuses (no double-bind)."""
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="~")        # unbound dev
    _make_dev_session(cfg, "S-u-w-p2", task_id="T-0001")   # live holder of T-0001
    _make_task(cfg, "T-0001")

    with pytest.raises(ActionError, match="capacity reached"):
        bind_task(cfg, "test-project", "S-u-w-p1", "T-0001")


def test_bind_task_rejects_unknown_task(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_task(cfg, "T-0001")

    with pytest.raises(ActionError, match="task not found"):
        bind_task(cfg, "test-project", "S-u-w-p1", "T-9999")


def test_bind_task_rejects_already_bound_elsewhere(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    # Second dev already owns T-0002 as its primary.
    _make_dev_session(cfg, "S-u-w-p2", task_id="T-0002")
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")

    with pytest.raises(ActionError, match="already bound"):
        bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")


def test_bind_task_rejects_already_bound_via_extras(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_dev_session(cfg, "S-u-w-p2", task_id="T-0003")
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")
    _make_task(cfg, "T-0003")
    # Now bind T-0002 to the second dev as an extra.
    bind_task(cfg, "test-project", "S-u-w-p2", "T-0002")

    with pytest.raises(ActionError, match="already bound"):
        bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")


# --- T-0237 Layer-1: strict binding cap = 1 LIVE session per task ---

def test_bind_task_allows_rebind_when_prior_holder_suspended(tmp_path):
    """T-0237 cap=1 counts only LIVE holders. A suspended prior holder (a
    crashed/abandoned one-time run) is a historical record and must NOT block
    binding the task to a fresh live session — otherwise the task is stuck."""
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")  # live binder
    dead = _make_dev_session(cfg, "S-u-w-p2", task_id="T-0002")  # prior holder
    m = _read_session_metadata(dead)
    m["status"] = "suspended"
    _write_session_metadata(dead, m)
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")

    res = bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")
    assert res["ok"] is True
    assert res["extras"] == ["T-0002"]


def test_bind_task_allows_rebind_when_prior_holder_archived(tmp_path):
    """An archived holder never counts toward the cap, even if its status row
    is somehow still active — the archived flag marks it as history."""
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    dead = _make_dev_session(cfg, "S-u-w-p2", task_id="T-0002")
    m = _read_session_metadata(dead)
    m["archived"] = "true"  # status left 'active' on purpose
    _write_session_metadata(dead, m)
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")

    res = bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")
    assert res["ok"] is True


def test_bind_task_capacity_reached_names_live_holder(tmp_path):
    """S4: a LIVE holder still blocks at cap=1, and the rejection is a clear
    capacity-reached state that names the holder (not a silent drop) so the
    task can be surfaced as pending."""
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_dev_session(cfg, "S-u-w-p2", task_id="T-0002")  # live (active) holder
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")

    with pytest.raises(ActionError, match="capacity reached.*S-u-w-p2"):
        bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")


def test_bind_task_idempotent_for_same_session(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")
    bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")

    # Second bind of same task to same session — idempotent, no error.
    result = bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")
    assert result["ok"] is True
    assert result.get("already_bound") is True
    assert result["extras"] == ["T-0002"]


def test_bind_task_rejects_unknown_session(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_task(cfg, "T-0001")

    with pytest.raises(ActionError, match="no session metadata"):
        bind_task(cfg, "test-project", "S-nope-p9", "T-0001")


# ---------------------------------------------------------------------------
# bind_initiative
# ---------------------------------------------------------------------------

def test_bind_initiative_appends_to_extras(tmp_path):
    cfg = _make_cfg(tmp_path)
    sess_md = _make_tl_session(cfg, "S-u-tl-p0", initiative="v0.7-news.md")
    _make_initiative(cfg, "v0.7-news.md")
    _make_initiative(cfg, "v0.8-foo.md")

    result = bind_initiative(cfg, "test-project", "S-u-tl-p0", "v0.8-foo.md")
    assert result["ok"] is True
    assert result["initiative"] == "v0.8-foo.md"
    assert result["extras"] == ["v0.8-foo.md"]

    meta = _read_session_metadata(sess_md)
    assert meta["initiative"] == "v0.7-news.md"
    assert meta["extra_initiatives"] == ["v0.8-foo.md"]


def test_bind_initiative_sends_peer_notification(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_tl_session(cfg, "S-u-tl-p0", initiative="v0.7-news.md")
    _make_initiative(cfg, "v0.7-news.md")
    _make_initiative(cfg, "v0.8-foo.md")

    bind_initiative(cfg, "test-project", "S-u-tl-p0", "v0.8-foo.md")

    inbox = cfg.data_dir / "test-project" / "_chat" / "inbox-S-u-tl-p0.log"
    assert inbox.exists()
    text = inbox.read_text()
    assert "[BIND_INITIATIVE from stakeholder]" in text
    assert "v0.8-foo.md" in text


def test_bind_initiative_rejects_dev_session(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_initiative(cfg, "v0.8-foo.md")

    with pytest.raises(ActionError, match="dev session, not a teamlead"):
        bind_initiative(cfg, "test-project", "S-u-w-p1", "v0.8-foo.md")


def test_bind_initiative_rejects_unknown_initiative(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_tl_session(cfg, "S-u-tl-p0")

    with pytest.raises(ActionError, match="initiative not found"):
        bind_initiative(cfg, "test-project", "S-u-tl-p0", "no-such.md")


def test_bind_initiative_rejects_invalid_name(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_tl_session(cfg, "S-u-tl-p0")
    _make_initiative(cfg, "x.md")

    with pytest.raises(ActionError, match="invalid initiative"):
        bind_initiative(cfg, "test-project", "S-u-tl-p0", "../etc/passwd.md")
    with pytest.raises(ActionError, match="invalid initiative"):
        bind_initiative(cfg, "test-project", "S-u-tl-p0", "subdir/x.md")
    with pytest.raises(ActionError, match="invalid initiative"):
        bind_initiative(cfg, "test-project", "S-u-tl-p0", "notamd")


def test_bind_initiative_rejects_already_bound_elsewhere(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_tl_session(cfg, "S-u-tl-p0", initiative="v0.7-news.md")
    _make_tl_session(cfg, "S-u-tl-p1", initiative="v0.8-foo.md")
    _make_initiative(cfg, "v0.7-news.md")
    _make_initiative(cfg, "v0.8-foo.md")

    with pytest.raises(ActionError, match="already bound"):
        bind_initiative(cfg, "test-project", "S-u-tl-p0", "v0.8-foo.md")


def test_bind_initiative_idempotent_for_same_session(tmp_path):
    cfg = _make_cfg(tmp_path)
    _make_tl_session(cfg, "S-u-tl-p0", initiative="v0.7-news.md")
    _make_initiative(cfg, "v0.7-news.md")
    _make_initiative(cfg, "v0.8-foo.md")
    bind_initiative(cfg, "test-project", "S-u-tl-p0", "v0.8-foo.md")

    result = bind_initiative(cfg, "test-project", "S-u-tl-p0", "v0.8-foo.md")
    assert result["ok"] is True
    assert result.get("already_bound") is True


# ---------------------------------------------------------------------------
# list_sessions surfaces extras
# ---------------------------------------------------------------------------

def test_list_sessions_returns_extras(tmp_path, monkeypatch):
    import subprocess

    from bot_squad_worker import sessions as S

    cfg = _make_cfg(tmp_path)
    repo = Path(next(iter(cfg.projects.values())).repo_path)
    _make_dev_session(cfg, "S-u-w-p1", task_id="T-0001")
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")
    # Update session md to point to the repo cwd so list_sessions's pane match
    # logic finds it.
    md = cfg.data_dir / "test-project" / "sessions" / "S-u-w-p1.md"
    meta = _read_session_metadata(md)
    meta["cwd"] = str(repo)
    _write_session_metadata(md, meta)
    bind_task(cfg, "test-project", "S-u-w-p1", "T-0002")

    fake_pane_output = f"%1|w|1111|{repo}|claude\n"
    monkeypatch.setattr(
        S, "_run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        if "list-panes" in args
        else subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = S.list_sessions(cfg, "test-project")
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "T-0001"
    assert row["extra_task_ids"] == ["T-0002"]
    assert row["extra_initiatives"] == []


# ---------------------------------------------------------------------------
# T-0157: multi-user claim lock — first-to-claim wins; no double-bind.
# ---------------------------------------------------------------------------

def test_bind_task_concurrent_claim_single_winner(tmp_path):
    """Two sessions racing to bind the SAME task: exactly one wins (flock)."""
    import threading

    cfg = _make_cfg(tmp_path)
    # Two dev sessions, each with its own primary task, both try to ALSO claim T-9.
    _make_dev_session(cfg, "S-alice-w-p1", task_id="T-0001")
    _make_dev_session(cfg, "S-bob-w-p2", task_id="T-0002")
    _make_task(cfg, "T-0001")
    _make_task(cfg, "T-0002")
    _make_task(cfg, "T-0009")

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def claim(sid: str):
        barrier.wait()  # maximise the race window
        try:
            results[sid] = bind_task(cfg, "test-project", sid, "T-0009")
        except ActionError as e:
            results[sid] = e

    threads = [
        threading.Thread(target=claim, args=("S-alice-w-p1",)),
        threading.Thread(target=claim, args=("S-bob-w-p2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [sid for sid, r in results.items() if not isinstance(r, ActionError)]
    errs = [sid for sid, r in results.items() if isinstance(r, ActionError)]
    assert len(oks) == 1, f"expected exactly one winner, got {results}"
    assert len(errs) == 1
    assert "already bound" in str(results[errs[0]])

    # The task is bound to exactly one session on disk.
    winners = []
    for sid in ("S-alice-w-p1", "S-bob-w-p2"):
        meta = _read_session_metadata(
            cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
        )
        if "T-0009" in (meta.get("extra_task_ids") or []):
            winners.append(sid)
    assert winners == oks, f"on-disk owner {winners} != winner {oks}"
