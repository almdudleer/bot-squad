"""Tests for messages_parser.py — uses fixture .jsonl files only, never real ~/.claude/."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.messages_parser import TEXT_MAX_BYTES, parse_messages

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_basic_conversation():
    """Parse sample_session.jsonl and verify role/text shape."""
    messages = parse_messages(FIXTURES / "sample_session.jsonl")
    roles = [m["role"] for m in messages]
    # Should have: user, assistant (text), assistant (tool_use), tool, assistant (text)
    assert "user" in roles
    assert "assistant" in roles
    assert "tool" in roles


def test_parse_user_message():
    """User message has correct role and text."""
    messages = parse_messages(FIXTURES / "sample_session.jsonl")
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) >= 1
    assert "Hello" in user_msgs[0]["text"]


def test_parse_assistant_with_text():
    """Assistant message has text field."""
    messages = parse_messages(FIXTURES / "sample_session.jsonl")
    asst = [m for m in messages if m["role"] == "assistant" and m.get("text")]
    assert len(asst) >= 1
    assert "help" in asst[0]["text"].lower() or "read" in asst[0]["text"].lower() or "I can" in asst[0]["text"]


def test_parse_assistant_with_tool_use():
    """Assistant tool_use record has tool_uses list with name and input."""
    messages = parse_messages(FIXTURES / "sample_session.jsonl")
    asst_tools = [
        m for m in messages
        if m["role"] == "assistant" and m.get("tool_uses")
    ]
    assert len(asst_tools) >= 1
    tu = asst_tools[0]["tool_uses"][0]
    assert tu["name"] == "Read"
    assert "file_path" in tu["input"]


def test_parse_tool_result():
    """Tool result record has role='tool' and tool_result field."""
    messages = parse_messages(FIXTURES / "sample_session.jsonl")
    tools = [m for m in messages if m["role"] == "tool"]
    assert len(tools) >= 1
    assert "tool_result" in tools[0]
    assert tools[0]["tool_result"]["tool_use_id"] == "tu_001"


def test_parse_skips_unknown_types(tmp_path: Path):
    """Records with unknown type are silently skipped."""
    jsonl = tmp_path / "test.jsonl"
    lines = [
        json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-05-11T00:00:00Z"}),
        json.dumps({"type": "attachment", "timestamp": "2026-05-11T00:00:01Z"}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "Hi"}, "timestamp": "2026-05-11T00:00:02Z"}),
    ]
    jsonl.write_text("\n".join(lines) + "\n")
    messages = parse_messages(jsonl)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_parse_malformed_lines_skipped(tmp_path: Path):
    """JSON decode errors on individual lines do not crash the parser."""
    jsonl = tmp_path / "test.jsonl"
    lines = [
        "not valid json",
        json.dumps({"type": "user", "message": {"role": "user", "content": "Valid"}, "timestamp": "2026-05-11T00:00:00Z"}),
        "{broken",
    ]
    jsonl.write_text("\n".join(lines) + "\n")
    messages = parse_messages(jsonl)
    assert len(messages) == 1
    assert messages[0]["text"] == "Valid"


def test_parse_limit(tmp_path: Path):
    """Limit parameter caps the number of returned messages."""
    jsonl = tmp_path / "test.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": f"msg {i}"}, "timestamp": "2026-05-11T00:00:00Z"})
        for i in range(10)
    ]
    jsonl.write_text("\n".join(lines) + "\n")
    messages = parse_messages(jsonl, limit=3)
    assert len(messages) == 3


def test_parse_offset(tmp_path: Path):
    """Offset parameter skips the specified number of messages."""
    jsonl = tmp_path / "test.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": f"msg {i}"}, "timestamp": "2026-05-11T00:00:00Z"})
        for i in range(5)
    ]
    jsonl.write_text("\n".join(lines) + "\n")
    messages = parse_messages(jsonl, offset=2)
    assert len(messages) == 3
    assert "msg 2" in messages[0]["text"]


def test_parse_truncates_long_text(tmp_path: Path):
    """Text over TEXT_MAX_BYTES is truncated."""
    big_text = "x" * (TEXT_MAX_BYTES + 1000)
    jsonl = tmp_path / "test.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": big_text}, "timestamp": "T"})
        + "\n"
    )
    messages = parse_messages(jsonl, full=False)
    assert len(messages) == 1
    assert len(messages[0]["text"].encode("utf-8")) <= TEXT_MAX_BYTES + 20  # small margin for "[truncated]"


def test_parse_full_no_truncation(tmp_path: Path):
    """full=True disables truncation."""
    big_text = "x" * (TEXT_MAX_BYTES + 1000)
    jsonl = tmp_path / "test.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": big_text}, "timestamp": "T"})
        + "\n"
    )
    messages = parse_messages(jsonl, full=True)
    assert len(messages) == 1
    assert len(messages[0]["text"]) >= TEXT_MAX_BYTES + 1000


def test_parse_does_not_use_read(tmp_path: Path):
    """Smoke check: parse a file line-by-line (no .read() call — verified by grep in CI)."""
    jsonl = tmp_path / "test.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Hi"}, "timestamp": "T"})
        + "\n"
    )
    # If this passes, the file was parsed correctly. The absence of .read()
    # in messages_parser.py is verified by grep in the self-review step.
    messages = parse_messages(jsonl)
    assert len(messages) == 1


def test_parse_empty_file(tmp_path: Path):
    """Empty file returns empty list."""
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    messages = parse_messages(jsonl)
    assert messages == []
