"""T-0828 DoD-6: the THREE implementations of the drive-mode record must agree,
and this file goes red the moment one grows a field or a value the others lack.

The twin (and the third copy)
-----------------------------
D-0069 puts the standing drive mode in the EXISTING ``pace.json``. Three
processes read that file and none of them may import the others:

* ``worker/bot_squad_worker/pace.py`` — the SSOT. Runs under systemd from
  ``worker/``.
* ``api/app/routes_transparency.py`` — runs in docker from ``api/``. The api
  cannot ``import bot_squad_worker`` (separate deployable packages), so it
  mirrors the on-disk layout. The established pattern: see ``frontmatter.py``,
  ``idalloc.py``, and the T-0749 differential this file is modelled on.
* ``scripts/cli/bsq`` — stdlib-only by design, runs on a bare ``python3`` with
  no venv and reaches the worker only over its socket. Its role-derivation block
  states the policy explicitly: every piece of worker logic it needs is a
  mirror, never an import.

So the drive normalisation exists three times. **If they drift, the failure is
SILENT and it is the exact defect the stakeholder reported**: the UI renders a
project with no drive mode set while ``bsq pace show`` says one is, or an axis
he set reads as the default on one surface only. Duplicate divergence is this
repo's top bug class (T-0831), and T-0778's rule is that a hand-maintained copy
is only allowed when a test pins it — this is that test.

⚠ WHAT THIS FILE DOES **NOT** COVER, and it must be said here because a green
run is otherwise read as "the surfaces agree"
------------------------------------------------------------------------------
**It compares NORMALISATION, not RENDERING.** All three can normalise a
``pace.json`` identically and still print contradictory sentences to the human.
That is not hypothetical: the T-0828 walkthrough found both surfaces fusing the
provenance onto the EFFECTIVE value, so a rejected ``scope: "opne"`` rendered as
``scope: all — set … from «закончить всё что в опен»`` — attributing a fallback
he never chose to words he did say. This file was green against that bug, by
construction. The rendering rule ("provenance belongs to the request, not to the
result") is pinned separately, per surface:
``web/src/components/ObservabilityPanel.driveModeCopy`` has a vitest, and the
CLI assembler is pinned in ``worker/tests/test_pace_drive_cli_render.py``.

Where it runs
-------------
Skips when the worker tree or the CLI is absent — the dev host's api-only docker
mount. A skip is not coverage, so ``lint.yml`` runs this file from a full
checkout on every push (the T-0749/T-0775/T-0778 pattern; T-0738's lesson is
that a guard only the nightly runs leaves a divergence live for hours).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_TREE = REPO_ROOT / "worker"
WORKER_PACE = WORKER_TREE / "bot_squad_worker" / "pace.py"
CLI_BSQ = REPO_ROOT / "scripts" / "cli" / "bsq"

pytestmark = pytest.mark.skipif(
    not (WORKER_PACE.is_file() and CLI_BSQ.is_file()),
    reason=(
        f"worker source and/or the bsq CLI not present under {REPO_ROOT} — run "
        f"from a full checkout (lint.yml does; the api-only docker mount does not)"
    ),
)

if WORKER_PACE.is_file() and CLI_BSQ.is_file():
    # APPEND, never insert(0): `worker/tests/` is a real package and
    # `api/tests/` is not, so putting the worker tree FIRST would let a bare
    # `tests` import resolve to the worker's test package. Nothing in the api
    # tree is named `bot_squad_worker`, so the end of the path is enough.
    if str(WORKER_TREE) not in sys.path:
        sys.path.append(str(WORKER_TREE))
    from bot_squad_worker import pace as WPACE

    from app import routes_transparency as ART

    # `bsq` has no `.py` suffix and is a script, so it is loaded by source path.
    # It is import-safe: everything executable sits behind `if __name__ ==
    # "__main__"`, and its module-level state is two path constants read from
    # the environment.
    _spec = importlib.util.spec_from_loader(
        "bsq_cli_under_test",
        importlib.machinery.SourceFileLoader("bsq_cli_under_test", str(CLI_BSQ)),
    )
    BSQ = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(BSQ)


# ---------------------------------------------------------------------------
# The fixture table — one `pace.json` body per row
# ---------------------------------------------------------------------------
# Deliberately includes the shapes a hand-edited or future-version file can
# take, not just the ones our own writer produces: the mirrors are what read a
# file the worker may have written under a LATER version after a partial deploy.

RAW_CASES: list[tuple[str, dict]] = [
    ("absent -- a project that never set a mode", {}),
    ("legacy pace.json, pre-T-0828 shape",
     {"max_in_progress": 7, "initiatives": {"x.md": {"weight": 2.0}}}),
    ("drive present but empty", {"drive": {}}),
    ("his worked example",
     {"drive": {"scope": "open_reopened", "set_by": "S-op", "set_at": "2026-07-30T14:52:31Z",
                "source_text": "закончить всё что в опен"}}),
    ("every axis set explicitly",
     {"drive": {"scope": "in_progress", "stop_when": "spend_quota", "on_stop": "alert",
                "set_by": "stakeholder", "set_at": "2026-07-30T09:46:40Z",
                "source_text": "Потратить квоту"}}),
    ("explicitly set to the WIDEST -- must not read as 'never set'",
     {"drive": {"scope": "all", "set_by": "S-op", "set_at": "2026-07-30T10:00:00Z"}}),
    ("out-of-set scope (typo)", {"drive": {"scope": "opne"}}),
    ("out-of-set on every axis",
     {"drive": {"scope": "OPEN", "stop_when": "forever", "on_stop": "ALERT!"}}),
    ("wrong TYPES, not just wrong values",
     {"drive": {"scope": 5, "stop_when": None, "on_stop": ["alert"],
                "set_by": 17, "set_at": 0, "source_text": 3.5}}),
    ("drive is not a dict at all", {"drive": "open_reopened"}),
    ("drive is null", {"drive": None}),
    ("unknown FUTURE field beside the known ones",
     {"drive": {"scope": "all", "expires_at": "2026-08-01T00:00:00Z"}}),
    ("whitespace around a valid value", {"drive": {"scope": "  open_reopened  "}}),
]

CASE_IDS = [name for name, _ in RAW_CASES]


def _all_three(raw: dict) -> dict[str, dict]:
    """Drive every implementation over ONE input. Each is called through its own
    module's function — nothing here re-implements the normalisation, which is
    the whole point: a copy that drifts cannot hide behind a fourth copy living
    in the test."""
    return {
        "worker/pace.py": WPACE._normalized_drive(raw),
        "api/routes_transparency.py": ART._normalized_drive(raw),
        "scripts/cli/bsq": BSQ._pace_normalized_drive(raw),
    }


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------

def test_the_denominator_three_implementations_over_a_non_empty_table():
    """A SCANNER'S FILE COUNT IS ITS PASS COUNT (p537's rule, T-0828).

    "Clean" over an unstated denominator is "no failures" wearing different
    clothes. This differential compares whatever ``_all_three`` happens to
    return over whatever ``RAW_CASES`` happens to hold — so if either shrank to
    nothing, every test above would PASS over zero comparisons and the guard
    would be ceremonial. That is the same defect class as the module's own
    skip-is-not-coverage note, one level in.

    So the denominator is asserted, not assumed: exactly THREE implementations,
    named, and a table that actually has rows.
    """
    impls = _all_three({})
    assert set(impls) == {
        "worker/pace.py",
        "api/routes_transparency.py",
        "scripts/cli/bsq",
    }, f"expected exactly the three known mirrors, got {sorted(impls)}"
    assert len(RAW_CASES) >= 10, f"fixture table shrank to {len(RAW_CASES)} rows"
    assert len(CASE_IDS) == len(set(CASE_IDS)), "duplicate case ids mask a lost row"


@pytest.mark.parametrize("_name,raw", RAW_CASES, ids=CASE_IDS)
def test_all_three_implementations_agree_exactly(_name: str, raw: dict):
    """The core differential: same input, byte-identical normalised output."""
    got = _all_three(raw)
    worker = got["worker/pace.py"]
    for impl, out in got.items():
        assert out == worker, (
            f"{impl} disagrees with worker/pace.py on {_name!r}.\n"
            f"  worker: {worker}\n  {impl}: {out}"
        )


@pytest.mark.parametrize("_name,raw", RAW_CASES, ids=CASE_IDS)
def test_all_three_carry_the_same_field_set(_name: str, raw: dict):
    """DoD-6 states the requirement as 'fails when one grows a field the other
    lacks', which is a stronger claim than value equality and is asserted
    separately so the failure message NAMES the field.

    This is the direction that actually bites: adding a field to the worker and
    not to the api leaves the UI reading `undefined` for it, which renders as
    "not set" — a false statement about his settings rather than a visible
    error.
    """
    got = _all_three(raw)
    worker_keys = set(got["worker/pace.py"])
    for impl, out in got.items():
        missing = worker_keys - set(out)
        extra = set(out) - worker_keys
        assert not (missing or extra), (
            f"{impl} has a different FIELD SET than worker/pace.py on {_name!r}: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


def test_the_closed_sets_themselves_are_identical():
    """The constants, not just the results.

    A value added to the worker's ``DRIVE_SCOPES`` but not to the other two
    would be accepted on write and then reported as INVALID by the api and the
    CLI — i.e. he sets a mode, the CLI confirms it, and the UI tells him it was
    rejected. The fixture table cannot catch that on its own, because a value
    nobody has heard of yet is not in it.
    """
    assert WPACE.DRIVE_SCOPES == ART._DRIVE_SCOPES == BSQ._PACE_DRIVE_SCOPES
    assert WPACE.DRIVE_STOP_WHEN == ART._DRIVE_STOP_WHEN == BSQ._PACE_DRIVE_STOP_WHEN
    assert WPACE.DRIVE_ON_STOP == ART._DRIVE_ON_STOP == BSQ._PACE_DRIVE_ON_STOP
    assert WPACE.DRIVE_DEFAULTS == ART._DRIVE_DEFAULTS == BSQ._PACE_DRIVE_DEFAULTS
    assert (WPACE.DRIVE_PROVENANCE_FIELDS
            == ART._DRIVE_PROVENANCE_FIELDS
            == BSQ._PACE_DRIVE_PROVENANCE_FIELDS)


def test_every_default_is_itself_a_member_of_its_closed_set():
    """A default outside its own closed set would make every read report the
    effective value as INVALID — including on a project that never set a mode."""
    for field, default in WPACE.DRIVE_DEFAULTS.items():
        assert default in WPACE.DRIVE_CHOICES[field], (
            f"default {default!r} for {field} is not in its own closed set")


def test_a_cross_process_roundtrip_reads_back_what_the_worker_wrote(tmp_path):
    """The mirrors read a file the WORKER wrote, so write it with the worker's
    own writer rather than by hand.

    This is the T-0749 lesson: tests that hand-write the store keep passing
    through exactly the change that breaks production, because the hand-written
    copy is a third copy of the format. Here ``pace.set_drive`` produces the
    bytes and the api + CLI readers consume them.
    """
    import json
    from types import SimpleNamespace

    slug = "test-project"
    cfg = SimpleNamespace(data_dir=tmp_path)
    (tmp_path / slug).mkdir(parents=True, exist_ok=True)

    WPACE.set_drive(
        cfg, slug,
        scope="open_reopened", on_stop="alert",
        set_by="S-almdudleer-operator-p533",
        source_text="закончить всё что в опен",
    )

    on_disk = json.loads((tmp_path / slug / "_worker" / "pace" / "pace.json").read_text())
    got = _all_three(on_disk)
    for impl, out in got.items():
        assert out["scope"] == "open_reopened", impl
        assert out["on_stop"] == "alert", impl
        assert out["configured"] is True, impl
        assert out["invalid"] == {}, impl
        assert out["source_text"] == "закончить всё что в опен", impl
        assert out["set_by"] == "S-almdudleer-operator-p533", impl
        # set_at must survive the roundtrip as the worker stamped it.
        assert out["set_at"] == on_disk["drive"]["set_at"], impl

    # And clearing puts every reader back to the never-set view.
    assert WPACE.clear_drive(cfg, slug) is True
    cleared = json.loads((tmp_path / slug / "_worker" / "pace" / "pace.json").read_text())
    for impl, out in _all_three(cleared).items():
        assert out["configured"] is False, impl
        assert out["scope"] == WPACE.DRIVE_DEFAULTS["scope"], impl
