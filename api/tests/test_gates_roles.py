"""T-0216 Phase A: the global-admin derivation bridge is quarantined behind one
helper (_derive_global_role) and behavior is preserved EXACTLY — global admin
iff (server admin AND MOTHERSHIP build). Removing this bridge is T-0228."""
import pytest

from app.config import UserMeta
from app.roles import GlobalRole, ServerRole
from app.routes_auth import _derive_global_role, _is_super_admin


def _admin() -> UserMeta:
    return UserMeta(linux_user="a", server_role=ServerRole.SERVER_ADMIN)


def _member() -> UserMeta:
    return UserMeta(linux_user="m", server_role=ServerRole.SERVER_MEMBER)


def test_server_admin_on_mothership_is_global_admin(monkeypatch) -> None:
    monkeypatch.setenv("MOTHERSHIP", "1")
    assert _derive_global_role(_admin()) is GlobalRole.GLOBAL_ADMIN
    assert _is_super_admin(_admin()) is True


def test_server_admin_off_mothership_is_not_global_admin(monkeypatch) -> None:
    monkeypatch.setenv("MOTHERSHIP", "0")
    assert _derive_global_role(_admin()) is GlobalRole.GLOBAL_MEMBER
    assert _is_super_admin(_admin()) is False


def test_server_member_on_mothership_is_not_global_admin(monkeypatch) -> None:
    monkeypatch.setenv("MOTHERSHIP", "1")
    assert _derive_global_role(_member()) is GlobalRole.GLOBAL_MEMBER
    assert _is_super_admin(_member()) is False


def test_server_member_off_mothership_is_not_global_admin(monkeypatch) -> None:
    monkeypatch.setenv("MOTHERSHIP", "0")
    assert _derive_global_role(_member()) is GlobalRole.GLOBAL_MEMBER
    assert _is_super_admin(_member()) is False
