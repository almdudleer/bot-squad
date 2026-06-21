"""T-0373: cross-process task-md mutation safety (worker side).

The worker (progress_add / close_hook / backoff / recovery / autonomous) and the
API both read-modify-write the SAME backlog task md files from DIFFERENT
processes. With no lock + a shared ``<name>.tmp`` they raced: concurrent writes
were lost and a shared tmp clobber 500'd. This module is the worker half of the
fix; it MUST use the same lockfile convention as ``api/app/markdown_writer.py``
(``<task>.md.lock``) so a worker and the API mutually exclude each other.

Use as::

    with task_lock(path):
        text = path.read_text()
        # ... modify ...
        atomic_write(path, new_text)
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from pathlib import Path

# Same suffix the API (markdown_writer.LOCK_SUFFIX) flocks — cross-process safe.
LOCK_SUFFIX = ".lock"


@contextlib.contextmanager
def task_lock(path: Path):
    """Hold an exclusive cross-process lock for mutating ``path``.

    Wrap the ENTIRE read→modify→write in this. The lockfile ``<path><LOCK_SUFFIX>``
    is created if absent and never deleted (deleting it would reopen the race)."""
    path = Path(path)
    lock_path = path.parent / (path.name + LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a UNIQUE tmp + ``os.replace`` (atomic).

    The per-writer unique tmp (``mkstemp``) replaces the old shared ``<name>.tmp``
    so two concurrent writers never clobber each other's tmp. Call inside
    ``task_lock`` for a full read-modify-write critical section."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
