from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client_logged_in(tmp_bot_squad: Path, monkeypatch):
    import hashlib, hmac, time
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    bot_token = "TESTBOT:TOKEN"
    base = {"id": 12345, "first_name": "Alexey", "auth_date": int(time.time())}
    secret = hashlib.sha256(bot_token.encode()).digest()
    s = "\n".join(f"{k}={base[k]}" for k in sorted(base))
    base["hash"] = hmac.new(secret, s.encode(), hashlib.sha256).hexdigest()
    client.post("/api/auth/tg", json=base)
    return client


def test_backlog_returns_parsed_tasks(tmp_bot_squad: Path, monkeypatch, fake_worker_tg):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-foo.md").write_text(
        "---\nid: T-0001\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )
    (backlog / "T-0002-bar.md").write_text(
        "---\nid: T-0002\ntitle: Bar\nstatus: closed\n---\n\nbody\n"
    )
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 2
    by_id = {t["id"]: t for t in tasks}
    assert by_id["T-0001"]["status"] == "open"
    assert by_id["T-0002"]["title"] == "Bar"


def test_backlog_skips_unparseable(tmp_bot_squad: Path, monkeypatch, fake_worker_tg):
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-bad.md").write_text("no frontmatter")
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/backlog")
    # Bad files are reported but don't 500.
    assert r.status_code == 200
    assert r.json() == []
