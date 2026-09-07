"""T-1046: a certification arm can be named in ``ARMS`` (the census in
``test_certification_surface_census.py``) and still never RUN, because the
only workflow that claims to cover it cannot fire at all.

THE DEFECT THIS PINS
------------------------------------------------------------------------
GitHub Actions only runs a workflow's ``schedule:``/``workflow_dispatch:``
triggers from the copy of the file on the repository's DEFAULT branch
(``master`` here). ``push:``/``pull_request:`` triggers are different — they
run the copy on the PUSHED ref, so they fire regardless of what ``master``
carries. ``.github/workflows/nightly-suites.yml`` is ``schedule:`` +
``workflow_dispatch:`` ONLY, and ``master`` carried zero workflow files when
this was measured (T-1046, 2026-09-07: ``git ls-tree origin/master
.github/workflows/`` — empty). The file documents this in its own banner —
prose, not a control — and the arm-existence check in the census (`test_every
_declared_arm_exists_where_the_map_says_it_does`) reads the job/step text
inside that file and is satisfied whether or not the file can ever execute.
Two arms were wired into it in the same session that surfaced this (T-0991
scripts-cli, T-1039 shell-tests) with nothing objecting.

This file closes that gap: an arm counts as covered only if EITHER
  (a) the workflow that names it can fire regardless of default-branch state
      (has a ``push``/``pull_request`` trigger), or
  (b) a host-side runner not gated on GitHub's default-branch mechanics also
      runs it (``scripts/ops/run_nightly_certification.sh``, driven by a
      ``bsq routine`` — see T-1046 Context for the declared routine id).

WHY THIS IS STATIC, NOT A LIVE CHECK OF WHAT master CURRENTLY CARRIES
------------------------------------------------------------------------
Whether ``origin/master`` currently has the workflow file is exactly the
fact T-1046 measured by hand — but re-deriving it here on every test run
would need ``origin/master`` present in the local git object database, which
a CI checkout does NOT guarantee (``actions/checkout@v4`` fetches only the
target ref by default) and a ``git archive`` extract never has (no ``.git``
at all). A check that skips whenever that ref happens to be unfetched would
reproduce T-1036's defect class inside the very file written to catch it.

So the rule here is structural instead: a ``schedule:``/``workflow_dispatch:``-
only workflow is ALWAYS treated as unable to fire on its own, regardless of
whatever `master` happens to carry today — the host-side arm
(``run_nightly_certification.sh``) is required as an independent, locally-
verifiable backstop. If `master` is later kept in sync, that only makes the
GitHub-side arm ALSO run; it does not weaken this guard, and this guard does
not need to know about it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_certification_surface_census import ARMS, ARM_NIGHTLY_JOBS

REPO = Path(__file__).resolve().parents[2]
NIGHTLY_WORKFLOW = REPO / ".github" / "workflows" / "nightly-suites.yml"
HOST_CERT_SCRIPT = REPO / "scripts" / "ops" / "run_nightly_certification.sh"

# Triggers that fire from the PUSHED ref's own copy — reachable regardless of
# what the default branch carries.
ALWAYS_REACHABLE_TRIGGERS = {"push", "pull_request"}

# arm -> a fragment that must appear in HOST_CERT_SCRIPT's source proving
# that arm is actually invoked there, same anchoring discipline as
# ARM_NIGHTLY_JOBS in the census (a needle stops a job from being renamed/
# gutted while claiming to still cover the surface).
ARM_HOST_JOBS = {
    "worker": "worker/tests -q",
    "api": "bot-squad-api:latest",
    "web": "cd '$DEST/web' && npm test",
    "scripts-cli": "-q scripts/cli",
    "shell-tests": ".githooks/test_commit_policy.sh",
}


def _workflow_triggers(wf_path: Path) -> set[str]:
    """The ``on:`` trigger keys of a workflow file.

    A HARD import, same reasoning as the census's own arm-existence check:
    ``pyyaml`` is a declared worker dependency and cannot legitimately be
    absent in any arm that collects ``worker/tests``.

    Parses only the HEADER (everything before the top-level ``jobs:`` key),
    not the whole document. Measured on this repo's own ``lint.yml``: a
    `step name containing "T-0975: real YAML reader"` — a colon-space inside
    a plain scalar, deep in `jobs:` — makes PyYAML raise `ScannerError`
    ("mapping values are not allowed here") on the full file, even though
    GitHub's own parser accepts it fine and the workflow runs green. The
    trigger block this function actually needs always precedes `jobs:` in
    every workflow in this repo, so slicing it off sidesteps that footgun
    entirely rather than papering over it with a broad try/except.
    """
    import yaml

    text = wf_path.read_text(encoding="utf-8")
    header = re.split(r"^jobs:", text, maxsplit=1, flags=re.MULTILINE)[0]
    data = yaml.safe_load(header)
    # T-1046: PyYAML's (YAML-1.1) safe_load resolves the unquoted mapping
    # key `on` to the boolean True, not the string "on" — a well-known
    # footgun for parsing GitHub Actions workflows. Read both keys.
    triggers = data.get("on", data.get(True))
    if triggers is None:
        return set()
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, (list, tuple)):
        return {str(t) for t in triggers}
    if isinstance(triggers, dict):
        return {str(k) for k in triggers}
    raise AssertionError(f"{wf_path}: unrecognised `on:` shape {triggers!r}")


def test_the_healthy_case_first_lint_is_reachable_via_push():
    """Positive control (run before the gate below): a push-triggered
    workflow is reachable on its own, no host-side backstop required — this
    proves the guard below is not simply reporting every workflow as dead.
    """
    lint_wf = REPO / ".github" / "workflows" / "lint.yml"
    assert lint_wf.is_file(), f"{lint_wf} is missing"
    triggers = _workflow_triggers(lint_wf)
    assert triggers & ALWAYS_REACHABLE_TRIGGERS, (
        f"lint.yml's triggers are {sorted(triggers)} — expected at least one "
        f"of {sorted(ALWAYS_REACHABLE_TRIGGERS)}. If this is now true, the "
        "positive control itself needs a different anchor workflow."
    )


def test_every_arm_is_reachable_via_push_or_a_host_side_backstop():
    """The gate. An arm covered ONLY by a schedule/workflow_dispatch-only
    workflow, with no host-side runner backing it up, is reported here —
    with the reason — instead of silently counting as coverage.
    """
    assert NIGHTLY_WORKFLOW.is_file(), f"{NIGHTLY_WORKFLOW} is missing"
    nightly_triggers = _workflow_triggers(NIGHTLY_WORKFLOW)
    nightly_reachable_via_push = bool(nightly_triggers & ALWAYS_REACHABLE_TRIGGERS)

    host_script_text = (
        HOST_CERT_SCRIPT.read_text(encoding="utf-8")
        if HOST_CERT_SCRIPT.is_file() else ""
    )

    unreachable: list[str] = []
    for arm in sorted(set(ARMS.values())):
        if arm not in ARM_NIGHTLY_JOBS:
            # Not one of nightly-suites.yml's arms; out of this check's scope.
            continue
        if nightly_reachable_via_push:
            continue
        needle = ARM_HOST_JOBS.get(arm)
        if needle is None:
            unreachable.append(
                f"{arm}: covered only by nightly-suites.yml (schedule/"
                "workflow_dispatch only — cannot fire unless it exists on "
                "the default branch), and ARM_HOST_JOBS names no host-side "
                "backstop for it"
            )
            continue
        if not HOST_CERT_SCRIPT.is_file():
            unreachable.append(
                f"{arm}: covered only by nightly-suites.yml (schedule/"
                f"workflow_dispatch only) — {HOST_CERT_SCRIPT} does not exist"
            )
        elif needle not in host_script_text:
            unreachable.append(
                f"{arm}: covered only by nightly-suites.yml (schedule/"
                f"workflow_dispatch only) — {HOST_CERT_SCRIPT} exists but no "
                f"longer contains {needle!r} (renamed/gutted?)"
            )

    assert not unreachable, (
        "these arms are claimed as coverage in ARMS but cannot actually run "
        "anywhere — nightly-suites.yml cannot fire (schedule/workflow_"
        "dispatch only, and this repo does not treat default-branch sync as "
        "provable here — see module docstring) and no host-side backstop "
        "covers them:\n  " + "\n  ".join(unreachable)
    )


def test_the_host_cert_script_is_measured_not_assumed():
    """The second end for the host-side backstop itself: it must exist and
    still name every arm ARM_HOST_JOBS claims it does — same "map is
    otherwise prose" reasoning as the census's workflow check.
    """
    if not HOST_CERT_SCRIPT.is_file():
        pytest.skip(
            f"{HOST_CERT_SCRIPT} does not exist yet — nothing to measure "
            "(test_every_arm_is_reachable_via_push_or_a_host_side_backstop "
            "is the gate that fails on this)"
        )
    text = HOST_CERT_SCRIPT.read_text(encoding="utf-8")
    missing = sorted(
        f"{arm} (needle {needle!r})"
        for arm, needle in ARM_HOST_JOBS.items()
        if needle not in text
    )
    assert not missing, (
        f"{HOST_CERT_SCRIPT} no longer names these arms:\n  "
        + "\n  ".join(missing)
    )
