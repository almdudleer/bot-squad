"""Tests for T-0783a pickup eligibility (``bot_squad_worker.pickup``).

The incident being pinned: the stakeholder reopened a ticket and no session took
it until he pushed the operator by hand. The bar was first stated as "surface
T-0719, not T-0612" and then CORRECTED — T-0612 turned out to be genuine
bot-squad work, so it is a positive fixture here, not an exclusion. Both the
corrected fixture set and the rule that would have wrongly suppressed it get
their own tests, alongside the ladder unit tests.

Every band assertion here is paired with its inverse, because the failure mode
this module is exposed to is a check that cannot go red: a queue builder that
returns everything looks identical to a correct one until you ask it what it
LEAVES OUT. So each exclusion test also asserts the same ticket DOES appear once
the disqualifying fact is removed, and the empty-queue case is asserted as
loudly as the populated one.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bot_squad_worker import pickup
from bot_squad_worker import sessions as S
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

NOW = 1_780_000_000.0  # fixed clock so "days idle" is deterministic
DAY = 86400.0


def _iso(epoch: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _meta(**over) -> dict:
    """A minimal takeable ticket — every field the ladder reads, set to the
    value that keeps it in the pickup band. Tests break ONE thing at a time so a
    band change always has one cause."""
    base = {
        "id": "T-0001",
        "title": "a real unit of work",
        "status": "open",
        "priority": "P2",
        "updated": _iso(NOW - DAY),
    }
    base.update(over)
    return base


def _classify(**over) -> dict:
    return pickup.classify_ticket(_meta(**over), now_epoch=NOW)


# --- the happy path, so every exclusion below has a control ------------------

def test_a_fresh_prioritised_open_ticket_is_takeable():
    row = _classify()
    assert row["band"] == pickup.BAND_PICKUP
    assert row["reject"] is None
    assert row["sanity"] == []
    assert row["effective_priority"] == 2


# --- mechanical exclusions: each with its inverse ----------------------------

@pytest.mark.parametrize("status,reject", [
    ("closed", "terminal:closed"),
    ("totest", "awaiting-review"),
    ("", "not-a-pickup-status:missing"),
    ("bogus", "not-a-pickup-status:bogus"),
    # T-0931: blocked_on_user is deliberately excluded, like paused's inverse —
    # no session can make progress on it until the stakeholder answers, so
    # offering it for pickup would only waste the picker.
    ("blocked_on_user", "not-a-pickup-status:blocked_on_user"),
])
def test_non_pickup_statuses_are_excluded_with_the_reason_named(status, reject):
    row = _classify(status=status)
    assert row["band"] == pickup.BAND_EXCLUDED
    assert row["reject"] == reject


@pytest.mark.parametrize("status", sorted(pickup.PICKUP_STATUSES))
def test_every_pickup_status_is_takeable(status):
    """The inverse of the test above: the four pickup statuses are NOT excluded.
    ``in_progress`` is deliberately among them — an in_progress ticket with no
    live holder is the abandoned-mid-flight case the ticket is about."""
    assert _classify(status=status)["band"] == pickup.BAND_PICKUP


def test_archived_is_excluded_and_unarchived_is_not():
    assert _classify(archived="true")["reject"] == "archived"
    assert _classify(archived="false")["band"] == pickup.BAND_PICKUP


def test_initiative_container_is_triaged_not_excluded_and_a_plain_task_is_neither():
    """An initiative aggregates child work, so a lone dev at the container is the
    wrong move — but T-0551 and T-0556 sat at in_progress with nothing live under
    them for 5 and 11 days, which is the attended-while-abandoned shape. Surfaced
    for triage, never auto-dispatched, and demoted out of the urgent bands."""
    row = _classify(kind="initiative", priority="0")
    assert row["band"] == pickup.BAND_TRIAGE
    assert row["reject"] is None
    assert "initiative-container" in row["sanity"]
    assert row["effective_priority"] == pickup.UNRANKED_BAND
    assert _classify(kind="bug")["band"] == pickup.BAND_PICKUP


def test_a_live_holder_excludes_and_its_absence_restores():
    held = pickup.classify_ticket(_meta(), held_by="S-dev-p1", now_epoch=NOW)
    assert held["band"] == pickup.BAND_EXCLUDED
    assert held["reject"] == "held-by:S-dev-p1"
    free = pickup.classify_ticket(_meta(), held_by=None, now_epoch=NOW)
    assert free["band"] == pickup.BAND_PICKUP


def test_an_open_blocker_excludes_and_an_empty_blocker_list_does_not():
    blocked = pickup.classify_ticket(_meta(), open_blockers=["T-9", "T-8"],
                                     now_epoch=NOW)
    assert blocked["reject"] == "blocked-by:T-8,T-9"
    assert pickup.classify_ticket(_meta(), open_blockers=[],
                                  now_epoch=NOW)["band"] == pickup.BAND_PICKUP


def test_the_first_mechanical_reject_wins_so_the_reason_is_unambiguous():
    """A ticket that is both archived and held reports ONE reason. A list of
    every reject would leave the reader deciding which one mattered."""
    row = pickup.classify_ticket(_meta(archived="true", status="closed"),
                                 held_by="S-dev-p1", now_epoch=NOW)
    assert row["reject"] == "archived"


def test_a_live_child_lane_makes_the_parent_attended_not_abandoned():
    """T-0801/T-0804: in_progress with no direct session, but their lanes are live
    under a TL. Telling an operator to pick up a ticket that has a live dev under
    it is how the operator learns to stop trusting the queue."""
    row = pickup.classify_ticket(_meta(status="in_progress"),
                                 lane_held_by="S-tl-p500", now_epoch=NOW)
    assert row["band"] == pickup.BAND_EXCLUDED
    assert row["reject"] == "lane-live:S-tl-p500"


def test_a_direct_holder_is_reported_ahead_of_a_lane_holder():
    """Distinct reasons, not one merged one: "a dev is on this ticket" and "a dev
    is on a child of this ticket" are different facts for a reader."""
    row = pickup.classify_ticket(_meta(), held_by="S-dev-p1",
                                 lane_held_by="S-tl-p500", now_epoch=NOW)
    assert row["reject"] == "held-by:S-dev-p1"


# --- judgement signals: triaged, never dropped ------------------------------

def test_stale_goes_to_triage_not_excluded_and_names_the_age():
    row = _classify(updated=_iso(NOW - 20 * DAY))
    assert row["band"] == pickup.BAND_TRIAGE
    assert row["reject"] is None, "a stale ticket must stay visible, not be excluded"
    assert row["sanity"] == ["stale:20d>=14d"]
    assert row["idle_days"] == 20.0


def test_just_inside_the_staleness_window_is_still_takeable():
    assert _classify(updated=_iso(NOW - 13.9 * DAY))["band"] == pickup.BAND_PICKUP


def test_the_staleness_window_is_env_tunable(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_PICKUP_STALE_DAYS", "3")
    assert pickup.stale_days() == 3
    assert _classify(updated=_iso(NOW - 5 * DAY))["band"] == pickup.BAND_TRIAGE


def test_a_bad_staleness_env_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_PICKUP_STALE_DAYS", "not-a-number")
    assert pickup.stale_days() == pickup.DEFAULT_STALE_DAYS
    monkeypatch.setenv("BOT_SQUAD_PICKUP_STALE_DAYS", "0")
    assert pickup.stale_days() == pickup.DEFAULT_STALE_DAYS


def test_no_timestamp_at_all_is_an_explicit_unknown_not_a_silent_fresh():
    """An absent ``updated`` must not read as "touched today". The reason is
    stored beside it (``activity-unknown``) so the null is distinguishable from a
    real negative."""
    row = pickup.classify_ticket(
        {"id": "T-1", "title": "t", "status": "open", "priority": "P1"},
        now_epoch=NOW)
    assert row["idle_days"] is None
    assert "activity-unknown" in row["sanity"]
    assert row["band"] == pickup.BAND_TRIAGE


def test_created_is_the_fallback_when_updated_is_absent():
    row = pickup.classify_ticket(
        {"id": "T-1", "title": "t", "status": "open", "priority": "P1",
         "created": _iso(NOW - 2 * DAY)},
        now_epoch=NOW)
    assert row["idle_days"] == 2.0
    assert row["band"] == pickup.BAND_PICKUP


# --- priority sanity: computed, never written back --------------------------

@pytest.mark.parametrize("raw,band,flag", [
    ("P0", 0, None), ("P4", 4, None), ("p1", 1, None),
    (0, 0, None), (3, 3, None), ("2", 2, None),
    ("", None, "priority-missing"), (None, None, "priority-missing"),
    ("high", 1, None), ("Medium", 2, None), ("low", 3, None),
    ("P12", None, "priority-unparseable:P12"),
    ("высокий", None, "priority-unparseable:высокий"),
    ("200", None, "priority-ordering-key:200"),
])
def test_parse_priority_reads_the_shapes_the_board_carries(raw, band, flag):
    """T-0877 REPLACED this case for ``high``, deliberately — it used to pin
    ``priority-unparseable:high`` on the reasoning that an unreadable urgency
    must never be guessed at. Measurement overturned it: ``high`` was not
    unreadable, it was a WRITTEN judgement in the other of the two vocabularies
    the same field carries, and refusing to read it hid 86 tickets on the
    watchrobot board for up to 20.6 days. The guess-nothing principle survives
    intact one step out — ``высокий`` is in no vocabulary and is still reported
    as itself, and ``200`` is the web UI's ordering key, named as that rather
    than folded into a band. See ``test_priority_vocabulary.py``."""
    assert pickup.parse_priority(raw) == (band, flag)


def test_a_title_that_contradicts_the_priority_field_demotes_without_rewriting():
    """T-0388's real shape: frontmatter ``P1``, title ``P3 DEFERRED: …``. The
    stored field is reported untouched; only the RANKING priority moves, and it
    moves DOWN."""
    row = _classify(priority="P1", title="P3 DEFERRED: auto-create TG supergroup")
    assert row["priority"] == "P1", "the stored field must be reported verbatim"
    assert row["effective_priority"] == pickup.UNRANKED_BAND
    assert "priority-title-disagreement:P3" in row["sanity"]
    assert "title-says-deferred:deferred" in row["sanity"]
    assert row["band"] == pickup.BAND_TRIAGE


def test_a_title_agreeing_with_the_field_raises_no_flag():
    row = _classify(priority="P1", title="P1 fix the thing")
    assert row["sanity"] == []
    assert row["effective_priority"] == 1


@pytest.mark.parametrize("word", pickup.DEFERRAL_WORDS)
def test_each_deferral_word_demotes_to_the_bottom_band(word):
    row = _classify(priority="P1", title=f"{word.upper()} container: framework areas")
    assert row["effective_priority"] == pickup.UNRANKED_BAND
    assert row["band"] == pickup.BAND_TRIAGE


def test_a_deferral_word_inside_a_longer_word_does_not_fire():
    """Word-boundary matching: ``unparked`` is not ``parked``."""
    assert _classify(title="unparked the queue and reparked it")["sanity"] == []


def test_effective_priority_only_ever_demotes():
    """A title claiming MORE urgency than the field cannot promote the ticket —
    a rule that could promote is a rule that can invent urgency."""
    row = _classify(priority="P3", title="P0 drop everything")
    assert row["effective_priority"] == 3
    assert "priority-title-disagreement:P0" in row["sanity"]


def test_a_missing_priority_is_triaged_at_the_bottom_band():
    row = _classify(priority=None)
    assert row["band"] == pickup.BAND_TRIAGE
    assert row["sanity"] == ["priority-missing"]
    assert row["effective_priority"] == pickup.UNRANKED_BAND


# --- no ownership predicate: the T-0612 correction ---------------------------

def test_another_projects_name_in_the_title_is_not_an_ownership_signal():
    """The correction that removed the ownership rule. T-0612's title opens
    ``watchrobot RV pair-trading program`` and it was cited twice as another
    project's work — but its verbatim is a voice note about BOT-SQUAD's own
    session-lifecycle behaviour, observed on that install. A rule keyed on the
    name would have suppressed a genuine P1 with a real stakeholder verbatim."""
    row = pickup.classify_ticket(
        _meta(status="in_progress", priority="P1",
              title="watchrobot RV pair-trading program (stakeholder 25-min voice)"),
        now_epoch=NOW)
    assert row["band"] == pickup.BAND_PICKUP
    assert row["sanity"] == []
    assert not any("foreign" in s for s in row["sanity"])


def test_no_ownership_predicate_survives_on_the_module():
    """A guard against the rule being helpfully re-added: a project name in a
    title is not evidence of ownership, and the reasoning is in the docstring."""
    assert not hasattr(pickup, "foreign_project")
    assert "NO cross-project-ownership rule" in pickup.__doc__


# --- ranking ----------------------------------------------------------------

def test_urgency_leads_then_status_then_longest_ignored():
    rows = [
        _classify(id="T-p2", priority="P2", status="reopened"),
        _classify(id="T-planned", priority="P1", status="planned"),
        _classify(id="T-reopened", priority="P1", status="reopened"),
        _classify(id="T-open-old", priority="P1", status="open",
                  updated=_iso(NOW - 10 * DAY)),
        _classify(id="T-open-new", priority="P1", status="open"),
    ]
    order = [r["id"] for r in sorted(rows, key=lambda r: r["rank"])]
    assert order == ["T-reopened", "T-open-old", "T-open-new", "T-planned", "T-p2"]


def test_reopened_leads_its_band_because_he_reported_it_twice():
    reopened = _classify(status="reopened")
    for other in ("in_progress", "open", "planned"):
        assert reopened["rank"] < _classify(status=other)["rank"]


# --- the board sweep --------------------------------------------------------

@pytest.fixture
def board(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug / "backlog").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / slug / "sessions").mkdir(parents=True, exist_ok=True)
    # No tmux in a test: nothing is a live holder unless a test says so.
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    return cfg, slug


def _write_task(cfg, slug, tid, **fields) -> Path:
    fields.setdefault("title", f"work for {tid}")
    fields.setdefault("status", "open")
    fields.setdefault("priority", "P2")
    fields.setdefault("updated", _iso(NOW - DAY))
    lines = "\n".join(f"{k}: {v}" for k, v in fields.items() if v is not None)
    p = cfg.data_dir / slug / "backlog" / f"{tid}-x.md"
    p.write_text(f"---\nid: {tid}\n{lines}\n---\n\nbody\n")
    return p


def _write_session(cfg, slug, sid, *, task_id, status="active"):
    (cfg.data_dir / slug / "sessions" / f"{sid}.md").write_text(
        f"---\nsid: {sid}\nstatus: {status}\ntask_id: {task_id}\n"
        f"window: dev\ncwd: /tmp\n---\n\nx\n"
    )


def test_the_reopened_p1_leads_the_queue(board):
    """The T-0719 case, end to end through the file sweep: a reopened P1 that
    nobody holds is the first thing an operator is told to take."""
    cfg, slug = board
    _write_task(cfg, slug, "T-0719", status="reopened", priority="P1",
                title="REGRESSION: reply-by-sid routing broke system-wide")
    _write_task(cfg, slug, "T-0640", status="planned", priority="P1")
    _write_task(cfg, slug, "T-0742", status="planned", priority="P2")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert [r["id"] for r in q["pickup"]] == ["T-0719", "T-0640", "T-0742"]
    assert q["counts"] == {"pickup": 3, "triage": 0, "excluded": 0, "board": 3}


def test_an_empty_board_yields_an_empty_queue_and_says_so(board):
    """The red/green pair for the surface: drive it where NO takeable ticket
    exists and it must stay quiet — and the brief must SAY the queue is empty
    rather than omitting the section, which a reader takes as "not computed"."""
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="closed")
    _write_task(cfg, slug, "T-2", status="totest")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert q["pickup"] == []
    assert q["counts"]["pickup"] == 0
    brief = pickup.pickup_brief(q)
    # T-0829 made the DRIVE SCOPE block the brief's first line, so this is no
    # longer a whole-string equality. What the original assertion was PROTECTING
    # — the empty case is said out loud, and nothing in the brief suggests work
    # was offered — is asserted directly instead.
    assert pickup.EMPTY_PICKUP_LINE in brief
    assert "EMPTY" in brief and "Do NOT invent work" in brief
    assert "PICKUP QUEUE (" not in brief and "NEEDS TRIAGE" not in brief
    assert "T-1" not in brief and "T-2" not in brief


def test_a_project_with_no_backlog_dir_is_safe(board):
    cfg, slug = board
    import shutil
    shutil.rmtree(cfg.data_dir / slug / "backlog")
    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    assert q["counts"] == {"pickup": 0, "triage": 0, "excluded": 0, "board": 0}


def test_a_live_session_holding_a_task_removes_it_from_the_queue(board, monkeypatch):
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="reopened", priority="P1")
    _write_task(cfg, slug, "T-2", status="open", priority="P1")
    _write_session(cfg, slug, "S-dev-p1", task_id="T-1")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {"S-dev-p1"})

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert [r["id"] for r in q["pickup"]] == ["T-2"]
    assert [(r["id"], r["reject"]) for r in q["excluded"]] == [("T-1", "held-by:S-dev-p1")]


def test_a_dead_pane_whose_md_still_reads_active_does_not_hold_its_task(board):
    """A crashed dev's md lingers ``status: active`` until the next gc tick.
    Treating that phantom as a holder would make its abandoned task invisible —
    the defect this module exists to fix, arriving through the back door."""
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="in_progress", priority="P1")
    _write_session(cfg, slug, "S-dead-p1", task_id="T-1")  # no live pane (fixture)

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)

    assert [r["id"] for r in q["pickup"]] == ["T-1"]


def test_an_archived_session_never_holds_its_task(board, monkeypatch):
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="open", priority="P1")
    (cfg.data_dir / slug / "sessions" / "S-old-p1.md").write_text(
        "---\nsid: S-old-p1\nstatus: active\narchived: true\ntask_id: T-1\n"
        "window: dev\n---\n\nx\n"
    )
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {"S-old-p1"})
    assert pickup.held_task_ids(cfg, slug) == {}
    assert [r["id"] for r in pickup.pickup_queue(cfg, slug, now_epoch=NOW)["pickup"]] == ["T-1"]


def test_a_closed_blocker_no_longer_blocks(board):
    """A closed blocker on a ticket nobody re-read would otherwise be a
    permanent, invisible hold — the same class of failure as the whole ticket."""
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="closed")
    _write_task(cfg, slug, "T-2", status="open", priority="P1", blocked_by="[T-1]")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    assert [r["id"] for r in q["pickup"]] == ["T-2"]


def test_an_open_blocker_still_blocks_through_the_sweep(board):
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="open", priority="P1")
    _write_task(cfg, slug, "T-2", status="open", priority="P1", blocked_by="[T-1]")

    q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    assert [r["id"] for r in q["pickup"]] == ["T-1"]
    assert [(r["id"], r["reject"]) for r in q["excluded"]] == [("T-2", "blocked-by:T-1")]


def test_the_operators_whole_fixture_set_lands_where_it_should(board):
    """The bar, restated with the board as measured on 2026-07-30 (operator p502,
    corroborated by watchrobot's operator finding the same defect independently):

    * the six in_progress-behind-no-live-session tickets all SURFACE — four as
      takeable, the two initiative containers as triage;
    * T-0801/T-0804 do NOT surface: their lanes are live under a TL;
    * T-0719, invisible through six sessions, LEADS the queue.
    """
    cfg, slug = board
    # The six abandoned ones, with their measured ages.
    _write_task(cfg, slug, "T-0328", status="in_progress", priority="P1",
                updated=_iso(NOW - 3 * DAY))
    _write_task(cfg, slug, "T-0331", status="in_progress", priority="P1",
                updated=_iso(NOW - 1 * DAY))
    _write_task(cfg, slug, "T-0626", status="in_progress", priority="P2",
                updated=_iso(NOW - 4 * DAY))
    _write_task(cfg, slug, "T-0612", status="in_progress", priority="P1",
                updated=_iso(NOW - 24 * DAY),
                title="watchrobot RV pair-trading program (stakeholder 25-min voice)")
    _write_task(cfg, slug, "T-0551", status="in_progress", priority="0",
                kind="initiative", updated=_iso(NOW - 5 * DAY))
    _write_task(cfg, slug, "T-0556", status="in_progress", priority="0",
                kind="initiative", updated=_iso(NOW - 11 * DAY))
    # The reopened P1 that nobody took.
    _write_task(cfg, slug, "T-0719", status="reopened", priority="P1",
                title="REGRESSION: reply-by-sid routing broke system-wide")
    # The two parents whose lanes are live under a TL.
    _write_task(cfg, slug, "T-0801", status="in_progress", priority="P2")
    _write_task(cfg, slug, "T-0807", status="in_progress", priority="P2",
                parent_task="T-0801")
    _write_session(cfg, slug, "S-tl-p500", task_id="T-0807")
    import bot_squad_worker.sessions as _S
    monkey = pytest.MonkeyPatch()
    monkey.setattr(_S, "_live_agent_sids", lambda: {"S-tl-p500"})
    try:
        q = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    finally:
        monkey.undo()

    pick = [r["id"] for r in q["pickup"]]
    triage = [r["id"] for r in q["triage"]]
    excluded = {r["id"]: r["reject"] for r in q["excluded"]}

    assert pick[0] == "T-0719", "the reopened P1 leads"
    assert set(pick) == {"T-0719", "T-0328", "T-0331", "T-0626"}
    # T-0612 is a POSITIVE fixture — a genuine P1, not another project's. At 24
    # days it lands in triage, which is SURFACED: an operator should read that
    # 25-minute voice note before a dev is dispatched at it.
    assert set(triage) == {"T-0551", "T-0556", "T-0612"}
    assert "stale:24d>=14d" in dict(
        (r["id"], r["sanity"]) for r in q["triage"])["T-0612"]
    assert excluded["T-0801"] == "lane-live:S-tl-p500"
    assert excluded["T-0807"] == "held-by:S-tl-p500"
    # Every one of the six is visible in one band or the other.
    surfaced = set(pick) | set(triage)
    for tid in ("T-0328", "T-0331", "T-0551", "T-0556", "T-0626", "T-0612"):
        assert tid in surfaced, f"{tid} must be surfaced, not swallowed"


def test_the_stale_p1_is_ranked_by_urgency_not_hidden_by_age(board):
    """T-0612 at 24 days: staleness moves it to triage but never out of sight,
    and once someone writes a note (resetting ``updated``) it is takeable at P1.
    Staleness is a floor on neglect, not a measure of it."""
    cfg, slug = board
    _write_task(cfg, slug, "T-0612", status="in_progress", priority="P1",
                updated=_iso(NOW - 24 * DAY))
    stale = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    assert [r["id"] for r in stale["triage"]] == ["T-0612"]
    assert stale["pickup"] == []

    _write_task(cfg, slug, "T-0612", status="in_progress", priority="P1",
                updated=_iso(NOW))
    fresh = pickup.pickup_queue(cfg, slug, now_epoch=NOW)
    assert [r["id"] for r in fresh["pickup"]] == ["T-0612"]


def test_an_md_without_frontmatter_is_skipped_not_fatal(board):
    cfg, slug = board
    (cfg.data_dir / slug / "backlog" / "README.md").write_text("just prose\n")
    _write_task(cfg, slug, "T-1", status="open", priority="P1")
    assert pickup.pickup_queue(cfg, slug, now_epoch=NOW)["counts"]["board"] == 1


def test_an_md_missing_its_id_field_falls_back_to_the_filename(board):
    cfg, slug = board
    (cfg.data_dir / slug / "backlog" / "T-0042-no-id.md").write_text(
        f"---\ntitle: t\nstatus: open\npriority: P1\nupdated: {_iso(NOW)}\n---\n\nx\n"
    )
    assert [r["id"] for r in pickup.pickup_queue(cfg, slug, now_epoch=NOW)["pickup"]] == ["T-0042"]


# --- the brief: what an operator is actually told ---------------------------

def test_the_brief_names_the_takeable_ids_with_the_facts_a_dispatch_needs(board):
    cfg, slug = board
    _write_task(cfg, slug, "T-0719", status="reopened", priority="P1",
                title="REGRESSION: reply-by-sid routing broke")
    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW))
    assert "T-0719" in brief
    assert "[reopened]" in brief
    assert "P1" in brief
    assert "PICKUP QUEUE (1 takeable" in brief


def test_the_brief_truncates_and_says_how_many_it_withheld(board):
    cfg, slug = board
    for i in range(12):
        _write_task(cfg, slug, f"T-{i:04d}", status="open", priority="P2")
    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW), limit=3)
    assert brief.count("\n  T-") == 3
    assert "… and 9 more" in brief


def test_the_brief_surfaces_the_triage_band_and_forbids_tidying_priorities(board):
    """The operator declined to rewrite T-0388's priority field to make a listing
    tidy. The brief that shows the suspect band says so, so the next reader does
    not helpfully "fix" it."""
    cfg, slug = board
    _write_task(cfg, slug, "T-0388", status="planned", priority="P1",
                title="P3 DEFERRED: auto-create TG supergroup")
    brief = pickup.pickup_brief(pickup.pickup_queue(cfg, slug, now_epoch=NOW))
    assert "NEEDS TRIAGE (1): T-0388" in brief
    assert "do NOT rewrite anyone's priority field" in brief


def test_a_clean_board_brief_carries_no_triage_line(board):
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="open", priority="P1")
    assert "NEEDS TRIAGE" not in pickup.pickup_brief(
        pickup.pickup_queue(cfg, slug, now_epoch=NOW))


def test_the_real_clock_is_used_when_no_epoch_is_given(board):
    cfg, slug = board
    _write_task(cfg, slug, "T-1", status="open", priority="P1", updated=_iso(time.time()))
    q = pickup.pickup_queue(cfg, slug)
    assert [r["id"] for r in q["pickup"]] == ["T-1"]
