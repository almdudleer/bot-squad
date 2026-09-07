"""Tests for bsq-pretooluse-protect-exec.py (T-1068).

Each case feeds the hook a PreToolUse JSON payload on stdin, exactly as
Claude Code does, and checks the exit code: 0 = allowed, 2 = refused.
"""
import json
import os
import subprocess
import sys

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "bsq-pretooluse-protect-exec.py")


def _run(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run([sys.executable, HOOK], input=payload,
                          capture_output=True, text=True, timeout=10)


def test_rm_of_bsq_is_refused():
    proc = _run("rm scripts/cli/bsq")
    assert proc.returncode == 2
    assert "scripts/cli/bsq" in proc.stderr


def test_rm_of_bsq_launch_is_refused():
    proc = _run("rm -f scripts/cli/bsq-launch")
    assert proc.returncode == 2


def test_mv_of_bsq_as_source_is_refused():
    proc = _run("mv scripts/cli/bsq /tmp/backup")
    assert proc.returncode == 2


def test_bare_rm_bsq_from_inside_cli_dir_is_refused():
    proc = _run("rm bsq")
    assert proc.returncode == 2


def test_mv_of_bsq_as_destination_is_allowed():
    """The correct hand-rolled temp-then-rename recipe: chmod a temp file
    then mv it ONTO bsq. Must not be refused, or the guard blocks its own
    suggested workaround."""
    proc = _run("mv /tmp/newbsq scripts/cli/bsq")
    assert proc.returncode == 0


def test_unrelated_file_is_allowed():
    proc = _run("rm scripts/cli/test_bsq_expert.py")
    assert proc.returncode == 0


def test_unrelated_command_is_allowed():
    proc = _run("git status")
    assert proc.returncode == 0


def test_quoted_mention_is_not_a_command():
    """A ticket note ABOUT the incident, quoting the dangerous command, must
    not itself be refused — same social-failure class the git guard's own
    quote-handling exists for."""
    proc = _run("bsq ticket note T-1068 'saw rm scripts/cli/bsq in the log'")
    assert proc.returncode == 0


def test_heredoc_body_mentioning_it_is_not_a_command():
    proc = _run("cat > /tmp/note.txt <<'EOF'\n"
                "this incident involved: rm scripts/cli/bsq\n"
                "EOF")
    assert proc.returncode == 0


def test_non_bash_tool_is_ignored():
    payload = json.dumps({"tool_name": "Edit",
                          "tool_input": {"file_path": "scripts/cli/bsq"}})
    proc = subprocess.run([sys.executable, HOOK], input=payload,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0


def test_malformed_stdin_fails_open():
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
