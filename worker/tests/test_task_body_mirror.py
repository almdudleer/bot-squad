"""`worker/.../task_body.py` and `api/app/task_body.py` must not diverge.

`task_body.py` exists twice — duplicated by design so worker and api stay
independent packages (see either module's docstring). The failure mode of that
design is a fix landing in ONE copy: T-0714 shipped broken exactly that way,
and T-0729 (verbatim absorbing agent-authored `## DoD` sections) had to be
fixed in both. This pins them byte-identical from the
`from __future__ import annotations` line onward — everything except each
copy's own module docstring.

If you changed one on purpose, copy it to the other; the docstring is the only
place the two are allowed to differ.

T-0738: this module stays the SSOT of the invariant, and it is now also the
ENGINE of the automatic gate. T-0733 shipped the divergence in 5fe547f with CI
green — this test was correct and silent because nothing ran it, and it stayed
silent until a session ran the full worker suite by hand hours later. So the
comparison lives exactly once, in `mirror_failure()` below, and
`scripts/lint/task_body_mirror.py` imports and calls THAT from CI and the
pre-push hook: the gate and the test can never disagree about the invariant
because there is only one of it.

Keep `mirror_failure()` importable with NOTHING but the stdlib — the lint
script runs on bare `python3`, no venv, no pytest, no api tree. That is why
`import pytest` lives inside the test function instead of at module scope, and
why the marker check raises instead of asserting (asserts vanish under -O).
"""
from __future__ import annotations

from pathlib import Path

_MARKER = "from __future__ import annotations\n"

_REPO = Path(__file__).resolve().parents[2]
_WORKER_SRC = _REPO / "worker" / "bot_squad_worker" / "task_body.py"
_API_SRC = _REPO / "api" / "app" / "task_body.py"


class MirrorMarkerError(Exception):
    """A copy lost the `from __future__ import annotations` split marker."""


def _code(path: Path) -> str:
    """File contents with the leading module docstring dropped."""
    text = path.read_text()
    if _MARKER not in text:
        raise MirrorMarkerError(f"{path} lost its `{_MARKER.strip()}` line")
    return text.split(_MARKER, 1)[1]


def api_copy_present() -> bool:
    """False when the worker is installed standalone, with no api tree beside it."""
    return _API_SRC.exists()


def mirror_failure() -> str | None:
    """The one byte comparison. `None` when the copies agree, else why they don't.

    Called by `test_worker_and_api_task_body_are_identical` AND by
    `scripts/lint/task_body_mirror.py` (the CI / pre-push gate).
    """
    worker_code = _code(_WORKER_SRC)
    api_code = _code(_API_SRC)
    if worker_code == api_code:
        return None
    return (
        "task_body.py has diverged between worker and api — a fix landed in "
        "only one copy. Mirror it into the other (module docstrings may differ)."
        f"\n  worker: {_WORKER_SRC}"
        f"\n  api:    {_API_SRC}"
    )


def test_worker_and_api_task_body_are_identical():
    import pytest

    if not api_copy_present():  # worker installed standalone, no api tree
        pytest.skip(f"api copy not present at {_API_SRC}")
    failure = mirror_failure()
    assert failure is None, failure
