"""T-0770: `bsq topic say` can name the destination EXPLICITLY.

Why the flags exist, measured rather than assumed. Every resolving form —
`--ticket` (`tg_bindings.find_by_ticket`) and `bsq tg ping`'s rung-3 lookup
(`actions._own_topic_binding` → `find_by_session`) — returns the FIRST binding
matching the session. On the live bindings of the incident T-0770 was filed for,
p70 held BOTH topic 220 and topic 517, so a session told to "answer in your
topic" would have posted into 220 while the stakeholder was writing in 517: the
topic he had ALREADY been ignored in. A direct-mode topic also frequently
carries no ticket_id at all (`/pin-session` sets only the session), leaving the
resolving form nothing to key on.

So the provenance envelope hands the session `--chat/--topic` with the values
taken straight from the update that arrived, and these tests pin that they reach
the worker as `tg_notify`'s rung-1 params — the one rung that consults no map.

`bsq` is extensionless, so it is loaded via SourceFileLoader (mirrors
test_bsq_access_points.py).
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_topic_say_mod", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_topic_say_mod", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

SID = "S-alice-dev-p9"
CHAT = "-1003761939853"
TOPIC = 517


@pytest.fixture(autouse=True)
def _stub_identity(monkeypatch):
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "watchrobot")
    monkeypatch.setattr(bsq, "require_sid", lambda given=None: given or SID)


def _record_post(monkeypatch, sent=True):
    calls = []

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        return {"ok": True, "sent": sent}

    monkeypatch.setattr(bsq, "post", fake_post)
    return calls


def _args(words, **kw):
    return SimpleNamespace(words=words, chat=kw.get("chat"), topic=kw.get("topic"),
                           sid=kw.get("sid"), urgent=kw.get("urgent", False))


def test_explicit_chat_and_topic_are_sent_as_rung_1_params(monkeypatch, capsys):
    calls = _record_post(monkeypatch)
    bsq.cmd_topic_say(_args(["готово,", "посмотри"], chat=CHAT, topic=TOPIC))
    assert calls == [("tg_notify", {
        "message": "готово, посмотри", "sid": SID, "urgent": False,
        "chat_id": CHAT, "topic_id": TOPIC,
    })]
    # …and NO ticket_id: naming both would put the resolving form back in play.
    assert "ticket_id" not in calls[0][1]
    assert f"chat={CHAT}" in capsys.readouterr().out


def test_a_negative_chat_id_is_a_value_not_a_flag(monkeypatch):
    """Every real supergroup id starts with `-100`. If argparse read it as an
    option the envelope's command would fail in the session's hands, which is
    the one place it must not."""
    parser = bsq.build_parser()
    args = parser.parse_args(["topic", "say", "--chat", CHAT, "--topic", str(TOPIC),
                              "привет"])
    assert args.chat == CHAT and args.topic == TOPIC and args.words == ["привет"]


def test_the_ticket_form_is_untouched(monkeypatch, capsys):
    """The original T-0660 verb keeps working exactly as before — this is an
    addition, not a replacement."""
    calls = _record_post(monkeypatch)
    bsq.cmd_topic_say(_args(["T-0314", "ready", "for", "review"]))
    assert calls == [("tg_notify", {
        "message": "ready for review", "sid": SID, "urgent": False,
        "ticket_id": "T-0314",
    })]
    assert "T-0314" in capsys.readouterr().out


def test_chat_without_topic_is_refused(monkeypatch):
    """A thread id is only meaningful inside its own chat; half a destination
    would silently fall back to the project's General room — which reads to him
    as being ignored all over again."""
    _record_post(monkeypatch)
    with pytest.raises(SystemExit):
        bsq.cmd_topic_say(_args(["hello"], chat=CHAT))
    with pytest.raises(SystemExit):
        bsq.cmd_topic_say(_args(["hello"], topic=TOPIC))


def test_an_empty_message_is_refused_in_both_forms(monkeypatch):
    _record_post(monkeypatch)
    with pytest.raises(SystemExit):
        bsq.cmd_topic_say(_args(["T-0314"]))
    with pytest.raises(SystemExit):
        bsq.cmd_topic_say(_args(["   "], chat=CHAT, topic=TOPIC))


def test_the_command_the_envelope_hands_out_parses_as_written(monkeypatch):
    """★ The join between the two halves of T-0770. The worker composes a
    command string; a session pastes it verbatim. If the two ever drift, the
    session is told to run something that does not work — and the failure looks
    exactly like the silence this ticket exists to remove."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
    from bot_squad_worker import tg_direct_reply as TDR

    rendered = TDR.reply_command(CHAT, TOPIC)          # …--chat X --topic N "<your answer>"
    argv = rendered.split()
    assert argv[:2] == ["bsq", "topic"]
    args = bsq.build_parser().parse_args(argv[1:-1] + ["мой ответ"])
    assert args.chat == CHAT and args.topic == TOPIC

    calls = _record_post(monkeypatch)
    bsq.cmd_topic_say(_args(["мой", "ответ"], chat=args.chat, topic=args.topic))
    assert calls[0][1]["chat_id"] == CHAT and calls[0][1]["topic_id"] == TOPIC
