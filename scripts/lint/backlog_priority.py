#!/usr/bin/env python3
"""T-0877: lint that a NEW backlog ticket's ``priority:`` is one the queue can rank.

Why (measured as watchrobot T-0586, 2026-08-11)
-----------------------------------------------
``priority`` had two writers and one reader, and none of them shared a
vocabulary. ``bsq task new --priority`` stored any string verbatim; the pickup
ranking understood only ``p1``/``p2``/``p3``. ``--priority high`` therefore
succeeded and dropped the ticket out of the queue for good: 86 tickets on one
board — its ENTIRE needs-triage band was there for the format of this one field
— the oldest invisible for 20.6 days.

T-0877 gave both tool writers the same table (``scripts/cli/priority.py`` /
``worker/bot_squad_worker/priority.py``, byte-identical, pinned by the
module-mirror gate). This lint covers the THIRD writer, which no gate can reach:
**a session hand-editing frontmatter.** That is the path by which
``priority: срочно`` could still arrive, and the reason it must go red here is
that its cost is silence — the ticket looks fine and simply stops being picked.

What it flags, and what it deliberately does not
------------------------------------------------
* **Flagged:** a value in no vocabulary (``срочно``, ``urgent``, ``hotfix``,
  ``P12``). The queue cannot rank it, so the ticket is invisible to dispatch.
* **NOT flagged — a multi-digit integer.** That is the web UI's kanban ORDERING
  KEY (``api/app/routes_backlog.py``: max+100, midpoint inserts), a legitimate
  write by a legitimate writer on a different scale. It is reported honestly in
  the triage band as ``priority-ordering-key``; failing a commit over it would
  red every web-created ticket.
* **NOT flagged — a missing priority.** There the field is genuinely empty and
  the triage band is the honest answer (T-0586 DoD item 5 keeps that a separate
  question).

Grandfathering: forward-only, exactly like ``backlog_provenance.py``. Only
tickets whose ``created:`` is on/after the cutoff (default 2026-08-11, the day
the reader learned the word vocabulary; override
``BOT_SQUAD_PRIORITY_CUTOFF=YYYY-MM-DD``) are in scope. **The pre-cutoff board is
exempt on purpose** — those tickets carry other people's judgement in other
people's words, the reader now understands them, and rewriting somebody else's
priority field to tidy a listing is forbidden by the operator's standing rule.
A ticket with no parseable ``created:`` is skipped.

Usage:
    scripts/lint/backlog_priority.py [DATA_DIR ...]

Defaults to ``$BOT_SQUAD_DATA_DIR`` or ``/home/www/bot-squad/data``. Exits 0 when
every in-scope ticket carries a rankable priority; exits 1 otherwise, printing
one ``path:reason`` line per offender to stderr.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

DEFAULT_CUTOFF = "2026-08-11"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_CREATED_RE = re.compile(r"^created:\s*(.+?)\s*$", re.MULTILINE)
_PRIORITY_RE = re.compile(r"^priority:\s*(.+?)\s*$", re.MULTILINE)


def _load_vocabulary():
    """The ONE priority vocabulary, loaded from ``scripts/cli/priority.py``.

    By path, not by copy: a lint with its own table would be the fourth
    vocabulary in a system whose whole defect was that two of them disagreed.
    (Stdlib only — this runs on a bare ``python3`` in pre-commit and CI.)
    """
    path = Path(__file__).resolve().parents[1] / "cli" / "priority.py"
    loader = SourceFileLoader("_lint_priority", str(path))
    spec = importlib.util.spec_from_loader("_lint_priority", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _unquote(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val.strip()


def _field(block: str, regex: re.Pattern[str]) -> str | None:
    m = regex.search(block)
    return _unquote(m.group(1)) if m else None


def _created_date(block: str) -> str | None:
    raw = _field(block, _CREATED_RE)
    if not raw:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else None


def lint_dir(data_dir: Path, cutoff: str, vocab=None) -> list[tuple[Path, str]]:
    vocab = vocab or _load_vocabulary()
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
            m = _FRONTMATTER_RE.match(text)
            if m is None:
                continue
            block = m.group(1)
            created = _created_date(block)
            if created is None or created < cutoff:
                continue
            raw = _field(block, _PRIORITY_RE)
            if raw is None or not raw:
                continue  # missing is a triage question, not a lint offence
            _band, flag = vocab.parse_priority(raw)
            if flag and flag.startswith(vocab.UNPARSEABLE_FLAG):
                offenders.append((md, f"unrankable priority={raw!r}"))
    return offenders


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dirs", nargs="*")
    args = parser.parse_args(argv)

    cutoff = os.environ.get("BOT_SQUAD_PRIORITY_CUTOFF", DEFAULT_CUTOFF)
    vocab = _load_vocabulary()

    if args.data_dirs:
        roots = [Path(p) for p in args.data_dirs]
    else:
        default = os.environ.get("BOT_SQUAD_DATA_DIR", "/home/www/bot-squad/data")
        roots = [Path(default)]

    offenders: list[tuple[Path, str]] = []
    for root in roots:
        offenders.extend(lint_dir(root, cutoff, vocab))

    if offenders:
        print(
            f"backlog priority lint: {len(offenders)} offender(s); a ticket created "
            f">= {cutoff} must carry a priority the queue can RANK — {vocab.ALLOWED_HELP} "
            "— or it silently drops out of `bsq pickup` (T-0877/T-0586)",
            file=sys.stderr,
        )
        for path, reason in offenders:
            print(f"{path}:{reason}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
