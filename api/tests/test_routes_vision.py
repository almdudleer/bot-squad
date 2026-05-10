from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _logged_in(tmp_bot_squad: Path, monkeypatch):
    import hashlib, hmac, time
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    c = TestClient(build_app())
    base = {"id": 12345, "first_name": "Alexey", "auth_date": int(time.time())}
    secret = hashlib.sha256("TESTBOT:TOKEN".encode()).digest()
    s = "\n".join(f"{k}={base[k]}" for k in sorted(base))
    base["hash"] = hmac.new(secret, s.encode(), hashlib.sha256).hexdigest()
    c.post("/api/auth/tg", json=base)
    return c


def test_vision_lists_files_and_initiatives(tmp_bot_squad: Path, monkeypatch, fake_worker_tg):
    vision = tmp_bot_squad / "data" / "test-project" / "vision"
    (vision / "north-star.md").write_text("# North star\n\nAim true.\n")
    (vision / "strategy.md").write_text("# Strategy\n\nBet on X.\n")
    (vision / "initiatives").mkdir()
    (vision / "initiatives" / "v0.6.md").write_text("# v0.6\n")
    with _logged_in(tmp_bot_squad, monkeypatch) as c:
        r = c.get("/api/projects/test-project/vision")
    assert r.status_code == 200
    names = [x["name"] for x in r.json()]
    assert "north-star.md" in names
    assert "strategy.md" in names
    assert "initiatives/v0.6.md" in names
