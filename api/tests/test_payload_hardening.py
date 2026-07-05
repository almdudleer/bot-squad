"""T-0600 (N1): no route — unauthenticated first — may 500 on a malformed body.

The F2 500-class: ``(payload.get("x") or "").strip()`` let any truthy
non-string (``123``, ``["a"]``, ``true``) survive ``or ""`` and crash on
``.strip()`` — reachable pre-auth on POST /api/auth/login (feedback
F-2026-07-05-bsq-eec6cd6069, T-0439 pass-2). The shared guard is
``app.payload_guard``; a source-level sweep below keeps the pattern from
creeping back into any hand-rolled dict route.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import build_app
from app.payload_guard import opt_str_field, str_field


def _env(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")


def _anon_client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    _env(tmp_bot_squad, monkeypatch)
    return TestClient(build_app())


def _client_logged_in(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    _env(tmp_bot_squad, monkeypatch)
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


# ---------------------------------------------------------------------------
# payload_guard unit behaviour
# ---------------------------------------------------------------------------

def test_str_field_missing_and_none_map_to_empty():
    assert str_field({}, "x") == ""
    assert str_field({"x": None}, "x") == ""


def test_str_field_strips_by_default_but_not_secrets():
    assert str_field({"x": "  a  "}, "x") == "a"
    assert str_field({"x": "  a  "}, "x", strip=False) == "  a  "


@pytest.mark.parametrize("bad", [123, 1.5, True, False, ["a"], {"a": 1}])
def test_str_field_rejects_non_strings_with_400(bad):
    with pytest.raises(HTTPException) as exc:
        str_field({"x": bad}, "x")
    assert exc.value.status_code == 400
    assert "x must be a string" in exc.value.detail


@pytest.mark.parametrize("bad", [123, True, ["a"], {"a": 1}])
def test_opt_str_field_rejects_non_strings_with_400(bad):
    with pytest.raises(HTTPException) as exc:
        opt_str_field({"x": bad}, "x")
    assert exc.value.status_code == 400


def test_opt_str_field_preserves_absent_vs_empty():
    assert opt_str_field({}, "x") is None
    assert opt_str_field({"x": None}, "x") is None
    assert opt_str_field({"x": ""}, "x") == ""
    assert opt_str_field({"x": " a "}, "x") == " a "  # bodies keep whitespace


# ---------------------------------------------------------------------------
# The pre-auth regression: POST /api/auth/login (F-2026-07-05-bsq-eec6cd6069)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("username", [123, ["a"], True, {"u": 1}])
def test_login_wrong_typed_username_is_400_not_500(tmp_bot_squad, monkeypatch, username):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.post("/api/auth/login", json={"username": username, "password": "x"})
    assert r.status_code == 400
    assert r.json()["detail"] == "username must be a string"


def test_login_wrong_typed_password_is_400_not_500(tmp_bot_squad, monkeypatch):
    # An int password used to blow up inside bcrypt (TypeError) even for a
    # real username — also a pre-auth 500.
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.post("/api/auth/login", json={"username": "testuser", "password": 123})
    assert r.status_code == 400
    assert r.json()["detail"] == "password must be a string"


def test_login_non_dict_body_is_422(tmp_bot_squad, monkeypatch):
    # FastAPI's own dict validation already covers the whole-body case.
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.post("/api/auth/login", json=["a"])
    assert r.status_code == 422


def test_login_still_works_and_still_400s_on_missing_fields(tmp_bot_squad, monkeypatch):
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        ok = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        missing = client.post("/api/auth/login", json={})
    assert ok.status_code == 200
    assert missing.status_code == 400


def test_unauth_release_telemetry_wrong_typed_install_id_is_400(tmp_bot_squad, monkeypatch):
    # The other cookie-less front door (autoupdate poller endpoint).
    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.post("/api/releases/_telemetry", json={"install_id": 123})
    assert r.status_code == 400
    assert r.json()["detail"] == "install_id must be a string"


# ---------------------------------------------------------------------------
# Sweep: wrong-typed title/body/sid/text across backlog + progress routes
# (the confirmed-live 500s from T-0439 pass-2) all 4xx now.
# ---------------------------------------------------------------------------

def _seed_task(tmp_bot_squad: Path) -> str:
    backlog = tmp_bot_squad / "data" / "test-project" / "backlog"
    (backlog / "T-0001-seed.md").write_text(
        "---\nid: T-0001\ntitle: Seed task\nstatus: open\n---\n\nbody\n"
    )
    return "T-0001"


BACKLOG = "/api/projects/test-project/backlog"

_SWEEP_CASES = [
    ("POST", BACKLOG, {"title": 123}),
    ("POST", BACKLOG, {"title": ["a"]}),
    ("POST", BACKLOG, {"title": True}),
    ("POST", BACKLOG, {"title": "typed body sweep probe", "body": 123}),
    ("POST", BACKLOG, {"title": "typed verbatim sweep probe", "verbatim_request": 123}),
    ("POST", BACKLOG, {"title": "typed force sweep probe", "force": "yes"}),
    ("PATCH", BACKLOG + "/T-0001", {"title": 123}),
    ("PATCH", BACKLOG + "/T-0001", {"body": 123}),
    ("POST", BACKLOG + "/T-0001/progress", {"sid": 123, "text": "x"}),
    ("POST", BACKLOG + "/T-0001/progress", {"sid": "S-x", "text": 123}),
]


@pytest.mark.parametrize("method,url,body", _SWEEP_CASES)
def test_backlog_and_progress_wrong_typed_fields_are_400(
    tmp_bot_squad, monkeypatch, method, url, body
):
    _seed_task(tmp_bot_squad)
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.request(method, url, json=body)
    assert r.status_code == 400, f"{method} {url} {body} -> {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Source-level closed-set sweep: the crash pattern must not creep back.
# (A flat app.routes enumeration is vacuous on this FastAPI version — routers
# hide behind _IncludedRouter — so we lint the source instead: every dict-
# payload field read goes through payload_guard.)
# ---------------------------------------------------------------------------

# (?<!str) — `str(payload.get(...) or "").strip()` coerces first and is safe.
_RAW_STRIP = re.compile(r"(?<!str)\(payload\.get\([^)]*\) or \"\"\)\.strip\(\)")
_RAW_PASSWORD = re.compile(r"payload\.get\(\"[^\"]*password[^\"]*\"\) or \"\"")


def test_no_route_source_uses_raw_payload_strip_pattern():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for f in sorted(app_dir.glob("routes_*.py")):
        src = f.read_text(encoding="utf-8")
        for rx in (_RAW_STRIP, _RAW_PASSWORD):
            for m in rx.finditer(src):
                # str(...)-coerced reads are safe; the regexes above only
                # match the bare crash-prone forms.
                line_no = src[: m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line_no}: {m.group(0)}")
    assert not offenders, (
        "raw `(payload.get(...) or \"\").strip()` / bare password reads found — "
        "use app.payload_guard.str_field (T-0600):\n" + "\n".join(offenders)
    )
