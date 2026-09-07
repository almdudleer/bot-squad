"""T-1021 — a DECLARED WINDOW: an occupation the arbiter holds across the gaps
between its owner's commands.

WHAT THE TICKET SAID, AND WHAT MEASUREMENT SAID BACK
----------------------------------------------------
The ticket was filed as *"an announced exclusive occupation holds no slot in
fleet_slot: the arbiter reads free and would admit a session straight into
someone else's declared window"*. **The headline is false, and it was refuted
before a line of this was written.** On 2026-09-06 at 21:47Z, with p678's
announced double-build occupation live, the arbiter reported it:

    HOLD  build  92.1s  pgid 2454180  p678  "T-1004 double --no-cache build,
                                             ONE occupation spanning both
                                             builds and the gap"
    1/2 container+build  [BUILD — exclusive]

and the raw record agreed. ``--kind exclusive`` was likewise proposed as an
existing mechanism and is a pure alias — ``normalise_kind('exclusive') ->
'build'``, with no ``declare``/``extend``/``end`` verb anywhere in the module.

What survived measurement is narrower and is what this file pins: **every
reservation in the module resolves liveness from a live process group**
(``reap`` → ``pgid_alive``), so an occupation spanning an interval with no
process of the owner's work in it cannot be expressed at all. Measured on HEAD
in an isolated state dir:

* composed the way the module's own docstring documents it — ``run --kind build
  -- ./deploy.sh``, one command per run — the slot released between commands,
  ``status`` printed ``0/2 container+build … (free)`` during the gap, and a
  peer build was **GRANTED 4.2s into it**;
* a pure announcement to inboxes held nothing: ``(free)``, peer granted at 1.1s.

p678's gap was covered by ``bash t1004_determinism.frozen.sh`` — its own
wrapper script, holding both builds and the gap in one process group. That is a
property of that lane's shell script, not of the gate, and it is unavailable to
the window this verb exists for: one with an **agent in the gap**, which has to
read the first result and decide before starting the second. p678 froze its
script precisely so it would not need one.

THE POSITIVE CONTROL IS IN THE FILE, NOT IN A COMMENT
------------------------------------------------------
:func:`test_the_gap_is_reserved_only_because_of_the_declaration` runs the
identical scenario twice — once under a declared window and once without — and
asserts the peer is REFUSED in the first arm and GRANTED in the second. A guard
observed only passing is documentation. The second arm is the same code path
with the fix removed from the *scenario* rather than from the module, so it
cannot drift out of step with the first.

For removing the fix from the MODULE — the other direction the operator asked
for — set ``BOT_SQUAD_FLEET_SLOT_PATH`` to a mutated copy and run this file
against it. The recorded mutation and its result are on T-1021.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: The module under test. The override exists so the suite can be pointed at a
#: MUTATED copy — "prove your guard can fail" needs the module broken on
#: purpose, and breaking the live file to prove a point about the live file is
#: not an option: every lane on this box calls it, and the worker's loader
#: fails OPEN, so a torn file makes the fleet run UNGATED rather than crash.
SLOT = Path(os.environ.get("BOT_SQUAD_FLEET_SLOT_PATH")
            or (REPO / "scripts" / "cli" / "fleet_slot.py"))

pytestmark = pytest.mark.skipif(
    not SLOT.exists(), reason="fleet_slot.py not present in this tree")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _env(state: Path) -> dict:
    env = dict(os.environ)
    env["BOT_SQUAD_FLEET_SLOTS_DIR"] = str(state)
    # This box runs at D-state 8-80 with several lanes live. A test whose
    # verdict depends on the host's D-state that second measures the host.
    env["BOT_SQUAD_FLEET_DSTATE_MAX"] = ""
    # Never contend for the REAL fleet's stopgap semaphore from a test.
    env["BOT_SQUAD_FLEET_LEGACY_SLOT_DIR"] = ""
    env.pop("BOT_SQUAD_FLEET_SLOTS", None)
    env.pop("BOT_SQUAD_FLEET_MAX_DECLARATION_S", None)
    return env


def _slot(state: Path, *argv: str, lane: str = "p_test", timeout: float = 90,
          **envkw) -> subprocess.CompletedProcess:
    env = _env(state)
    env["BOT_SQUAD_SID"] = lane
    env.update({k: str(v) for k, v in envkw.items()})
    return subprocess.run([sys.executable, str(SLOT), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _status(state: Path) -> dict:
    out = _slot(state, "status", "--json")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _declare(state: Path, *, kind: str = "build", for_s: float = 120,
             note: str = "T-1021 window", lane: str = "p_owner",
             **envkw) -> str:
    out = _slot(state, "declare", "--kind", kind, "--for", str(for_s),
                "--note", note, lane=lane, **envkw)
    assert out.returncode == 0, out.stderr
    token = out.stdout.strip()
    assert token, f"declare printed no token; stderr={out.stderr}"
    return token


def _module():
    spec = importlib.util.spec_from_file_location("_t1021_fleet_slot", SLOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ECHO = "LANDED-IN-THE-POOL"


def _peer_asks_for_the_pool(state: Path, *, kind: str = "build",
                            wait: float = 4.0, note: str = "peer work",
                            under: str | None = None
                            ) -> subprocess.CompletedProcess:
    argv = ["run", "--kind", kind, "--note", note, "--wait", str(wait)]
    if under:
        argv += ["--under", under]
    argv += ["--", "/bin/echo", _ECHO]
    return _slot(state, *argv, lane="p_peer", timeout=wait + 60)


def _admitted(out: subprocess.CompletedProcess) -> bool:
    """Did the pool actually admit this run? Read the WORK's own output.

    Not the exit code: a refusal and a command that failed on its own both come
    back non-zero, and the claim here is about admission, not about rc.
    """
    return _ECHO in out.stdout


# ---------------------------------------------------------------------------
# 1. THE HEALTHY CASE FIRST — a detector that has never been run against a
#    correct system reports its own bugs as findings.
# ---------------------------------------------------------------------------

def test_the_healthy_case_declare_run_extend_run_end(tmp_path):
    """The whole T-1004 shape: two commands, a gap, one occupation."""
    tok = _declare(tmp_path, for_s=60, note="two builds and the gap")

    first = _slot(tmp_path, "run", "--kind", "build", "--under", tok,
                  "--note", "build 1 of 2", "--wait", "20",
                  "--", "/bin/echo", _ECHO, lane="p_owner")
    assert _admitted(first), f"owner blocked by its own window: {first.stderr}"

    ext = _slot(tmp_path, "extend", tok, "--for", "60", lane="p_owner")
    assert ext.returncode == 0, ext.stderr

    second = _slot(tmp_path, "run", "--kind", "build", "--under", tok,
                   "--note", "build 2 of 2", "--wait", "20",
                   "--", "/bin/echo", _ECHO, lane="p_owner")
    assert _admitted(second), second.stderr

    end = _slot(tmp_path, "end", tok, lane="p_owner")
    assert end.returncode == 0, end.stderr
    snap = _status(tmp_path)
    assert snap["declarations"] == []
    assert snap["build_held"] is False


# ---------------------------------------------------------------------------
# 2. DoD 1 — the arbiter REPORTS the window instead of reading free
# ---------------------------------------------------------------------------

def test_the_arbiter_reports_a_declared_window_and_not_free(tmp_path):
    tok = _declare(tmp_path, note="T-1004 double build")
    snap = _status(tmp_path)

    decls = snap["declarations"]
    assert len(decls) == 1, snap
    assert decls[0]["token"] == tok
    assert decls[0]["kind"] == "build"
    assert decls[0]["lane"] == "p_owner"
    assert decls[0]["expires_in_s"] > 0
    # The pool is OCCUPIED even though no process of the owner's work exists.
    assert snap["build_held"] is True, snap

    human = _slot(tmp_path, "status")
    assert human.returncode == 0
    assert "(free)" not in human.stdout, human.stdout
    assert "DECL" in human.stdout
    assert "1/2 container+build" in human.stdout
    # Say the VALUE: a reader must be able to tell a declared gap from an idle
    # host, or they conclude the arbiter is wrong and route around it.
    assert tok in human.stdout
    assert "BETWEEN commands" in human.stdout


# ---------------------------------------------------------------------------
# 3. DoD 2 — the peer is refused, AND told whose window it is
# ---------------------------------------------------------------------------

def test_a_peer_is_refused_into_a_declared_window(tmp_path):
    tok = _declare(tmp_path, note="T-1004 double build", lane="p_owner")

    out = _peer_asks_for_the_pool(tmp_path, kind="build")
    assert not _admitted(out), f"peer admitted into a declared window: {out.stdout}"
    assert out.returncode == 75

    # A bare refusal against a quiet host reads as a broken gate. The reason
    # has to carry the owner, the token and the lease.
    assert "DECLARED build window" in out.stderr
    assert "p_owner" in out.stderr
    assert tok in out.stderr
    assert "lease expires in" in out.stderr
    assert "NOT MEASURED" in out.stderr


def test_a_container_run_is_also_refused_by_a_build_window(tmp_path):
    """A build window is exclusive of the whole daemon pool, declared or not."""
    _declare(tmp_path, kind="build", note="exclusive daemon window")
    out = _peer_asks_for_the_pool(tmp_path, kind="container")
    assert not _admitted(out), out.stdout
    assert "DECLARED build window" in out.stderr


# ---------------------------------------------------------------------------
# 4. THE SPECIMEN, WITH ITS OWN POSITIVE CONTROL — both directions
# ---------------------------------------------------------------------------

def _lease_legitimately_lapsed(tmp_path: Path, tok: str) -> bool:
    """Did the arbiter's OWN record say this token's lease expired?

    T-1043: a peer admitted because ``reap_declarations`` reclaimed a lapsed
    lease is the arbiter working correctly; a peer admitted while the
    declaration was still live is the defect. Both look identical from the
    bare fact of admission — ``LANDED-IN-THE-POOL`` either way — so the two
    events are told apart the only way that does not depend on wall clock:
    reading ``reclaims.log``, which ``reap_declarations`` writes at the
    moment it drops a lapsed record (fleet_slot.py, ``_log_reclaim``).
    """
    log = tmp_path / "reclaims.log"
    if not log.exists():
        return False
    for line in log.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("token") == tok and "lease expired" in entry.get("why", ""):
            return True
    return False


@pytest.mark.parametrize("declared", [True, False])
def test_the_gap_is_reserved_only_because_of_the_declaration(tmp_path, declared):
    """The exact scenario that produced this ticket, run with and without.

    ``declared=True``  — the fix: the peer is REFUSED during the gap.
    ``declared=False`` — the fix removed from the scenario: the peer is
    GRANTED, which is the measured HEAD behaviour (peer build granted 4.2s into
    the gap on 2026-09-06) reproduced here as a live control rather than quoted
    from a note. Without this arm the passing arm proves only that something
    refused the peer, not that the declaration is what refused it.

    T-1043: on a saturated host the ``for_s=60`` lease taken at the top of
    this test can lapse for real before the peer ever asks — that is the
    arbiter reclaiming a dead lease correctly, not a defect, and the original
    version of this test could not tell the two apart (confirmed: forcing a
    real lapse reproduces the exact observed symptom — peer admitted,
    ``LANDED-IN-THE-POOL``, stderr with no reclaim line — while
    ``reclaims.log`` carries a ``lease expired`` entry for the token the
    whole time). Two independent defenses, so the ``declared=True`` arm stops
    depending on how slow the host was between here and the peer's request:
    the lease is RENEWED right before the gap, so it races the few seconds a
    subprocess spawn takes rather than the whole scenario's wall time; and if
    the peer is admitted anyway, ``reclaims.log`` — the arbiter's own record,
    not an inference from timing — says whether that was a legitimate reclaim
    or a live-window breach before this test calls it a defect.
    """
    tok = _declare(tmp_path, for_s=60, note="two builds and the gap") if declared else None

    # BUILD 1 — runs and EXITS. This is the release that opens the gap.
    argv = ["run", "--kind", "build", "--note", "build 1 of 2", "--wait", "20"]
    if tok:
        argv += ["--under", tok]
    argv += ["--", "/bin/echo", _ECHO]
    first = _slot(tmp_path, *argv, lane="p_owner")
    assert _admitted(first), first.stderr

    if tok:
        # Renew now, immediately before the gap, so the peer's request races
        # a fresh 60s window rather than for_s minus whatever BUILD 1 and
        # this scenario already spent under load.
        ext = _slot(tmp_path, "extend", tok, "--for", "60", lane="p_owner")
        assert ext.returncode == 0, ext.stderr

    # THE GAP. The owner is reading build 1's result and deciding; nothing of
    # its work is running. Confirm that from /proc, not from an assumption.
    snap = _status(tmp_path)
    assert snap["holders"] == [], f"a process of the owner's work survived: {snap}"

    peer = _peer_asks_for_the_pool(tmp_path, kind="build",
                                   note="peer build landing in the gap")
    if declared:
        if _admitted(peer) and _lease_legitimately_lapsed(tmp_path, tok):
            # T-1043: the token stays OUT of this reason on purpose. It is
            # random per run, and _skip_census (scripts/cli/bsq) sha256s
            # (count, location, reason) into the fingerprint a release gate
            # compares between refs — a token in the text would make this
            # skip's fingerprint different on every run it fires, which
            # reads as "these runs aren't comparable" for a reason that is
            # pure nonce and trains a reader to ignore a moved fingerprint.
            print(f"T-1043: reclaims.log confirms declared token {tok}'s "
                  "lease expired before the peer's request")
            pytest.skip(
                "the host stretched this scenario past the renewed 60s "
                "lease and the arbiter correctly reclaimed the declared "
                "window (confirmed via reclaims.log) before the peer's "
                "request — not a defect, see T-1043")
        assert not _admitted(peer), (
            "THE DEFECT: a peer landed in the middle of a declared occupation "
            "and reclaims.log records no lease expiry for this token — the "
            "declaration was still live. "
            f"stdout={peer.stdout!r} stderr={peer.stderr[-400:]!r}")
        assert snap["build_held"] is True
    else:
        assert _admitted(peer), (
            "CONTROL FAILED: without a declaration the peer should have been "
            "admitted into the gap, so this suite cannot distinguish the fix "
            f"from any other refusal. stderr={peer.stderr[-400:]!r}")
        assert snap["build_held"] is False


# ---------------------------------------------------------------------------
# 5. DoD 3 — the owner is not blocked by its own window, and learns when it lapses
# ---------------------------------------------------------------------------

def test_the_owner_runs_inside_its_own_window_without_queueing(tmp_path):
    tok = _declare(tmp_path, for_s=60)
    t0 = time.time()
    out = _slot(tmp_path, "run", "--kind", "build", "--under", tok,
                "--note", "owner work", "--wait", "8",
                "--", "/bin/echo", _ECHO, lane="p_owner")
    assert _admitted(out), f"the owner queued behind itself: {out.stderr}"
    # It must be GRANTED, not merely eventually granted after the lease ran out.
    assert time.time() - t0 < 8, "granted only because the window expired"


def test_a_run_under_a_lapsed_window_is_refused_loudly(tmp_path):
    """Silently degrading to a normal queued run is the dangerous outcome.

    A lane that believes it is the exclusive occupant of a pool peers can now
    enter has the fleet's picture wrong in the direction that interleaves two
    builds. It gets told, and its run does not start.
    """
    tok = _declare(tmp_path, for_s=2, note="short lease")
    time.sleep(3.0)

    out = _slot(tmp_path, "run", "--kind", "build", "--under", tok,
                "--note", "work after the lease lapsed", "--wait", "5",
                "--", "/bin/echo", _ECHO, lane="p_owner")
    assert not _admitted(out), out.stdout
    assert out.returncode == 75
    assert "no live declared window" in out.stderr
    assert "NOT the exclusive occupant" in out.stderr


def test_an_unknown_token_is_refused_rather_than_ignored(tmp_path):
    out = _slot(tmp_path, "run", "--kind", "build", "--under", "deadbeefcafe",
                "--note", "typo in the token", "--wait", "5",
                "--", "/bin/echo", _ECHO, lane="p_owner")
    assert not _admitted(out), out.stdout
    assert out.returncode == 75


def test_a_window_covers_its_own_pool_and_not_the_other_one(tmp_path):
    """``--under`` is scoped by KIND, because a window reserves a POOL.

    A build window reserves the daemon pool; it does not reserve a
    containerless slot, so containerless work under it must still take one.
    """
    tok = _declare(tmp_path, kind="build", for_s=60)
    out = _slot(tmp_path, "run", "--kind", "containerless", "--under", tok,
                "--note", "containerless under a build window", "--wait", "4",
                "--", "/bin/echo", _ECHO, lane="p_owner")
    assert not _admitted(out), out.stdout
    assert "covers containerless work" in out.stderr


# ---------------------------------------------------------------------------
# 6. DoD 4 — a declaration cannot wedge the fleet
# ---------------------------------------------------------------------------

def test_a_lapsed_lease_frees_the_pool_and_says_so_in_reclaims_log(tmp_path):
    """A lane that dies stops renewing, and the pool comes back on its own.

    This is the whole safety argument for a lease. It also has to be
    OBSERVABLE: a window that silently stopped existing teaches the next reader
    that the arbiter is unreliable, and its owner needs to be able to find out.
    """
    tok = _declare(tmp_path, for_s=2, note="a lane that is about to die")
    assert _status(tmp_path)["build_held"] is True

    time.sleep(3.0)
    snap = _status(tmp_path)
    assert snap["declarations"] == [], snap
    assert snap["build_held"] is False

    peer = _peer_asks_for_the_pool(tmp_path, kind="build")
    assert _admitted(peer), f"the pool stayed wedged after the lease: {peer.stderr}"

    log = (tmp_path / "reclaims.log").read_text()
    assert tok in log
    entry = [json.loads(l) for l in log.splitlines() if tok in l][0]
    assert "lease expired" in entry["why"]
    assert entry["lane"] == "p_owner"
    assert entry["note"] == "a lane that is about to die"


def test_extend_renews_the_lease(tmp_path):
    tok = _declare(tmp_path, for_s=3)
    before = _status(tmp_path)["declarations"][0]["expires_in_s"]
    out = _slot(tmp_path, "extend", tok, "--for", "120", lane="p_owner")
    assert out.returncode == 0, out.stderr
    after = _status(tmp_path)["declarations"][0]
    assert after["expires_in_s"] > before + 60
    assert after["renewals"] == 1


def test_extend_refuses_to_resurrect_a_lapsed_window(tmp_path):
    """Re-creating a lapsed record restores a belief the fleet no longer shares.

    Once the lease expired the arbiter told every peer the pool was free, and
    one may already be inside it. A lapsed window is RE-DECLARED, through the
    queue, like any other claim.
    """
    tok = _declare(tmp_path, for_s=2)
    time.sleep(3.0)
    out = _slot(tmp_path, "extend", tok, "--for", "120", lane="p_owner")
    assert out.returncode == 75
    assert "NOT resurrected" in out.stderr
    assert _status(tmp_path)["declarations"] == []


def test_a_declaration_longer_than_the_cap_is_refused(tmp_path):
    out = _slot(tmp_path, "declare", "--kind", "build", "--for", "99999",
                "--note", "forever", lane="p_owner")
    assert out.returncode == 2
    assert "exceeds the cap" in out.stderr
    assert _status(tmp_path)["declarations"] == []


def test_the_cap_is_configurable_without_a_code_change(tmp_path):
    tok = _declare(tmp_path, for_s=50,
                   BOT_SQUAD_FLEET_MAX_DECLARATION_S="60")
    assert _status(tmp_path)["declarations"][0]["token"] == tok
    out = _slot(tmp_path, "declare", "--kind", "build", "--for", "70",
                "--note", "over the lowered cap", lane="p_owner",
                BOT_SQUAD_FLEET_MAX_DECLARATION_S="60")
    assert out.returncode == 2


# ---------------------------------------------------------------------------
# 7. A window is granted BY THE ARBITER, under the same policy as a run
# ---------------------------------------------------------------------------

def test_a_declaration_waits_for_a_live_holder_to_drain(tmp_path):
    """A window must not be granted over the top of somebody's running work."""
    sleeper = subprocess.Popen(
        [sys.executable, str(SLOT), "run", "--kind", "container",
         "--note", "peer container run", "--wait", "60", "--",
         sys.executable, "-c", "import time; time.sleep(6)"],
        env={**_env(tmp_path), "BOT_SQUAD_SID": "p_peer"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not _status(tmp_path)["holders"]:
            time.sleep(0.2)
        assert _status(tmp_path)["holders"], "peer never took its slot"

        out = _slot(tmp_path, "declare", "--kind", "build", "--for", "60",
                    "--note", "window over a live holder", "--wait", "2",
                    lane="p_owner")
        assert out.returncode == 75, out.stdout
        assert "drains the pool" in out.stderr
        assert _status(tmp_path)["declarations"] == []
    finally:
        sleeper.wait(timeout=60)


def test_a_containerless_window_does_not_block_a_build(tmp_path):
    """The two-pool split survives declarations, or a window recreates the
    defect T-0994 exists to remove: an occupation exclusive of every kind of
    testing is one every lane routes around."""
    _declare(tmp_path, kind="containerless", for_s=60,
             note="a long containerless occupation")
    snap = _status(tmp_path)
    assert snap["containerless_held"] == 1
    assert snap["build_held"] is False

    peer = _peer_asks_for_the_pool(tmp_path, kind="build")
    assert _admitted(peer), f"a containerless window blocked a build: {peer.stderr}"


def test_a_containerless_window_takes_one_of_its_own_pool(tmp_path):
    _declare(tmp_path, kind="containerless", for_s=60, note="cl window")
    out = _peer_asks_for_the_pool(tmp_path, kind="containerless", wait=3.0,
                                  note="peer cl work")
    # 3 slots, 1 declared: still room.
    assert _admitted(out), out.stderr
    snap = _status(tmp_path)
    assert snap["containerless_held"] == 1
    assert snap["containerless_slots"] == 3


# ---------------------------------------------------------------------------
# 8. THE TWO-COPIES HAZARD — an OLD copy must not be able to delete a window
# ---------------------------------------------------------------------------

def test_the_pgid_reaper_never_touches_a_declaration(tmp_path):
    """Declarations live outside ``holders/``, and that is load-bearing.

    Two copies of this module run on the box and share ONE registry: the
    install's (loaded by ``deploy.py`` at an absolute path) and the clone's. A
    declaration written into ``holders/`` would be read by whichever copy is
    older, whose ``reap`` resolves liveness from the pgid, found dead **during
    the very gap the declaration exists to cover**, and DELETED — an old copy
    silently erasing a new copy's reservation and logging it as a reclaim.

    So the property under test is not a preference: ``reap`` — the pgid-based
    reaper, which is the code an old copy also has — must leave a declaration
    standing even though its pgid is dead by construction.
    """
    mod = _module()
    tok = _declare(tmp_path, for_s=60, note="window with no live process")
    decl_file = tmp_path / "declarations" / f"{tok}.json"
    assert decl_file.exists()

    rec = json.loads(decl_file.read_text())
    assert rec["pgid"] is None, "a declaration must not claim a process group"

    with mod._Registry(tmp_path):
        holders, waiters = mod.reap(tmp_path)
    assert holders == [] and waiters == []
    assert decl_file.exists(), "the pgid reaper deleted a declared window"

    # And it is still enforced afterwards.
    assert _status(tmp_path)["build_held"] is True
