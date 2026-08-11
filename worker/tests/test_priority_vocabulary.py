"""T-0877 (damage measured on watchrobot as T-0586): the writer and the reader
of ``priority`` must share ONE vocabulary, and something must FAIL when they
stop.

The defect was never in either half. ``bsq task new --priority`` stored any
string it was handed ("priority value (frontmatter, verbatim)"); pickup's
ranking read only ``p1``/``p2``/``p3``; and **nothing in the system compared the
two**. So ``--priority high`` reported success and dropped the ticket out of the
queue permanently — 86 tickets on the watchrobot board, the whole needs-triage
band there for the FORMAT of one field, the oldest invisible for 20.6 days.

Which is why the central test here is not "high parses" but
:func:`test_the_writer_accepts_exactly_what_the_reader_can_rank`: it quantifies
over the writer's whole accepted vocabulary and asserts the reader ranks every
member, and over a junk corpus and asserts neither side takes it. Add a word to
the writer's table without teaching the reader — or the reverse — and this reds.
It is wired into ``.github/workflows/lint.yml`` for the reason T-0738 records:
a guard only the nightly runs leaves a divergence live for hours.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bot_squad_worker.actions as A
from bot_squad_worker import frontmatter as _fm
from bot_squad_worker import pickup, priority
from bot_squad_worker.config import Config

# Values no vocabulary claims — the writer must refuse all of them and the
# reader must rank none of them. `высокий` is the DoD's negative case (the word
# a Russian-speaking session would naturally type); `hotfix` and `closing` are
# real values sitting on the live boards today; `P12` and `200` are the two
# near-misses that must not be guessed into a band.
JUNK = ["высокий", "срочно", "urgent", "hotfix", "closing", "P12", "p-1",
        "200", "100", "50", "highest", "med", "", "   ", "high priority"]


def _variants(value: str) -> list[str]:
    """The same value as a caller would really type it — case and stray spaces."""
    return [value, value.upper(), value.capitalize(), f"  {value} "]


# --- the invariant this ticket exists for ----------------------------------


def test_the_writer_accepts_exactly_what_the_reader_can_rank():
    """The one sentence nobody was saying. Both directions, so neither half can
    quietly grow past the other."""
    for value in priority.ACCEPTED_VALUES:
        for spelling in _variants(value):
            band, flag = pickup.parse_priority(spelling)
            assert priority.priority_valid(spelling), spelling
            assert band is not None and flag is None, (spelling, band, flag)
            # and the writer's canonical form is itself rankable
            assert pickup.parse_priority(priority.normalize_priority(spelling))[0] == band

    for value in JUNK:
        assert not priority.priority_valid(value), value
        assert pickup.parse_priority(value)[0] is None, value
        with pytest.raises(ValueError):
            priority.normalize_priority(value)


def test_the_reader_ranks_the_words_the_board_actually_carries():
    """The 86 recovered tickets, by the four words they were written with —
    high 54 · medium 16 · normal 9 · low 7 on watchrobot, 2026-08-11."""
    assert pickup.parse_priority("high") == (1, None)
    assert pickup.parse_priority("medium") == (2, None)
    assert pickup.parse_priority("normal") == (2, None)
    assert pickup.parse_priority("low") == (3, None)
    assert pickup.parse_priority("critical") == (0, None)
    # case is not a second vocabulary
    assert pickup.parse_priority("HIGH") == (1, None)
    assert pickup.parse_priority("High") == (1, None)


def test_normal_and_medium_are_one_rung_not_two():
    """Two spellings of "the middle" — pinned because a future edit that splits
    them would silently re-rank 9 watchrobot tickets in one direction or 16 in
    the other."""
    assert priority.WORD_BANDS["normal"] == priority.WORD_BANDS["medium"]


def test_pN_and_bare_digits_still_read_as_before():
    """No regression on the shapes that already worked — the fix adds a
    vocabulary, it does not replace one."""
    assert pickup.parse_priority("P1") == (1, None)
    assert pickup.parse_priority("p2") == (2, None)
    assert pickup.parse_priority("3") == (3, None)
    assert pickup.parse_priority(0) == (0, None)
    assert pickup.parse_priority(None) == (None, priority.MISSING_FLAG)
    assert pickup.parse_priority("") == (None, priority.MISSING_FLAG)


# --- the third scale, named rather than merged ------------------------------


def test_a_multi_digit_value_is_reported_as_the_ordering_key_it_is():
    """The web UI writes ``priority`` as a kanban SORT KEY (max+100, midpoint
    inserts — hence 50/100/150/200 on the boards). It is a position in a list,
    not a band, so it stays in triage; what changes is that the reason now says
    WHICH scale it is instead of ``unparseable``, which is a sentence an
    operator can act on."""
    band, flag = pickup.parse_priority("200")
    assert band is None
    assert flag == f"{priority.ORDERING_KEY_FLAG}:200"
    assert priority.UNPARSEABLE_FLAG not in flag


def test_a_single_digit_is_still_a_band_not_an_ordering_key():
    """The two scales are indistinguishable at one digit, and re-reading those
    as ordering keys would demote real tickets on a guess."""
    assert pickup.parse_priority("7") == (7, None)


# --- reader: the 86 come back, WITHOUT any field being rewritten ------------


def _classify(**meta):
    base = {"id": "T-1", "title": "a ticket", "status": "open",
            "updated": "2026-08-11T00:00:00Z"}
    base.update(meta)
    # now = the fixture's `updated`, so staleness never contaminates the read
    return pickup.classify_ticket(base, now_epoch=1786492800.0)


def test_a_word_priority_ticket_is_takeable_and_its_field_is_untouched():
    row = _classify(priority="high")
    assert row["band"] == pickup.BAND_PICKUP
    assert row["effective_priority"] == 1
    assert row["sanity"] == []
    assert row["priority"] == "high", "the stored field must be reported verbatim"


def test_an_unknown_word_still_goes_to_triage_with_its_value_named():
    row = _classify(priority="высокий")
    assert row["band"] == pickup.BAND_TRIAGE
    assert row["sanity"] == [f"{priority.UNPARSEABLE_FLAG}:высокий"]
    assert row["effective_priority"] == pickup.UNRANKED_BAND


# --- writer: the negative test, with its control ----------------------------
#
# DoD wording: "подать `--priority высокий` и убедиться, что КРАСНЕЕТ. Перед ним
# в выводе — зелёная контрольная строка на `--priority high`, иначе тест не
# отличит «отклонил всё» от «отклонил нужное»." Hence the control mints FIRST,
# in the same file, over the same code path.


def _cfg(monkeypatch, tmp_config_dir: Path) -> Config:
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    return cfg


def _backlog_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "test-project" / "backlog"


def test_control_the_writer_still_mints_on_a_vocabulary_word(tmp_path, tmp_config_dir, monkeypatch):
    """CONTROL — must be GREEN. Without it the negative test below cannot tell
    "rejected the bad value" from "rejects everything"."""
    _cfg(monkeypatch, tmp_config_dir)
    res = A.dispatch("task_new", {
        "slug": "test-project", "title": "control: a word priority is accepted",
        "provenance": "T-0877", "priority": "high",
    })
    assert res["ok"] is True
    # Read it back through the frontmatter parser the board is read with, not by
    # grepping the raw text — the value is stored quoted, and a substring match
    # would be measuring the serialiser rather than the field.
    meta = _fm.parse_or_none(Path(res["file_path"]).read_text(encoding="utf-8"))[0]
    # stored CANONICAL, so the vocabulary stops sprawling; the judgement is the
    # author's and it is preserved exactly — band 1 in, band 1 out.
    assert meta["priority"] == "p1"
    assert pickup.parse_priority(meta["priority"]) == (1, None)


def test_the_writer_refuses_a_value_the_queue_cannot_rank(tmp_path, tmp_config_dir, monkeypatch):
    """RED CASE. Before T-0877 this call returned ok:true and the ticket was
    lost to the queue for good."""
    _cfg(monkeypatch, tmp_config_dir)
    with pytest.raises(A.ActionError) as exc:
        A.dispatch("task_new", {
            "slug": "test-project", "title": "negative: a non-vocabulary priority",
            "provenance": "T-0877", "priority": "высокий",
        })
    msg = str(exc.value)
    assert "высокий" in msg
    assert priority.ALLOWED_HELP in msg, "a refusal must say what to type instead"
    # and nothing was written: the gate runs before the id is allocated
    assert not list(_backlog_dir(tmp_path).glob("*negative*.md"))


def test_the_writer_names_the_ordering_key_when_one_is_typed(tmp_path, tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    with pytest.raises(A.ActionError) as exc:
        A.dispatch("task_new", {
            "slug": "test-project", "title": "negative: an ordering key as priority",
            "provenance": "T-0877", "priority": "200",
        })
    assert "ORDERING KEY" in str(exc.value)


# --- the third writer: a hand-edited frontmatter, caught by the lint ---------


def _lint():
    """`scripts/lint/backlog_priority.py`, loaded by path (it is a script, not
    an importable package member)."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    path = Path(__file__).resolve().parents[2] / "scripts" / "lint" / "backlog_priority.py"
    loader = SourceFileLoader("_t0877_lint", str(path))
    spec = importlib.util.spec_from_loader("_t0877_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _ticket(dirpath: Path, tid: str, created: str, prio: str | None) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    fm = f"id: {tid}\ntitle: \"t\"\ncreated: {created}\n"
    if prio is not None:
        fm += f"priority: {prio}\n"
    (dirpath / f"{tid}-x.md").write_text(f"---\n{fm}---\n\nbody\n", encoding="utf-8")


def test_the_lint_passes_every_legitimate_write_and_fails_the_hand_edited_one(tmp_path):
    """CONTROL FIRST, then the offender — a lint that refused everything would
    read identically to one that refused the right thing.

    The controls are the three legitimate writers: a vocabulary word
    (`bsq task new`), the web UI's kanban ordering key, and no priority at all.
    Plus a pre-cutoff ticket carrying a word nobody's vocabulary knows, which
    must stay grandfathered — the board is full of other people's judgement in
    other people's words and this lint does not exist to rewrite it."""
    lint = _lint()
    backlog = tmp_path / "proj" / "backlog"
    _ticket(backlog, "T-9001", "2026-08-12T00:00:00Z", "high")     # bsq writer
    _ticket(backlog, "T-9002", "2026-08-12T00:00:00Z", "200")      # web UI ordering key
    _ticket(backlog, "T-9003", "2026-08-12T00:00:00Z", None)       # missing: a triage question
    _ticket(backlog, "T-9004", "2026-07-01T00:00:00Z", "hotfix")   # pre-cutoff, grandfathered
    assert lint.lint_dir(tmp_path, lint.DEFAULT_CUTOFF) == []

    _ticket(backlog, "T-9005", "2026-08-12T00:00:00Z", "срочно")   # hand-edited
    offenders = lint.lint_dir(tmp_path, lint.DEFAULT_CUTOFF)
    assert [p.name for p, _ in offenders] == ["T-9005-x.md"]
    assert "срочно" in offenders[0][1]


def test_the_lint_reads_the_shared_vocabulary_rather_than_a_fourth_copy():
    assert _lint()._load_vocabulary().WORD_BANDS == priority.WORD_BANDS


def test_an_absent_priority_is_still_absent_not_defaulted(tmp_path, tmp_config_dir, monkeypatch):
    """The 19 `priority-missing` tickets are a SEPARATE question (T-0586 DoD
    item 5): there the field is genuinely empty and triage is the honest answer.
    The writer must not invent a band to make the band look tidy."""
    _cfg(monkeypatch, tmp_config_dir)
    res = A.dispatch("task_new", {
        "slug": "test-project", "title": "no priority given at all",
        "provenance": "T-0877",
    })
    meta = _fm.parse_or_none(Path(res["file_path"]).read_text(encoding="utf-8"))[0]
    assert "priority" not in meta
