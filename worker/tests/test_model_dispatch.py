"""T-0909: the model-dispatch ledger, and the compliance number read off it.

THE INSTRUMENT FAILURE THIS EXISTS TO PREVENT. This ticket's first pass counted
`model_source: explicit` across `data/*/sessions/*.md` and reported that the
per-ticket model choice had NEVER been exercised, on any project, since T-0871
shipped it. The number was zero because session mds are DELETED when a session
is reaped — the grep could only ever see the handful alive at that moment. The
worker journal over the same eight days:

    104  role=dev model=opus   (explicit)
     92  role=dev model=opus   (config:dev)
      2  role=dev model=sonnet (explicit)

Same fleet, opposite conclusion. So the ledger is not a convenience over the
journal — it is the reading that does not disappear, and these tests pin the
properties that make it trustworthy:

  * an EMPTY ledger reports "no measurement", never a percentage computed from
    nothing ([[feedback_explicit_unknown_not_silent_none]] — an absent field
    must not read as a real negative);
  * an unrecognised model counts as `unknown`, not as a guess in either
    direction;
  * a resume lands in the ledger too, because it spends on the model it resumes
    ON — a compliance number blind to resumes describes less spend than it
    claims;
  * a ledger write can NEVER fail a spawn.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bot_squad_worker import model_dispatch as MD
from tests.test_explicit_model_effort import (  # noqa: F401
    _make_cfg, _session_md, _spawn_cmd, _stub_tmux,
)


# ---------------------------------------------------------------------------
# the pricing table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("sonnet", "sonnet"), ("claude-sonnet-5", "sonnet"),
    ("opus", "opus"), ("claude-opus-5", "opus"), ("claude-opus-4-8", "opus"),
    ("opus[1m]", "opus"),
    ("fable", "fable"), ("claude-fable-5", "fable"),
    ("", ""), ("luna", ""), ("gpt-5.6-terra", ""), (None, ""),
])
def test_model_class(value, expected):
    assert MD.model_class(value) == expected


def test_fable_is_premium_on_PRICE_not_availability():
    """`fleet_model.UNAVAILABLE_CLASSES` blocks Fable today for an unrelated
    reason (no purchased credits). If that gate is ever lifted, Fable must NOT
    silently become an ungated default — it is the TOP tier ($10/$50 per Mtok),
    so it belongs on the same side of this table as Opus."""
    from bot_squad_worker import fleet_model

    assert "fable" in MD.PREMIUM_CLASSES
    assert "fable" in fleet_model.UNAVAILABLE_CLASSES  # today's separate gate
    assert MD.is_premium("fable") and MD.is_premium("claude-fable-5")


def test_premium_and_economy_do_not_overlap():
    assert not (MD.PREMIUM_CLASSES & MD.ECONOMY_CLASSES)


# ---------------------------------------------------------------------------
# record / read
# ---------------------------------------------------------------------------

def test_record_then_read_round_trips(tmp_path):
    MD.record(tmp_path, kind="spawn", slug="p", sid="S-1", role="dev",
              model="sonnet", source="explicit", window="w", task_id="T-1")
    recs = MD.read(tmp_path)
    assert len(recs) == 1
    assert recs[0]["model"] == "sonnet"
    assert recs[0]["model_class"] == "sonnet"   # derived at write time
    assert recs[0]["kind"] == "spawn"
    assert recs[0]["ts"]


def test_record_never_raises_on_an_unwritable_ledger(tmp_path):
    """A cost-control feature that can take the fleet down is worse than the
    cost. The ledger path is made unwritable and the call must still return."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o500)
    try:
        MD.record(blocked, kind="spawn", slug="p", role="dev", model="opus")
    finally:
        os.chmod(blocked, 0o700)


def test_read_skips_a_malformed_line_instead_of_dying(tmp_path):
    MD.record(tmp_path, kind="spawn", role="dev", model="sonnet")
    path = MD.ledger_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    MD.record(tmp_path, kind="spawn", role="dev", model="opus")
    assert [r["model"] for r in MD.read(tmp_path)] == ["sonnet", "opus"]


def test_a_long_reason_is_truncated_not_rejected(tmp_path):
    MD.record(tmp_path, kind="spawn", role="dev", model="opus", reason="x" * 900)
    assert len(MD.read(tmp_path)[0]["reason"]) == 500


# ---------------------------------------------------------------------------
# compliance
# ---------------------------------------------------------------------------

def _fill(data_dir, models, **kw):
    kind = kw.pop("kind", "spawn")
    for m in models:
        MD.record(data_dir, kind=kind, slug=kw.get("slug", "p"),
                  role=kw.get("role", "dev"), model=m,
                  source=kw.get("source", "explicit"),
                  reason=kw.get("reason", "because"))


def test_empty_ledger_reports_no_measurement_not_a_percentage(tmp_path):
    """The property that separates 'nothing was dispatched' from '0% complied'.
    A number-shaped answer here would be the same defect class as the
    session-md grep that started this ticket."""
    comp = MD.compliance(tmp_path)
    assert comp["n"] == 0
    assert comp["economy_pct"] is None
    assert "no dispatches recorded" in MD.summary_line(comp)


def test_compliance_counts_and_percentage(tmp_path):
    _fill(tmp_path, ["sonnet", "sonnet", "opus", "opus"])
    comp = MD.compliance(tmp_path)
    assert (comp["n"], comp["economy"], comp["premium"]) == (4, 2, 2)
    assert comp["economy_pct"] == 50.0


def test_an_unrecognised_model_is_unknown_not_guessed(tmp_path):
    _fill(tmp_path, ["sonnet", "some-new-model-9"])
    comp = MD.compliance(tmp_path)
    assert (comp["economy"], comp["premium"], comp["unknown"]) == (1, 0, 1)


def test_compliance_windows_to_the_last_n(tmp_path):
    _fill(tmp_path, ["opus"] * 10)
    _fill(tmp_path, ["sonnet"] * 3)
    comp = MD.compliance(tmp_path, limit=3)
    assert comp["n"] == 3 and comp["economy"] == 3


def test_premium_reasons_are_surfaced_with_their_dispatch(tmp_path):
    MD.record(tmp_path, kind="spawn", role="dev", model="opus",
              task_id="T-0909", reason="cause unknown, spawn lifecycle")
    MD.record(tmp_path, kind="spawn", role="dev", model="sonnet", task_id="T-0910")
    reasons = MD.compliance(tmp_path)["premium_reasons"]
    assert len(reasons) == 1
    assert reasons[0]["task_id"] == "T-0909"
    assert "cause unknown" in reasons[0]["reason"]


def test_a_config_default_is_counted_but_attributable(tmp_path):
    """Automated/defaulted dispatches spend the same money, so they are IN the
    number — but `by_source` keeps 'an agent chose this' separable from 'a
    config default chose this', which is the distinction the operator acts on."""
    MD.record(tmp_path, kind="spawn", role="dev", model="opus", source="config:dev")
    MD.record(tmp_path, kind="spawn", role="dev", model="opus", source="explicit")
    comp = MD.compliance(tmp_path)
    assert comp["premium"] == 2
    assert comp["by_source"] == {"config:dev": 1, "explicit": 1}


def test_resumes_are_recorded_but_not_mixed_into_the_spawn_number(tmp_path):
    _fill(tmp_path, ["opus", "opus"], kind="resume")
    _fill(tmp_path, ["sonnet"])
    assert MD.compliance(tmp_path)["n"] == 1                       # spawns only
    assert MD.compliance(tmp_path, kinds=("resume",))["n"] == 2
    assert MD.compliance(tmp_path, kinds=("spawn", "resume"))["n"] == 3


def test_role_and_slug_filters(tmp_path):
    MD.record(tmp_path, kind="spawn", role="dev", slug="a", model="sonnet")
    MD.record(tmp_path, kind="spawn", role="operator", slug="a", model="opus")
    MD.record(tmp_path, kind="spawn", role="dev", slug="b", model="opus")
    assert MD.compliance(tmp_path)["n"] == 2
    assert MD.compliance(tmp_path, role="")["n"] == 3
    assert MD.compliance(tmp_path, slug="b")["n"] == 1


def test_prune_keeps_the_tail(tmp_path):
    _fill(tmp_path, ["sonnet"] * 5)
    path = MD.ledger_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines * (2 * MD.MAX_RECORDS // 5 + 2)), encoding="utf-8")
    MD.record(tmp_path, kind="spawn", role="dev", model="opus", task_id="LAST")
    recs = MD.read(tmp_path)
    assert len(recs) <= MD.MAX_RECORDS
    assert recs[-1]["task_id"] == "LAST"


# ---------------------------------------------------------------------------
# the wiring — spawn() and resume() actually write to it
# ---------------------------------------------------------------------------

def test_spawn_writes_a_ledger_record(tmp_path, monkeypatch):
    """A ledger nothing writes to is a ledger that reports perfect compliance
    forever ([[feedback_prove_the_path_is_reached]])."""
    _spawn_cmd(tmp_path, monkeypatch, "w", model="sonnet",
               model_reason="one-file copy change", dispatched_by="bsq spawn")
    recs = MD.read(tmp_path / "data")
    assert len(recs) == 1
    r = recs[0]
    assert r["kind"] == "spawn" and r["role"] == "dev"
    assert r["model"] == "sonnet" and r["source"] == "explicit"
    assert r["reason"] == "one-file copy change"
    assert r["dispatched_by"] == "bsq spawn"
    assert r["effort"] and r["effort_source"]


def test_spawn_records_the_ROLE_DEFAULT_dispatch_too(tmp_path, monkeypatch):
    """The 92 dispatches nobody typed anything for. If the ledger only saw
    explicit choices it would score the corpus the operator already complies
    on, which is precisely the blindness this ticket is correcting."""
    _spawn_cmd(tmp_path, monkeypatch, "w", settings='[models]\ndev = "opus"\n')
    r = MD.read(tmp_path / "data")[0]
    assert r["model"] == "opus" and r["source"] == "config:dev"
    assert r["reason"] == ""
    assert MD.compliance(tmp_path / "data")["premium"] == 1


def test_spawn_stamps_the_reason_on_the_session_md(tmp_path, monkeypatch):
    """So a live session can be asked "why are you on Opus" without a ledger
    lookup — and so the answer survives in the same place `model_source` does."""
    _spawn_cmd(tmp_path, monkeypatch, "w", model="opus",
               model_reason="cross-module design, cause unknown")
    md = _session_md(tmp_path)
    assert md.get("model_reason") == "cross-module design, cause unknown"
    assert md.get("model_source") == "explicit"


def test_spawn_without_a_reason_leaves_no_empty_field(tmp_path, monkeypatch):
    _spawn_cmd(tmp_path, monkeypatch, "w", model="sonnet")
    assert "model_reason" not in _session_md(tmp_path)


def test_a_broken_ledger_does_not_break_the_spawn(tmp_path, monkeypatch):
    """End-to-end version of test_record_never_raises: the spawn still returns
    its launch command with the ledger raising underneath."""
    import bot_squad_worker.model_dispatch as _MD

    def boom(*a, **k):
        raise OSError("ledger is on fire")

    monkeypatch.setattr(_MD, "record", boom)
    cmd = _spawn_cmd(tmp_path, monkeypatch, "w", model="sonnet")
    assert "--model sonnet" in cmd


# ---------------------------------------------------------------------------
# negative controls — prove the two load-bearing readings CAN fail
#
# Each mutates ONE property on a scratch copy of `model_dispatch.py` and
# asserts the assertion above it stops holding. A green test whose subject
# cannot go red is a green from a broken instrument
# ([[feedback_ask_what_the_green_read]]).
# ---------------------------------------------------------------------------

def _mutated_module(tmp_path: Path, old: str, new: str, name: str):
    import importlib.util

    src = Path(MD.__file__).read_text(encoding="utf-8")
    assert src.count(old) == 1, f"anchor {old!r} not unique ({src.count(old)})"
    dst = tmp_path / f"md_{name}.py"
    dst.write_text(src.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"md_{name}", dst)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_negative_control_empty_ledger_rendered_as_a_percentage(tmp_path):
    """Mutation: the empty case returns 0.0 instead of None.
    Goes red: test_empty_ledger_reports_no_measurement_not_a_percentage.

    This is the whole defect this ticket started with, in miniature: a
    measurement of nothing that renders as a real number."""
    mut = _mutated_module(
        tmp_path,
        '"economy_pct": (round(100.0 * economy / n, 1) if n else None),',
        '"economy_pct": (round(100.0 * economy / n, 1) if n else 0.0),',
        "pct")
    assert mut.compliance(tmp_path)["economy_pct"] == 0.0     # indistinguishable
    assert MD.compliance(tmp_path)["economy_pct"] is None     # from a real 0%


def test_negative_control_unknown_model_folded_into_economy(tmp_path):
    """Mutation: an unrecognised model reads as sonnet.
    Goes red: test_an_unrecognised_model_is_unknown_not_guessed — and the
    compliance number would silently improve every time a model is renamed."""
    mut = _mutated_module(
        tmp_path,
        '    for cls in ("sonnet", "opus", "fable", "haiku"):\n'
        '        if cls in v:\n'
        '            return cls\n'
        '    return ""',
        '    for cls in ("sonnet", "opus", "fable", "haiku"):\n'
        '        if cls in v:\n'
        '            return cls\n'
        '    return "sonnet"',
        "guess")
    assert mut.model_class("some-new-model-9") == "sonnet"
    assert MD.model_class("some-new-model-9") == ""


def test_negative_control_ledger_blind_to_config_defaults(tmp_path):
    """Mutation: compliance scores only dispatches an agent chose explicitly.
    Goes red: test_spawn_records_the_ROLE_DEFAULT_dispatch_too's compliance
    assertion — and the fleet would report perfect compliance while 92 of 198
    dispatches took the premium default untouched."""
    mut = _mutated_module(
        tmp_path,
        '        and (not role or r.get("role") == role)',
        '        and (not role or r.get("role") == role)\n'
        '        and r.get("source") == "explicit"',
        "blind")
    MD.record(tmp_path, kind="spawn", role="dev", model="opus", source="config:dev")
    assert mut.compliance(tmp_path)["n"] == 0        # the 92 vanish
    assert MD.compliance(tmp_path)["premium"] == 1   # ...and are counted here
