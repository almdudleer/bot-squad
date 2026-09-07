"""T-0978 — the composer read stops at the box, not at the first visual row.

His words, 2026-09-07:

    «блин, и всё ещё происходит ровно та же хрень с моим текстом, который я
    пишу, ты видишь? мало того, что он его отсылает, так он еще мне назад
    вставляет только первую строчку, остальное просто удаляется!!»

and, correcting the diagnosis an hour later:

    «обрезается не многострочное, а то что просто в терминале выходит за
    пределы одной строки, даже без \\n»

Three clauses, one mechanism, all three reproduced here:

* the reader took the last ``❯`` row, so a draft that WRAPPED was captured as
  its first row — «вставляет только первую строчку»;
* ``_swap_is_safe`` then measured that already-truncated capture against the
  pane width, a check the truncation itself makes pass — «остальное просто
  удаляется»;
* and one ``C-u`` kills one VISUAL ROW, not the composer, so the rows the
  reader never saw were still in the box when the payload's Enter landed on
  them — «мало того, что он его отсылает».

EVERY FIXTURE HERE IS A REAL ``tmux capture-pane``, per T-0957/T-0962's own
lesson that a hand-built fixture encoding the tester's wrong model of the
chrome cannot falsify the bug it is meant to catch — the previous guard for
this exact defect was pinned on a hand-typed 60-character row, a shape the
renderer cannot produce. They were taken on 2026-09-07 from Claude Code
v2.1.263 running on an ISOLATED tmux server (``tmux -L t0978``), never from the
stakeholder's own pane, and each one ships with the draft that was typed to
produce it so the reconstruction is checked against the PRODUCER's value rather
than against a re-reading of the capture.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from bot_squad_worker import composer_watch as CW
from bot_squad_worker import input_mux

FIXTURES = Path(__file__).parent / "fixtures" / "t0978"

#: name -> (pane width, pane height) the capture was taken at.
GEOMETRY = {
    "f_wrap3_228": (228, 40),
    "f_wrap_100": (100, 40),
    "f_hardnl_228": (228, 40),
    "f_midword_228": (228, 40),
    "f_pasted_120": (120, 20),
    "f_tall_120": (120, 20),
}


def _capture(name: str) -> str:
    return io.open(FIXTURES / f"{name}.txt", encoding="utf-8").read()


def _typed(name: str) -> str:
    """The draft that was actually typed to produce the capture."""
    return io.open(FIXTURES / f"{name}.expected", encoding="utf-8").read()


def _block(name: str):
    width, height = GEOMETRY[name]
    return CW.composer_block(_capture(name), width=width, height=height)


# --- A. the read ------------------------------------------------------------

@pytest.mark.parametrize("name", ["f_wrap3_228", "f_wrap_100"])
def test_a_wrapped_draft_is_read_whole_and_reconstructed_exactly(name):
    """DoD 1. The whole box, from the ``❯`` row to the closing rule, and the
    value equals what was typed — character for character, not "about right".

    ``f_wrap_100`` is the same draft shape at a 100-column pane: the geometry
    is derived from the frame, so the reader must not be carrying a constant
    that only happens to be true at the live install's 228 columns.
    """
    block = _block(name)
    assert block is not None
    assert block.complete is True, block.reason
    assert len(block.rows) > 1                      # it really did wrap
    assert block.text == _typed(name)


@pytest.mark.parametrize("name", ["f_wrap3_228", "f_wrap_100"])
def test_the_pre_fix_reader_would_have_lost_most_of_it(name):
    """The falsification arm: this capture DOES exercise the bug.

    Without it the suite above proves only that the new reader agrees with
    itself. The pre-T-0978 reader was "take the last ``❯`` row", which is what
    ``_rune_row`` still is, so the loss it would have caused is measurable here
    rather than asserted from the ticket.
    """
    typed = _typed(name)
    first_row = CW._rune_row(_capture(name))[1]
    assert typed.startswith(first_row)
    assert len(first_row) < len(typed)
    lost = len(typed) - len(first_row)
    assert lost > 100, f"only {lost} chars would have been lost — weak fixture"


def test_a_hard_newline_is_told_apart_from_a_wrap():
    """He said the case is a wrap «даже без \\n», but the reader has to be right
    about both or it cannot be right about either: the two produce the SAME
    shape in a capture — row 1, then a row indented by two spaces.

    The geometry is what separates them. Here row 1 is 16 characters in a
    224-character box and the next row's first word would have fitted easily,
    so the break was his keystroke, not the renderer's.
    """
    block = _block("f_hardnl_228")
    assert block.complete is True, block.reason
    assert block.text == _typed("f_hardnl_228") == "first short line\nsecond short line"


# --- B. what the read REFUSES ----------------------------------------------

@pytest.mark.parametrize("name,expected_in_reason", [
    # A word longer than the box is cut in half, and a capture cannot say
    # whether the break ate a space or split a word.
    ("f_midword_228", "brim"),
    # Claude Code collapses a big paste to `[Pasted text #1]`. Typing that
    # label back would REPLACE what he pasted with 16 literal characters.
    ("f_pasted_120", "paste"),
    # The box scrolls once it hits its height cap and drops rows off the TOP
    # with no marker — the rune simply sits on the first row still visible.
    ("f_tall_120", "scrolled"),
])
def test_an_unreadable_composer_says_so_instead_of_guessing(name,
                                                            expected_in_reason):
    block = _block(name)
    assert block.complete is False
    assert expected_in_reason in block.reason
    assert input_mux._swap_is_safe(block) is False


def test_the_scrolled_capture_really_is_missing_its_head():
    """The refusal above is worth having only if the thing it refuses is really
    lossy. It is: the box shows five rows starting mid-draft, and the text that
    was typed before them is nowhere in the frame."""
    block = _block("f_tall_120")
    assert block.text.startswith("echo142")     # not the draft's first word
    assert "alpha0" not in _capture("f_tall_120")


def test_a_box_with_no_closing_rule_is_not_readable():
    """Without the rule there is no answer to "where does his text end", so the
    read degrades to the pre-T-0978 first-row value AND says it is not whole."""
    block = CW.composer_block("● work\n❯ some text\n  ⏵⏵ bypass permissions\n",
                              width=228, height=40)
    assert block.text == "some text"            # the old value, unchanged
    assert block.complete is False
    assert input_mux._swap_is_safe(block) is False


def test_a_mid_resize_frame_is_refused_by_the_second_instrument():
    """tmux's pane width and the rule the renderer drew are two instruments on
    the same geometry. They agree on a settled frame; a disagreement means the
    frame was caught mid-resize and nothing measured in columns holds in it."""
    buf = _capture("f_wrap3_228")
    assert CW.composer_block(buf, width=228, height=40).complete is True
    off_by_one = CW.composer_block(buf, width=229, height=40)
    assert off_by_one.complete is False
    assert "mid-resize" in off_by_one.reason


# --- C. the guard that could not fire --------------------------------------

def test_the_old_width_guard_passed_the_very_read_it_existed_to_stop():
    """DoD 2, stated as a measurement rather than as a claim.

    ``_swap_is_safe`` used to be ``len(draft) + 6 < pane_width``. Feed it the
    value the OLD reader produced from this real capture and it says yes — the
    truncation is what makes its input short enough to pass, so the guard was
    structurally unable to fire for the case it was written for. Widening the
    threshold cannot fix that; the input was wrong.
    """
    width, _ = GEOMETRY["f_wrap3_228"]
    truncated = CW._rune_row(_capture("f_wrap3_228"))[1]
    assert len(truncated) + 6 < width           # the old guard: "safe"
    assert len(truncated) < len(_typed("f_wrap3_228"))   # ...on a partial read

    # And there is no threshold left to get wrong: the answer now comes from
    # whether the box could be bounded and every break inside it resolved.
    assert input_mux._swap_is_safe(_block("f_wrap3_228")) is True
    assert not hasattr(input_mux, "_COMPOSER_CHROME_COLS")


# --- D. the delivery, end to end -------------------------------------------

class _WrappedPane:
    """A composer that wraps like the real one and kills like the real one.

    Both behaviours are MEASURED, not assumed, and the measurements are pinned
    by :func:`test_the_pane_model_reproduces_the_real_capture` below:

    * it wraps greedily on spaces at ``width - 4`` columns, consuming the space
      it breaks on (pane widths 100 / 137 / 228 -> 96 / 133 / 224 columns);
    * ``C-u`` kills the current VISUAL ROW, not the composer. A 577-character
      draft over three rows took three presses to clear, and after the first
      two of his rows were still in the box. Modelling ``C-u`` as "clear
      everything" is what let the one-press bug live under a green suite.
    """

    def __init__(self, draft: str, width: int, height: int):
        self.draft = draft
        self.width = width
        self.height = height
        self.submitted: list[str] = []
        self.presses = 0

    # -- rendering ----------------------------------------------------------
    def rows(self) -> list[str]:
        content = self.width - CW.COMPOSER_MARGIN_COLS
        out: list[str] = []
        for para in self.draft.split("\n"):
            line = ""
            for word in para.split(" "):
                if not line:
                    line = word
                elif len(line) + 1 + len(word) <= content:
                    line += " " + word
                else:
                    out.append(line)
                    line = word
                while len(line) > content:          # a word wider than the box
                    out.append(line[:content])
                    line = line[content:]
            out.append(line)
        return out

    def capture(self, pane_id: str) -> str:
        rule = "─" * self.width
        rows = self.rows()
        body = [f"❯ {rows[0]}"] + [f"  {r}" for r in rows[1:]]
        return "\n".join(["● did some work", rule, *body, rule,
                          "  ⏵⏵ bypass permissions on"]) + "\n"

    # -- keystrokes ---------------------------------------------------------
    def keys(self, pane_id: str, *keys: str) -> None:
        if keys == ("C-u",):
            self.presses += 1
            rows = self.rows()
            if not self.draft:
                return                              # measured: a no-op
            killed = rows[-1]
            self.draft = self.draft[:len(self.draft) - len(killed)]
            if self.draft.endswith((" ", "\n")):
                self.draft = self.draft[:-1]        # the wrap ate that space
        elif keys == ("Enter",):
            self.submitted.append(self.draft)
            self.draft = ""
        elif keys and keys[0] == "--":
            self.draft += keys[1]

    def paste(self, pane_id: str, text: str) -> None:
        self.draft += text


@pytest.fixture
def wrapped_pane(monkeypatch):
    def _make(name: str):
        width, height = GEOMETRY[name]
        pane = _WrappedPane(_typed(name), width, height)
        monkeypatch.setattr(input_mux, "raw_keys", pane.keys)
        monkeypatch.setattr(input_mux, "_paste_block", pane.paste)
        monkeypatch.setattr(input_mux, "_DIRECT_INTERLINE_PAUSE_SEC", 0)
        monkeypatch.setattr(input_mux, "_CLEAR_KEY_PAUSE_SEC", 0)
        monkeypatch.setattr(input_mux, "_DIRECT_GATE_TIMEOUT_SEC", 0)
        monkeypatch.setattr(input_mux, "_pane_width", lambda p: width)
        monkeypatch.setattr(input_mux, "_pane_height", lambda p: height)
        monkeypatch.setattr(input_mux, "_composer_is_ghost", lambda *a: None)
        return pane
    return _make


@pytest.mark.parametrize("name", ["f_wrap3_228", "f_wrap_100", "f_hardnl_228"])
def test_the_pane_model_reproduces_the_real_capture(wrapped_pane, name):
    """The end-to-end test below is only as good as this: the simulated pane
    must render the typed draft into the SAME rows the real terminal did."""
    pane = wrapped_pane(name)
    real = [r for r in _block(name).rows]
    assert pane.rows() == list(real)


@pytest.mark.parametrize("name", ["f_wrap3_228", "f_wrap_100"])
def test_a_nudge_lands_ahead_of_a_wrapped_draft_and_gives_all_of_it_back(
        wrapped_pane, name, tmp_path):
    """DoD 4's shape, on the bench: a nudge delivered while his draft is
    wrapped across 2+ rows submits the nudge ALONE and leaves the whole draft
    in the composer."""
    pane = wrapped_pane(name)
    typed = _typed(name)

    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=pane.capture)

    assert pane.submitted == ["check mail"]     # his text was NOT sent
    assert pane.draft == typed                  # ...and all of it came back
    assert pane.presses == len(_block(name).rows)   # one C-u per visual row


def test_one_c_u_would_have_submitted_his_text(wrapped_pane, tmp_path):
    """The other half of «мало того, что он его отсылает», isolated.

    Same pane, same payload, but the pre-fix clear — a single ``C-u``. His
    surviving rows are still in the box when the Enter lands, so they go out
    with the nudge. This is why the fix counts presses instead of assuming one.
    """
    pane = wrapped_pane("f_wrap3_228")
    typed = _typed("f_wrap3_228")

    input_mux.raw_keys("%1", "C-u")                      # the pre-fix clear
    input_mux.raw_keys("%1", "--", "check mail")
    input_mux.raw_keys("%1", "Enter")

    assert pane.submitted != ["check mail"]
    assert pane.submitted[0].startswith(typed[:40])
    assert pane.submitted[0].endswith("check mail")


def test_a_draft_with_a_newline_is_pasted_back_not_typed_back(wrapped_pane,
                                                              tmp_path):
    """A restore through ``send-keys`` turns his newline into an Enter, which
    would SUBMIT the draft the swap exists to preserve. It goes back through
    the bracketed paste instead — and with no Enter after it, because restoring
    is putting it back in the box, not sending it."""
    pane = wrapped_pane("f_hardnl_228")
    pasted: list[str] = []
    original_paste = pane.paste

    def _record(pane_id, text):
        pasted.append(text)
        original_paste(pane_id, text)

    input_mux._paste_block = _record                     # monkeypatched above
    try:
        input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                                 capture=pane.capture)
    finally:
        input_mux._paste_block = original_paste

    assert pasted == [_typed("f_hardnl_228")]
    assert pane.draft == _typed("f_hardnl_228")
    assert pane.submitted == ["check mail"]


def test_his_whole_draft_reaches_disk_before_the_composer_is_touched(
        wrapped_pane, tmp_path):
    """The swap is the only place the system deletes something a human typed,
    so it is also the only place that keeps a copy first — and after T-0978 the
    copy is the whole draft, not its first row. The two drafts saved from his
    own pane on 2026-09-06 were 220 bytes each, both ending mid-word."""
    pane = wrapped_pane("f_wrap3_228")
    input_mux.deliver_direct(tmp_path, "S-x", "%1", "check mail",
                             capture=pane.capture)
    saved = sorted(input_mux.drafts_dir(tmp_path).glob("S-x-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == _typed("f_wrap3_228")


# --- E. the other caller of the read ---------------------------------------

def test_editing_a_row_the_old_reader_never_saw_restarts_the_typing_clock(
        tmp_path):
    """`observe` hashes the composer's CONTENT to answer "has it moved" (T-0954).

    Hashing the first visual row meant an edit anywhere below it did not move
    the hash, so a pane he was actively typing in could age into ``stale`` and
    the lifecycle would then act over his live text — the exact outcome T-0954
    was opened to prevent, surviving inside it because the reader under it was
    short. Not a separate fix: it falls out of reading the whole box.
    """
    import types

    cfg = types.SimpleNamespace(data_dir=tmp_path)
    buf = _capture("f_wrap3_228")
    rows = _block("f_wrap3_228").rows
    edited = buf.replace(f"  {rows[-1]}\n", f"  {rows[-1]} tail\n", 1)
    assert edited != buf

    # The edit is INVISIBLE to the pre-T-0978 reader — that is the control.
    assert CW._rune_row(buf)[1] == CW._rune_row(edited)[1]

    CW.observe(cfg, "bot-squad", "S-x", buf, now=1_000.0)
    aged = CW.observe(cfg, "bot-squad", "S-x", buf, now=1_000.0 + 700)
    assert aged["state"] == CW.STATE_STALE          # untouched for 11 minutes

    moved = CW.observe(cfg, "bot-squad", "S-x", edited, now=1_000.0 + 700)
    assert moved["state"] == CW.STATE_TYPING        # ...he is still writing
