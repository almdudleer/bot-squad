"""T-0937 — the operator SEAT: a live root session drives the board itself.

Promotes the manual walkthrough (written + walked FIRST, per T-0158) of the
measured 2026-08-31 10:19 incident: the root user-conversation session set a
drive mode, and 25 seconds later the 60s re-drive minted a separate operator
behind it, because "who holds the operator seat" was derived only from
``role == operator`` on a live session.

The prerequisite every test here shares IS that incident's shape — pending
backlog, load ABOVE the T-0855 ceilings so the tier is ``operator``, a live
root, no live operator. :func:`test_baseline_no_seat_claim_still_respawns` is
the NEGATIVE CONTROL for the whole file: on exactly that fixture, with no seat
claimed, the tick still spawns. Without it every assertion below would also
pass against a gate that suppressed the operator unconditionally.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_squad_worker import actions as A
from bot_squad_worker import dispatch
from bot_squad_worker import operator_redrive as ord_
from bot_squad_worker import operator_seat as seat
from bot_squad_worker import pace
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

#: The root's SID shape: the window embedded in it is what makes it the root
#: (``budding.is_root_session``), not any stored role.
ROOT = "S-u-gu_root-user-conversation-p490"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """The incident's shape: 4 pending tickets, 3 live devs holding 3 tasks (AT
    the T-0855 ceiling, so the tier is ``operator``), one live root, no operator.

    Yields ``(cfg, slug, spawns)``; ``spawns`` records every session the
    re-drive would have opened.
    """
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug / "backlog").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / slug / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ord_, "_SPAWN_COOLDOWN_SEC", 0)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: "/home/u")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: None)

    spawns: list[dict] = []

    def _fake_spawn(c, s, window, initial_prompt=None, owner=None, model=None, **kw):
        sid = f"S-op-{len(spawns)}"
        spawns.append({"window": window, "owner": owner, "sid": sid})
        return {"ok": True, "sid": sid}

    monkeypatch.setattr(S, "spawn", _fake_spawn)
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: [])
    monkeypatch.delenv("BOT_SQUAD_OPERATOR_SEAT", raising=False)

    for i in (1, 2, 3, 4):
        _task(cfg, slug, f"T-000{i}")
    for i in (1, 2, 3):
        _session(cfg, slug, f"S-u-d{i}-dev-p{i}", f"d{i}-dev", task_id=f"T-000{i}")
    _session(cfg, slug, ROOT, "gu_root-user-conversation")
    return cfg, slug, spawns


def _task(cfg, slug, tid, *, status="open"):
    (cfg.data_dir / slug / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: x\nstatus: {status}\ninitiative: ~\n---\nbody\n")


def _session(cfg, slug, sid, window, *, task_id=None, status="active",
             archived=False):
    meta = {"sid": sid, "status": status, "window": window,
            "cwd": str(cfg.projects[slug].repo_path), "claude_uuid": "u-" + sid}
    if task_id:
        meta["task_id"] = task_id
    if archived:
        meta["archived"] = "true"
    S._write_session_metadata(cfg.data_dir / slug / "sessions" / f"{sid}.md", meta)


# ---------------------------------------------------------------------------
# The negative control, and the fix
# ---------------------------------------------------------------------------

def test_baseline_no_seat_claim_still_respawns(env):
    """CONTROL. Same fixture, nothing claimed: the operator IS minted.

    This is the bug as measured, and it is what makes every other test in this
    file mean something — a gate that always suppressed would pass them all.
    """
    cfg, slug, spawns = env
    assert seat.seat_holder(cfg, slug) is None

    res = ord_.tick(cfg, slug)

    assert res["action"] == "respawned"
    assert len(spawns) == 1


def test_drive_set_by_a_live_root_holds_the_seat(env):
    """The incident, closed. `bsq pace drive` stamps the setter's SID into
    ``drive.set_by``; that live root is by construction the session driving the
    board, so the tick continues instead of minting a second dispatcher."""
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by=ROOT,
                   source_text="запрос на параллелизм")

    holder = seat.seat_holder(cfg, slug)
    assert holder["sid"] == ROOT
    assert holder["kind"] == "drive"
    # The hat is EXTRA — the session keeps its own role (no morph), which is
    # what keeps its user-conversation routing intact.
    assert holder["role"] == "user-conversation"

    res = ord_.tick(cfg, slug)

    assert res["action"] == "seat-held"
    assert res["seat"]["sid"] == ROOT
    assert spawns == []


def test_seat_vacates_when_the_holder_dies(env):
    """Liveness is the whole lease: no expiry, no heartbeat, no reaper. An
    archived holder means the seat is vacant and the backlog is driven again —
    «spawn resumes if it dies with drive on»."""
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by=ROOT)
    assert ord_.tick(cfg, slug)["action"] == "seat-held"

    _session(cfg, slug, ROOT, "gu_root-user-conversation", archived=True)

    assert seat.seat_holder(cfg, slug) is None
    res = ord_.tick(cfg, slug)
    assert res["action"] == "respawned"
    assert len(spawns) == 1


def test_a_paused_holder_still_holds_the_seat(env):
    """A PAUSED root is a parked process that comes back, not a vacancy.
    Treating that window as empty is the race this module exists to remove."""
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by=ROOT)
    _session(cfg, slug, ROOT, "gu_root-user-conversation", status="paused")

    assert seat.seat_holder(cfg, slug)["sid"] == ROOT
    assert ord_.tick(cfg, slug)["action"] == "seat-held"
    assert spawns == []


def test_only_the_root_may_wear_the_hat(env):
    """A DEV that set the drive does NOT hold the seat — otherwise any leaf
    worker could suppress the board's driver. The rejection is REPORTED, not
    silently dropped: a claim by an ineligible session must not read the same
    as no claim at all."""
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by="S-u-d1-dev-p1")

    assert seat.seat_holder(cfg, slug) is None
    status = seat.seat_status(cfg, slug)
    assert status["held"] is False
    assert any("S-u-d1-dev-p1" in why for why in status["rejected"])
    assert ord_.tick(cfg, slug)["action"] == "respawned"


def test_a_non_sid_set_by_is_provenance_not_a_claim(env):
    """The worker write path defaults ``set_by`` to ``"user"``. Only something
    that can NAME a session may hold a seat."""
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by="user")

    assert seat.seat_holder(cfg, slug) is None
    assert ord_.tick(cfg, slug)["action"] == "respawned"


def test_kill_switch_disables_the_seat_entirely(env, monkeypatch):
    """``BOT_SQUAD_OPERATOR_SEAT=0`` is the whole rollback: every gate behaves
    exactly as it did before this module existed."""
    cfg, slug, spawns = env
    seat.claim(cfg, slug, ROOT)
    assert ord_.tick(cfg, slug)["action"] == "seat-held"

    monkeypatch.setenv("BOT_SQUAD_OPERATOR_SEAT", "0")

    assert seat.seat_holder(cfg, slug) is None
    assert seat.seat_status(cfg, slug)["enabled"] is False
    assert ord_.tick(cfg, slug)["action"] == "respawned"


def test_an_unreadable_seat_fails_open_but_says_so(env, monkeypatch):
    """Fail-open, deliberately: a broken seat read resolves to VACANT so it can
    never strand the backlog. The SURFACE still reports the fault as an error
    rather than as an ordinary empty seat (the silent-None failure D-0069 names
    as its most dangerous line)."""
    cfg, slug, spawns = env
    seat.claim(cfg, slug, ROOT)

    def _boom(*a, **k):
        raise RuntimeError("state is unreadable")

    monkeypatch.setattr(seat, "_resolve", _boom)

    assert seat.seat_holder(cfg, slug) is None
    status = seat.seat_status(cfg, slug)
    assert status["held"] is False
    assert "state is unreadable" in status["error"]
    assert ord_.tick(cfg, slug)["action"] == "respawned"


# ---------------------------------------------------------------------------
# The explicit claim / release pair
# ---------------------------------------------------------------------------

def test_explicit_claim_works_without_any_drive_block(env):
    """The deliberate move, and the one that works on a project that has never
    configured a drive mode."""
    cfg, slug, spawns = env
    assert pace.read_drive(cfg, slug)["configured"] is False

    res = seat.claim(cfg, slug, ROOT, source="оператор одновременно с юзер-сессией")

    assert res["seat"]["kind"] == "claim"
    assert res["seat"]["source"] == "оператор одновременно с юзер-сессией"
    assert ord_.tick(cfg, slug)["action"] == "seat-held"
    assert spawns == []


def test_release_actually_releases_a_drive_backed_seat(env):
    """The tombstone. Deleting the explicit claim would not be enough: the drive
    block still names the same live root, so the implicit claim would re-assert
    itself and "release" would silently not release."""
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by=ROOT)
    assert ord_.tick(cfg, slug)["action"] == "seat-held"

    res = seat.release(cfg, slug, ROOT)

    assert res["released"] is True
    assert seat.seat_holder(cfg, slug) is None
    assert ord_.tick(cfg, slug)["action"] == "respawned"


def test_a_re_stated_drive_revives_the_seat_after_a_release(env):
    """...and the tombstone must not FREEZE the seat either: a fresh drive
    statement is a fresher instruction and takes it back.

    The two writes here land in the same wall-clock second, which is the case a
    timestamp comparison loses — ``pace`` stamps ``set_at`` at second
    resolution while the tombstone carries a fractional epoch, so the re-set
    reads as older than the release that preceded it. Measured in the T-0937
    walkthrough (step 7) before the statement-identity rule replaced it.
    """
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by=ROOT)
    seat.release(cfg, slug, ROOT)
    assert seat.seat_holder(cfg, slug) is None

    pace.set_drive(cfg, slug, scope="in_progress", set_by=ROOT)

    assert seat.seat_holder(cfg, slug)["sid"] == ROOT
    assert ord_.tick(cfg, slug)["action"] == "seat-held"


def test_release_of_a_vacant_seat_is_a_no_op(env):
    cfg, slug, spawns = env
    assert seat.release(cfg, slug, ROOT) == {"ok": True, "released": False,
                                             "was": None}


def test_claim_refuses_a_dead_session(env):
    cfg, slug, spawns = env
    with pytest.raises(ActionError, match="not a live session"):
        seat.claim(cfg, slug, "S-u-gu_ghost-user-conversation-p1")


def test_claim_refuses_while_an_operator_session_is_live(env, monkeypatch):
    """T-0472 owns that case: an operator session already drives the board, and
    a seat claim beside it would be the second dispatcher this whole ticket is
    about preventing."""
    cfg, slug, spawns = env
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: ["S-op-live"])

    with pytest.raises(ActionError, match="already driving"):
        seat.claim(cfg, slug, ROOT)


def test_claim_refuses_to_take_another_live_roots_seat_without_force(env):
    cfg, slug, spawns = env
    other = "S-u-gu_other-user-conversation-p2"
    _session(cfg, slug, other, "gu_other-user-conversation")
    seat.claim(cfg, slug, other)

    with pytest.raises(ActionError, match="already holds the seat"):
        seat.claim(cfg, slug, ROOT)

    res = seat.claim(cfg, slug, ROOT, force=True)
    assert res["replaced"] == other
    assert seat.seat_holder(cfg, slug)["sid"] == ROOT


def test_release_refuses_someone_elses_claim_without_force(env):
    cfg, slug, spawns = env
    other = "S-u-gu_other-user-conversation-p2"
    _session(cfg, slug, other, "gu_other-user-conversation")
    seat.claim(cfg, slug, other)

    with pytest.raises(ActionError, match="held by"):
        seat.release(cfg, slug, ROOT)

    assert seat.release(cfg, slug, ROOT, force=True)["released"] is True


# ---------------------------------------------------------------------------
# The handover (T-0932's L1→L2 rung)
# ---------------------------------------------------------------------------

def test_bud_operator_by_the_seat_holder_hands_the_wheel_over(env):
    """The one move that legitimately ends a root's own drive: it spawns the
    operator AND drops the seat, so no claim is left contradicting the operator
    it just created."""
    cfg, slug, spawns = env
    seat.claim(cfg, slug, ROOT)

    res = ord_.bud_operator(cfg, slug, requested_by=ROOT)

    assert res["spawned"] is True
    assert len(spawns) == 1
    assert seat.seat_holder(cfg, slug) is None


def test_bud_operator_by_anyone_else_is_refused_while_the_seat_is_held(env):
    """Same reason a live operator refuses it: two dispatchers double-drive the
    backlog. The reason NAMES the holder so the caller can route through it."""
    cfg, slug, spawns = env
    seat.claim(cfg, slug, ROOT)

    res = ord_.bud_operator(cfg, slug, requested_by="S-u-d1-dev-p1")

    assert res["spawned"] is False
    assert ROOT in res["reason"]
    assert spawns == []


def test_the_seat_is_not_released_when_the_handover_spawn_is_deferred(env,
                                                                      monkeypatch):
    """Releasing before the spawn would open a window in which the 60s tick
    could mint a SECOND operator behind the one being handed to."""
    cfg, slug, spawns = env
    seat.claim(cfg, slug, ROOT)
    monkeypatch.setattr(ord_, "_respawn_operator", lambda c, s: None)

    res = ord_.bud_operator(cfg, slug, requested_by=ROOT)

    assert res["spawned"] is False
    assert seat.seat_holder(cfg, slug)["sid"] == ROOT


# ---------------------------------------------------------------------------
# What the surfaces say
# ---------------------------------------------------------------------------

def test_topology_reports_the_seat_and_keeps_the_tier_honest(env):
    """`tier` still answers "how much load is there" and says ``operator``;
    `operator_needed` answers "must one be MINTED" and says no. Collapsing them
    into one number would hide the first fact from `bsq route`."""
    cfg, slug, spawns = env
    pace.set_drive(cfg, slug, scope="all", set_by=ROOT)

    topo = dispatch.decide_topology(cfg, slug)

    assert topo["tier"] == "operator"
    assert topo["operator_needed"] is False
    assert topo["route"] == "direct"
    assert topo["may_dispatch_directly"] is True
    assert topo["operator_seat"]["sid"] == ROOT
    assert any(s.startswith("operator-seat-held:") for s in topo["signals"])
    assert ROOT in topo["reason"]
    # The load that WOULD have justified the tier is still stated, not swallowed.
    assert "would otherwise justify the operator tier" in topo["reason"]


def test_topology_without_a_seat_is_unchanged(env):
    """CONTROL for the pin above — the T-0855 verdict on the same fixture."""
    cfg, slug, spawns = env

    topo = dispatch.decide_topology(cfg, slug)

    assert topo["tier"] == "operator"
    assert topo["operator_needed"] is True
    assert topo["route"] == "via_operator"
    assert topo["operator_seat"] is None


def test_operator_status_action_reports_seat_held(env, monkeypatch):
    """`bsq operator status` must read "somebody else is driving", not "nothing
    is happening" — the two look identical from outside."""
    cfg, slug, spawns = env
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    pace.set_drive(cfg, slug, scope="all", set_by=ROOT)

    out = A._action_operator_status({"slug": slug})

    assert out["state"] == "seat-held"
    assert out["operator_seat"]["held"] is True
    assert out["operator_seat"]["holder"]["sid"] == ROOT


def test_operator_status_action_without_a_seat_is_unchanged(env, monkeypatch):
    """CONTROL: the same read on the same fixture with no seat claimed."""
    cfg, slug, spawns = env
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    out = A._action_operator_status({"slug": slug})

    assert out["state"] == "pending-redrive"
    assert out["operator_seat"]["held"] is False


def test_seat_actions_round_trip_through_the_worker_surface(env, monkeypatch):
    """The two write actions the CLI calls."""
    cfg, slug, spawns = env
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    res = A._action_operator_seat_claim({"slug": slug, "sid": ROOT,
                                         "source": "его слова"})
    assert res["seat"]["sid"] == ROOT
    assert ord_.tick(cfg, slug)["action"] == "seat-held"

    res = A._action_operator_seat_release({"slug": slug, "sid": ROOT})
    assert res["released"] is True
    assert ord_.tick(cfg, slug)["action"] == "respawned"


def test_seat_actions_reject_unknown_params(env, monkeypatch):
    cfg, slug, spawns = env
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="unexpected params"):
        A._action_operator_seat_claim({"slug": slug, "sid": ROOT, "nope": 1})
    with pytest.raises(ActionError, match="missing required params"):
        A._action_operator_seat_release({})


# ---------------------------------------------------------------------------
# The gate's place in the tick's order
# ---------------------------------------------------------------------------

def test_pause_still_wins_over_a_held_seat(env):
    """The user's one "stop everything automatic" lever is not weakened by the
    seat: a paused project reports paused, whoever is driving."""
    cfg, slug, spawns = env
    seat.claim(cfg, slug, ROOT)
    ord_.pause(cfg, slug, by="user", reason="test")

    assert ord_.tick(cfg, slug)["action"] == "paused"


def test_a_live_operator_still_wins_over_a_held_seat(env, monkeypatch):
    """T-0472's ``continue`` is checked first: if an operator session somehow
    exists, the tick must report it rather than a stale claim."""
    cfg, slug, spawns = env
    seat.claim(cfg, slug, ROOT)
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: ["S-op-live"])

    res = ord_.tick(cfg, slug)

    assert res["action"] == "continue"
    assert res["operator"] == "S-op-live"


def test_an_empty_backlog_is_still_idle_with_a_seat_held(env):
    cfg, slug, spawns = env
    for md in (cfg.data_dir / slug / "backlog").glob("*.md"):
        md.unlink()
    seat.claim(cfg, slug, ROOT)

    assert ord_.tick(cfg, slug)["action"] == "idle-empty-backlog"


# ---------------------------------------------------------------------------
# The third spawn seam: `spawn_session` (API auto-spawn-on-create and a
# hand-run `bsq spawn --window operator`). The re-drive gate does not cover it.
# ---------------------------------------------------------------------------

def test_spawn_session_refuses_an_operator_while_the_seat_is_held(env, monkeypatch):
    """An operator spawned past a held seat is the second dispatcher T-0472
    exists to prevent, arriving through the one door the re-drive gate does not
    watch. The refusal names the holder AND the verb that unblocks it."""
    cfg, slug, spawns = env
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    seat.claim(cfg, slug, ROOT)

    with pytest.raises(ActionError) as exc:
        A._action_spawn_session({"slug": slug, "window": "operator"})

    assert ROOT in str(exc.value)
    assert "bsq operator seat release" in str(exc.value)
    assert spawns == []


def test_spawn_session_allows_an_operator_when_the_seat_is_vacant(env, monkeypatch):
    """CONTROL: the same call on the same fixture with nothing claimed. Without
    it, a guard that refused every operator spawn would pass the test above."""
    cfg, slug, spawns = env
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    assert seat.seat_holder(cfg, slug) is None

    A._action_spawn_session({"slug": slug, "window": "operator"})

    assert len(spawns) == 1
    assert spawns[0]["window"] == "operator"


def test_spawn_session_still_allows_a_dev_while_the_seat_is_held(env, monkeypatch):
    """The seat suppresses a rival DISPATCHER, never the work. A root holding
    the board must be able to spawn the devs it is steering — that is the whole
    point of holding it."""
    cfg, slug, spawns = env
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    seat.claim(cfg, slug, ROOT)

    A._action_spawn_session({"slug": slug, "window": "d9-dev", "task_id": "T-0004"})

    assert len(spawns) == 1
    assert spawns[0]["window"] == "d9-dev"
