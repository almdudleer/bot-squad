"""T-0883 — a project's OWN group chat is a rigid routing boundary.

The live bug this pins: the project-of-record for an unquoted message was read
from the per-global-user pin (``get_current_project``), which knows nothing
about WHICH CHAT the message arrived in. The stakeholder was pinned to
bot-squad, so every message he typed in guestent's own Telegram supergroup was
recorded in bot-squad's conversation store and answered by bot-squad's
attendant — which then replied to him inside guestent's chat.

His words (2026-08-12, ticket ``## Stakeholder notes``): «чат должен быть
жестко привязан именно к проекту. И договорились что в этом чате всё будет
падать на воркера юзера flomaster … целиком этот чат так должен быть привязан».

Two claims, tested separately because they can fail independently:

1. the chat decides the PROJECT (and the pin is not even consulted), and
2. a dedicated chat's messages land on the project's ONE canonical
   conversation owner, not on each sender's own thread.

Every test states the assertion in terms of what the durable record and the
attendant wake were given — the two things that decide whose thread a message
enters — never an exit code.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import bot_squad_worker.tg_listener as TL


# --- the live topology, reproduced ------------------------------------------
# bot-squad and watchrobot really do share ONE positive-id DM (404580642);
# guestent really does own the negative-id supergroup -1004380986138.
DM_CHAT = "404580642"
GUESTENT_CHAT = "-1004380986138"
HIM = "gu_dc8262b6cea9098d98e04d7e"        # almdudleer
FLOMASTER = "gu_882fd08e873671df11d282ab"  # guestent's conversation owner


def _make_cfg(tmp_path: Path, *, projects: dict[str, str] | None = None):
    """Config-like namespace whose ``projects`` mirror the live tg_chat map."""
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True)
    projects = projects if projects is not None else {
        "watchrobot": DM_CHAT,
        "bot-squad": DM_CHAT,
        "guestent": GUESTENT_CHAT,
    }
    return types.SimpleNamespace(
        tg_bot_token="TESTBOT:TOKEN",
        data_dir=data_dir,
        projects={slug: types.SimpleNamespace(tg_chat=chat)
                  for slug, chat in projects.items()},
        tg_proxy_url="",
        voice_enabled=False,
    )


def _write_groups(cfg, slug: str, *, owner: str | None, members: list[str]):
    """The live shape of ``data/<slug>/groups.json`` (T-0496 store)."""
    d = Path(cfg.data_dir) / slug
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": 1,
        "groups": [{"id": "grp_x", "name": f"{slug}-collaborators",
                    "role": "project_member", "access_scope": "", "prompt": "",
                    "created_at": "2026-08-08T08:04:35Z"}],
        "memberships": {m: "grp_x" for m in members},
    }
    if owner is not None:
        doc["conversation_owner_global_user_id"] = owner
    (d / "groups.json").write_text(json.dumps(doc))


class Spy:
    """Records what the two thread-deciding calls were handed."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, str]] = []   # (slug, gid)
        self.ensured: list[tuple[str, str]] = []    # (slug, gid)
        self.fyi: list[tuple[str, str]] = []        # (slug, gid)
        self.pin_reads: list[str] = []              # gids the pin was read for
        self.asked = 0

    def install(self, monkeypatch, *, pin: str | None = None):
        # ``pin`` survives only so a still-installed stub can PROVE the retired
        # T-0492 pin is never read (T-0640): every test now asserts
        # ``pin_reads == []``, which a removed stub could not distinguish from
        # a pin that was read and ignored.
        monkeypatch.setattr(TL, "append_conversation",
                            lambda c, slug, gid, msg, **k: self.appended.append((slug, gid)))
        monkeypatch.setattr(TL, "append_conversation_fyi",
                            lambda c, slug, gid, **k: self.fyi.append((slug, gid)))
        monkeypatch.setattr(TL, "_ensure_user_conversation",
                            lambda c, slug, gid, ref, **k: self.ensured.append((slug, gid)))
        monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: None)

        def _pin(c, gid):
            self.pin_reads.append(gid)
            return pin
        monkeypatch.setattr(TL, "get_current_project", _pin)

        def _ask(c, chat, **k):
            self.asked += 1
        monkeypatch.setattr(TL, "_ask_which_project", _ask)

        import bot_squad_worker.conversation_locus as CL
        monkeypatch.setattr(CL, "set_locus", lambda *a, **k: None)
        return self


def _identity(monkeypatch, gid: str):
    monkeypatch.setattr(
        TL, "resolve_or_link_sender",
        lambda c, m, slug: {"global_user_id": gid, "created": False, "slug": slug},
    )


def _update(chat_id: str, text: str = "сайт лагает, переворот карточки",
            *, thread_id: int | None = None, reply_to: dict | None = None):
    msg = {
        "message_id": 700,
        "chat": {"id": int(chat_id), "type": "supergroup"},
        "from": {"id": 555123, "first_name": "Alexey", "username": "alx"},
        "text": text,
    }
    if thread_id is not None:
        msg["message_thread_id"] = thread_id
    if reply_to is not None:
        msg["reply_to_message"] = reply_to
    return {"update_id": 1, "message": msg}


# ---------------------------------------------------------------------------
# Claim 1: the chat decides the project — the pin cannot override it.
# ---------------------------------------------------------------------------

def test_dedicated_group_chat_outranks_the_pin(tmp_path, monkeypatch):
    """THE live incident. Pinned to bot-squad, typing in guestent's own chat:
    the record and the attendant wake must both name guestent."""
    cfg = _make_cfg(tmp_path)
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin="bot-squad")

    result = TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert spy.appended == [("guestent", HIM)], "durable record went to the wrong project"
    assert spy.ensured == [("guestent", HIM)], "the woken attendant belongs to the wrong project"
    assert result["slug"] == "guestent"


def test_dedicated_group_chat_never_reads_the_pin(tmp_path, monkeypatch):
    """Stronger than "the pin loses": for a chat that IS a project boundary the
    pin is not consulted at all, so no future pin state can move this chat."""
    cfg = _make_cfg(tmp_path)
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin="bot-squad")

    TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert spy.pin_reads == []


def test_a_dedicated_room_does_not_classify_the_message_at_all(tmp_path, monkeypatch):
    """The chat is the answer, so the per-message classifier must never run.

    Distinct from the tests either side of it, which assert WHERE the message
    landed: this one asserts that the T-0640 resolution ladder is not entered.
    Running it in a dedicated room costs nothing when it agrees and, when it
    does not, either adds friction or names other projects to people who are
    only in this one.

    Lives here rather than in `test_tg_user_worker_routing.py`, where it
    arrived: it drives `_resolve_por` / `_route_to_project` / `_replay_parked`,
    which are T-0640's symbols, and `monkeypatch.setattr` RAISES on a missing
    attribute — so in a commit without T-0640 it fails on its first line
    (caught in review by p194, whose measurement I reproduced).
    """
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(
        TL, "_resolve_por",
        lambda *_a: pytest.fail("a dedicated room must not classify content"),
    )
    seen = {}
    monkeypatch.setattr(
        TL, "_route_to_project",
        lambda cfg, chat, gid, por, msg: seen.update(por=por) or {"ok": True, "slug": por},
    )
    monkeypatch.setattr(TL, "_replay_parked", lambda *_a: 0)

    result = TL._handle_unquoted(cfg, GUESTENT_CHAT, "guestent", HIM, {"text": "hello"})

    assert result == {
        "ok": True, "slug": "guestent",
        "resolved_by": "dedicated_static_group_chat",
    }
    assert seen["por"] == "guestent"


def test_dedicated_group_chat_routes_even_when_unpinned(tmp_path, monkeypatch):
    """Unpinned in a dedicated chat must ROUTE, not ask which project — the
    room already answered that question."""
    cfg = _make_cfg(tmp_path)
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin=None)

    TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert spy.asked == 0, "asked which project inside a project's own chat"
    assert spy.ensured == [("guestent", HIM)]


def test_shared_private_chat_is_decided_by_the_message_not_the_chat(tmp_path, monkeypatch):
    """The negative-id rule must not touch his DM: bot-squad and watchrobot
    share that positive id deliberately, so the CHAT cannot separate them.

    T-0640 changed this test's ANSWER, not its question. It used to assert
    that the pin separated them; the stakeholder retired the pin («эту
    механику с закреплением проекта, давай мы ее уберем») and what separates
    them now is the message itself. The claim under test is the same one and
    is still the one that matters: a shared room is not a boundary, so the
    room does not get to decide. Two messages, one chat, two projects — which
    the pin could never have expressed either."""
    cfg = _make_cfg(tmp_path)
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch)

    TL.handle_update(cfg, _update(DM_CHAT, "in watchrobot сайт лагает"))
    TL.handle_update(cfg, _update(DM_CHAT, "in bot-squad доска не грузится"))

    assert spy.ensured == [("watchrobot", HIM), ("bot-squad", HIM)]
    assert spy.pin_reads == []   # the retired pin is not consulted at all


def test_group_chat_claimed_by_two_projects_is_not_a_boundary(tmp_path, monkeypatch):
    """A negative id named by TWO projects is evidence for neither — the room
    stops being a boundary and resolution falls through to the message.

    T-0640 changed what it falls through TO (per-message content resolution,
    then the ask-when-ambiguous net) but not the claim: an ambiguous room must
    not quietly pick one of its own claimants. That claim is now checked more
    directly than it was against the pin — a message naming no project wakes
    NOBODY and produces a question, instead of landing on whichever project
    happened to be pinned."""
    cfg = _make_cfg(tmp_path, projects={
        "bot-squad": DM_CHAT,
        "guestent": GUESTENT_CHAT,
        "other": GUESTENT_CHAT,
    })
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch)

    result = TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert result["action"] == "ask_project"
    assert spy.asked == 1
    assert spy.ensured == []          # no attendant woken on a guess
    assert spy.pin_reads == []
    # …but the message is still recorded, so it is not lost while we wait.
    assert spy.appended == [("guestent", HIM)]


# ---------------------------------------------------------------------------
# Claim 2: one canonical conversation owner per dedicated chat.
# ---------------------------------------------------------------------------

def test_dedicated_chat_routes_to_the_conversation_owner(tmp_path, monkeypatch):
    """«это должно идти от юзера flomaster» — his message enters flomaster's
    thread, which is the thread flomaster's live codex attendant reads."""
    cfg = _make_cfg(tmp_path)
    _write_groups(cfg, "guestent", owner=FLOMASTER, members=[FLOMASTER, HIM])
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin="bot-squad")

    TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert spy.appended == [("guestent", FLOMASTER)]
    assert spy.ensured == [("guestent", FLOMASTER)], (
        "woke a second per-sender attendant instead of the project's own"
    )


def test_owner_routing_holds_for_a_reply_too(tmp_path, monkeypatch):
    """«целиком этот чат» — the remap sits ahead of the slash/reply/voice fork,
    so a reply-quote in that chat is recorded in the owner's thread as well."""
    cfg = _make_cfg(tmp_path)
    _write_groups(cfg, "guestent", owner=FLOMASTER, members=[FLOMASTER, HIM])
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin="bot-squad")
    monkeypatch.setattr(TL, "_handle_reply",
                        lambda *a, **k: {"ok": True, "action": "reply"})

    TL.handle_update(cfg, _update(
        GUESTENT_CHAT, "да, так и сделай",
        reply_to={"message_id": 100, "text": "[S-flomaster-x-p1] needs your input — waiting"},
    ))

    assert spy.fyi == [("guestent", FLOMASTER)]


def test_no_groups_file_keeps_the_sender_thread(tmp_path, monkeypatch):
    """Default for every project that never opted in: unchanged behaviour."""
    cfg = _make_cfg(tmp_path)
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin="bot-squad")

    TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert spy.ensured == [("guestent", HIM)]


def test_declared_owner_who_is_not_a_member_is_refused(tmp_path, monkeypatch):
    """A stale/mistyped owner id would redirect the project's conversations
    into a gid no session attends. Fall back to the sender and say so."""
    cfg = _make_cfg(tmp_path)
    _write_groups(cfg, "guestent", owner="gu_typo", members=[FLOMASTER, HIM])
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin="bot-squad")

    with pytest.MonkeyPatch.context():
        TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert spy.ensured == [("guestent", HIM)]


def test_malformed_groups_file_keeps_the_sender_thread(tmp_path, monkeypatch):
    """Broken JSON degrades to pre-T-0883 behaviour, never to a guess."""
    cfg = _make_cfg(tmp_path)
    d = Path(cfg.data_dir) / "guestent"
    d.mkdir(parents=True)
    (d / "groups.json").write_text("{not json")
    _identity(monkeypatch, HIM)
    spy = Spy().install(monkeypatch, pin="bot-squad")

    TL.handle_update(cfg, _update(GUESTENT_CHAT))

    assert spy.ensured == [("guestent", HIM)]


# ---------------------------------------------------------------------------
# The helpers, directly.
# ---------------------------------------------------------------------------

def test_dedicated_slug_ignores_positive_ids(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert TL._dedicated_static_group_chat_slug(cfg, DM_CHAT) == ""
    assert TL._dedicated_static_group_chat_slug(cfg, GUESTENT_CHAT) == "guestent"
    assert TL._dedicated_static_group_chat_slug(cfg, "-999") == ""


def test_conversation_owner_gid_resolution(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert TL._dedicated_chat_conversation_gid(cfg, "guestent", HIM) == HIM
    _write_groups(cfg, "guestent", owner=FLOMASTER, members=[FLOMASTER, HIM])
    assert TL._dedicated_chat_conversation_gid(cfg, "guestent", HIM) == FLOMASTER
    assert TL._dedicated_chat_conversation_gid(cfg, "guestent", FLOMASTER) == FLOMASTER
