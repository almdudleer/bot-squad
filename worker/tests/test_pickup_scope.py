"""T-0829 — the drive SCOPE axis applied to the pickup queue (design D-0069).

The ask, verbatim (stakeholder, 2026-07-29T12:20:46Z):

    надо предусмотреть разные режимы драйва оператора:
    • Закрыть все задачи в Open / Reopened
    • Закрыть все задачи в In Progress
    • Закрыть все задачи вообще, включая backlog

Three bullets, one axis. The third is already ``pickup.PICKUP_STATUSES``, so the
other two are NARROWING FILTERS over the queue that already exists and the
default is the widest.

What these tests are shaped by
------------------------------
**Every scope value is pinned BOTH WAYS.** A filter that silently does nothing
is indistinguishable from a correct one if you only ever ask it what it lets
through — the same reasoning ``test_pickup.py`` opens with. So each scope gets a
fixture where in-scope work exists and surfaces, AND a fixture where only
OUT-of-scope work exists and the queue comes back empty with the exclusion
reason NAMED. The second half is the one that catches a no-op filter.

**The no-op is pinned as loudly as the filtering.** Absent config must be
today's behaviour bit-identical: the point of shipping this is that it changes
nothing until he sets a mode.

**The residue fixtures are T-0800's acceptance inputs.** ``scope_with_residue``
and ``scope_truly_empty`` are exported as fixtures on purpose (TL
``S-almdudleer-drive-modes-p534``, 2026-07-30): the stall alert has to be
written against the same inputs this lane pinned, not against a re-derivation of
them. A scope whose pickup band is empty while in-scope triage work remains is
NOT a finished board, and «ВСЁ СДЕЛАНО, ПРОВЕРЯЙ, МЫ ПРОСТАИВАЕМ» posted over one
is the lie this file exists to make impossible.
"""
from __future__ import annotations

import json

import pytest

from bot_squad_worker import pace
from bot_squad_worker import pickup
from bot_squad_worker import sessions as S
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

NOW = 1_780_000_000.0
DAY = 86400.0


def _iso(epoch: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def board(tmp_path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    for sub in ("backlog", "sessions"):
        (cfg.data_dir / slug / sub).mkdir(parents=True, exist_ok=True)
    # No live panes: liveness is not what this file is about, and a stray tmux
    # scan would make the board depend on the host.
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    return cfg, slug


def _write_task(cfg, slug, tid, *, status="open", priority="P2", title=None,
                updated=None, **extra):
    fields = {
        "title": title if title is not None else f"{tid} a real unit of work",
        "status": status,
        "priority": priority,
        "updated": updated or _iso(NOW - DAY),
    }
    fields.update(extra)
    lines = "\n".join(f"{k}: {v}" for k, v in fields.items() if v is not None)
    p = cfg.data_dir / slug / "backlog" / f"{tid}-x.md"
    p.write_text(f"---\nid: {tid}\n{lines}\n---\n\nbody\n")
    return p


def _ids(rows):
    return sorted(r["id"] for r in rows)


def _rejects(q):
    return {r["id"]: r["reject"] for r in q["excluded"]}


# ---------------------------------------------------------------------------
# The mapping itself — the table in D-0069, pinned against pace's closed set
# ---------------------------------------------------------------------------

def test_the_scope_values_are_exactly_the_ones_the_config_can_store():
    """The two modules hold two halves of one closed set: ``pace`` validates the
    stored value, ``pickup`` maps it to statuses. A scope added to one and not
    the other is the drift ``resolve_drive_scope``'s ``unknown-scope`` problem
    exists to survive — this pins that it never has to."""
    assert set(pickup.SCOPE_STATUSES) == set(pace.DRIVE_SCOPES)
    assert pickup.DEFAULT_SCOPE == pace.DRIVE_DEFAULTS["scope"]


def test_all_is_the_pickup_statuses_which_is_why_the_default_cannot_narrow():
    """D-0069: «Закрыть все задачи вообще, включая backlog» IS today's queue.
    The no-op property rests on this identity, not on a branch."""
    assert pickup.SCOPE_STATUSES["all"] == pickup.PICKUP_STATUSES


@pytest.mark.parametrize("scope", sorted(pickup.SCOPE_STATUSES))
def test_no_scope_can_widen_the_queue_or_readmit_totest(scope):
    """A scope may only ever REMOVE statuses. ``totest`` is review work awaiting
    a verifier, not work awaiting a doer, and no mode gets to hand it to a dev."""
    statuses = pickup.SCOPE_STATUSES[scope]
    assert statuses <= pickup.PICKUP_STATUSES
    assert "totest" not in statuses
    assert "closed" not in statuses


# ---------------------------------------------------------------------------
# DoD 4 — absent scope is today's behaviour, bit-identical
# ---------------------------------------------------------------------------

# T-0889: these are derived from ``pickup.PICKUP_STATUSES``, not hand-typed.
# ``_full_board`` already builds its tickets from that set, so a hardcoded
# expectation pins a set the fixture no longer produces — which is exactly what
# broke here when ``paused`` joined the band: the fixture grew a T-paused and six
# assertions still expected the old four.
def _all_pickup_ids():
    return [f"T-{s}" for s in sorted(pickup.PICKUP_STATUSES)]


def _full_board_size():
    """Every pickup ticket plus the two that are in no scope (totest, closed)."""
    return len(pickup.PICKUP_STATUSES) + 2


def _full_board(cfg, slug):
    """One takeable ticket per pickup status, plus the two that are in no scope."""
    for status in sorted(pickup.PICKUP_STATUSES):
        _write_task(cfg, slug, f"T-{status}", status=status, priority="P2")
    _write_task(cfg, slug, "T-totest", status="totest")
    _write_task(cfg, slug, "T-closed", status="closed")


def test_with_no_drive_block_the_queue_is_exactly_what_it_was_before(board):
    """THE no-op test. Nothing configured -> every pickup status still surfaces,
    the scope exclusion never fires, and the counts are the pre-T-0829 ones."""
    cfg, slug = board
    _full_board(cfg, slug)

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert _ids(q["pickup"]) == _all_pickup_ids()
    assert q["counts"] == {
        "pickup": len(pickup.PICKUP_STATUSES),
        "triage": 0,
        "excluded": 2,  # totest + closed, the two in no scope
        "board": _full_board_size(),
    }
    assert not [r for r in q["excluded"]
                if str(r["reject"]).startswith("out-of-drive-scope")]
    assert q["drive_scope"]["out_of_scope"] == 0
    assert q["drive_scope"]["scope"] == "all"
    assert q["drive_scope"]["configured"] is False
    assert q["drive_scope"]["problem"] is None


def test_an_unset_config_and_an_explicit_all_drive_identically(board):
    """Clearing the mode must be equivalent to setting the widest one. If this
    ever diverges, ``configured`` has leaked into BEHAVIOUR — it is a visibility
    flag only (T-0828's DoD, stated by its owner)."""
    cfg, slug = board
    _full_board(cfg, slug)

    unset = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    explicit = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope="all")
    pace.set_drive(cfg, slug, scope="all", set_by="stakeholder")
    stored = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    for other in (explicit, stored):
        for key in ("pickup", "triage", "excluded", "counts"):
            assert other[key] == unset[key]
        assert other["drive_scope"]["statuses"] == unset["drive_scope"]["statuses"]


def test_the_whole_board_is_still_counted_when_a_scope_filters(board):
    """Out-of-scope tickets are EXCLUDED with the reason named, never dropped:
    ``counts.board`` stays the board, so the filter is auditable."""
    cfg, slug = board
    _full_board(cfg, slug)

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope="in_progress")

    assert q["counts"]["board"] == _full_board_size()
    assert (
        q["counts"]["pickup"] + q["counts"]["triage"] + q["counts"]["excluded"]
        == _full_board_size()
    )


# ---------------------------------------------------------------------------
# DoD 7 — every scope value pinned BOTH ways
# ---------------------------------------------------------------------------

#: scope -> (statuses that must surface, statuses that must be filtered out)
_SCOPE_CASES = {
    "open_reopened": (["open", "reopened"], ["in_progress", "planned"]),
    # T-0889: the `in_progress` scope is his BOARD COLUMN, not the bare status —
    # `paused` rolls up into that same canonical column, and it is where the
    # auto-pause puts the abandoned in_progress tickets this scope exists to
    # close. Pinned here rather than derived from the constant so that draining
    # the scope stays a RED test, not a silently-restated one.
    "in_progress": (["in_progress", "paused"], ["open", "reopened", "planned"]),
    # T-0889: "all" IS the pickup band by definition (see
    # test_all_is_the_pickup_statuses_...), so it derives. The two NAMED scopes
    # above stay hand-written on purpose — spelling them out is what makes them a
    # real expectation rather than a restatement of the code under test.
    "all": (sorted(pickup.PICKUP_STATUSES), []),
}


@pytest.mark.parametrize("scope", sorted(_SCOPE_CASES))
def test_in_scope_work_surfaces(board, scope):
    cfg, slug = board
    _full_board(cfg, slug)

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope=scope)

    inside, _ = _SCOPE_CASES[scope]
    assert _ids(q["pickup"]) == [f"T-{s}" for s in sorted(inside)]
    assert q["drive_scope"]["scope"] == scope
    assert q["drive_scope"]["statuses"] == sorted(inside)


@pytest.mark.parametrize("scope", sorted(_SCOPE_CASES))
def test_a_board_of_only_out_of_scope_work_yields_an_empty_queue(board, scope):
    """THE half that catches a filter which silently does nothing. Note ``all``
    is in here too, with an empty out-of-scope set — it is a genuine control:
    under the default the board is NOT empty, which is what proves the other two
    emptied it by filtering rather than by breaking the sweep."""
    cfg, slug = board
    _, outside = _SCOPE_CASES[scope]
    for status in outside:
        _write_task(cfg, slug, f"T-{status}", status=status, priority="P1")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope=scope)

    if not outside:  # the 'all' control
        assert q["counts"]["board"] == 0
        return
    assert q["pickup"] == []
    assert q["counts"]["pickup"] == 0
    assert _rejects(q) == {f"T-{s}": f"out-of-drive-scope:{s}" for s in outside}
    assert q["drive_scope"]["out_of_scope"] == len(outside)


@pytest.mark.parametrize("scope", sorted(_SCOPE_CASES))
def test_totest_is_out_of_every_scope_and_keeps_its_own_reason(board, scope):
    """A scope narrows; it never readmits. And the reason stays ``awaiting-review``
    rather than becoming an out-of-scope one, so the two are never confused."""
    cfg, slug = board
    _write_task(cfg, slug, "T-t", status="totest")
    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope=scope)
    assert q["pickup"] == []
    assert _rejects(q) == {"T-t": "awaiting-review"}


# ---------------------------------------------------------------------------
# DoD 3 — the scope comes from the pace config, and nothing else stores it
# ---------------------------------------------------------------------------

def test_the_scope_is_read_from_the_pace_config(board):
    cfg, slug = board
    _full_board(cfg, slug)
    pace.set_drive(cfg, slug, scope="open_reopened", set_by="stakeholder",
                   source_text="закончить всё что в опен")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert _ids(q["pickup"]) == ["T-open", "T-reopened"]
    ds = q["drive_scope"]
    assert ds["scope"] == "open_reopened"
    assert ds["source"] == "config"
    assert ds["configured"] is True
    assert ds["set_by"] == "stakeholder"
    assert ds["source_text"] == "закончить всё что в опен"


def test_there_is_no_second_store_only_pace_json(board):
    """The fork both parent tickets warn about: a parallel settings surface.
    Setting the mode writes ONE file, and it is pace's."""
    cfg, slug = board
    before = {p for p in (cfg.data_dir / slug).rglob("*") if p.is_file()}
    pace.set_drive(cfg, slug, scope="in_progress", set_by="stakeholder")
    _write_task(cfg, slug, "T-1", status="in_progress")
    pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    new = {p for p in (cfg.data_dir / slug).rglob("*")
           if p.is_file() and p not in before and p.suffix != ".md"}
    assert {p.name for p in new} == {"pace.json"}
    assert json.loads((cfg.data_dir / slug / "_worker" / "pace" / "pace.json")
                      .read_text())["drive"]["scope"] == "in_progress"


def test_an_explicit_scope_overrides_the_stored_one(board):
    cfg, slug = board
    _full_board(cfg, slug)
    pace.set_drive(cfg, slug, scope="open_reopened", set_by="stakeholder")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope="in_progress")

    # Derived from the scope, not hand-typed: this test is about which scope
    # WINS, so pinning that scope's membership here would just be a second place
    # to update (T-0889 already broke six such assertions in this file).
    assert _ids(q["pickup"]) == sorted(
        f"T-{s}" for s in pickup.SCOPE_STATUSES["in_progress"])
    assert q["drive_scope"]["source"] == "explicit"


# ---------------------------------------------------------------------------
# Never narrow on a value we could not read — and NAME the problem
# ---------------------------------------------------------------------------

def test_a_stored_scope_pace_rejects_widens_and_names_the_raw_value(board):
    """He must see the typo he made, not the word "invalid". And an unreadable
    setting must never HIDE work: it widens to the default, which costs a glance,
    where narrowing would cost him a ticket nobody is told about."""
    cfg, slug = board
    _full_board(cfg, slug)
    path = cfg.data_dir / slug / "_worker" / "pace"
    path.mkdir(parents=True, exist_ok=True)
    (path / "pace.json").write_text(json.dumps({"drive": {"scope": "opne"}}))

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert q["drive_scope"]["scope"] == "all"
    assert "opne" in q["drive_scope"]["problem"]
    assert _ids(q["pickup"]) == _all_pickup_ids()
    assert "DID NOT TAKE EFFECT" in pickup.pickup_brief(q)


def test_a_scope_pace_knows_and_pickup_does_not_widens_and_names_itself(board, monkeypatch):
    """The drift case the parity test above prevents — pinned anyway, because
    the cost of getting it wrong is a crash inside the operator re-drive tick."""
    cfg, slug = board
    _full_board(cfg, slug)
    monkeypatch.setattr(pace, "read_drive", lambda c, s: {
        "scope": "closed_only", "configured": True, "invalid": {},
        "set_by": None, "set_at": None, "source_text": None,
    })

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert q["drive_scope"]["scope"] == "all"
    assert q["drive_scope"]["problem"] == "unknown-scope:'closed_only'"
    assert q["counts"]["pickup"] == len(pickup.PICKUP_STATUSES)


def test_an_unreadable_config_never_empties_the_board(board, monkeypatch):
    """``read_drive`` is documented never to raise. If it ever does, the queue
    still has to answer — a config read must not be able to make the board look
    empty, which is the failure mode with the highest blast radius here."""
    cfg, slug = board
    _full_board(cfg, slug)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pace, "read_drive", _boom)

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert q["counts"]["pickup"] == len(pickup.PICKUP_STATUSES)
    assert q["drive_scope"]["scope"] == "all"
    assert q["drive_scope"]["problem"] == "drive-config-unreadable:RuntimeError"


# ---------------------------------------------------------------------------
# DoD 6 — THE HONESTY CONSTRAINT. These two fixtures are T-0800's inputs.
# ---------------------------------------------------------------------------

@pytest.fixture()
def scope_with_residue(board):
    """A configured scope whose PICKUP band is empty while in-scope work remains.

    T-0800's alert must NOT fire here. Both residue tickets are ``open`` — inside
    the ``open_reopened`` scope — and both land in the triage band for reasons
    T-0783a MEASURED and this lane is forbidden to re-derive: a title that
    contradicts its own priority field, and a ticket stale past the window
    (staleness measures the last WRITE, not the last work). The out-of-scope
    ``in_progress`` ticket is there to prove the residue count is scope-relative.
    """
    cfg, slug = board
    pace.set_drive(cfg, slug, scope="open_reopened", set_by="stakeholder",
                   source_text="закончить всё что в опен")
    _write_task(cfg, slug, "T-0388", status="open", priority="P1",
                title="P3 DEFERRED: auto-create TG supergroup")
    _write_task(cfg, slug, "T-0612", status="open", priority="P1",
                title="watchrobot RV pair-trading program", updated=_iso(NOW - 24 * DAY))
    _write_task(cfg, slug, "T-elsewhere", status="in_progress", priority="P1")
    return cfg, slug


@pytest.fixture()
def scope_truly_empty(board):
    """The other half: the configured scope is empty of EVERYTHING. Only here may
    an alert say the scope is done."""
    cfg, slug = board
    pace.set_drive(cfg, slug, scope="open_reopened", set_by="stakeholder")
    _write_task(cfg, slug, "T-done", status="closed")
    _write_task(cfg, slug, "T-review", status="totest")
    _write_task(cfg, slug, "T-elsewhere", status="in_progress", priority="P1")
    return cfg, slug


def test_in_scope_triage_residue_is_visible_in_the_scopes_own_output(scope_with_residue):
    cfg, slug = scope_with_residue

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert q["pickup"] == []
    assert q["drive_scope"][pickup.TRIAGE_IN_SCOPE_KEY] == 2
    assert _ids(q["triage"]) == ["T-0388", "T-0612"]


def test_an_empty_pickup_band_alone_would_call_a_scope_done_that_is_not(scope_with_residue):
    """The failure this DoD item exists to prevent, asserted as the wrong
    conclusion rather than as the right one. A consumer reading only "the pickup
    band is empty" — which is exactly T-0800's trigger — concludes DONE over a
    scope holding two tickets that still need a human. The residue field is what
    stops «ВСЁ СДЕЛАНО» being posted over an unfinished board."""
    cfg, slug = scope_with_residue

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    naive_verdict_is_done = not q["pickup"]
    honest_verdict_is_done = (not q["pickup"]
                              and not q["drive_scope"][pickup.TRIAGE_IN_SCOPE_KEY])
    assert naive_verdict_is_done is True
    assert honest_verdict_is_done is False


def test_a_genuinely_empty_scope_reports_zero_residue(scope_truly_empty):
    """The inverse, or the test above would pass on a field hardcoded non-zero."""
    cfg, slug = scope_truly_empty

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert q["pickup"] == []
    assert q["drive_scope"][pickup.TRIAGE_IN_SCOPE_KEY] == 0
    assert q["counts"]["board"] == 3  # the work exists; it is not in this scope


def test_the_residue_is_scope_relative_not_a_board_wide_triage_count(board):
    """An out-of-scope triage ticket must NOT inflate the residue: T-0800 would
    then refuse to report a genuinely finished scope, and an alert that never
    fires is the same defect as one that always does."""
    cfg, slug = board
    pace.set_drive(cfg, slug, scope="open_reopened", set_by="stakeholder")
    _write_task(cfg, slug, "T-0388", status="planned", priority="P1",
                title="P3 DEFERRED: auto-create TG supergroup")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert q["drive_scope"][pickup.TRIAGE_IN_SCOPE_KEY] == 0
    assert _rejects(q) == {"T-0388": "out-of-drive-scope:planned"}


@pytest.mark.parametrize("scope", sorted(_SCOPE_CASES))
def test_the_residue_field_always_equals_the_triage_band(board, scope):
    """The invariant that makes the field safe to depend on: it is the triage
    band's size BY CONSTRUCTION (an out-of-scope ticket is excluded, and a reject
    outranks every judgement signal). Pinned so the two can never drift into two
    different numbers for the same question."""
    cfg, slug = board
    _full_board(cfg, slug)
    _write_task(cfg, slug, "T-stale", status="open", priority="P1",
                updated=_iso(NOW - 30 * DAY))
    _write_task(cfg, slug, "T-cont", status="in_progress", priority="P1",
                kind="initiative")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope=scope)

    assert q["drive_scope"][pickup.TRIAGE_IN_SCOPE_KEY] == q["counts"]["triage"]
    assert all(r["band"] == pickup.BAND_TRIAGE for r in q["triage"])


# ---------------------------------------------------------------------------
# DoD 5 — the brief NAMES the active scope
# ---------------------------------------------------------------------------

def test_the_brief_names_the_scope_and_the_words_that_set_it(scope_with_residue):
    """«я просил закончить всё что в опен, но видимо это не интерпретировалось» —
    the brief prints the mode AND his phrase, so an operator incarnation states
    the scope it drives under instead of inferring one."""
    cfg, slug = scope_with_residue

    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW))

    assert brief.startswith("DRIVE SCOPE: open_reopened")
    assert "open, reopened" in brief
    assert "закончить всё что в опен" in brief
    assert "stakeholder" in brief


def test_the_brief_states_the_residue_so_empty_is_not_read_as_done(scope_with_residue):
    cfg, slug = scope_with_residue
    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW))
    assert "2 ticket(s) INSIDE this scope need triage" in brief
    assert "NOT 'the scope is done'" in brief
    assert pickup.EMPTY_PICKUP_LINE in brief


def test_the_brief_says_how_many_tickets_the_scope_withheld(scope_with_residue):
    cfg, slug = scope_with_residue
    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW))
    assert "1 board ticket(s) are OUT of this scope" in brief


def test_the_brief_says_when_nothing_is_set_that_the_default_is_the_widest(board):
    """The unset case is STATED, not omitted — an operator handed no scope line
    would be back to inferring the mode, which is the complaint."""
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="open", priority="P1")

    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW))

    assert brief.startswith("DRIVE SCOPE: all")
    assert "never set" in brief and "WIDEST" in brief
    assert "need triage" not in brief and "OUT of this scope" not in brief


def test_a_clean_in_scope_board_carries_no_residue_or_withheld_line(board):
    cfg, slug = board
    pace.set_drive(cfg, slug, scope="open_reopened", set_by="stakeholder")
    _write_task(cfg, slug, "T-1", status="open", priority="P1")

    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW))

    assert "need triage" not in brief
    assert "OUT of this scope" not in brief
    assert "DID NOT TAKE EFFECT" not in brief
    assert "T-1" in brief


def test_a_queue_dict_without_a_scope_block_still_renders(board):
    """``pickup_brief`` is called on hand-built dicts in other lanes' tests and on
    whatever a future caller passes. A missing block must not raise."""
    assert pickup.pickup_brief({"pickup": [], "triage": []}) == pickup.EMPTY_PICKUP_LINE


def test_the_scope_block_is_json_serialisable(board):
    """It travels over the worker socket inside the ``pickup_queue`` action's
    result — a frozenset in there would 500 the action, not fail a test."""
    cfg, slug = board
    pace.set_drive(cfg, slug, scope="in_progress", set_by="stakeholder")
    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    assert json.loads(json.dumps(q["drive_scope"]))["scope"] == "in_progress"


# ---------------------------------------------------------------------------
# T-0889 DoD 4 — the operator's working set, answered and pinned
# ---------------------------------------------------------------------------
# "We need to define clearly what's the working set for the operator ... maybe
# only in-progress/paused, without open" (2026-08-04). Answered as his own
# sentence: started-and-unfinished work, both halves of it. These tests exist so
# the answer is a machine-checked fact rather than a paragraph in a ticket.

def test_the_operator_working_set_is_in_progress_plus_paused_without_open():
    assert pickup.OPERATOR_WORKING_SET_STATUSES == {"in_progress", "paused"}
    for backlog_status in ("open", "planned", "reopened"):
        assert backlog_status not in pickup.OPERATOR_WORKING_SET_STATUSES
    for done_ish in ("totest", "closed"):
        assert done_ish not in pickup.OPERATOR_WORKING_SET_STATUSES


def test_the_working_set_is_a_subset_of_the_pickup_band():
    """Every member must be takeable, or the operator watches work no session
    may pick up — the "unpickable paused ticket" failure one level down."""
    assert pickup.OPERATOR_WORKING_SET_STATUSES <= pickup.PICKUP_STATUSES


def test_the_in_progress_drive_scope_is_the_working_set(board):
    """The regression this prevents: the auto-pause moves abandoned tickets out
    of ``in_progress``, so a drive configured to «закрыть все задачи в In
    Progress» would have quietly emptied out on the first reconcile tick."""
    assert pickup.SCOPE_STATUSES["in_progress"] == pickup.OPERATOR_WORKING_SET_STATUSES

    cfg, slug = board
    _write_task(cfg, slug, "T-was-abandoned", status="paused", priority="P1")
    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW, scope="in_progress")
    assert _ids(q["pickup"]) == ["T-was-abandoned"]
