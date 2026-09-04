"""T-0962 — the composer swap fed itself one NO-BREAK SPACE per delivery.

His report: «ghost от клода распознается как мой текст, потом вставляется и
ghost, и /compact, с какими-то табами между ними, и еще и не отсылается», and
then «критично что check mail перестал отправляться … коммуникация между
сессиями по сути сломана».

The "табы" are U+00A0. Claude Code separates the ❯ rune from the composer
content with a NO-BREAK SPACE, and `composer_text` stripped only an ASCII one,
so the separator was read as the first character of his draft. The delivery
restore typed that value back verbatim, the next tick read it one NBSP longer,
and the padding compounded.

WHY 4135 EXISTING TESTS WERE BLIND TO IT: every pane fixture in the suite is
built by `test_t0954_composer_watch._pane`, which renders `f"❯ {composer}"` —
an ASCII space. A fixture that encodes the same wrong model as the code cannot
falsify it. The chrome below is transcribed from a real `tmux capture-pane` of
live pane %513 (2026-09-04T14:2xZ) instead; only the message body is neutral.
"""
from __future__ import annotations

import pytest

from bot_squad_worker import composer_watch as CW

NBSP = " "


def _real_pane(composer: str = "", *, sep: str = NBSP) -> str:
    """A pane rendered the way the live one is: ❯ + U+00A0 + content."""
    return ("● did some work\n"
            "─────────────────────────────────────────\n"
            f"❯{sep}{composer}\n"
            "─────────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · ← for agents\n")


def test_the_nbsp_separator_is_chrome_not_his_first_character():
    """The measurement that named the bug: on the live pane the character after
    the rune is U+00A0, and `startswith(" ")` never matched it."""
    assert CW.composer_text(_real_pane("ок, заводи тикет по группировке")) \
        == "ок, заводи тикет по группировке"


def test_the_restore_is_a_fixed_point_and_no_longer_feeds_itself():
    """THE regression: read → restore → read must converge, not grow.

    `_deliver_ahead_of_draft` types the captured value back into the composer,
    so `composer_text(pane(x)) == x` is the property that closes the loop. On
    the old reader each pass returned one NBSP more than the last — measured on
    disk as operator p513's drafts going 59, 61, 63 … 87 bytes.
    """
    original = "ок, заводи тикет по группировке"
    text = original
    seen = []
    for _ in range(6):
        text = CW.composer_text(_real_pane(text))
        seen.append(len(text))
    assert seen == [len(original)] * 6, seen
    assert text == original


def test_an_already_poisoned_composer_drains_in_one_delivery():
    """His live pane carried the accumulated padding in front of a real
    sentence (15 NBSP + «ок, заводи тикет по группировке»). Reading it must
    hand back the sentence, so the restore writes the clean value."""
    poisoned = NBSP * 15 + "ок, заводи тикет по группировке"
    assert CW.composer_text(_real_pane(poisoned)) \
        == "ок, заводи тикет по группировке"


def test_a_composer_holding_only_padding_is_empty_not_a_draft():
    """All-padding must read as empty, or the swap treats chrome as something
    worth protecting and parks the payload behind it."""
    assert CW.composer_text(_real_pane(NBSP * 40)) == ""


def test_the_no_suggestion_ghost_is_an_empty_box():
    """Claude Code's fourth placeholder. It reached disk as a whole saved
    "draft" whose entire content was one NBSP plus this string (p603)."""
    assert CW.composer_text(_real_pane("<no suggestion>")) == ""
    assert CW.composer_text(_real_pane(NBSP * 12 + "<no suggestion>")) == ""


def test_his_own_leading_ascii_spaces_still_survive():
    """The negative control for the drain: only U+00A0 is chrome. Leading
    ASCII whitespace is his and the restore must put it back byte for byte."""
    assert CW.composer_text(_real_pane("  двойной отступ")) == "  двойной отступ"


def test_the_ascii_separator_is_still_accepted():
    """Older/other renderers use a plain space; both are chrome."""
    assert CW.composer_text(_real_pane("привет", sep=" ")) == "привет"


@pytest.mark.parametrize("body,expected", [
    (NBSP + "check mail", "check mail"),
    (NBSP * 60 + "check mail", "check mail"),
    ("check mail", "check mail"),
    (NBSP, ""),
    ("", ""),
])
def test_the_padding_boundary_is_a_table(body, expected):
    assert CW.composer_text(_real_pane(body)) == expected


# --- the ghost: Claude Code marks what is NOT his with SGR 2 (faint) --------
#
# Every line below is transcribed from `tmux capture-pane -p -e` — the live
# operator pane %513 and uc pane %514 on 2026-09-04, and an isolated throwaway
# Claude Code session brought up on its own tmux server for the two controls.
# Typing one character replaces the whole ghost, so real and faint never mix.

GHOST_LAST_MESSAGE = "\x1b[39m❯\xa0\x1b[2mcheck mail\x1b[0m"
GHOST_LONGER = ("\x1b[39m❯\xa0\x1b[2mresume checking mail on the "
                "trigger too\x1b[0m")
GHOST_PLACEHOLDER = '\x1b[39m❯\xa0\x1b[2mTry "how do I log an error?"\x1b[0m'
HIS_REAL_TEXT = "\x1b[39m❯\xa0привет это настоящий текст"
HIS_ONE_CHAR = "\x1b[39m❯\xa0п"
EMPTY_BOX = "\x1b[38;5;241m❯\xa0\x1b[39m"


def _ansi_pane(rune_line: str) -> str:
    return ("● did some work\n"
            "────────────────────────────\n"
            f"{rune_line}\n"
            "────────────────────────────\n")


@pytest.mark.parametrize("line", [GHOST_LAST_MESSAGE, GHOST_LONGER,
                                  GHOST_PLACEHOLDER])
def test_a_faint_composer_is_an_empty_box(line):
    from bot_squad_worker import input_mux
    assert input_mux._composer_is_ghost(
        "%513", lambda _p: _ansi_pane(line)) is True


@pytest.mark.parametrize("line", [HIS_REAL_TEXT, HIS_ONE_CHAR])
def test_his_own_typing_carries_no_faint_and_is_never_called_a_ghost(line):
    """The control that matters: this probe may only ever downgrade "there is
    a draft" to "empty". Saying ghost about HIS text would erase it."""
    from bot_squad_worker import input_mux
    assert input_mux._composer_is_ghost(
        "%513", lambda _p: _ansi_pane(line)) is not True


def test_an_unanswerable_probe_returns_none_and_changes_nothing():
    from bot_squad_worker import input_mux
    assert input_mux._composer_is_ghost("%513", lambda _p: "") is None
    assert input_mux._composer_is_ghost("%513", lambda _p: "no rune") is None
    # No faint anywhere: nothing to conclude, keep the old behaviour.
    assert input_mux._composer_is_ghost(
        "%513", lambda _p: _ansi_pane(EMPTY_BOX)) is None

    def _boom(_p):
        raise OSError("tmux went away")
    assert input_mux._composer_is_ghost("%513", _boom) is None


def test_unfainted_splits_the_line_the_way_the_renderer_meant_it():
    from bot_squad_worker import input_mux
    assert input_mux._unfainted("\xa0\x1b[2mcheck mail\x1b[0m") \
        == ("\xa0", True)
    assert input_mux._unfainted("\xa0привет") == ("\xa0привет", False)
    # A faint run that ENDS still leaves his tail visible.
    assert input_mux._unfainted("\xa0\x1b[2mdim\x1b[22m tail") \
        == ("\xa0 tail", True)


def test_the_swap_does_not_protect_a_ghost(monkeypatch):
    """End to end for his p0: «написано check mail, и не отправлено».

    The old path read the ghost as a draft, saved it, and RESTORED it by typing
    it — turning a dim hint into real unsent text that then blocked the pane.
    """
    from bot_squad_worker import input_mux
    # raising=False on purpose: this is the one BEHAVIOURAL red control. Against
    # the old module the attribute simply does not exist, and the assertion
    # below then fails on the VALUE — `_live_draft` hands back "check mail",
    # the ghost, as though he had typed it — rather than on a missing symbol.
    monkeypatch.setattr(input_mux, "_capture_pane_ansi",
                        lambda _p: _ansi_pane(GHOST_LAST_MESSAGE),
                        raising=False)
    plain = _real_pane("check mail")
    assert input_mux._live_draft(lambda _p: plain, "%513") == ""


def test_the_swap_still_protects_his_text_when_the_probe_says_so(monkeypatch):
    from bot_squad_worker import input_mux
    monkeypatch.setattr(input_mux, "_capture_pane_ansi",
                        lambda _p: _ansi_pane(HIS_REAL_TEXT))
    plain = _real_pane("привет это настоящий текст")
    assert input_mux._live_draft(lambda _p: plain, "%513") \
        == "привет это настоящий текст"
