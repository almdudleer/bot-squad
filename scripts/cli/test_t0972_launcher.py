"""T-0972: the executed bsq is a published snapshot, never the edited file.

Two kinds of assertion live here and they answer different questions.

* ``TestInstalledPath`` is the DoD-5 guard: it looks at what this host will
  ACTUALLY execute when someone types ``bsq``, and goes red if that resolves
  back into an editable working tree. It is the only test here that can catch
  a regression made outside the repo — someone re-pointing the symlink.
* everything else drives ``bsq-launch``'s ``refresh()`` against a scratch home
  and pins the promotion rules, including the two failure classes that took
  the fleet down on 2026-09-06 and could not both be caught by one check.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_CLI_DIR = Path(__file__).resolve().parent
_REPO = _CLI_DIR.parents[1]
_LAUNCH_SRC = _CLI_DIR / "bsq-launch"
_REAL_BSQ = _CLI_DIR / "bsq"

_loader = SourceFileLoader("bsq_launch_t0972", str(_LAUNCH_SRC))
_spec = importlib.util.spec_from_loader("bsq_launch_t0972", _loader)
launch = importlib.util.module_from_spec(_spec)
_loader.exec_module(launch)


# ==========================================================================
# DoD 5 — what this host actually executes
# ==========================================================================
def _git_toplevel(path: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class TestInstalledPath:
    """The whole point of the ticket, asserted against the live host."""

    def _installed(self) -> Path:
        found = shutil.which("bsq")
        if not found:
            pytest.skip("no `bsq` on PATH — nothing is installed on this host to "
                        "make a claim about. This is a real gap, not a pass: on a "
                        "host where bsq IS installed this test is the only thing "
                        "that notices the symlink coming back.")
        return Path(found)

    def test_bsq_on_path_is_not_a_symlink(self):
        p = self._installed()
        assert not p.is_symlink(), (
            f"{p} is a symlink to {os.readlink(p)}.\n"
            "That is the T-0972 defect verbatim: the file every session EXECUTES "
            "is the file one session is EDITING, so every save is an unreviewed "
            "fleet-wide deploy.\n"
            f"Fix: python3 {_LAUNCH_SRC} --bsq-install {_REAL_BSQ}")

    def test_the_executed_bsq_does_not_resolve_into_a_working_tree(self):
        p = self._installed()
        real = Path(os.path.realpath(p))
        top = _git_toplevel(real)
        assert top is None, (
            f"`bsq` resolves to {real}, which is inside the git working tree "
            f"{top}.\nA file under version control is a file somebody edits, and "
            "an edit to it is live for the whole fleet the instant it is saved.\n"
            f"Fix: python3 {_LAUNCH_SRC} --bsq-install {_REAL_BSQ}")

    def test_the_snapshot_it_execs_is_also_outside_any_working_tree(self):
        p = self._installed()
        r = subprocess.run([str(p), "--bsq-where"], capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0:
            pytest.skip(f"{p} does not understand --bsq-where — it predates "
                        "T-0972 and test_bsq_on_path_is_not_a_symlink covers it")
        snap = Path(r.stdout.strip())
        assert _git_toplevel(snap) is None, (
            f"the snapshot {snap} is itself inside a working tree — the "
            "indirection exists but points back at the hazard")


class TestInstalledLauncherMatchesTheRepo:
    def test_the_installed_launcher_is_the_tracked_one(self):
        found = shutil.which("bsq")
        if not found:
            pytest.skip("no `bsq` on PATH")
        p = Path(found)
        if p.is_symlink():
            pytest.skip("covered by TestInstalledPath, which fails on this")
        installed = p.read_bytes()
        if b"--bsq-install" not in installed:
            pytest.skip("the installed bsq predates T-0972")
        assert installed == _LAUNCH_SRC.read_bytes(), (
            f"{p} has drifted from {_LAUNCH_SRC}. The launcher is a COPY, so a "
            "repo edit does not reach the fleet on its own — the fleet is "
            "running an older launcher than the one under review.\n"
            f"Fix: python3 {_LAUNCH_SRC} --bsq-install {_REAL_BSQ}")


# ==========================================================================
# the promotion rules
# ==========================================================================
_MINI = textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, sys
    MARKER = {marker!r}
    def build_parser():
        p = argparse.ArgumentParser(prog="bsq")
        sub = p.add_subparsers(dest="cmd")
        for v in ({verbs}):
            sub.add_parser(v)
        {extra}
        return p
    def main(argv):
        print(MARKER)
    if __name__ == "__main__":
        main(sys.argv[1:])
    ''')


def _mini(marker="v1", verbs=None, extra="pass"):
    verbs = verbs if verbs is not None else list(launch.CORE_VERBS)
    return _MINI.format(marker=marker,
                        verbs=", ".join(repr(v) for v in verbs) + ("," if verbs else ""),
                        extra=extra)


@pytest.fixture
def lab(tmp_path):
    home = tmp_path / "home"
    src = tmp_path / "tree" / "bsq"
    src.parent.mkdir(parents=True)
    _save(src, _mini("v1"))
    _age(src)
    assert launch.refresh(home, src, force=True, quiet=True)[0] == "promoted"
    return home, src


def _save(path: Path, text: str):
    """Write a candidate source AS ONE REALLY EXISTS — executable.

    `scripts/cli/bsq` is mode 755. A fixture that writes 644 is not a smaller
    version of the real thing, it is a different thing, and it trips the
    exec-bit warning in tests that are about something else entirely.
    """
    path.write_text(text)
    os.chmod(path, 0o755)


def _age(path: Path, seconds: float = 5.0):
    """Push mtime back past the settle window — these tests are about the
    validation rules, not about the debounce (which has its own test)."""
    t = time.time() - seconds
    os.utime(path, (t, t))


def _snapshot_text(home: Path) -> str:
    return launch._snapshot(home).read_text()


def test_healthy_source_promotes_and_is_what_gets_executed(lab):
    home, src = lab
    assert "v1" in _snapshot_text(home)
    # unchanged source takes the fast path, no re-validation
    assert launch.refresh(home, src)[0] == "current"


def test_a_good_edit_is_promoted(lab):
    home, src = lab
    _save(src, _mini("v2")); _age(src)
    assert launch.refresh(home, src)[0] == "promoted"
    assert "v2" in _snapshot_text(home)


def test_a_torn_save_is_rejected_and_the_fleet_keeps_the_last_good_copy(lab, capsys):
    home, src = lab
    whole = _mini("v2")
    _save(src, whole[:int(len(whole) * 0.6)])  # what write_text leaves mid-stream
    _age(src)
    outcome, _ = launch.refresh(home, src)
    assert outcome == "rejected"
    assert "v1" in _snapshot_text(home), "the last good copy must still be executed"
    err = capsys.readouterr().err
    assert "NOT RUNNING YOUR LATEST EDIT" in err, "the failure must reach the editor"


def test_a_subparser_collision_is_rejected_although_it_parses(lab, capsys):
    """Outage 1 of 3. ast.parse/py_compile pass this; build_parser() does not.

    This is the arm that makes the second half of DoD 2 load-bearing rather
    than belt-and-braces.
    """
    home, src = lab
    text = _mini("v2", extra='sub.add_parser("inbox")')  # a second `inbox`
    import ast
    ast.parse(text)  # the candidate is perfectly valid Python
    _save(src, text); _age(src)
    outcome, detail = launch.refresh(home, src)
    assert outcome == "rejected"
    assert "conflicting subparser" in detail
    assert "v1" in _snapshot_text(home)


def test_a_candidate_that_parses_and_builds_but_lost_a_core_verb_is_rejected(lab):
    """A prefix of a Python file can be valid Python. `it parsed` is not
    `it is whole`, so the verb set is checked too."""
    home, src = lab
    verbs = [v for v in launch.CORE_VERBS if v != "inbox"]
    _save(src, _mini("v2", verbs=verbs)); _age(src)
    outcome, detail = launch.refresh(home, src)
    assert outcome == "rejected"
    assert "core verbs are MISSING" in detail and "inbox" in detail
    assert "v1" in _snapshot_text(home)


def test_the_rejection_is_repeated_every_time_until_it_is_fixed(lab, capsys):
    home, src = lab
    _save(src, "def build_parser(:\n"); _age(src)
    launch.refresh(home, src); capsys.readouterr()
    for _ in range(3):
        assert launch.refresh(home, src)[0] == "rejected"
        assert "NOT RUNNING YOUR LATEST EDIT" in capsys.readouterr().err, (
            "a warning that fires once is a warning the editor scrolls past")


def test_recovery_promotes_on_the_very_next_call(lab, capsys):
    home, src = lab
    _save(src, "def build_parser(:\n"); _age(src)
    assert launch.refresh(home, src)[0] == "rejected"
    _save(src, _mini("v3")); _age(src)
    assert launch.refresh(home, src)[0] == "promoted"
    assert "v3" in _snapshot_text(home)
    assert not (home / "rejected").exists()


def test_a_save_still_in_flight_is_neither_promoted_nor_reported(lab, capsys):
    """The settle window. A file written moments ago is assumed to still be
    streaming; that state is transient by construction, so it is silent."""
    home, src = lab
    _save(src, _mini("v2"))
    _age(src, 5.0)
    # A 60s window against a 5s-old file: "recently saved" without depending on
    # how fast this host happens to be scheduling us.
    outcome, _ = launch.refresh(home, src, settle_s=60.0)
    assert outcome == "in-flight"
    assert "v1" in _snapshot_text(home)
    assert capsys.readouterr().err == "", (
        "a save in progress is not a failure and must not cry wolf")


def test_force_ignores_the_settle_window(lab):
    """`--bsq-publish` is an editor saying `I have finished writing`."""
    home, src = lab
    _save(src, _mini("v2"))
    _age(src, 5.0)
    assert launch.refresh(home, src, force=True, quiet=True,
                          settle_s=60.0)[0] == "promoted"


def test_a_vanished_source_leaves_the_fleet_running(lab, capsys):
    home, src = lab
    src.unlink()
    assert launch.refresh(home, src)[0] == "no-source"
    assert "v1" in _snapshot_text(home)


def test_identical_bytes_with_a_new_mtime_are_restamped_not_revalidated(lab):
    """`git checkout` of an unchanged file must not cost a validation."""
    home, src = lab
    before = json.loads((home / "stamp").read_text())
    os.utime(src, None); _age(src, 1.0)
    assert launch.refresh(home, src)[0] == "current"
    after = json.loads((home / "stamp").read_text())
    assert after["sha"] == before["sha"]
    assert after["stat"] != before["stat"], "the fast path must stop missing"


def test_concurrent_refreshes_produce_one_valid_snapshot(lab):
    home, src = lab
    _save(src, _mini("v2")); _age(src)
    procs = [subprocess.Popen(
        [sys.executable, "-c",
         f"import importlib.machinery as m, importlib.util as u, pathlib;"
         f"l=m.SourceFileLoader('L', {str(_LAUNCH_SRC)!r});"
         f"s=u.spec_from_loader('L', l);mod=u.module_from_spec(s);l.exec_module(mod);"
         f"print(mod.refresh(pathlib.Path({str(home)!r}), pathlib.Path({str(src)!r}))[0])"],
        stdout=subprocess.PIPE, text=True) for _ in range(8)]
    outs = [p.communicate()[0].strip() for p in procs]
    assert all(o in ("promoted", "current") for o in outs), outs
    assert "v2" in _snapshot_text(home)
    leftovers = [p.name for p in (home / "bin").iterdir() if p.name.startswith(".candidate")]
    assert leftovers == [], f"temp candidates left behind: {leftovers}"


# ==========================================================================
# one arm against the REAL artifact, not a stand-in
# ==========================================================================
def test_the_real_bsq_validates_and_carries_every_core_verb(tmp_path):
    """The mini-CLI above tests the launcher's rules. This tests the claim
    those rules make ABOUT THE ACTUAL FILE: that today's scripts/cli/bsq
    passes ast.parse + build_parser() and exposes every verb the launcher
    treats as load-bearing. If someone renames one, this is what says so."""
    err, verbs = launch._validate(_REAL_BSQ)
    assert err is None, err
    missing = [v for v in launch.CORE_VERBS if v not in verbs]
    assert missing == [], (
        f"scripts/cli/bsq no longer defines {missing}. Either the rename is "
        "intended — then update CORE_VERBS in bsq-launch — or the file is "
        "incomplete.")


# ==========================================================================
# THE SIBLING SET (added after this launcher's own cutover broke the fleet)
#
# `scripts/cli/bsq` loads four modules as sibling FILES by path, because a
# mirrored module may not depend on its package being importable. The first
# cutover published `bsq` alone: --help, `team status` and `inbox check` all
# passed, and every frontmatter-reading verb died with FileNotFoundError.
# The unit that can be published is the directory.
# ==========================================================================
class TestSiblingSet:
    def test_the_set_is_derived_from_the_real_bsq(self):
        found = launch.sibling_modules(_REAL_BSQ)
        assert found, ("no sibling modules derived from scripts/cli/bsq — if "
                       "the sibling pattern is genuinely gone this test should "
                       "be deleted deliberately, not left passing vacuously")
        for name in found:
            assert (_CLI_DIR / name).is_file(), f"{name} is declared but absent"

    def test_derivation_is_by_ast_not_by_text(self, tmp_path):
        """This very file, and bsq-launch, discuss `frontmatter.py` in prose.
        A textual scan of a 470KB script cannot tell a docstring from a load."""
        f = tmp_path / "prose.py"
        f.write_text('"""We load frontmatter.py as a sibling."""\n'
                     'X = "fleet_slot.py"\n')
        assert launch.sibling_modules(f) == []

    def test_a_promoted_revision_carries_the_whole_set(self, tmp_path):
        home = tmp_path / "home"
        tree = tmp_path / "tree"
        tree.mkdir()
        for name in ["bsq"] + launch.sibling_modules(_REAL_BSQ):
            shutil.copyfile(_CLI_DIR / name, tree / name)
        _age(tree / "bsq")
        assert launch.refresh(home, tree / "bsq", force=True, quiet=True)[0] == "promoted"
        snap = launch._snapshot(home)
        for name in launch.sibling_modules(_REAL_BSQ):
            assert (snap.parent / name).is_file(), (
                f"{name} was not published beside bsq — this is the 10:05 "
                "cutover defect exactly")

    def test_bin_is_a_symlink_to_a_revision_directory(self, tmp_path):
        home = tmp_path / "home"
        tree = tmp_path / "tree"
        tree.mkdir()
        for name in ["bsq"] + launch.sibling_modules(_REAL_BSQ):
            shutil.copyfile(_CLI_DIR / name, tree / name)
        _age(tree / "bsq")
        launch.refresh(home, tree / "bsq", force=True, quiet=True)
        assert (home / "bin").is_symlink(), (
            "a revision must be swapped in by renaming ONE symlink; publishing "
            "into a real directory means a reader can see it half-built")
        assert (home / "bin").resolve().parent == (home / "rev").resolve()

    def test_a_sibling_missing_at_the_SOURCE_is_refused(self, tmp_path):
        home = tmp_path / "home"
        tree = tmp_path / "tree"
        tree.mkdir()
        shutil.copyfile(_REAL_BSQ, tree / "bsq")  # siblings deliberately absent
        _age(tree / "bsq")
        outcome, detail = launch.refresh(home, tree / "bsq", force=True, quiet=True)
        assert outcome == "rejected"
        assert "sibling" in detail

    def test_validating_a_lone_copy_is_refused_by_the_loader_exercise_too(
            self, tmp_path):
        """Two independent guards, not one read twice.

        The declared-set existence check and the `_load_*` exercise catch this
        by different means: pass an EMPTY declared set and the loader exercise
        still refuses, with the same FileNotFoundError the fleet saw.
        """
        lone = tmp_path / "bsq"
        shutil.copyfile(_REAL_BSQ, lone)
        err_declared, _ = launch._validate(lone, launch.sibling_modules(_REAL_BSQ))
        assert err_declared and "sibling modules missing" in err_declared
        err_loader, _ = launch._validate(lone, [])
        assert err_loader and "FileNotFoundError" in err_loader

    def test_the_pre_fix_validator_accepted_this(self, tmp_path):
        """THE CONTROL. The 10:05 probe — ast.parse + exec_module +
        build_parser, no sibling check, no loader exercise — reconstructed
        verbatim. It returns 0 on a snapshot with no siblings at all, which is
        why the cutover looked clean and took ticket writes down fleet-wide.
        Without this arm the tests above could be passing against a validator
        that was never capable of the failure.
        """
        lone = tmp_path / "bsq"
        shutil.copyfile(_REAL_BSQ, lone)
        old_probe = (
            "import ast, importlib.machinery, importlib.util, pathlib, sys\n"
            "p = sys.argv[1]\n"
            "ast.parse(pathlib.Path(p).read_text(), filename=p)\n"
            "l = importlib.machinery.SourceFileLoader('c', p)\n"
            "s = importlib.util.spec_from_loader('c', l)\n"
            "m = importlib.util.module_from_spec(s)\n"
            "l.exec_module(m)\n"
            "m.build_parser()\n")
        r = subprocess.run([sys.executable, "-c", old_probe, str(lone)],
                           capture_output=True, text=True,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                           timeout=120)
        assert r.returncode == 0, (
            "the pre-fix validator no longer accepts a sibling-less snapshot, "
            "so this control has stopped controlling anything — rewrite it "
            "against whatever it is now testing, do not delete it")

    def test_a_non_python_sibling_would_also_be_published(self, tmp_path):
        """All four of bsq's siblings are `.py` today. A derivation that only
        understood `.py` would drop a `.sh` or `.toml` companion silently the
        day someone added one — the same defect as the 10:05 cutover, one file
        type over. Pinned so the widening cannot be quietly narrowed again."""
        f = tmp_path / "cli"
        f.write_text('from pathlib import Path\n'
                     'A = Path(__file__).resolve().parent / "helper.sh"\n'
                     'B = Path(__file__).resolve().parent / "table.toml"\n')
        assert launch.sibling_modules(f) == ["helper.sh", "table.toml"]

    def test_bsq_declares_no_self_relative_path_the_launcher_cannot_publish(self):
        """Every `Path(__file__) / X` in bsq must resolve to a NAME the
        derivation can see. A directory, a `..`, or a computed path would be
        invisible to it, and invisible means absent from the snapshot."""
        import ast as _ast

        tree = _ast.parse(_REAL_BSQ.read_text(encoding="utf-8"))
        opaque = []
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Div)
                    and "__file__" in _ast.unparse(node.left)):
                if not (isinstance(node.right, _ast.Constant)
                        and isinstance(node.right.value, str)
                        and "/" not in node.right.value
                        and "." in node.right.value.strip(".")):
                    opaque.append(_ast.unparse(node))
        assert opaque == [], (
            "bsq resolves these against its own location in a form "
            f"sibling_modules() cannot read: {opaque}. Whatever they point at "
            "will NOT be in the published snapshot, and the verb that needs it "
            "will die with FileNotFoundError for the whole fleet.")


# ==========================================================================
# STALENESS ACROSS THE SET (found by p731 after the second cutover)
#
# The stamp originally keyed the fast path on `bsq`'s own (size, mtime).
# Editing `frontmatter.py` alone left that key unchanged, so refresh answered
# "current", the sibling edit was NEVER promoted, and `--bsq-status` reported
# `in sync` while the snapshot served stale bytes. Silent version skew: worse
# than the loud outage this launcher exists to prevent, and invisible for the
# same reason both outages were — the thing being checked was not the thing
# that changed.
# ==========================================================================
class TestSetStaleness:
    @pytest.fixture
    def real_lab(self, tmp_path):
        home = tmp_path / "home"
        tree = tmp_path / "tree"
        tree.mkdir()
        for name in ["bsq"] + launch.sibling_modules(_REAL_BSQ):
            shutil.copyfile(_CLI_DIR / name, tree / name)
        _age(tree / "bsq")
        for name in launch.sibling_modules(_REAL_BSQ):
            _age(tree / name)
        assert launch.refresh(home, tree / "bsq", force=True, quiet=True)[0] == "promoted"
        return home, tree / "bsq"

    def test_a_sibling_only_edit_is_promoted(self, real_lab):
        home, src = real_lab
        sib = src.parent / "frontmatter.py"
        sib.write_text(sib.read_text() + "\nT0972_MARKER = 1\n")
        _age(sib)
        assert launch.refresh(home, src)[0] == "promoted", (
            "bsq is byte-identical, so a stamp keyed on bsq alone says "
            "'current' and this edit never reaches the fleet")
        published = launch._snapshot(home).parent / "frontmatter.py"
        assert "T0972_MARKER" in published.read_text()

    def test_status_does_not_claim_in_sync_when_a_sibling_drifted(self, real_lab, capsys):
        home, src = real_lab
        sib = src.parent / "priority.py"
        sib.write_text(sib.read_text() + "\nT0972_MARKER = 1\n")
        _age(sib)
        launch._cmd_status(home, src)
        out = capsys.readouterr().out
        assert "in sync  : NO" in out
        assert "priority.py" in out, "and it must name WHICH member drifted"

    def test_a_broken_sibling_is_refused_and_the_last_good_set_is_kept(
            self, real_lab, capsys):
        home, src = real_lab
        sib = src.parent / "frontmatter.py"
        good = (launch._snapshot(home).parent / "frontmatter.py").read_bytes()
        sib.write_text("def broken(:\n")
        _age(sib)
        outcome, _ = launch.refresh(home, src)
        assert outcome == "rejected"
        assert (launch._snapshot(home).parent / "frontmatter.py").read_bytes() == good
        assert "NOT RUNNING YOUR LATEST EDIT" in capsys.readouterr().err

    def test_the_revision_digest_changes_when_only_a_sibling_changes(self, real_lab):
        """The identity of a revision is the identity of the whole set. A
        digest over bsq alone cannot tell two revisions apart, which would make
        both the already-current and already-rejected shortcuts fire on the
        wrong evidence."""
        home, src = real_lab
        first = json.loads((home / "stamp").read_text())["sha"]
        sib = src.parent / "task_search.py"
        sib.write_text(sib.read_text() + "\nT0972_MARKER = 1\n")
        _age(sib)
        launch.refresh(home, src)
        second = json.loads((home / "stamp").read_text())["sha"]
        assert first != second


# ==========================================================================
# THE EXEC BIT (raised by p735 against this launcher)
#
# The launcher reads the source and runs it under `sys.executable`, so it
# NEVER NEEDS the execute bit. When scripts/cli/bsq lost that bit, every
# session going through the launcher kept working and only direct-path callers
# broke: the protection MASKED a breakage it does not cover. That is worse
# than not covering it, because the healthy majority is what people check.
# ==========================================================================
class TestSourceExecBit:
    @pytest.fixture
    def lab2(self, tmp_path):
        home = tmp_path / "home"
        src = tmp_path / "tree" / "bsq"
        src.parent.mkdir(parents=True)
        _save(src, _mini("v1"))
        os.chmod(src, 0o755)
        _age(src)
        launch.refresh(home, src, force=True, quiet=True)
        return home, src

    def test_a_chmod_alone_is_noticed(self, lab2):
        """chmod changes ctime, NOT mtime. A key of (size, mtime) cannot see
        it at all, which is why mode is part of the identity."""
        home, src = lab2
        before = os.stat(src).st_mtime_ns
        stamped_before = json.loads((home / "stamp").read_text())["set"]["bsq"]
        os.chmod(src, 0o644)
        assert os.stat(src).st_mtime_ns == before, (
            "if chmod started moving mtime this test proves nothing — the "
            "point is that it does not")

        # The outcome is legitimately `current`: the CONTENT is unchanged, so
        # the published snapshot is still right and nothing needs promoting.
        # What must not happen is the pre-lock fast path swallowing the change
        # without ever looking — which is exactly what a (size, mtime) key does.
        launch.refresh(home, src, quiet=True)
        stamped_after = json.loads((home / "stamp").read_text())["set"]["bsq"]
        assert stamped_after != stamped_before, (
            "the mode change was invisible: refresh returned without even "
            "re-reading the source, so nothing could ever report it")
        assert stamped_after[2] == 0o644

    def test_a_non_executable_source_is_reported_every_time(self, lab2, capsys):
        home, src = lab2
        os.chmod(src, 0o644)
        for _ in range(3):
            launch.refresh(home, src)
            err = capsys.readouterr().err
            assert "NOT EXECUTABLE" in err, (
                "reported EVERY time on purpose: nothing else on the host is "
                "positioned to notice, so a once-only warning is a warning "
                "that gets scrolled past")
            assert "chmod +x" in err, "and it must say how to fix it"

    def test_an_executable_source_says_nothing(self, lab2, capsys):
        home, src = lab2
        os.chmod(src, 0o755)
        _age(src)
        launch.refresh(home, src)
        assert "NOT EXECUTABLE" not in capsys.readouterr().err

    def test_status_shows_the_mode(self, lab2, capsys):
        home, src = lab2
        os.chmod(src, 0o644)
        launch._cmd_status(home, src)
        out = capsys.readouterr().out
        assert "src mode : 0o644" in out and "NOT EXECUTABLE" in out

    def test_the_launcher_still_works_without_the_bit(self, lab2, capsys):
        """The masking itself is correct behaviour and must not regress into a
        refusal — the fleet keeping working is the point. It just must not be
        SILENT about it."""
        home, src = lab2
        src.write_text(_mini("v2"))
        os.chmod(src, 0o644)
        _age(src)
        assert launch.refresh(home, src)[0] == "promoted"
        assert "v2" in _snapshot_text(home)


# ==========================================================================
# T-1069: THE INSTALL ITSELF MUST NEVER MAKE ~/.local/bin/bsq ABSENT
#
# `_cmd_install` used to `dest.unlink()` before writing the new copy, which
# left the path a concurrent `bsq` call could resolve to NOTHING for the
# length of a mkstemp + copyfile + chmod. `os.replace` already swaps an
# EXISTING file atomically, so the fix is simply: never unlink first. These
# tests pin that behaviourally — by polling the path from a concurrent thread
# during a deliberately slowed install — rather than by asserting the
# implementation doesn't call unlink, which would pass even if a refactor
# reintroduced the same hazard through a different call.
# ==========================================================================
class TestInstallIsAtomic:
    def _prep(self, tmp_path):
        home = tmp_path / "home"
        src = tmp_path / "tree" / "bsq"
        src.parent.mkdir(parents=True)
        _save(src, _mini("v1"))
        dest = tmp_path / "bin" / "bsq"
        dest.parent.mkdir(parents=True)
        _save(dest, _mini("v0"))  # a pre-existing install, as every real host has
        return home, src, dest

    def test_dest_is_never_absent_during_a_slow_install(self, tmp_path, monkeypatch):
        import threading

        home, src, dest = self._prep(tmp_path)

        real_copyfile = shutil.copyfile

        def slow_copyfile(a, b):
            # The window the old code left open: mkstemp already happened,
            # the temp is about to be filled. Sleeping HERE, not before, is
            # what makes a concurrent poll land inside the real install
            # window rather than before it starts.
            real_copyfile(a, b)
            time.sleep(0.3)

        monkeypatch.setattr(shutil, "copyfile", slow_copyfile)

        seen_missing = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                if not dest.exists():
                    seen_missing.append(time.time())

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            rc = launch._cmd_install(home, [str(src), str(dest)])
        finally:
            stop.set()
            poller.join(timeout=5)

        assert rc == 0
        assert not seen_missing, (
            f"{dest} was observed ABSENT {len(seen_missing)} time(s) during "
            "install — a concurrent `bsq` call in that window gets "
            "'command not found', the exact T-1069 incident.")
        assert dest.exists()

    def test_a_preexisting_symlink_dest_is_also_never_absent(self, tmp_path, monkeypatch):
        """The other dest shape `_cmd_install` handles specially (recording
        `was a symlink -> ...` before replacing it) must be equally atomic —
        the backup bookkeeping runs BEFORE the replace either way, so this
        pins that it does not itself introduce a window."""
        import threading

        home, src, real_dest = self._prep(tmp_path)
        dest = tmp_path / "bin" / "bsq-link"
        dest.symlink_to(real_dest)

        real_copyfile = shutil.copyfile

        def slow_copyfile(a, b):
            real_copyfile(a, b)
            time.sleep(0.3)

        monkeypatch.setattr(shutil, "copyfile", slow_copyfile)

        seen_missing = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                if not dest.exists() and not dest.is_symlink():
                    seen_missing.append(time.time())

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            rc = launch._cmd_install(home, [str(src), str(dest)])
        finally:
            stop.set()
            poller.join(timeout=5)

        assert rc == 0
        assert not seen_missing
        assert not dest.is_symlink(), "install must leave a real copy, not the old symlink"
