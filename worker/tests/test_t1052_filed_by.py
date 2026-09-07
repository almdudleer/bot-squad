"""T-1052: task_new stamps the filing session's SID as `filed_by`.

`session_history` means "worked it" (stamped only at bind/resume — see
`sessions._append_task_session_history`); filing a ticket via `task_new`
never wrote anything there, so the one session that measured a ticket and
found the trap in it was invisible to `bsq spawn --resume`'s expert scan
(scripts/cli/test_bsq_expert.py covers that side). This file covers the
worker-side stamp: a distinct, create-time-only `filed_by` scalar, best-effort
(a malformed value is dropped, never fails the mint).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bot_squad_worker.actions as A
from bot_squad_worker.config import Config


def _cfg(monkeypatch, tmp_config_dir: Path) -> Config:
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    return cfg


def test_task_new_stamps_filed_by(tmp_config_dir, monkeypatch):
    _cfg(monkeypatch, tmp_config_dir)
    res = A._action_task_new({
        "slug": "test-project", "title": "gateme", "provenance": "T-1052",
        "filed_by": "S-almdudleer-dev_bsq_spawn_dispatch_never_stamps_a-p714",
    })
    body = Path(res["file_path"]).read_text(encoding="utf-8")
    assert "filed_by: S-almdudleer-dev_bsq_spawn_dispatch_never_stamps_a-p714" in body


def test_task_new_without_filed_by_omits_the_field(tmp_config_dir, monkeypatch):
    # An API/web-board create, or any caller that has no session SID to give,
    # must mint exactly as before — filed_by is optional, not required.
    _cfg(monkeypatch, tmp_config_dir)
    res = A._action_task_new(
        {"slug": "test-project", "title": "gateme", "provenance": "T-1052"}
    )
    body = Path(res["file_path"]).read_text(encoding="utf-8")
    assert "filed_by" not in body


def test_task_new_drops_a_malformed_filed_by_without_failing_the_mint(tmp_config_dir, monkeypatch):
    # Best-effort (docstring at the T-1052 gate): the mint is the load-bearing
    # thing here, not the forensic breadcrumb — a bad value is dropped, not
    # a raised ActionError.
    _cfg(monkeypatch, tmp_config_dir)
    res = A._action_task_new({
        "slug": "test-project", "title": "gateme", "provenance": "T-1052",
        "filed_by": "not-a-sid\nwith-a-newline",
    })
    body = Path(res["file_path"]).read_text(encoding="utf-8")
    assert "filed_by" not in body


# DoD #1 (filed_by is create-time-only, like provenance/owner — never
# PATCH-mutable) is covered on the API side, where `_ALLOWED_UPDATE_KEYS`
# actually lives: api/tests/test_markdown_writer.py::
# test_merge_task_update_rejects_filed_by
