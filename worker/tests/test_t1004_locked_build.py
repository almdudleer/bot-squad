"""T-1004: the api image must be a function of the ref, and the build must prove it.

`api/pyproject.toml` declared 13 dependency specs, every one a `>=` floor, with
no lockfile — so two builds of the SAME commit on two days resolved whatever
PyPI served each day. Measured, not theoretical: on 2026-09-06 the install ran
fastapi 0.136.1 while an image under test had 0.141.1, and an api suite result
had already changed because a test split MOVED between fastapi versions — a
count difference with no commit behind it.

Three inputs were uncontrolled, not one, and a lockfile alone would have let a
reader believe the build was reproducible when it was not: the pip floors, and
BOTH `FROM` lines, which named mutable tags on a registry we do not control —
the same rebuild-moves-the-tag defect as `bot-squad-api:latest` (T-1001), one
layer down and somewhere the outgoing artefact cannot even be retained.

THE POINT OF THIS FILE IS THE THIRD TEST. Asserting that a lockfile exists and
that `FROM` carries a digest is presence-checking, and a presence check is blind
to whether the thing works. So the drift guard — the one control that makes the
lock a pin rather than a record — is EXECUTED HERE, as the Dockerfile's own
bytes, against a matching pair and a drifted one, with `pip` stubbed. A guard
nobody has watched fail is a guard nobody has tested.

apt remains unpinned and CI still installs from the floors. Both are named in
`api/requirements.lock`; neither is fixed here, and neither should be discovered
by a reader who trusted the word "locked".
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "api" / "Dockerfile"
LOCK = REPO / "api" / "requirements.lock"
PYPROJECT = REPO / "api" / "pyproject.toml"

_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;#]+)\s*$")


def _lock_pins() -> dict:
    pins = {}
    for raw in LOCK.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        assert m, f"{LOCK.name}: not an exact pin: {line!r}"
        pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pins


def _from_lines(text: str) -> list:
    return [ln.strip() for ln in text.splitlines()
            if ln.strip().upper().startswith("FROM ")]


def _unpinned_bases(text: str) -> list:
    """FROM lines naming a MUTABLE reference. A named build STAGE is not one —
    `FROM x AS builder` … `COPY --from=builder` refers to this build, not to a
    registry, so requiring a digest there would be an unsatisfiable gate."""
    stages = set()
    bad = []
    for line in _from_lines(text):
        parts = line.split()
        ref = parts[1]
        if len(parts) >= 4 and parts[2].upper() == "AS":
            stages.add(parts[3].lower())
        if ref.lower() in stages:
            continue
        if "@sha256:" not in ref:
            bad.append(ref)
    return bad


# --- The lock ---------------------------------------------------------------

def test_every_line_of_the_lock_is_an_exact_pin():
    pins = _lock_pins()
    assert len(pins) >= 30, f"only {len(pins)} pins — that is not the api set"
    assert pins["fastapi"] == "0.141.1"
    assert pins["pydantic"] == "2.13.5"
    assert pins["uvicorn"] == "0.52.4"


def _declared_dependencies() -> set:
    """Every dependency `api/pyproject.toml` declares, PARSED, not grepped.

    The first version of this read the file with a regex over `"name>=…"`, and
    it had a hole big enough to defeat the test that uses it: **TOML strings may
    be single-quoted**, so `'fastapi>=0.110'` would have been invisible and the
    caller would have reported full coverage of a set with a package missing
    from it. That is the same class as the three grep censuses that failed
    tonight — a textual counter cannot tell a declaration from a quotation of
    one, and it returns a confident answer either way. `tomllib` is the parser
    the file is written for; use it.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    project = data.get("project", {})
    specs = list(project.get("dependencies", []))
    for extra in (project.get("optional-dependencies") or {}).values():
        specs.extend(extra)
    names = set()
    for spec in specs:
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)", str(spec))
        if m:
            names.add(m.group(1).lower().replace("_", "-"))
    return names


def test_the_declared_set_is_not_empty():
    """CHECK THE INSTRUMENT CAN SEE A ONE BEFORE BELIEVING ITS ZERO. If the
    parse silently returned nothing, the coverage test below would pass over an
    empty set and report that a lock covering nothing covers everything."""
    declared = _declared_dependencies()
    assert len(declared) >= 10, f"only {len(declared)} declared deps parsed"
    assert "fastapi" in declared and "pytest" in declared


def test_a_single_quoted_dependency_is_still_seen():
    """The exact hole the regex version had. TOML accepts both quote styles and
    a reader that only knows one reports coverage of a set it cannot see."""
    data = tomllib.loads(
        "[project]\ndependencies = ['quoted-single>=1.0', \"quoted-double>=2\"]\n")
    assert set(data["project"]["dependencies"]) == {
        "quoted-single>=1.0", "quoted-double>=2"}


def test_the_lock_covers_every_dependency_pyproject_names():
    """A lock that omits a declared dependency lets pip resolve that one
    freely — the defect surviving inside its own fix."""
    missing = sorted(d for d in _declared_dependencies() if d not in _lock_pins())
    assert not missing, f"declared in pyproject but not locked: {missing}"


def test_the_lock_says_what_it_does_not_pin():
    """apt and CI are still unpinned. A reader who trusts the word 'locked'
    must not have to discover that from a build."""
    text = LOCK.read_text()
    assert "apt" in text and "CI" in text
    assert "hash" in text.lower()


# --- The bases --------------------------------------------------------------

def test_no_build_stage_pulls_a_mutable_tag():
    assert _unpinned_bases(DOCKERFILE.read_text()) == []


def test_the_base_check_can_actually_fail():
    """The red control. Without it this file would pass against a checker that
    returned an empty list for everything."""
    drifted = DOCKERFILE.read_text().replace(
        "FROM python:3.12-slim@sha256:", "FROM python:3.12-slim#", 1)
    assert _unpinned_bases(drifted) == ["python:3.12-slim#"
                                        + drifted.split("FROM python:3.12-slim#")[1].split()[0]]


def test_a_named_stage_is_not_treated_as_a_mutable_base():
    """`COPY --from=web-builder` names this build, not a registry. A checker
    that demanded a digest there would be a gate nobody could satisfy."""
    assert _unpinned_bases(
        "FROM alpine@sha256:" + "a" * 64 + " AS web-builder\n"
        "FROM web-builder AS final\n") == []


# --- The guard, executed ----------------------------------------------------

def _run_block() -> str:
    """The Dockerfile's own RUN body — continuations joined, `RUN ` stripped."""
    text = DOCKERFILE.read_text()
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("RUN pip install --no-cache-dir -r "
                                   "requirements.lock")), None)
    assert start is not None, (
        "the locked install line is gone from api/Dockerfile — this test would "
        "silently stop executing the guard it exists to test")
    out = []
    for ln in lines[start:]:
        out.append(ln)
        if not ln.rstrip().endswith("\\"):
            break
    return "\n".join(out)[len("RUN "):]


PIP_STUB = r'''#!/usr/bin/env python3
import os, sys
if sys.argv[1:2] == ["freeze"]:
    sys.stdout.write(open(os.environ["FAKE_INSTALLED"]).read())
sys.exit(0)
'''


def _exec_guard(tmp_path, locked: str, installed: str) -> int:
    """Run the SHIPPED RUN body with `pip` stubbed. The install steps become
    no-ops; the comparison is the Dockerfile's, byte for byte."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "pip").write_text(PIP_STUB)
    (bin_dir / "pip").chmod(0o755)
    (tmp_path / "requirements.lock").write_text(locked)
    fake = tmp_path / "installed.txt"
    fake.write_text(installed)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    env["FAKE_INSTALLED"] = str(fake)
    script = tmp_path / "run.sh"
    script.write_text("set -e\n" + _run_block())
    return subprocess.run(["bash", str(script)], cwd=str(tmp_path),
                          capture_output=True, text=True, env=env).returncode


LOCKED = "# a comment\nfastapi==0.141.1\npydantic_core==2.46.5\nPyYAML==6.0.3\n"


def test_the_guard_passes_when_the_installed_set_matches(tmp_path):
    """The green case first. A guard that fails on everything is not a guard,
    and a red-only test cannot tell the two apart."""
    assert _exec_guard(tmp_path, LOCKED,
                       "fastapi==0.141.1\npydantic-core==2.46.5\npyyaml==6.0.3\n") == 0


def test_the_guard_fails_the_build_when_a_version_drifts(tmp_path):
    assert _exec_guard(tmp_path, LOCKED,
                       "fastapi==0.142.0\npydantic-core==2.46.5\npyyaml==6.0.3\n") != 0


def test_the_guard_fails_on_an_UNLOCKED_extra_package(tmp_path):
    """The case a version-by-version check misses: pip resolved a transitive
    the lock never named."""
    assert _exec_guard(
        tmp_path, LOCKED,
        "fastapi==0.141.1\npydantic-core==2.46.5\npyyaml==6.0.3\nsniffio==1.3.1\n") != 0


def test_the_guard_fails_on_a_MISSING_package(tmp_path):
    assert _exec_guard(tmp_path, LOCKED,
                       "fastapi==0.141.1\npyyaml==6.0.3\n") != 0


def test_case_and_underscore_differences_are_not_drift(tmp_path):
    """pip freeze and PyPI disagree on both (pydantic_core vs pydantic-core,
    PyYAML vs pyyaml). A guard that called those a mismatch would fail every
    build and be removed within the day."""
    assert _exec_guard(tmp_path, "PyYAML==6.0.3\npydantic_core==2.46.5\n",
                       "pyyaml==6.0.3\npydantic-core==2.46.5\n") == 0


def test_there_is_no_silent_fallback_install():
    """The old line ended `|| pip install --no-cache-dir .` — a fallback that
    installs a DIFFERENT dependency set when the primary fails is how a build
    reports success while producing an environment nobody asked for.

    COMMENTS ARE STRIPPED FIRST, and that is not a detail: the Dockerfile
    EXPLAINS why the fallback was removed, quoting it, and the first version of
    this test went red on its own documentation. A detector that cannot tell a
    defect from a description of the defect reports the fix as the bug.
    """
    body = "\n".join(ln for ln in DOCKERFILE.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "|| pip install" not in body
    assert "|| pip install" in DOCKERFILE.read_text(), (
        "the comment explaining the removal is gone — if this ever passes "
        "because nothing mentions the fallback any more, the test above is "
        "no longer distinguishing code from prose")
