"""T-0987: a certification must publish the skips it bought, not just the passes.

`git archive` carries COMMITTED PATHS ONLY, so the frozen extract the fleet was
told to certify on has no `.git`. Every test needing a real checkout therefore
skips — and a skip is a green-adjacent outcome nobody reads. On 2026-09-06 the
fleet spent a day reading `N passed, M skipped` as certification with nobody
naming what M held, while the flag everyone had standardised on (`-rf`) was
REPLACING the default report chars and printing no skip reasons at all.

TWO THINGS THIS FILE PINS, and they are different claims:

  * THE REASONS ARE PRINTED. Not "a reader could ask for them" — the tool
    merges `s` into `-r` itself, because a requirement that lives only in prose
    is not a control.
  * EQUAL COUNTS ARE NOT EQUAL SETS. Today's api attribution leaned partly on
    1+1604+2 == 0+1605+2 to argue exactly one test moved and nothing else was
    disturbed — an argument that assumes the two skips are the SAME two on both
    sides, which nobody checked. `test_a_swapped_skip_at_an_equal_count_*`
    below is that check, and it is the reason the census publishes a
    fingerprint of the SET rather than a total.

EVERY pytest fragment here was CAPTURED from a real pytest 9.0.3 run against a
throwaway tree, never composed to fit the parser — the same discipline
test_bsq_verify_measurement.py's header sets out, and for the same reason: the
first cut of `_PYTEST_SKIPPED_LINE_RE` passed on hand-written lines and then
split `sub/test_modskip.py:2: no ambient git repo…` at the FIRST colon, filing
the line number as the head of the reason. Only real output showed it.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
bsq = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("bsq_mod", _loader))
_loader.exec_module(bsq)


# --- Real outputs -----------------------------------------------------------

# CAPTURED 2026-09-06 from pytest 9.0.3 (worker/.venv) over a throwaway tree
# holding one module-level skip, two `pytest.skip()` calls and one
# `skipif` marker. This is the shape the census must read.
REAL_RS = """\
.sssF                                                                    [100%]
=========================== short test summary info ============================
SKIPPED [1] sub/test_modskip.py:2: no ambient git repo (this tree is a `git archive` extract) — the index-vs-disk comparison has no index to read here
SKIPPED [1] test_skips.py:9: not a git checkout
SKIPPED [1] test_skips.py:13: not a git checkout
SKIPPED [1] test_skips.py:15: git or git-guard.sh not available
1 failed, 1 passed, 4 skipped in 0.03s
"""

# THE SAME RUN, THE SAME TREE, with the flag the fleet had standardised on.
# Four skips, zero reasons. This is not a degraded report — it is the entire
# defect, and it is why the reasons are injected rather than requested.
REAL_RF = """\
=========================== short test summary info ============================
FAILED test_skips.py::test_fails - assert 1 == 2
1 failed, 1 passed, 4 skipped in 0.02s
"""


def _census(text):
    m = bsq._measure_pytest_output(text)
    return bsq._skip_census(text, m["counts"])


# --- The reasons ------------------------------------------------------------

def test_rf_alone_reports_four_skips_and_names_none_of_them():
    """The premise, pinned on real output rather than asserted in a comment."""
    c = _census(REAL_RF)
    assert c["reported"] == 4
    assert c["entries"] == []
    assert c["complete"] is False


def test_the_census_names_every_skip_with_its_reason_verbatim():
    c = _census(REAL_RS)
    assert c["reported"] == 4 and c["named"] == 4 and c["complete"] is True
    reasons = [why for _, _, why in c["entries"]]
    assert "not a git checkout" in reasons
    assert "git or git-guard.sh not available" in reasons
    assert any(r.startswith("no ambient git repo (this tree is a `git archive`")
               for r in reasons), reasons


def test_the_location_keeps_its_line_number_and_the_reason_starts_at_the_reason():
    """Control over this file's own first bug. A non-greedy split at the first
    colon yields location `sub/test_modskip.py` and reason `2: no ambient…` —
    both wrong, and the reason silently gains a number at its head."""
    c = _census(REAL_RS)
    locs = {loc for _, loc, _ in c["entries"]}
    assert "sub/test_modskip.py:2" in locs, locs
    for _, loc, why in c["entries"]:
        assert re.search(r":\d+$", loc), loc
        assert not re.match(r"^\d+:", why), why


def test_reasons_not_captured_is_stated_and_not_implied_by_silence():
    lines = "\n".join(bsq._census_lines(_census(REAL_RF)))
    assert "REASONS NOT CAPTURED" in lines
    assert "does NOT say the suite is green over them" in lines
    # And the count is still published — the hole has a size even unnamed.
    assert "4 skipped" in lines


def test_a_partial_naming_is_flagged_rather_than_read_as_the_whole_set():
    """A summary saying 4 with 3 named is not a 3-skip run. Dropping the
    difference is how a census becomes the thing it replaced."""
    text = REAL_RS.replace(
        "SKIPPED [1] test_skips.py:15: git or git-guard.sh not available\n", "")
    c = _census(text)
    assert c["reported"] == 4 and c["named"] == 3 and c["complete"] is False
    lines = "\n".join(bsq._census_lines(c))
    assert "PARTIAL" in lines and "only 3 were named" in lines


# --- Equal counts are not equal sets ---------------------------------------

def test_a_swapped_skip_at_an_equal_count_moves_the_fingerprint():
    """THE CHECK THE CONSERVATION ARGUMENT NEEDED.

    Two runs, two skips each, one member swapped. A total conserves; the set
    does not. If this ever goes green the fingerprint has stopped being a set
    hash and the `1+1604+2 == 0+1605+2` style of argument is unguarded again.
    """
    a = ("SKIPPED [1] test_x.py:9: not a git checkout\n"
         "SKIPPED [1] test_y.py:4: worker tree not available\n"
         "2 passed, 2 skipped in 0.10s\n")
    b = ("SKIPPED [1] test_x.py:9: not a git checkout\n"
         "SKIPPED [1] test_z.py:7: recipe not present in this checkout\n"
         "2 passed, 2 skipped in 0.10s\n")
    ca, cb = _census(a), _census(b)
    assert ca["reported"] == cb["reported"] == 2      # the total conserves…
    assert ca["fingerprint"] != cb["fingerprint"]     # …the membership does not


def test_the_same_set_in_a_different_order_keeps_one_fingerprint():
    """Otherwise every comparison is a false alarm and the instrument is
    abandoned within a day."""
    a = ("SKIPPED [1] test_x.py:9: not a git checkout\n"
         "SKIPPED [1] test_y.py:4: worker tree not available\n"
         "2 passed, 2 skipped in 0.10s\n")
    b = ("SKIPPED [1] test_y.py:4: worker tree not available\n"
         "SKIPPED [1] test_x.py:9: not a git checkout\n"
         "2 passed, 2 skipped in 0.10s\n")
    assert _census(a)["fingerprint"] == _census(b)["fingerprint"]


def test_a_moved_count_at_the_same_location_moves_the_fingerprint():
    a = "SKIPPED [1] test_x.py:9: not a git checkout\n1 passed, 1 skipped in 0.1s\n"
    b = "SKIPPED [3] test_x.py:9: not a git checkout\n1 passed, 3 skipped in 0.1s\n"
    assert _census(a)["fingerprint"] != _census(b)["fingerprint"]


# --- The tag claims only what a keyword read establishes --------------------

def test_a_reason_about_the_git_BINARY_is_not_tagged_a_checkout_condition():
    """Control over the second bug this file's author shipped and measured.

    `git or git-guard.sh not available` fires on `shutil.which("git")` — the
    binary, not the metadata. Tagging it as caused by the missing `.git` turns
    a re-runnable skip into a "can never be certified", which is a worse error
    than no tag at all.
    """
    assert bsq._STRUCTURAL_SKIP_RE.search("not a git checkout")
    assert bsq._STRUCTURAL_SKIP_RE.search(
        "no ambient git repo (this tree is a `git archive` extract)")
    assert not bsq._STRUCTURAL_SKIP_RE.search(
        "git or git-guard.sh not available")
    assert not bsq._STRUCTURAL_SKIP_RE.search(
        "[voice] extra not installed (run provision-voice.sh)")


def test_the_tag_says_names_the_condition_not_is_caused_by_it():
    lines = "\n".join(bsq._census_lines(_census(REAL_RS)))
    assert "NAMES a checkout/worktree/VCS condition" in lines
    assert "VERBATIM" in lines
    assert "2 of 3 reason families name such a condition" in lines


def test_the_census_groups_by_reason_so_a_family_reads_as_a_class():
    """Requested by the operator and p664 after a THIRD family turned up:
    two api tests derive the worker path differently, so their guards are
    satisfied by MUTUALLY EXCLUSIVE mounts and no single invocation runs both.
    One line per site hides that; one entry per reason, with its sites under
    it, makes it a class you can see.
    """
    lines = bsq._census_lines(_census(REAL_RS))
    body = "\n".join(lines)
    # The two `not a git checkout` sites collapse into ONE reason entry…
    assert "2 skip(s) at 2 site(s): not a git checkout" in body, body
    # …and both locations are still named under it.
    assert "      test_skips.py:9" in lines
    assert "      test_skips.py:13" in lines
    # One entry per DISTINCT reason, never one per site.
    assert sum(1 for l in lines if " skip(s) at " in l) == 3, lines


def test_one_reason_at_many_sites_is_not_reported_as_many_reasons():
    """The control for the grouping: three sites, one reason, one entry."""
    text = ("SKIPPED [1] a.py:1: worker tree not available in this environment\n"
            "SKIPPED [1] b.py:2: worker tree not available in this environment\n"
            "SKIPPED [1] c.py:3: worker tree not available in this environment\n"
            "5 passed, 3 skipped in 0.20s\n")
    lines = bsq._census_lines(_census(text))
    entries = [l for l in lines if " skip(s) at " in l]
    assert len(entries) == 1, lines
    assert "3 skip(s) at 3 site(s)" in entries[0], entries


def test_zero_skips_is_published_too():
    """The value beside the verdict. A census printed only when it looked
    interesting is a census a reader cannot tell from one nobody took."""
    lines = "\n".join(bsq._census_lines(_census("2 passed in 0.10s\n")))
    assert "SKIP CENSUS — 0 skipped" in lines


# --- The injection ----------------------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    (["pytest", "-q"],                       ["pytest", "-q", "-rs"]),
    (["pytest", "-q", "-rf"],                ["pytest", "-q", "-rfs"]),
    (["pytest", "-rsf"],                     ["pytest", "-rsf"]),
    (["pytest", "-qrf"],                     ["pytest", "-qrfs"]),
    (["python", "-m", "pytest", "-r", "fE"], ["python", "-m", "pytest", "-r", "fEs"]),
    # `-k` CONSUMES the rest of its group. A scan that does not stop there
    # reads `rfoo` out of somebody's keyword expression as report chars and
    # rewrites the expression.
    (["pytest", "-k", "xrfoo", "-q"],        ["pytest", "-k", "xrfoo", "-q", "-rs"]),
    (["pytest", "-kxrfoo"],                  ["pytest", "-kxrfoo", "-rs"]),
    # Not a pytest argv: nothing to merge into, and nothing is guessed.
    (["bash", "-c", "pytest -q"],            ["bash", "-c", "pytest -q"]),
])
def test_skip_reasons_are_merged_into_r_rather_than_replacing_it(argv, expected):
    got, _ = bsq._with_skip_reasons(argv)
    assert got == expected


def test_the_injection_changes_reporting_only_never_what_runs():
    """The property that makes injecting safe: every token that is not the
    `-r` value survives byte-identical, so no result can move because of it."""
    for argv in (["pytest", "-q", "-rf", "tests/test_a.py", "-k", "not slow"],
                 ["python", "-m", "pytest", "--tb=no", "-q", "tests"]):
        got, _ = bsq._with_skip_reasons(argv)
        strip = lambda c: [t for t in c if not re.fullmatch(r"-[a-zA-Z]*r[a-zA-Z]*", t)]
        assert strip(got) == strip(argv), (argv, got)


def test_a_shape_we_cannot_rewrite_says_so_instead_of_guessing():
    got, note = bsq._with_skip_reasons(["pytest", "-r"])
    assert got == ["pytest", "-r"]
    assert note and "could not safely merge" in note


# --- End to end -------------------------------------------------------------

_SKIPPING_TESTS = '''\
import pytest
from pathlib import Path

def test_ok():
    assert True

def test_needs_a_checkout():
    if not (Path(__file__).parent / ".git").exists():
        pytest.skip("not a git checkout")
'''


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """A throwaway repo for verify-isolated to extract, plus a test file kept
    OUTSIDE it and passed by absolute path.

    The repo is its own, not this checkout's (T-0983): a `git archive` extract
    has no `.git`, so a test reaching for the ambient repo goes red on every
    clean run the moment this suite is certified the mandated way.
    """
    d = tmp_path_factory.mktemp("t0987")
    (d / "test_skipping.py").write_text(_SKIPPING_TESTS)
    repo = d / "repo"
    repo.mkdir()
    (repo / "README").write_text("t0987 throwaway repo\n")
    for argv in (["git", "init", "-q", "."],
                 ["git", "config", "user.email", "t0987@example.invalid"],
                 ["git", "config", "user.name", "t0987"],
                 ["git", "add", "-A"],
                 ["git", "commit", "-qm", "base"]):
        subprocess.run(argv, cwd=str(repo), check=True, capture_output=True)
    return d


def _run_verify(sandbox, cmd):
    proc = subprocess.run(
        [sys.executable, str(_BSQ_PATH), "verify-isolated", "--"] + list(cmd),
        cwd=str(sandbox / "repo"), env=dict(os.environ),
        capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def test_a_run_that_asked_for_rf_still_gets_its_skip_reasons(sandbox):
    """THE WHOLE TICKET, end to end, through the real CLI.

    The caller writes the flag the fleet standardised on; the tool merges `s`
    in, says that it did, and publishes the reason verbatim beside the pass
    count. Nothing here depends on the operator remembering a rule.
    """
    rc, out = _run_verify(sandbox, [
        sys.executable, "-m", "pytest", "-q", "-rf",
        str(sandbox / "test_skipping.py")])
    assert "`-rf` -> `-rfs`" in out, out
    assert "SKIP CENSUS — 1 skipped" in out, out
    assert "not a git checkout" in out, out
    assert "NAMES a checkout/worktree/VCS condition" in out, out
    assert "fingerprint: skips:" in out, out
    assert "MEASURED 1 test(s) executed" in out, out
    assert rc == 0, out


def test_the_census_rides_every_run_including_a_clean_one(sandbox):
    """A census printed only when skips exist teaches readers that its absence
    means nothing happened, which is the habit this ticket exists to break."""
    rc, out = _run_verify(sandbox, [
        sys.executable, "-m", "pytest", "-q",
        str(sandbox / "test_skipping.py"), "-k", "test_ok"])
    assert "SKIP CENSUS — 0 skipped" in out, out
    assert rc == 0, out
