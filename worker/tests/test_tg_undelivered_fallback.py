"""T-0746 — a message aimed at a reaped session does not evaporate.

The live incident (2026-07-27): the stakeholder answered
``S-almdudleer-rv-pair-trading-signals-poc-review-real--p266``, a WATCHROBOT
session that had since been reaped, from a chat feed bound to BOT-SQUAD. The
only outcome was a TG notice reading "message dropped" — nothing woke, and the
arrival record sat under the wrong project.

Covered here:
  (a) the text reaches the TARGET project's user-conversation, naming the SID
  (b) an impossible fallback is surfaced to the SENDER as a failure, and
      NOTHING is written to the store as content
  (c) the store records the system's account and the human's words separately
  (e) the destination is the target SESSION's project, not the arrival store's
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import bot_squad_worker.tg_listener as TL
from bot_squad_worker import sessions as S


REAPED = "S-almdudleer-rv-pair-trading-signals-poc-review-real--p266"
GID = "gu_dc8262b6cea9098d98e04d7e"
TEXT = "Почини все эти проблемы, и давай делай актуальные сигналы"


def _cfg(tmp_path: Path, *, sessions: dict[str, list[str]] | None = None):
    """A cfg over two registered projects, with session mds on disk.

    ``sessions`` maps slug -> SIDs whose md exists — the on-disk fact
    ``project_of_sid`` reads, and the reason a REAPED session is still
    resolvable (the md survives the reap; verified on the live install).
    """
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True, exist_ok=True)
    for slug, sids in (sessions or {}).items():
        d = data_dir / slug / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        for sid in sids:
            (d / f"{sid}.md").write_text(f"---\nsid: {sid}\n---\n", encoding="utf-8")
    return types.SimpleNamespace(
        tg_bot_token="8206895402:SECRET",
        data_dir=data_dir,
        projects={
            "bot-squad": types.SimpleNamespace(tg_chat="-100123"),
            "watchrobot": types.SimpleNamespace(tg_chat="0"),
        },
        tg_proxy_url="",
        voice_enabled=False,
    )


@pytest.fixture()
def spy(monkeypatch):
    """Capture the two side effects that matter: what we told the sender, and
    what we wrote to the store."""
    state = {"notices": [], "posts": [], "ensured": []}
    monkeypatch.setattr(
        TL, "_notify",
        lambda cfg, chat_id, text, **k: state["notices"].append((chat_id, k.get("thread_id"), text)))
    # `raising=False`: both of these are T-0746 additions, and the two
    # negative guards below (a LIVE session must be unaffected) have to be
    # runnable against unfixed sources too — a guard that can only run on the
    # fixed tree proves nothing about what the fix left alone.
    monkeypatch.setattr(
        TL, "_post_conversation",
        lambda cfg, slug, gid, payload: (state["posts"].append((slug, gid, payload)) or True),
        raising=False)
    monkeypatch.setattr(
        TL, "_ensure_user_conversation",
        lambda cfg, slug, gid, ref, **k: (state["ensured"].append((slug, gid)) or
                                          {"ok": True, "spawned": False}),
        raising=False)
    return state


# ---------------------------------------------------------------------------
# project_of_sid — item (e)'s deterministic destination
# ---------------------------------------------------------------------------

def test_project_of_sid_finds_the_owning_project(tmp_path):
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    assert S.project_of_sid(cfg, REAPED) == "watchrobot"


def test_project_of_sid_is_independent_of_the_arrival_store(tmp_path):
    """The whole point of item (e): bot-squad is where the message ARRIVED and
    it must not win. The md is watchrobot's, so watchrobot is the answer."""
    cfg = _cfg(tmp_path, sessions={
        "bot-squad": ["S-almdudleer-skills-vs-docs-doc-p266"],
        "watchrobot": [REAPED],
    })
    assert S.project_of_sid(cfg, REAPED) == "watchrobot"


def test_project_of_sid_unknown_sid(tmp_path):
    assert S.project_of_sid(_cfg(tmp_path), "S-nobody-nothing-p1") == ""


def test_project_of_sid_tolerates_a_missing_data_dir(tmp_path):
    cfg = types.SimpleNamespace(data_dir=tmp_path / "gone", projects={"x": None})
    assert S.project_of_sid(cfg, REAPED) == ""


def test_project_of_sid_resolves_a_renamed_window(tmp_path):
    """A tmux rename leaves the md at its pre-rename FILENAME with the new SID
    in the body — the mirror of the case _find_session_md handles."""
    cfg = _cfg(tmp_path)
    d = cfg.data_dir / "watchrobot" / "sessions"
    d.mkdir(parents=True)
    (d / "S-almdudleer-oldname-p266.md").write_text(f"---\nsid: {REAPED}\n---\n",
                                                    encoding="utf-8")
    assert S.project_of_sid(cfg, REAPED) == "watchrobot"


def test_project_of_sid_is_deterministic_across_a_collision(tmp_path):
    """Two projects claiming the same SID must always answer the same way —
    sorted slug order, not dict insertion order."""
    cfg = _cfg(tmp_path, sessions={"bot-squad": [REAPED], "watchrobot": [REAPED]})
    assert S.project_of_sid(cfg, REAPED) == "bot-squad"
    cfg.projects = {"watchrobot": cfg.projects["watchrobot"],
                    "bot-squad": cfg.projects["bot-squad"]}
    assert S.project_of_sid(cfg, REAPED) == "bot-squad"


# ---------------------------------------------------------------------------
# item (a) — the message reaches the target project's attendant
# ---------------------------------------------------------------------------

def test_reply_to_a_reaped_session_falls_back(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))

    result = TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID)

    assert result["ok"] is True
    assert result["action"] == "inject_fallback"
    assert result["fallback_slug"] == "watchrobot"
    assert spy["ensured"] == [("watchrobot", GID)]


def test_fallback_carries_the_original_text_verbatim(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))

    TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID)

    user_records = [p for p in spy["posts"] if p[2]["author"] == "user"]
    assert len(user_records) == 1
    slug, gid, payload = user_records[0]
    assert (slug, gid) == ("watchrobot", GID)
    # Unprefixed and unedited — the store holds what he actually said.
    assert payload["text"] == TEXT


def test_fallback_names_the_intended_sid(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))

    TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID)

    ctx = [p for p in spy["posts"] if p[2]["author"] == TL._UNDELIVERED_AUTHOR]
    assert len(ctx) == 1
    assert REAPED in ctx[0][2]["text"]


def test_the_system_line_and_his_words_are_separate_records(tmp_path, monkeypatch, spy):
    """Item (c) applied to our OWN writes: system prose is never stapled onto a
    record that claims the stakeholder wrote it."""
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))

    TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID)

    authors = [p[2]["author"] for p in spy["posts"]]
    assert authors == [TL._UNDELIVERED_AUTHOR, "user"]  # context first, then his text
    assert spy["posts"][0][2]["direction"] == "in"      # we never sent this note


def test_fallback_is_threadless(tmp_path, monkeypatch, spy):
    """A thread id is only meaningful inside its own chat. The sender was in a
    topic bound to ANOTHER project, so carrying it over would file the message
    under a topic of the target project that does not exist."""
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))

    TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID, thread_id=275)

    assert all("thread_id" not in p[2] for p in spy["posts"])


def test_sender_is_told_where_the_message_went(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))

    TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID, thread_id=275)

    assert len(spy["notices"]) == 1
    chat_id, thread, text = spy["notices"][0]
    assert (chat_id, thread) == ("-100123", 275)
    assert "watchrobot" in text and REAPED in text
    assert "dropped" not in text


def test_saturation_is_reported_without_claiming_delivery(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: {"ok": False, "parked": True})

    result = TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID)

    assert result["fallback"] == "parked"
    assert "воркеры" in spy["notices"][0][2]


# ---------------------------------------------------------------------------
# item (b) — an impossible fallback fails LOUDLY and writes nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,sessions,why", [
    ({"gid": ""}, {"watchrobot": [REAPED]}, "unrecognized sender"),
    ({"gid": GID}, {}, "no project claims the sid"),
    ({"gid": GID}, {"watchrobot": [REAPED]}, "empty text"),
])
def test_impossible_fallback_writes_nothing_and_says_so(
    tmp_path, monkeypatch, spy, kwargs, sessions, why,
):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions=sessions)
    monkeypatch.setattr(A, "dispatch", _raiser(A))
    text = "" if why == "empty text" else TEXT

    result = TL._handle_reply(cfg, "-100123", REAPED, text, **kwargs)

    assert result["ok"] is False
    assert result["fallback"] == "impossible"
    assert spy["posts"] == []          # never smuggled into the store as content
    assert len(spy["notices"]) == 1
    assert "НЕ доставлено" in spy["notices"][0][2]


def test_a_store_failure_is_not_reported_as_delivered(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))
    monkeypatch.setattr(TL, "_post_conversation", lambda *a, **k: None)

    result = TL._handle_reply(cfg, "-100123", REAPED, TEXT, gid=GID)

    assert result["ok"] is False and result["fallback"] == "store_failed"
    assert "НЕ доставлено" in spy["notices"][0][2]
    assert spy["ensured"] == []        # nothing to wake an attendant FOR


# ---------------------------------------------------------------------------
# NEGATIVE GUARDS — a LIVE session must be unaffected
# ---------------------------------------------------------------------------

def test_a_live_session_still_gets_the_text_injected(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    calls = []
    monkeypatch.setattr(A, "dispatch",
                        lambda n, p: calls.append((n, p)) or {"ok": True, "lines_sent": 1})
    monkeypatch.setattr(TL, "_clear_stall", lambda *a, **k: None)

    # No `gid`: a LIVE session never needs one, and leaving it off keeps this
    # guard runnable against unfixed sources — which is the point of a guard.
    result = TL._handle_reply(cfg, "-100123", REAPED, TEXT)

    assert result == {"ok": True, "action": "inject", "sid": REAPED,
                      "result": {"ok": True, "lines_sent": 1}}
    assert calls == [("inject_input", {"sid": REAPED, "text": TEXT})]
    assert spy["posts"] == [] and spy["notices"] == []


def test_say_to_a_reaped_session_falls_back_too(tmp_path, monkeypatch, spy):
    """/say is the same verb — "a message addressed to a session" — so it must
    not be left as the unfixed twin of the bug this ticket is about."""
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path, sessions={"watchrobot": [REAPED]})
    monkeypatch.setattr(A, "dispatch", _raiser(A))

    result = TL._handle_slash(cfg, "-100123", "say", f"{REAPED} {TEXT}", gid=GID)

    assert result["action"] == "say_fallback"
    assert result["fallback_slug"] == "watchrobot"
    assert [p[2]["text"] for p in spy["posts"]][-1] == TEXT


def test_say_to_a_live_session_is_unchanged(tmp_path, monkeypatch, spy):
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(A, "dispatch", lambda n, p: {"ok": True, "lines_sent": 1})

    result = TL._handle_slash(cfg, "-100123", "say", f"{REAPED} hello")

    assert result == {"ok": True, "action": "say", "sid": REAPED,
                      "result": {"ok": True, "lines_sent": 1}}


def _raiser(A):
    """`inject_input` failing exactly as it does for a reaped SID."""
    def _dispatch(name, params):
        raise A.ActionError(f"inject_input: no live pane for sid {params['sid']!r}")
    return _dispatch
