"""Per-user routing for Telegram-created user-conversation sessions."""
from __future__ import annotations

import os
import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import actions
from bot_squad_worker import tg_listener as listener


def _cfg(tmp_path: Path) -> SimpleNamespace:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    (data_dir / "_sock").mkdir(parents=True)
    (config_dir / "auth.toml").write_text(
        """
[user_meta.flomaster]
linux_user = "flomaster"
attached_to_global_user = "gu_flomaster"
""".strip()
    )
    return SimpleNamespace(config_dir=config_dir, data_dir=data_dir)


def test_resolves_attached_global_user_to_linux_user(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert listener._linux_user_for_global_user(cfg, "gu_flomaster") == "flomaster"
    assert listener._linux_user_for_global_user(cfg, "gu_unknown") == ""


def test_an_unresolved_sender_gets_no_user_worker_route(tmp_path: Path) -> None:
    """Identity resolution is best-effort and yields "" on failure, and that
    empty string reaches this function: `handle_update` sets
    ``gid = identity.get("global_user_id") if identity else ""`` and calls
    `_handle_unquoted` with it unguarded.

    The live auth.toml shape is what makes that dangerous — MOST accounts carry
    no ``attached_to_global_user``, so "" compares equal to every one of them
    and an unguarded match returns whichever auth.toml lists FIRST. That is a
    routing decision made by file order: on this install the first entry
    happens to be the coordinator's own account, so it looks correct for a
    reason that could change with an unrelated config edit.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    (data_dir / "_sock").mkdir(parents=True)
    (config_dir / "auth.toml").write_text(
        # Deliberately mirrors live: an UNATTACHED account listed before the
        # attached one. With the guard removed this returns "aqice".
        """
[user_meta.aqice]
linux_user = "aqice"

[user_meta.flomaster]
linux_user = "flomaster"
attached_to_global_user = "gu_flomaster"
""".strip()
    )
    cfg = SimpleNamespace(config_dir=config_dir, data_dir=data_dir)

    assert listener._linux_user_for_global_user(cfg, "") == ""
    assert listener._linux_user_for_global_user(cfg, None) == ""
    assert listener._linux_user_for_global_user(cfg, "   ") == ""
    # and the real lookup still works past the guard
    assert listener._linux_user_for_global_user(cfg, "gu_flomaster") == "flomaster"


def test_attached_user_conversation_uses_user_worker(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    sock = cfg.data_dir / "_sock" / "user-flomaster.sock"
    sock.touch()
    sent = {}

    monkeypatch.setattr(
        listener.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="coordinator"),
    )
    monkeypatch.setattr(
        listener,
        "_worker_action_over_socket",
        lambda path, params: sent.update(path=path, params=params) or {"ok": True},
    )
    monkeypatch.setattr(
        actions,
        "dispatch",
        lambda *_args, **_kwargs: pytest.fail("must not dispatch in coordinator"),
    )

    params = {"slug": "guestent", "global_user_id": "gu_flomaster"}
    assert listener._dispatch_user_conversation(cfg, "gu_flomaster", params) == {
        "ok": True
    }
    assert sent == {"path": sock, "params": params}


def test_missing_user_worker_does_not_fall_back_to_coordinator(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        listener.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="coordinator"),
    )
    monkeypatch.setattr(
        actions,
        "dispatch",
        lambda *_args, **_kwargs: pytest.fail("must not dispatch in coordinator"),
    )

    with pytest.raises(listener._UserConversationActionError, match="unavailable"):
        listener._dispatch_user_conversation(
            cfg,
            "gu_flomaster",
            {"slug": "guestent", "global_user_id": "gu_flomaster"},
        )


def test_the_fail_closed_refusal_is_loud(tmp_path: Path, monkeypatch, caplog) -> None:
    """Fail-closed is right; failing closed INVISIBLY is not (p192 review).

    The refusal is the deliberate path, so it is the one that must be
    reconstructible: the message stays durably recorded, nobody is woken, and
    without this line there is nothing in the log to explain the silence. Per
    T-0880 nothing restarts a per-user worker, so this state is live-reachable.
    """
    import logging

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        listener.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="coordinator")
    )
    with caplog.at_level(logging.ERROR, logger=listener.log.name):
        with pytest.raises(listener._UserConversationActionError):
            listener._dispatch_user_conversation(
                cfg, "gu_flomaster", {"slug": "guestent"}
            )
    assert [r for r in caplog.records if "NO attendant was woken" in r.getMessage()]


def test_an_unavailable_worker_is_distinguishable_from_no_attempt(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A bare None reads identically to "nothing was tried". The caller that
    owns the chat has to be able to tell the two apart to say anything useful,
    so the refusal carries its own marker."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        listener.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="coordinator")
    )
    out = listener._ensure_user_conversation(cfg, "guestent", "gu_flomaster", "ref")
    assert out == {"ok": False, "user_worker_unavailable": True}


def test_the_socket_timeout_outlasts_the_callees_own_wait(monkeypatch) -> None:
    """A client timeout at or below the callee's composer wait can ONLY time
    out on a cold spawn — while the spawn on the other side very likely
    SUCCEEDED. It was hardcoded 10.0 against a 15.0 wait. Derived now, so the
    two constants cannot drift apart again."""
    from bot_squad_worker.sessions import _COMPOSER_READY_TIMEOUT_SEC

    assert listener._user_worker_timeout() > _COMPOSER_READY_TIMEOUT_SEC
    # and by a margin that covers window creation + agent launch, not by 1s
    assert listener._user_worker_timeout() >= _COMPOSER_READY_TIMEOUT_SEC + 20


def test_coordinator_owned_or_unattached_user_stays_local(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path)
    current = pwd.getpwuid(os.geteuid()).pw_name
    (cfg.config_dir / "auth.toml").write_text(
        f'''\n[user_meta.local]\nlinux_user = "{current}"\nattached_to_global_user = "gu_local"\n'''
    )
    monkeypatch.setattr(actions, "dispatch", lambda name, params: {"name": name})

    assert listener._dispatch_user_conversation(cfg, "gu_local", {}) == {
        "name": "ensure_user_conversation"
    }


def test_unique_static_supergroup_is_a_dedicated_project_boundary(tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        projects={
            "guestent": SimpleNamespace(tg_chat="-1004380986138"),
            "other": SimpleNamespace(tg_chat="12345"),
        }
    )
    assert (
        listener._dedicated_static_group_chat_slug(cfg, "-1004380986138")
        == "guestent"
    )
    # A private chat remains a potentially shared cross-project inbox.
    assert listener._dedicated_static_group_chat_slug(cfg, "12345") == ""


def test_trusted_senders_share_configured_conversation_owner(tmp_path: Path) -> None:
    cfg = SimpleNamespace(data_dir=tmp_path)
    project = tmp_path / "guestent"
    project.mkdir()
    (project / "groups.json").write_text(
        """{
          "conversation_owner_global_user_id": "flomaster",
          "groups": [{"id": "g"}],
          "memberships": {"flomaster": "g", "almdudleer": "g"}
        }"""
    )
    assert (
        listener._dedicated_chat_conversation_gid(
            cfg, "guestent", "almdudleer"
        )
        == "flomaster"
    )
    assert (
        listener._dedicated_chat_conversation_gid(cfg, "guestent", "flomaster")
        == "flomaster"
    )
