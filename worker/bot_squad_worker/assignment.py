"""The assignment interface — the spine of the process paradigm (T-0463 / M1-F1.1).

Source of truth: ``vision/INI-XX-process-paradigm-SOURCE-VERBATIM.md`` Part A:

  > "Each session should be provided with an assignment (not necessarily new) on
  >  which it's working. I.e. with something faithfully implementing assignment
  >  interface for this session: a) small prompt on how it's expected to work
  >  with an assignment, b) specific assignment ID, c) means to access its text,
  >  d) means to write the result."
  > "Tasks: Implement the assignment interface"
  > "Routines: Second thing implementing the assignment interface"

Every session is driven by ONE *assignment* exposing four primitives:

  (a) ``how_to_prompt``   — a small prompt on how to work this kind of assignment
  (b) ``assignment_id``   — the stable id of the assignment
  (c) ``read_text()``     — read the assignment's full text
  (d) ``write_result()``  — write the session's result back into an in-system
                            artifact (NOT only the disposable Claude jsonl)

Both **Tasks** and **Routines** implement this one interface. ``TaskAssignment``
conforms today; ``RoutineAssignment`` is a conformance STUB filled by M1-T2.

The write-result primitive (d) is backed by ``Artifact`` — a generic markdown
document at a well-known path. This is deliberately the ONE reusable artifact
seam, not a Task-only hack: the universal autocompact "write-everything-down"
flow (T-0467) and the operator state-doc (T-0473) plug into the SAME ``Artifact``
mechanism — the build plan forbids forking two artifact stores. An assignment
decides *which* artifact is its result sink (its ``result_artifact()``); the
mechanism for writing it is shared.

Worker-only module today (no api mirror) — the write-result action is invoked
through the worker socket like ``task_progress_add``.
"""
from __future__ import annotations

import abc
import contextlib
import fcntl
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# --- how-to prompts (primitive a) -----------------------------------------
# Small, role-agnostic guidance on how a session is expected to work an
# assignment. Kept short on purpose: it orients, it does not replace the
# assignment's own text (read via primitive c).

_TASK_HOW_TO = (
    "You are working a TASK assignment. Read its text (the backlog md) to learn "
    "the verbatim request, context and progress. Do the work, staying targeted "
    "at the stated request and not letting the goal drift as context accumulates. "
    "When you reach a result, WRITE IT BACK via the write-result primitive so the "
    "work-product survives this session — do not leave it only in the chat log."
)

_ROUTINE_HOW_TO = (
    "You are working a ROUTINE assignment — a rule that spawns work on an event "
    "(e.g. a schedule). [M1-T2: routine conformance is a stub today.]"
)


# --- Artifact: the generic, reusable write-result sink --------------------

class Artifact:
    """A markdown document at a well-known path — the one write-result sink.

    Writes are atomic (unique tmp + ``os.replace``) and serialized across
    processes by an advisory ``flock`` on ``<path>.lock`` — the same convention
    as ``mdlock`` so the worker and api never clobber each other. ``read()``
    treats an absent artifact as empty (never raises) so a fresh assignment can
    be written for the first time.

    The autocompact flow (T-0467) and operator state-doc (T-0473) write through
    this same handle; ``write`` is a FULL REPLACE, which is exactly the
    "write everything down" semantics those flows need.
    """

    _LOCK_SUFFIX = ".lock"

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / (self.path.name + self._LOCK_SUFFIX)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(tmp_name, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compose_result_body(assignment_id: str, kind: str, content: str,
                        *, sid: str | None = None, ts: str | None = None) -> str:
    """Wrap a result in a small, traceable frontmatter header + body.

    The header (assignment id / kind / sid / updated) is the seed of the
    role-artifact schema T-0467/T-0473 extend — keep it minimal here.
    """
    ts = ts or _utcnow_iso()
    lines = [
        "---",
        f"assignment: {assignment_id}",
        f"kind: {kind}",
    ]
    if sid:
        lines.append(f"sid: {sid}")
    lines.append(f"updated: {ts}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + content.rstrip("\n") + "\n"


# --- the interface --------------------------------------------------------

class Assignment(abc.ABC):
    """The 4-primitive assignment interface (Part A). Tasks + Routines conform."""

    #: short kind tag ("task" / "routine")
    kind: str = "assignment"

    @property
    @abc.abstractmethod
    def assignment_id(self) -> str:
        """(b) the stable id of this assignment."""

    @property
    @abc.abstractmethod
    def how_to_prompt(self) -> str:
        """(a) a small prompt on how to work this assignment."""

    @abc.abstractmethod
    def read_text(self) -> str:
        """(c) read the assignment's full text."""

    @abc.abstractmethod
    def result_artifact(self) -> Artifact:
        """The Artifact this assignment's result is written into."""

    def write_result(self, content: str, *, sid: str | None = None,
                     ts: str | None = None) -> Artifact:
        """(d) persist the session's result into the assignment's artifact.

        Full-replace into ``result_artifact()`` with a traceable header. Returns
        the Artifact written.
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("write_result: empty content")
        art = self.result_artifact()
        art.write(compose_result_body(self.assignment_id, self.kind, content,
                                      sid=sid, ts=ts))
        return art


# --- Task conforms --------------------------------------------------------

class TaskAssignment(Assignment):
    """A backlog Task as an assignment.

    (a) how_to_prompt = the task how-to; (b) id = ``T-NNNN``;
    (c) read_text = the backlog md; (d) write_result = a sidecar artifact at
    ``data/<slug>/artifacts/<task_id>.md`` (the result is written BACK to the
    assignment, but as a sidecar — the immutable verbatim/context/progress of the
    task body is never overwritten).
    """

    kind = "task"

    def __init__(self, data_dir: Path | str, slug: str, task_id: str):
        self._data_dir = Path(data_dir)
        self._slug = slug
        self._task_id = task_id

    @property
    def assignment_id(self) -> str:
        return self._task_id

    @property
    def how_to_prompt(self) -> str:
        return _TASK_HOW_TO

    def _backlog_md(self) -> Path:
        backlog = self._data_dir / self._slug / "backlog"
        matches = sorted(backlog.glob(f"{self._task_id}-*.md"))
        if not matches:
            raise FileNotFoundError(
                f"assignment: task not found: {self._task_id} (slug {self._slug})"
            )
        return matches[0]

    def read_text(self) -> str:
        return self._backlog_md().read_text(encoding="utf-8")

    def result_artifact(self) -> Artifact:
        return Artifact(self._data_dir / self._slug / "artifacts" / f"{self._task_id}.md")


# --- Routine conformance STUB (filled by M1-T2) ---------------------------

class RoutineAssignment(Assignment):
    """A Routine as an assignment — the SECOND thing implementing the interface.

    STUB: the conformance slot is reserved here so the interface has two
    implementers per Part A, but the routine seam (event/schedule rules that
    spawn sessions) lands in M1-T2. read_text / result_artifact raise
    ``NotImplementedError`` until then.
    """

    kind = "routine"

    def __init__(self, routine_id: str):
        self._routine_id = routine_id

    @property
    def assignment_id(self) -> str:
        return self._routine_id

    @property
    def how_to_prompt(self) -> str:
        return _ROUTINE_HOW_TO

    def read_text(self) -> str:
        raise NotImplementedError(
            "RoutineAssignment.read_text is a conformance stub — implemented by M1-T2"
        )

    def result_artifact(self) -> Artifact:
        raise NotImplementedError(
            "RoutineAssignment.result_artifact is a conformance stub — implemented by M1-T2"
        )


# --- factory --------------------------------------------------------------

def for_task(data_dir: Path | str, slug: str, task_id: str) -> TaskAssignment:
    """Build the assignment for a backlog task."""
    return TaskAssignment(data_dir, slug, task_id)
