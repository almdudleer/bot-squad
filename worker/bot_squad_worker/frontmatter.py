"""Shared YAML-frontmatter parser/writer for bot-squad task + session md files.

SINGLE SOURCE OF TRUTH (T-0075). This module is byte-identical mirrored at
``worker/bot_squad_worker/frontmatter.py`` and ``api/app/frontmatter.py`` —
edit BOTH copies together (same convention as ``idalloc.py``). It imports only
the stdlib + ``yaml`` so the two copies can stay identical.

WHY THIS EXISTS
---------------
Before T-0075 the codebase had two incompatible write paths and six line-based
readers for the SAME md frontmatter. The API wrote list-valued fields
(``blocked_by`` / ``session_history`` / ``related_docs``) in block style via
``yaml.safe_dump``::

    blocked_by:
    - T-0001
    - T-0002

while every worker/api reader split on ``\\n`` and partitioned on ``:`` — so a
block-style list parsed as ``{"blocked_by": "", "- T-0001": None, ...}`` and
the list contents were silently dropped. The moment a user PATCHed a task's
``blocked_by`` through the UI, every worker-side reader saw the field empty.

ONE pair, used everywhere, kills the drift.

DESIGN CONSTRAINTS
------------------
- ``parse`` uses pyyaml, so BOTH block-style and inline ``[a, b]`` lists read
  identically and legacy inline mds stay readable.
- ``dump`` is backward-compatible with the ONE remaining line-based holdout
  (``scripts/hooks/session_start.sh``, which parses inline-only session fields
  in shell+python):
    * lists serialize INLINE/flow (``key: [a, b]``) so the hook's
      ``extra_task_ids: [...]`` regex keeps matching;
    * ``None`` serializes as ``~`` (the legacy session-md convention), not
      ``null``;
    * ISO timestamps stay PLAIN strings in both directions — the loader does
      not auto-resolve them to ``datetime`` (callers string-compare and
      ``datetime.fromisoformat`` ``started_at``), and the dumper does not
      defensively quote them.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)", re.DOTALL)


class FrontmatterError(Exception):
    """Raised when a string has no parseable ``---`` frontmatter mapping."""


class AmbiguousIdError(Exception):
    """Raised by ``resolve_id_file`` (strict mode) when an id's filename glob
    matches 2+ files and 0 or 2+ of them declare that id in their own
    frontmatter — a genuine, unresolved id collision (T-0231)."""


def _loader_base() -> type:
    """CSafeLoader (libyaml) is a drop-in perf swap for SafeLoader — same
    safe-tag surface, same Resolver mixin the timestamp-strip below relies
    on — but parses in C instead of pure Python.

    T-0668: ``binding_gc_tick`` re-globs + re-parses ~1,450 session/task mds
    across its 13 passes every 60s, all in the ONE worker process that also
    serves the fan-out HTTP handler; pure-Python ``SafeLoader`` parsing that
    volume held the GIL long enough to starve the HTTP-serving threads,
    surfacing as the 5s fan-out timeout. Falls back to SafeLoader if libyaml
    isn't available in this environment.
    """
    return yaml.CSafeLoader if getattr(yaml, "__with_libyaml__", False) else yaml.SafeLoader


# Loader/Dumper subclasses with the implicit *timestamp* resolver removed, so
# ISO date strings round-trip as plain ``str`` in BOTH directions (read: no
# datetime objects; write: no defensive quoting). Everything else keeps stock
# SafeLoader/SafeDumper semantics.
class _Loader(_loader_base()):
    pass


class _Dumper(yaml.SafeDumper):
    pass


def _strip_timestamp_resolver(cls: type) -> None:
    # T-0668: rebind a FRESH dict onto ``cls`` rather than mutating
    # ``cls.yaml_implicit_resolvers`` in place. Before a subclass sets its own
    # entry, that attribute resolves (via the MRO) to the SAME dict object
    # shared by yaml.SafeLoader/CSafeLoader/SafeDumper — an in-place
    # ``cls.yaml_implicit_resolvers[ch] = ...`` mutates that shared object,
    # silently stripping timestamp auto-resolution from every OTHER consumer
    # of those stock loader/dumper classes in the process (caught when the
    # CSafeLoader swap below made this same latent bug corrupt CSafeLoader
    # too — global side effect, not scoped to _Loader/_Dumper).
    cls.yaml_implicit_resolvers = {
        ch: [
            (tag, regexp)
            for tag, regexp in mappings
            if tag != "tag:yaml.org,2002:timestamp"
        ]
        for ch, mappings in cls.yaml_implicit_resolvers.items()
    }


_strip_timestamp_resolver(_Loader)
_strip_timestamp_resolver(_Dumper)


def _represent_none(dumper: yaml.Dumper, _data: Any) -> Any:
    # Emit ``~`` for None — the legacy session-md convention the line-based
    # hook reader recognizes — rather than ``null`` / empty.
    return dumper.represent_scalar("tag:yaml.org,2002:null", "~")


def _represent_list(dumper: yaml.Dumper, data: list) -> Any:
    # Inline/flow style (``key: [a, b]``) so the line-based hook keeps matching
    # and block-style legacy lists are healed to inline on the next write.
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


_Dumper.add_representer(type(None), _represent_none)
_Dumper.add_representer(list, _represent_list)


def _coerce_scalar(v: str) -> Any:
    """Type a single frontmatter value the way the strict loader would.

    Re-runs the timestamp-stripped loader on just this scalar so ``true`` →
    bool, ``[a, b]`` → list, ``~`` → None, ints → int, timestamps → str — i.e.
    identical typing to the strict path. If pyyaml rejects this particular
    scalar (e.g. a bare ``%9``), the raw string is kept. This keeps the
    fallback's per-value semantics equal to the strict parse so a legacy file
    round-trips byte-for-byte once rewritten.

    A frontmatter field value is a scalar or a list — never a mapping. The
    fallback partitions on the FIRST ``:`` per line, so a value that itself
    contains ``: `` (e.g. a legacy unquoted ``title: Recheck model switch:
    budget caps``) leaves an inner ``key: value`` that pyyaml would re-read into
    a ``dict``. That is a mis-parse, not a real mapping — keep the raw string so
    the title stays a string. (T-0206: a dict title was served raw to the SPA
    and crashed it with React error #31, object-as-child.)
    """
    try:
        loaded = yaml.load(v, Loader=_Loader)
    except yaml.YAMLError:
        return v
    if isinstance(loaded, dict):
        return v
    return loaded


def _line_based_parse(block: str) -> dict:
    """Tolerant line reader for legacy frontmatter that isn't strict YAML.

    Splits on the first ``:`` per line (so a value like an unquoted tmux
    ``pane_id: %9`` — ``%`` is a reserved YAML indicator — no longer aborts the
    whole parse) and types each value via ``_coerce_scalar``. Used ONLY as a
    fallback when pyyaml rejects the block. Block-style YAML lists only ever
    come from valid-YAML writers (the api), so they never reach this fallback
    and the T-0075 drift fix is preserved.
    """
    meta: dict = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = _coerce_scalar(v.strip())
    return meta


def parse(text: str) -> tuple[dict, str]:
    """Split frontmatter md ``text`` into ``(meta, body)``.

    ``meta`` is the parsed frontmatter mapping (YAML types preserved, except
    ISO timestamps which stay plain strings). ``body`` is everything after the
    closing ``---``, with leading newlines stripped.

    Strict pyyaml is tried first (so block- and inline-style lists read
    identically). If the block isn't strict YAML — a legacy hand-rolled md with
    e.g. an unquoted ``pane_id: %9`` — it falls back to the tolerant line
    reader instead of failing, so no file the old worker could read becomes
    unreadable.

    Raises ``FrontmatterError`` only when there is no ``---`` frontmatter block
    at all.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise FrontmatterError("no YAML frontmatter")
    block = m.group(1)
    body = m.group(2).lstrip("\n")
    try:
        meta = yaml.load(block, Loader=_Loader)
    except yaml.YAMLError:
        return _line_based_parse(block), body
    if meta is None:
        return {}, body
    if not isinstance(meta, dict):
        # Valid YAML but not a mapping (pathological frontmatter) — try the
        # line reader before giving up.
        return _line_based_parse(block), body
    return meta, body


def parse_or_none(text: str) -> tuple[dict, str] | None:
    """Like ``parse`` but returns ``None`` instead of raising — for the worker
    readers that historically treated a missing/blank frontmatter as ``None``."""
    try:
        return parse(text)
    except FrontmatterError:
        return None


def dump_frontmatter(meta: dict) -> str:
    """Serialize a frontmatter mapping to its YAML block (no ``---`` fences).

    Key order preserved; lists inline; ``None`` as ``~``; timestamps unquoted.
    """
    return yaml.dump(
        meta,
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def dump(meta: dict, body: str = "") -> str:
    """Serialize ``(meta, body)`` to a complete frontmatter md string.

    Produces ``---\\n<yaml>---\\n\\n<body>`` — the canonical task-md shape.
    """
    return f"---\n{dump_frontmatter(meta)}---\n\n{body}"


def as_list(value: Any) -> list[str]:
    """Normalize a list-valued frontmatter field to a ``list[str]``.

    ``parse`` already returns real lists for YAML sequences, but this also
    tolerates a bare scalar, ``None``/``~``, or a legacy inline ``[a, b]``
    string so every list-field reader (``blocked_by`` / ``extra_task_ids`` /
    ``session_history`` / ``related_docs``) can call ONE normalizer instead of
    re-implementing bracket-splitting. ``~`` entries are dropped.
    """
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(x).strip() for x in value]
        return [x for x in items if x and x != "~"]
    s = str(value).strip()
    if not s or s == "~":
        return []
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
        if not s:
            return []
        return [x.strip() for x in s.split(",") if x.strip() and x.strip() != "~"]
    return [s]


def resolve_id_file(dir_path: Path, entity_id: str, *, strict: bool = False) -> Path | None:
    """Find ``dir_path/<entity_id>-*.md``, disambiguating by ``id:`` frontmatter (T-0231).

    Two real incidents (T-0030, T-0222) happened because every call site that
    resolved an id to a file did ``sorted(dir.glob(f"{id}-*.md"))[0]`` — the
    alphabetically-FIRST filename match, chosen without ever reading the
    file's own ``id:`` frontmatter. When a genuine id collision existed (two
    files whose filenames both start with the same id), or a tombstone was
    left behind after a renumber, this silently resolved to whichever name
    sorted first — sometimes the wrong ticket.

    Behavior:
    - 0 matches -> ``None``.
    - 1 match -> that file (no frontmatter read needed).
    - 2+ matches -> re-read each candidate's ``id:`` field; if exactly ONE
      declares ``id: <entity_id>`` exactly, return it (this is what makes the
      tombstone convention work: a renumbered ticket's stub is stamped with a
      non-matching id, e.g. ``T-0030-DUPLICATE-DO-NOT-USE``, specifically so
      it's skipped here regardless of alphabetical sort order).
    - 2+ matches, 0 or 2+ of which declare a matching id (a genuine unresolved
      collision) -> if ``strict``, raises :class:`AmbiguousIdError`; otherwise
      falls back to the alphabetically-first match (the old behavior), so a
      read-mostly/best-effort caller (e.g. a background nag) degrades instead
      of crashing.
    """
    matches = sorted(Path(dir_path).glob(f"{entity_id}-*.md"))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    exact = []
    for p in matches:
        parsed = parse_or_none(p.read_text(errors="replace"))
        if parsed and str(parsed[0].get("id", "")).strip() == entity_id:
            exact.append(p)
    if len(exact) == 1:
        return exact[0]
    if strict:
        raise AmbiguousIdError(
            f"id {entity_id!r} matches {len(matches)} files under {dir_path}, "
            f"and {len(exact)} of them declare that id in frontmatter "
            f"(need exactly 1): {[str(p) for p in matches]}"
        )
    return matches[0]
