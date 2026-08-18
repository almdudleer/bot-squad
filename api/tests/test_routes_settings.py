"""Tests for /api/system-settings — admin-only system config."""
from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _set_env(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(root / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    # T-0171: the mothership-attached state (which locks the TG fields) is read
    # from these envs. Clear them so the default test posture is "standalone /
    # detached" (editable) unless a test sets them explicitly.
    monkeypatch.delenv("MOTHERSHIP", raising=False)
    monkeypatch.delenv("BOT_SQUAD_MOTHERSHIP_URL", raising=False)
    monkeypatch.delenv("BOTSQUAD_MOTHERSHIP_URL", raising=False)
    # Need a secrets.toml for the api fixture (worker mounts it; for api it
    # only matters when reading bot_token_set).
    (root / "config" / "secrets.toml").write_text(
        '[telegram]\nbot_token = ""\nauth_age_max = 86400\n'
    )


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def test_unauthenticated_401(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/system-settings")
    assert r.status_code == 401


def test_non_admin_403(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        r = client.get("/api/system-settings")
    assert r.status_code == 403


def test_get_defaults_when_missing(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/system-settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tg"]["bot_token_set"] is False
    assert body["tg"]["quiet_hours_start_utc"] == 17
    assert body["tg"]["quiet_hours_end_utc"] == 5
    assert body["session"]["ttl"] == "7d"
    assert body["admin"]["coordinator_user"] == "almdudleer"


def test_put_writes_settings_and_creates_file(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={
                "tg": {"quiet_hours_start_utc": 18, "quiet_hours_end_utc": 6},
                "session": {"ttl": "14d"},
                "admin": {"coordinator_user": "almdudleer"},
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # T-0691: quiet_hours_start_utc/end_utc are boot-cached (TgClient/MaxClient
    # read them once at construction, and the module-level client singleton
    # isn't rebuilt by reload_projects) — a real change here DOES need a
    # restart, same as bot_token/proxy_url.
    assert body["restart_required"] is True
    assert body["tg"]["quiet_hours_start_utc"] == 18
    assert body["tg"]["quiet_hours_end_utc"] == 6
    assert body["session"]["ttl"] == "14d"
    # File on disk
    path = tmp_bot_squad / "config" / "system_settings.toml"
    raw = tomllib.loads(path.read_text())
    assert raw["tg"]["quiet_hours_start_utc"] == 18
    assert raw["tg"]["quiet_hours_end_utc"] == 6
    assert raw["session"]["ttl"] == "14d"


# ---------------------------------------------------------------------------
# PASS-2 P2-01-BE: restart_required must reflect the SOURCE OF TRUTH — true ONLY
# when a boot-cached field actually changed. Caps + ttl + coordinator are
# fresh-read per spawn/tick, so a caps-only save needs no restart (else the two
# caps editors contradict each other). T-0691: tg.bot_token / tg.proxy_url /
# tg.quiet_hours_start_utc / tg.quiet_hours_end_utc are ALL boot-cached (see
# TgClient/MaxClient.__init__) and DO need one.
# ---------------------------------------------------------------------------

def test_caps_only_save_needs_no_restart(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"max_parallel_sessions": 12, "max_total_tokens": 0}})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is False


def test_caps_idle_suspend_sec_round_trips(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0408: idle_suspend_sec is a [caps] field — PUT persists it, GET returns
    it, and (fresh-read per tick) it's a caps-only save with no restart."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"idle_suspend_sec": 43200}})
        assert r.status_code == 200, r.text
        assert r.json()["restart_required"] is False
        g = client.get("/api/system-settings")
    assert g.json()["caps"]["idle_suspend_sec"] == 43200


def test_caps_idle_suspend_sec_rejects_negative(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"idle_suspend_sec": -5}})
    assert r.status_code == 400


def test_proxy_url_change_requires_restart(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"tg": {"proxy_url": "socks5://10.0.0.1:1080"}})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is True


def test_proxy_url_unchanged_resend_needs_no_restart(tmp_bot_squad: Path, monkeypatch) -> None:
    """The System-Settings panel resends the whole tg form; a proxy_url matching
    the persisted value must NOT trigger a restart prompt."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        client.put("/api/system-settings", json={"tg": {"proxy_url": "socks5://10.0.0.1:1080"}})
        r = client.put("/api/system-settings",
                       json={"tg": {"proxy_url": "socks5://10.0.0.1:1080"}})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is False


def test_bot_token_change_requires_restart(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"bot_token": "123:newtoken"}})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is True


def test_quiet_hours_change_requires_restart(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0691: quiet_hours_start_utc/end_utc are boot-cached by TgClient/
    MaxClient — a live worker keeps using the OLD value (Config.load() via
    reload_projects doesn't reconstruct those module-level singletons), so a
    PUT changing them must report restart_required, not silently claim it
    took effect."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"tg": {"quiet_hours_start_utc": 20}})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is True


def test_quiet_hours_unchanged_resend_needs_no_restart(tmp_bot_squad: Path, monkeypatch) -> None:
    """The System-Settings panel resends the whole tg form; quiet_hours values
    matching the persisted ones must NOT trigger a restart prompt."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        client.put("/api/system-settings",
                    json={"tg": {"quiet_hours_start_utc": 20, "quiet_hours_end_utc": 8}})
        r = client.put("/api/system-settings",
                        json={"tg": {"quiet_hours_start_utc": 20, "quiet_hours_end_utc": 8}})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is False


def test_put_preserves_unmanaged_max_section(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0368: a settings save must NOT drop the [max] section (or any section
    this endpoint doesn't manage) — else operator->stakeholder DMs revert to
    TG-only (the T-0247 regression) on the next worker restart."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg_path = tmp_bot_squad / "config" / "system_settings.toml"
    cfg_path.write_text(
        '[tg]\nquiet_hours_start_utc = 22\nquiet_hours_end_utc = 4\n\n'
        '[max]\ndefault_chat_id = "211170965"\nrecipient_kind = "chat_id"\nproxy_url = ""\n'
    )
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"quiet_hours_start_utc": 18}})
    assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg_path.read_text())
    assert raw["tg"]["quiet_hours_start_utc"] == 18            # managed change applied
    assert raw["max"]["default_chat_id"] == "211170965"        # unmanaged section PRESERVED
    assert raw["max"]["recipient_kind"] == "chat_id"
    assert raw["max"]["proxy_url"] == ""


def test_put_is_atomic_rejected_request_persists_nothing(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0367: a PUT mixing a VALID change (caps) with an INVALID bot_token must
    400 and persist NOTHING — caps are spawn-time enforced, so a partial write
    would silently change the LIVE cap while reporting failure."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg_path = tmp_bot_squad / "config" / "system_settings.toml"
    cfg_path.write_text("[caps]\nmax_parallel_sessions = 7\nmax_total_tokens = 0\n")
    before = cfg_path.read_text()
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={"caps": {"max_parallel_sessions": 99}, "tg": {"bot_token": 12345}},  # bot_token not a str
        )
    assert r.status_code == 400, r.text
    # NOTHING persisted — the file is byte-identical, live cap untouched (7, not 99)
    assert cfg_path.read_text() == before
    raw = tomllib.loads(cfg_path.read_text())
    assert raw["caps"]["max_parallel_sessions"] == 7


def test_put_mothership_locked_409_persists_nothing(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0367: the mothership-lock refusal must be atomic too — a PUT mixing a
    VALID caps change with a locked field (bot_token) 409s and persists NOTHING
    (settings byte-identical, secrets untouched)."""
    _set_env(monkeypatch, tmp_bot_squad)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example/")
    cfg_path = tmp_bot_squad / "config" / "system_settings.toml"
    cfg_path.write_text("[caps]\nmax_parallel_sessions = 7\nmax_total_tokens = 0\n")
    before = cfg_path.read_text()
    secrets_path = tmp_bot_squad / "config" / "secrets.toml"
    secrets_before = secrets_path.read_text()
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={"caps": {"max_parallel_sessions": 99}, "tg": {"bot_token": "12345:ABC"}},
        )
    assert r.status_code == 409, r.text
    assert cfg_path.read_text() == before
    assert secrets_path.read_text() == secrets_before


def test_put_bot_token_writes_secrets(tmp_bot_squad: Path, monkeypatch) -> None:
    # No BOT_SQUAD_SECRETS_KEY (dev / fresh install) → plaintext passthrough.
    monkeypatch.delenv("BOT_SQUAD_SECRETS_KEY", raising=False)
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"bot_token": "12345:ABCDEF"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tg"]["bot_token_set"] is True
    raw = tomllib.loads((tmp_bot_squad / "config" / "secrets.toml").read_text())
    assert raw["telegram"]["bot_token"] == "12345:ABCDEF"


def test_put_bot_token_encrypts_at_rest_with_key(tmp_bot_squad: Path, monkeypatch) -> None:
    # T-0179: when a key is configured, the token is encrypted at rest (enc:
    # prefix) — the plaintext never lands on disk — and round-trips via decrypt.
    from cryptography.fernet import Fernet

    from app import secret_crypto

    monkeypatch.setenv("BOT_SQUAD_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"bot_token": "12345:ABCDEF"}})
    assert r.status_code == 200, r.text
    assert r.json()["tg"]["bot_token_set"] is True
    raw = tomllib.loads((tmp_bot_squad / "config" / "secrets.toml").read_text())
    stored = raw["telegram"]["bot_token"]
    assert stored.startswith("enc:")
    assert "12345:ABCDEF" not in stored  # plaintext is not on disk
    assert secret_crypto.decrypt(stored) == "12345:ABCDEF"


def test_put_bot_token_clear(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/system-settings", json={"tg": {"bot_token": "abc"}})
        r = client.put("/api/system-settings", json={"tg": {"bot_token": ""}})
    assert r.status_code == 200
    assert r.json()["tg"]["bot_token_set"] is False


def test_put_validates_quiet_hours(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"quiet_hours_start_utc": 99}})
        assert r.status_code == 400
        r = client.put("/api/system-settings", json={"tg": {"quiet_hours_end_utc": -1}})
        assert r.status_code == 400


def test_put_validates_ttl(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"session": {"ttl": "five days"}})
    assert r.status_code == 400


# --- T-0171: per-server default chat + lock-when-attached -------------------


def test_get_default_chat_and_not_managed_when_standalone(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/system-settings")
    body = r.json()
    assert body["tg"]["default_chat_id"] == ""
    assert body["tg"]["managed_by_mothership"] is False
    assert body["tg"]["mothership_url"] is None


def test_put_default_chat_id_roundtrip(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"tg": {"default_chat_id": "404580642"}})
        assert r.status_code == 200, r.text
        assert r.json()["tg"]["default_chat_id"] == "404580642"
        # persisted to system_settings.toml (non-secret)
        raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
        assert raw["tg"]["default_chat_id"] == "404580642"
        # survives a fresh GET
        assert client.get("/api/system-settings").json()["tg"]["default_chat_id"] == "404580642"


def test_tg_proxy_url_default_empty(tmp_bot_squad: Path, monkeypatch) -> None:
    # T-0194: no system_settings.toml → proxy is empty (direct egress).
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        body = client.get("/api/system-settings").json()
    assert body["tg"]["proxy_url"] == ""


def test_put_tg_proxy_url_roundtrip(tmp_bot_squad: Path, monkeypatch) -> None:
    # T-0194: admin sets + persists + clears the per-installation TG proxy.
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={"tg": {"proxy_url": "http://153.80.195.83:8888"}},
        )
        assert r.status_code == 200, r.text
        assert r.json()["tg"]["proxy_url"] == "http://153.80.195.83:8888"
        raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
        assert raw["tg"]["proxy_url"] == "http://153.80.195.83:8888"
        assert client.get("/api/system-settings").json()["tg"]["proxy_url"] == "http://153.80.195.83:8888"
        # socks5:// is accepted too
        assert client.put(
            "/api/system-settings", json={"tg": {"proxy_url": "socks5://10.0.0.1:1080"}}
        ).status_code == 200
        # cleared back to direct
        r = client.put("/api/system-settings", json={"tg": {"proxy_url": ""}})
        assert r.status_code == 200
        assert r.json()["tg"]["proxy_url"] == ""


def test_put_tg_proxy_url_rejects_bad_scheme(tmp_bot_squad: Path, monkeypatch) -> None:
    # T-0194: only socks5://, http://, https:// (or empty) are valid.
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        assert client.put(
            "/api/system-settings", json={"tg": {"proxy_url": "ftp://nope"}}
        ).status_code == 400
        assert client.put(
            "/api/system-settings", json={"tg": {"proxy_url": "153.80.195.83:8888"}}
        ).status_code == 400


def test_tg_proxy_url_editable_while_attached(tmp_bot_squad: Path, monkeypatch) -> None:
    # T-0194: the proxy is a host-network egress concern, independent of whose
    # bot token is used — so it is NOT locked while attached to a mothership.
    _set_env(monkeypatch, tmp_bot_squad)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example/")
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        # token + default_chat are locked (409) but proxy_url saves fine.
        assert client.put("/api/system-settings", json={"tg": {"bot_token": "x"}}).status_code == 409
        r = client.put(
            "/api/system-settings", json={"tg": {"proxy_url": "http://153.80.195.83:8888"}}
        )
        assert r.status_code == 200, r.text
        assert r.json()["tg"]["proxy_url"] == "http://153.80.195.83:8888"


def test_managed_when_attached_consumer_and_writes_refused(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    # Attached consumer: not the mothership, but pointed at one.
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example/")
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        body = client.get("/api/system-settings").json()
        assert body["tg"]["managed_by_mothership"] is True
        assert body["tg"]["mothership_url"] == "https://mom.example"
        # The locked fields cannot be written while attached.
        assert client.put("/api/system-settings", json={"tg": {"bot_token": "x"}}).status_code == 409
        assert client.put("/api/system-settings", json={"tg": {"default_chat_id": "1"}}).status_code == 409
        # Non-locked fields still save fine.
        assert client.put("/api/system-settings", json={"session": {"ttl": "1d"}}).status_code == 200


def test_not_managed_on_mothership_self(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    # The mothership itself owns @bot_squad_bot — token stays editable.
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example")
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        body = client.get("/api/system-settings").json()
        assert body["tg"]["managed_by_mothership"] is False
        # writes to the token are accepted (not locked)
        assert client.put("/api/system-settings", json={"tg": {"bot_token": "ok:tok"}}).status_code == 200


def test_get_does_not_leak_bot_token(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "secrets.toml").write_text(
        '[telegram]\nbot_token = "REAL:SECRET"\nauth_age_max = 86400\n'
    )
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/system-settings")
    body = r.json()
    assert body["tg"]["bot_token_set"] is True
    assert "bot_token" not in body["tg"]
    assert "REAL:SECRET" not in r.text


# ---------------------------------------------------------------------------
# T-0239 — user-settable resource caps (parallel sessions + token usage)
# ---------------------------------------------------------------------------

def test_get_caps_defaults_unlimited(tmp_bot_squad: Path, monkeypatch) -> None:
    """Fresh install: caps present in the GET contract, 0 = unlimited."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        body = client.get("/api/system-settings").json()
    assert body["caps"]["max_parallel_sessions"] == 0
    assert body["caps"]["max_total_tokens"] == 0


def test_put_caps_roundtrip(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={"caps": {"max_parallel_sessions": 5, "max_total_tokens": 1_000_000}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["caps"]["max_parallel_sessions"] == 5
        assert body["caps"]["max_total_tokens"] == 1_000_000
        # persisted + survives a re-read
        again = client.get("/api/system-settings").json()
        assert again["caps"]["max_parallel_sessions"] == 5
        assert again["caps"]["max_total_tokens"] == 1_000_000
    raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
    assert raw["caps"]["max_parallel_sessions"] == 5
    assert raw["caps"]["max_total_tokens"] == 1_000_000


def test_put_caps_partial_update_keeps_other(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.put("/api/system-settings",
                   json={"caps": {"max_parallel_sessions": 8, "max_total_tokens": 50}})
        # update only one field — the other persists
        body = client.put("/api/system-settings",
                          json={"caps": {"max_total_tokens": 99}}).json()
    assert body["caps"]["max_parallel_sessions"] == 8
    assert body["caps"]["max_total_tokens"] == 99


def test_put_caps_rejects_negative(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"max_parallel_sessions": -1}})
    assert r.status_code == 400
    assert "max_parallel_sessions" in r.text


def test_put_caps_rejects_non_int(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"max_total_tokens": "lots"}})
    assert r.status_code == 400
    assert "max_total_tokens" in r.text


# ---------------------------------------------------------------------------
# T-0418 (PASS-2 P2-20): a token cap with no [quota] anchor is a permanent
# ratchet — _output_since_anchor pins its baseline on an empty anchor_key and
# only grows, so once it hits the cap _enforce_token_cap refuses every spawn
# forever (the only "free" was a manual [quota].set_at edit in a different
# section). Arming the cap must auto-stamp the anchor: the OPEN names its free.
# ---------------------------------------------------------------------------

def test_arming_token_cap_auto_stamps_quota_anchor(tmp_bot_squad: Path, monkeypatch) -> None:
    """max_total_tokens armed >0 with no prior anchor → [quota].set_at stamped."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"max_total_tokens": 1_000_000}})
    assert r.status_code == 200, r.text
    raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
    set_at = (raw.get("quota") or {}).get("set_at", "")
    assert set_at, "arming a token cap with no anchor must auto-stamp [quota].set_at"


def test_arming_token_cap_preserves_existing_anchor(tmp_bot_squad: Path, monkeypatch) -> None:
    """An operator-set anchor is NOT clobbered when the cap is (re)saved."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(
        "[caps]\nmax_parallel_sessions = 0\nmax_total_tokens = 500\n"
        "[quota]\nbudget_tokens = 900\nset_at = \"2026-01-01T00:00:00Z\"\n"
    )
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"max_total_tokens": 750}})
    assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg.read_text())
    assert raw["quota"]["set_at"] == "2026-01-01T00:00:00Z", "must not clobber a deliberate anchor"
    assert raw["quota"]["budget_tokens"] == 900, "must preserve the burndown budget"


def test_token_cap_zero_does_not_stamp_anchor(tmp_bot_squad: Path, monkeypatch) -> None:
    """An unarmed cap (0 = unlimited) creates no anchor — nothing to free."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"caps": {"max_parallel_sessions": 5, "max_total_tokens": 0}})
    assert r.status_code == 200, r.text
    raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
    assert not (raw.get("quota") or {}).get("set_at", ""), "an unarmed token cap must not stamp an anchor"


# ---------------------------------------------------------------------------
# T-0894: [operator].weekly_quota_target_pct — the operator's weekly quota-
# utilization target. Before this, the endpoint hand-emitted exactly four
# sections and preserved everything else UNREAD, so there was no write path for
# this value from any session or role: it could only be set by hand-editing
# config/system_settings.toml on the host. The worker
# (operator_redrive.weekly_quota_target_pct) reads it fresh per call, so a PUT
# takes effect without a restart.
# ---------------------------------------------------------------------------

def test_get_operator_target_unset_is_null(tmp_bot_squad: Path, monkeypatch) -> None:
    """Fresh install: the field is in the GET contract and is null, not 0.

    null and 0 are DIFFERENT states — the reader spells "no target" as an absent
    key, while 0 would be a live 0% target that pins pace_verdict to "over"."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.get("/api/system-settings")
    assert r.status_code == 200, r.text
    assert r.json()["operator"]["weekly_quota_target_pct"] is None


def test_put_operator_target_roundtrip(tmp_bot_squad: Path, monkeypatch) -> None:
    """PUT persists it into [operator] and GET returns it — and the TOML holds
    the value under the exact key the worker's reader looks up."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": 81}})
        assert r.status_code == 200, r.text
        assert r.json()["operator"]["weekly_quota_target_pct"] == 81.0
        again = client.get("/api/system-settings")
        assert again.json()["operator"]["weekly_quota_target_pct"] == 81.0
    raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
    assert raw["operator"]["weekly_quota_target_pct"] == 81.0


def test_put_operator_target_accepts_float(tmp_bot_squad: Path, monkeypatch) -> None:
    """A percent is not a count — unlike [caps], a fractional value is valid."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": 20.5}})
        assert r.status_code == 200, r.text
    raw = tomllib.loads((tmp_bot_squad / "config" / "system_settings.toml").read_text())
    assert raw["operator"]["weekly_quota_target_pct"] == 20.5


def test_put_operator_target_null_clears_it(tmp_bot_squad: Path, monkeypatch) -> None:
    """Explicit null REMOVES the key — TOML has no null, and the worker's reader
    keys off absence. Writing 0 instead would be a live 0% target."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text("[operator]\nweekly_quota_target_pct = 81.0\n")
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": None}})
        assert r.status_code == 200, r.text
        assert r.json()["operator"]["weekly_quota_target_pct"] is None
    raw = tomllib.loads(cfg.read_text())
    assert "weekly_quota_target_pct" not in (raw.get("operator") or {})


def test_put_unrelated_section_keeps_operator_target(tmp_bot_squad: Path, monkeypatch) -> None:
    """A caps-only save must not drop a target someone already set — the
    T-0368 shape, now for a section this endpoint has started managing.

    This one passes on the pre-fix code too, by design: [operator] was already
    preserved as an unmanaged section, and the risk being pinned is that
    STARTING to manage it silently converts a preserved section into a
    hand-emitted one. It is a regression guard, not evidence of the new path."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text("[operator]\nweekly_quota_target_pct = 81.0\n")
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"caps": {"max_parallel_sessions": 3}})
        assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg.read_text())
    assert raw["operator"]["weekly_quota_target_pct"] == 81.0
    assert raw["caps"]["max_parallel_sessions"] == 3


def test_put_operator_target_preserves_sibling_keys(tmp_bot_squad: Path, monkeypatch) -> None:
    """[operator] is only PARTIALLY managed: this endpoint owns
    weekly_quota_target_pct and must leave every other key in that section
    alone. Adding "operator" to _MANAGED_SECTIONS would pass every other test
    here while silently dropping these — which is exactly the T-0368 bug."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(
        "[operator]\n"
        "weekly_quota_target_pct = 20.0\n"
        'some_future_knob = "keep-me"\n'
        "another_future_knob = 7\n"
    )
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": 81}})
        assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg.read_text())
    assert raw["operator"]["weekly_quota_target_pct"] == 81.0   # managed key changed
    assert raw["operator"]["some_future_knob"] == "keep-me"     # unmanaged PRESERVED
    assert raw["operator"]["another_future_knob"] == 7


def test_clearing_operator_target_preserves_sibling_keys(tmp_bot_squad: Path, monkeypatch) -> None:
    """Clearing the target drops only that key, never the section around it."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(
        '[operator]\nweekly_quota_target_pct = 20.0\nsome_future_knob = "keep-me"\n'
    )
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": None}})
        assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg.read_text())
    assert "weekly_quota_target_pct" not in raw["operator"]
    assert raw["operator"]["some_future_knob"] == "keep-me"


def test_put_operator_target_rejects_non_number(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        for bad in ("81", [81], {"pct": 81}):
            r = client.put("/api/system-settings",
                           json={"operator": {"weekly_quota_target_pct": bad}})
            assert r.status_code == 400, f"{bad!r} must be rejected: {r.text}"


def test_put_operator_target_rejects_bool(tmp_bot_squad: Path, monkeypatch) -> None:
    """isinstance(True, int) is True, so a bare number check would accept it and
    write `weekly_quota_target_pct = true` — same trap [caps] guards against."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": True}})
    assert r.status_code == 400, r.text


def test_put_operator_target_rejects_out_of_range(tmp_bot_squad: Path, monkeypatch) -> None:
    """It is compared against a spend-to-date PERCENT, so outside 0–100 it can
    only be a typo — and an unreachable target pins the verdict to "under"."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        for bad in (-1, 101, 8100):
            r = client.put("/api/system-settings",
                           json={"operator": {"weekly_quota_target_pct": bad}})
            assert r.status_code == 400, f"{bad!r} must be rejected: {r.text}"


def test_put_operator_target_accepts_the_bounds(tmp_bot_squad: Path, monkeypatch) -> None:
    """0 and 100 are legal targets — 0 means "spend nothing more this week"."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        for good in (0, 100):
            r = client.put("/api/system-settings",
                           json={"operator": {"weekly_quota_target_pct": good}})
            assert r.status_code == 200, r.text
            assert r.json()["operator"]["weekly_quota_target_pct"] == float(good)


def test_operator_target_save_needs_no_restart(tmp_bot_squad: Path, monkeypatch) -> None:
    """operator_redrive re-reads the TOML on every call (no boot cache), so the
    new target is live immediately — the endpoint must not claim otherwise.

    Asserts the WRITE as well as the flag on purpose: an endpoint that ignored
    the field entirely would also report restart_required False, so the flag
    alone passes vacuously (it does, on the pre-fix code)."""
    _set_env(monkeypatch, tmp_bot_squad)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": 81}})
    assert r.status_code == 200, r.text
    assert r.json()["operator"]["weekly_quota_target_pct"] == 81.0
    assert r.json()["restart_required"] is False


def test_rejected_operator_target_persists_nothing(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0367 atomicity, extended to the new field: a PUT mixing a VALID caps
    change with an INVALID target must 400 and write NOTHING."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(
        "[caps]\nmax_parallel_sessions = 7\nmax_total_tokens = 0\n"
        "[operator]\nweekly_quota_target_pct = 20.0\n"
    )
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={"caps": {"max_parallel_sessions": 99},
                  "operator": {"weekly_quota_target_pct": 900}},
        )
        assert r.status_code == 400, r.text
    raw = tomllib.loads(cfg.read_text())
    assert raw["caps"]["max_parallel_sessions"] == 7
    assert raw["operator"]["weekly_quota_target_pct"] == 20.0


def test_get_degrades_unusable_operator_target_to_null(tmp_bot_squad: Path, monkeypatch) -> None:
    """A hand-edited garbage value must not 500 the whole settings GET — the
    worker's reader already degrades it to "no target", so this agrees."""
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "system_settings.toml").write_text(
        '[operator]\nweekly_quota_target_pct = "not-a-number"\n'
    )
    with TestClient(build_app()) as client:
        _login(client)
        r = client.get("/api/system-settings")
    assert r.status_code == 200, r.text
    assert r.json()["operator"]["weekly_quota_target_pct"] is None


def test_put_normalizes_away_an_unusable_operator_target(tmp_bot_squad: Path, monkeypatch) -> None:
    """A garbage value in a MANAGED key is already "no target" to the worker, so
    the next write drops it rather than carrying a lie forward. Deliberate, and
    only for the managed key — the sibling is still untouched."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(
        '[operator]\nweekly_quota_target_pct = "not-a-number"\nsome_future_knob = "keep-me"\n'
    )
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"caps": {"max_parallel_sessions": 3}})
        assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg.read_text())
    assert "weekly_quota_target_pct" not in raw["operator"]
    assert raw["operator"]["some_future_knob"] == "keep-me"


# ---------------------------------------------------------------------------
# T-0910: the preserve loop emitted every key of an unmanaged section BARE, and
# a TOML bare key is limited to A-Za-z0-9_- . Since T-0866 the live config
# carries a catch-all key "*" in BOTH [models] and [effort], so ANY successful
# save re-emitted `* = "sonnet"` and the file stopped parsing for every reader
# — worker config load, fleet_model's role->model/effort resolution,
# GET /api/system-settings itself (500), and operator_redrive, which swallows
# the decode error and reports "no target". The PUT still returned ok:true,
# because nothing re-parses what it wrote.
#
# The suite was green over this because the only preserve fixture ([max]) has
# all-bare-legal keys. These fixtures carry the LIVE shape instead.
# ---------------------------------------------------------------------------

_LIVE_SHAPE_TOML = (
    '[caps]\nmax_parallel_sessions = 0\nmax_total_tokens = 0\nidle_suspend_sec = 0\n\n'
    '[operator]\nweekly_quota_target_pct = 70\n\n'
    '[models]\ndev = "opus"\nteamlead = "sonnet"\n"*" = "sonnet"\n\n'
    '[effort]\ndev = "high"\n"*" = "medium"\n'
)


def test_put_does_not_corrupt_a_config_with_quoted_keys(tmp_bot_squad: Path, monkeypatch) -> None:
    """The whole ticket in one assertion: after a save, the file must still
    PARSE. Without _toml_key this raises TOMLDecodeError on the `*` line."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(_LIVE_SHAPE_TOML)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings", json={"caps": {"max_parallel_sessions": 3}})
        assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg.read_text())          # <- the failing line pre-fix
    assert raw["models"]["*"] == "sonnet"
    assert raw["effort"]["*"] == "medium"
    assert raw["models"]["dev"] == "opus"          # bare keys still bare
    assert raw["caps"]["max_parallel_sessions"] == 3


def test_get_still_works_after_a_put_on_the_live_shape(tmp_bot_squad: Path, monkeypatch) -> None:
    """The corruption was silent at write time — the PUT reported ok:true and
    only the NEXT read failed. Pin that the next read succeeds."""
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "system_settings.toml").write_text(_LIVE_SHAPE_TOML)
    with TestClient(build_app()) as client:
        _login(client)
        assert client.put("/api/system-settings",
                          json={"session": {"ttl": "3d"}}).status_code == 200
        g = client.get("/api/system-settings")
    assert g.status_code == 200, g.text
    assert g.json()["session"]["ttl"] == "3d"


def test_operator_target_write_survives_the_live_shape(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0894 x T-0910: the first-ever writer of weekly_quota_target_pct goes
    through the same preserve loop, so on the real config it would have set the
    target AND made it unreadable in the same call — and operator_redrive
    reports an unparseable file as "no target", i.e. exactly the symptom
    T-0894 exists to remove. 70 -> 81 is the live change."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(_LIVE_SHAPE_TOML)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.put("/api/system-settings",
                       json={"operator": {"weekly_quota_target_pct": 81}})
        assert r.status_code == 200, r.text
    raw = tomllib.loads(cfg.read_text())
    assert raw["operator"]["weekly_quota_target_pct"] == 81.0
    assert raw["models"]["*"] == "sonnet"
    assert raw["effort"]["*"] == "medium"


def test_put_preserves_list_and_table_values(tmp_bot_squad: Path, monkeypatch) -> None:
    """T-0910: list/dict values used to fall through to the str() branch and be
    rewritten as a Python repr INSIDE a quoted string — it parses, so nothing
    complained, but the reader got text where it had written a list."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(
        '[future]\n'
        'allowed = ["a", "b"]\n'
        'counts = [1, 2, 3]\n'
        'nested = { left = "l", right = 2 }\n'
    )
    with TestClient(build_app()) as client:
        _login(client)
        assert client.put("/api/system-settings",
                          json={"session": {"ttl": "3d"}}).status_code == 200
    raw = tomllib.loads(cfg.read_text())
    assert raw["future"]["allowed"] == ["a", "b"], "must stay a list, not become its repr"
    assert raw["future"]["counts"] == [1, 2, 3]
    assert raw["future"]["nested"] == {"left": "l", "right": 2}


def test_put_quotes_other_non_bare_keys_too(tmp_bot_squad: Path, monkeypatch) -> None:
    """The fix is a general predicate on the key, not a special case for "*" —
    a dotted or spaced key round-trips as one key, not as a path."""
    _set_env(monkeypatch, tmp_bot_squad)
    cfg = tmp_bot_squad / "config" / "system_settings.toml"
    cfg.write_text(
        '[future]\n'
        '"a.b" = "dotted"\n'
        '"has space" = "spaced"\n'
        '"*" = "star"\n'
        'plain-key_1 = "bare"\n'
    )
    with TestClient(build_app()) as client:
        _login(client)
        assert client.put("/api/system-settings",
                          json={"session": {"ttl": "3d"}}).status_code == 200
    text = cfg.read_text()
    raw = tomllib.loads(text)
    assert raw["future"] == {
        "a.b": "dotted", "has space": "spaced", "*": "star", "plain-key_1": "bare",
    }
    assert "\nplain-key_1 = " in text, "a bare-legal key must NOT be needlessly quoted"
