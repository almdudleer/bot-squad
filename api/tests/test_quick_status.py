"""Unit tests for the project quick-status aggregator (T-0016).

Pure-function tests over the session row shape — no FastAPI / worker needed.
The contract is locked here; T-0025 cross-server view and T-0008 picker
dropdown both consume the same enum.
"""
from __future__ import annotations

from app.quick_status import aggregate_project_status


def _row(status: str, **kwargs) -> dict:
    """Minimal session row shaped like list_sessions output."""
    base = {
        "sid": kwargs.pop("sid", f"S-x-{status}-p0"),
        "status": status,
        "window": "",
        "cwd": "",
        "started_at": None,
        "paused_at": None,
        "suspended_at": None,
    }
    base.update(kwargs)
    return base


def test_empty_rows_returns_idle_with_no_since():
    out = aggregate_project_status([])
    assert out == {"status": "idle", "status_since": None}


def test_any_active_wins_as_working():
    rows = [
        _row("suspended", suspended_at="2026-05-14T10:00:00Z"),
        _row("paused",    paused_at="2026-05-14T11:00:00Z"),
        _row("active",    started_at="2026-05-14T09:00:00Z"),
    ]
    out = aggregate_project_status(rows)
    assert out["status"] == "working"
    assert out["status_since"] == "2026-05-14T09:00:00Z"


def test_working_since_is_max_started_at_among_actives():
    rows = [
        _row("active", started_at="2026-05-14T09:00:00Z", sid="a"),
        _row("active", started_at="2026-05-14T12:30:00Z", sid="b"),
        _row("active", started_at="2026-05-14T10:00:00Z", sid="c"),
    ]
    assert aggregate_project_status(rows)["status_since"] == "2026-05-14T12:30:00Z"


def test_working_with_missing_started_at_yields_none_since():
    """started_at is best-effort; if every active lacks one, since is null."""
    rows = [_row("active"), _row("active", sid="b")]
    out = aggregate_project_status(rows)
    assert out["status"] == "working"
    assert out["status_since"] is None


def test_paused_only_yields_needs_input():
    rows = [
        _row("suspended", suspended_at="2026-05-14T10:00:00Z"),
        _row("paused",    paused_at="2026-05-14T11:00:00Z"),
    ]
    out = aggregate_project_status(rows)
    assert out["status"] == "needs-input"
    assert out["status_since"] == "2026-05-14T11:00:00Z"


def test_paused_since_is_max_paused_at():
    rows = [
        _row("paused", paused_at="2026-05-14T11:00:00Z", sid="a"),
        _row("paused", paused_at="2026-05-14T13:00:00Z", sid="b"),
    ]
    assert aggregate_project_status(rows)["status_since"] == "2026-05-14T13:00:00Z"


def test_suspended_only_yields_idle_with_since():
    rows = [
        _row("suspended", suspended_at="2026-05-14T07:00:00Z"),
        _row("suspended", suspended_at="2026-05-14T09:30:00Z"),
    ]
    out = aggregate_project_status(rows)
    assert out == {"status": "idle", "status_since": "2026-05-14T09:30:00Z"}


def test_unknown_status_strings_are_ignored():
    """Defensive: future taxonomies shouldn't crash this aggregator."""
    rows = [_row("zombie"), _row("future-state")]
    out = aggregate_project_status(rows)
    assert out == {"status": "idle", "status_since": None}


def test_paused_takes_priority_over_suspended():
    """Order: active > paused > suspended. Paused trumps suspended."""
    rows = [
        _row("suspended", suspended_at="2026-05-14T20:00:00Z"),
        _row("paused",    paused_at="2026-05-14T08:00:00Z"),
    ]
    assert aggregate_project_status(rows)["status"] == "needs-input"


# ---------------------------------------------------------------------------
# T-0046: active-at-prompt rolls active sessions into needs-input.
# ---------------------------------------------------------------------------

def test_active_at_prompt_alone_yields_needs_input():
    """An active session that's been quiet past the worker's threshold —
    worker flagged active_at_prompt=True — flips the project to needs-input."""
    rows = [_row("active", started_at="2026-05-14T09:00:00Z", active_at_prompt=True)]
    out = aggregate_project_status(rows)
    assert out["status"] == "needs-input"
    assert out["status_since"] == "2026-05-14T09:00:00Z"


def test_active_not_at_prompt_is_working():
    """Plain active (worker didn't flag at-prompt) remains working."""
    rows = [_row("active", started_at="2026-05-14T09:00:00Z", active_at_prompt=False)]
    out = aggregate_project_status(rows)
    assert out["status"] == "working"
    assert out["status_since"] == "2026-05-14T09:00:00Z"


def test_mixed_active_at_prompt_and_crunching_is_working():
    """If ANY active session is crunching, project is working — the
    quiet one doesn't downgrade the pill."""
    rows = [
        _row("active", started_at="2026-05-14T09:00:00Z", active_at_prompt=True,  sid="a"),
        _row("active", started_at="2026-05-14T10:00:00Z", active_at_prompt=False, sid="b"),
    ]
    out = aggregate_project_status(rows)
    assert out["status"] == "working"
    # status_since reflects the crunching row, not the at-prompt one.
    assert out["status_since"] == "2026-05-14T10:00:00Z"


def test_active_at_prompt_plus_paused_uses_max_timestamp():
    """needs-input status_since folds paused_at + at-prompt started_at."""
    rows = [
        _row("active", started_at="2026-05-14T15:00:00Z", active_at_prompt=True, sid="a"),
        _row("paused", paused_at="2026-05-14T11:00:00Z", sid="b"),
    ]
    out = aggregate_project_status(rows)
    assert out["status"] == "needs-input"
    # max across paused_at and at-prompt started_at.
    assert out["status_since"] == "2026-05-14T15:00:00Z"


def test_missing_active_at_prompt_key_treated_as_false():
    """Legacy row shape without active_at_prompt key still works as 'crunching'."""
    rows = [_row("active", started_at="2026-05-14T09:00:00Z")]  # no active_at_prompt
    out = aggregate_project_status(rows)
    assert out["status"] == "working"
