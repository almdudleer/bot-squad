"""T-1002: pin the substrate-refusal guard — that it fires, and that it doesn't.

The defect: `worker/tests/` run under the bot-squad-api image reports a
plausible **122 passed / 5 failed** for `test_monitors.py`, because
`apscheduler` is a worker dependency the api tree does not declare and
`ScheduleTrigger.__init__` imports it lazily. Nobody reads five red lines out
of 127 as "this run measured nothing about schedules". T-0824 measured the
same shape at 22 failures from a missing `rsync`.

Two halves, and the second is the one that makes the first mean anything:

* the guard FIRES on a genuinely absent module / binary (end-to-end, through
  the real conftest hooks, in a subprocess pytest);
* the guard STAYS SILENT on an ordinary failure, on a ModuleNotFoundError for
  a module that IS importable, and on a FileNotFoundError for a binary that IS
  on PATH — otherwise it would relabel real product bugs as "the environment"
  and become the mask it exists to remove.

The end-to-end runs load the hooks OUT OF THE REAL `conftest.py` by path
rather than restating them, so this test and the shipped guard cannot drift
apart (the same construction lint.yml's mirror gate uses).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REAL_CONFTEST = _TESTS_DIR / "conftest.py"

#: absent by construction — nothing may ever create these
ABSENT_MODULE = "definitely_not_a_real_module_t1002"
ABSENT_BINARY = "definitely-not-a-binary-t1002"


def _load_substrate():
    spec = importlib.util.spec_from_file_location(
        "t1002_substrate_under_test", _TESTS_DIR / "substrate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def substrate():
    """A FRESH module per test — ``GAPS`` is module-global by design."""
    return _load_substrate()


# --- the classifier, both directions -----------------------------------------


def test_absent_module_is_a_substrate_gap(substrate):
    try:
        importlib.import_module(ABSENT_MODULE)
    except ModuleNotFoundError as e:
        assert substrate.classify(e) == ("distribution", ABSENT_MODULE)
    else:  # pragma: no cover - would mean the name got created
        pytest.fail(f"{ABSENT_MODULE} unexpectedly importable")


def test_absent_binary_is_a_substrate_gap(substrate):
    with pytest.raises(FileNotFoundError) as ei:
        subprocess.run([ABSENT_BINARY], check=False)
    assert substrate.classify(ei.value) == ("binary", ABSENT_BINARY)


def test_a_submodule_reports_its_top_level_distribution(substrate):
    e = ModuleNotFoundError("No module named 'x'", name=f"{ABSENT_MODULE}.triggers.cron")
    assert substrate.classify(e) == ("distribution", ABSENT_MODULE)


def test_importable_module_is_not_a_substrate_gap(substrate):
    """A test that scrubs sys.modules to prove a lazy-import path is NOT a gap."""
    e = ModuleNotFoundError("No module named 'os'", name="os")
    assert substrate.classify(e) is None


def test_binary_on_path_is_not_a_substrate_gap(substrate):
    """A wrong argv[0] for a tool that IS installed is a product bug, not us."""
    e = FileNotFoundError(2, "No such file or directory", "sh")
    assert substrate.classify(e) is None


def test_a_path_not_a_program_name_is_not_a_substrate_gap(substrate):
    """A file the code expected to exist is the code's problem."""
    e = FileNotFoundError(2, "No such file or directory", "/nope/config.toml")
    assert substrate.classify(e) is None


def test_an_ordinary_failure_is_not_a_substrate_gap(substrate):
    assert substrate.classify(AssertionError("assert 1 == 2")) is None


def test_a_wrapped_gap_is_still_found(substrate):
    """Fixtures and helpers re-raise; the chain is where the cause lives."""
    try:
        try:
            importlib.import_module(ABSENT_MODULE)
        except ModuleNotFoundError as inner:
            raise RuntimeError("fixture blew up") from inner
    except RuntimeError as e:
        assert substrate.classify(e) == ("distribution", ABSENT_MODULE)


def test_gaps_accumulate_per_name(substrate):
    e = ModuleNotFoundError("No module named 'x'", name=ABSENT_MODULE)
    substrate.record_exception(e)
    substrate.record_exception(e)
    substrate.record_exception(AssertionError("unrelated"))
    assert substrate.GAPS["distribution"] == {ABSENT_MODULE: 2}
    assert substrate.total_failures() == 2


# --- the declared-dependency half --------------------------------------------


def test_declared_missing_reads_the_workers_own_declaration(substrate, tmp_path):
    """No hand-listed dep names — a dependency added tomorrow is covered."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        "[project]\n"
        "dependencies = [\n"
        '  "pytest>=8",\n'
        '  "httpx[socks]>=0.27",\n'
        f'  "{ABSENT_MODULE}>=1.0",\n'
        "]\n"
    )
    missing = substrate.declared_missing(pp)
    # pytest is installed here by definition; the invented one is not.
    assert ABSENT_MODULE in missing
    assert "pytest" not in missing


def test_declared_missing_covers_the_dev_extra_too(substrate, tmp_path):
    """pytest-asyncio lives there and its absence costs a SKIP, not a failure."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        "[project]\n"
        'dependencies = ["pytest>=8"]\n'
        "[project.optional-dependencies]\n"
        f'dev = ["{ABSENT_MODULE}-dev>=1"]\n'
        'voice = ["never-checked-extra>=1"]\n'
    )
    missing = substrate.declared_missing(pp)
    assert f"{ABSENT_MODULE}-dev" in missing
    # optional extras that are opt-in by design are NOT a substrate gap
    assert "never-checked-extra" not in missing


def test_declared_missing_survives_a_missing_or_odd_pyproject(substrate, tmp_path):
    assert substrate.declared_missing(tmp_path / "nope.toml") == []
    junk = tmp_path / "junk.toml"
    junk.write_text("[project]\nname = 'x'\n")
    assert substrate.declared_missing(junk) == []


def test_the_real_worker_pyproject_is_where_it_is_expected(substrate):
    """A wrong path would make declared_missing silently always empty."""
    assert substrate.PYPROJECT.is_file(), substrate.PYPROJECT
    assert "bot-squad-worker" in substrate.PYPROJECT.read_text()


# --- end to end, through the REAL conftest hooks ------------------------------

_TMP_CONFTEST = f'''
import importlib.util
_spec = importlib.util.spec_from_file_location("real_conftest", r"{_REAL_CONFTEST}")
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)

# The SHIPPED hooks, not a restatement of them.
pytest_exception_interact = _real.pytest_exception_interact
pytest_terminal_summary = _real.pytest_terminal_summary
pytest_unconfigure = _real.pytest_unconfigure
'''

_PLAIN_FAILURE = '''
def test_ordinary_bug():
    assert 1 == 2
'''

_SUBSTRATE_FAILURES = f'''
import subprocess


def test_needs_a_missing_package():
    import {ABSENT_MODULE}  # noqa: F401


def test_needs_a_missing_binary():
    subprocess.run(["{ABSENT_BINARY}"], check=False)


def test_ordinary_bug():
    assert 1 == 2
'''


def _run_pytest(tmp_path: Path, test_source: str) -> str:
    (tmp_path / "conftest.py").write_text(_TMP_CONFTEST)
    (tmp_path / "test_sample.py").write_text(test_source)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_sample.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr


def test_missing_substrate_gets_a_banner_and_a_trailer(tmp_path):
    substrate = _load_substrate()
    out = _run_pytest(tmp_path, _SUBSTRATE_FAILURES)

    assert substrate.BANNER_TITLE in out, out
    assert ABSENT_MODULE in out
    assert ABSENT_BINARY in out
    # 3 tests failed but only 2 are the environment — the banner must not
    # launder the real bug into the substrate count.
    assert "2 failure(s) in this run are THE ENVIRONMENT" in out, out

    # The trailer lands AFTER pytest's own count line, so the caveat travels
    # with whichever line a reader copies into a report.
    trailer_at = out.rfind(substrate.TRAILER_PREFIX)
    count_at = out.rfind("3 failed")
    assert trailer_at > -1, out
    assert count_at > -1, out
    assert trailer_at > count_at, out


def test_an_ordinary_red_run_says_nothing_about_the_substrate(tmp_path):
    """The green control: without this the guard could fire on everything."""
    substrate = _load_substrate()
    out = _run_pytest(tmp_path, _PLAIN_FAILURE)

    assert "1 failed" in out, out
    assert substrate.BANNER_TITLE not in out, out
    assert substrate.TRAILER_PREFIX not in out, out
