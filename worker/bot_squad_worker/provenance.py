"""T-0519: worker-side mirror of the canonical provenance grammar.

This is a BYTE-IDENTICAL mirror of the grammar in
``scripts/lint/backlog_provenance.py`` (Cluster C / T-0506) — the worker
package cannot import across the ``scripts/`` boundary cleanly, so we replicate
it per the ``idalloc.py`` precedent. ``worker/tests/test_provenance_gate.py``
asserts the two stay in sync (no drift).

A provenance value is a comma-separated list of tokens, each matching one of:
``corpus:<lc-token>`` | ``F-<digits>`` | ``T-<4+digits>`` | ``stakeholder:YYYY-MM-DD``.
"""
from __future__ import annotations

import re

# One token's allowed shapes. A value may be a comma-separated list of these.
_TOKEN_RE = re.compile(
    r"\A(?:"
    r"corpus:[a-z0-9][a-z0-9-]*"
    r"|F-\d+"
    r"|T-\d{4,}"  # T-0371: ids cross 9999
    r"|stakeholder:\d{4}-\d{2}-\d{2}"
    r")\Z"
)

# Human-facing allowed-form summary — kept identical to the lint/CLI wording so
# the three gate surfaces give the same error.
ALLOWED_HELP = "corpus:<token> | F-NNNN | T-NNNN | stakeholder:YYYY-MM-DD"


def provenance_valid(value: str) -> bool:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return bool(parts) and all(_TOKEN_RE.match(p) for p in parts)
