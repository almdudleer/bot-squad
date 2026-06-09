"""T-0075: shared frontmatter parser/writer + the cross-reader drift fix.

The dormant bug: the api wrote list-valued task-md fields (``blocked_by`` etc.)
in BLOCK YAML style (``key:\\n- a\\n- b``), but every worker/api reader was
line-based and silently dropped block-style list contents. These tests pin the
fix: ONE pyyaml-based parser reads both styles, every worker-side reader sees
the list, and the writer round-trips inline + hook-compatibly.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import frontmatter as fm


# ---------------------------------------------------------------------------
# parse: both list styles read identically
# ---------------------------------------------------------------------------

BLOCK_TASK = (
    "---\n"
    "id: T-0062\n"
    "title: Links\n"
    "status: open\n"
    "blocked_by:\n"
    "- T-0001\n"
    "- T-0002\n"
    "---\n\n"
    "body here\n"
)

INLINE_TASK = (
    "---\n"
    "id: T-0062\n"
    "title: Links\n"
    "status: open\n"
    "blocked_by: [T-0001, T-0002]\n"
    "---\n\n"
    "body here\n"
)


def test_parse_block_style_list():
    meta, body = fm.parse(BLOCK_TASK)
    assert meta["blocked_by"] == ["T-0001", "T-0002"]
    assert body == "body here\n"


def test_parse_inline_style_list():
    meta, _ = fm.parse(INLINE_TASK)
    assert meta["blocked_by"] == ["T-0001", "T-0002"]


def test_block_and_inline_parse_identically():
    assert fm.parse(BLOCK_TASK)[0] == fm.parse(INLINE_TASK)[0]


def test_parse_no_frontmatter_raises():
    with pytest.raises(fm.FrontmatterError):
        fm.parse("no frontmatter here\n")


def test_parse_or_none_returns_none_without_frontmatter():
    assert fm.parse_or_none("plain text") is None


# ---------------------------------------------------------------------------
# dump: inline lists, None as ~, timestamps unquoted, healing
# ---------------------------------------------------------------------------

def test_dump_emits_inline_lists():
    out = fm.dump_frontmatter({"blocked_by": ["T-0001", "T-0002"]})
    assert out == "blocked_by: [T-0001, T-0002]\n"


def test_dump_none_as_tilde():
    assert fm.dump_frontmatter({"task_id": None}) == "task_id: ~\n"


def test_dump_timestamp_stays_plain_string():
    # No defensive quoting + parses back as a str (not datetime), so callers
    # that fromisoformat() started_at keep working.
    out = fm.dump_frontmatter({"started_at": "2026-06-03T04:37:54Z"})
    assert out == "started_at: 2026-06-03T04:37:54Z\n"
    meta, _ = fm.parse("---\n" + out + "---\n\n")
    assert meta["started_at"] == "2026-06-03T04:37:54Z"
    assert isinstance(meta["started_at"], str)


def test_dump_heals_block_to_inline():
    meta, body = fm.parse(BLOCK_TASK)
    healed = fm.dump(meta, body)
    assert "blocked_by: [T-0001, T-0002]" in healed
    assert "- T-0001" not in healed
    # round-trips
    assert fm.parse(healed)[0]["blocked_by"] == ["T-0001", "T-0002"]


def test_roundtrip_preserves_key_order():
    meta = {"id": "T-1", "status": "open", "blocked_by": ["T-2"], "owner": None}
    parsed, _ = fm.parse(fm.dump(meta, "b\n"))
    assert list(parsed.keys()) == ["id", "status", "blocked_by", "owner"]


# ---------------------------------------------------------------------------
# Legacy fallback: unquoted tmux pane_id (`%` is a reserved YAML indicator)
# ---------------------------------------------------------------------------

LEGACY_SESSION = (
    "---\n"
    "sid: S-almdudleer-operator-p23\n"
    "pane_id: %23\n"
    "status: suspended\n"
    "task_id: ~\n"
    "extra_task_ids: [T-0001, T-0002]\n"
    "guidance_harvested: true\n"
    "started_at: 2026-05-14T12:41:12Z\n"
    "---\n"
)


def test_legacy_unquoted_pane_id_does_not_break_parse():
    meta, _ = fm.parse(LEGACY_SESSION)
    assert meta["pane_id"] == "%23"
    assert meta["status"] == "suspended"
    assert meta["task_id"] is None
    # other fields still type-coerced like the strict path
    assert meta["extra_task_ids"] == ["T-0001", "T-0002"]
    assert meta["guidance_harvested"] is True
    assert meta["started_at"] == "2026-05-14T12:41:12Z"


def test_legacy_session_self_heals_on_rewrite():
    # Reading a legacy %N file then dumping produces valid YAML (pane_id quoted)
    # that round-trips through the strict parser.
    meta, _ = fm.parse(LEGACY_SESSION)
    rewritten = fm.dump(meta, "")
    assert "pane_id: '%23'" in rewritten
    reparsed, _ = fm.parse(rewritten)
    assert reparsed["pane_id"] == "%23"


# ---------------------------------------------------------------------------
# T-0206: a legacy unquoted scalar value that itself contains ``: `` (e.g. a
# title "Recheck model switch: budget caps") makes the strict block fail, so we
# hit the line fallback. The fallback partitions on the FIRST colon, leaving the
# value with an inner ``: `` — which is valid YAML *mapping* syntax. The
# per-value coercer must NOT re-read that into a dict: a frontmatter field is a
# scalar or a list, never a mapping. A dict title was served raw to the SPA and
# crashed it with React error #31 (object-as-child).
# ---------------------------------------------------------------------------

LEGACY_COLON_TITLE = (
    "---\n"
    "id: T-0012\n"
    "title: Recheck model switch: budget caps + usage-API reconciliation\n"
    "status: open\n"
    "---\n\n"
    "- [ ] Recheck model switch: budget caps + usage-API reconciliation.\n"
)


def test_legacy_colon_in_value_stays_string_not_dict():
    meta, _ = fm.parse(LEGACY_COLON_TITLE)
    # The whole point: title is the raw string, never a {"...": "..."} mapping.
    assert isinstance(meta["title"], str)
    assert meta["title"] == "Recheck model switch: budget caps + usage-API reconciliation"
    # Sibling scalars on the same legacy block still type-coerce normally.
    assert meta["id"] == "T-0012"
    assert meta["status"] == "open"


def test_legacy_colon_value_self_heals_to_quoted_yaml():
    # Once read + rewritten, the colon-bearing title is quoted, so the strict
    # parser (not the fallback) handles it on every subsequent read.
    meta, _ = fm.parse(LEGACY_COLON_TITLE)
    rewritten = fm.dump(meta, "")
    reparsed, _ = fm.parse(rewritten)
    assert reparsed["title"] == "Recheck model switch: budget caps + usage-API reconciliation"
    assert isinstance(reparsed["title"], str)


# ---------------------------------------------------------------------------
# as_list normalizer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, []),
    ("~", []),
    ([], []),
    (["T-1", "T-2"], ["T-1", "T-2"]),
    ("[T-1, T-2]", ["T-1", "T-2"]),
    ("T-1", ["T-1"]),
    (["T-1", "~", "T-2"], ["T-1", "T-2"]),
])
def test_as_list(value, expected):
    assert fm.as_list(value) == expected


# ---------------------------------------------------------------------------
# DoD: a block-style `blocked_by` is read by EVERY worker-side reader
# ---------------------------------------------------------------------------

def test_dod_block_style_blocked_by_read_by_autonomous_parse_task(tmp_path: Path):
    from bot_squad_worker import autonomous
    p = tmp_path / "T-0062-links.md"
    p.write_text(BLOCK_TASK)
    task = autonomous._parse_task(p)
    assert task["blocked_by"] == ["T-0001", "T-0002"]  # pre-T-0075: dropped → ""


def test_dod_block_style_extra_task_ids_read_by_session_readers(tmp_path: Path):
    """Block-style session list field read by both worker session readers."""
    from bot_squad_worker import intersession, sessions
    slug = "p"
    sess_dir = tmp_path / "data" / slug / "sessions"
    sess_dir.mkdir(parents=True)
    sid = "S-almdudleer-foo-p2"
    (sess_dir / f"{sid}.md").write_text(
        "---\n"
        f"sid: {sid}\n"
        "status: active\n"
        "task_id: T-0075\n"
        "extra_task_ids:\n"
        "- T-0091\n"
        "- T-0100\n"
        "---\n"
    )
    # sessions._read_session_metadata
    meta = sessions._read_session_metadata(sess_dir / f"{sid}.md")
    assert meta["extra_task_ids"] == ["T-0091", "T-0100"]
    # intersession._list_session_sids
    cfg = types.SimpleNamespace(data_dir=tmp_path / "data")
    rows = intersession._list_session_sids(cfg, slug)
    assert rows[0][1]["extra_task_ids"] == ["T-0091", "T-0100"]


def test_dod_block_style_session_history_appended_correctly(tmp_path: Path):
    """A task md with a block-style session_history is read + appended inline."""
    from bot_squad_worker import sessions
    backlog = tmp_path / "backlog"
    backlog.mkdir()
    (backlog / "T-0075-x.md").write_text(
        "---\n"
        "id: T-0075\n"
        "status: in_progress\n"
        "session_history:\n"
        "- S-old-p1\n"
        "---\n\n"
        "body\n"
    )
    changed = sessions._append_task_session_history(backlog, "T-0075", "S-new-p2")
    assert changed is True
    meta, _ = fm.parse((backlog / "T-0075-x.md").read_text())
    assert meta["session_history"] == ["S-old-p1", "S-new-p2"]
    # idempotent
    assert sessions._append_task_session_history(backlog, "T-0075", "S-new-p2") is False
