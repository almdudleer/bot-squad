"""T-0828: what ``bsq pace show`` actually PRINTS for the drive block.

Why this file exists separately from the mirror test
----------------------------------------------------
``api/tests/test_pace_drive_mirror.py`` pins that the three implementations
NORMALISE a ``pace.json`` identically. It cannot see a RENDERING divergence, and
it was green against the defect the T-0828 walkthrough actually found: both
surfaces fused the provenance stamp onto the EFFECTIVE value, so a rejected
``scope: "opne"`` printed as

    scope: all — set 2026-07-30 15:14 from «закончить всё что в опен»

Every field there is individually correct and the sentence is false — his words
asked for ``open_reopened``, the stored value was rejected, and ``all`` is OUR
fallback. The line attributes a value he never chose to words he did say, which
in the surface built to make him trust what he reads is worse than a blank.

**PROVENANCE BELONGS TO THE REQUEST, NOT TO THE RESULT.** This file is that
rule's pin on the CLI side; ``web/src/components/ObservabilityPanel.driveMode.test.ts``
is its twin on the web side. Both must change together.

It lives in ``worker/tests/`` and loads ``scripts/cli/bsq`` by source path — the
same shape as ``test_bsq_role_derivation_mirror.py``, which lint.yml already
runs from a full checkout for exactly this "a copy is only allowed when a test
pins it" reason (T-0778).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_BSQ = REPO_ROOT / "scripts" / "cli" / "bsq"

pytestmark = pytest.mark.skipif(
    not CLI_BSQ.is_file(),
    reason=f"scripts/cli/bsq not present under {REPO_ROOT} — needs a full checkout",
)


@pytest.fixture(scope="module")
def bsq():
    """The CLI loaded as a module. Import-safe: everything executable sits
    behind ``if __name__ == "__main__"`` and its module-level state is two path
    constants read from the environment. Nothing here runs a bsq VERB — every
    verb hits the live worker socket and there is no dry-run surface."""
    spec = importlib.util.spec_from_loader(
        "bsq_cli_render_under_test",
        importlib.machinery.SourceFileLoader("bsq_cli_render_under_test", str(CLI_BSQ)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HIS_WORDS = "закончить всё что в опен"


def _render(bsq, raw: dict, capsys) -> str:
    bsq._pace_print_drive(bsq._pace_normalized_drive(raw))
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# The clean cases
# ---------------------------------------------------------------------------

def test_never_set_says_so_and_does_not_present_defaults_as_chosen(bsq, capsys):
    """A project with no drive block. The defaults ARE shown — he should know
    what is in force — but the line leads with "not set" so they cannot be read
    as a decision he made. That distinction is the whole reason `configured`
    exists."""
    out = _render(bsq, {}, capsys)
    assert "not set" in out
    assert "scope=all" in out
    assert "stop_when=scope_exhausted" in out
    assert "on_stop=nothing" in out
    # No provenance can exist for a mode nobody set.
    assert "set 20" not in out
    assert "«" not in out


def test_the_set_case_prints_the_line_d0069_specifies(bsq, capsys):
    """DoD-4's literal deliverable:

        scope: open_reopened — set 2026-07-30 14:52 from «закончить всё что в опен»

    That line is what answers "did my instruction land", so it is pinned as a
    shape, not merely as "the fields appear somewhere"."""
    out = _render(bsq, {"drive": {
        "scope": "open_reopened",
        "set_by": "S-almdudleer-operator-p533",
        "set_at": "2026-07-30T14:52:31Z",
        "source_text": HIS_WORDS,
    }}, capsys)

    scope_line = next(ln for ln in out.splitlines() if ln.strip().startswith("scope:"))
    assert "open_reopened" in scope_line
    assert "— set 2026-07-30 14:52" in scope_line
    assert "by S-almdudleer-operator-p533" in scope_line
    assert f"«{HIS_WORDS}»" in scope_line
    # The other two axes are shown too — he asked which mode is set, not which
    # scope is set.
    assert "stop_when: scope_exhausted" in out
    assert "on_stop:   nothing" in out


def test_explicitly_set_to_the_widest_is_not_the_never_set_line(bsq, capsys):
    """Both read scope=all; only one of them is a decision."""
    never = _render(bsq, {}, capsys)
    chosen = _render(bsq, {"drive": {
        "scope": "all", "set_by": "S-op", "set_at": "2026-07-30T10:00:00Z"}}, capsys)
    assert "not set" in never
    assert "not set" not in chosen
    assert never != chosen


# ---------------------------------------------------------------------------
# THE WALKTHROUGH DEFECT — provenance belongs to the request
# ---------------------------------------------------------------------------

REJECTED = {"drive": {
    "scope": "opne",
    "set_by": "S-almdudleer-operator-p533",
    "set_at": "2026-07-30T15:14:34Z",
    "source_text": HIS_WORDS,
}}


def test_a_rejected_value_never_appears_as_an_attributed_choice(bsq, capsys):
    """THE REGRESSION PIN. The exact fused sentence that was wrong must not
    reappear: a scope line carrying the effective fallback AND his words with a
    "set" verb, as though those words produced it."""
    out = _render(bsq, REJECTED, capsys)
    scope_line = next(ln for ln in out.splitlines() if ln.strip().startswith("scope:"))

    # The bug, stated precisely: the effective value fused to the provenance.
    assert not (scope_line.strip().startswith("scope:     all")
                and "«" in scope_line), (
        f"provenance fused to the fallback value: {scope_line!r}")
    # The verb must attach to the REQUEST, never assert the fallback was set.
    assert "— set 2026" not in out
    assert "requested 2026-07-30 15:14" in out


def test_it_carries_both_facts_the_raw_request_and_what_is_in_effect(bsq, capsys):
    out = _render(bsq, REJECTED, capsys)
    scope_line = next(ln for ln in out.splitlines() if ln.strip().startswith("scope:"))
    assert "'opne'" in scope_line          # his typo, verbatim — he must see it
    assert "requested" in scope_line
    assert "not recognised" in scope_line
    assert "'all' in effect" in scope_line
    # His words survive; only their ATTACHMENT was wrong.
    assert f"«{HIS_WORDS}»" in out


def test_the_invalid_warning_names_field_value_default_and_the_fix(bsq, capsys):
    """A bare "invalid" reads as a bug in us. The actionable form names what to
    type next."""
    out = _render(bsq, REJECTED, capsys)
    warn = next(ln for ln in out.splitlines() if "INVALID" in ln)
    assert "scope='opne'" in warn
    assert "'all' is in effect" in warn
    assert "open_reopened, in_progress, all" in warn
    assert "bsq pace drive --scope" in warn


def test_a_valid_axis_stays_clean_while_another_is_rejected(bsq, capsys):
    out = _render(bsq, {"drive": {"scope": "in_progress", "on_stop": "ALERT!",
                                  "set_at": "2026-07-30T15:14:34Z"}}, capsys)
    scope_line = next(ln for ln in out.splitlines() if ln.strip().startswith("scope:"))
    on_stop_line = next(ln for ln in out.splitlines() if ln.strip().startswith("on_stop:"))
    assert scope_line.strip() == "scope:     in_progress"
    assert "'ALERT!' requested (not recognised)" in on_stop_line
    assert "'nothing' in effect" in on_stop_line


# ---------------------------------------------------------------------------
# REQUESTED vs IN EFFECT, one axis over — the refused stopping condition
# ---------------------------------------------------------------------------
# Same defect class as the provenance fix: a setting shown as active when it is
# in fact refused. «Потратить квоту» reads one signal, and D-0069 Q2 measured
# that signal as UNKNOWN on every project today (no _quota.json anchor, T-0695),
# so `spend_quota` refuses to be the active condition rather than reading as
# "not spent yet, keep going" — the unbounded-drive failure.

def _render_with_spend(bsq, monkeypatch, raw, spend_pct, capsys):
    monkeypatch.setattr(bsq, "_pace_spend_pct", lambda slug: spend_pct)
    bsq._pace_print_drive(bsq._pace_normalized_drive(raw), "test-project")
    return capsys.readouterr().out


def test_spend_quota_with_no_anchor_prints_refused_with_its_reason(
        bsq, monkeypatch, capsys):
    """A bare "refused" reads as a bug in us. The reason is actionable — he can
    create the anchor — so it is part of the line, not a footnote."""
    out = _render_with_spend(
        bsq, monkeypatch, {"drive": {"stop_when": "spend_quota"}}, None, capsys)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("stop_when:"))
    assert "'spend_quota' requested" in line
    assert "REFUSED" in line
    assert "no quota anchor" in line
    assert "'scope_exhausted' is in effect" in line


def test_spend_quota_with_a_readable_spend_is_NOT_marked_refused(
        bsq, monkeypatch, capsys):
    """The negative control. Without it this pin would pass on a renderer that
    marked every stopping condition refused."""
    out = _render_with_spend(
        bsq, monkeypatch, {"drive": {"stop_when": "spend_quota"}}, 42.0, capsys)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("stop_when:"))
    assert line.strip() == "stop_when: spend_quota"
    assert "REFUSED" not in out


def test_the_default_stopping_condition_says_nothing_about_refusal(
        bsq, monkeypatch, capsys):
    """`scope_exhausted` is not gated on any signal, so the refusal line must be
    silent until he actually sets the quota mode — otherwise every project shows
    a warning about a setting nobody chose."""
    out = _render_with_spend(
        bsq, monkeypatch, {"drive": {"scope": "all"}}, None, capsys)
    assert "REFUSED" not in out
    assert "stop_when: scope_exhausted" in out


def test_an_out_of_set_stop_when_reports_the_typo_not_a_refusal(
        bsq, monkeypatch, capsys):
    """Two different failures must not be conflated: an unrecognised value is
    his typo, a refusal is our inability to act on a valid choice."""
    out = _render_with_spend(
        bsq, monkeypatch, {"drive": {"stop_when": "forever"}}, None, capsys)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("stop_when:"))
    assert "'forever' requested (not recognised)" in line
    assert "REFUSED" not in line


def test_an_unparseable_stamp_is_passed_through_not_dropped(bsq, capsys):
    """An unreadable stamp is still evidence of WHEN. Dropping it would be the
    same silent omission this block exists to close."""
    out = _render(bsq, {"drive": {"scope": "all", "set_at": "not-a-date"}}, capsys)
    assert "not-a-date" in out
