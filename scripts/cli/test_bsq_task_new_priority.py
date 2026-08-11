"""T-0877: `bsq task new --priority` refuses what the queue cannot rank.

The CLI is the surface that actually lost the tickets — its own help said
"priority value (frontmatter, verbatim)" and it meant it: any string, forwarded
untouched, stored untouched, unread by the pickup ranking. `--priority high`
exited 0 with a created ticket that the board could never surface again (86 of
them on watchrobot, the oldest invisible 20.6 days; measured T-0586).

The gate lives in `scripts/cli/priority.py`, a byte-identical mirror of
`worker/bot_squad_worker/priority.py` pinned by the module-mirror gate — so the
value this refuses and the value the reader ranks cannot drift apart, which is
the actual defect. This file asserts the CLI reaches that module at all, since a
correct helper the CLI never calls would leave the surface exactly as it was.

`bsq` is an extensionless script loaded via SourceFileLoader.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_priority", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_priority", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def test_the_cli_loads_the_shared_vocabulary_not_a_copy_of_it():
    mod = bsq._load_priority()
    assert mod.WORD_BANDS["high"] == 1
    # the same object on a second call — the CLI must not re-exec it per use
    assert bsq._load_priority() is mod


def test_control_a_vocabulary_word_normalises():
    """CONTROL — green, and it must come before the refusal below: without it
    the negative case cannot tell "refused the bad value" from "refuses
    everything"."""
    mod = bsq._load_priority()
    assert mod.normalize_priority("high") == "p1"
    assert mod.normalize_priority("  HIGH ") == "p1"
    assert mod.normalize_priority("P2") == "p2"


def test_a_non_vocabulary_value_is_refused_with_the_allowed_list():
    mod = bsq._load_priority()
    with pytest.raises(ValueError) as exc:
        mod.normalize_priority("высокий")
    assert mod.ALLOWED_HELP in str(exc.value)


def test_the_help_text_names_the_vocabulary_it_will_accept():
    """The old help promised `verbatim` and delivered it. A caller has to be
    able to read what is accepted BEFORE typing a value that would be lost."""
    parser = bsq.build_parser()
    help_text = parser.format_help()
    assert "verbatim" not in _task_new_priority_help(parser)
    assert bsq._load_priority().ALLOWED_HELP in _task_new_priority_help(parser)
    assert help_text  # the parser still builds as a whole


def _task_new_priority_help(parser) -> str:
    """The `--priority` help string as `bsq task new --help` would print it."""
    for action in parser._subparsers._group_actions:  # noqa: SLF001 - argparse has no public reader
        task = action.choices.get("task")
        if task is None:
            continue
        for sub in task._subparsers._group_actions:  # noqa: SLF001
            new = sub.choices.get("new")
            if new is None:
                continue
            for a in new._actions:  # noqa: SLF001
                if "--priority" in getattr(a, "option_strings", ()):
                    return a.help or ""
    raise AssertionError("bsq task new --priority not found in the parser")
