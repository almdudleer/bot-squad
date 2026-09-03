"""T-0954: the composer is WAITED on, not deferred on forever; and no bare compact.

Two rules from the stakeholder, 2026-09-03, and one measurement behind each.

    «и он должен ждать, если я что-то пишу в окне. Если там просто застрял
    текст, который уже 10 минут там лежит один и тот же, он должен подписывать,
    что кажется, это stale текст в окне.»

The measurement: `routine-handler-p528` deferred its recycle 1554 times in 26h
and the watchrobot operator continuously from 08-31 20:02 to 09-03 09:22, each
behind text nobody was editing — in one case the system's own unsent
`check mail`. The old gate could not tell "he is typing" from "something is
stuck", so it treated both as the former, forever.

    «не должно происходить просто compact, должен всегда handoff + ready for
    compact и только потом compact»

The measurement: compact-and-stay sent a bare `/compact` and left the session
running on a summarized transcript with nothing written down anywhere.

Every test here fails against the pre-T-0954 implementation; the negative
controls at the bottom pin the parts that must NOT change.
"""
from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import autocompact as A
from bot_squad_worker import composer_watch as CW
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import sessions as S

from tests.test_idle_timeout import _make_cfg, _row, seams  # noqa: F401


# --- pane buffers, in the shapes a real Claude Code pane renders ------------

def _pane(composer: str = "") -> str:
    """An idle pane whose composer holds ``composer``."""
    return ("● did some work\n"
            "─────────────────────────────────────────\n"
            f"❯ {composer}\n"
            "─────────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · ← for agents\n")


def _generating_pane() -> str:
    return ("✻ Baking… (12s)\n"
            "❯ \n"
            "  ⏸ manual mode on · esc to interrupt · ← for agents\n")


def _dialog_pane() -> str:
    """A permission prompt — measured verbatim on Claude Code v2.1.259.

    It renders its OWN ``❯`` in front of a numbered option, which is why the
    pre-T-0954 gate read it as "human-typed text is sitting in the composer"
    and deferred forever with nobody at the keyboard.
    """
    return (" Bash command\n"
            "   mail 2>&1 || echo \"no mail\"\n"
            " This command requires approval\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. Yes, and don't ask again for: mail\n"
            "   3. No\n"
            " Esc to cancel · Tab to amend\n")


@pytest.fixture
def cw_cfg(tmp_path):
    """A config whose data_dir is real (composer_watch persists per session)."""
    return types.SimpleNamespace(data_dir=tmp_path / "data"), "bot-squad"


# --- A. what is in the composer, and for how long --------------------------

def test_empty_composer_is_actionable(cw_cfg):
    cfg, slug = cw_cfg
    obs = CW.observe(cfg, slug, "S-x", _pane(""), now=1000.0)
    assert obs["state"] == CW.STATE_EMPTY
    assert obs["state"] in CW.ACTIONABLE


def test_same_text_becomes_stale_after_ten_minutes(cw_cfg):
    """His rule, and the exact failure it fixes: the same bytes for 10 minutes
    are not a person typing, and the lifecycle must stop treating them as one."""
    cfg, slug = cw_cfg
    buf = _pane("check mail")
    first = CW.observe(cfg, slug, "S-x", buf, now=1000.0)
    assert first["state"] == CW.STATE_TYPING      # ...and we WAIT
    assert first["state"] not in CW.ACTIONABLE

    mid = CW.observe(cfg, slug, "S-x", buf, now=1000.0 + CW.STALE_AFTER_SEC - 1)
    assert mid["state"] == CW.STATE_TYPING        # one second short: still waiting

    late = CW.observe(cfg, slug, "S-x", buf, now=1000.0 + CW.STALE_AFTER_SEC)
    assert late["state"] == CW.STATE_STALE
    assert late["state"] in CW.ACTIONABLE
    assert int(late["unchanged_for"]) == CW.STALE_AFTER_SEC
    assert "STALE" in CW.describe(late)           # «должен подписывать»


def test_editing_restarts_the_clock(cw_cfg):
    """«если это что-то, что я прямо сейчас или в последние 10 минут менял, то
    он должен просто ждать в очереди, пока я закончу и отправлю» — a composer he
    is still working in never ages into stale, however long he takes."""
    cfg, slug = cw_cfg
    t = 1000.0
    for keystroke in ("раз", "раз два", "раз два три", "раз два три четыре"):
        obs = CW.observe(cfg, slug, "S-x", _pane(keystroke), now=t)
        assert obs["state"] == CW.STATE_TYPING
        t += CW.STALE_AFTER_SEC - 60   # nine minutes between edits
    # 30+ minutes of elapsed time, and it is STILL not stale — because it moved.
    assert obs["unchanged_for"] < CW.STALE_AFTER_SEC


def test_submitting_forgets_the_record(cw_cfg):
    """He sent it: the next thing typed starts its own clock, not the old one."""
    cfg, slug = cw_cfg
    CW.observe(cfg, slug, "S-x", _pane("a draft"), now=1000.0)
    CW.observe(cfg, slug, "S-x", _pane(""), now=1005.0)          # submitted
    again = CW.observe(cfg, slug, "S-x", _pane("a draft"), now=1006.0)
    assert again["state"] == CW.STATE_TYPING
    assert again["unchanged_for"] == 0.0


def test_a_permission_dialog_is_not_typing_and_never_goes_stale(cw_cfg):
    """The false positive measured on 2026-09-03: a dialog's own ``❯`` read as
    his text. It must be named as a dialog — and, unlike text, must NEVER age
    into `stale`, because proceeding would mean typing into a permission
    prompt."""
    cfg, slug = cw_cfg
    buf = _dialog_pane()
    now = CW.observe(cfg, slug, "S-x", buf, now=1000.0)
    assert now["state"] == CW.STATE_DIALOG
    assert now["state"] not in CW.ACTIONABLE

    hours_later = CW.observe(cfg, slug, "S-x", buf,
                             now=1000.0 + 20 * CW.STALE_AFTER_SEC)
    assert hours_later["state"] == CW.STATE_DIALOG
    assert "dialog" in CW.describe(hours_later)


def test_mid_generation_is_its_own_state(cw_cfg):
    cfg, slug = cw_cfg
    obs = CW.observe(cfg, slug, "S-x", _generating_pane(), now=1000.0)
    assert obs["state"] == CW.STATE_GENERATING
    assert obs["state"] not in CW.ACTIONABLE


def test_composer_text_round_trips_a_draft(cw_cfg):
    """The restore path types this value back into the pane, so it must be the
    draft and not a strip()ed approximation of it."""
    # `_pane` renders "❯ " + the draft, and the reader removes exactly ONE
    # separating space — so his own leading whitespace survives intact.
    assert CW.composer_text(_pane("  двойной отступ")) == "  двойной отступ"
    assert CW.composer_text(_pane("")) == ""
    assert CW.composer_text("no pane at all") is None


def test_stale_window_is_configurable(cw_cfg, monkeypatch):
    cfg, slug = cw_cfg
    monkeypatch.setenv("BOT_SQUAD_COMPOSER_STALE_SEC", "60")
    assert CW.stale_after_sec() == 60
    CW.observe(cfg, slug, "S-x", _pane("stuck"), now=1000.0)
    assert CW.observe(cfg, slug, "S-x", _pane("stuck"),
                      now=1061.0)["state"] == CW.STATE_STALE
    monkeypatch.setenv("BOT_SQUAD_COMPOSER_STALE_SEC", "garbage")
    assert CW.stale_after_sec() == CW.STALE_AFTER_SEC  # never collapses to 0


# --- B. the gate the lifecycle actually calls ------------------------------

def test_gate_waits_while_he_types_and_opens_once_it_is_stale(cw_cfg):
    cfg, slug = cw_cfg
    buf = _pane("недописанное сообщение")
    assert A.composer_free(buf, sid="S-x", now=1000.0, cfg=cfg, slug=slug) is False
    assert A.composer_free(buf, sid="S-x", now=1000.0 + CW.STALE_AFTER_SEC,
                           cfg=cfg, slug=slug) is True


def test_gate_never_opens_on_a_dialog(cw_cfg):
    cfg, slug = cw_cfg
    buf = _dialog_pane()
    assert A.composer_free(buf, sid="S-x", now=1000.0, cfg=cfg, slug=slug) is False
    assert A.composer_free(buf, sid="S-x", now=1000.0 + 50 * CW.STALE_AFTER_SEC,
                           cfg=cfg, slug=slug) is False


def test_gate_without_cfg_keeps_the_pre_t0954_rule(cw_cfg):
    """The degraded path has no state directory to age text against, so it can
    only do what it always did: defer on any text. Pinned so a caller that
    forgets to pass cfg/slug fails SAFE (waits) rather than compacting over
    him."""
    buf = _pane("his half-typed message")
    assert A.composer_free(buf, sid="S-x", now=1e9) is False
    assert A.composer_free(_pane(""), sid="S-x", now=1e9) is True


# --- C. handoff -> ready-for-compact -> compact, on both triggers ----------

def test_no_compact_leaves_before_the_handoff_lands(tmp_path, seams):
    """The heart of «не должно происходить просто compact». Tick 1 asks; the
    /compact does not exist until the write has landed, and the md carries the
    ready state it left from."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    md = data / "bot-squad" / "sessions" / f"{sid}.md"

    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == []
    assert len(seams["calls"]["handoff"]) == 1
    meta = S._read_session_metadata(md)
    assert meta["compact_stay_phase"] == IT.PHASE_STAY_HANDOFF
    assert meta["compact_stay_mark"]

    # ...and a second tick while the write has NOT landed still sends nothing.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []

    # the session writes → ready-for-compact → the squeeze
    meta["compact_stay_mark"] = "ctx:before-the-write"
    S._write_session_metadata(md, meta)
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]
    assert seams["calls"]["terminate"] == []


def test_an_abandoned_handoff_never_falls_back_to_a_bare_compact(tmp_path, seams):
    """Past the handoff timeout the sequence is dropped — NOT completed without
    its first step. A squeeze whose handoff never happened is the exact thing
    this ticket removed, so 'never wedge' must not become 'compact anyway'."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    md = data / "bot-squad" / "sessions" / f"{sid}.md"

    # Arm through the real path, so the mark is the destination's REAL state...
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    meta = S._read_session_metadata(md)
    assert meta["compact_stay_phase"] == IT.PHASE_STAY_HANDOFF
    # ...then let the wait run out with the session never having written.
    meta["compact_stay_armed_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - A.handoff_timeout_sec() - 60))
    S._write_session_metadata(md, meta)
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == []
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(md)
    assert "compact_stay_phase" not in meta
    assert "compact_stay_mark" not in meta
    assert meta["compact_stay_last_at"]          # window closed, no re-arm loop


def test_no_handoff_destination_means_no_compact(tmp_path, seams, monkeypatch):
    """Nowhere to write the forward-state → the squeeze does not happen at all.
    Deliberate: a summarized transcript with nothing written down loses whatever
    the summary dropped."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    monkeypatch.setattr(A, "_resolve_compact_target",
                        lambda cfg_, slug_, rec_: {"kind": "none", "role": "",
                                                   "assignment_id": None})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "compact_stay_phase" not in meta


def test_stale_composer_lets_the_stuck_session_through(tmp_path, seams):
    """End to end, the measured failure: text parked in the composer used to
    defer the sequence every tick forever. Now the first ten minutes wait, and
    then the handoff is asked for."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    seams["state"]["buf"] = _pane("check mail")   # the real parked nudge

    t0 = time.time()
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=t0,
                            user_home="/home/x") is False   # waiting on him
    assert seams["calls"]["handoff"] == []

    assert IT.maybe_recycle(cfg, "bot-squad", row,
                            now=t0 + CW.STALE_AFTER_SEC + 1,
                            user_home="/home/x") is True    # stale → proceed
    assert len(seams["calls"]["handoff"]) == 1


# --- D. an injected nudge never lands on his text --------------------------

class _FakePane:
    """A composer that responds to the three keystrokes the swap uses.

    Deliberately models tmux's own semantics: ``send-keys -- <text>`` types AT
    THE CURSOR (which is why the old path appended to his draft), ``C-u`` kills
    the line, and ``Enter`` submits whatever is in the box.
    """

    def __init__(self, draft: str = ""):
        self.composer = draft
        self.submitted: list[str] = []

    def keys(self, pane_id, *keys):
        if keys == ("C-u",):
            self.composer = ""
        elif keys == ("Enter",):
            self.submitted.append(self.composer)
            self.composer = ""
        elif keys and keys[0] == "--":
            self.composer += keys[1]

    def capture(self, pane_id):
        return _pane(self.composer)


@pytest.fixture
def fake_pane(monkeypatch):
    from bot_squad_worker import input_mux

    pane = _FakePane()
    monkeypatch.setattr(input_mux, "raw_keys", pane.keys)
    monkeypatch.setattr(input_mux, "_DIRECT_INTERLINE_PAUSE_SEC", 0)
    monkeypatch.setattr(input_mux, "_DIRECT_GATE_TIMEOUT_SEC", 0)
    monkeypatch.setattr(input_mux, "_pane_width", lambda pane_id: 200)
    return pane


def test_nudge_is_delivered_ahead_of_his_draft_which_survives(tmp_path, fake_pane):
    """«он должен слать check mail вперед моего текста, а мой текст оставлять
    как есть в поле ввода» — the nudge is submitted as its own message and his
    draft is still sitting in the composer afterwards."""
    from bot_squad_worker import input_mux

    fake_pane.composer = "мой недописанный вопрос про ленту"
    sent = input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                                    capture=fake_pane.capture)
    assert sent == 1
    assert fake_pane.submitted == ["check mail"]          # ...alone, not merged
    assert fake_pane.composer == "мой недописанный вопрос про ленту"


def test_the_pre_t0954_failure_shape_does_not_reproduce(tmp_path, fake_pane):
    """The measured defect, as a regression: his text and the nudge submitted as
    ONE line, with no separator. If the swap is removed this is what comes back."""
    from bot_squad_worker import input_mux

    fake_pane.composer = "мой текст"
    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=fake_pane.capture)
    assert "мой текстcheck mail" not in fake_pane.submitted
    assert all("мой текст" not in s for s in fake_pane.submitted)


def test_his_draft_is_saved_to_disk_before_the_composer_is_touched(tmp_path, fake_pane):
    """The swap is the only place the system deletes something a human typed, so
    a copy exists before the C-u — recoverable even if every later step fails."""
    from bot_squad_worker import input_mux

    fake_pane.composer = "черновик, который нельзя потерять"
    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=fake_pane.capture)
    saved = list(input_mux.drafts_dir(tmp_path).glob("S-x-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "черновик, который нельзя потерять"


def test_an_empty_composer_takes_the_plain_path(tmp_path, fake_pane):
    """No draft, no swap: the ordinary case must not grow a clear/restore cycle
    (and must not write a draft file for text that never existed)."""
    from bot_squad_worker import input_mux

    sent = input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                                    capture=fake_pane.capture)
    assert sent == 1 and fake_pane.submitted == ["check mail"]
    assert not input_mux.drafts_dir(tmp_path).exists()


def test_a_dialog_is_not_treated_as_a_draft(tmp_path, monkeypatch):
    """A permission prompt owns the ``❯``; clearing it would answer it. The
    payload goes out the legacy way instead of C-u'ing a dialog."""
    from bot_squad_worker import input_mux

    keys: list = []
    monkeypatch.setattr(input_mux, "raw_keys", lambda p, *k: keys.append(k))
    monkeypatch.setattr(input_mux, "_DIRECT_INTERLINE_PAUSE_SEC", 0)
    monkeypatch.setattr(input_mux, "_DIRECT_GATE_TIMEOUT_SEC", 0)
    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=lambda pane: _dialog_pane())
    assert ("C-u",) not in keys
    assert ("--", "check mail") in keys


def test_delivery_still_happens_when_the_clear_fails(tmp_path, monkeypatch):
    """Fails toward DELIVERY, never toward silence: a composer that refuses to
    clear gets the pre-T-0954 keystrokes rather than a dropped nudge."""
    from bot_squad_worker import input_mux

    keys: list = []
    monkeypatch.setattr(input_mux, "raw_keys", lambda p, *k: keys.append(k))
    monkeypatch.setattr(input_mux, "_DIRECT_INTERLINE_PAUSE_SEC", 0)
    monkeypatch.setattr(input_mux, "_DIRECT_GATE_TIMEOUT_SEC", 0)
    # a pane whose composer never empties, whatever we send it
    sent = input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                                    capture=lambda pane: _pane("stubborn draft"))
    assert sent == 1
    assert ("--", "check mail") in keys


# --- E. the warning, when he is mid-sentence at the deadline ---------------

def test_he_is_warned_once_while_typing_near_the_cache_edge(tmp_path, seams,
                                                            monkeypatch):
    """«Либо же, если я пишу, он должен вставлять enter и warning … через 5
    минут, сессия выйдет из кеша» — and exactly once per window, because a
    warning every 60 seconds is noise he would learn to ignore."""
    sent: list[tuple] = []
    monkeypatch.setattr(IT, "_send_typing_warning",
                        lambda sid, text: sent.append((sid, text)))
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    seams["state"]["buf"] = _pane("он печатает прямо сейчас")

    # tick 1 — a first sighting cannot tell him from leftover text, so it is
    # silent (see test_no_warning_on_a_first_sighting for why that matters)
    t0 = time.time()
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=t0,
                            user_home="/home/x") is False
    assert sent == []

    # tick 2 — the same text is still there and someone is evidently at it
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=t0 + 60,
                            user_home="/home/x") is True
    assert len(sent) == 1
    assert "выйдет из кеша" in sent[0][1]
    assert seams["calls"]["compact"] == []      # nothing touched his pane

    # ...and the next tick, still typing, says nothing more
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=t0 + 120,
                            user_home="/home/x") is False
    assert len(sent) == 1


def test_a_stale_composer_is_never_warned(tmp_path, seams, monkeypatch):
    """«но не на stale сессию очевидно» — nobody is typing there, so there is
    nobody to tell. That pane gets the compact sequence instead."""
    sent: list[tuple] = []
    monkeypatch.setattr(IT, "_send_typing_warning",
                        lambda sid, text: sent.append((sid, text)))
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    seams["state"]["buf"] = _pane("застрявший текст")

    t0 = time.time()
    IT.maybe_recycle(cfg, "bot-squad", row, now=t0, user_home="/home/x")
    sent.clear()                                    # the first tick may warn
    assert IT.maybe_recycle(cfg, "bot-squad", row,
                            now=t0 + CW.STALE_AFTER_SEC + 1,
                            user_home="/home/x") is True
    assert sent == []                               # stale → no warning...
    assert len(seams["calls"]["handoff"]) == 1      # ...the sequence runs


def test_the_warning_never_fires_before_the_deadline_is_near(tmp_path, seams,
                                                             monkeypatch):
    """The negative control for the trigger itself: typing in a session whose
    cache is nowhere near the edge, and whose context is small, is just typing.
    Without this, 'warn while typing' would degrade into 'warn always'."""
    sent: list[tuple] = []
    monkeypatch.setattr(IT, "_send_typing_warning",
                        lambda sid, text: sent.append((sid, text)))
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    seams["state"]["buf"] = _pane("печатает, но время есть")
    seams["state"]["idle_age"] = 60.0               # a minute in, not 55
    seams["state"]["tokens"] = 5_000                # nothing to squeeze either

    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert sent == []


def test_cache_warning_window_is_five_minutes(tmp_path):
    """The threshold as a table, so the boundary is a thing that can go red."""
    window = IT.idle_timeout_sec()
    assert IT.cache_warning_due(window - IT.WARN_BEFORE_CACHE_SEC, window) is True
    assert IT.cache_warning_due(window - IT.WARN_BEFORE_CACHE_SEC - 1, window) is False
    assert IT.cache_warning_due(None, window) is False
    assert IT.WARN_BEFORE_CACHE_SEC == 300


# --- F. every new behaviour has a switch back ------------------------------

def test_kill_switches_restore_the_pre_t0954_behaviour(tmp_path, seams,
                                                       monkeypatch, fake_pane):
    """Four behaviours, four switches, each rolled back independently. A switch
    nobody has exercised is a switch that does not work, so each one is used
    here rather than merely declared in the ticket."""
    from bot_squad_worker import input_mux

    # 1. the staleness clock — back to "any text defers, forever"
    monkeypatch.setenv("BOT_SQUAD_COMPOSER_WAIT", "0")
    cfg = types.SimpleNamespace(data_dir=tmp_path / "d")
    buf = _pane("stuck text")
    assert A.composer_free(buf, sid="S-x", now=1e9, cfg=cfg,
                           slug="bot-squad") is False
    monkeypatch.delenv("BOT_SQUAD_COMPOSER_WAIT")

    # 2. the draft swap — back to typing over his text
    monkeypatch.setenv("BOT_SQUAD_DRAFT_SWAP", "0")
    fake_pane.composer = "мой текст"
    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=fake_pane.capture)
    assert fake_pane.submitted == ["мой текстcheck mail"]   # the old defect
    monkeypatch.delenv("BOT_SQUAD_DRAFT_SWAP")

    # 3. the mid-typing warning — back to silence
    sent: list = []
    monkeypatch.setattr(IT, "_send_typing_warning",
                        lambda sid, text: sent.append(text))
    monkeypatch.setenv("BOT_SQUAD_TYPING_WARNINGS", "0")
    sid = "S-almdudleer-user-session-p8"
    (tmp_path / "w").mkdir()
    wcfg, data = _make_cfg(tmp_path / "w", sid=sid, window="user-session",
                           task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=(tmp_path / "w") / "repo")
    seams["state"]["buf"] = _pane("он печатает")
    IT.maybe_recycle(wcfg, "bot-squad", row, now=time.time(), user_home="/home/x")
    assert sent == []
    monkeypatch.delenv("BOT_SQUAD_TYPING_WARNINGS")

    # 4. handoff-before-compact — back to the bare /compact
    monkeypatch.setenv("BOT_SQUAD_COMPACT_HANDOFF_FIRST", "0")
    sid2 = "S-almdudleer-user-session-p9"
    (tmp_path / "b").mkdir()
    bcfg, bdata = _make_cfg(tmp_path / "b", sid=sid2, window="user-session",
                            task_id=None)
    brow = _row(sid2, window="user-session", task_id=None,
                cwd_repo=(tmp_path / "b") / "repo")
    seams["state"]["buf"] = "❯ \n"
    seams["calls"]["compact"].clear()
    seams["calls"]["handoff"].clear()
    assert IT.maybe_recycle(bcfg, "bot-squad", brow, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid2]      # straight to the squeeze
    assert seams["calls"]["handoff"] == []          # ...with no handoff


def test_phase_two_waits_if_he_started_typing_after_the_handoff(tmp_path, seams):
    """«он должен ждать, если я что-то пишу в окне» binds the SECOND half of the
    sequence too. The handoff has landed and the /compact is next — but if he
    began a message in the meantime, the squeeze waits for him, not the other
    way round."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    md = data / "bot-squad" / "sessions" / f"{sid}.md"

    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True   # handoff armed
    meta = S._read_session_metadata(md)
    meta["compact_stay_mark"] = "ctx:before-the-write"      # the write landed
    S._write_session_metadata(md, meta)

    seams["state"]["buf"] = _pane("он начал печатать пока сессия писала state")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []

    # he sends it → the squeeze goes ahead on the next tick
    seams["state"]["buf"] = _pane("")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]


# --- G. what the LIVE deploy caught, as regressions ------------------------

def _placeholder_pane(hint: str = 'Try "fix lint errors"') -> str:
    """An EMPTY Claude Code composer — it renders a dim placeholder, not blank.

    Captured verbatim from a live pane immediately after `C-u`. Reading this as
    typed text is what made the delivery swap skip its restore and leave three
    of his unsent messages on disk instead of in their panes (2026-09-03).
    """
    return _pane(hint)


@pytest.mark.parametrize("hint", ['Try "fix lint errors"', 'Try "add a test"',
                                  "Press up to edit queued messages"])
def test_an_empty_composer_placeholder_is_not_text(cw_cfg, hint):
    cfg, slug = cw_cfg
    assert CW.composer_text(_placeholder_pane(hint)) == ""
    obs = CW.observe(cfg, slug, "S-x", _placeholder_pane(hint), now=1000.0)
    assert obs["state"] == CW.STATE_EMPTY
    assert A.composer_free(_placeholder_pane(hint), sid="S-x", now=1000.0,
                           cfg=cfg, slug=slug) is True


def test_a_human_typing_the_placeholder_words_is_still_respected(cw_cfg):
    """The negative control for that exclusion: it is anchored to the WHOLE
    line, so his own sentence that merely starts the same way is still his."""
    cfg, slug = cw_cfg
    assert CW.composer_text(_pane('Try "fix lint errors" on the api package')) \
        == 'Try "fix lint errors" on the api package'
    assert A.composer_free(_pane('Try "fix lint errors" on the api package'),
                           sid="S-x", now=1000.0, cfg=cfg, slug=slug) is False


def test_the_swap_restores_the_draft_over_a_placeholder(tmp_path, monkeypatch):
    """End to end for the live defect: a pane that shows a placeholder once
    cleared must still be treated as cleared, so the restore runs."""
    from bot_squad_worker import input_mux

    class _PlaceholderPane(_FakePane):
        def capture(self, pane_id):
            return _pane(self.composer if self.composer
                         else 'Try "fix lint errors"')

    pane = _PlaceholderPane("мой черновик")
    monkeypatch.setattr(input_mux, "raw_keys", pane.keys)
    monkeypatch.setattr(input_mux, "_DIRECT_INTERLINE_PAUSE_SEC", 0)
    monkeypatch.setattr(input_mux, "_DIRECT_GATE_TIMEOUT_SEC", 0)
    monkeypatch.setattr(input_mux, "_pane_width", lambda pane_id: 200)

    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=pane.capture)
    assert pane.submitted == ["check mail"]
    assert pane.composer == "мой черновик"      # ...and it came BACK


def test_no_warning_on_a_first_sighting(tmp_path, seams, monkeypatch):
    """Measured on the deploy: four sessions were warned «ты печатаешь» within
    eight seconds of a worker restart, at panes nobody had touched for days —
    because a first sighting reads as `typing` by construction. Speaking now
    requires a SECOND sighting; waiting still does not."""
    sent: list = []
    monkeypatch.setattr(IT, "_send_typing_warning",
                        lambda sid, text: sent.append(text))
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    seams["state"]["buf"] = _pane("текст, который лежит тут вторые сутки")

    t0 = time.time()
    IT.maybe_recycle(cfg, "bot-squad", row, now=t0, user_home="/home/x")
    assert sent == []                       # first sighting: say nothing
    IT.maybe_recycle(cfg, "bot-squad", row, now=t0 + 60, user_home="/home/x")
    assert len(sent) == 1                   # second: now it is a real signal


def test_an_in_flight_handoff_is_finalized_even_on_an_exempt_session(tmp_path,
                                                                     monkeypatch):
    """Measured live 2026-09-03: a user-conversation session was armed at
    12:18:07, wrote its checkpoint at 12:19:45, said HANDOFF WRITTEN — and never
    compacted. Every later tick took the exempt branch, which drives a DIFFERENT
    state machine, so `rec['compact']['phase'] == 'writing'` was never looked at
    again. Whoever arms a sequence has to be able to finish it.

    The control is the pairing: the same session with NO arm still routes to the
    exempt path, so this fix cannot be "the exempt branch stopped working".
    """
    seen = {"finalize": 0, "stay_ceiling": 0}
    monkeypatch.setattr(A, "autocompact_enabled", lambda: True)
    monkeypatch.setattr(A, "_maybe_finalize",
                        lambda cfg, slug, rec, compact, now:
                        seen.__setitem__("finalize", seen["finalize"] + 1) or True)
    monkeypatch.setattr(A, "_maybe_compact_stay_ceiling",
                        lambda *a, **k:
                        seen.__setitem__("stay_ceiling",
                                         seen["stay_ceiling"] + 1) or True)
    monkeypatch.setattr(A, "_pane_for", lambda sid, **kw: "%9")
    monkeypatch.setattr(A, "_capture_pane", lambda pane, **kw: _pane(""))
    monkeypatch.setattr(A.recycle_gate, "is_attached", lambda t, **kw: False)
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "bot-squad")

    sid = "S-almdudleer-gu_x-user-conversation-p514"
    cfg, data = _make_cfg(tmp_path, sid=sid,
                          window="gu_x-user-conversation", task_id=None)

    armed = {"sid": sid, "activity": "idle", "role": "user-conversation",
             "compact": {"phase": "writing", "kind": "artifact",
                         "armed_at": 1000.0, "arm_mtime": 999.0,
                         "artifact_path": str(tmp_path / "art.md"),
                         "role": "user-conversation", "stay": True}}
    assert A.maybe_compact(cfg, "bot-squad", armed, "none", now=2000.0) is True
    assert seen["finalize"] == 1        # the arm is driven to its end...
    assert seen["stay_ceiling"] == 0

    # ...and with no arm in flight, the exempt routing is untouched (control)
    plain = {"sid": sid, "activity": "idle", "role": "user-conversation"}
    A.maybe_compact(cfg, "bot-squad", plain, "urgent", now=2000.0)
    assert seen["stay_ceiling"] == 1
    assert seen["finalize"] == 1


def test_the_staleness_clock_ticks_every_tick_not_only_at_the_deepest_gate(
        tmp_path, seams):
    """Measured 2026-09-03: five live sessions each held ONE observation from
    12:17 and still held it at 12:38, because the only caller of `observe` was a
    gate their tick short-circuited before reaching. A clock that stops is not a
    ten-minute rule, so the sweep samples the composer for every active session
    it looks at.

    The control is the record's own `last_seen`: it must advance on a tick that
    takes an early exit (here, the anti-loop stamp blocks any action at all).
    """
    from bot_squad_worker import composer_watch as CW2

    sid = "S-almdudleer-user-session-p8"
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_last_at": recent})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    seams["state"]["buf"] = _pane("залипший текст")

    t0 = time.time()
    # the anti-loop stamp means this tick does nothing...
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=t0,
                            user_home="/home/x") is False
    rec = CW2.state_path(cfg, "bot-squad", sid)
    assert rec.exists(), "the composer was never sampled"
    import json
    first = json.loads(rec.read_text())["last_seen"]

    # ...and the NEXT tick, equally inert, still advances the clock
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=t0 + 60,
                            user_home="/home/x") is False
    assert json.loads(rec.read_text())["last_seen"] > first


def test_a_wrapped_draft_is_never_swapped(tmp_path, monkeypatch):
    """The reader takes the last `❯` line, so a draft that wrapped is captured
    SHORT — swapping on that would restore a truncated version of something he
    wrote. Confined to drafts that provably fit one line; everything else takes
    the legacy path, which merges but never loses."""
    from bot_squad_worker import input_mux

    keys: list = []
    monkeypatch.setattr(input_mux, "raw_keys", lambda p, *k: keys.append(k))
    monkeypatch.setattr(input_mux, "_DIRECT_INTERLINE_PAUSE_SEC", 0)
    monkeypatch.setattr(input_mux, "_DIRECT_GATE_TIMEOUT_SEC", 0)
    monkeypatch.setattr(input_mux, "_pane_width", lambda pane_id: 40)

    long_draft = "x" * 60          # wider than the pane → certainly wrapped
    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=lambda pane: _pane(long_draft))
    assert ("C-u",) not in keys                  # his text was never touched
    assert ("--", "check mail") in keys          # ...and the nudge still went

    # ...and an unknown pane width is treated as "cannot prove it fits"
    keys.clear()
    monkeypatch.setattr(input_mux, "_pane_width", lambda pane_id: 0)
    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=lambda pane: _pane("short"))
    assert ("C-u",) not in keys
