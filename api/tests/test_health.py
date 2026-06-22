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
