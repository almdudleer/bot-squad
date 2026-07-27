"""T-0755: the authorship convention + the inbound/outbound distinction.

The store looked like a transcript and was an inbox. These pin the three things
that make it a transcript again: a CLOSED author vocabulary (so a reader can
trust `author` to answer "did a human say this?"), a `direction` derived from it
(so months of existing history classify correctly with NO migration), and a
chronological read (so a drain that lags a send can't show a reply before the
message it answered).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import conversation_store as CS


SID = "S-almdudleer-gu_dc8262b6cea9098d98e04d7e-user-conversation-p5"
SLUG, GID = "bot-squad", "gu_abc"


def _read(tmp_path: Path) -> list[dict]:
    return CS.list_messages(tmp_path, SLUG, GID, limit=100)["messages"]


# ---------------------------------------------------------------------------
# The closed vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("author", ["user", f"session:{SID}", "system:task-lifecycle"])
def test_valid_authors_are_accepted(tmp_path: Path, author: str) -> None:
    assert CS.append(tmp_path, SLUG, GID, author=author, text="x")["author"] == author


@pytest.mark.parametrize("author", ["user:alexey", "session", "session:", "bot:x", ""])
def test_invented_author_classes_are_refused(tmp_path: Path, author: str) -> None:
    """`append` is the ONE write path into this store, so refusing here is what
    turns the convention into a guarantee T-0746 can rely on."""
    with pytest.raises(ValueError, match="vocabulary"):
        CS.append(tmp_path, SLUG, GID, author=author, text="x")


def test_we_can_never_write_as_the_stakeholder(tmp_path: Path) -> None:
    """`author="user"` means A HUMAN wrote this content. An outbound one would
    be us putting words in his mouth, in the very record used to audit what was
    said."""
    with pytest.raises(ValueError, match="cannot be direction"):
        CS.append(tmp_path, SLUG, GID, author="user", text="x", direction="out")


def test_unknown_direction_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="direction must be"):
        CS.append(tmp_path, SLUG, GID, author="user", text="x", direction="sideways")


# ---------------------------------------------------------------------------
# direction — derived, not stored, so nothing on disk has to change
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("author,expected", [
    ("user", "in"),
    (f"session:{SID}", "out"),
    ("system:task-lifecycle", "out"),
])
def test_direction_is_derived_from_the_author(author: str, expected: str) -> None:
    assert CS.default_direction(author) == expected


def test_a_derived_direction_is_not_written_to_disk(tmp_path: Path) -> None:
    """An inbound record must stay BYTE-IDENTICAL to its pre-T-0755 shape — it
    is the overwhelming majority of writes and the one a live reader is most
    likely to be mid-parse on."""
    rec = CS.append(tmp_path, SLUG, GID, author="user", text="привет")
    assert "direction" not in rec
    raw = (tmp_path / "_mothership" / "conversations" / SLUG / f"{GID}.jsonl").read_text()
    assert '"direction"' not in raw


def test_an_overriding_direction_IS_written(tmp_path: Path) -> None:
    """The one case a reader could not infer: an inbound system record."""
    rec = CS.append(tmp_path, SLUG, GID, author="system:tg-listener",
                    text="diagnostic", direction="in")
    assert rec["direction"] == "in"
    assert _read(tmp_path)[0]["direction"] == "in"


def test_existing_history_classifies_correctly_with_no_migration(tmp_path: Path) -> None:
    """A file written before T-0755 carries no `direction` at all. Deriving it
    on READ is what retro-classifies the 233 session: and 198 system: records
    already on the live install — rewriting that file to say so would be exactly
    the silent edit this ticket exists to prevent."""
    p = tmp_path / "_mothership" / "conversations" / SLUG / f"{GID}.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        '{"timestamp": "2026-07-18T12:57:26Z", "author": "user", "text": "a", '
        '"attachments": [], "channel": "tg", "fyi": false}\n'
        '{"timestamp": "2026-07-18T12:57:27Z", "author": "session:' + SID + '", '
        '"text": "b", "attachments": [], "channel": "tg", "fyi": false}\n'
        '{"timestamp": "2026-07-18T12:57:28Z", "author": "system:task-lifecycle", '
        '"text": "c"}\n', encoding="utf-8")
    got = _read(tmp_path)
    assert [r["direction"] for r in got] == ["in", "out", "out"]
    assert [r["delivered"] for r in got] == [False, False, False]
    assert [r["channel"] for r in got] == ["tg", "tg", "tg"]  # T-0631 default intact


# ---------------------------------------------------------------------------
# delivered — the gate that stops a recorded send being sent again
# ---------------------------------------------------------------------------

def test_delivered_is_stored_only_when_true(tmp_path: Path) -> None:
    assert "delivered" not in CS.append(tmp_path, SLUG, GID, author="user", text="a")
    rec = CS.append(tmp_path, SLUG, GID, author=f"session:{SID}", text="b",
                    direction="out", delivered=True)
    assert rec["delivered"] is True


# ---------------------------------------------------------------------------
# Chronological read — the interleave itself
# ---------------------------------------------------------------------------

def test_reads_are_ordered_by_time_not_by_arrival(tmp_path: Path) -> None:
    """The outbound half arrives via a drain tick that can lag its send, so
    arrival order would show a reply AFTER a message it preceded — in the one
    file whose job is to show who spoke when."""
    CS.append(tmp_path, SLUG, GID, author="user", text="q1",
              timestamp="2026-07-27T04:57:58Z")
    CS.append(tmp_path, SLUG, GID, author="user", text="q2",
              timestamp="2026-07-27T04:59:07Z")
    # drained late, but SENT between the two
    CS.append(tmp_path, SLUG, GID, author=f"session:{SID}", text="a1",
              timestamp="2026-07-27T04:58:34Z", direction="out", delivered=True)
    assert [r["text"] for r in _read(tmp_path)] == ["q1", "a1", "q2"]


def test_ordering_is_stable_for_equal_timestamps(tmp_path: Path) -> None:
    """The live thread has three records sharing 2026-07-27T04:57:58Z — they
    must keep their arrival order among themselves."""
    for t in ("x", "y", "z"):
        CS.append(tmp_path, SLUG, GID, author="user", text=t,
                  timestamp="2026-07-27T04:57:58Z")
    assert [r["text"] for r in _read(tmp_path)] == ["x", "y", "z"]


def test_search_also_sees_the_outbound_half(tmp_path: Path) -> None:
    """"What exactly did we tell him about the glossary?" is the question the
    operator could not answer — search must reach outbound records too."""
    CS.append(tmp_path, SLUG, GID, author="user", text="а глоссарий быстрее?")
    CS.append(tmp_path, SLUG, GID, author=f"session:{SID}",
              text="глоссарий не быстрее — 23.9s -> 24.9s", direction="out",
              delivered=True)
    hits = CS.search(tmp_path, SLUG, GID, "глоссарий")["messages"]
    assert len(hits) == 2
    assert any(h["direction"] == "out" for h in hits)
