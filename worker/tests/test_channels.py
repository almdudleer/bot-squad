"""Unit tests for bot_squad_worker.channels — the T-0490 channel abstraction.

Covers the interface contract, the TG impl, the MAX impl (stub-ready), the
registry/factory selection, the unwired receive() seam, and the deploy-notify
reroute (technical deliveries flow through the abstraction).

Manual-first (T-0158): walked through in scenarios/T-0490-*.md before this
automation — a narrated REPL run of get_channel/send/receive + deploy_monitor_one
against fakes, observing the real recorded calls.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import channels


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeCfg:
    """Minimal cfg the channels need (selection + MAX recipient default)."""

    max_recipient_kind = "chat_id"


class _FakeTgClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None):
        self.calls.append(
            dict(chat_id=chat_id, text=text, sid=sid, user=user,
                 urgent=urgent, topic_id=topic_id)
        )
        return True


class _FakeMaxClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, chat_id, text, sid="", user="", urgent=False, recipient_kind="chat_id"):
        self.calls.append(
            dict(chat_id=chat_id, text=text, sid=sid, user=user,
                 urgent=urgent, recipient_kind=recipient_kind)
        )
        return True


@pytest.fixture()
def fake_clients(monkeypatch):
    """Patch the actions singletons the channels delegate to."""
    import bot_squad_worker.actions as A

    ftg, fmax = _FakeTgClient(), _FakeMaxClient()
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: ftg)
    monkeypatch.setattr(A, "_get_max_client", lambda _cfg: fmax)
    return ftg, fmax


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------

def test_builtin_channels_registered():
    assert "tg" in channels.available()
    assert "max" in channels.available()


def test_default_channel_is_tg():
    c = channels.get_channel(_FakeCfg())
    assert isinstance(c, channels.TgChannel)
    assert c.name == "tg"


def test_explicit_name_selects_impl():
    assert isinstance(channels.get_channel(_FakeCfg(), name="tg"), channels.TgChannel)
    assert isinstance(channels.get_channel(_FakeCfg(), name="max"), channels.MaxChannel)


def test_config_default_channel_override():
    class Cfg(_FakeCfg):
        default_channel = "max"

    assert isinstance(channels.get_channel(Cfg()), channels.MaxChannel)


def test_unknown_channel_raises():
    with pytest.raises(ValueError, match="unknown channel 'bogus'"):
        channels.get_channel(_FakeCfg(), name="bogus")


def test_register_new_channel_needs_no_caller_change():
    """Adding a channel = one register() call; get_channel() finds it unchanged."""
    class _MailChannel(channels.Channel):
        name = "mail-test"

        def __init__(self, cfg):
            self.cfg = cfg

        def send(self, text, *, chat_id="", sid="", user="", urgent=False,
                 topic_id=None, **extra):
            return True

    try:
        channels.register("mail-test", _MailChannel)
        c = channels.get_channel(_FakeCfg(), name="mail-test")
        assert isinstance(c, _MailChannel)
        assert "mail-test" in channels.available()
    finally:
        channels._REGISTRY.pop("mail-test", None)


# ---------------------------------------------------------------------------
# TG impl
# ---------------------------------------------------------------------------

def test_tg_channel_delegates_to_tgclient(fake_clients):
    ftg, _ = fake_clients
    c = channels.get_channel(_FakeCfg(), name="tg")
    sent = c.send("hi", chat_id="C1", sid="deploy_monitor", user="bob",
                  urgent=True, topic_id=7)
    assert sent is True
    assert ftg.calls == [
        dict(chat_id="C1", text="hi", sid="deploy_monitor", user="bob",
             urgent=True, topic_id=7)
    ]


def test_tg_channel_fetches_client_per_send(monkeypatch):
    """A monkeypatch installed AFTER the channel is built still takes effect."""
    import bot_squad_worker.actions as A

    c = channels.get_channel(_FakeCfg(), name="tg")
    late = _FakeTgClient()
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: late)
    c.send("late", chat_id="X")
    assert late.calls and late.calls[0]["text"] == "late"


# ---------------------------------------------------------------------------
# MAX impl (stub-ready)
# ---------------------------------------------------------------------------

def test_max_channel_delegates_to_maxclient(fake_clients):
    _, fmax = fake_clients
    c = channels.get_channel(_FakeCfg(), name="max")
    # topic_id is TG-only — MAX must ignore it, not error.
    sent = c.send("yo", chat_id="M1", sid="deploy_monitor", urgent=True, topic_id=99)
    assert sent is True
    assert fmax.calls == [
        dict(chat_id="M1", text="yo", sid="deploy_monitor", user="",
             urgent=True, recipient_kind="chat_id")
    ]


def test_max_channel_recipient_kind_override(fake_clients):
    _, fmax = fake_clients
    c = channels.get_channel(_FakeCfg(), name="max")
    c.send("dm", chat_id="U1", recipient_kind="user_id")
    assert fmax.calls[-1]["recipient_kind"] == "user_id"


# ---------------------------------------------------------------------------
# receive() seam — declared, NOT wired
# ---------------------------------------------------------------------------

def test_receive_is_unwired_seam():
    for name in ("tg", "max"):
        c = channels.get_channel(_FakeCfg(), name=name)
        with pytest.raises(NotImplementedError, match="unwired seam"):
            c.receive()


# ---------------------------------------------------------------------------
# Interface contract
# ---------------------------------------------------------------------------

def test_channel_send_is_abstract():
    with pytest.raises(TypeError):
        channels.Channel()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Deploy-notify reroute — technical deliveries flow through the abstraction
# ---------------------------------------------------------------------------

def _import_test_jobs():
    import importlib.util
    import sys

    here = Path(__file__).parent
    sys.path.insert(0, str(here))
    spec = importlib.util.spec_from_file_location("tj_helpers", here / "test_jobs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_deploy_notifications_flow_through_channel(tmp_path, monkeypatch):
    """deploy_monitor_one routes start+finish pings via channels.get_channel,
    proving the reroute (T-0490 DoD: technical deliveries through the abstraction).
    """
    monkeypatch.setenv("BOT_SQUAD_DISABLE_QUIET_HOURS", "1")
    tj = _import_test_jobs()

    import bot_squad_worker.actions as A
    import bot_squad_worker.jobs as J
    from bot_squad_worker import deploy as _deploy

    proj = tj._make_project_with_repo(tmp_path)
    cfg = tj._make_config_with_project(tmp_path, proj)
    tj._make_recipe(cfg, proj.slug, "staging", rc=0)
    _deploy.enqueue(cfg, proj.slug, "staging", "test", "pytest")

    fake_tg = tj._FakeTgClient()
    # The TgChannel delegates to this singleton — so patching it proves the
    # deploy path went through the abstraction (no direct tg.send).
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    J.deploy_monitor_one(cfg, proj.slug)

    texts = [c["text"] for c in fake_tg.calls]
    assert any("starting deploy" in t for t in texts), texts
    assert any("SUCCESS" in t for t in texts), texts
    # Sanity: the channel preserved the deploy_monitor sid + urgent flag.
    assert all(c["sid"] == "deploy_monitor" and c["urgent"] for c in fake_tg.calls)
