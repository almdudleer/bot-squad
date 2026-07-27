"""T-0577: dedupe-vs-create gate on the worker ``task_new`` action.

``task_search.py`` (rank/score of similar backlog tasks, T-0486) previously had
ZERO ingest callers (conformance audit D-0047) — every TG/voice-driven task
filing minted unconditionally, so a firehose-driven backlog proliferated
duplicate tickets. This closes that gap: before minting, ``task_new`` ranks
the new ask (title [+ verbatim text]) against the project's existing backlog
and rejects — with an ``ActionError`` carrying the similar task ids/titles and
the exact retry recipe — when the top match clears a CONSERVATIVE coverage
threshold (``cfg.tasks_dedupe_threshold``, default 0.9). ``force: true``
bypasses. Tuned for precision over recall: a false reject on someone's first
live voice-filed task is worse than an occasional slipped-through duplicate,
so only near-total-token, near-duplicate titles trip the gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bot_squad_worker.actions as A
from bot_squad_worker.config import Config


def _cfg(monkeypatch, tmp_config_dir: Path, tasks_toml: str | None = None) -> Config:
    if tasks_toml is not None:
        (tmp_config_dir / "system_settings.toml").write_text(tasks_toml)
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    return cfg


def _seed(backlog_dir: Path, tid: str, title: str, body: str, status: str = "planned") -> None:
    backlog_dir.mkdir(parents=True, exist_ok=True)
    (backlog_dir / f"{tid}-seed.md").write_text(
        "---\n"
        f"id: {tid}\n"
        f'title: "{title}"\n'
        f"status: {status}\n"
        "---\n\n"
        f"## Verbatim request\n\n{body}\n"
    )


def _backlog_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "test-project" / "backlog"


# --- near-duplicate reject / force bypass / sub-threshold mint --------------


def test_near_duplicate_title_rejected(tmp_path, tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    _seed(
        _backlog_dir(tmp_path), "T-0500", "Fix login page crash on Safari",
        "Users report the login page crashes on Safari after entering credentials.",
    )
    with pytest.raises(A.ActionError) as exc:
        A.dispatch("task_new", {
            "slug": "test-project", "title": "Login page crashes on Safari",
            "provenance": "T-0577",
        })
    msg = str(exc.value)
    assert "T-0500" in msg
    assert "Fix login page crash on Safari" in msg
    assert "force:true" in msg
    assert "bsq task new ... --force" in msg
    # nothing minted — no new file, no counter burned
    assert sorted(_backlog_dir(tmp_path).glob("T-*.md"))[0].name == "T-0500-seed.md"
    assert len(list(_backlog_dir(tmp_path).glob("T-*.md"))) == 1


def test_force_true_bypasses_the_gate(tmp_path, tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    _seed(
        _backlog_dir(tmp_path), "T-0500", "Fix login page crash on Safari",
        "Users report the login page crashes on Safari after entering credentials.",
    )
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "Login page crashes on Safari",
        "provenance": "T-0577", "force": True,
    })
    assert out["ok"] is True
    assert Path(out["file_path"]).exists()


def test_force_must_be_boolean(tmp_path, tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    _backlog_dir(tmp_path).mkdir(parents=True)
    with pytest.raises(A.ActionError, match="force must be a boolean"):
        A.dispatch("task_new", {
            "slug": "test-project", "title": "x", "provenance": "T-0577",
            "force": "true",
        })


def test_sub_threshold_similarity_mints(tmp_path, tmp_config_dir, monkeypatch):
    """0.80 coverage < the default 0.9 threshold -> mints (not flagged)."""
    _cfg(monkeypatch, tmp_config_dir)
    _seed(
        _backlog_dir(tmp_path), "T-0501", "Add dark mode toggle to settings page",
        "Stakeholder wants a dark mode toggle in the settings page.",
    )
    out = A.dispatch("task_new", {
        "slug": "test-project",
        "title": "Add a dark mode toggle to the settings screen",
        "provenance": "T-0577",
    })
    assert out["ok"] is True


def test_unrelated_query_mints(tmp_path, tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    _seed(
        _backlog_dir(tmp_path), "T-0500", "Fix login page crash on Safari",
        "Users report the login page crashes on Safari after entering credentials.",
    )
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "Improve onboarding email copy",
        "provenance": "T-0577",
    })
    assert out["ok"] is True


def test_empty_backlog_mints(tmp_path, tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    # No backlog dir at all yet.
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "Anything at all, first ticket ever",
        "provenance": "T-0577",
    })
    assert out["ok"] is True


def test_threshold_config_override_honored(tmp_path, tmp_config_dir, monkeypatch):
    """The same 0.80-coverage ask that mints under the default 0.9 threshold
    (test_sub_threshold_similarity_mints) is REJECTED once the admin lowers
    [tasks].dedupe_threshold below 0.80."""
    _cfg(monkeypatch, tmp_config_dir, tasks_toml="[tasks]\ndedupe_threshold = 0.75\n")
    _seed(
        _backlog_dir(tmp_path), "T-0501", "Add dark mode toggle to settings page",
        "Stakeholder wants a dark mode toggle in the settings page.",
    )
    with pytest.raises(A.ActionError, match="near-duplicate"):
        A.dispatch("task_new", {
            "slug": "test-project",
            "title": "Add a dark mode toggle to the settings screen",
            "provenance": "T-0577",
        })


def test_verbatim_widens_the_dedupe_query(tmp_path, tmp_config_dir, monkeypatch):
    """Title alone has only 1 meaningful token (below the 2-token floor) so the
    gate never trips on title alone — but once ``verbatim`` supplies the
    ticket's own distinctive words, the widened query hits full coverage and
    is rejected."""
    _cfg(monkeypatch, tmp_config_dir)
    _seed(
        _backlog_dir(tmp_path), "T-0500", "Fix login page crash on Safari",
        "Users report the login page crashes on Safari after entering credentials.",
    )
    # Title alone: 1 token ("crashes") -> under the floor -> mints.
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "Crashes", "provenance": "T-0577",
    })
    assert out["ok"] is True

    # Same short title, but verbatim text reconstructs the near-duplicate ask.
    with pytest.raises(A.ActionError, match="near-duplicate"):
        A.dispatch("task_new", {
            "slug": "test-project", "title": "Crashes",
            "verbatim": "Login page crashes on Safari",
            "provenance": "T-0577",
        })


def test_verbatim_is_not_stored_on_the_ticket(tmp_path, tmp_config_dir, monkeypatch):
    """verbatim is dedupe SIGNAL only — task_new's body stays the stub
    placeholder, unchanged (the attendant still edits the file afterward per
    the documented user-conversation protocol)."""
    _cfg(monkeypatch, tmp_config_dir)
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "Totally new distinct ask",
        "verbatim": "some verbatim words that should not leak into the body",
        "provenance": "T-0577",
    })
    body = Path(out["file_path"]).read_text()
    assert "(filed via task_new)" in body
    assert "some verbatim words" not in body


def test_closed_ticket_is_not_flagged_as_duplicate(tmp_path, tmp_config_dir, monkeypatch):
    """A closed ticket is done, not a live duplicate risk — re-filing a
    regression (or an unrelated ask sharing vocabulary with an old closed
    ticket) must not need --force (T-0581 finding 1)."""
    _cfg(monkeypatch, tmp_config_dir)
    _seed(
        _backlog_dir(tmp_path), "T-0500", "Fix login page crash on Safari",
        "Users report the login page crashes on Safari after entering credentials.",
        status="closed",
    )
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "Login page crashes on Safari",
        "provenance": "T-0577",
    })
    assert out["ok"] is True


def test_dedupe_gate_exception_fails_open(tmp_path, tmp_config_dir, monkeypatch):
    """An unexpected exception inside the dedupe gate (a bug in
    task_search.rank, a PermissionError walking the backlog dir, ...) must
    never block a legitimate mint — fail OPEN, not closed (T-0581 finding 2).
    Only the deliberate ActionError reject (an actual near-dup) should ever
    stop a mint."""
    _cfg(monkeypatch, tmp_config_dir)
    _seed(
        _backlog_dir(tmp_path), "T-0500", "Fix login page crash on Safari",
        "Users report the login page crashes on Safari after entering credentials.",
    )

    def _boom(cfg, slug, query):
        raise RuntimeError("boom")

    monkeypatch.setattr(A, "_task_new_similar_backlog", _boom)
    out = A.dispatch("task_new", {
        "slug": "test-project", "title": "Login page crashes on Safari",
        "provenance": "T-0577",
    })
    assert out["ok"] is True


def test_recall_gap_body_only_dup_not_crowded_out_by_low_coverage_high_score(
    tmp_path, tmp_config_dir, monkeypatch,
):
    """``rank()`` must be evaluated UNBOUNDED and coverage-filtered before any
    cap is applied — six decoys that each partially match the TITLE (score 6,
    coverage 0.4) must not push a true body-only near-dupe (score 5, coverage
    1.0) out of the candidate pool before it is even coverage-checked
    (T-0581 finding 3: capping the ranked list first, at the old
    limit=5, drops it — recall gap)."""
    _cfg(monkeypatch, tmp_config_dir)
    backlog = _backlog_dir(tmp_path)
    decoys = [
        ("T-0510", "Alpha beta ticket one"),
        ("T-0511", "Alpha gamma ticket two"),
        ("T-0512", "Alpha delta ticket three"),
        ("T-0513", "Alpha epsilon ticket four"),
        ("T-0514", "Beta gamma ticket five"),
        ("T-0515", "Beta delta ticket six"),
    ]
    for tid, title in decoys:
        _seed(backlog, tid, title, "General notes about the ticket.")
    _seed(
        backlog, "T-0520", "Unrelated login page problem",
        "This report mentions alpha, beta, gamma, delta and epsilon in detail.",
    )
    with pytest.raises(A.ActionError, match="near-duplicate") as exc:
        A.dispatch("task_new", {
            "slug": "test-project",
            "title": "Alpha beta gamma delta epsilon",
            "provenance": "T-0577",
        })
    assert "T-0520" in str(exc.value)


def test_dedupe_listing_still_capped_after_filter(tmp_path, tmp_config_dir, monkeypatch):
    """Ranking+filtering is unbounded now, but the final reject listing stays
    capped at ``_TASK_DEDUPE_CANDIDATE_LIMIT`` for readability (T-0581
    finding 3: filter-then-cap, not cap-then-filter)."""
    _cfg(monkeypatch, tmp_config_dir)
    backlog = _backlog_dir(tmp_path)
    for i in range(7):
        _seed(
            backlog, f"T-060{i}", f"Alpha beta gamma delta epsilon variant {i}",
            "Alpha beta gamma delta epsilon all present in body too.",
        )
    with pytest.raises(A.ActionError) as exc:
        A.dispatch("task_new", {
            "slug": "test-project",
            "title": "Alpha beta gamma delta epsilon",
            "provenance": "T-0577",
        })
    listing = str(exc.value)
    assert sum(listing.count(f"T-060{i}") for i in range(7)) == A._TASK_DEDUPE_CANDIDATE_LIMIT


# --- worker/scripts-cli mirror -----------------------------------------------
# ``worker/bot_squad_worker/task_search.py`` is a byte-identical copy of the
# canonical ``scripts/cli/task_search.py``. Checked by the single registry in
# `worker/tests/test_module_mirrors.py` since T-0743, which also runs on every
# push via `scripts/lint/module_mirrors.py`. It used to be a fifth copy of the
# same comparison, right here.
