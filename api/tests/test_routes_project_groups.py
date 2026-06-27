"""T-0496: per-project user-group management routes (/api/m/projects/...).

Mirrors ``test_routes_conversations.py``: project-access-gated (admin manages),
mothership-only. Covers group CRUD, membership, the T-0478 seam lookup, the
authz gate (non-admin denied), and route-absence off the mothership build.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    monkeypatch.setenv("MOTHERSHIP", "1")
    return TestClient(build_app())


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def _make_nonadmin(tmp_bot_squad: Path) -> None:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )


GROUPS = "/api/m/projects/test-project/groups"


def _create(client: TestClient, **body) -> dict:
    body.setdefault("name", "developers")
    r = client.post(GROUPS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- group CRUD -------------------------------------------------------------


def test_create_and_list_group(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    g = _create(client, name="developers", role="developer",
                access_scope="full bot-squad control", prompt="you are a dev")
    assert g["id"].startswith("grp_")
    assert g["prompt"] == "you are a dev"

    r = client.get(GROUPS)
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["groups"]] == [g["id"]]


def test_create_duplicate_name_is_400(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _create(client, name="developers")
    r = client.post(GROUPS, json={"name": "developers"})
    assert r.status_code == 400


def test_create_empty_name_is_400(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    r = client.post(GROUPS, json={"name": ""})
    assert r.status_code == 400


def test_get_group(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    g = _create(client)
    r = client.get(f"{GROUPS}/{g['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == g["id"]
    assert client.get(f"{GROUPS}/grp_nope").status_code == 404


def test_update_group_partial(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    g = _create(client, name="support", role="support", prompt="old")
    r = client.patch(f"{GROUPS}/{g['id']}", json={"prompt": "new"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prompt"] == "new"
    assert body["role"] == "support"  # untouched


def test_update_unknown_group_is_404(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    r = client.patch(f"{GROUPS}/grp_nope", json={"prompt": "x"})
    assert r.status_code == 404


def test_delete_group(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    g = _create(client)
    r = client.delete(f"{GROUPS}/{g['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] == g["id"]
    # idempotency -> 404 on the second delete.
    assert client.delete(f"{GROUPS}/{g['id']}").status_code == 404


def test_unknown_project_is_404(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    r = client.get("/api/m/projects/no-such-project/groups")
    assert r.status_code == 404


# ---- membership + seam ------------------------------------------------------


def test_membership_lifecycle_and_seam(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    g = _create(client, name="developers", role="developer", prompt="dev prompt")

    # Bind a user.
    r = client.put(f"{GROUPS}/{g['id']}/members/gu_abc")
    assert r.status_code == 200, r.text

    # List members.
    r = client.get(f"{GROUPS}/{g['id']}/members")
    assert r.status_code == 200
    assert r.json()["members"] == ["gu_abc"]

    # Seam lookup: which group is the user in (with role/access/prompt).
    r = client.get("/api/m/projects/test-project/members/gu_abc/group")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group"]["id"] == g["id"]
    assert body["group"]["prompt"] == "dev prompt"

    # Remove membership.
    r = client.delete("/api/m/projects/test-project/members/gu_abc")
    assert r.status_code == 200
    r = client.get("/api/m/projects/test-project/members/gu_abc/group")
    assert r.json()["group"] is None


def test_seam_lookup_none_when_no_membership(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    r = client.get("/api/m/projects/test-project/members/gu_nobody/group")
    assert r.status_code == 200
    assert r.json()["group"] is None


def test_set_membership_unknown_group_is_400(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    r = client.put(f"{GROUPS}/grp_nope/members/gu_abc")
    assert r.status_code == 400


def test_remove_membership_not_member_is_404(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    r = client.delete("/api/m/projects/test-project/members/gu_abc")
    assert r.status_code == 404


# ---- authz ------------------------------------------------------------------


def test_routes_require_auth(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)  # no login
    assert client.get(GROUPS).status_code == 401
    assert client.post(GROUPS, json={"name": "x"}).status_code == 401


def test_nonadmin_denied_writes_and_reads(tmp_bot_squad: Path, monkeypatch):
    """Project-access gate: a non-admin (project-limited user) is denied group
    management AND the private group read (require_project_member / _read,
    admin-only today — see app.project_authz)."""
    _make_nonadmin(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    assert client.post(GROUPS, json={"name": "x"}).status_code == 403
    assert client.get(GROUPS).status_code == 403
    assert client.put(f"{GROUPS}/grp_x/members/gu_abc").status_code == 403
    assert client.get("/api/m/projects/test-project/members/gu_abc/group").status_code == 403


def test_routes_absent_when_not_mothership(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.delenv("MOTHERSHIP", raising=False)
    client = TestClient(build_app())
    # Off the mothership build the router never mounts -> 404 (not 401/403).
    assert client.get(GROUPS).status_code == 404
