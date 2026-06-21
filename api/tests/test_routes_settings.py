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
    # PASS-2 P2-01-BE: quiet_hours/ttl/coordinator are fresh-read by their
    # consumers — no worker restart needed (only boot-cached bot_token/proxy_url
    # require one).
    assert body["restart_required"] is False
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
# when a boot-cached field (tg.bot_token / tg.proxy_url) actually changed. Caps +
# ttl + quiet_hours + coordinator are fresh-read per spawn/tick, so a caps-only
# save needs no restart (else the two caps editors contradict each other).
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
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put(
            "/api/system-settings",
            json={"caps": {"max_parallel_sessions": 99}, "tg": {"bot_token": 12345}},  # bot_token not a str
        )
    assert r.status_code == 400, r.text
    # NOTHING persisted — the live cap is untouched (still 7, not 99)
    raw = tomllib.loads(cfg_path.read_text())
    assert raw["caps"]["max_parallel_sessions"] == 7


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
