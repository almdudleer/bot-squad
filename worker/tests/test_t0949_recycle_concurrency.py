"""T-0949 — three concurrent scheduler jobs drive ONE session's pane.

Provenance: the fable ``lifecycle-loop-audit`` workflow (wf_9db98295-3b9,
2026-08-31), three findings, each confirmed by an adversarial verifier that
read the code:

1. [major] ``_write_session_metadata`` is a whole-file rewrite from an
   in-memory dict with no cross-writer lock. ``idle_timeout_tick``,
   ``graceful_exit_tick`` and ``telemetry_tick`` are separate 60s apscheduler
   jobs on a 30-thread pool (``max_instances=1`` guards each only against
   ITSELF), each reading the md at tick start and writing a mutated copy
   later — so the last writer resurrects its stale snapshot of every other
   machine's fields. Field separation does not protect them.
2. [major] The ceiling recycler keeps its in-flight state in the TELEMETRY
   record, idle_timeout in ``idle_recycle_phase``, graceful_exit in
   ``exit_handoff_phase``; none reads the others, and ``maybe_compact`` read
   no task status at all — so the ceiling armed a checkpoint + ``/compact`` on
   a dev that had already delivered into ``to_accept`` and was inside
   graceful_exit's quiet grace (and each injection pushed that grace out).
3. [minor] The ``compact_stay_*`` anti-loop guard is stamped at FINALIZE, so
   it is a cross-TICK guard only: within one 60s window both producers pass
   the phase check before either writes it, and both send.

The three fixes under test: the session-md lock + merge-on-write
(:func:`sessions.session_md_lock` / :class:`sessions.SessionMeta`), the
non-blocking per-session :func:`sessions.recycle_lease` every recycler takes
for its decide→act→write pass, and the cross-machine arbitration in
:func:`recycle_gate.other_recycler` + :func:`autocompact.bound_task_done`.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import autocompact as A
from bot_squad_worker import graceful_exit as GE
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import mdlock
from bot_squad_worker import recycle_gate as RG
from bot_squad_worker import sessions as S


# =========================================================================
# 1. session-md read-modify-write: the lost update
# =========================================================================

def _md(tmp_path: Path, **fields) -> Path:
    p = tmp_path / "S-almdudleer-dev-p5.md"
    base = {"sid": "S-almdudleer-dev-p5", "status": "active"}
    base.update(fields)
    S._write_session_metadata(p, base)
    return p


def test_a_stale_snapshot_write_no_longer_erases_a_peers_field(tmp_path):
    """THE finding, in its smallest form: graceful_exit arms
    ``exit_handoff_phase`` after idle_timeout read the same md but before
    idle_timeout writes its copy. Pre-fix the second write resurrected a
    snapshot without the arm and the exit handoff was silently disarmed — so
    the next tick re-injected the prompt into a pane already answering the
    first one, and the arm-time digest mark was lost with it."""
    p = _md(tmp_path)
    idle_view = S._read_session_metadata(p)        # idle_timeout's tick-start read
    exit_view = S._read_session_metadata(p)        # graceful_exit's tick-start read

    exit_view["exit_handoff_phase"] = "writing"
    exit_view["exit_handoff_mark"] = "digest-abc"
    S._write_session_metadata(p, exit_view, atomic=True)

    idle_view["dev_nudge_last_at"] = "2026-09-06T10:00:00Z"
    S._write_session_metadata(p, idle_view, atomic=True)   # the late writer

    after = S._read_session_metadata(p)
    assert after["exit_handoff_phase"] == "writing"   # not erased
    assert after["exit_handoff_mark"] == "digest-abc"
    assert after["dev_nudge_last_at"] == "2026-09-06T10:00:00Z"


def test_the_symmetric_case_ceiling_write_does_not_erase_idle_recycle_phase(tmp_path):
    """The other direction named in the finding: telemetry's ``compact_stay``
    write erasing an in-flight ``idle_recycle_phase``, which made idle_timeout
    start a SECOND handoff for a session already finalizing one."""
    p = _md(tmp_path)
    ceiling_view = S._read_session_metadata(p)
    idle_view = S._read_session_metadata(p)

    idle_view["idle_recycle_phase"] = "finalizing"
    S._write_session_metadata(p, idle_view, atomic=True)

    ceiling_view["compact_stay_phase"] = "handoff"
    S._write_session_metadata(p, ceiling_view, atomic=True)

    after = S._read_session_metadata(p)
    assert after["idle_recycle_phase"] == "finalizing"
    assert after["compact_stay_phase"] == "handoff"


def test_a_reader_still_removes_the_fields_it_meant_to_remove(tmp_path):
    """Merge-on-write applies DELETIONS too — ``_clear_recycle_state`` pops its
    fields, and that must still reach disk (otherwise an armed phase would be
    immortal)."""
    p = _md(tmp_path, idle_recycle_phase="finalizing",
            idle_recycle_armed_at="2026-09-06T09:00:00Z")
    meta = S._read_session_metadata(p)
    IT._clear_recycle_state(meta)
    S._write_session_metadata(p, meta, atomic=True)
    after = S._read_session_metadata(p)
    assert "idle_recycle_phase" not in after
    assert "idle_recycle_armed_at" not in after


def test_a_deletion_still_wins_over_a_concurrent_unrelated_write(tmp_path):
    p = _md(tmp_path, idle_recycle_phase="finalizing")
    mine = S._read_session_metadata(p)
    peer = S._read_session_metadata(p)
    peer["exit_handoff_phase"] = "writing"
    S._write_session_metadata(p, peer, atomic=True)
    IT._clear_recycle_state(mine)
    S._write_session_metadata(p, mine, atomic=True)
    after = S._read_session_metadata(p)
    assert "idle_recycle_phase" not in after      # my deletion landed
    assert after["exit_handoff_phase"] == "writing"  # the peer's arm survived


def test_a_rebuilt_md_is_still_a_whole_file_write(tmp_path):
    """A caller that REBUILDS the md from a fresh dict (``sessions.suspend``
    does exactly this) means the whole file, not a merge — otherwise a
    suspend could not drop a field. Only a dict that came from
    ``_read_session_metadata`` carries a baseline to merge against."""
    p = _md(tmp_path, idle_recycle_phase="finalizing")
    S._write_session_metadata(p, {"sid": "S-almdudleer-dev-p5",
                                  "status": "suspended"}, atomic=True)
    after = S._read_session_metadata(p)
    assert after == {"sid": "S-almdudleer-dev-p5", "status": "suspended"}


def test_a_write_to_a_different_path_is_never_merged(tmp_path):
    """The rename paths read one md and write another. A merge there would be
    against an unrelated file, so the source path is pinned in the snapshot."""
    src = _md(tmp_path, task_id="T-0001")
    meta = S._read_session_metadata(src)
    dst = tmp_path / "S-almdudleer-dev-p9.md"
    S._write_session_metadata(dst, {"sid": "S-almdudleer-dev-p9",
                                    "status": "active",
                                    "stale_field": "x"})
    meta["sid"] = "S-almdudleer-dev-p9"
    S._write_session_metadata(dst, meta, atomic=True)
    after = S._read_session_metadata(dst)
    assert after["task_id"] == "T-0001"
    assert "stale_field" not in after     # whole-file, not a merge


def test_the_writer_keeps_working_from_what_is_now_on_disk(tmp_path):
    """After a write the caller's dict is what the FILE holds — including a
    field a concurrent machine added. Its next mutation therefore diffs against
    reality, not against a snapshot two writes old."""
    p = _md(tmp_path)
    mine = S._read_session_metadata(p)
    peer = S._read_session_metadata(p)
    peer["exit_handoff_phase"] = "writing"
    S._write_session_metadata(p, peer, atomic=True)
    mine["compact_stay_phase"] = "handoff"
    S._write_session_metadata(p, mine, atomic=True)
    assert mine["exit_handoff_phase"] == "writing"


def test_concurrent_writers_lose_no_updates(tmp_path):
    """The lock, under real thread contention: 6 threads x 25 read-modify-write
    passes each on ONE md. Every field must survive."""
    p = _md(tmp_path)
    workers, rounds = 5, 6
    start = threading.Barrier(workers)

    def run(n: int) -> None:
        start.wait()
        for i in range(rounds):
            meta = S._read_session_metadata(p)
            meta[f"w{n}"] = str(i)
            S._write_session_metadata(p, meta, atomic=True)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    after = S._read_session_metadata(p)
    assert after is not None
    for n in range(workers):
        assert after.get(f"w{n}") == str(rounds - 1), f"lost writer {n}"


def test_the_write_tmp_is_unique_per_writer(tmp_path, monkeypatch):
    """The old atomic path used ONE shared ``<name>.tmp`` per md — two writers
    racing on the same session clobbered each other's tmp (the class
    ``mdlock.atomic_write`` was written for on the task mds)."""
    seen: list[str] = []
    real = mdlock.tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = real(*a, **kw)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(mdlock.tempfile, "mkstemp", spy)
    p = _md(tmp_path)
    S._write_session_metadata(p, S._read_session_metadata(p), atomic=True)
    S._write_session_metadata(p, S._read_session_metadata(p), atomic=True)
    assert len(seen) >= 2 and len(set(seen)) == len(seen)
    assert not (p.parent / (p.name + ".tmp")).exists()


def test_the_session_md_lock_uses_the_shared_lockfile_convention(tmp_path):
    """Same ``<file>.lock`` name :mod:`mdlock` (and the API's markdown_writer,
    and now the SessionStart hook) flock — a convention that only excludes
    anything if everyone spells it the same way."""
    p = _md(tmp_path)
    S._write_session_metadata(p, S._read_session_metadata(p))
    assert (p.parent / (p.name + mdlock.LOCK_SUFFIX)).exists()


def test_the_lock_is_reentrant_within_one_thread(tmp_path):
    """``fcntl.flock`` is per open-file-description: a nested acquire from the
    same thread on a NEW fd would deadlock against itself."""
    p = _md(tmp_path)
    with S.session_md_lock(p):
        with S.session_md_lock(p):
            S._write_session_metadata(p, S._read_session_metadata(p))
    assert S._read_session_metadata(p)["sid"] == "S-almdudleer-dev-p5"


# =========================================================================
# 2. the per-session recycle lease
# =========================================================================

def test_lease_is_exclusive_across_threads(tmp_path):
    p = _md(tmp_path)
    got: list[bool] = []
    entered = threading.Event()

    def other():
        with S.recycle_lease(p) as leased:
            got.append(leased)

    with S.recycle_lease(p) as mine:
        assert mine is True
        entered.set()
        t = threading.Thread(target=other)
        t.start()
        t.join(timeout=10)
    assert got == [False]


def test_lease_is_released_when_the_block_ends(tmp_path):
    p = _md(tmp_path)
    with S.recycle_lease(p) as first:
        assert first is True
    got: list[bool] = []
    t = threading.Thread(target=lambda: got.append(
        S.recycle_lease(p).__enter__()))
    t.start()
    t.join(timeout=10)
    assert got == [True]


def test_lease_is_per_session_not_global(tmp_path):
    a = _md(tmp_path)
    b = tmp_path / "S-almdudleer-dev-p6.md"
    S._write_session_metadata(b, {"sid": "S-almdudleer-dev-p6"})
    got: list[bool] = []
    with S.recycle_lease(a):
        t = threading.Thread(target=lambda: got.append(
            _take_lease_in_thread(b)))
        t.start()
        t.join(timeout=10)
    assert got == [True]


def _take_lease_in_thread(p: Path) -> bool:
    with S.recycle_lease(p) as leased:
        return leased


def test_lease_is_reentrant_within_one_thread(tmp_path):
    p = _md(tmp_path)
    with S.recycle_lease(p) as outer:
        with S.recycle_lease(p) as inner:
            assert (outer, inner) == (True, True)


def test_lease_excludes_another_PROCESS(tmp_path):
    """The lease has to be an flock, not a threading.Lock: the SessionStart
    hook and any second worker are other PROCESSES."""
    p = _md(tmp_path)
    held = tmp_path / "held"
    code = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n" % str(Path(__file__).resolve().parents[1]) +
        "from bot_squad_worker import sessions as S\n"
        "from pathlib import Path\n"
        "with S.recycle_lease(Path(%r)) as ok:\n" % str(p) +
        "    Path(%r).write_text('1' if ok else '0')\n" % str(held) +
        "    time.sleep(3)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.time() + 10
        while not held.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert held.read_text() == "1", "the child never took the lease"
        with S.recycle_lease(p) as leased:
            assert leased is False
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # ...and it is free again once that process is gone
    with S.recycle_lease(p) as leased:
        assert leased is True


# =========================================================================
# 3. cross-machine arbitration (pure)
# =========================================================================

def test_other_recycler_names_the_machine_in_flight():
    assert RG.other_recycler({"exit_handoff_phase": "writing"},
                             own=(RG.MACHINE_IDLE_RECYCLE,
                                  RG.MACHINE_COMPACT_STAY)) == RG.MACHINE_EXIT_HANDOFF
    assert RG.other_recycler({"idle_recycle_phase": "finalizing"},
                             own=(RG.MACHINE_EXIT_HANDOFF,)) == RG.MACHINE_IDLE_RECYCLE
    assert RG.other_recycler({}, own=(), ceiling_phase="writing") == RG.MACHINE_CEILING


def test_a_machines_own_in_flight_state_never_blocks_it():
    """Or it could never finalize what it armed — the wedge this arbitration
    must not create."""
    assert RG.other_recycler({"exit_handoff_phase": "writing"},
                             own=(RG.MACHINE_EXIT_HANDOFF,)) is None
    assert RG.other_recycler({"compact_stay_phase": "handoff"},
                             own=(RG.MACHINE_IDLE_RECYCLE,
                                  RG.MACHINE_COMPACT_STAY)) is None


def test_unset_and_tilde_are_not_in_flight():
    assert RG.inflight_machines({"idle_recycle_phase": "~",
                                 "exit_handoff_phase": ""}) == ()
    assert RG.other_recycler(None, own=()) is None


# =========================================================================
# 4. the ceiling on a DONE session (Interleaving B) + the sibling gates
# =========================================================================

def _make_cfg(tmp_path: Path, *, sid: str, task_id: str | None,
              task_status: str = "in_progress", md_extra: dict | None = None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    fm = {"sid": sid, "status": "active", "window": "T-0042",
          "cwd": str(repo), "claude_uuid": "uuid-" + sid}
    if task_id:
        fm["task_id"] = task_id
    fm.update(md_extra or {})
    S._write_session_metadata(sess / f"{sid}.md", fm)
    if task_id:
        backlog = data_dir / "bot-squad" / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        (backlog / f"{task_id}-demo.md").write_text(
            f"---\nid: {task_id}\ntitle: Demo\nstatus: {task_status}\n---\n\nbody\n")
    cfg = types.SimpleNamespace(projects={"bot-squad": object()},
                                data_dir=data_dir)
    return cfg, data_dir


@pytest.fixture
def ceiling(monkeypatch):
    """Stub every pane/tmux seam the ceiling touches."""
    calls = {"ctx_handoff": [], "handoff": [], "compact": []}
    state = {"pane": "%9", "buf": "❯ \n"}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: calls["compact"].append(sid))
    monkeypatch.setattr(
        A, "_inject_context_handoff",
        lambda sid, task_id, *, relaunch=True, stay=False, resume=False:
        calls["ctx_handoff"].append((sid, task_id, relaunch)))
    monkeypatch.setattr(
        A, "_inject_handoff",
        lambda sid, path, role=None, *, relaunch=True, stay=False, resume=False:
        calls["handoff"].append((sid, path)))
    monkeypatch.setattr(RG, "is_attached", lambda target, **kw: False)
    monkeypatch.setattr(A.recycle_gate, "is_attached", lambda target, **kw: False)
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "bot-squad")
    monkeypatch.delenv("BOT_SQUAD_AUTOCOMPACT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_COMPACT_MODE", raising=False)
    return {"calls": calls, "state": state}


def _rec(sid: str, task_id: str | None = "T-0042", role: str = "dev") -> dict:
    return {"sid": sid, "activity": "idle", "alert_fired_at": {}, "role": role,
            "task_id": task_id, "window": "T-0042"}


SID = "S-almdudleer-T-0042-p5"


def test_the_ceiling_arms_on_a_session_whose_work_is_NOT_done(tmp_path, ceiling):
    """POSITIVE CONTROL for the two gates below — without it a blanket
    "return False" would pass every test in this section."""
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="in_progress")
    rec = _rec(SID)
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert ceiling["calls"]["ctx_handoff"] == [(SID, "T-0042", True)]
    assert rec["compact"]["phase"] == "writing"


@pytest.mark.parametrize("status", ["to_accept", "totest", "closed"])
def test_the_ceiling_never_fires_on_a_done_session(tmp_path, ceiling, status):
    """Interleaving B, the confirmed one: a dev delivers into ``to_accept`` and
    sits in graceful_exit's 180s quiet grace; the task-status-blind ceiling
    armed a checkpoint + native /compact on it, each injection resetting the
    jsonl mtime that grace is measured from, and the session was suspended
    kill-not-resume minutes later. A full summarization spent on a session
    nothing resumes."""
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042", task_status=status)
    rec = _rec(SID)
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is False
    assert ceiling["calls"] == {"ctx_handoff": [], "handoff": [], "compact": []}
    assert not rec.get("compact")


def test_a_ceiling_handoff_already_armed_is_abandoned_when_the_work_lands(
        tmp_path, ceiling):
    """The mid-flight case: the dev delivered AFTER the checkpoint was armed.
    Finishing it would relaunch a fresh incarnation of a session whose work is
    done; the forward-state it was asked for is on the ticket either way."""
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042", task_status="to_accept")
    rec = _rec(SID)
    rec["compact"] = {"phase": "writing", "kind": "context", "armed_at": 900.0,
                      "task_id": "T-0042"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is False
    assert rec["compact"] == {}
    assert ceiling["calls"]["compact"] == []


def test_a_partly_delivered_session_is_still_the_ceilings(tmp_path, ceiling):
    """One of two bundled tickets terminal is not "done" — the session is still
    working, so the ceiling still owns it."""
    cfg, data = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                          task_status="to_accept",
                          md_extra={"extra_task_ids": ["T-0043"]})
    (data / "bot-squad" / "backlog" / "T-0043-demo.md").write_text(
        "---\nid: T-0043\ntitle: Demo2\nstatus: in_progress\n---\n\nbody\n")
    rec = _rec(SID)
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert ceiling["calls"]["ctx_handoff"] == [(SID, "T-0042", True)]


def test_the_ceiling_defers_while_graceful_exit_has_a_handoff_in_flight(
        tmp_path, ceiling):
    """Interleaving A's mechanism: two machines injecting contradictory asks
    ("this SAME session continues" vs "this incarnation is ending") into one
    pane in one window."""
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="in_progress",
                       md_extra={"exit_handoff_phase": "writing"})
    rec = _rec(SID)
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is False
    assert ceiling["calls"]["ctx_handoff"] == []


def test_the_ceiling_defers_while_idle_timeout_is_recycling(tmp_path, ceiling):
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="in_progress",
                       md_extra={"idle_recycle_phase": "finalizing"})
    rec = _rec(SID)
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is False
    assert ceiling["calls"]["ctx_handoff"] == []


def test_the_ceiling_still_finalizes_its_OWN_armed_handoff(tmp_path, ceiling,
                                                           monkeypatch):
    """The gate must not wedge the machine that armed the sequence: a sibling's
    phase blocks a NEW sequence, never a finalize."""
    cfg, data = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                          task_status="in_progress",
                          md_extra={"exit_handoff_phase": "writing"})
    finalized: list[str] = []
    monkeypatch.setattr(A, "_maybe_finalize",
                        lambda cfg_, slug, rec_, compact, now:
                        finalized.append(rec_["sid"]) or True)
    rec = _rec(SID)
    rec["compact"] = {"phase": "writing", "kind": "context", "armed_at": 900.0,
                      "task_id": "T-0042"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert finalized == [SID]


def test_ceiling_phase_is_readable_by_the_sibling_machines(tmp_path):
    """The ceiling's in-flight marker lives in the TELEMETRY record, which is
    why the md-only readers could not see it. This is the read they were
    missing."""
    from bot_squad_worker import telemetry
    cfg, data = _make_cfg(tmp_path, sid=SID, task_id="T-0042")
    assert A.ceiling_phase(cfg, "bot-squad", SID) == ""
    path = telemetry._record_path(cfg, "bot-squad", SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    telemetry._write_json(path, {"sid": SID, "compact": {"phase": "writing"}})
    assert A.ceiling_phase(cfg, "bot-squad", SID) == "writing"


# =========================================================================
# 5. idle_timeout / graceful_exit defer to a sibling mid-sequence
# =========================================================================

def _row(sid: str, repo: Path, *, task_id="T-0042", role="dev") -> dict:
    return {"sid": sid, "status": "active", "window": "T-0042",
            "task_id": task_id, "role": role, "cwd": str(repo),
            "claude_uuid": "uuid-" + sid, "linux_user": "", "initiative": None}


@pytest.fixture
def lifecycle(monkeypatch):
    """Seams for idle_timeout + graceful_exit."""
    calls = {"ctx_handoff": [], "compact": [], "suspend": [], "nudge": []}
    state = {"pane": "%9", "buf": "❯ \n", "idle_age": 100000.0}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: calls["compact"].append(sid))
    monkeypatch.setattr(
        A, "_inject_context_handoff",
        lambda sid, task_id, *, relaunch=True, stay=False, resume=False:
        calls["ctx_handoff"].append((sid, task_id)))
    monkeypatch.setattr(GE, "_suspend",
                        lambda cfg, slug, sid: calls["suspend"].append(sid))
    monkeypatch.setattr(IT, "_send_dev_nudge",
                        lambda sid, text: calls["nudge"].append(sid))
    monkeypatch.setattr(RG, "is_attached", lambda target, **kw: False)
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - state["idle_age"])
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "bot-squad")
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_GRACEFUL_EXIT", raising=False)
    monkeypatch.setenv("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC", "180")
    return {"calls": calls, "state": state}


def test_idle_timeout_defers_while_graceful_exit_is_mid_handoff(tmp_path,
                                                                lifecycle):
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="in_progress",
                       md_extra={"exit_handoff_phase": "writing"})
    row = _row(SID, tmp_path / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert lifecycle["calls"]["ctx_handoff"] == []
    assert lifecycle["calls"]["nudge"] == []


def test_idle_timeout_defers_while_the_ceiling_is_mid_handoff(tmp_path,
                                                              lifecycle):
    from bot_squad_worker import telemetry
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="in_progress")
    path = telemetry._record_path(cfg, "bot-squad", SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    telemetry._write_json(path, {"sid": SID, "compact": {"phase": "writing"}})
    row = _row(SID, tmp_path / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert lifecycle["calls"]["ctx_handoff"] == []


def test_idle_timeout_still_acts_when_nothing_else_is_in_flight(tmp_path,
                                                                 lifecycle):
    """POSITIVE CONTROL for the two above — same session, same clock, nothing
    in flight: the tick acts (a dev still holding live work is nudged at the
    window, T-0945's PLAN_NUDGE). Without this a blanket ``return False``
    would satisfy every deferral test in this section."""
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="in_progress")
    row = _row(SID, tmp_path / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert lifecycle["calls"]["nudge"] == [SID]


def test_graceful_exit_defers_while_idle_timeout_is_mid_recycle(tmp_path,
                                                                lifecycle):
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="to_accept",
                       md_extra={"idle_recycle_phase": "finalizing"})
    row = _row(SID, tmp_path / "repo")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=time.time(),
                         user_home="/home/x") is False
    assert lifecycle["calls"]["suspend"] == []
    assert lifecycle["calls"]["ctx_handoff"] == []


def test_graceful_exit_still_exits_when_nothing_else_is_in_flight(tmp_path,
                                                                  lifecycle):
    """POSITIVE CONTROL: two ticks — arm the pre-exit handoff, then exit."""
    cfg, data = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                          task_status="to_accept")
    row = _row(SID, tmp_path / "repo")
    now = time.time()
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now,
                         user_home="/home/x") is True
    assert lifecycle["calls"]["ctx_handoff"] == [(SID, "T-0042")]
    md = data / "bot-squad" / "backlog" / "T-0042-demo.md"
    md.write_text(md.read_text() + "\n## Context\n\nwhat is true now\n")
    assert GE.maybe_exit(cfg, "bot-squad", row, now=now + 1,
                         user_home="/home/x") is True
    assert lifecycle["calls"]["suspend"] == [SID]


# =========================================================================
# 6. the lease, end to end: two recyclers in the same 60s window
# =========================================================================

def test_two_recyclers_in_one_window_produce_ONE_injection(tmp_path, lifecycle):
    """The TOCTOU in its real shape: idle_timeout and the telemetry tick both
    capture the pane, both see a free composer, and both inject — because each
    writes its phase only AFTER the send returns. The lease makes the window
    belong to one of them; the loser defers to its next tick.

    The inject seam blocks while held so the two really do overlap in time.
    """
    cfg, _ = _make_cfg(tmp_path, sid=SID, task_id="T-0042",
                       task_status="in_progress")
    in_inject = threading.Event()
    release = threading.Event()
    injected: list[str] = []

    def slow_inject(sid, text=None, *a, **kw):
        injected.append(sid)
        in_inject.set()
        release.wait(timeout=10)

    lifecycle_patch = pytest.MonkeyPatch()
    lifecycle_patch.setattr(IT, "_send_dev_nudge", slow_inject)
    lifecycle_patch.setattr(A, "_inject_context_handoff", slow_inject)
    try:
        row = _row(SID, tmp_path / "repo")
        results: dict[str, object] = {}

        def idle():
            results["idle"] = IT.maybe_recycle(cfg, "bot-squad", row,
                                               now=time.time(),
                                               user_home="/home/x")

        t = threading.Thread(target=idle)
        t.start()
        assert in_inject.wait(timeout=10), "idle_timeout never reached the inject"
        # ...the telemetry tick fires while that injection is still in flight
        rec = _rec(SID)
        assert A.maybe_compact(cfg, "bot-squad", rec, "urgent",
                               now=time.time()) is False
        assert not rec.get("compact")
        release.set()
        t.join(timeout=10)
        assert results["idle"] is True
        assert injected == [SID]
    finally:
        release.set()
        lifecycle_patch.undo()
