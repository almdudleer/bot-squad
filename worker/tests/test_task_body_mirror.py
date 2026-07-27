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
"""
from __future__ import annotations

from pathlib import Path

import pytest

_MARKER = "from __future__ import annotations\n"

_REPO = Path(__file__).resolve().parents[2]
_WORKER_SRC = _REPO / "worker" / "bot_squad_worker" / "task_body.py"
_API_SRC = _REPO / "api" / "app" / "task_body.py"


def _code(path: Path) -> str:
    """File contents with the leading module docstring dropped."""
    text = path.read_text()
    assert _MARKER in text, f"{path} lost its `{_MARKER.strip()}` line"
    return text.split(_MARKER, 1)[1]


def test_worker_and_api_task_body_are_identical():
    if not _API_SRC.exists():  # worker installed standalone, no api tree
        pytest.skip(f"api copy not present at {_API_SRC}")
    worker_code = _code(_WORKER_SRC)
    api_code = _code(_API_SRC)
    assert worker_code == api_code, (
        "task_body.py has diverged between worker and api — a fix landed in "
        "only one copy. Mirror it into the other (module docstrings may differ)."
    )
