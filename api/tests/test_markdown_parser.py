from pathlib import Path

from app.markdown_parser import parse_task


def test_parse_task_with_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "T-0001-foo.md"
    f.write_text(
        "---\n"
        "id: T-0001\n"
        "title: Daily quiet-recheck digest\n"
        "status: open\n"
        "---\n"
        "\n"
        "Task body here.\n"
        "\n"
        "## Comments\n"
        "### 2026-05-09 Alexey\n"
        "Want this scoped per-subscriber.\n"
    )
    parsed = parse_task(f)
    assert parsed["id"] == "T-0001"
    assert parsed["title"] == "Daily quiet-recheck digest"
    assert parsed["status"] == "open"
    assert "Task body here." in parsed["body"]
    assert parsed["path"].endswith("T-0001-foo.md")


def test_parse_task_without_frontmatter_raises(tmp_path: Path) -> None:
    f = tmp_path / "T-bad.md"
    f.write_text("no frontmatter here\n")
    import pytest
    from app.markdown_parser import ParseError
    with pytest.raises(ParseError):
        parse_task(f)


def test_parse_task_priority_int(tmp_path: Path) -> None:
    f = tmp_path / "T-0050-prio.md"
    f.write_text(
        "---\nid: T-0050\ntitle: P\nstatus: open\npriority: 250\n---\n\nbody\n"
    )
    parsed = parse_task(f)
    assert parsed["priority"] == 250
    assert isinstance(parsed["priority"], int)


def test_parse_task_priority_missing_is_none(tmp_path: Path) -> None:
    f = tmp_path / "T-0051-noprio.md"
    f.write_text("---\nid: T-0051\ntitle: P\nstatus: open\n---\n\nbody\n")
    parsed = parse_task(f)
    assert parsed["priority"] is None


def test_parse_task_priority_str_coerced(tmp_path: Path) -> None:
    f = tmp_path / "T-0052-strprio.md"
    f.write_text(
        "---\nid: T-0052\ntitle: P\nstatus: open\npriority: \"42\"\n---\n\nbody\n"
    )
    parsed = parse_task(f)
    assert parsed["priority"] == 42


def test_parse_task_priority_garbage_is_none(tmp_path: Path) -> None:
    f = tmp_path / "T-0053-bad.md"
    f.write_text(
        "---\nid: T-0053\ntitle: P\nstatus: open\npriority: not-a-number\n---\n\nbody\n"
    )
    parsed = parse_task(f)
    assert parsed["priority"] is None


# ---------------------------------------------------------------------------
# T-0038: initiative / parent_task / blocked_by pass-through
# ---------------------------------------------------------------------------

def test_parse_task_initiative_field(tmp_path: Path) -> None:
    f = tmp_path / "T-0060-init.md"
    f.write_text(
        "---\nid: T-0060\ntitle: I\nstatus: open\n"
        "initiative: multi-server-installation-process.md\n---\n\nbody\n"
    )
    parsed = parse_task(f)
    assert parsed["initiative"] == "multi-server-installation-process.md"


def test_parse_task_initiative_missing(tmp_path: Path) -> None:
    f = tmp_path / "T-0061-noinit.md"
    f.write_text("---\nid: T-0061\ntitle: I\nstatus: open\n---\n\nbody\n")
    parsed = parse_task(f)
    assert parsed.get("initiative") is None


def test_parse_task_parent_and_blocked_by(tmp_path: Path) -> None:
    f = tmp_path / "T-0062-links.md"
    f.write_text(
        "---\nid: T-0062\ntitle: L\nstatus: open\n"
        "parent_task: T-0007\n"
        "blocked_by: [T-0001, T-0002]\n---\n\nbody\n"
    )
    parsed = parse_task(f)
    assert parsed["parent_task"] == "T-0007"
    assert parsed["blocked_by"] == ["T-0001", "T-0002"]
