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
