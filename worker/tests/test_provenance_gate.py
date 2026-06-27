"""T-0519: provenance gate on the worker ``task_new`` action.

The CLI (``bsq task new``) already requires + validates provenance client-side,
but a direct caller of the ``task_new`` worker action could mint a sourceless
ticket. This gate closes that leak: the action itself requires + validates +
stamps provenance, using a byte-identical mirror of the canonical grammar in
``scripts/lint/backlog_provenance.py`` (Cluster C / T-0506). New creates are
always post-cutoff so provenance is always required; the 95 pre-cutoff tickets
stay grandfathered by the lint's cutoff (untouched here).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import bot_squad_worker.actions as A
from bot_squad_worker.config import Config


def _cfg(monkeypatch, tmp_config_dir: Path) -> Config:
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    return cfg


def test_task_new_rejects_missing_provenance(tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    with pytest.raises(A.ActionError, match="requires provenance"):
        A._action_task_new({"slug": "test-project", "title": "gateme"})


def test_task_new_rejects_invalid_provenance(tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    with pytest.raises(A.ActionError, match="invalid provenance"):
        A._action_task_new(
            {"slug": "test-project", "title": "gateme", "provenance": "not-a-valid-token"}
        )


def test_task_new_accepts_and_stamps_valid_provenance(tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    res = A._action_task_new(
        {"slug": "test-project", "title": "gateme", "provenance": "T-0519"}
    )
    body = Path(res["file_path"]).read_text(encoding="utf-8")
    assert "provenance: T-0519" in body


def test_task_new_accepts_comma_list_provenance(tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    res = A._action_task_new(
        {"slug": "test-project", "title": "gateme", "provenance": "T-0506, stakeholder:2026-06-27"}
    )
    body = Path(res["file_path"]).read_text(encoding="utf-8")
    assert "provenance: T-0506, stakeholder:2026-06-27" in body


def _load_canonical():
    """Load Cluster C's canonical provenance grammar module by path."""
    here = Path(__file__).resolve()
    # worker/tests/ -> repo root -> scripts/lint/backlog_provenance.py
    root = here.parents[2]
    path = root / "scripts" / "lint" / "backlog_provenance.py"
    spec = importlib.util.spec_from_file_location("_canon_provenance", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_worker_provenance_grammar_matches_canonical():
    """The worker mirror must agree with C's canonical validator on every case
    (idalloc.py byte-identical-mirror precedent: replicate + assert no drift)."""
    from bot_squad_worker import provenance as wp

    canon = _load_canonical()
    cases = [
        "corpus:guidance",
        "F-12",
        "T-0001",
        "T-0519",
        "stakeholder:2026-06-27",
        "T-0506, stakeholder:2026-06-27",
        "",
        "not-a-token",
        "T-001",  # too few digits
        "corpus:",
        "stakeholder:2026-6-1",  # unpadded
        "F-",
    ]
    for c in cases:
        assert wp.provenance_valid(c) == canon._provenance_valid(c), c
