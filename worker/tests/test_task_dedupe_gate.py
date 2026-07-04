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

import filecmp
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


def _seed(backlog_dir: Path, tid: str, title: str, body: str) -> None:
    backlog_dir.mkdir(parents=True, exist_ok=True)
    (backlog_dir / f"{tid}-seed.md").write_text(
        "---\n"
        f"id: {tid}\n"
        f'title: "{title}"\n'
        "status: planned\n"
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


# --- worker/scripts-cli mirror -----------------------------------------------


def test_task_search_worker_mirror_is_byte_identical():
    """``worker/bot_squad_worker/task_search.py`` is a byte-identical copy of
    the canonical ``scripts/cli/task_search.py`` (same convention as
    ``idalloc.py`` / T-0174) — the worker package cannot import across the
    ``scripts/`` boundary cleanly, so we replicate instead."""
    here = Path(__file__).resolve()
    root = here.parents[2]  # worker/tests/ -> repo root
    canonical = root / "scripts" / "cli" / "task_search.py"
    mirror = root / "worker" / "bot_squad_worker" / "task_search.py"
    assert filecmp.cmp(canonical, mirror, shallow=False), (
        f"{canonical} and {mirror} have drifted — re-sync them "
        "(they must be byte-identical)."
    )
