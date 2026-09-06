"""T-0942 — the project's ONE work-state doc, writable by any role-holder.

WHAT THESE TESTS ARE PINNING, and in what order of importance. The stakeholder's
ask was one doc with a concurrency lock; the measured failure was that NOBODY
WROTE THE DOC for five weeks across 38 operator spawns. Those are different
problems and the lock only addresses the first, so the suite is ordered by which
mechanism is load-bearing:

  1. ROUTING (`assignment.role_artifact`) — the non-volitional half. A compact
     happens whether or not a session chooses to keep the doc current, so
     sending operator AND user-conversation compacts to the same file is what
     actually closes the gap. This is also where the bug WAS: the seat-holder
     was a `user-conversation` session, so its faithful compact landed in
     `role-user-conversation-<window>.md` while the seat's own doc rotted.
  2. READ-TIME CLAIM (`autocompact.boot_prompt_from_artifact`) — the other
     non-volitional half, and the only one that helps in the case that actually
     happened, where nothing had been written at all. A successor handed a
     five-week doc must not be told it is "your ONLY memory … continue from
     there". The ABSENCE of that sentence is asserted, not just the presence of
     a warning: a banner bolted onto the old claim leaves two contradicting
     instructions and the reader follows the confident one.
  3. LOCK + CAS (`work_state.write`) — what the stakeholder asked for and what
     keeps two holders from silently clobbering each other now that both write.
  4. The freshness sweep, last, because it is a top-up and is deliberately
     constrained: drift_check is the same "ask repeatedly" design and the fleet
     switched it off on all eight lanes on 2026-09-06.

The staleness fixture is REAL — `fixtures/work_state/operator-state-2026-07-31-p533.md`
is the verbatim head of the document `S-almdudleer-operator-p640` actually
booted from on 2026-09-06. See that directory's README for why it must not be
"fixed" to match the schema.
"""
from __future__ import annotations

import multiprocessing as mp
import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import assignment, autocompact, work_state

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "work_state"
STALE_FIXTURE = FIXTURES / "operator-state-2026-07-31-p533.md"


def _project(tmp_path: Path, slug: str = "proj") -> Path:
    for sub in ("artifacts", "backlog", "sessions"):
        (tmp_path / slug / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _cfg(tmp_path: Path, slug: str = "proj"):
    return types.SimpleNamespace(data_dir=tmp_path, projects={slug: object()})


# ---------------------------------------------------------------------------
# 1. Routing — the load-bearing half
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["operator", "user-conversation"])
def test_project_roles_compact_into_the_one_work_state_doc(tmp_path, role):
    art = assignment.role_artifact(tmp_path, "proj", role=role,
                                   sid="S-u-window-p1", task_id=None)
    assert art is not None
    assert art.path == tmp_path / "proj" / "artifacts" / "work-state.md"


def test_user_conversation_no_longer_gets_its_own_role_file():
    """The regression, stated as the file that must NOT be produced.

    This is the defect verbatim: on the live install
    `artifacts/role-user-conversation-S-…-user-conversation.md` was dated
    2026-09-04 and `artifacts/operator-state.md` 2026-07-31. The session holding
    the operator seat was writing — into the file this assertion forbids."""
    art = assignment.role_artifact(
        "/d", "proj", role="user-conversation",
        sid="S-almdudleer-gu_x-user-conversation-p490", task_id=None)
    assert "role-user-conversation" not in art.path.name
    assert art.path.name == "work-state.md"


@pytest.mark.parametrize("role", ["dev", "qa", "teamlead", "prod-teamlead"])
def test_non_project_roles_keep_their_per_assignment_artifact(tmp_path, role):
    """The widening must stop at the project roles. A task-less dev's
    forward-state is about ITS assignment; full-replacing the project's shared
    work-state with it would be the same conflation in the other direction."""
    art = assignment.role_artifact(tmp_path, "proj", role=role,
                                   sid="S-u-win-p1", task_id=None)
    assert art.path.name == f"role-{role}-S-u-win.md"


def test_task_bound_session_still_hands_off_to_its_ticket(tmp_path):
    """T-0863 is untouched: a task-bound session's destination is its ticket."""
    art = assignment.role_artifact(tmp_path, "proj", role="operator",
                                   sid="S-u-win-p1", task_id="T-0942")
    assert art.path.name == "T-0942.md"


def test_compact_guidance_carries_the_schema_for_every_project_role():
    """A `user-conversation` compact now lands in a SCHEMA'd shared document.
    Sending it there with the generic "write everything down" prompt is how that
    document becomes one session's free-form handover note."""
    # Named literally, NOT iterated off PROJECT_ROLES: a test that reads the
    # same constant the code reads goes green when the constant shrinks back to
    # ("operator",), which is the exact regression this ticket removes.
    for role in ("operator", "user-conversation"):
        g = assignment.role_compact_guidance(role)
        assert "WORK-STATE" in g, role
        for title, _hint in assignment.OPERATOR_STATE_SECTIONS:
            assert title in g, (role, title)
    assert assignment.role_compact_guidance("dev") == ""


# ---------------------------------------------------------------------------
# 2. Read-time claim — the other non-volitional half
# ---------------------------------------------------------------------------

_ONLY_MEMORY = "ONLY memory is the role artifact"
_CONTINUE = "continue the work from there"


def test_boot_prompt_drops_the_only_memory_claim_on_a_stale_doc(tmp_path):
    """The exhibit: the real July doc p640 booted from.

    Asserting the ABSENCE of the old claim is the point. p640 read a body five
    weeks older than its own `updated:` frontmatter line as current fact,
    because the prompt around it said the file was its only memory and to
    continue from there."""
    art = tmp_path / "work-state.md"
    art.write_text(STALE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    text = autocompact.boot_prompt_from_artifact(
        role="operator", assignment_id="operator", artifact_path=str(art))

    assert _ONLY_MEMORY not in text
    assert _CONTINUE not in text
    assert "STALE" in text and "HISTORY" in text
    # names the age and the last writer — the two facts a date alone hid
    assert "S-almdudleer-operator-p533" in text
    assert "2026-07-30T17:00:04Z" in text
    assert "bsq work-state write" in text


def test_the_stale_prompt_names_the_verb_the_reader_can_actually_use(tmp_path):
    """The banner is shown for per-ROLE artifacts too (every task-less boot and
    every crash recovery), where `bsq work-state write` is the wrong verb — that
    session's artifact is its own file and it writes it with `bsq compact-save`.
    A warning ending in a command the reader cannot use teaches them to stop
    reading the warning."""
    body = STALE_FIXTURE.read_text(encoding="utf-8")
    ws = tmp_path / "work-state.md"; ws.write_text(body, encoding="utf-8")
    role = tmp_path / "role-dev-S-x.md"; role.write_text(body, encoding="utf-8")

    t_ws = autocompact.boot_prompt_from_artifact(
        role="operator", assignment_id="operator", artifact_path=str(ws))
    assert "bsq work-state write" in t_ws and "compact-save" not in t_ws

    t_role = autocompact.boot_prompt_from_artifact(
        role="dev", assignment_id="x", artifact_path=str(role))
    assert "compact-save" in t_role and "bsq work-state write" not in t_role


def test_boot_prompt_is_unchanged_for_a_current_doc(tmp_path):
    """The other half of the same guarantee: a fresh doc's prompt must not have
    picked up a warning, or the warning stops meaning anything."""
    art = tmp_path / "work-state.md"
    art.write_text(work_state.compose("state", rev=3, sid="S-a-p1",
                                      role="operator"), encoding="utf-8")
    text = autocompact.boot_prompt_from_artifact(
        role="operator", assignment_id="operator", artifact_path=str(art))
    assert _ONLY_MEMORY in text
    assert _CONTINUE in text
    assert "STALE" not in text


def test_boot_prompt_keeps_the_old_wording_when_the_probe_cannot_read(tmp_path):
    """A broken instrument must not start telling every successor it is stale.

    (A positive control for the direction of the failure, per the rule that an
    absent reading is not a negative one.)"""
    text = autocompact.boot_prompt_from_artifact(
        role="dev", assignment_id="x", artifact_path=str(tmp_path / "gone.md"))
    assert _ONLY_MEMORY in text


def test_staleness_reads_the_real_specimen():
    st = work_state.staleness(STALE_FIXTURE.read_text(encoding="utf-8"),
                              STALE_FIXTURE)
    assert st["stale"] is True
    assert st["stale_reason"] == "age"
    assert st["updated_by"] == "S-almdudleer-operator-p533"
    assert st["rev"] == 0  # a pre-T-0942 doc has no rev, and 0 is the truth
    banner = work_state.staleness_banner(st)
    assert "HISTORY" in banner and "S-almdudleer-operator-p533" in banner


def test_staleness_prefers_frontmatter_over_mtime(tmp_path):
    """A `cp`/rsync of the data dir rewrites every mtime. A doc that looks fresh
    because it was COPIED is exactly the lie the verdict exists to prevent."""
    p = tmp_path / "work-state.md"
    p.write_text(STALE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    # mtime is NOW (just written); the frontmatter says July.
    assert work_state.staleness(p.read_text(), p)["stale"] is True


def test_movement_makes_an_otherwise_fresh_doc_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_WORK_STATE_MOVED_SESSIONS", "8")
    monkeypatch.setenv("BOT_SQUAD_WORK_STATE_MOVEMENT_FLOOR_HOURS", "2")
    text = work_state.compose("s", rev=1, sid="S-a-p1", role="operator",
                              ts=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(time.time() - 3 * 3600)))
    assert work_state.staleness(text)["stale"] is False
    st = work_state.staleness(text, movement={"sessions_since": 9})
    assert st["stale"] is True and st["stale_reason"] == "movement"


def test_movement_cannot_fire_below_the_age_floor(tmp_path, monkeypatch):
    """The calibration this ticket had to make twice. The first cut also counted
    backlog mds touched since, at a bar of 10, and on the live fleet it returned
    STALE for a doc written ELEVEN MINUTES earlier — a ticket md is rewritten by
    every note, so it measures chatter. A warning that fires on an 11-minute-old
    doc teaches its reader to skip the banner."""
    monkeypatch.setenv("BOT_SQUAD_WORK_STATE_MOVED_SESSIONS", "8")
    monkeypatch.setenv("BOT_SQUAD_WORK_STATE_MOVEMENT_FLOOR_HOURS", "2")
    text = work_state.compose("s", rev=1, sid="S-a-p1", role="operator")
    st = work_state.staleness(text, movement={"sessions_since": 500})
    assert st["stale"] is False


def test_project_movement_counts_starts_not_mtimes(tmp_path):
    """`sessions/*.md` is rewritten on every status change, so an mtime scan
    counts every live pane as new seconds after a write."""
    root = _project(tmp_path)
    sess = root / "proj" / "sessions"
    for i in range(3):
        (sess / f"S-old-p{i}.md").write_text(
            "---\nsid: S-old\nstarted_at: 2026-07-01T00:00:00Z\n---\n")
    for i in range(4):
        (sess / f"S-new-p{i}.md").write_text(
            "---\nsid: S-new\nstarted_at: 2999-01-01T00:00:00+00:00\n---\n")
    mv = work_state.project_movement(root, "proj", time.time())
    # every md was just WRITTEN, so an mtime scan would say 7
    assert mv["sessions_since"] == 4


# ---------------------------------------------------------------------------
# 3. The lock + CAS
# ---------------------------------------------------------------------------

def test_write_bumps_rev_and_snapshots_the_previous_file(tmp_path):
    root = _project(tmp_path)
    r1 = work_state.write(root, "proj", "# one\n", sid="S-a-p1", role="operator",
                          base_rev=0)
    assert r1["rev"] == 1 and r1["snapshot"] is None  # nothing to snapshot yet
    r2 = work_state.write(root, "proj", "# two\n", sid="S-b-p1",
                          role="user-conversation", base_rev=1)
    assert r2["rev"] == 2
    assert Path(r2["snapshot"]).read_text().find("# one") != -1


def test_stale_base_rev_is_refused_with_the_current_text(tmp_path):
    root = _project(tmp_path)
    work_state.write(root, "proj", "# one\n", sid="S-a-p1", role="operator",
                     base_rev=0)
    work_state.write(root, "proj", "# two\n", sid="S-b-p1", role="operator",
                     base_rev=1)
    with pytest.raises(work_state.WorkStateConflict) as e:
        work_state.write(root, "proj", "# three\n", sid="S-c-p1",
                         role="user-conversation", base_rev=1)
    assert e.value.actual == 2
    assert "# two" in e.value.content  # merge without a second read


def test_a_blind_write_over_live_state_is_refused(tmp_path):
    root = _project(tmp_path)
    work_state.write(root, "proj", "# one\n", sid="S-a-p1", role="operator",
                     base_rev=0)
    with pytest.raises(work_state.WorkStateConflict):
        work_state.write(root, "proj", "# clobber\n", sid="S-b-p1", role="operator")
    assert "# one" in (work_state.doc_path(root, "proj")).read_text()


def test_the_compact_seam_may_write_blind_but_the_overwrite_is_visible(tmp_path):
    """A finalizing session has no base_rev and its context is cleared the
    moment it answers, so a refusal there does not protect the doc — it drops
    the forward-state at the only moment it can still be written. Accepted,
    snapshotted, and FLAGGED."""
    root = _project(tmp_path)
    work_state.write(root, "proj", "# one\n", sid="S-a-p1", role="operator",
                     base_rev=0)
    res = work_state.write(root, "proj", "# compact\n", sid="S-b-p1",
                           role="user-conversation", allow_blind=True)
    assert res["blind"] is True and res["blind_over"] == "S-a-p1"
    assert Path(res["snapshot"]).read_text().find("# one") != -1


def _racer(root: str, slug: str, sid: str, q):
    from bot_squad_worker import work_state as W
    try:
        q.put(("ok", W.write(root, slug, f"# from {sid}\n", sid=sid,
                             role="operator", base_rev=1)["rev"]))
    except W.WorkStateConflict as exc:
        q.put(("conflict", exc.actual))


def test_two_concurrent_writers_on_the_same_base_rev_do_not_both_land(tmp_path):
    """The stakeholder's «понадобится concurrency lock», exercised across real
    processes rather than asserted about one.

    Both writers read rev 1 and write from separate processes. The flock
    serializes them; the CAS is what makes the loser LOUD instead of silently
    discarded — a lock alone would let the second overwrite the first."""
    root = _project(tmp_path)
    work_state.write(root, "proj", "# base\n", sid="S-seed-p1", role="operator",
                     base_rev=0)
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_racer, args=(str(root), "proj", sid, q))
             for sid in ("S-a-p1", "S-b-p1")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    outcomes = sorted(q.get() for _ in procs)
    assert [o[0] for o in outcomes] == ["conflict", "ok"]
    assert work_state.read(root, "proj")["rev"] == 2


def test_the_lock_is_released_when_a_holder_dies(tmp_path):
    """fcntl flocks are kernel-released, so a writer that dies mid-write can
    never wedge the doc — no TTL, nothing to reap."""
    root = _project(tmp_path)
    ctx = mp.get_context("fork")

    def _die(rootstr):
        import os
        from bot_squad_worker import work_state as W
        fd_ctx = W._flock(rootstr, "proj")
        fd_ctx.__enter__()
        os._exit(9)

    p = ctx.Process(target=_die, args=(str(root),))
    p.start()
    p.join(30)
    # would block forever if the dead process still held it
    res = work_state.write(root, "proj", "# after\n", sid="S-a-p1",
                           role="operator", base_rev=0)
    assert res["rev"] == 1


def test_write_is_atomic_and_leaves_no_stray_tmp(tmp_path):
    root = _project(tmp_path)
    work_state.write(root, "proj", "# one\n", sid="S-a-p1", role="operator",
                     base_rev=0)
    arts = work_state.artifacts_dir(root, "proj")
    assert not list(arts.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 4. Migration + the widened permission
# ---------------------------------------------------------------------------

def test_migrate_adopts_the_operator_state_doc_once(tmp_path):
    root = _project(tmp_path)
    legacy = work_state.legacy_path(root, "proj")
    legacy.write_text(STALE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    r = work_state.migrate(root, "proj")
    assert r["migrated"] is True
    assert work_state.doc_path(root, "proj").read_text() == legacy.read_text()
    # idempotent, and never deletes the old file
    assert work_state.migrate(root, "proj")["migrated"] is False
    assert legacy.exists()


def test_read_falls_back_to_the_legacy_name_before_migration(tmp_path):
    root = _project(tmp_path)
    work_state.legacy_path(root, "proj").write_text(
        STALE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    res = work_state.read(root, "proj")
    assert res["read_from"].endswith("operator-state.md")
    assert res["staleness"]["updated_by"] == "S-almdudleer-operator-p533"


def test_write_mirrors_to_the_legacy_name_for_one_release(tmp_path):
    """Rollback story: an old worker (or a rolled-back install) still reads
    `operator-state.md`, and leaving it frozen at July is the same defect with
    a new filename."""
    root = _project(tmp_path)
    work_state.write(root, "proj", "# now\n", sid="S-a-p1", role="operator",
                     base_rev=0)
    assert "# now" in work_state.legacy_path(root, "proj").read_text()


def test_any_live_session_may_write_but_an_unknown_sid_may_not():
    """«в него должна иметь право и юзер-сессия писать» — the widening is a
    liveness check, not a role allow-list. The one refusal is a caller with no
    session md: an unattributable full-replace of the project's state is worse
    than a refused one."""
    assert work_state.may_write({"sid": "S-uc-p1", "role": "user-conversation"})
    assert work_state.may_write({"sid": "S-dev-p1", "role": "dev"})
    assert work_state.may_write(None) is False


def test_work_state_write_action_refuses_an_unregistered_sid(tmp_path, monkeypatch):
    import bot_squad_worker.actions as A
    from bot_squad_worker.actions import ActionError

    root = _project(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: _cfg(root))
    with pytest.raises(ActionError) as e:
        A.dispatch("work_state_write", {"slug": "proj", "sid": "S-ghost-p9",
                                        "content": "x"})
    assert "no session md" in str(e.value)


def test_work_state_write_action_accepts_a_user_conversation_session(tmp_path,
                                                                     monkeypatch):
    import bot_squad_worker.actions as A

    root = _project(tmp_path)
    (root / "proj" / "sessions" / "S-u-user_session-p1.md").write_text(
        "---\nsid: S-u-user_session-p1\nwindow: user_session\nstatus: active\n"
        "task_id: ~\nstarted_at: 2026-09-06T00:00:00Z\n---\n")
    monkeypatch.setattr(A, "_get_config", lambda: _cfg(root))
    out = A.dispatch("work_state_write", {
        "slug": "proj", "sid": "S-u-user_session-p1",
        "content": "# work state\n", "base_rev": 0})
    assert out["rev"] == 1
    hdr = work_state.parse_header(work_state.doc_path(root, "proj").read_text())
    assert hdr["updated_by"] == "S-u-user_session-p1"
    assert hdr["role"] == "user-conversation"


def test_work_state_doc_action_carries_the_verdict_not_just_a_date(tmp_path,
                                                                   monkeypatch):
    import bot_squad_worker.actions as A

    root = _project(tmp_path)
    work_state.legacy_path(root, "proj").write_text(
        STALE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(A, "_get_config", lambda: _cfg(root))
    out = A.dispatch("work_state_doc", {"slug": "proj"})
    assert out["staleness"]["stale"] is True
    assert "HISTORY" in out["banner"]
    # the action migrates on read, so the doc now lives under the new name
    assert work_state.doc_path(root, "proj").exists()


def test_the_old_action_name_still_resolves(tmp_path, monkeypatch):
    """`operator_state_doc` is in a released `bsq` and in the api. Renaming the
    doc must not break the callers that have not been redeployed yet."""
    import bot_squad_worker.actions as A

    root = _project(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: _cfg(root))
    out = A.dispatch("operator_state_doc", {"slug": "proj"})
    assert out["ok"] is True
    assert out["path"].endswith("work-state.md")


def test_compact_write_state_routes_a_uc_session_through_the_locked_writer(
        tmp_path, monkeypatch):
    """The end-to-end shape of the defect, exercised at the seam that failed:
    a task-LESS user-conversation session's compact must land in work-state.md,
    revisioned, and NOT in a per-role file."""
    import bot_squad_worker.actions as A

    root = _project(tmp_path)
    (root / "proj" / "sessions" / "S-u-user_session-p1.md").write_text(
        "---\nsid: S-u-user_session-p1\nwindow: user_session\nstatus: active\n"
        "task_id: ~\nstarted_at: 2026-09-06T00:00:00Z\n---\n")
    monkeypatch.setattr(A, "_get_config", lambda: _cfg(root))
    out = A.dispatch("compact_write_state", {
        "slug": "proj", "sid": "S-u-user_session-p1",
        "content": "## Priorities\n\nship T-0942\n"})
    assert out["artifact_path"].endswith("work-state.md")
    assert out["rev"] == 1
    assert not list(work_state.artifacts_dir(root, "proj").glob("role-*.md"))


# ---------------------------------------------------------------------------
# 5. The freshness sweep — the top-up, constrained against the drift precedent
# ---------------------------------------------------------------------------

def _sweep_env(tmp_path, monkeypatch, *, role="operator", activity="idle",
               status="active"):
    from bot_squad_worker import sessions as S

    root = _project(tmp_path)
    work_state.doc_path(root, "proj").write_text(
        STALE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    sid = "S-u-operator-p1"
    (root / "proj" / "sessions" / f"{sid}.md").write_text(
        f"---\nsid: {sid}\nwindow: operator\nstatus: {status}\n"
        "task_id: ~\nstarted_at: 2026-09-06T00:00:00Z\n---\n")

    monkeypatch.setattr(work_state, "_int_env", lambda n, d: d)
    import bot_squad_worker.automation as AU
    monkeypatch.setattr(AU, "gate", lambda cfg, slug, who: True)
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: [
        {"sid": sid, "role": role, "status": status, "activity": activity}])
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "compute_sid", lambda user, w, p: sid)
    monkeypatch.setattr(S, "list_panes", lambda: [
        types.SimpleNamespace(window="operator", pane_id="%1")])
    delivered: list = []
    monkeypatch.setattr(S, "_deliver_prompt",
                        lambda pane, text, **kw: delivered.append((pane, text)))
    return root, sid, delivered


def test_sweep_nudges_a_stale_doc_holder_exactly_once_per_episode(tmp_path,
                                                                 monkeypatch):
    """ONE nudge per stale EPISODE, not one per cooldown. drift_check re-fired
    every 2 minutes into busy panes and the fleet switched it off on all eight
    lanes; this sweep tells a session once and tells it again only after
    somebody has actually written the doc."""
    root, sid, delivered = _sweep_env(tmp_path, monkeypatch)
    cfg = _cfg(root)
    r1 = work_state.freshness_sweep(cfg, "proj")
    assert [n["sid"] for n in r1["nudged"]] == [sid]
    assert "work-state" in delivered[0][1].lower() or "WORK-STATE" in delivered[0][1]

    r2 = work_state.freshness_sweep(cfg, "proj")
    assert r2["nudged"] == []
    assert len(delivered) == 1


def test_sweep_nudges_again_only_after_the_doc_actually_moves(tmp_path, monkeypatch):
    root, sid, delivered = _sweep_env(tmp_path, monkeypatch)
    cfg = _cfg(root)
    work_state.freshness_sweep(cfg, "proj")
    # a new stale episode: same doc rewritten, still stale (age forced by ts)
    work_state.doc_path(root, "proj").write_text(
        work_state.compose("s", rev=9, sid="S-x-p1", role="operator",
                           ts="2026-08-01T00:00:00Z"), encoding="utf-8")
    work_state.freshness_sweep(cfg, "proj")
    assert len(delivered) == 2


def test_sweep_never_injects_into_a_running_pane(tmp_path, monkeypatch):
    """An injection into a busy composer parks unsubmitted — that is how
    drift_check's nudges became pure noise."""
    root, sid, delivered = _sweep_env(tmp_path, monkeypatch, activity="running")
    assert work_state.freshness_sweep(_cfg(root), "proj")["nudged"] == []
    assert delivered == []


def test_sweep_ignores_roles_that_do_not_own_the_doc(tmp_path, monkeypatch):
    root, sid, delivered = _sweep_env(tmp_path, monkeypatch, role="dev")
    assert work_state.freshness_sweep(_cfg(root), "proj")["nudged"] == []
    assert delivered == []


def test_sweep_is_silent_on_a_current_doc(tmp_path, monkeypatch):
    root, sid, delivered = _sweep_env(tmp_path, monkeypatch)
    work_state.doc_path(root, "proj").write_text(
        work_state.compose("s", rev=1, sid="S-x-p1", role="operator"),
        encoding="utf-8")
    res = work_state.freshness_sweep(_cfg(root), "proj")
    assert res["stale"] is False and res["nudged"] == []
    assert delivered == []
