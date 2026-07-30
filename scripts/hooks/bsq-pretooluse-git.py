#!/usr/bin/env python3
"""Claude Code PreToolUse hook: route the worktree-destroying git verbs through
`bsq-worktree-guard.sh` before the Bash tool can run them (T-0826).

PROVENANCE — LIFTED, THEN WIDENED
---------------------------------
watchrobot's `.githooks/bsq-pretooluse-git.py` (their T-0433, eeacb17e plus the
`git_args` redirection fix they were adding on top of it), taken wholesale at
2026-07-30T13:41Z and offered for exactly that. Widened from `git stash` alone
to the five families operator p502 scoped in at 13:52Z. Found a defect in the
lifted parts? Tell their T-0433; do not fork this.

WHY THIS ENTRY POINT EXISTS AT ALL
---------------------------------
GIT HAS NO PRE-STASH HOOK. There is nothing in `.git/hooks` that can see a
`git stash` coming — which is why nothing fired at 12:55:49Z on 2026-07-30 and
why this guard cannot live in `.githooks/`. Two mechanisms can see it:

  1. A PATH-shimmed `git`. Real, but opt-in: it needs PATH ahead of /usr/bin in
     every session's shell, and it is deliberately NOT force-injected.
     NOT SHIPPED HERE. Measured on this tree across all five families, no script
     under deploy-recipes/, scripts/, worker/ or api/ EXECUTES one — the sole
     executed git-checkout in a recipe is `git -C <install> checkout -B <branch>
     origin/<branch>` (deploy-recipes/bot-squad/staging.sh:67), which is
     branch-level and destroys nothing. So our entire exposure is agent-typed
     command strings, which is what this layer catches. Re-measure before
     changing that; see scripts/hooks/README-git-guards.md.
  2. THIS hook. Every session in this clone is a Claude Code session and every
     git command it runs goes through the Bash tool, so a PreToolUse hook sees
     the command before the shell does: no PATH setup, nothing to remember, and
     it cannot be defeated by forgetting to source something. An allow-list
     would NOT do this — only a PreToolUse hook can refuse a tool call.

CONTRACT: stdin is the PreToolUse JSON. Exit 0 allows; exit 2 blocks and the
stderr text is fed back to the agent as the refusal reason.

SCOPE DISCIPLINE — THIS IS THE RISK UNIQUE TO ADDING IT HERE, and widening from
one verb to five widens it further. This hook sees EVERY Bash call in EVERY
session in this clone, so a bug in it breaks all work, not just stashes.
`checkout` and `reset` in particular are everyday commands. Therefore:
  - anything that is not a command-position match on one of the five verbs
    exits 0 immediately, before any subprocess;
  - the guard itself decides destructive-vs-harmless, so `git checkout <branch>`
    and `git reset --soft` pass — one place holds that judgement, not two;
  - any internal error in THIS FILE exits 0 (fail OPEN) rather than blocking
    unrelated work: at that point the hook knows nothing about the command;
  - but once a command IS identified as one of the five, a GUARD that fails —
    missing, unparseable, crashing, hanging, or returning anything outside its
    {0 allow, 3 refuse} contract — fails CLOSED. A broken check is not evidence
    the command is safe. watchrobot shipped and verified the opposite of this,
    and sabotage found it: their guard exited 127 when missing, which read as
    "not a refusal", so deleting or RENAMING the guard silently disarmed every
    session. They had renamed it themselves earlier in the same ticket.

That asymmetry is the whole design: a broken guard must not block `ls`, `npm
test` or `git status`, and must still refuse `git stash`.
`scripts/hooks/test_worktree_guard.sh` pins both directions by sabotage.
"""

import json
import os
import re
import subprocess
import sys

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bsq-worktree-guard.sh")

# Seconds to wait for the guard. Env-overridable so the timeout branch is
# provable in a selftest in seconds instead of in a minute and a half — an
# untimed timeout branch is where a "fails closed" claim goes stale.
try:
    GUARD_TIMEOUT = float(os.environ.get("BOT_SQUAD_GUARD_TIMEOUT") or 90)
except ValueError:
    GUARD_TIMEOUT = 90.0

# The closed set. `checkout` is here for `checkout -- <paths>`; the guard is what
# decides that `checkout <branch>` is harmless, so this layer stays dumb.
VERBS = ("stash", "reset", "checkout", "restore", "clean")

# Find `git <verb>` in COMMAND POSITION.
#
# Matching the bare substring would be wrong in a way that matters: agents write
# about this incident constantly — ticket notes, commit messages, the guard's own
# help text — and blocking `bsq ticket note "a bare git stash swept ..."` would
# train everyone to disable the hook. So the pattern requires the start of a
# command (line start or after a shell operator), tolerates a path prefix and
# git's pre-subcommand options; quoted occurrences are dropped separately.
# The `env` group matters as much as the match itself: `VAR=1 git stash` puts the
# assignment between the command position and `git`, so without it the pattern
# misses — and MISSING means the command runs unguarded. That is a silent bypass
# for any env-prefixed invocation, and it also happens to be the exact shape of
# the override this guard tells people to use, so the override would have
# "worked" by evading the hook rather than by being honoured. Captured here and
# replayed into the guard's environment below, so both cases go through it.
GIT_VERB = re.compile(
    r"""(?:^|[;&|(\n{]|&&|\|\|)\s*
        (?P<env>(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;&|]*)\s+)*)
        (?:sudo\s+)?
        (?:[\w./-]*/)?git\b
        (?:\s+(?:-C\s+\S+|-c\s+\S+|--git-dir[=\s]\S+|--work-tree[=\s]\S+
              |--no-pager|--no-replace-objects|-P))*
        \s+(?P<verb>""" + "|".join(VERBS) + r""")\b
        (?P<args>[^;&|)\n]*)""",
    re.VERBOSE,
)

ENV_ASSIGN = re.compile(
    r"""([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|'([^']*)'|([^\s;&|]*))"""
)

# `git -C <dir> …` acts on <dir>, not on cwd. Judging it as cwd would guard the
# wrong tree in both directions: waving through a destructive command aimed at
# the shared clone from elsewhere, and refusing a harmless one aimed at a
# scratch repo. This is the shape `deploy-recipes/bot-squad/staging.sh` uses.
GIT_C_DIR = re.compile(r"""\s-C\s+(?:'([^']+)'|"([^"]+)"|(\S+))""")
GIT_WORK_TREE = re.compile(r"""\s--work-tree[=\s]+(?:'([^']+)'|"([^"]+)"|(\S+))""")
GIT_DIR = re.compile(r"""\s--git-dir[=\s]+\S+""")

CD_PREFIX = re.compile(r"""\s*cd\s+(?:'([^']+)'|"([^"]+)"|([^\s;&|]+))\s*(?:&&|;)""")


def parse_env_prefix(prefix):
    """Env assignments written inline before `git`, as a dict."""
    out = {}
    for m in ENV_ASSIGN.finditer(prefix or ""):
        name = m.group(1)
        value = next((g for g in m.groups()[1:] if g is not None), "")
        out[name] = value
    return out


def git_args(raw):
    """The verb's arguments, with shell redirections stripped.

    `git stash 2>&1` otherwise yields an argument list of `['2>&1']`, which the
    guard then reads as a SUBCOMMAND named `2>&1` — so a bare stash classifies
    as an unknown verb, and `git stash push -- x 2>&1` loses its pathspec.
    Redirection is punctuation addressed to the shell, not an argument to git,
    so it is cut here: everything from the first < or > goes, and a trailing
    bare file-descriptor number goes with it.
    """
    head = re.split(r"[<>]", raw or "", maxsplit=1)[0]
    tokens = head.split()
    if tokens and re.fullmatch(r"\d+", tokens[-1]):
        tokens.pop()
    return tokens


HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def heredoc_spans(text):
    """Byte ranges covered by heredoc BODIES.

    Found by writing this guard's own tests: `cat > f <<'EOF' … git reset --hard
    … EOF` is a command-position match on every pattern here, and refusing it
    blocks anyone writing a test, a doc or a ticket note ABOUT these verbs. It is
    the same social failure as blocking prose, one quoting style over — and a
    heredoc body is not quoted, so `inside_quotes` cannot see it. A guard people
    route around is worse than none.

    Deliberately naive: it finds the delimiter, then treats everything up to a
    line equal to that delimiter as body. Nested and unterminated heredocs
    resolve to "body runs to end of string", which errs toward ALLOWING — the
    correct direction for a false-positive fix, and the reason this cannot be
    used to hide a real command: a real destructive command placed after an
    unterminated heredoc would not run either.
    """
    spans = []
    for m in HEREDOC_START.finditer(text):
        delim = m.group(2)
        body = text.find("\n", m.end())
        if body == -1:
            continue
        end = len(text)
        for line in re.finditer(r"^[ \t]*" + re.escape(delim) + r"[ \t]*$",
                                text[body:], re.MULTILINE):
            end = body + line.start()
            break
        spans.append((body, end))
    return spans


def inside_spans(spans, pos):
    return any(start <= pos < end for start, end in spans)


def inside_quotes(text, pos):
    """True if `pos` falls inside a single- or double-quoted string."""
    sq = dq = False
    i = 0
    while i < pos and i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'" and not dq:
            sq = not sq
        elif c == '"' and not sq:
            dq = not dq
        i += 1
    return sq or dq


def resolve_dir(target, cwd):
    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        target = os.path.join(cwd, target)
    return target if os.path.isdir(target) else None


def effective_cwd(payload, command, match):
    """The repo the command will actually act on, or None if it is not derivable.

    A leading `cd <dir>` retargets it; `git -C <dir>` and `--work-tree=<dir>`
    retarget it harder. Judging the wrong tree is damaging in BOTH directions,
    which is why this is not best-effort: watchrobot reproduced both halves —
    `git -C <shared clone> stash` typed from a clean scratch cwd returned rc=0
    and was waved through at a tree with 13 dirty paths in it, and a harmless
    stash aimed at a clean scratch repo was refused because the shared tree was
    dirty. Over-blocking unrelated repos is how a guard gets switched off; the
    other direction is the incident.

    Returns None when the target is NOT derivable from the command line — an
    unresolvable `-C`, or a `--git-dir` with no `--work-tree`. The caller fails
    closed on that, because guessing which tree is about to be rewritten is the
    same error in a quieter form.
    """
    cwd = payload.get("cwd") or os.getcwd()
    m = CD_PREFIX.match(command)
    if m:
        cwd = resolve_dir(next(g for g in m.groups() if g), cwd) or cwd

    # These are written between `git` and the verb, i.e. inside this match's
    # span, and apply cumulatively after the cd.
    span = command[match.start():match.start("verb")]
    for pattern in (GIT_C_DIR, GIT_WORK_TREE):
        hit = pattern.search(span)
        if hit:
            resolved = resolve_dir(next(g for g in hit.groups() if g), cwd)
            if resolved is None:
                return None
            cwd = resolved
    if GIT_DIR.search(span) and not GIT_WORK_TREE.search(span):
        return None
    return cwd


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return 0
    if not any(v in command for v in VERBS):
        return 0

    try:
        skip = heredoc_spans(command)
        hits = [
            m for m in GIT_VERB.finditer(command)
            if not inside_quotes(command, m.start())
            and not inside_spans(skip, m.start())
        ]
    except Exception:
        return 0
    if not hits:
        return 0

    reasons = []
    for m in hits:
        verb = m.group("verb")
        args = git_args(m.group("args"))
        failure = None
        target = effective_cwd(payload, command, m)
        if target is None:
            reasons.append(
                "bsq-worktree-guard: REFUSED — `git %s %s` names a repository this\n"
                "hook cannot resolve (an unreadable -C/--work-tree, or a --git-dir\n"
                "with no --work-tree). Which tree is about to be rewritten is not\n"
                "derivable from the command line, and guessing it is how a guard\n"
                "waves through the one command it exists to stop. Re-run from the\n"
                "target repo, or name --work-tree explicitly."
                % (verb, " ".join(args))
            )
            continue
        try:
            proc = subprocess.run(
                ["bash", GUARD, "check", verb] + args,
                cwd=target,
                capture_output=True, text=True, timeout=GUARD_TIMEOUT,
                env={**os.environ, **parse_env_prefix(m.group("env"))},
            )
        except Exception as exc:
            failure = str(exc) or exc.__class__.__name__
        else:
            if proc.returncode == 3:
                reasons.append(proc.stderr.strip() or "bsq-worktree-guard: REFUSED")
                continue
            if proc.returncode != 0:
                # NOT a verdict. 127 = the guard is missing (a rename disarms
                # every session), 2 = bash could not even parse it, anything
                # else = it broke midway. The contract has two codes; this is
                # neither, so the guard did not run.
                failure = "guard exited %d: %s" % (
                    proc.returncode,
                    (proc.stderr or "").strip()[:400] or "(no output)")

        if failure is not None:
            # We KNOW this is one of the five and could not evaluate it.
            # FAIL CLOSED — a broken check is not evidence the command is safe.
            reasons.append(
                "bsq-worktree-guard could not evaluate `git %s %s` — %s\n"
                "\n"
                "REFUSING, because a guard that did not run is not a guard that\n"
                "said yes. In a shared clone an unevaluated worktree-destroying\n"
                "command is the T-0826 incident. Check that\n"
                "  %s\n"
                "exists and runs; re-run with BOT_SQUAD_ALLOW_WORKTREE_WIPE=1 if\n"
                "you are certain you own every uncommitted path in the tree."
                % (verb, " ".join(args), failure, GUARD)
            )

    if reasons:
        sys.stderr.write("\n\n".join(reasons) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never let an unexpected failure in this hook block unrelated work.
        sys.exit(0)
