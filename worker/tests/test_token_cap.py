"""T-0306: enforce max_total_tokens at spawn (semantics B — budget per quota period).

The caps API stored ``max_total_tokens`` and the UI claimed 'enforced at spawn',
but nothing in the spawn path consumed it (false protection). Enforce it like
``max_parallel_sessions``: a budget on OUTPUT tokens since the ``[quota]`` anchor
(summed across projects via each ``_quota.json`` ``output_tokens_cum_total``),
which FREES when the operator re-anchors (the anchor ``set_at`` changes → the
baseline rebases). 0 = unlimited.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from bot_squad_worker.sessions import (
    _enforce_token_cap, _output_since_anchor,
)


def _cfg(tmp_path, *, cap_tokens=0, set_at="A"):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "system_settings.toml").write_text(
        f"[caps]\nmax_parallel_sessions = 0\nmax_total_tokens = {cap_tokens}\n"
        f'[quota]\nbudget_tokens = 999999\nset_at = "{set_at}"\n'
    )
    data = tmp_path / "data"
    data.mkdir()
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=data,
                                 config_dir=tmp_path / "config")


def _set_output(cfg, total):
    q = cfg.data_dir / "p1" / "_worker" / "telemetry"
    q.mkdir(parents=True, exist_ok=True)
    (q / "_quota.json").write_text(json.dumps({"output_tokens_cum_total": total}))


# --- enforcement -----------------------------------------------------------

def test_token_cap_unlimited_when_zero(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cap_tokens=0)
    monkeypatch.setattr(S, "_output_since_anchor", lambda c: 10_000_000)
    _enforce_token_cap(cfg)  # cap 0 = unlimited → no raise


def test_token_cap_refuses_over_budget(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cap_tokens=1000)
    monkeypatch.setattr(S, "_output_since_anchor", lambda c: 1500)
    with pytest.raises(ActionError, match="token budget"):
        _enforce_token_cap(cfg)


def test_token_cap_allows_under_budget(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cap_tokens=1000)
    monkeypatch.setattr(S, "_output_since_anchor", lambda c: 500)
    _enforce_token_cap(cfg)  # 500 < 1000 → no raise


# --- anchor-rebased budget -------------------------------------------------

def test_output_since_anchor_counts_from_baseline(tmp_path):
    cfg = _cfg(tmp_path, set_at="A")
    _set_output(cfg, 1000)
    # first read captures baseline 1000 → since = 0
    assert _output_since_anchor(cfg) == 0
    _set_output(cfg, 1700)
    assert _output_since_anchor(cfg) == 700  # same anchor → grows


def test_output_since_anchor_rebases_on_reanchor(tmp_path):
    cfg = _cfg(tmp_path, set_at="A")
    _set_output(cfg, 1000)
    assert _output_since_anchor(cfg) == 0
    _set_output(cfg, 5000)
    assert _output_since_anchor(cfg) == 4000
    # operator re-anchors (set_at changes) → budget frees, baseline rebases to 5000
    (cfg.config_dir / "system_settings.toml").write_text(
        '[caps]\nmax_total_tokens = 0\n[quota]\nbudget_tokens = 9\nset_at = "B"\n')
    assert _output_since_anchor(cfg) == 0
    _set_output(cfg, 5500)
    assert _output_since_anchor(cfg) == 500
