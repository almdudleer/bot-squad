#!/usr/bin/env python3
"""Claude Code PreToolUse hook: refuse a raw `rm`/`mv`-away of the shared CLI
launcher files (T-1068).

WHY THIS EXISTS
----------------
`scripts/cli/bsq` and `scripts/cli/bsq-launch` are tracked at mode 100755 and
were observed dropping to 664 on this box twice in one hour (2026-09-07,
10:42Z and 10:52Z) with ZERO commits touching the file in that window — so
nothing COMMITTED did it. `git status`/`git diff` never showed a mode-only
change either, which the reconciliation logic already rules out as the cause.

The investigation for T-1068 reproduced the tools every live session actually
uses to save a file — Claude Code's Edit and Write tool calls — against both a
small and a ~1MB file, at both 755 and 775, including the adversarial case of
an external mode change landing BETWEEN a Read and the next Edit with no
re-read in between. Every one of those runs preserved the file's CURRENT
on-disk mode. That behaviour is "preserve whatever mode is already there", not
"restore the committed mode" — which means it is not itself capable of
producing a mode of 664, and once something else drops it, ordinary edits keep
carrying the bad mode forward rather than fixing it. Nothing in this repo's own
code writes either file's content either (grepped for writers; both are pure
CLI sources sessions edit directly).

664 (rw-rw-r--, no exec bits at all) is exactly what a BRAND NEW regular file
gets under this box's umask (0666 & ~002) — not what "preserve current mode"
would ever produce from an existing 775 file. So the drop requires the path to
stop existing and then be recreated by something that does not read the
original file's permissions at all — e.g. an ad hoc `rm` + recreate, or a `mv`
of the tracked file to a scratch name followed by writing a fresh one in its
place. Neither leaves a code artifact to patch; the guard below closes the
precondition instead: if the file is never deleted, nothing ever needs to be
"recreated" at a default mode, and the sanctioned edit path (proven above)
keeps working correctly.

SCOPE, DELIBERATELY NARROW
---------------------------
Two exact basenames, no wildcard, no "any executable script" generalisation:
`bsq` and `bsq-launch` under a `cli` directory. This is the file two outages
already named; widening it invites the same false-positive risk the sibling
git guard's docstring warns about (a broken/over-eager guard trains everyone
to route around it or disable it). `rm scripts/cli/bsq` and `mv scripts/cli/bsq
<anywhere>` (the file as SOURCE) are refused; `mv <anywhere> scripts/cli/bsq`
(the file as DESTINATION) is NOT — that shape is the correct temp-then-rename
recipe done by hand and must keep working.

CONTRACT: stdin is the PreToolUse JSON. Exit 0 allows; exit 2 blocks and the
stderr text is fed back to the agent as the refusal reason. Any internal error
in THIS file exits 0 (fail OPEN) — a bug here must not block unrelated Bash
calls fleet-wide, mirroring `bsq-pretooluse-git.py`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Reuse the already-tested quote/heredoc-aware scanning helpers from the git
# guard rather than re-deriving them — this hook fires on the SAME "every Bash
# call, every session" surface, and re-inventing "is this position inside a
# quoted string or a heredoc body" badly is exactly how a note that merely
# QUOTES an `rm scripts/cli/bsq` incident (like this docstring almost did)
# gets refused as if it were the command itself.
_spec = importlib.util.spec_from_file_location(
    "_bsq_pretooluse_git", os.path.join(_HERE, "bsq-pretooluse-git.py"))
_git_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_git_guard)

PROTECTED_BASENAMES = ("bsq", "bsq-launch")
PROTECTED_PARENT = "cli"

# `rm`/`mv` in command position: same delimiter set the git guard uses (line
# start, or after a shell operator), tolerant of an env-assignment prefix and
# `sudo`. Redirections/pipes end the argument list.
VERB_RE = re.compile(
    r"""(?:^|[;&|(\n{]|&&|\|\|)\s*
        (?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;&|]*)\s+)*)
        (?:sudo\s+)?
        (?P<verb>rm|mv)\b
        (?P<args>[^;&|\n]*)""",
    re.VERBOSE,
)


def _is_protected(token: str) -> bool:
    """Does this argument NAME one of the two protected files (by path shape,
    not by a bare word match — a `bsq` appearing as, say, a commit message
    word is not a path and must not trip this)."""
    token = re.split(r"[<>]", token, maxsplit=1)[0]  # strip trailing redirection noise
    if "/" not in token and token not in PROTECTED_BASENAMES:
        return False
    base = os.path.basename(token.rstrip("/"))
    if base not in PROTECTED_BASENAMES:
        return False
    parent = os.path.basename(os.path.dirname(token.rstrip("/")))
    # A bare basename with no directory component at all (just `bsq`) is
    # ambiguous — could be anything named bsq in cwd. Require the `cli/`
    # parent when a directory component is present; a bare basename alone
    # (no "/") already returned True above only via the explicit-name branch,
    # which is intentionally permissive because `rm bsq` run FROM
    # scripts/cli IS the hazard.
    return parent == PROTECTED_PARENT or "/" not in token


def _tokens(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError:
        # Unbalanced quote — shlex cannot tokenize it, so we cannot see
        # arguments reliably either. Fail open: an unparsable rm/mv is not
        # evidence it targets a protected file.
        return []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not any(v in command for v in ("rm", "mv")):
        return 0

    try:
        skip = _git_guard.heredoc_spans(command)
        hits = [
            m for m in VERB_RE.finditer(command)
            if not _git_guard.inside_quotes(command, m.start())
            and not _git_guard.inside_spans(skip, m.start("verb"))
        ]
    except Exception:
        return 0

    reasons = []
    for m in hits:
        verb = m.group("verb")
        tokens = [t for t in _tokens(m.group("args")) if not t.startswith("-")]
        if not tokens:
            continue
        if verb == "rm":
            hit = next((t for t in tokens if _is_protected(t)), None)
        else:  # mv: only the SOURCE (first operand) leaving its home is the hazard
            hit = tokens[0] if _is_protected(tokens[0]) else None
        if hit is None:
            continue
        reasons.append(
            f"bsq-pretooluse-protect-exec: REFUSED — `{verb} {' '.join(tokens)}` "
            f"removes {hit} from the tree.\n"
            f"     That file is tracked at mode 100755 and is edited live by the\n"
            f"     whole fleet; deleting it means whatever recreates it next\n"
            f"     starts from NOTHING and gets this box's default new-file mode\n"
            f"     (664, no exec bits) instead of preserving 775 — the exact T-1068\n"
            f"     incident (10:42Z and 10:52Z, 2026-09-07). Edit it in place with\n"
            f"     the Edit/Write tool instead — that path was verified (T-1068) to\n"
            f"     preserve whatever mode the file currently has.\n"
            f"     Genuinely need to replace it wholesale? Write the new content to\n"
            f"     a temp file, `chmod 755` THAT, then `mv` it ONTO {hit} (dest, not\n"
            f"     source) — that shape is not refused."
        )

    if reasons:
        sys.stderr.write("\n\n".join(reasons) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
