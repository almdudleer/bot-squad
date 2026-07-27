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


def test_health_surfaces_worker_sha(tmp_bot_squad: Path, monkeypatch) -> None:
    """The worker's boot sha (heartbeat body) is surfaced under worker.git_sha."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha="matchsha")
    _write_heartbeat(tmp_bot_squad, "matchsha\n")
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.json()["worker"]["git_sha"] == "matchsha"


def test_health_worker_ok_has_no_health_alarm(tmp_bot_squad: Path, monkeypatch) -> None:
    """Failure-only: a fresh heartbeat with a MATCHING sha emits no worker.health."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha="matchsha")
    _write_heartbeat(tmp_bot_squad, "matchsha\n")
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
    _health_env(tmp_bot_squad, monkeypatch, api_sha="apisha999")
    _write_heartbeat(tmp_bot_squad, "workersha111\n")
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    worker = r.json()["worker"]
    assert worker["alive"] is True
    assert "sha_drift" in worker["health"]


def test_health_no_false_drift_when_sha_unknown(tmp_bot_squad: Path, monkeypatch) -> None:
    """Unknown API sha (or empty worker sha) must NOT raise a false drift alarm."""
    _health_env(tmp_bot_squad, monkeypatch, api_sha="unknown")
    _write_heartbeat(tmp_bot_squad, "workersha111\n")
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    worker = r.json()["worker"]
    assert worker["alive"] is True
    assert "health" not in worker


# ---------------------------------------------------------------------------
# T-0739: restart_pending vs bare sha_drift
# ---------------------------------------------------------------------------

import json
import os
import time


def _drifting_client(tmp_bot_squad: Path, monkeypatch):
    """A live worker whose heartbeat sha differs from the API image sha — i.e.
    REAL drift, the condition every case below has to interpret."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SQUAD_GIT_SHA", "b" * 40)
    wdir = tmp_bot_squad / "data" / "_worker"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "heartbeat").write_text("a" * 40)
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
