"""T-0758 — the sender tag is a transport guarantee, not a typing habit.

The measured defect these pin (live install, 2026-07-27): watchrobot's
attendant relayed 213 replies and 0 carried a sender tag, while bot-squad's
carried one on 4 of 33 — because the API's session-writeback relay passes an
explicit ``chat_id`` and neither ``sid`` nor ``slug``, so ``tg._prefix`` had
nothing to render and the only tags in the channel were hand-typed.

Demoed against unfixed sources — HEAD's transports with only the new module
dropped in — 19 of 29 go RED. The 10 that pass on BOTH are the negative guards,
and they are the point: the fix must not start labelling what was already
right. They are the sends that name NO sender (a lifecycle notice, an
interactive command reply, a bare split-page part), the class sender that
labels itself (``[voice_intake]``), a hand-typed tag with nothing better to
replace it, the ``[<label> @ <user>]`` peer-mirror form, and the module's own
contracts.

Three tests below pin invariants that ALSO held before this ticket — the
spool/wire byte equality, the debounce key, the echo guard's blindness to an
untagged body — and they still go RED at HEAD, but only because they pass
``sender_sid``, a kwarg HEAD's transport does not accept. Said plainly rather
than filed under "negative guard": the property is old, the test is new.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import sender_tag
from bot_squad_worker.max import MaxClient
from bot_squad_worker.tg import TgClient

# The two live attendants, measured 2026-07-27. Same window, different pane —
# so the SID alone CANNOT name the project and the on-disk session md must.
BS_ATT = "S-almdudleer-gu_dc8262b6cea9098d98e04d7e-user-conversation-p5"
WR_ATT = "S-almdudleer-gu_dc8262b6cea9098d98e04d7e-user-conversation-p70"


class _Cfg:
    """Config stand-in with the two live projects sharing ONE tg_chat.

    Sharing the chat is not incidental colour — it is the reason the tag is
    resolved from the SENDER: on the live install both projects carry
    ``tg_chat = "404580642"``, so a chat->slug lookup answers confidently and
    wrongly.
    """

    def __init__(self, data_dir: Path, token: str = "8000000000:TESTTOKEN") -> None:
        self.data_dir = data_dir
        self.tg_bot_token = token
        self.max_bot_token = token
        self.tg_proxy_url = ""
        self.max_proxy_url = ""
        self.max_recipient_kind = "chat_id"
        self.projects = {"bot-squad": object(), "watchrobot": object()}


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Cfg:
    for slug, sid in (("bot-squad", BS_ATT), ("watchrobot", WR_ATT)):
        sess = tmp_path / slug / "sessions"
        sess.mkdir(parents=True)
        (sess / f"{sid}.md").write_text(f"---\nsid: {sid}\n---\n")
    (tmp_path / "bot-squad" / "sessions" / "S-almdudleer-operator-p298.md").write_text(
        "---\nsid: S-almdudleer-operator-p298\n---\n"
    )
    monkeypatch.setenv("BOT_SQUAD_DISABLE_QUIET_HOURS", "1")
    return _Cfg(tmp_path)


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture what ``_post`` was handed, for both transports."""
    sent: list[dict] = []

    def _tg_post(self, **kw):
        sent.append(dict(kw, channel="tg"))
        return {"result": {"message_id": 7}}

    def _max_post(self, **kw):
        sent.append(dict(kw, channel="max"))

    monkeypatch.setattr(TgClient, "_post", _tg_post)
    monkeypatch.setattr(MaxClient, "_post", _max_post)
    return sent


# ---------------------------------------------------------------------------
# (a) + (d) — the tag names the SENDING SESSION's project and role
# ---------------------------------------------------------------------------

def test_relay_send_without_sid_is_tagged_from_sender_sid(cfg, wire) -> None:
    """THE REPORTED BUG. The relay spells out a chat and names no `sid`."""
    TgClient(cfg).send(
        chat_id="404580642", text="Понял, делаю.", sender_sid=WR_ATT, debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot user-conversation] Понял, делаю."


def test_same_window_different_project_reads_as_its_own_project(cfg, wire) -> None:
    """(d) — the point of the ask. Both attendants share a window name; only
    the on-disk session md distinguishes them, and both would resolve to the
    same chat."""
    client = TgClient(cfg)
    client.send(chat_id="404580642", text="ответ", sender_sid=BS_ATT, debounce=False)
    client.send(chat_id="404580642", text="ответ", sender_sid=WR_ATT, debounce=False)
    assert [m["text"] for m in wire] == [
        "[bot-squad user-conversation] ответ",
        "[watchrobot user-conversation] ответ",
    ]


def test_max_applies_the_same_tag(cfg, wire) -> None:
    """(a) — both channels, one rule, one module."""
    MaxClient(cfg).send(chat_id="404580642", text="Понял.", sender_sid=WR_ATT)
    assert wire[0]["text"] == "[watchrobot user-conversation] Понял."


def test_raw_sid_as_display_label_is_resolved_not_printed(cfg, wire) -> None:
    """A caller with no slug on hand used to leak the raw SID at the user."""
    TgClient(cfg).send(
        chat_id="404580642", text="привет", sid="S-almdudleer-operator-p298",
        debounce=False,
    )
    assert wire[0]["text"] == "[bot-squad operator] привет"


def test_stall_escalation_stops_leaking_a_raw_sid(cfg, wire) -> None:
    """THE UNFIXED TWIN, found by enumerating every `_send_stakeholder_dm`
    call site rather than assuming the relay was the only one:
    ``tg_stall.py`` escalates a specific stalled session with ``sid=<raw SID>``
    and NO ``slug``, so ``sid_display_label`` degraded to the bare SID and the
    stakeholder got ``[S-almdudleer-…-p266]`` — a routing key, and still no
    project. One transport-level resolution fixes both without tg_stall
    changing a line, which is the whole argument for the chokepoint."""
    sess = cfg.data_dir / "watchrobot" / "sessions"
    sid = "S-almdudleer-rv-pair-trading-signals-poc-review-real--p266"
    (sess / f"{sid}.md").write_text(f"---\nsid: {sid}\n---\n")
    TgClient(cfg).send(
        chat_id="404580642", text="⏳ сессия не отвечает 15 мин", sid=sid,
        debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot dev] ⏳ сессия не отвечает 15 мин"


def test_route_sid_alone_still_tags(cfg, wire) -> None:
    """The net under a send path written later that sets only the routing key."""
    TgClient(cfg).send(
        chat_id="404580642", text="привет", route_sid=WR_ATT, debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot user-conversation] привет"


def test_topic_binding_names_the_project_when_the_sid_does_not(cfg, wire) -> None:
    """Slug fallback: an unknown SID + a bound forum topic still names a
    project. Deliberately the ONLY destination-derived rung — and it is a
    fallback for the SLUG, never a reason to tag."""
    worker_dir = cfg.data_dir / "_worker"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "tg_bindings.json").write_text(
        '{"-100999:23": {"slug": "watchrobot", "ticket_id": null,'
        ' "session_id": null, "pinned_message_id": null}}'
    )
    TgClient(cfg).send(
        chat_id="-100999", topic_id=23, text="привет",
        sender_sid="S-nobody-ghost-p9", debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot dev] привет"


# ---------------------------------------------------------------------------
# (b) — no double-tagging; the manual habit is absorbed, and corrected
# ---------------------------------------------------------------------------

def test_hand_typed_tag_is_replaced_not_duplicated(cfg, wire) -> None:
    TgClient(cfg).send(
        chat_id="404580642", text="[bot-squad user-conversation] Понял.",
        sender_sid=BS_ATT, debounce=False,
    )
    assert wire[0]["text"] == "[bot-squad user-conversation] Понял."
    assert wire[0]["text"].count("[bot-squad user-conversation]") == 1


def test_a_wrong_hand_typed_tag_is_corrected(cfg, wire) -> None:
    """The case that actively lies to the reader: a watchrobot session copying
    bot-squad's habit verbatim. Nothing catches this today."""
    TgClient(cfg).send(
        chat_id="404580642", text="[bot-squad user-conversation] Понял.",
        sender_sid=WR_ATT, debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot user-conversation] Понял."


def test_hand_typed_tag_survives_when_no_sender_is_named(cfg, wire) -> None:
    """Passes on BOTH sources — a negative guard. With nothing better to say,
    the session's own tag is left exactly as typed."""
    TgClient(cfg).send(
        chat_id="404580642", text="[bot-squad user-conversation] Понял.",
        debounce=False,
    )
    assert wire[0]["text"] == "[bot-squad user-conversation] Понял."


def test_every_part_of_a_split_page_carries_the_tag(cfg, wire) -> None:
    """Splitting happens above the transport (``actions._split_page``), so each
    part is its own send and each must read alike.

    Part 1 is the trap: ``_split_page`` puts ``tg.part_marker``'s ``(n/N)`` in
    FRONT of the body, so a hand-typed tag on a long reply hides behind the
    marker. A leading-bracket check alone would miss it and prepend a second
    tag — the exact double-tagging DoD (b) forbids, on the messages most likely
    to matter (the long ones)."""
    client = TgClient(cfg)
    parts = ("(1/2) [watchrobot user-conversation] начало", "(2/2) конец")
    for part in parts:
        client.send(chat_id="404580642", text=part, sender_sid=WR_ATT, debounce=False)
    assert [m["text"] for m in wire] == [
        "[watchrobot user-conversation] (1/2) начало",
        "[watchrobot user-conversation] (2/2) конец",
    ]


def test_split_part_marker_survives_when_no_sender_is_named(cfg, wire) -> None:
    """The marker is re-emitted verbatim, so a send that tags nothing is
    byte-identical to its pre-T-0758 shape."""
    TgClient(cfg).send(chat_id="404580642", text="(2/3) середина", debounce=False)
    assert wire[0]["text"] == "(2/3) середина"


# ---------------------------------------------------------------------------
# (c) — what is already identified is left alone
# ---------------------------------------------------------------------------

def test_a_send_that_names_nobody_is_untouched(cfg, wire) -> None:
    """Passes on BOTH sources. `tg_listener`'s command replies state no
    identity, so there is nothing to name — this falls out of the identity
    rule, not from any per-class exemption."""
    TgClient(cfg).send(
        chat_id="404580642", text="📋 T-0746 → closed — сообщение сессии",
        debounce=False,
    )
    assert wire[0]["text"] == "📋 T-0746 → closed — сообщение сессии"


def test_system_notice_with_a_declared_project_is_tagged_slug_only(cfg, wire) -> None:
    """The lifecycle-notice shape (T-0758 follow-up): no session composed it,
    so `_send_stakeholder_dm` renders the slug alone as the label. The tag
    names the project and stops short of inventing a role."""
    TgClient(cfg).send(
        chat_id="404580642", text="📋 T-0320 → totest — RV pair-trading PoC",
        sid="watchrobot", debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot] 📋 T-0320 → totest — RV pair-trading PoC"


def test_the_same_ticket_id_in_two_projects_reads_differently(cfg, wire) -> None:
    """WHY the notice needed the tag, in one assertion. Operator p298 measured
    189 of watchrobot's 192 ticket ids also present in bot-squad — so the id in
    a `📋` notice identifies almost nothing, and both projects deliver into the
    same supergroup."""
    client = TgClient(cfg)
    for slug in ("bot-squad", "watchrobot"):
        client.send(chat_id="404580642", text="📋 T-0320 → totest", sid=slug,
                    debounce=False)
    assert [m["text"] for m in wire] == [
        "[bot-squad] 📋 T-0320 → totest",
        "[watchrobot] 📋 T-0320 → totest",
    ]


def test_class_sender_with_a_project_is_combined_not_replaced(cfg, wire) -> None:
    """The autopilot/telemetry twins. `[autopilot]` answers WHAT is speaking
    but not WHICH project, and both are per-project alerts — so the project is
    added rather than the class dropped."""
    TgClient(cfg).send(
        chat_id="404580642", text="⚠️ 3 sessions idle > 2h",
        sid="[watchrobot] autopilot", debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot autopilot] ⚠️ 3 sessions idle > 2h"


def test_class_sender_keeps_its_own_name(cfg, wire) -> None:
    """Passes on BOTH sources. `[voice_intake] …` already says what it is."""
    TgClient(cfg).send(
        chat_id="404580642", text="✅ got your voice note", sid="voice_intake",
        debounce=False,
    )
    assert wire[0]["text"] == "[voice_intake] ✅ got your voice note"


def test_self_describing_body_marker_is_not_double_bracketed(cfg, wire) -> None:
    TgClient(cfg).send(
        chat_id="404580642", text="[FYI — ответ не требуется] сессия написала",
        sender_sid=WR_ATT, debounce=False,
    )
    assert wire[0]["text"] == "[FYI — ответ не требуется] сессия написала"


def test_a_leading_markdown_link_is_not_a_marker(cfg, wire) -> None:
    """A body that OPENS with a link still gets its tag — the skip rule is
    "already identified", not "starts with a bracket"."""
    TgClient(cfg).send(
        chat_id="404580642", text="[T-0758](https://x/t) готово",
        sender_sid=WR_ATT, debounce=False,
    )
    assert wire[0]["text"] == "[watchrobot user-conversation] [T-0758](https://x/t) готово"


def test_interactive_command_reply_stays_bare(cfg, wire) -> None:
    """Passes on BOTH sources — tg_listener's replies name no sender."""
    TgClient(cfg).send(chat_id="404580642", text="Сессии проекта:\n- p298",
                       debounce=False)
    assert wire[0]["text"] == "Сессии проекта:\n- p298"


def test_nested_bracket_label_is_flattened(cfg, wire) -> None:
    """`sid_display_label` renders `[<slug>] <name>` for a non-SID sender, and
    `_prefix` used to wrap that into the malformed `[[bot-squad] …]`."""
    TgClient(cfg).send(
        chat_id="404580642", text="🟡 deploys paused", sid="[bot-squad] deploy_monitor",
        debounce=False,
    )
    assert wire[0]["text"] == "[bot-squad deploy_monitor] 🟡 deploys paused"


def test_user_component_survives(cfg, wire) -> None:
    """The peer-send mirror's `[<label> @ <user>]` form is unchanged."""
    TgClient(cfg).send(
        chat_id="404580642", text="привет", sid="bot-squad dev", user="alexey",
        debounce=False,
    )
    assert wire[0]["text"] == "[bot-squad dev @ alexey] привет"


# ---------------------------------------------------------------------------
# (e) — the bytes we send are the bytes we record, or the echo guard goes blind
# ---------------------------------------------------------------------------

def test_spool_records_the_tagged_bytes_the_wire_carried(cfg, wire) -> None:
    """The invariant is older than this ticket and must survive it.
    `echo_guard.recent_send_match` asks "did we send these EXACT bytes to this
    chat", so tagging after the spool write — or leaving the spooled copy
    untagged — would blind it against every forwarded reply."""
    from bot_squad_worker import outbound_log

    TgClient(cfg).send(
        chat_id="404580642", text="Понял, делаю прямо сейчас, подожди минуту.",
        sender_sid=WR_ATT, debounce=False,
    )
    spooled = outbound_log.read_spool(cfg.data_dir, chat_id="404580642")
    assert len(spooled) == 1
    assert spooled[0]["text"] == wire[0]["text"]
    assert spooled[0]["text"].startswith("[watchrobot user-conversation] ")


def test_echo_guard_matches_a_forward_of_the_tagged_message(cfg, wire) -> None:
    """End to end: the stakeholder forwards our reply back, tag included. The
    guard must still recognise it as OUR text and not record it as his."""
    from bot_squad_worker import echo_guard

    body = "Понял, делаю прямо сейчас, подожди минуту."
    TgClient(cfg).send(chat_id="404580642", text=body, sender_sid=WR_ATT,
                       debounce=False)
    delivered = wire[0]["text"]
    verdict = echo_guard.classify_inbound(
        cfg, {"text": delivered, "chat": {"id": 404580642}}, chat_id="404580642",
    )
    assert verdict["author"] == echo_guard.ECHO_AUTHOR
    assert verdict["reason"] == "outbound-match"


def test_echo_guard_does_not_match_the_untagged_body(cfg, wire) -> None:
    """The other half of the ordering, and the reason it is not free: what he
    TYPES carries no tag, so it must NOT be charged as our echo merely because
    we sent the same sentence tagged. Tagging widens the gap between our bytes
    and his — in the safe direction, and this pins that it stays safe."""
    from bot_squad_worker import echo_guard

    body = "Понял, делаю прямо сейчас, подожди минуту."
    TgClient(cfg).send(chat_id="404580642", text=body, sender_sid=WR_ATT,
                       debounce=False)
    verdict = echo_guard.classify_inbound(
        cfg, {"text": body, "chat": {"id": 404580642}}, chat_id="404580642",
    )
    assert verdict["author"] == "user"


def test_debounce_key_still_uses_the_untagged_body(cfg, wire) -> None:
    """The cooldown keys on the CALLER's text, not the tagged bytes, so adding
    a tag must not turn one debounced payload into two distinct ones."""
    client = TgClient(cfg)
    assert client.send(chat_id="404580642", text="то же самое", sender_sid=WR_ATT) is True
    assert client.send(chat_id="404580642", text="то же самое", sender_sid=WR_ATT) is False
    assert len(wire) == 1


# ---------------------------------------------------------------------------
# Module contracts
# ---------------------------------------------------------------------------

def test_sid_predicate_matches_sessions(cfg) -> None:
    """`sender_tag._SID_RE` is a local copy so the transport need not import
    the session machinery. Pinned so the two agree on every input this module
    actually sees — real SIDs, class sender names, display labels."""
    from bot_squad_worker.sessions import _SID_SHAPE_RE

    for value in (BS_ATT, WR_ATT, "S-almdudleer-operator-p298",
                  "deploy_monitor", "voice_intake", "routine:R-0007",
                  "bot-squad operator", "bot-squad user-conversation",
                  "[bot-squad] deploy_monitor", "", "S-no-pane"):
        assert sender_tag.is_sid(value) is bool(_SID_SHAPE_RE.match(value)), value


def test_sid_predicate_is_deliberately_stricter_about_spaces(cfg) -> None:
    """The ONE divergence, recorded so it is not read as a copy slip:
    ``sessions._SID_SHAPE_RE`` uses ``.+`` and so accepts a space inside a SID.
    Here the input may be a DISPLAY label instead of a routing key, and
    ``\\S+`` is what keeps a two-word label from being resolved as a session."""
    from bot_squad_worker.sessions import _SID_SHAPE_RE

    spacey = "S-almdudleer-some window-p1"
    assert _SID_SHAPE_RE.match(spacey)
    assert sender_tag.is_sid(spacey) is False


def test_compose_never_raises_and_falls_back(cfg, monkeypatch) -> None:
    """A tag is a courtesy; a delivery is not."""
    def _boom(*a, **kw):
        raise RuntimeError("session store on fire")

    monkeypatch.setattr(sender_tag, "resolve_label", _boom)
    assert sender_tag.compose(cfg, "привет", sid="bot-squad operator") == (
        "[bot-squad operator] привет"
    )


def test_send_survives_a_broken_tag_resolver(cfg, wire, monkeypatch) -> None:
    """A missing session md, an unreadable bindings file, a store on fire —
    the message still goes out, untagged."""
    def _boom(*a, **kw):
        raise RuntimeError("session store on fire")

    monkeypatch.setattr(sender_tag, "resolve_label", _boom)
    assert TgClient(cfg).send(chat_id="404580642", text="привет",
                              sender_sid=WR_ATT, debounce=False) is True
    assert wire[0]["text"] == "привет"


def test_unknown_session_degrades_to_the_bare_sid(cfg, wire) -> None:
    """No session md, no binding — the pre-T-0758 rendering, not a guess."""
    TgClient(cfg).send(
        chat_id="404580642", text="привет", sender_sid="S-nobody-ghost-p9",
        debounce=False,
    )
    assert wire[0]["text"] == "[S-nobody-ghost-p9] привет"


def test_is_identity_tag_distinguishes_sender_from_class(cfg) -> None:
    for inner in ("bot-squad user-conversation", "watchrobot", BS_ATT,
                  "bot-squad dev @ alexey"):
        assert sender_tag.is_identity_tag(cfg, inner) is True, inner
    for inner in ("voice_intake", "FYI — ответ не требуется", "routine:R-0007",
                  "1/2", ""):
        assert sender_tag.is_identity_tag(cfg, inner) is False, inner
