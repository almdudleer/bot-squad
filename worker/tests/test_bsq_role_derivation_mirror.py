"""T-0778: `bsq` and the worker must answer "which role is this window?" the same.

THE DEFECT THIS PINS. `scripts/cli/bsq::_role_from_sid` carried a docstring
calling itself a *"Mirror of scripts/hooks/derive_role.sh /
sessions._derive_role"* while having branches for only `operator`,
`user-conversation` and `tl|teamlead`. `sessions._derive_role` has known two
more roles since T-0197. Both consequences were SILENT — nothing raised, the
session just read a contract that was not its own:

  * a `…-prod-tl` window hit bsq's `tl|teamlead` arm FIRST and got
    `teamlead.md`. That is the precedence `worker/tests/test_sessions.py`
    asserts must NOT happen — the worker test was the oracle, and bsq had the
    arms in the opposite order.
  * a `…-qa` window fell through to `dev` and got `dev.md`.
  * `_ROLE_FILES` had no key for either, so even a corrected derivation still
    landed on cmd_brief's `.get(role, "dev.md")`. Both halves are pinned here.

WHY A MIRROR AND NOT AN IMPORT. `bsq` is stdlib-only by design: a bare
`/usr/bin/env python3`, no venv, worker contact over the socket only. Importing
`bot_squad_worker` from it is *possible* (measured: ~0.1s, the
`sessions` import chain is stdlib-clean) but would make the one file every live
session runs depend at runtime on a second tree, and force a choice between the
install's `worker/` and the editing clone's — which legitimately differ across a
deploy. The copy that rotted was the UNPINNED one: `derive_role.sh` has been a
third copy since T-0041 and never drifted, because `test_derive_role.sh`
cross-checks it against live python on a shared table. This module is that same
net for the python-CLI copy, and bsq's docstring now says "hand-maintained copy"
instead of claiming a delegation it never performed.

WHAT IT CHECKS, in order of strength:
  1. one WINDOW TABLE driven through BOTH implementations, answers must be
     identical (the DoD's "not two independent tests that can drift apart").
  2. the regex SOURCES and their PRECEDENCE ORDER match the worker's — this
     covers window shapes the table does not happen to contain, which a
     behaviour table alone never can.
  3. every role `_derive_role` can emit is keyed in `_ROLE_FILES` AND its
     contract file exists on disk.
  4. the table is a SUPERSET of the bash mirror test's cases, so the two
     tables cannot drift apart either.
  5. a POSITIVE CONTROL (T-0740): the pre-fix bsq implementation is
     reconstructed and required to FAIL check 1. A table that passes with the
     defect present pins nothing.

`bsq` is extensionless — loaded via SourceFileLoader, as in
`scripts/cli/test_bsq_role_ssot.py`.
"""
from __future__ import annotations

import importlib.util
import re
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from bot_squad_worker import sessions

_REPO = Path(__file__).resolve().parents[2]
_BSQ_PATH = _REPO / "scripts" / "cli" / "bsq"


def _load_bsq():
    loader = SourceFileLoader("bsq_mod_role_mirror", str(_BSQ_PATH))
    spec = importlib.util.spec_from_loader("bsq_mod_role_mirror", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bsq = _load_bsq()


# The shared window table. Every entry is driven through BOTH implementations.
# `expected` documents the contract; the drift check does not depend on it, so a
# wrong expectation cannot mask a divergence (and vice-versa).
WINDOWS: tuple[tuple[str, str], ...] = (
    # operator
    ("operator", "operator"),
    ("bot-squad-operator", "operator"),
    ("OPERATOR", "operator"),
    ("xoperator", "dev"),                       # no separator ⟹ not a marker
    # prod-teamlead (T-0197) — the arm bsq was missing
    ("prod-tl", "prod-teamlead"),
    ("bot-squad-prod-tl", "prod-teamlead"),
    ("bot_squad_prod_teamlead", "prod-teamlead"),
    ("prod-teamlead", "prod-teamlead"),
    ("PROD-TL", "prod-teamlead"),
    ("prod-ops-tl", "teamlead"),                # no `prod` adjacent to `-tl`
    # qa (T-0197) — the other arm bsq was missing
    ("qa", "qa"),
    ("bot-squad-qa", "qa"),
    ("signal_tracker_qa", "qa"),
    ("vodqa", "dev"),                           # no separator ⟹ dev
    ("qa-runner", "dev"),                       # marker must be a SUFFIX
    # user-conversation (T-0478)
    ("user-conversation", "user-conversation"),
    ("user_conversation", "user-conversation"),
    ("gu_a1b2c3-user-conversation", "user-conversation"),
    ("gu_qa-user-conversation", "user-conversation"),  # suffix beats inner `qa`
    ("user-conversation-extra", "dev"),
    ("conversation", "dev"),
    # teamlead
    ("tl", "teamlead"),
    ("multi_server-TL", "teamlead"),
    ("signal-tracker_teamlead", "teamlead"),
    ("operator-as-distinct-role-not-teamlead", "teamlead"),
    ("total", "dev"),                           # ends in `al`, not a tl marker
    # dev / degenerate
    ("heatmaps", "dev"),
    ("adhoc-notes", "dev"),
    ("bsq-role-from-sid-has-diverged-from-the-", "dev"),
    ("", "dev"),
)

# bsq's public entry point takes a SID, the worker's takes a window. Both halves
# are exercised: `_role_from_window` against `_derive_role` directly, and
# `_role_from_sid` against a synthesised SID, so the SID-parsing step cannot
# quietly change the answer either.
_SID_USER = "almdudleer"


def _sid_for(window: str) -> str:
    return f"S-{_SID_USER}-{window}-p42"


@pytest.mark.parametrize("window,expected", WINDOWS, ids=[w or "(empty)" for w, _ in WINDOWS])
def test_both_implementations_agree(window: str, expected: str) -> None:
    """1. THE DRIFT CHECK — one table, both implementations, identical answers."""
    worker_role = sessions._derive_role(window, None, None)
    bsq_role = bsq._role_from_window(window)
    assert bsq_role == worker_role, (
        f"role derivation DRIFTED for window {window!r}: "
        f"bsq._role_from_window={bsq_role!r} vs sessions._derive_role={worker_role!r}"
    )
    assert worker_role == expected, (
        f"contract changed for window {window!r}: both implementations agree on "
        f"{worker_role!r} but this table expects {expected!r} — if the change is "
        f"intended, update the table AND scripts/hooks/test_derive_role.sh"
    )


@pytest.mark.parametrize("window,expected", WINDOWS, ids=[w or "(empty)" for w, _ in WINDOWS])
def test_sid_entry_point_agrees_too(window: str, expected: str) -> None:
    """1b. The SID-shaped entry point bsq actually calls resolves the same.

    An empty window makes a SID with no window segment; it is exercised through
    `_role_from_window` above and skipped here rather than asserted on a
    malformed SID.
    """
    if not window:
        pytest.skip("empty window does not form a SID")
    assert bsq._role_from_sid(_sid_for(window)) == expected


def test_empty_and_unparseable_sids_are_dev() -> None:
    assert bsq._role_from_sid(None) == "dev"
    assert bsq._role_from_sid("") == "dev"
    assert bsq._role_from_sid("deploy_monitor") == "dev"  # the synthetic sender


# ---------------------------------------------------------------------------
# 2. The regexes themselves — covers window shapes the table does not contain.
# ---------------------------------------------------------------------------

# The worker keeps its markers as named module-level regexes and applies them in
# a hardcoded if-chain; bsq keeps an ordered tuple. This is the one place the
# correspondence is written down, and it is compared against BOTH sides below.
WORKER_MARKERS_IN_PRECEDENCE_ORDER: tuple[tuple[str, str], ...] = (
    ("operator", "_OPERATOR_WINDOW_RE"),
    ("prod-teamlead", "_PROD_TL_WINDOW_RE"),
    ("qa", "_QA_WINDOW_RE"),
    ("user-conversation", "_USERCONV_WINDOW_RE"),
    ("teamlead", "_TL_WINDOW_RE"),
)


def test_marker_patterns_and_precedence_are_identical() -> None:
    """bsq's ordered marker table equals the worker's, pattern and flags."""
    bsq_table = bsq._ROLE_WINDOW_RES
    assert [role for role, _ in bsq_table] == \
        [role for role, _ in WORKER_MARKERS_IN_PRECEDENCE_ORDER], \
        "bsq's marker PRECEDENCE diverged from sessions._derive_role's if-chain"

    for (role, bsq_re), (_, attr) in zip(bsq_table, WORKER_MARKERS_IN_PRECEDENCE_ORDER):
        worker_re = getattr(sessions, attr)
        assert bsq_re.pattern == worker_re.pattern, (
            f"{role}: bsq pattern {bsq_re.pattern!r} != worker {attr} "
            f"{worker_re.pattern!r}"
        )
        assert bsq_re.flags == worker_re.flags, (
            f"{role}: bsq flags {bsq_re.flags} != worker {attr} {worker_re.flags}"
        )


def test_prod_tl_precedes_plain_tl():
    """T-0197's precedence, asserted on the ORDER not just on one window.

    `worker/tests/test_sessions.py` pins this for the worker; bsq had the two
    arms in the opposite order, which is the whole defect. Asserting the index
    relationship catches a reordering even if every table window still happens
    to resolve correctly.
    """
    order = [role for role, _ in bsq._ROLE_WINDOW_RES]
    assert order.index("prod-teamlead") < order.index("teamlead"), (
        "a `…-prod-tl` window also ends in `tl` — the prod-TL arm must be tried "
        "first or prod-TLs silently get teamlead.md (T-0778)"
    )


# ---------------------------------------------------------------------------
# 3. The second half: a correct role still needs a contract file.
# ---------------------------------------------------------------------------

def test_every_derivable_role_has_a_contract_file() -> None:
    """`_ROLE_FILES` must key every role either implementation can return.

    Without this, a fixed `_role_from_sid` still hands prod-TL/qa `dev.md` via
    cmd_brief's `.get(role, "dev.md")` fallback — the second half of T-0778.
    Roles are taken from the WORKER (the canonical set), so adding a role there
    without teaching bsq about it fails HERE rather than silently downgrading a
    live session to dev.
    """
    roles = {sessions._derive_role(w, None, None) for w, _ in WINDOWS}
    assert {"operator", "prod-teamlead", "qa", "user-conversation", "teamlead", "dev"} <= roles, \
        "the window table stopped covering every role — it is the input to this check"

    roles_dir = _REPO / "api" / "app" / "resources" / "roles"
    for role in sorted(roles):
        assert role in bsq._ROLE_FILES, (
            f"role {role!r} is derivable but has no _ROLE_FILES entry — "
            f"cmd_brief would serve it dev.md"
        )
        contract = roles_dir / bsq._ROLE_FILES[role]
        assert contract.is_file(), f"role {role!r} maps to a missing contract {contract}"


def test_spawn_role_choices_are_derived_from_role_files() -> None:
    """T-0778 decision: `--role` choices are DERIVED, not a fourth copy.

    A literal choices list is the same divergence class as the one this module
    pins, and it had already opened — `--role user-conversation` was rejected at
    the parser while `_ROLE_FILES` mapped it.
    """
    assert set(bsq._SPAWNABLE_ROLES) == set(bsq._ROLE_FILES)
    parser = bsq.build_parser()
    for verb in ("spawn", "cluster-launch"):
        action = _role_action(parser, verb)
        assert set(action.choices) == set(bsq._ROLE_FILES), (
            f"`bsq {verb} --role` choices diverged from _ROLE_FILES"
        )


def _role_action(parser, verb: str):
    subparsers = next(a for a in parser._actions if hasattr(a, "choices") and a.choices
                      and verb in getattr(a, "choices", {}))
    return next(a for a in subparsers.choices[verb]._actions if a.dest == "role")


# ---------------------------------------------------------------------------
# 4. The bash mirror's table cannot drift away from this one.
# ---------------------------------------------------------------------------

def test_window_table_covers_the_bash_mirror_cases() -> None:
    """Every window `test_derive_role.sh` exercises is in this table too.

    Three copies of one rule are checked by two tests; if their tables diverge,
    a case added to one is not a case the other defends. Parsed rather than
    duplicated, so adding a bash case forces it in here.
    """
    sh = (_REPO / "scripts" / "hooks" / "test_derive_role.sh").read_text(encoding="utf-8")
    block = re.search(r"^CASES=\((.*?)^\)", sh, re.S | re.M)
    assert block, "could not find the CASES array in test_derive_role.sh"
    bash_windows = {
        m.group(1).split("\t", 1)[0]
        for m in re.finditer(r'^\s*"([^"]+)"', block.group(1), re.M)
    }
    assert len(bash_windows) >= 20, (
        f"only parsed {len(bash_windows)} bash cases — the extractor is broken, "
        f"and a broken extractor returns a well-formed EMPTY result that passes"
    )
    missing = sorted(bash_windows - {w for w, _ in WINDOWS})
    assert not missing, (
        f"windows covered by the bash mirror test but not by this one: {missing}"
    )


# ---------------------------------------------------------------------------
# 5. POSITIVE CONTROL (T-0740) — the table must REJECT the pre-fix code.
# ---------------------------------------------------------------------------

def _pre_fix_role_from_window(window: str | None) -> str:
    """Verbatim reconstruction of bsq's `_role_from_sid` body before T-0778."""
    w = (window or "").lower()
    if w == "operator" or re.search(r"[-_]operator$", w):
        return "operator"
    if w == "user-conversation" or re.search(r"[-_]user[-_]conversation$", w):
        return "user-conversation"
    if w in ("tl", "teamlead") or re.search(r"[-_](tl|teamlead)$", w):
        return "teamlead"
    return "dev"


def test_positive_control_table_catches_the_pre_fix_implementation() -> None:
    """The pre-fix implementation must FAIL the drift check.

    The expected divergence set is the MEASURED one, and it is one role wider
    than T-0778 reported. Running this control is what surfaced the third:

      * `…-prod-tl` → `teamlead` instead of `prod-teamlead`  (ticket)
      * `…-qa`      → `dev` instead of `qa`                  (ticket)
      * `user_conversation` → `dev` instead of `user-conversation`  (NEW)

    The third is the same root cause found on a case nobody had enumerated:
    pre-fix bsq spelled every marker as
    ``window == "<hyphen-form>" or re.search(r"[-_]<marker>$")``, so the bare
    UNDERSCORE spelling with no prefix matched neither arm. The worker's
    ``(?:^|[-_])user[-_]conversation$`` matches it and `derive_role.sh` lists it
    as an explicit case — bsq was the only one of the three copies that got it
    wrong. Low reach in practice (a real UC window is
    ``<gu_id>-user-conversation``, which the old arm did match), but the
    correct answer changes, so it is pinned rather than tolerated.
    """
    divergent = {
        w: (_pre_fix_role_from_window(w), sessions._derive_role(w, None, None))
        for w, _ in WINDOWS
        if _pre_fix_role_from_window(w) != sessions._derive_role(w, None, None)
    }
    assert divergent, (
        "the window table does not discriminate: the PRE-FIX bsq passes it, so "
        "it would have pinned nothing"
    )
    got_wrong = {old for old, _ in divergent.values()}
    should_be = {new for _, new in divergent.values()}
    assert should_be == {"prod-teamlead", "qa", "user-conversation"}, (
        f"control caught a different divergence set than the measured one: {should_be}"
    )
    assert got_wrong == {"teamlead", "dev"}, (
        f"unexpected pre-fix answers: {got_wrong}"
    )
    assert divergent["user_conversation"] == ("dev", "user-conversation")
