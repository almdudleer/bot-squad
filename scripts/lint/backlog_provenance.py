#!/usr/bin/env python3
"""T-0301: lint that every NEW backlog ticket declares a ``provenance:`` source.

Stakeholder directive (2026-06-20): "make sure everything you do attaches to a
piece of guidance I gave before OR to user feedback." Every ticket created on or
after the cutoff must carry a ``provenance:`` frontmatter field pointing at a
guidance-corpus section, a user-feedback id, an originating ticket, or a dated
stakeholder directive. See ``data/<slug>/docs/roadmap/provenance.md``.

Allowed ``provenance:`` value forms (comma-separated list allowed):
    corpus:<token>          a guidance-corpus section (guidance-corpus.md legend)
    F-NNNN                   a curated user-feedback id
    T-NNNN                   an originating ticket carrying the stakeholder verbatim
    stakeholder:YYYY-MM-DD   a direct, dated stakeholder directive

Grandfathering: forward-only. Only tickets whose ``created:`` is on/after the
cutoff (default 2026-06-21, override ``BOT_SQUAD_PROVENANCE_CUTOFF=YYYY-MM-DD``)
are required to carry provenance; the pre-cutoff backlog is exempt. A ticket with
no parseable ``created:`` is skipped.

Usage:
    scripts/lint/backlog_provenance.py [DATA_DIR ...]

Defaults to ``$BOT_SQUAD_DATA_DIR`` or ``/home/www/bot-squad/data``. Exits 0 when
every in-scope ticket has a valid provenance; exits 1 otherwise, printing one
``path:reason`` line per offender to stderr.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_CUTOFF = "2026-06-21"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_CREATED_RE = re.compile(r"^created:\s*(.+?)\s*$", re.MULTILINE)
_PROVENANCE_RE = re.compile(r"^provenance:\s*(.+?)\s*$", re.MULTILINE)

# One token's allowed shapes. A value may be a comma-separated list of these.
_TOKEN_RE = re.compile(
    r"\A(?:"
    r"corpus:[a-z0-9][a-z0-9-]*"
    r"|F-\d+"
    r"|T-\d{4}"
    r"|stakeholder:\d{4}-\d{2}-\d{2}"
    r")\Z"
)


def _unquote(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val.strip()


def _frontmatter(text: str) -> str | None:
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def _field(block: str, regex: re.Pattern[str]) -> str | None:
    m = regex.search(block)
    return _unquote(m.group(1)) if m else None


def _created_date(block: str) -> str | None:
    """Return the YYYY-MM-DD prefix of the created field, or None if unparseable."""
    raw = _field(block, _CREATED_RE)
    if not raw:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else None


def _provenance_valid(value: str) -> bool:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return bool(parts) and all(_TOKEN_RE.match(p) for p in parts)


def lint_dir(data_dir: Path, cutoff: str) -> list[tuple[Path, str]]:
    offenders: list[tuple[Path, str]] = []
    if not data_dir.exists():
        return offenders
    for backlog in sorted(data_dir.glob("*/backlog")):
        if not backlog.is_dir():
            continue
        for md in sorted(backlog.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            block = _frontmatter(text)
            if block is None:
                continue
            created = _created_date(block)
            if created is None or created < cutoff:
                # Pre-cutoff or undatable → grandfathered / skipped.
                continue
            prov = _field(block, _PROVENANCE_RE)
            if prov is None:
                offenders.append((md, "missing provenance"))
            elif not _provenance_valid(prov):
                offenders.append((md, f"invalid provenance={prov!r}"))
    return offenders


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dirs", nargs="*")
    args = parser.parse_args(argv)

    cutoff = os.environ.get("BOT_SQUAD_PROVENANCE_CUTOFF", DEFAULT_CUTOFF)

    if args.data_dirs:
        roots = [Path(p) for p in args.data_dirs]
    else:
        default = os.environ.get("BOT_SQUAD_DATA_DIR", "/home/www/bot-squad/data")
        roots = [Path(default)]

    offenders: list[tuple[Path, str]] = []
    for root in roots:
        offenders.extend(lint_dir(root, cutoff))

    if offenders:
        print(
            f"backlog provenance lint: {len(offenders)} offender(s); every ticket "
            f"created >= {cutoff} needs provenance: corpus:<token> | F-NNNN | T-NNNN "
            f"| stakeholder:YYYY-MM-DD (see docs/roadmap/provenance.md)",
            file=sys.stderr,
        )
        for path, reason in offenders:
            print(f"{path}:{reason}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
