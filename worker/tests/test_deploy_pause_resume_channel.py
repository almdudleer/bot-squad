"""T-0591 (F5.3): pause_deploys/resume_deploys notifications route through
the channel abstraction (channels.get_channel) instead of a raw TgClient
call — same wiring as tg_listener._channel_notify."""
from __future__ import annotations

from pathlib import Path

from bot_squad_worker.config import Config


class _FakeTgClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, chat_id, text, sid="", user="", urgent=False,
              topic_id=None, debounce=True, **extra) -> bool:
        self.calls.append({"chat_id": chat_id, "text": text, "sid": sid,
                            "topic_id": topic_id})
        return True


def _inject(monkeypatch, tmp_config_dir):
    import bot_squad_worker.actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    fake = _FakeTgClient()
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: fake)
    return cfg, fake


def test_pause_deploys_notifies_via_channel_abstraction(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject(monkeypatch, tmp_config_dir)
    out = A.dispatch("pause_deploys", {
        "slug": "test-project", "reason": "incident", "requested_by": "alex",
    })
    assert out["ok"] is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["chat_id"] == "0"  # test-project's configured tg_chat
    assert "paused" in fake.calls[0]["text"]


def test_pause_deploys_already_paused_sends_no_second_notification(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    _inject(monkeypatch, tmp_config_dir)
    A.dispatch("pause_deploys", {
        "slug": "test-project", "reason": "first", "requested_by": "alex"})
    cfg, fake2 = _inject(monkeypatch, tmp_config_dir)
    out = A.dispatch("pause_deploys", {
        "slug": "test-project", "reason": "second", "requested_by": "alex"})
    assert out["was_already_paused"] is True
    assert fake2.calls == []


def test_resume_deploys_notifies_via_channel_abstraction(tmp_config_dir, monkeypatch):
    import bot_squad_worker.actions as A

    cfg, fake = _inject(monkeypatch, tmp_config_dir)
    A.dispatch("pause_deploys", {
        "slug": "test-project", "reason": "incident", "requested_by": "alex"})
    fake.calls.clear()
    out = A.dispatch("resume_deploys", {
        "slug": "test-project", "requested_by": "alex"})
    assert out == {"ok": True, "was_paused": True}
    assert len(fake.calls) == 1
    assert "resumed" in fake.calls[0]["text"]
