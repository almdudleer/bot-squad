"""``bsq topic bind`` can express — and REPORT — the per-task-topic fields
(T-0771).

Before this, the CLI offered only chat_id/thread_id/slug, so a rebind of a
topic that carried a ticket_id/session_id dropped them, printed the same
success line it prints for a faithful write, and left no way to put them back.
The worker refuses the lossy case; this half is about what a human can TYPE and
what it is TOLD afterwards.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_topic_bind", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_topic_bind", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _run(monkeypatch, argv: list[str], response: dict) -> tuple[list, str, object]:
    posts: list = []

    def _fake_post(action, params, timeout=35.0, fatal=True):
        posts.append((action, params))
        return response

    monkeypatch.setattr(bsq, "post", _fake_post)
    args = bsq.build_parser().parse_args(argv)
    args.func(args)
    return posts, "", args


def _res(binding: dict, changes: dict, cleared: list) -> dict:
    return {"ok": True, "binding": binding, "changes": changes, "cleared": cleared}


def test_bare_bind_sends_only_the_three_original_params(monkeypatch, capsys):
    """NEGATIVE GUARD — the T-0639 project binding is typed exactly as before
    and must not start sending new keys the worker would reject."""
    posts, _, _ = _run(
        monkeypatch, ["topic", "bind", "-100123", "7", "watchrobot"],
        _res({"slug": "watchrobot", "ticket_id": None, "session_id": None,
              "pinned_message_id": None},
             {"slug": {"from": None, "to": "watchrobot"}}, []))

    assert posts == [("tg_topic_bind", {
        "chat_id": "-100123", "thread_id": "7", "slug": "watchrobot"})]
    out = capsys.readouterr().out
    assert "-> slug=watchrobot" in out
    assert "slug: (none) -> watchrobot" in out


def test_none_thread_id_still_means_the_general_feed(monkeypatch, capsys):
    posts, _, _ = _run(
        monkeypatch, ["topic", "bind", "-100123", "none", "watchrobot"],
        _res({"slug": "watchrobot", "ticket_id": None, "session_id": None,
              "pinned_message_id": None}, {}, []))
    assert posts[0][1]["thread_id"] is None


def test_ticket_and_session_flags_reach_the_action(monkeypatch, capsys):
    posts, _, _ = _run(
        monkeypatch,
        ["topic", "bind", "-100123", "517", "watchrobot",
         "--ticket", "T-0314", "--session", "S-x-p70"],
        _res({"slug": "watchrobot", "ticket_id": "T-0314",
              "session_id": "S-x-p70", "pinned_message_id": None},
             {"ticket_id": {"from": None, "to": "T-0314"},
              "session_id": {"from": None, "to": "S-x-p70"}}, []))

    assert posts[0][1] == {
        "chat_id": "-100123", "thread_id": "517", "slug": "watchrobot",
        "ticket_id": "T-0314", "session_id": "S-x-p70",
    }
    out = capsys.readouterr().out
    assert "ticket_id: (none) -> T-0314" in out
    assert "session_id: (none) -> S-x-p70" in out


def test_clear_flags_are_sent_only_when_asked_for(monkeypatch, capsys):
    """An absent flag must not send `clear_*: false` — the worker distinguishes
    "not named" from "clear it", and a always-present false would be noise on
    the surface where the distinction lives."""
    posts, _, _ = _run(
        monkeypatch,
        ["topic", "bind", "-100123", "517", "watchrobot",
         "--ticket", "T-0314", "--clear-session"],
        _res({"slug": "watchrobot", "ticket_id": "T-0314", "session_id": None,
              "pinned_message_id": None},
             {"session_id": {"from": "S-x-p70", "to": None}}, ["session_id"]))

    assert posts[0][1] == {
        "chat_id": "-100123", "thread_id": "517", "slug": "watchrobot",
        "ticket_id": "T-0314", "clear_session_id": True,
    }
    assert "clear_ticket_id" not in posts[0][1]
    out = capsys.readouterr().out
    assert "session_id: S-x-p70 -> (cleared)" in out


def test_clear_ticket_flag(monkeypatch, capsys):
    posts, _, _ = _run(
        monkeypatch,
        ["topic", "bind", "-100123", "275", "watchrobot", "--clear-ticket"],
        _res({"slug": "watchrobot", "ticket_id": None, "session_id": None,
              "pinned_message_id": None},
             {"ticket_id": {"from": "T-0320", "to": None}}, ["ticket_id"]))

    assert posts[0][1]["clear_ticket_id"] is True
    assert "ticket_id: T-0320 -> (cleared)" in capsys.readouterr().out


def test_a_write_that_moved_nothing_says_so(monkeypatch, capsys):
    """The reported harm is a lossy write reading like a faithful one. The
    inverse matters too: an idempotent re-bind must not read like a rewrite."""
    _run(monkeypatch,
         ["topic", "bind", "-100123", "517", "watchrobot", "--ticket", "T-0314"],
         _res({"slug": "watchrobot", "ticket_id": "T-0314", "session_id": None,
               "pinned_message_id": None}, {}, []))
    assert "(no fields changed)" in capsys.readouterr().out


def test_clearing_an_already_empty_field_is_reported_as_such(monkeypatch, capsys):
    """Not silence, and not a fake "cleared" line either — the caller asked a
    question and gets a straight answer."""
    _run(monkeypatch,
         ["topic", "bind", "-100123", "517", "watchrobot",
          "--ticket", "T-0314", "--clear-session"],
         _res({"slug": "watchrobot", "ticket_id": "T-0314", "session_id": None,
               "pinned_message_id": None}, {}, ["session_id"]))
    out = capsys.readouterr().out
    assert "session_id: already empty, nothing to clear" in out


def test_clearing_session_id_warns_about_the_pin_left_in_the_topic(
        monkeypatch, capsys):
    """T-0677's marker is a message pinned in the TOPIC: the binding no longer
    routes to that session, but Telegram still shows a pin saying it does. The
    record keeps the id (so it can still be unpinned) and the human is told
    where to go — this verb makes no Telegram call of its own."""
    _run(monkeypatch,
         ["topic", "bind", "-100123", "517", "watchrobot",
          "--ticket", "T-0314", "--clear-session"],
         _res({"slug": "watchrobot", "ticket_id": "T-0314", "session_id": None,
               "pinned_message_id": 555},
              {"session_id": {"from": "S-x-p70", "to": None}}, ["session_id"]))
    out = capsys.readouterr().out
    assert "message_id=555" in out
    assert "/pin-session off" in out


def test_no_pin_warning_when_the_session_id_did_not_move(monkeypatch, capsys):
    """A pin that still matches the binding is not news."""
    _run(monkeypatch,
         ["topic", "bind", "-100123", "517", "bot-squad",
          "--ticket", "T-0314", "--session", "S-x-p70"],
         _res({"slug": "bot-squad", "ticket_id": "T-0314",
               "session_id": "S-x-p70", "pinned_message_id": 555},
              {"slug": {"from": "watchrobot", "to": "bot-squad"}}, []))
    assert "message_id=555" not in capsys.readouterr().out
