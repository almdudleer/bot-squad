"""T-0920 — what a WATCHDOG-KILLED deploy tells people.

Two independent halves, both anchored on the 2026-08-18 13:18Z run
``311ba682-e812-4b01-9f62-abeb3d74ce15`` (rc=124, elapsed 1802s):

1. **The alert fanout.** T-0839 recorded "one event generated ~30 duplicate
   peer alerts" as an observed count with no traced mechanism. It is neither of
   the two mechanisms that ticket proposed. Measured below, and reproduced here
   as a fixture rather than asserted from prose.
2. **What the message says** — the requester notice, the numbers, and the
   excerpt (see ``test_t0920_killed_message.py``'s section further down).

--- the fanout, measured ---------------------------------------------------

``_peer_to_operators`` resolved its recipients by walking
``sessions.list_sessions`` for ``role == "operator"``. That returns EVERY
session md ever written for the project, archived ones included, so the filter
matched the whole HISTORY of the role. On this install at 2026-08-18 20:18Z:
**50 SIDs, one of them live.**

The old ``dict.fromkeys`` deduped the sids it was about to ASK for, which is the
wrong end of the pipe. ``intersession.send`` then resolves each recycled
predecessor to its live successor (T-0790) — and a successor is found by WINDOW
STEM, so every ``S-almdudleer-operator-p<N>`` in that history resolves to the
one live ``S-almdudleer-operator-p381``.

From the worker journal for that kill, 13:18:06Z–13:18:13Z, ONE tick:

    31 × "intersession: S-almdudleer-operator-p<N> was RECYCLED — routing to
          its live successor S-almdudleer-operator-p381"

and from the inbox files themselves: 32 identical lines in
``inbox-S-almdudleer-operator-p381.log`` (31 redirected + 1 addressed directly)
plus 18 single lines in inboxes of differently-stemmed dead operators
(``…-bot-squad-operator-p309``, ``…-per-project-operator-p33``, …) which no
session drains. 50 sends, one event.

It is NOT re-firing: the next ``deploy_monitor_one`` ticks (13:18:40, 13:19:40)
emitted nothing, and the stakeholder DM leg fired exactly once (one record in
``_worker/outbound/2026-08-18.jsonl`` at 13:18:05Z).

So it is not a legitimate per-operator fanout either — 32 of the 50 went to ONE
session. The fix resolves through ``dispatch.live_operator_sids``, the
operator-identity SSOT (T-0523) that ``intersession``'s own ``operator`` keyword
already uses.

The fixture below reproduces BOTH classes of historical SID, and the negative
control re-runs the PRE-FIX resolution against the same fixture — so a green
here is a green over a roster that provably still produces the old flood.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import intersession as IS
from bot_squad_worker import jobs as J


LIVE_OPERATOR = "S-almdudleer-operator-p381"

#: Predecessors sharing the LIVE operator's window stem — these are the ones
#: T-0790 redirects onto the live successor, i.e. the duplicates.
RECYCLED_PREDECESSORS = tuple(
    f"S-almdudleer-operator-p{n}" for n in
    (3, 4, 5, 6, 9, 11, 23, 29, 32, 47, 53, 57, 59, 60, 63, 66, 69,
     188, 239, 240, 241, 260, 298, 317, 343, 374, 455, 502, 533, 548, 575)
)

#: Dead operators under OTHER window stems — no successor is found for these,
#: so the pre-fix code wrote each one a private inbox nobody drains.
UNSTEMMED_DEAD = (
    "S-almdudleer-bot-squad-operator-p309",
    "S-almdudleer-per-project-operator-p33",
    "S-almdudleer-t0041verify-operator-p55",
)


def _write_session_md(cfg, slug: str, sid: str, *, window: str,
                      status: str, archived: bool = False) -> None:
    sess = Path(cfg.data_dir) / slug / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / f"{sid}.md").write_text(
        "---\n"
        f"sid: {sid}\n"
        f"tmux_session: {slug}\n"
        f"window: {window}\n"
        "role: operator\n"
        f"status: {status}\n"
        f"archived: {'true' if archived else 'false'}\n"
        "linux_user: almdudleer\n"
        "---\n\nbody\n"
    )


@pytest.fixture
def roster(tmp_path: Path):
    """The live install's operator history, in the two shapes it actually has.

    One live operator, 31 recycled predecessors in the SAME window, and three
    archived operators under different window stems — the shape the 13:18Z
    forensics found. Deliberately real files: the redirect under test is a
    session-md lookup, so a mocked roster would not exercise it.
    """
    slug = "demo"
    project = SimpleNamespace(slug=slug, tg_chat="404580642", tg_topic_id=None,
                              staging_url="", mothership=False,
                              repo_path=str(tmp_path / "repo"))
    cfg = SimpleNamespace(data_dir=tmp_path / "data", projects={slug: project})
    (Path(cfg.data_dir) / slug / "_chat").mkdir(parents=True)

    _write_session_md(cfg, slug, LIVE_OPERATOR, window="operator", status="active")
    for sid in RECYCLED_PREDECESSORS:
        _write_session_md(cfg, slug, sid, window="operator", status="suspended")
    for sid in UNSTEMMED_DEAD:
        _write_session_md(cfg, slug, sid, window=sid.split("-", 2)[2].rsplit("-p", 1)[0],
                          status="archived", archived=True)
    return cfg, slug


def _inbox_lines(cfg, slug: str) -> dict[str, int]:
    """Every inbox file that exists, and how many lines it holds."""
    chat = Path(cfg.data_dir) / slug / "_chat"
    return {
        p.name.removeprefix("inbox-").removesuffix(".log"):
            len([l for l in p.read_text().splitlines() if l.strip()])
        for p in sorted(chat.glob("inbox-*.log"))
    }


def _no_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tmux in a test env — pin the SSOT's pane-scan half to empty so the
    md scan is what the assertions are reading."""
    from bot_squad_worker import dispatch as D
    monkeypatch.setattr(D, "_live_operator_sids_from_tmux", lambda slug, seen: [])


# ---------------------------------------------------------------------------
# The fixture is faithful: the PRE-FIX resolution still floods it
# ---------------------------------------------------------------------------

def test_negative_control_the_prefix_resolution_still_produces_the_flood(
    roster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green above means nothing unless this roster can still reproduce the
    defect. This re-runs the resolution ``_peer_to_operators`` used before
    T-0920 — verbatim — against the same fixture and the same REAL
    ``send_notice``, and asserts the measured shape comes back.
    """
    cfg, slug = roster
    from bot_squad_worker import sessions as S

    rows = S.list_sessions(cfg, slug)
    operator_sids = [r.get("sid") for r in rows
                     if r.get("role") == "operator" and r.get("sid")]
    for sid in dict.fromkeys(operator_sids):
        IS.send_notice(cfg, slug, "S-deploy_monitor", sid, "❌ deploy demo/staging KILLED")

    lines = _inbox_lines(cfg, slug)
    assert lines[LIVE_OPERATOR] == 1 + len(RECYCLED_PREDECESSORS), (
        "the pre-fix walk no longer collapses recycled predecessors onto the "
        "live operator — this fixture has stopped reproducing the defect, so "
        "the guard below is measuring nothing"
    )
    assert sorted(k for k in lines if k != LIVE_OPERATOR) == sorted(UNSTEMMED_DEAD), (
        "the pre-fix walk no longer writes dead un-stemmed inboxes"
    )
    assert sum(lines.values()) == 1 + len(RECYCLED_PREDECESSORS) + len(UNSTEMMED_DEAD)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def test_one_kill_reaches_the_live_operator_exactly_once(
    roster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One event, one line, in the one inbox a human reads."""
    cfg, slug = roster
    _no_tmux(monkeypatch)

    J._peer_to_operators(cfg, slug, "❌ deploy demo/staging KILLED")

    assert _inbox_lines(cfg, slug) == {LIVE_OPERATOR: 1}


def test_a_dead_operators_inbox_is_never_written(
    roster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same defect, stated on its own so a change that
    fixes the duplicates but keeps writing dead inboxes still goes red. An
    inbox no session drains is not a quieter alert, it is a lost one."""
    cfg, slug = roster
    _no_tmux(monkeypatch)

    J._peer_to_operators(cfg, slug, "❌ deploy demo/staging KILLED")

    chat = Path(cfg.data_dir) / slug / "_chat"
    for sid in RECYCLED_PREDECESSORS + UNSTEMMED_DEAD:
        assert not (chat / f"inbox-{sid}.log").exists(), (
            f"wrote to {sid}, which is not a live session"
        )


def test_no_live_operator_is_reported_not_swallowed(
    roster, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """With every operator dead the alert reaches no pane. That is allowed —
    the stakeholder DM leg has already fired — but it must leave a trace, or a
    broken roster is indistinguishable from a delivered alert."""
    cfg, slug = roster
    _no_tmux(monkeypatch)
    (Path(cfg.data_dir) / slug / "sessions" / f"{LIVE_OPERATOR}.md").unlink()

    with caplog.at_level("WARNING"):
        J._peer_to_operators(cfg, slug, "❌ deploy demo/staging KILLED")

    assert _inbox_lines(cfg, slug) == {}
    assert any("no LIVE operator" in r.getMessage() for r in caplog.records), caplog.text


def test_resolution_failure_never_raises_out_of_the_alert(
    roster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_alert_operators`` is the loud path for every way a deploy goes wrong;
    a roster read that blows up must cost the peer leg, never the tick."""
    cfg, slug = roster
    from bot_squad_worker import dispatch as D

    def _boom(_cfg, _slug):
        raise RuntimeError("sessions dir on fire")

    monkeypatch.setattr(D, "live_operator_sids", _boom)
    J._peer_to_operators(cfg, slug, "❌ deploy demo/staging KILLED")  # must not raise
    assert _inbox_lines(cfg, slug) == {}


# ===========================================================================
# 2. WHAT THE MESSAGE SAYS
#
# Every assertion below is on the CLAIM in the text, never on an exit code or
# on "a send happened" — a green suite that only counts sends cannot see a
# message that lies, and the message lying is this ticket's actual defect. The
# numbers are the real ones from run 311ba682 (T-0919's measurement), so a
# wording change that stops carrying them goes red rather than merely changing.
# ===========================================================================

#: The literal tail of the 2026-08-18 13:18Z run log, bytes as they are on
#: disk. Composed from the real file rather than typed to shape, so the excerpt
#: assertions pin what a reader would actually be shown.
LIVE_KILLED_LOG_TAIL = """#18 [web-builder 6/8] COPY scripts/install /scripts/install
#18 DONE 583.7s

#19 [web-builder 7/8] COPY api/response_shapes.json /api/response_shapes.json
#19 DONE 102.2s

#20 [web-builder 8/8] RUN npm run build
#20 54.27 
#20 54.27 > bot-squad-web@0.1.0 build
#20 54.27 > tsc -b && vite build
#20 54.27 


❌ WATCHDOG: killed — exceeded hard timeout of 1800s. Elapsed 1802s. rc=124.
"""

REQUESTER = "S-almdudleer-release-timeout-recovery-p406"

#: Exactly what the branch said before T-0920, kept verbatim as the thing the
#: assertions have to be able to reject.
PRE_T0920_LINE = "KILLED by hard timeout (build ran too long)"


@pytest.fixture
def kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Drive ``_run_project_deploy`` with a REAL DeployResult and hand back
    every message each lane received.

    A real dataclass, not a mirror: ``test_msg_routes.py`` was broken twice by
    hand-mirroring this exact object (`15f4d01`), and a stub is the one place a
    field can go missing without anything going red.
    """
    from bot_squad_worker import actions as A
    from bot_squad_worker import channels as C
    from bot_squad_worker import deploy as D
    import json as _json

    slug = "demo"
    project = SimpleNamespace(slug=slug, tg_chat="404580642", tg_topic_id=None,
                              staging_url="", mothership=False,
                              repo_path=str(tmp_path / "repo"))
    cfg = SimpleNamespace(data_dir=tmp_path / "data", projects={slug: project})
    (Path(cfg.data_dir) / slug / "_chat").mkdir(parents=True)

    log_path = tmp_path / "311ba682-e812-4b01-9f62-abeb3d74ce15.log"
    log_path.write_text(LIVE_KILLED_LOG_TAIL)
    qfile = tmp_path / "q.json"
    qfile.write_text(_json.dumps({"target": "staging"}))

    lanes: dict[str, list] = {"channel": [], "dm": [], "peer": []}

    class _Chan:
        def send(self, text, *, chat_id="", sid="", urgent=False, topic_id=None, **kw):
            lanes["channel"].append(text)
            return True

    monkeypatch.setattr(D, "list_queued", lambda c, s: [qfile])
    monkeypatch.setattr(D, "is_paused", lambda c, s: None)
    monkeypatch.setattr(D, "is_clean_for_target", lambda c, s, t: True)
    monkeypatch.setattr(C, "get_channel", lambda c, **kw: _Chan())
    monkeypatch.setattr(A, "_send_stakeholder_dm",
                        lambda cfg, **kw: lanes["dm"].append(kw.get("message")) or True)
    monkeypatch.setattr(IS, "send_notice",
                        lambda c, s, frm, to, t: lanes["peer"].append((to, t)) or {"ok": True})
    from bot_squad_worker import dispatch as DISP
    monkeypatch.setattr(DISP, "live_operator_sids", lambda c, s: [LIVE_OPERATOR])

    def _run(**fields):
        base = dict(
            ok=False, returncode=124, queue_id="311ba682-e812-4b01-9f62-abeb3d74ce15",
            log_path=log_path, target="staging", requested_by=REQUESTER,
            killed_reason="timeout", killed_elapsed_s=1802.0, killed_limit_s=1800,
            killed_limit_name="wall-clock timeout", silence_s=15.0,
            completed_steps=19,
            last_step="#20 [web-builder 8/8] RUN npm run build",
            last_completed_step="#19 [web-builder 7/8] COPY api/response_shapes.json (102.2s)",
        )
        base.update(fields)
        monkeypatch.setattr(D, "run_next", lambda c, s: D.DeployResult(**base))
        for lane in lanes.values():
            lane.clear()
        J._run_project_deploy(cfg, slug, project)
        return lanes

    return _run


def _alert(lanes) -> str:
    assert lanes["dm"], "the kill produced no operator alert at all"
    return lanes["dm"][-1]


def _to_requester(lanes) -> list[str]:
    return [t for to, t in lanes["peer"] if to == REQUESTER]


# --- DoD 1: the requester is told ------------------------------------------

def test_the_session_that_asked_is_told_the_deploy_was_killed(kill) -> None:
    """T-0453's principle, on the one failure path that still skipped it. The
    requester is holding ``{"ok": true, queue_id: …}`` and nothing else in the
    system will ever contradict it."""
    lanes = kill()

    notices = _to_requester(lanes)
    assert len(notices) == 1, f"requester got {len(notices)} notices: {lanes['peer']}"
    assert "KILLED" in notices[0]
    assert "311ba682" in notices[0], "the notice does not name the run it is about"


def test_negative_control_the_prefix_branch_leaves_the_requester_silent(
    kill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control the DoD asks for, and it has to be a control over the
    INSTRUMENT, not over the code: it re-installs the pre-T-0920 behaviour
    (operator alert only) and shows the assertion above going red. Without
    this, "the requester was notified" could be green because the fixture wires
    every peer message to the requester."""
    monkeypatch.setattr(J, "_notify_requester", lambda cfg, slug, who, text: None)

    lanes = kill()

    assert lanes["dm"], "the operator alert must be unaffected by the control"
    assert _to_requester(lanes) == [], (
        "the control did not actually remove the requester notice, so the test "
        "above proves nothing"
    )


def test_a_non_session_requester_is_not_notified(kill) -> None:
    """A CLI/cron caller has no inbox. The filter lives in ``_notify_requester``
    and this drives the real one rather than a stand-in."""
    lanes = kill(requested_by="cli")
    assert [to for to, _ in lanes["peer"]] == [LIVE_OPERATOR]


# --- DoD 2: it names the numbers -------------------------------------------

def test_the_alert_names_elapsed_limit_and_what_had_completed(kill) -> None:
    """The 1802s run reported nothing of itself. All three go in the message."""
    text = _alert(kill())

    assert "1802s" in text, "elapsed time is still missing"
    assert "1800s" in text, "the limit that fired is still missing"
    assert "19 build step(s) had completed" in text
    assert "#19 [web-builder 7/8] COPY api/response_shapes.json (102.2s)" in text, (
        "what had finished is missing"
    )
    assert "#20 [web-builder 8/8] RUN npm run build" in text, (
        "the step that was in flight is missing — the reader cannot tell how far "
        "the build got"
    )


def test_absent_telemetry_is_stated_not_rendered_as_a_zero(kill) -> None:
    """A record from before T-0919 carries 0.0 for every one of these. Printing
    "silent for 0s" would read as a measurement of a build that was producing
    output at the instant it died — a fabricated fact, and the opposite of an
    unknown. Say the numbers are absent instead."""
    text = _alert(kill(killed_elapsed_s=0.0, killed_limit_s=0, silence_s=0.0,
                       completed_steps=0, last_step="", last_completed_step=""))

    assert "NOT RECORDED" in text
    assert "0s" not in text.split("The queue is now unblocked")[0], (
        "a zero from an unpopulated field was rendered as a measured value"
    )


# --- DoD 3: the diagnosis is not a wrong pointer ---------------------------

def test_a_build_still_producing_output_is_not_reported_as_slow(kill) -> None:
    """The wrong-pointer defect, stated as its own guard.

    311ba682's log was still growing when the kill landed — ``npm run build``
    had printed 15s earlier. "build ran too long" sends the reader hunting for a
    slow build step; the cause was a wall-clock budget consumed by host I/O
    contention, and the same build takes minutes on a quiet box."""
    text = _alert(kill(silence_s=15.0))

    assert PRE_T0920_LINE not in text, "the wrong diagnosis is still being sent"
    assert "ran too long" not in text
    assert "still being written to" in text
    assert "15s before the kill" in text, (
        "the silence measurement — the ONE number that separates a live build "
        "from a wedged one — is not in the message"
    )
    assert "ALIVE" in text


def test_a_genuinely_wedged_build_is_called_wedged(kill) -> None:
    """The twin, so the test above is not just "never say wedged". A
    no-progress kill means the log really had gone quiet, and the message must
    say so — with T-0919's caveat, because buildkit prints nothing during a
    COPY step and the longest silence on a HEALTHY build measured 583.7s
    against a 600s budget."""
    text = _alert(kill(killed_reason="no_progress", returncode=125,
                       killed_limit_name="no-progress budget",
                       killed_limit_s=1436, silence_s=1436.0))

    assert "WEDGED" in text
    assert "no-progress budget" in text
    assert "1436s" in text
    assert "ALIVE" not in text, "a wedged build must not be described as alive"
    assert "COPY step" in text, (
        "the false-positive caveat is missing — a reader who does not know "
        "buildkit is silent during COPY will over-trust this kill"
    )


def test_a_kill_is_never_presented_as_a_recipe_or_build_failure(kill) -> None:
    """T-0839 item 2, in the words it asked for."""
    text = _alert(kill())
    assert "a WATCHDOG ended this run" in text
    assert "The recipe did not fail and the build did not error." in text


# --- DoD 4: the log is quoted ----------------------------------------------

def test_the_alert_quotes_the_log_so_nobody_has_to_tail_it(kill) -> None:
    """Reuses ``_recipe_failure_detail`` (T-0878). A killed run has no ``FATAL``
    block, so this exercises the helper's TAIL fallback — measured against the
    real 311ba682 bytes before any code was written, and it already carried the
    last completed step, the step in flight and the watchdog's own marker. No
    second helper was needed; this pins that."""
    text = _alert(kill())

    assert "#19 DONE 102.2s" in text
    assert "> tsc -b && vite build" in text
    assert "❌ WATCHDOG: killed" in text, "the watchdog's own marker is not quoted"


def test_an_unreadable_log_never_costs_the_alert(kill, tmp_path: Path) -> None:
    """A missing log degrades the alert's detail, never the alert (T-0878)."""
    text = _alert(kill(log_path=tmp_path / "gone.log"))
    assert "KILLED" in text and "1802s" in text


# --- T-0839 item 3: the invisible intermediate state -----------------------

def test_a_kill_that_left_the_install_ahead_of_the_worker_says_so(kill) -> None:
    """T-0839 item 3, and T-0919 measured it as the DEFAULT outcome of a kill,
    not a race: the sync landed 13s into a 1802s run and
    ``_should_restart_worker`` returns False whenever ``ok`` is False. Nothing
    else surfaces it — per T-0824 ``/api/health`` has no term for the install
    tree."""
    text = _alert(kill(install_sha_drift=True,
                       install_tree_sha="93960ba1c0de4f5a6b7c8d9e0f1a2b3c4d5e6f70"))

    assert "93960ba1c0de" in text
    assert "OLD code" in text
    assert "systemctl --user restart bot-squad-worker.service" in text


def test_no_drift_no_scary_clause(kill) -> None:
    """The discriminating half — an alert that always warns about a stale
    worker has stopped carrying information."""
    text = _alert(kill(install_sha_drift=False))
    assert "systemctl --user restart" not in text


# --- the budget NOTICE (fires without a kill) ------------------------------

def test_a_run_that_crossed_the_budget_and_SUCCEEDED_still_says_so(kill) -> None:
    """T-0919 turned the 1800s cap from a kill into a notice, so a slow build
    now finishes. An unreported notice is the cap silently deleted — and the
    success path is the one case no failure branch would ever have covered."""
    lanes = kill(ok=True, returncode=0, killed_reason=None, killed_elapsed_s=0.0,
                 budget_exceeded=True, budget_s=1800)

    terminal = lanes["channel"][-1]
    assert terminal.startswith("✅"), terminal
    assert "1800s" in terminal and "allowed to continue" in terminal


def test_no_budget_notice_on_an_ordinary_fast_deploy(kill) -> None:
    lanes = kill(ok=True, returncode=0, killed_reason=None, budget_exceeded=False)
    assert "allowed to continue" not in lanes["channel"][-1]


# --- the contract with T-0919 ----------------------------------------------

def test_deploy_result_declares_every_field_this_report_reads() -> None:
    """The meeting point of the two lanes (T-0919 item 4), pinned so a rollback
    or a rename on the deploy.py side goes red HERE rather than as an
    AttributeError inside a live kill — which ``deploy_monitor_one`` catches and
    logs, turning a reported kill back into a silent one."""
    import dataclasses
    from bot_squad_worker.deploy import DeployResult

    declared = {f.name for f in dataclasses.fields(DeployResult)}
    required = {
        "killed_reason", "killed_elapsed_s", "killed_limit_s", "killed_limit_name",
        "silence_s", "completed_steps", "last_step", "last_completed_step",
        "budget_exceeded", "budget_s", "install_sha_drift", "install_tree_sha",
        "requested_by", "target", "log_path",
    }
    assert required <= declared, f"DeployResult is missing {sorted(required - declared)}"


def test_a_kill_never_claims_the_run_was_allowed_to_continue(kill) -> None:
    """The budget notice and a kill can co-occur — the run crosses its
    wall-clock budget, is deliberately allowed to keep going (T-0919), and is
    killed later by something else. Carrying the success-path wording into the
    kill's opening sentence produced "KILLED … and was allowed to continue",
    which is a contradiction in the one line a reader scans. The fact still gets
    stated, in its own sentence and in the order it happened."""
    text = _alert(kill(killed_reason="ceiling", killed_limit_name="absolute ceiling",
                       killed_limit_s=5400, silence_s=4.0,
                       budget_exceeded=True, budget_s=1800))

    head = text.splitlines()[0]
    assert "KILLED" in head and "allowed to continue" not in head, head
    assert "had already passed its 1800s" in text
    assert "not the limit that ended the run" in text


def test_the_alive_or_wedged_threshold_is_the_systems_own_no_progress_floor(
    kill
) -> None:
    """The "was it alive?" call needs a scale, and a made-up one (half the
    limit, say) would put a guess into the sentence whose whole job is to
    replace a guess. It uses T-0919's no-progress FLOOR: below it, this system
    by its own rule does not consider a run wedged. Pinned to the constant, so
    raising the floor moves this sentence instead of leaving a stale copy."""
    from bot_squad_worker.deploy import DEFAULT_NO_PROGRESS_FLOOR as FLOOR

    just_under = _alert(kill(killed_reason="ceiling", killed_limit_name="absolute ceiling",
                             killed_limit_s=14400, silence_s=float(FLOOR - 1)))
    assert "ALIVE" in just_under, (
        "a wall-clock kill whose silence never reached the no-progress floor "
        "killed a build the wedge-detector would have left alone"
    )

    well_over = _alert(kill(killed_reason="ceiling", killed_limit_name="absolute ceiling",
                            killed_limit_s=14400, silence_s=float(FLOOR * 4)))
    assert "ALIVE" not in well_over
    assert "silent for" in well_over
