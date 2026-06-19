"""T-0216 Phase A: UserMeta carries an explicit ServerRole (replacing the
overloaded is_admin bool), with dual-read back-compat + an is_admin property
so every existing reader keeps working unchanged (behavior-preserving)."""
from pathlib import Path

from app.config import AuthConfig, UserMeta
from app.roles import ServerRole, server_role_for


def _write_auth(tmp_bot_squad: Path, user_meta_block: str) -> AuthConfig:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        f'{user_meta_block}'
        '[session]\nttl = "7d"\n'
    )
    return AuthConfig.load(tmp_bot_squad / "config")


def test_server_role_for_maps_bool() -> None:
    assert server_role_for(True) is ServerRole.SERVER_ADMIN
    assert server_role_for(False) is ServerRole.SERVER_MEMBER


def test_usermeta_default_is_member() -> None:
    m = UserMeta(linux_user="x")
    assert m.server_role is ServerRole.SERVER_MEMBER
    assert m.is_admin is False


def test_usermeta_is_admin_property_reflects_role() -> None:
    assert UserMeta(linux_user="x", server_role=ServerRole.SERVER_ADMIN).is_admin is True
    assert UserMeta(linux_user="x", server_role=ServerRole.SERVER_MEMBER).is_admin is False


def test_load_dual_read_legacy_is_admin_true(tmp_bot_squad: Path) -> None:
    # un-migrated row: only is_admin = true on disk
    auth = _write_auth(
        tmp_bot_squad,
        '[user_meta.testuser]\nlinux_user = "almdudleer"\nis_admin = true\n',
    )
    meta = auth.meta_for("testuser")
    assert meta.server_role is ServerRole.SERVER_ADMIN
    assert meta.is_admin is True


def test_load_dual_read_legacy_is_admin_false(tmp_bot_squad: Path) -> None:
    auth = _write_auth(
        tmp_bot_squad,
        '[user_meta.testuser]\nlinux_user = "almdudleer"\nis_admin = false\n',
    )
    assert auth.meta_for("testuser").server_role is ServerRole.SERVER_MEMBER
    assert auth.meta_for("testuser").is_admin is False


def test_load_prefers_explicit_server_role(tmp_bot_squad: Path) -> None:
    # migrated row: explicit server_role wins even if is_admin disagrees
    auth = _write_auth(
        tmp_bot_squad,
        '[user_meta.testuser]\nlinux_user = "almdudleer"\n'
        'is_admin = false\nserver_role = "server_admin"\n',
    )
    meta = auth.meta_for("testuser")
    assert meta.server_role is ServerRole.SERVER_ADMIN
    assert meta.is_admin is True


def test_meta_for_default_is_member(tmp_bot_squad: Path) -> None:
    auth = AuthConfig.load(tmp_bot_squad / "config")
    # an unknown user gets the safe default: server member, not admin
    assert auth.meta_for("nobody").server_role is ServerRole.SERVER_MEMBER
    assert auth.meta_for("nobody").is_admin is False
