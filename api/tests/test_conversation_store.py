"""T-0489: per-(project, user) conversation history store.

A dedicated, file-backed store for the full TG user-conversation thread — the
durable record (like the Claude jsonl session files) that survives session
recycles and feeds the user-conversation seam (M5-T8/T9). Keyed by
(project_slug, global_user_id); append-only, paginated list + text search.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import conversation_store as CS


def test_list_missing_returns_empty(tmp_path: Path):
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["total"] == 0
    assert out["messages"] == []


def test_append_then_list_roundtrip(tmp_path: Path):
    rec = CS.append(
        tmp_path, "proj", "gu_abc",
        author="user", text="hello there",
        timestamp="2026-06-27T00:00:00Z",
    )
    assert rec["author"] == "user"
    assert rec["text"] == "hello there"
    assert rec["timestamp"] == "2026-06-27T00:00:00Z"
    assert rec["attachments"] == []

    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["total"] == 1
    assert out["messages"][0]["text"] == "hello there"


def test_append_defaults_timestamp_when_absent(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi")
    assert rec["timestamp"]  # an ISO timestamp was stamped


def test_append_defaults_channel_to_tg(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi")
    assert rec["channel"] == "tg"
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["channel"] == "tg"


def test_append_records_explicit_channel(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi", channel="mcp")
    assert rec["channel"] == "mcp"
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["channel"] == "mcp"


def test_read_normalizes_missing_channel_to_tg(tmp_path: Path):
    """T-0631: a record written before the channel field existed has no
    ``channel`` key on disk — reads must still expose "tg" (every pre-T-0631
    record is TG-origin), not a missing/None field."""
    p = CS.conv_path(tmp_path, "proj", "gu_abc")
    p.parent.mkdir(parents=True)
    p.write_text('{"timestamp": "2026-06-01T00:00:00Z", "author": "user", '
                 '"text": "pre-migration", "attachments": []}\n', encoding="utf-8")
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["channel"] == "tg"


def test_append_defaults_fyi_to_false(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi")
    assert rec["fyi"] is False
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["fyi"] is False


def test_append_records_explicit_fyi(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi", fyi=True)
    assert rec["fyi"] is True
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["fyi"] is True


def test_read_normalizes_missing_fyi_to_false(tmp_path: Path):
    """T-0660: a record written before the fyi field existed has no ``fyi``
    key on disk — reads must still expose False, not a missing/None field."""
    p = CS.conv_path(tmp_path, "proj", "gu_abc")
    p.parent.mkdir(parents=True)
    p.write_text('{"timestamp": "2026-06-01T00:00:00Z", "author": "user", '
                 '"text": "pre-migration", "attachments": []}\n', encoding="utf-8")
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["fyi"] is False


def test_append_preserves_attachments(tmp_path: Path):
    atts = [{"type": "voice", "file_id": "VID"}]
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="", attachments=atts)
    assert rec["attachments"] == atts
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["attachments"] == atts


def test_messages_are_chronological_append_order(tmp_path: Path):
    for i in range(5):
        CS.append(tmp_path, "proj", "gu_abc", author="user", text=f"m{i}",
                  timestamp=f"2026-06-27T00:00:0{i}Z")
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert [m["text"] for m in out["messages"]] == ["m0", "m1", "m2", "m3", "m4"]


def test_pagination_offset_and_limit(tmp_path: Path):
    for i in range(10):
        CS.append(tmp_path, "proj", "gu_abc", author="user", text=f"m{i}")
    page = CS.list_messages(tmp_path, "proj", "gu_abc", limit=3, offset=2)
    assert page["total"] == 10           # total is the full count, not the page size
    assert page["limit"] == 3
    assert page["offset"] == 2
    assert [m["text"] for m in page["messages"]] == ["m2", "m3", "m4"]


def test_offset_past_end_returns_empty_page(tmp_path: Path):
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="only")
    page = CS.list_messages(tmp_path, "proj", "gu_abc", limit=10, offset=50)
    assert page["total"] == 1
    assert page["messages"] == []


def test_search_filters_case_insensitively(tmp_path: Path):
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="Deploy the worker")
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="unrelated chatter")
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="please DEPLOY again")
    out = CS.search(tmp_path, "proj", "gu_abc", "deploy")
    assert out["total"] == 2
    assert [m["text"] for m in out["messages"]] == ["Deploy the worker", "please DEPLOY again"]


def test_search_paginates_matches(tmp_path: Path):
    for i in range(6):
        CS.append(tmp_path, "proj", "gu_abc", author="user", text=f"deploy {i}")
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="noise")
    out = CS.search(tmp_path, "proj", "gu_abc", "deploy", limit=2, offset=1)
    assert out["total"] == 6
    assert [m["text"] for m in out["messages"]] == ["deploy 1", "deploy 2"]


def test_thread_is_scoped_per_project_and_user(tmp_path: Path):
    CS.append(tmp_path, "proj-a", "gu_1", author="user", text="a1")
    CS.append(tmp_path, "proj-b", "gu_1", author="user", text="b1")
    CS.append(tmp_path, "proj-a", "gu_2", author="user", text="a2-other-user")

    assert [m["text"] for m in CS.list_messages(tmp_path, "proj-a", "gu_1")["messages"]] == ["a1"]
    assert [m["text"] for m in CS.list_messages(tmp_path, "proj-b", "gu_1")["messages"]] == ["b1"]
    assert [m["text"] for m in CS.list_messages(tmp_path, "proj-a", "gu_2")["messages"]] == ["a2-other-user"]


def test_survives_recycle_durable_on_disk(tmp_path: Path):
    """The store IS the durable record: a fresh process (no in-memory state)
    reads back everything written before — this is the continuity substrate."""
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="before recycle",
              timestamp="2026-06-27T00:00:00Z")
    # Simulate a recycle: nothing cached; read straight off disk by path.
    assert CS.conv_path(tmp_path, "proj", "gu_abc").exists()
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["total"] == 1
    assert out["messages"][0]["text"] == "before recycle"


def test_corrupt_line_is_skipped_not_fatal(tmp_path: Path):
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="good")
    p = CS.conv_path(tmp_path, "proj", "gu_abc")
    with p.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="after")
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert [m["text"] for m in out["messages"]] == ["good", "after"]


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "", "a\\b"])
def test_path_traversal_segments_rejected(tmp_path: Path, bad: str):
    with pytest.raises(ValueError):
        CS.append(tmp_path, bad, "gu_abc", author="user", text="x")
    with pytest.raises(ValueError):
        CS.list_messages(tmp_path, "proj", bad)


# ---------------------------------------------------------------------------
# T-0676 items 3/6: optional thread_id — per-topic isolation, back-compat
# when absent/None.
# ---------------------------------------------------------------------------


def test_thread_id_absent_matches_pre_t0676_path(tmp_path: Path):
    """No thread_id -> the EXACT pre-existing path, byte for byte."""
    assert CS.conv_path(tmp_path, "proj", "gu_abc") == CS.conv_path(tmp_path, "proj", "gu_abc", None)
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi")
    assert "thread_id" not in rec


def test_thread_id_isolates_from_bare_and_other_threads(tmp_path: Path):
    """Two different bound topics of the SAME (slug, gid) get their own
    isolated histories — neither collides with each other nor with the bare
    (no-thread) DM thread (the item-6 cross-topic bleed this closes)."""
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="dm message")
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="topic 7 message", thread_id=7)
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="topic 9 message", thread_id=9)

    dm = CS.list_messages(tmp_path, "proj", "gu_abc")
    t7 = CS.list_messages(tmp_path, "proj", "gu_abc", thread_id=7)
    t9 = CS.list_messages(tmp_path, "proj", "gu_abc", thread_id=9)

    assert [m["text"] for m in dm["messages"]] == ["dm message"]
    assert [m["text"] for m in t7["messages"]] == ["topic 7 message"]
    assert [m["text"] for m in t9["messages"]] == ["topic 9 message"]


def test_thread_id_recorded_on_the_stored_record(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi", thread_id=7)
    assert rec["thread_id"] == 7
    out = CS.list_messages(tmp_path, "proj", "gu_abc", thread_id=7)
    assert out["messages"][0]["thread_id"] == 7


def test_thread_id_search_scoped_to_its_own_thread(tmp_path: Path):
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="deploy in DM")
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="deploy in topic", thread_id=7)
    dm_hits = CS.search(tmp_path, "proj", "gu_abc", "deploy")
    t7_hits = CS.search(tmp_path, "proj", "gu_abc", "deploy", thread_id=7)
    assert [m["text"] for m in dm_hits["messages"]] == ["deploy in DM"]
    assert [m["text"] for m in t7_hits["messages"]] == ["deploy in topic"]


def test_thread_id_path_nested_under_gid(tmp_path: Path):
    p = CS.conv_path(tmp_path, "proj", "gu_abc", 7)
    assert p.parent.name == "gu_abc"
    assert p.name == "t7.jsonl"


# ---------------------------------------------------------------------------
# T-0693 Finding B: thread_id=None is overloaded — a genuine DM/non-topic
# message and an explicit tg_bindings General-feed binding (thread_id=None
# bound on purpose) both land in the same bare (slug, gid) file. `general_feed`
# marks a record as the latter so the two are distinguishable on read.
# ---------------------------------------------------------------------------


def test_append_defaults_general_feed_omitted(tmp_path: Path):
    """The overwhelmingly common case (a plain DM) is byte-identical to
    before this flag existed — no `general_feed` key on the stored record."""
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi")
    assert "general_feed" not in rec


def test_append_records_explicit_general_feed(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi", general_feed=True)
    assert rec["general_feed"] is True
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["general_feed"] is True


def test_read_normalizes_missing_general_feed_to_false(tmp_path: Path):
    """A record written before this field existed has no `general_feed` key
    on disk — reads must still expose False, not a missing/None field
    (mirrors the fyi/channel back-compat pattern)."""
    p = CS.conv_path(tmp_path, "proj", "gu_abc")
    p.parent.mkdir(parents=True)
    p.write_text('{"timestamp": "2026-06-01T00:00:00Z", "author": "user", '
                 '"text": "pre-migration", "attachments": []}\n', encoding="utf-8")
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["general_feed"] is False


def test_general_feed_and_dm_still_share_the_bare_thread(tmp_path: Path):
    """The marker distinguishes WHY thread_id is None on each record — it does
    NOT isolate storage (see conversation_store module docstring on why: that
    would require rewiring the attendant-spawn/relay plumbing, out of scope).
    A General-feed message and a plain DM for the same (slug, gid) still
    accrue into the SAME file; the marker keeps each record's origin
    traceable instead of silently indistinguishable."""
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="general feed msg", general_feed=True)
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="dm msg")
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert [m["general_feed"] for m in out["messages"]] == [True, False]


# ---------------------------------------------------------------------------
# T-0746 item (c): `forwarded_from` — the content did not originate with the
# sender. The author vocabulary alone cannot say this: a forward of another
# human's words is still author="user" and would otherwise read as the
# sender's own. See bot_squad_worker.echo_guard for how it is decided.
# ---------------------------------------------------------------------------


def test_append_omits_forwarded_from_when_the_sender_composed_it(tmp_path: Path):
    """NEGATIVE GUARD — the common case stays byte-identical to its pre-T-0746
    shape, so no existing record's meaning shifts."""
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi")
    assert "forwarded_from" not in rec


def test_append_records_forwarded_from(tmp_path: Path):
    rec = CS.append(tmp_path, "proj", "gu_abc", author="user", text="глянь",
                    forwarded_from="user:999")
    assert rec["forwarded_from"] == "user:999"
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["forwarded_from"] == "user:999"


def test_read_normalizes_missing_forwarded_from(tmp_path: Path):
    p = CS.conv_path(tmp_path, "proj", "gu_abc")
    p.parent.mkdir(parents=True)
    p.write_text('{"timestamp": "2026-06-01T00:00:00Z", "author": "user", '
                 '"text": "pre-migration", "attachments": []}\n', encoding="utf-8")
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["forwarded_from"] == ""


def test_our_own_echoed_notice_is_storable_as_an_inbound_system_record(tmp_path: Path):
    """The exact live line that started this ticket, stored the way T-0746
    records it: a system author (no new class), inbound direction, and the body
    preserved verbatim."""
    notice = ("❌ session S-almdudleer-rv-pair-trading-signals-poc-review-real--p266 "
              "not active — message dropped")
    rec = CS.append(tmp_path, "proj", "gu_abc", author="system:bot-echo",
                    text=notice, direction="in", forwarded_from="bot")
    assert rec["author"] == "system:bot-echo"
    assert rec["direction"] == "in"     # explicit: `system:` derives to "out"
    assert rec["text"] == notice
    out = CS.list_messages(tmp_path, "proj", "gu_abc")
    assert out["messages"][0]["direction"] == "in"


def test_echo_author_passes_the_closed_vocabulary(tmp_path: Path):
    """p312's instruction: consume the T-0755 vocabulary, do not add a fourth
    class. `system:bot-echo` is a `system:<kind>`, so the existing gate accepts
    it and an invented class still 400s."""
    assert CS.is_valid_author("system:bot-echo")
    with pytest.raises(ValueError):
        CS.append(tmp_path, "proj", "gu_abc", author="bot:echo", text="x")
