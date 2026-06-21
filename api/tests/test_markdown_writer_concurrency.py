"""T-0373: concurrent task-md mutations must not lose writes.

The shared-tmp + unlocked read-modify-write pattern lost updates (and 500'd) when
multiple writers hit the same task md — QA fired 8 concurrent comment-adds and
only 2 survived. A cross-process flock + a unique per-writer tmp must make every
write survive.
"""
from __future__ import annotations

import threading
from pathlib import Path

from app.markdown_writer import append_comment, merge_task_update, write_task


def _seed(tmp_path: Path) -> Path:
    p = tmp_path / "T-0001-x.md"
    write_task(p, {"id": "T-0001", "title": "x", "status": "open"}, "Body.\n\n## Comments\n")
    return p


def test_concurrent_append_comment_no_lost_updates(tmp_path: Path):
    p = _seed(tmp_path)
    n = 12
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        barrier.wait()  # maximize contention
        try:
            append_comment(p, f"unique-comment-{i}", f"author{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"writers raised: {errors!r}"
    text = p.read_text()
    missing = [i for i in range(n) if f"unique-comment-{i}" not in text]
    assert not missing, f"LOST comments: {missing}"


def test_concurrent_merge_update_no_corruption(tmp_path: Path):
    """Concurrent frontmatter updates each land + the file stays parseable."""
    p = _seed(tmp_path)
    n = 10
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            merge_task_update(p, {"title": f"title-{i}"})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"writers raised: {errors!r}"
    # the file is intact + parseable (exactly one frontmatter block survived,
    # no clobbered/half-written content) and a real concurrent write landed.
    from app.markdown_parser import parse_task
    task = parse_task(p)
    assert task["id"] == "T-0001"
    assert task["title"].startswith("title-")          # a concurrent update won
    assert p.read_text().count("\n---\n") <= 1          # not double-framed/corrupt
