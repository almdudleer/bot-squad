"""T-1002: make an INCOMPLETE TEST SUBSTRATE refuse to be read as a result.

The defect this exists for
--------------------------
`worker/tests/` is routinely run under the `bot-squad-api:latest` image (or a
bare `api[dev]` install) via ``cd worker && PYTHONPATH=. pytest``. That works
because `conftest.py` and several worker modules are stdlib-clean — and four
CI lint steps depend on it, deliberately.

But that substrate is NOT the worker's. `apscheduler>=3.10` is declared in
`worker/pyproject.toml` and absent from `api/pyproject.toml`; the image also
has no `rsync` and no `docker`. `routines.ScheduleTrigger.__init__` imports
apscheduler LAZILY, so the module imports fine and only the tests that
CONSTRUCT a schedule trigger blow up.

The result is the dangerous shape: **a plausible 122 passed / 5 failed**, not
an obviously broken harness. Nobody reads five red lines out of a hundred as
"this run measured nothing about schedules" — they read it as "five bugs" or,
worse, cite the 122 as coverage. T-0824 measured the same thing at larger
scale: 22 failures in that image that do not exist anywhere else, 13 of them a
`FileNotFoundError: 'rsync'` visible only if you open the traceback.

What this does
--------------
Classify every failing test's exception. When it is a missing module or a
missing executable **that is genuinely absent from this environment**, the run
is annotated as SUBSTRATE INCOMPLETE — a banner in the terminal summary, and a
one-line trailer printed AFTER pytest's own count so whichever line a reader
copies carries the caveat with it.

It is conditioned on the PROPERTY, not on a hand-listed set of names
(no "apscheduler, rsync, tmux" list to drift): a ModuleNotFoundError counts
only when `find_spec` also cannot see the module, and a FileNotFoundError
counts only when `shutil.which` also cannot see the binary. So a genuine
product bug that raises either one — a wrong module name, a bad argv[0] for a
tool that IS installed — is left alone and reported as the plain failure it is.

KNOWN LIMITS, stated so nobody over-reads this.

* It fires on FAILURES only. A path lost to `importorskip` or any other skip
  reports as a skip and this does not see it (that loss class is T-0987's). It
  also cannot see a path that has no test at all.
* **The attributed count is a LOWER BOUND on the environment's damage, not a
  split of the failures into "environment" and "real".** Measured on the api
  image at 19:35Z: 6 failed, of which this module attributed 5 to apscheduler
  and correctly refused to claim the 6th
  (`test_sweep_capacity_deferral_retries_without_stamping_cooldown`) — which
  then turned out to reproduce under NEITHER the complete worker venv NOR the
  worker venv with apscheduler hidden. So it is an artefact of that substrate
  by some other route, and reading it as a product bug is exactly the mistake
  this module exists to prevent. The banner says so rather than letting the
  unlisted remainder read as vindicated.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import shutil
import tomllib
from pathlib import Path
from typing import Optional, Tuple

#: kind -> {name: failure count}. Populated by :func:`record_exception`.
GAPS: dict[str, dict[str, int]] = {"distribution": {}, "binary": {}}


def _module_is_absent(name: str) -> bool:
    """True when `name` cannot be imported in THIS interpreter.

    `find_spec` raises ModuleNotFoundError itself when an ancestor package is
    missing, and ValueError on a few degenerate inputs; both mean absent.
    """
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def classify(exc: BaseException) -> Optional[Tuple[str, str]]:
    """Return ``(kind, name)`` if `exc` is an environment gap, else ``None``.

    Walks the ``__cause__``/``__context__`` chain, because the interesting
    exception is often wrapped (a fixture re-raising, a helper catching and
    re-raising with context).
    """
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        hit = _classify_one(cur)
        if hit is not None:
            return hit
        cur = cur.__cause__ or cur.__context__
    return None


def _classify_one(exc: BaseException) -> Optional[Tuple[str, str]]:
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        top = exc.name.split(".")[0]
        # Only a module that is REALLY not here. A test that scrubs
        # sys.modules to prove a lazy-import path is not a substrate gap.
        if _module_is_absent(top):
            return ("distribution", top)
        return None
    if isinstance(exc, FileNotFoundError):
        name = exc.filename
        # subprocess sets `filename` to the program it could not exec. A bare
        # name (no path separator) is a PATH lookup; anything with a slash is
        # a real file the code expected to exist, which is not our business.
        if isinstance(name, str) and name and "/" not in name and "\\" not in name:
            if shutil.which(name) is None:
                return ("binary", name)
    return None


def record_exception(exc: BaseException) -> None:
    hit = classify(exc)
    if hit is None:
        return
    kind, name = hit
    GAPS[kind][name] = GAPS[kind].get(name, 0) + 1


def total_failures() -> int:
    return sum(n for bucket in GAPS.values() for n in bucket.values())


def any_gap() -> bool:
    return total_failures() > 0


def _names() -> list[str]:
    out: list[str] = []
    for bucket in GAPS.values():
        out.extend(sorted(bucket))
    return sorted(out)


#: `worker/pyproject.toml`, i.e. the worker's OWN declaration of what it needs.
#: Read at report time rather than hand-listed here, so a dependency added
#: tomorrow is covered without touching this file.
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def declared_missing(pyproject: Optional[Path] = None) -> list[str]:
    """Declared worker dependencies that are NOT installed in this interpreter.

    Checked as DISTRIBUTIONS, not modules, so there is no dist-name -> module
    -> import-name table to get wrong (`pyyaml` imports as `yaml`,
    `httpx[socks]` as `httpx`). `importlib.metadata` normalises the name, so
    the declared `apscheduler` matches the installed `APScheduler`.

    Covers `[project].dependencies` and the `dev` extra.

    A gap listed here has not necessarily BITTEN this run — a targeted run may
    simply never have collected the tests that need it. That is the point: it
    is the territory the run could not have reached.
    """
    path = pyproject or PYPROJECT
    try:
        project = tomllib.loads(path.read_text())["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return []
    # Runtime deps AND the `dev` extra: the test substrate's own declaration of
    # what running this suite needs. pytest-asyncio lives in the latter and its
    # absence costs a SKIP, not a failure — exactly the quiet loss class this
    # module exists for.
    declared = list(project.get("dependencies") or [])
    declared += list((project.get("optional-dependencies") or {}).get("dev") or [])
    missing: list[str] = []
    for spec in declared:
        # "httpx[socks]>=0.27" -> "httpx"; "pyyaml>=6" -> "pyyaml"
        name = re.split(r"[\[<>=!~;\s]", spec, maxsplit=1)[0].strip()
        if not name:
            continue
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    return missing


BANNER_TITLE = "SUBSTRATE INCOMPLETE — THIS RUN IS NOT A CERTIFICATION"

#: Printed after pytest's own count line, so the caveat is adjacent to the
#: number a reader is most likely to copy into a report.
TRAILER_PREFIX = "!! NOT A CERTIFICATION"


def banner_lines() -> list[str]:
    """The body of the terminal-summary block (title written by the caller)."""
    n = total_failures()
    lines = [
        f"{n} failure(s) in this run are THE ENVIRONMENT, not the code under test:",
        "",
    ]
    for name, count in sorted(GAPS["distribution"].items()):
        lines.append(
            f"  missing python distribution : {name:<24} -> {count} failure(s)"
        )
    for name, count in sorted(GAPS["binary"].items()):
        lines.append(
            f"  missing executable on PATH  : {name:<24} -> {count} failure(s)"
        )
    also = [d for d in declared_missing() if d not in GAPS["distribution"]]
    if also:
        lines += [
            "",
            "Also declared in worker/pyproject.toml and NOT installed here — no",
            "test in this run reached them, which is territory the run could not",
            "have covered, not territory that is fine:",
            "",
            f"  {', '.join(also)}",
        ]
    lines += [
        "",
        "⚠ THAT COUNT IS A LOWER BOUND, NOT A SPLIT. A failure NOT listed above",
        "is UNATTRIBUTED, not proven real: an incomplete substrate is known to",
        "produce failures that exist nowhere else (T-0824 measured 22 in the api",
        "image; T-1002 measured a 6th in test_monitors.py that reproduces under",
        "neither the worker venv nor apscheduler's absence alone). Reproduce any",
        "of them on a COMPLETE substrate before filing one as a defect.",
        "",
        "Every code path needing one of those went UNMEASURED. The pass count",
        "below counts only what this substrate could reach — it is not a result",
        "for this suite and must not be cited as one.",
        "",
        "Complete substrate for worker/tests: `pip install -e worker[dev]` with",
        "git, rsync and tmux on PATH (on the dev host: worker/.venv/bin/pytest).",
        "The bot-squad-api image is NOT one: it has neither apscheduler nor rsync.",
    ]
    return lines


def trailer_line() -> str:
    return (
        f"{TRAILER_PREFIX} — substrate incomplete: "
        f"{', '.join(_names())} missing, {total_failures()} failure(s) unmeasured. "
        "The count above is not a suite result."
    )
