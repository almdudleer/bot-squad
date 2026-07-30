import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def test_health_no_worker(tmp_bot_squad: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["worker"]["alive"] is False    # heartbeat file doesn't exist


def test_health_worker_fresh(tmp_bot_squad: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    hb = tmp_bot_squad / "data" / "_worker" / "heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.touch()
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.json()["worker"]["alive"] is True


def test_health_exposes_git_sha(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0379: /api/health surfaces the image's build sha (BOT_SQUAD_GIT_SHA),
    so 'is the deployed commit actually running?' is one curl — and the deploy
    recipe / monitor can assert running==deployed instead of trusting a success
    report (the stale-image gap that shipped T-0376 unfixed)."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", "abc1234deadbeef")
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.json()["git_sha"] == "abc1234deadbeef"


def test_health_git_sha_unknown_when_unset(tmp_bot_squad: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.delenv("BOT_SQUAD_GIT_SHA", raising=False)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.json()["git_sha"] == "unknown"


# ---------------------------------------------------------------------------
# T-0456: worker boot_git_sha surface + FAILURE-ONLY worker.health pill
# (dead heartbeat OR API/worker sha drift). The worker heartbeat now carries
# its boot sha as the file body; the API reads it and flags drift/dead ONLY
# when there's a real problem (no green noise).
# ---------------------------------------------------------------------------


def _health_env(tmp_bot_squad: Path, monkeypatch, api_sha: str = "matchsha") -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", api_sha)


def _write_heartbeat(tmp_bot_squad: Path, content: str) -> Path:
    hb = tmp_bot_squad / "data" / "_worker" / "heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(content)
    return hb


def _write_install(tmp_bot_squad: Path, sha: str) -> Path:
    """T-0824: the third sha — what the INSTALL TREE's HEAD is, as the worker
    publishes it beside the heartbeat.

    Every scenario below now has to say what the install tree is at, and that is
    the point of the ticket rather than test bookkeeping: a payload that cannot
    name the deployed sha cannot answer "is the running worker executing the
    deployed code?", so leaving it out is itself a distinct, flagged state
    (`install_sha_unknown`) and not a quiet one.
    """
    wdir = tmp_bot_squad / "data" / "_worker"
    wdir.mkdir(parents=True, exist_ok=True)
    marker = wdir / "install_tree.json"
    marker.write_text(json.dumps({"git_sha": sha, "at": time.time()}))
    return marker


# 40-hex, because the install marker is validated as a full sha (a prefix or a
# placeholder buys no comparison — same bar as the deploy job's `target_sha`).
CONVERGED_SHA = "1" * 40


def test_health_surfaces_worker_sha(tmp_bot_squad: Path, monkeypatch) -> None:
    """The worker's boot sha (heartbeat body) is surfaced under worker.git_sha."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha=CONVERGED_SHA)
    _write_heartbeat(tmp_bot_squad, CONVERGED_SHA + "\n")
    _write_install(tmp_bot_squad, CONVERGED_SHA)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.json()["worker"]["git_sha"] == CONVERGED_SHA


def test_health_worker_ok_has_no_health_alarm(tmp_bot_squad: Path, monkeypatch) -> None:
    """Failure-only: a fresh heartbeat with a MATCHING sha emits no worker.health."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha=CONVERGED_SHA)
    _write_heartbeat(tmp_bot_squad, CONVERGED_SHA + "\n")
    _write_install(tmp_bot_squad, CONVERGED_SHA)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    worker = r.json()["worker"]
    assert worker["alive"] is True
    assert "health" not in worker  # no green noise


def test_health_flags_dead_heartbeat(tmp_bot_squad: Path, monkeypatch) -> None:
    """Missing heartbeat → worker.health flags dead_heartbeat."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha="matchsha")
    # no heartbeat file written
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    worker = r.json()["worker"]
    assert worker["alive"] is False
    assert "dead_heartbeat" in worker["health"]


def test_health_flags_sha_drift(tmp_bot_squad: Path, monkeypatch) -> None:
    """Fresh heartbeat but worker sha != API sha → worker.health flags sha_drift."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha="9" * 40)
    _write_heartbeat(tmp_bot_squad, CONVERGED_SHA + "\n")
    # T-0824: the worker IS on the deployed code here — this scenario is the api
    # container lagging, and naming the install tree is what makes that legible.
    _write_install(tmp_bot_squad, CONVERGED_SHA)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    worker = r.json()["worker"]
    assert worker["alive"] is True
    assert "sha_drift" in worker["health"]


def test_health_no_false_drift_when_sha_unknown(tmp_bot_squad: Path, monkeypatch) -> None:
    """Unknown API sha (or empty worker sha) must NOT raise a false drift alarm."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha="unknown")
    _write_heartbeat(tmp_bot_squad, CONVERGED_SHA + "\n")
    _write_install(tmp_bot_squad, CONVERGED_SHA)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    worker = r.json()["worker"]
    assert worker["alive"] is True
    assert "health" not in worker


# ---------------------------------------------------------------------------
# T-0739: restart_pending vs bare sha_drift
# ---------------------------------------------------------------------------


def _drifting_client(tmp_bot_squad: Path, monkeypatch):
    """A live worker whose heartbeat sha differs from the API image sha — i.e.
    REAL drift, the condition every case below has to interpret.

    T-0824: the install tree is pinned to the WORKER's sha here, which is not a
    convenience — it is what this fixture always meant. Every case below is about
    the api CONTAINER lagging (the shape measured on the 75dc01a deploy: worker/
    byte-identical, restart correctly skipped, old container still answering with
    its baked sha). The worker in these scenarios is running the deployed code,
    so `worker_stale` must stay silent throughout and `sha_drift` is the only
    comparison under test. A fixture that left the install term absent would
    instead add `install_sha_unknown` to all eighteen — true, but a different
    statement from the one each case is making.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", "b" * 40)
    wdir = tmp_bot_squad / "data" / "_worker"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "heartbeat").write_text("a" * 40)
    (wdir / "install_tree.json").write_text(
        json.dumps({"git_sha": "a" * 40, "at": time.time()})
    )
    return build_app(), wdir


def _flags(app) -> list:
    with TestClient(app) as client:
        return client.get("/api/health").json()["worker"].get("health", [])


def test_drift_with_no_pending_restart_is_a_bare_sha_drift(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The T-0717 shape — nothing is coming to fix this. THIS is the case that
    must keep breaching R-0005, and the reason T-0739 is not implemented as a
    blanket post-deploy grace period."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    assert _flags(app) == ["sha_drift"]


def test_drift_during_a_pending_restart_reports_restart_pending(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Both self-healing shapes: the restart FIRED and is in flight (what p281
    watched on 6b0fc20), and the restart was DEFERRED by T-0305's rate limiter."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    now = time.time()
    for name, state in (("restart_inflight.json", "in_flight"),
                        ("restart_pending.json", "deferred")):
        (wdir / name).write_text(json.dumps({"at": now, "expected_by": now + 180}))
        with TestClient(app) as client:
            worker = client.get("/api/health").json()["worker"]
        assert worker["health"] == ["restart_pending"]
        assert worker["restart"]["state"] == state
        assert worker["restart"]["overdue"] is False
        (wdir / name).unlink()


def test_an_overdue_restart_falls_back_to_sha_drift_and_says_why(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The bound on the whole mechanism. Past its deadline the marker no longer
    excuses the drift — the flag goes back to sha_drift so R-0005 is free to
    breach — but it is still REPORTED, because 'a restart has been owed since X
    and never landed' is exactly the line nobody had on the T-0717 night."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    (wdir / "restart_pending.json").write_text(
        json.dumps({"at": time.time() - 900, "expected_by": time.time() - 1})
    )
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["sha_drift"]
    assert worker["restart"]["overdue"] is True


def test_an_unreadable_marker_degrades_to_the_alarm(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """T-0717's degradation rule: a bad probe yields a false ALARM, never a
    false all-clear. Junk on disk must not be able to silence drift."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    for junk in ("{not json", "[]", '"str"', "", json.dumps({"at": "nonsense"})):
        (wdir / "restart_pending.json").write_text(junk)
        assert _flags(app) == ["sha_drift"]


def test_a_pending_restart_never_softens_a_dead_heartbeat(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A dead worker is actionable on its own — a restart it recorded before
    dying is not a reassurance, it is more evidence the restart failed."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    hb = wdir / "heartbeat"
    os.utime(hb, (time.time() - 9999, time.time() - 9999))
    now = time.time()
    (wdir / "restart_pending.json").write_text(
        json.dumps({"at": now, "expected_by": now + 400})
    )
    assert _flags(app) == ["dead_heartbeat"]


def test_no_drift_means_no_flags_even_with_a_stale_marker(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """T-0717 leg-1 regression guard: once the shas agree there is nothing to
    explain, so a leftover marker must not conjure a pill out of a healthy
    worker (the no-green-noise close, T-0456)."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", "b" * 40)
    wdir = tmp_bot_squad / "data" / "_worker"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "heartbeat").write_text("b" * 40)
    # T-0824: converged on all three sides — the row-3 shape.
    (wdir / "install_tree.json").write_text(
        json.dumps({"git_sha": "b" * 40, "at": time.time()})
    )
    now = time.time()
    (wdir / "restart_inflight.json").write_text(
        json.dumps({"at": now, "expected_by": now + 180})
    )
    with TestClient(build_app()) as client:
        worker = client.get("/api/health").json()["worker"]
    assert "health" not in worker
    assert "restart" not in worker


def test_the_later_deadline_wins_when_both_markers_exist(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The catch-up tick launches before clearing its deferral, so both markers
    briefly coexist. An overdue one must not mask a live one."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    now = time.time()
    (wdir / "restart_inflight.json").write_text(
        json.dumps({"at": now - 300, "expected_by": now - 1})
    )
    (wdir / "restart_pending.json").write_text(
        json.dumps({"at": now, "expected_by": now + 400})
    )
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["restart_pending"]
    assert worker["restart"]["state"] == "deferred"


# ---------------------------------------------------------------------------
# T-0754: deploy_pending vs bare sha_drift
#
# The case the restart markers CANNOT cover, because there is correctly no
# marker to write: a deploy whose worker/ subtree is byte-identical skips the
# restart, so the worker's effective sha follows the new checkout immediately
# while the OLD API container keeps answering /api/health with its own baked-in
# sha until it is recreated. Measured on the 75dc01a deploy: 62.3s of continuous
# bare sha_drift with marker=none on every one of 106 samples, on a deploy where
# nothing was wrong.
#
# The evidence used instead is the deploy job the API can already see — but only
# when its recorded `target_sha` equals the side that has ALREADY converged.
# "A deploy is happening" alone would excuse any drift that merely coincides
# with one, which is the blanket post-deploy grace this system has refused.
# ---------------------------------------------------------------------------

WORKER_SHA = "a" * 40    # what `_drifting_client` writes to the heartbeat
API_SHA = "b" * 40       # what it bakes into BOT_SQUAD_GIT_SHA


def _job(
    tmp_bot_squad: Path,
    *,
    target_sha: str | None = WORKER_SHA,
    slug: str = "bot-squad",
    state: str = "processing",
    queued_at: float | None = None,
    log_age: float | None = 5.0,
    **extra,
) -> Path:
    """Write a deploy job file the way `deploy.enqueue` + `run_next` would.

    ``log_age`` seconds ago is when the recipe last wrote to its run log — the
    deploy's sign of life. None = no log yet (a job still sitting in queue/).
    """
    queued_at = time.time() - 65 if queued_at is None else queued_at
    base = tmp_bot_squad / "data" / slug / "_jobs" / "deploy"
    (base / state).mkdir(parents=True, exist_ok=True)
    queue_id = f"qid-{state}-{slug}-{target_sha}"
    payload = {
        "queue_id": queue_id,
        "slug": slug,
        "target": "staging",
        "reason": "ship it",
        "requested_by": "S-test",
        "queued_at": queued_at,
        "restart_worker": False,
        **extra,
    }
    if target_sha is not None:
        payload["target_sha"] = target_sha
    path = base / state / f"{int(queued_at * 1000)}-{queue_id}.json"
    path.write_text(json.dumps(payload))
    if log_age is not None:
        (base / "runs").mkdir(parents=True, exist_ok=True)
        log = base / "runs" / f"{queue_id}.log"
        log.write_text("#5 [api 3/8] RUN pip install\n")
        os.utime(log, (time.time() - log_age, time.time() - log_age))
    return path


def test_drift_during_an_in_flight_deploy_of_that_commit_is_not_an_alarm(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """THE TICKET. The exact 75dc01a shape: worker already on the deployed
    commit, API not yet recreated, and no restart marker anywhere because no
    restart is owed. Today that is a bare sha_drift for a minute-plus."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    _job(tmp_bot_squad, target_sha=WORKER_SHA)
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["deploy_pending"]
    assert worker["deploy"]["state"] == "in_flight"
    assert worker["deploy"]["converged"] == "worker"
    assert worker["deploy"]["target_sha"] == WORKER_SHA
    assert worker["deploy"]["overdue"] is False
    assert "restart" not in worker    # nothing was owed; nothing is claimed


def test_the_deploy_may_be_the_one_the_API_has_already_landed(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The other direction, which p343's measurement corrected the model on:
    the converged side can be the API. Naming WHICH is what keeps the record
    diagnosable instead of just 'a deploy is happening'."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    _job(tmp_bot_squad, target_sha=API_SHA)
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["deploy_pending"]
    assert worker["deploy"]["converged"] == "api"


def test_a_deploy_of_a_DIFFERENT_commit_does_not_excuse_the_drift(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The line between this and the refused blanket grace. A deploy in flight
    is not evidence about THIS drift unless one side is already on its target;
    otherwise any drift that happened to coincide with a deploy would be
    silenced, which is the time-based excuse this system has refused."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    _job(tmp_bot_squad, target_sha="c" * 40)
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["sha_drift"]
    assert "deploy" not in worker


def test_a_payload_without_a_target_sha_buys_no_excuse(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A pre-T-0754 worker persisted no target_sha — every job file already on
    disk at deploy time looks like this. It must degrade to the ALARM, not to
    'a deploy is running, near enough'. Same for a malformed one: an
    unparseable probe yields a false alarm, never a false all-clear (T-0717)."""
    import shutil
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data" / "bot-squad" / "_jobs" / "deploy" / "processing"
    for bad in (None, "", "unknown", "a" * 39, "zzzzzzzz" * 5, WORKER_SHA[:12]):
        shutil.rmtree(d, ignore_errors=True)
        _job(tmp_bot_squad, target_sha=bad)
        assert _flags(app) == ["sha_drift"], f"target_sha={bad!r} bought an excuse"


def test_a_corrupt_job_file_degrades_to_the_alarm(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Junk on disk must not be able to silence drift."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data" / "bot-squad" / "_jobs" / "deploy" / "processing"
    d.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", "[]", '"str"', "", json.dumps({"target_sha": 5})):
        (d / "1-x.json").write_text(junk)
        assert _flags(app) == ["sha_drift"]


def test_a_stranded_deploy_stops_excusing_before_R0005_could_breach(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """THE BOUND, and it is pinned against a number rather than a feeling.

    A job stranded in processing/ (worker killed mid-run) is not swept for 2h.
    If that silenced health for 2h it would hide the wedged deploy R-0005 exists
    to catch. The excuse lapses 300s after the deploy's last sign of life —
    strictly inside R-0005's persist_s of 600 — so the monitor's power over a
    wedged deploy is unchanged. The block is still REPORTED while overdue,
    because 'a deploy of X has been in flight since 07:43 and never converged'
    is the headline for whoever gets paged."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    stale = time.time() - 1200
    _job(tmp_bot_squad, queued_at=stale, log_age=1200.0)
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["sha_drift"]
    assert worker["deploy"]["overdue"] is True
    from app.routes_health import _DEPLOY_PROGRESS_DEADLINE_SECONDS
    assert _DEPLOY_PROGRESS_DEADLINE_SECONDS < 600   # R-0005 persist_s


def test_a_long_but_LIVE_build_keeps_its_excuse(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The other half of that bound. signal-tracker's image build runs ~17min;
    anchoring the deadline on ENQUEUE would strip the excuse off every healthy
    long deploy. The anchor is the run log — the same sign of life the worker's
    own no-progress watchdog measures — so a build still writing keeps it."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    _job(tmp_bot_squad, queued_at=time.time() - 1200, log_age=3.0)
    assert _flags(app) == ["deploy_pending"]


def test_a_job_still_in_the_queue_counts(tmp_bot_squad: Path, monkeypatch) -> None:
    """queue/ as well as processing/ — a deploy that has not started yet still
    explains a drift its target sha matches, and expires on queued_at alone
    (which is also what expires a job parked behind a paused queue)."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    _job(tmp_bot_squad, state="queue", log_age=None)
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["deploy_pending"]
    assert worker["deploy"]["state"] == "queued"
    assert worker["deploy"]["last_progress"] is None


def test_another_projects_deploy_is_not_an_excuse(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The scan covers every slug rather than guessing which one owns this
    install — the sha match does the selecting, on evidence. watchrobot
    deploying its own commit says nothing about bot-squad's drift."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    _job(tmp_bot_squad, slug="watchrobot", target_sha="d" * 40)
    assert _flags(app) == ["sha_drift"]


def test_a_pending_restart_is_the_more_specific_statement_and_wins(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A restart_worker deploy has both. The restart marker names the action
    that will close the drift, so it leads — and the deploy block is then
    ABSENT because the API did not look, not because no deploy is running."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    now = time.time()
    (wdir / "restart_pending.json").write_text(
        json.dumps({"at": now, "expected_by": now + 300})
    )
    _job(tmp_bot_squad)
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["restart_pending"]
    assert "deploy" not in worker


def test_an_OVERDUE_restart_falls_through_to_the_deploy(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Once the restart marker lapses it no longer excuses anything, so the
    deploy gets its turn — a stale marker from an earlier deploy must not
    condemn the drift the CURRENT one is explaining."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    (wdir / "restart_pending.json").write_text(
        json.dumps({"at": time.time() - 900, "expected_by": time.time() - 1})
    )
    _job(tmp_bot_squad)
    with TestClient(app) as client:
        worker = client.get("/api/health").json()["worker"]
    assert worker["health"] == ["deploy_pending"]
    assert worker["restart"]["overdue"] is True   # still reported


def test_a_deploy_never_softens_a_dead_heartbeat(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A dead worker is actionable on its own. A deploy running through it is
    more evidence, not a reassurance."""
    app, wdir = _drifting_client(tmp_bot_squad, monkeypatch)
    os.utime(wdir / "heartbeat", (time.time() - 9999, time.time() - 9999))
    _job(tmp_bot_squad)
    assert _flags(app) == ["dead_heartbeat"]


def test_no_drift_means_no_flags_even_mid_deploy(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The no-green-noise close (T-0456). Once the shas agree there is nothing
    to explain, and a deploy still running must not conjure a pill."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", API_SHA)
    wdir = tmp_bot_squad / "data" / "_worker"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "heartbeat").write_text(API_SHA)
    # T-0824: converged on all THREE sides — the row-3 shape. Without the install
    # term this reads as `install_sha_unknown`, which is the honest answer to
    # "can you tell?" but not the answer this case is asserting.
    (wdir / "install_tree.json").write_text(
        json.dumps({"git_sha": API_SHA, "at": time.time()})
    )
    _job(tmp_bot_squad, target_sha=API_SHA)
    with TestClient(build_app()) as client:
        worker = client.get("/api/health").json()["worker"]
    assert "health" not in worker
    assert "deploy" not in worker


def test_no_job_dirs_at_all_is_a_plain_sha_drift(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A fresh install that has never deployed. The scan must return nothing and
    say nothing — not raise, and not invent an explanation."""
    app, _ = _drifting_client(tmp_bot_squad, monkeypatch)
    assert _flags(app) == ["sha_drift"]


# ---------------------------------------------------------------------------
# T-0824: the INSTALL TREE term — separating two byte-identical QUIET readings
# ---------------------------------------------------------------------------
# Measured on this install, twelve hours apart, by operator p502/p548:
#
#   row 1  12:56Z  api 3749ee8 · worker 3749ee8 · health ABSENT
#                  → the worker was 22 modules STALE. Dangerous.
#   row 2  13:06Z  api 3749ee8 · worker e18b1e5 · health ["sha_drift"]
#                  → benign api container lag. Fine.
#   row 3  18:53Z  api 5ae2811 · worker 5ae2811 · health ABSENT
#                  → genuinely converged. Fine.
#
# ROWS 1 AND 3 ARE THE SAME PAYLOAD and mean opposite things, and the flag is
# silent in the dangerous one and loud in the fine one. No arrangement of the
# two existing terms can separate them: in both rows those two terms are EQUAL.
# The tests below are written against that pair specifically — an assertion that
# "drift fires when the shas differ" would pass on the broken code and prove
# nothing, because rows 1 and 3 differ in NEITHER sha.
#
# ⚠ These worker shas are the EFFECTIVE sha (`deploy.effective_worker_git_sha`),
# which returns the frozen boot sha EARLY when boot == deployed. So on a
# converged install the worker sha and the tree sha agree TRIVIALLY, with no
# subtree probe having run. That agreement is not independent evidence and is
# not what any of these cases rests on.

INSTALL_SHA = "e" * 40   # what the deploy ff-merged into the install tree
BOOT_SHA = "3" * 40      # what the running worker actually loaded


def _three_sha_client(
    tmp_bot_squad: Path, monkeypatch, *, install: str | None, worker: str, api: str
):
    """A live worker with all three shas stated explicitly.

    ``install=None`` writes NO marker — a worker running code older than T-0824,
    or one that could not read the tree.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", api)
    wdir = tmp_bot_squad / "data" / "_worker"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "heartbeat").write_text(worker + "\n")
    if install is not None:
        (wdir / "install_tree.json").write_text(
            json.dumps({"git_sha": install, "at": time.time()})
        )
    return build_app(), wdir


def _payload(app) -> dict:
    with TestClient(app) as client:
        return client.get("/api/health").json()


def test_row3_a_converged_install_is_silent(tmp_bot_squad: Path, monkeypatch) -> None:
    """THE GREEN CONTROL, and it comes first on purpose: without it a red on
    row 1 proves only that something is broken, not that the right thing fires.

    All three sides on the same commit — the 18:53Z reading. Nothing is wrong and
    nothing may be said (the no-green-noise close, T-0456)."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch,
        install=INSTALL_SHA, worker=INSTALL_SHA, api=INSTALL_SHA,
    )
    body = _payload(app)
    assert body["worker"]["alive"] is True
    assert "health" not in body["worker"]
    assert body["install"]["git_sha"] == INSTALL_SHA


def test_row1_a_worker_pinned_to_boot_behind_the_install_tree_is_LOUD(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """THE TICKET. The 12:56Z reading, reconstructed exactly: the deploy ff-merged
    the tree, `worker/` changed so `effective_worker_git_sha` correctly PINNED to
    the boot sha, and the api container was deliberately not rebuilt — so the two
    published terms are EQUAL and the old comparison has nothing to fire on.

    This is the test that goes red without the install term: delete
    `_install_state`/the `worker_stale` branch and this payload becomes
    byte-identical to `test_row3_a_converged_install_is_silent`'s."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch,
        install=INSTALL_SHA, worker=BOOT_SHA, api=BOOT_SHA,
    )
    body = _payload(app)
    assert body["worker"]["health"] == ["worker_stale"]
    # The old comparison is silent here, and correctly so — it is asked a
    # different question and api == worker really is true.
    assert body["git_sha"] == body["worker"]["git_sha"] == BOOT_SHA
    assert body["install"]["git_sha"] == INSTALL_SHA


def test_rows_1_and_3_are_no_longer_the_same_reading(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The DoD's bar, asserted as one statement rather than inferred from two.

    Both rows have api == worker. Under the old surface their payloads differed
    in NOTHING — same keys, `health` absent in both. They must now differ, and
    differ on the flag, not merely somewhere. Read from the SAME install,
    reconfigured in place, so nothing but the three shas can account for it."""
    app1, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker=BOOT_SHA, api=BOOT_SHA,
    )
    row1 = _payload(app1)
    app3, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch,
        install=INSTALL_SHA, worker=INSTALL_SHA, api=INSTALL_SHA,
    )
    row3 = _payload(app3)
    assert row1["worker"].get("health") == ["worker_stale"]
    assert row3["worker"].get("health") is None
    # And the term that separates them is on the surface for a consumer to read,
    # not only inside the flag's derivation.
    assert row1["install"]["git_sha"] != row1["worker"]["git_sha"]
    assert row3["install"]["git_sha"] == row3["worker"]["git_sha"]


def test_a_missing_install_marker_is_an_EXPLICIT_unknown_never_a_quiet_pass(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The defect one level up, refused. A worker that publishes no install term
    leaves the endpoint unable to answer its own question — and rendering that as
    the quiet payload of a converged install would recreate exactly the
    indistinguishable pair this ticket exists to break. It is a flag, with the
    reason on the surface."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=None, worker=BOOT_SHA, api=BOOT_SHA,
    )
    body = _payload(app)
    assert body["worker"]["health"] == ["install_sha_unknown"]
    assert body["install"] == {"git_sha": None, "reason": "no_marker"}


def test_a_malformed_install_marker_buys_no_false_equality(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A short sha, a placeholder, or garbage is NOT a term. Degrading to the
    explicit unknown is the only safe reading: accepting it would let a
    comparison be computed off a value meaning "we could not look", and a false
    equality there is the false all-clear itself."""
    for body_text, reason in (
        ('{"git_sha": "e18b1e5"}', "malformed_marker"),
        ('{"git_sha": null}', "malformed_marker"),
        ('["not", "a", "dict"]', "unreadable_marker"),
        ("{not json at all", "unreadable_marker"),
    ):
        app, wdir = _three_sha_client(
            tmp_bot_squad, monkeypatch, install=None, worker=BOOT_SHA, api=BOOT_SHA,
        )
        (wdir / "install_tree.json").write_text(body_text)
        payload = _payload(app)
        assert payload["install"] == {"git_sha": None, "reason": reason}, body_text
        assert payload["worker"]["health"] == ["install_sha_unknown"], body_text


def test_worker_stale_and_sha_drift_are_INDEPENDENT_facts(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Three different shas — the state after a tree sync where neither process
    has caught up and the api was already behind. Both comparisons hold, and
    folding either into the other would drop a real statement: `worker_stale`
    means live BEHAVIOUR differs from what was shipped, `sha_drift` means the api
    CONTAINER is behind."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker=BOOT_SHA, api="d" * 40,
    )
    assert _payload(app)["worker"]["health"] == ["worker_stale", "sha_drift"]


def test_a_dead_worker_says_dead_and_does_not_guess_at_staleness(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A dead heartbeat is the actionable signal on its own, and a dead process's
    last-written sha says nothing about what it is running. Same gating the drift
    flags have had since T-0456 — the install term must not change it."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", BOOT_SHA)
    app = build_app()
    body = _payload(app)
    assert body["worker"]["health"] == ["dead_heartbeat"]
    # The term is still REPORTED — a responder wants to know what was deployed
    # even when the worker is down — it just does not raise a second flag.
    assert body["install"] == {"git_sha": None, "reason": "no_marker"}


def test_an_empty_heartbeat_body_still_raises_no_false_staleness(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """T-0456's rule, extended to the new comparison: an unknown worker sha (a
    pre-T-0456 worker, or a git-lookup fallback) is not evidence of anything."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker="", api=BOOT_SHA,
    )
    worker = _payload(app)["worker"]
    assert worker["alive"] is True
    assert "health" not in worker


def test_a_pending_restart_explains_worker_staleness(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The self-healing reading, and the reason `worker_stale` is not simply
    always-loud: a restart the worker RECORDED as owed, whose own deadline has
    not passed, names the action that closes this. Same bound as T-0739 — nothing
    is excused by elapsed time, only by a marker with time left on it."""
    app, wdir = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker=BOOT_SHA, api=BOOT_SHA,
    )
    now = time.time()
    (wdir / "restart_pending.json").write_text(
        json.dumps({"at": now, "expected_by": now + 180, "reason": "worker/ changed"})
    )
    body = _payload(app)
    assert body["worker"]["health"] == ["restart_pending"]
    assert body["worker"]["restart"]["overdue"] is False


def test_an_OVERDUE_restart_reverts_to_worker_stale(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A restart that was owed and never landed must end up looking like the
    alarm it is. This is the T-0717 failure with the install term added: the
    worker is on stale code and nothing is coming."""
    app, wdir = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker=BOOT_SHA, api=BOOT_SHA,
    )
    now = time.time()
    (wdir / "restart_pending.json").write_text(
        json.dumps({"at": now - 900, "expected_by": now - 600, "reason": "worker/ changed"})
    )
    body = _payload(app)
    assert body["worker"]["health"] == ["worker_stale"]
    # The lapsed marker still explains WHY someone is staring at this.
    assert body["worker"]["restart"]["overdue"] is True


def test_a_deploy_that_has_reached_the_TREE_explains_worker_staleness(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The window between a recipe's ff-merge and the restart it launches: the
    INSTALL side has reached the deploy's target and neither process has. Same
    evidential bar as the other two sides — the deploy's own recorded target_sha
    has to equal the sha of a side that actually reached it, so a deploy of some
    other commit still buys nothing."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker=BOOT_SHA, api=BOOT_SHA,
    )
    _job(tmp_bot_squad, target_sha=INSTALL_SHA)
    body = _payload(app)
    assert body["worker"]["health"] == ["deploy_pending"]
    assert body["worker"]["deploy"]["converged"] == "install"


def test_a_deploy_of_a_DIFFERENT_commit_does_not_excuse_worker_staleness(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """A deploy merely being in progress is not evidence about THIS staleness —
    the blanket grace this system has refused five times."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker=BOOT_SHA, api=BOOT_SHA,
    )
    _job(tmp_bot_squad, target_sha="c" * 40)
    assert _payload(app)["worker"]["health"] == ["worker_stale"]


def test_a_stranded_deploy_stops_excusing_staleness_before_R0005_could_breach(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """The bound is chosen against R-0005's own `persist_s: 600`. A job stranded
    in processing/ by a killed worker — not swept for 2h — must not hide a
    genuinely stale worker for those two hours."""
    app, _ = _three_sha_client(
        tmp_bot_squad, monkeypatch, install=INSTALL_SHA, worker=BOOT_SHA, api=BOOT_SHA,
    )
    _job(tmp_bot_squad, target_sha=INSTALL_SHA, queued_at=time.time() - 7200, log_age=3600)
    body = _payload(app)
    assert body["worker"]["health"] == ["worker_stale"]
    assert body["worker"]["deploy"]["overdue"] is True


def test_the_install_term_is_always_on_the_surface(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """Top-level and unconditional. A consumer — R-0005 included — must be able
    to answer "is the running worker executing the deployed code?" from the
    payload alone, in every state, without the key's PRESENCE being one of the
    things it has to interpret."""
    for install, worker, api in (
        (INSTALL_SHA, INSTALL_SHA, INSTALL_SHA),
        (INSTALL_SHA, BOOT_SHA, BOOT_SHA),
        (None, BOOT_SHA, BOOT_SHA),
    ):
        app, _ = _three_sha_client(
            tmp_bot_squad, monkeypatch, install=install, worker=worker, api=api,
        )
        body = _payload(app)
        assert "install" in body, (install, worker, api)
        assert "git_sha" in body["install"], (install, worker, api)
