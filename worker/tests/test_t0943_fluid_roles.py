"""T-0943: a session holds a SET of roles, and the system routes by the set.

THE DEFECT THESE PIN, in the system's own words. The stakeholder raised a
project's only session and asked what it had produced:

    "None. I haven't created any signals on prod — or anywhere. My role in this
    session is user-conversation only: attend the Telegram thread, file his asks
    as backlog tickets, and ESCALATE TO THE OPERATOR/DEV SESSIONS."

There was no operator and no dev. Its contract told it to hand work to nobody,
so it did nothing — and reported that as COMPLIANCE. Two independent halves,
both tested here:

  * a solo session HOLDS operator and dev, so the drive nudges, the task nudges
    and an escalation addressed to ``operator`` all reach it;
  * an escalation to a role NOBODY holds is REFUSED, instead of returning
    ``ok: True`` with an empty delivery that reads exactly like a success.

Every behaviour test is preceded by the HEALTHY case it must not disturb — a
single-role session's answers are byte-identical to what they were — because a
widening that also changes the ordinary case is not a widening.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import dispatch as D
from bot_squad_worker import drift as DR
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import intersession as I
from bot_squad_worker import sessions as S


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(data_dir=tmp_path / "data",
                                 projects={"p": {"slug": "p"}})


def _write_session(tmp_path: Path, slug: str, sid: str, *, window: str,
                   roles: list | None = None, role: str = "",
                   status: str = "active", uc_gid: str = "") -> Path:
    sess_dir = tmp_path / "data" / slug / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"sid: {sid}", f"status: {status}", "task_id: ~",
             f"window: {window}"]
    if uc_gid:
        lines.append(f"user_conversation_gid: {uc_gid}")
    if role:
        lines.append(f"role: {role}")
    if roles is not None:
        lines.append("roles: [" + ", ".join(roles) + "]")
    lines += ["---", ""]
    p = sess_dir / f"{sid}.md"
    p.write_text("\n".join(lines))
    return p


# ---------------------------------------------------------------------------
# roles_of — the read primitive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("window,expected", [
    # HEALTHY CASES FIRST: every single-role window is unchanged.
    ("dev_add_a_button", ("dev",)),
    ("operator", ("operator",)),
    ("bot-squad-operator", ("operator",)),
    ("feature-TL", ("teamlead",)),
    ("nightly-qa", ("qa",)),
    ("gu_abc-user-conversation", ("user-conversation",)),
    # ...INCLUDING the attendant, which differs from the solo session along
    # exactly the axis under test: same role, different window, NOT widened.
    ("user_session_alex", ("user-conversation",)),
    # THE WIDENING: the one session a project runs holds all three.
    ("universal_bsq_session", S.SOLO_ROLES),
])
def test_roles_of_derives_the_held_set_from_the_window(window, expected):
    assert S.roles_of({"window": window}) == expected


def test_roles_of_reads_the_combination_windows_back():
    """The window is a projection of the set, so it reads back as one."""
    assert S.roles_of({"window": "talking_operator"}) == ("operator",
                                                          "user-conversation")
    assert S.roles_of({"window": "working_operator"}) == ("operator", "dev")


def test_declared_roles_beat_the_window():
    assert S.roles_of({"window": "universal_bsq_session",
                       "roles": ["user-conversation"]}) == ("user-conversation",)
    assert S.roles_of({"window": "dev_x", "roles": ["operator", "dev"]}) == (
        "operator", "dev")


def test_declared_roles_parse_from_an_inline_string():
    """A hand-edited md reads the same as a pyyaml-parsed list."""
    assert S.roles_of({"window": "dev_x", "roles": "[operator, dev]"}) == (
        "operator", "dev")


def test_empty_roles_is_a_TOMBSTONE_that_suppresses_BOTH_fallbacks():
    """`roles: []` means "I hold nothing" and must beat the stored role AND the
    window — otherwise a session that gave its last role away has it re-minted
    by the name it never renamed."""
    meta = {"window": "operator", "role": "operator", "roles": []}
    assert S.roles_of(meta) == ()
    assert S.roles_label(S.roles_of(meta)) == "none"


def test_tilde_roles_is_UNDECLARED_not_a_tombstone():
    """`~` is the registry's unset sentinel; it must not read as "holds
    nothing", which is a different and much louder statement."""
    assert S.roles_of({"window": "universal_bsq_session",
                       "roles": "~"}) == S.SOLO_ROLES


def test_a_morph_stamp_still_narrows_a_universal_window():
    """A universal-window session that morphed to operator declared a
    narrowing; the window widening must not undo it."""
    assert S.roles_of({"window": "universal_bsq_session",
                       "role": "operator"}) == ("operator",)


# ---------------------------------------------------------------------------
# roles_label / window_for_roles — HIS vocabulary, computed from the SET
# ---------------------------------------------------------------------------

def test_roles_label_is_the_stakeholders_own_table():
    assert S.roles_label(S.SOLO_ROLES) == "solo-session"
    assert S.roles_label(("operator", "user-conversation")) == "talking-operator"
    assert S.roles_label(("operator", "dev")) == "working-operator"
    assert S.roles_label(("operator",)) == "pure-operator"
    assert S.roles_label(("user-conversation",)) == "attendant"
    assert S.roles_label(("dev",)) == "dev"
    assert S.roles_label(()) == "none"


def test_roles_label_is_order_insensitive():
    """It names a SET. Two sessions holding the same roles in a different order
    are in the same state and must be called the same thing."""
    assert S.roles_label(("dev", "operator")) == S.roles_label(
        ("operator", "dev"))


def test_window_for_roles_names_the_combination():
    assert S.window_for_roles(S.SOLO_ROLES) == S.UNIVERSAL_WINDOW
    assert S.window_for_roles(("operator", "user-conversation")) == "talking_operator"
    assert S.window_for_roles(("operator", "dev")) == "working_operator"
    assert S.window_for_roles(("operator",)) == "operator"


def test_window_for_roles_declines_where_the_set_does_not_determine_a_name():
    """An attendant's window names WHICH USER and a dev's names WHICH TASK —
    information the role set does not carry. `None` keeps the current name
    rather than inventing a lossy one."""
    assert S.window_for_roles(("user-conversation",)) is None
    assert S.window_for_roles(("dev",)) is None
    assert S.window_for_roles(()) is None


def test_every_named_window_reads_back_to_the_set_that_named_it():
    """Round-trip: the projection and its read-back cannot drift apart."""
    for roles, window in S.ROLE_SET_WINDOWS.items():
        if "routine-handler" in roles:
            continue  # sideways from the three; keyed on `owner`, not a window
        assert set(S.roles_of({"window": window})) == set(roles), window


# ---------------------------------------------------------------------------
# Operator identity: the GUARD and the ROUTING answer are different questions
# ---------------------------------------------------------------------------

def test_live_operator_sids_ignores_a_multi_role_holder(tmp_path, monkeypatch):
    """The SINGLETON GUARD must not see a solo session, or `bsq bud operator`
    — the handover the stakeholder asked for — would be refused as a duplicate."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-universal_bsq_session-p1",
                   window="universal_bsq_session")
    assert D.live_operator_sids(cfg, "p") == []


def test_live_operator_sids_still_sees_a_dedicated_operator(tmp_path, monkeypatch):
    """HEALTHY CONTROL for the test above: the guard's own case is unchanged."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-operator-p2", window="operator")
    assert D.live_operator_sids(cfg, "p") == ["S-u-operator-p2"]


def test_live_operator_sids_sees_a_DECLARED_plain_operator(tmp_path, monkeypatch):
    """A declaration is authoritative: a session that declared itself a plain
    operator is one, whatever its window is called."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-user_session_x-p3",
                   window="user_session_x", roles=["operator"])
    assert D.live_operator_sids(cfg, "p") == ["S-u-user_session_x-p3"]


def test_operator_role_holders_returns_the_solo_session(tmp_path, monkeypatch):
    """ROUTING: «драйв должен драйвить именно ее» — the drive, the re-drive
    tick and a peer send to `operator` resolve to the session that is actually
    driving the board."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-universal_bsq_session-p1",
                   window="universal_bsq_session")
    assert D.operator_role_holders(cfg, "p") == ["S-u-universal_bsq_session-p1"]


def test_operator_role_holders_prefers_a_dedicated_operator(tmp_path, monkeypatch):
    """A PREFERENCE, not a union — «budding в оператора, на которого перейдет
    драйв». The moment an operator is budded off, the drive moves to it and the
    parent stops being the answer. Two dispatchers is the thing T-0472 forbids."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-universal_bsq_session-p1",
                   window="universal_bsq_session")
    _write_session(tmp_path, "p", "S-u-operator-p2", window="operator")
    assert D.operator_role_holders(cfg, "p") == ["S-u-operator-p2"]


def test_operator_role_holders_is_empty_when_nobody_holds_it(tmp_path, monkeypatch):
    """The case the escalation refusal below depends on: a project with only a
    dev running has NO operator, and this must say so rather than fall back to
    'somebody'."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-dev_thing-p9", window="dev_thing")
    assert D.operator_role_holders(cfg, "p") == []


# ---------------------------------------------------------------------------
# Escalation is conditioned on a recipient EXISTING
# ---------------------------------------------------------------------------

def test_escalation_to_an_unfilled_operator_is_REFUSED(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-dev_thing-p9", window="dev_thing")

    out = I.send(cfg, "p", "S-u-dev_thing-p9", "operator", "I am stuck")

    assert out["ok"] is False
    assert out["reason"] == "role-unfilled"
    assert out["delivered_to"] == []
    # The refusal has to tell the sender what to DO — an error naming only the
    # failure is what the old log line already was.
    assert "hold that role yourself" in out["error"]


def test_a_refused_escalation_writes_NO_inbox(tmp_path, monkeypatch):
    """The forbidden outcome is 'believed sent, received by nobody'. A refusal
    that still left a file behind would be a third state, worse than both."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-dev_thing-p9", window="dev_thing")
    I.send(cfg, "p", "S-u-dev_thing-p9", "operator", "I am stuck")
    chat = tmp_path / "data" / "p" / "_chat"
    assert not chat.exists() or list(chat.glob("inbox-*.log")) == []


def test_the_SAME_escalation_reaches_a_solo_session(tmp_path, monkeypatch):
    """The other half, and the one that makes the refusal correct rather than
    merely loud: when somebody DOES hold the role, the escalation lands."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-universal_bsq_session-p1",
                   window="universal_bsq_session")
    _write_session(tmp_path, "p", "S-u-dev_thing-p9", window="dev_thing")

    out = I.send(cfg, "p", "S-u-dev_thing-p9", "operator", "I am stuck")

    assert out["ok"] is True
    assert out["delivered_to"] == ["S-u-universal_bsq_session-p1"]


def test_an_empty_teamlead_fanout_is_still_quiet(tmp_path, monkeypatch):
    """GREEN GUARD, preserved: T-0790 ruled that an empty teamlead/dev/all
    fan-out is an ordinary state. A plain send is a BROADCAST, not an
    escalation, and widening it would have re-opened a noise decision that was
    made deliberately."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    for role in ("teamlead", "dev", "all"):
        out = I.send(cfg, "p", "S-u-dev_thing-p9", role, "anyone?")
        assert out["ok"] is True, role
        assert out["delivered_to"] == [], role


def test_a_DECLARED_BLOCK_to_an_unfilled_teamlead_is_refused(tmp_path, monkeypatch):
    """`--blocked` (T-0977) is the sender saying "I am STOPPED until this is
    answered". Addressed to a role nobody holds, that is the same dead
    escalation — and it is the exact clause the dev contract sends devs down."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    out = I.send(cfg, "p", "S-u-dev_thing-p9", "teamlead", "need a decision",
                 blocked=True)
    assert out["ok"] is False
    assert out["reason"] == "role-unfilled"


# ---------------------------------------------------------------------------
# The nudges follow the ROLE HELD
# ---------------------------------------------------------------------------

def test_multi_role_plan_is_silent_for_a_single_role_session():
    """The narrowness IS the design: every single-role session keeps its
    existing plan byte-for-byte."""
    for role in ("dev", "operator", "user-conversation", "teamlead"):
        assert IT.multi_role_plan(
            roles=(role,), meta={}, tasks_alive=True, board_pending=True,
        ) == (None, None)


def test_a_solo_session_gets_the_DRIVE_nudge_at_the_operator_cadence():
    """«Та сессия, на которой висит роль оператора, должна получать nudges, что
    движения по проекту нет, если drive on.»"""
    plan, deciding = IT.multi_role_plan(
        roles=S.SOLO_ROLES, meta={"drive": "on"}, tasks_alive=False,
        board_pending=True)
    assert plan == IT.PLAN_NUDGE
    # The DECIDING role is what `maybe_recycle` routes on, and it is the whole
    # reason this function returns two values — see the end-to-end test below.
    assert deciding == "operator"


def test_a_solo_session_with_an_EMPTY_board_is_not_nudged():
    """Fails in the safe direction. Both the md `drive` field and the project
    drive block default to ON, so without a real "there is work" input this arm
    would fire on every solo session forever — the nudge loop T-0948 exists to
    break."""
    assert IT.multi_role_plan(
        roles=S.SOLO_ROLES, meta={"drive": "on"}, tasks_alive=False,
        board_pending=False) == (None, None)


def test_a_solo_session_with_drive_OFF_is_not_nudged():
    assert IT.multi_role_plan(
        roles=S.SOLO_ROLES, meta={"drive": "off"}, tasks_alive=False,
        board_pending=True) == (None, None)


def test_a_solo_session_gets_the_TASK_nudge_at_the_dev_cadence():
    """«Та сессия, на которой висит роль девелопера по задаче, должна получать
    nudges по этой задаче.»"""
    plan, deciding = IT.multi_role_plan(
        roles=S.SOLO_ROLES, meta={"drive": "off"}, tasks_alive=True,
        board_pending=False)
    assert plan == IT.PLAN_NUDGE
    assert deciding == "dev"
    assert IT.worker_nudge_sec(deciding) == IT.dev_nudge_sec()


def test_the_two_arms_run_at_DIFFERENT_cadences():
    """Why the deciding role has to be returned at all. If the two nudges ran
    at the same cadence there would be nothing to decide — and picking the
    wrong one is T-0930: a session that is legitimately waiting, nudged every
    five minutes."""
    assert IT.operator_nudge_sec() != IT.dev_nudge_sec()


def test_the_nudge_cap_stops_a_multi_role_holder_too():
    """T-0948's escalation cap reaches this table as well, so widening a role's
    reach never buys an uncapped nudge loop."""
    assert IT.multi_role_plan(
        roles=S.SOLO_ROLES, meta={"drive": "on"}, tasks_alive=True,
        board_pending=True, nudge_capped=True) == (None, None)


def test_recycle_plan_is_unchanged_when_no_roles_are_passed():
    """REGRESSION PIN: every pre-T-0943 caller passes no `roles`, and must get
    exactly the plan it got before."""
    assert IT.recycle_plan(role="user-conversation", window="user_session_x",
                           meta={}, attached=False, tasks_alive=False) in (
        IT.PLAN_COMPACT_EXIT, IT.PLAN_STAY)
    assert IT.recycle_plan(role="dev", window="dev_x", meta={},
                           attached=False, tasks_alive=False) == IT.PLAN_HANDOFF_EXIT


def test_recycle_plan_nudges_a_solo_session_instead_of_recycling_it():
    """The end-to-end of the two tests above: the session that IS the operator
    is driven on rather than compact-exited while the board still has work."""
    assert IT.recycle_plan(
        role="user-conversation", window="universal_bsq_session",
        meta={"drive": "on"}, attached=False, tasks_alive=False,
        roles=S.SOLO_ROLES, board_pending=True) == IT.PLAN_NUDGE


def test_an_attached_pane_still_wins_over_everything():
    """A human is looking at it. That gate is first for a reason and this
    ticket must not have moved it."""
    assert IT.recycle_plan(
        role="user-conversation", window="universal_bsq_session",
        meta={"drive": "on"}, attached=True, tasks_alive=True,
        roles=S.SOLO_ROLES, board_pending=True) == IT.PLAN_STAY


# ---------------------------------------------------------------------------
# The drift dev-gate
# ---------------------------------------------------------------------------

def test_holds_dev_role_sees_a_solo_session():
    assert DR.holds_dev_role({"roles": list(S.SOLO_ROLES)}) is True


def test_holds_dev_role_healthy_and_negative_cases():
    assert DR.holds_dev_role({"roles": ["dev"], "role": "dev"}) is True
    assert DR.holds_dev_role({"roles": ["operator"], "role": "operator"}) is False
    # An older producer's row has no `roles` — it degrades to the single role,
    # not to "nobody is a dev".
    assert DR.holds_dev_role({"role": "dev"}) is True
    assert DR.holds_dev_role({"role": "teamlead"}) is False
    assert DR.holds_dev_role({}) is False


# ---------------------------------------------------------------------------
# END TO END: the drive nudge actually reaches a solo session's pane
# ---------------------------------------------------------------------------
#
# The unit tests above pin the DECISION. This one pins the EXECUTION, because
# the decision is worth nothing if `maybe_recycle` routes it to the wrong
# sender — which is precisely the shape of the defect it exists to fix: a
# correct-looking verdict that reached nobody.

def _full_cfg(tmp_path: Path, *, sid: str, window: str, extra_md: dict):
    import types as _t
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True)
    fm = {"sid": sid, "status": "active", "window": window, "cwd": str(repo),
          "claude_uuid": "uuid-" + sid, "task_id": "~"}
    fm.update(extra_md)
    S._write_session_metadata(sess / f"{sid}.md", fm)
    cfg = Config.load(cfg_dir)
    return _t.SimpleNamespace(projects=cfg.projects, data_dir=data_dir), repo


@pytest.fixture
def recycle_seams(monkeypatch):
    """Stub the tmux/telemetry seams `maybe_recycle` touches, and RECORD which
    of the two nudge senders it chose."""
    import time as _time
    from bot_squad_worker import autocompact as A

    chosen: dict = {"keepalive": 0, "worker": [], "idle_age": 5000.0}
    monkeypatch.setattr(A, "_pane_for", lambda sid, **kw: "%9")
    monkeypatch.setattr(A, "_capture_pane", lambda pane, **kw: "❯ \n")
    monkeypatch.setattr(IT, "_context_tokens", lambda cfg, slug, sid: 100)
    monkeypatch.setattr(IT.recycle_gate, "is_attached", lambda t, **kw: False)
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: _time.time() - chosen["idle_age"])

    def _keepalive(cfg, slug, sid, row, meta, md_path, now, pane, user_home):
        chosen["keepalive"] += 1
        return True

    def _worker(cfg, slug, sid, row, meta, md_path, now, pane, user_home, *,
                role=None):
        chosen["worker"].append(role)
        return True

    monkeypatch.setattr(IT, "_maybe_keepalive_nudge", _keepalive)
    monkeypatch.setattr(IT, "_maybe_worker_nudge", _worker)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    return chosen


def test_maybe_recycle_sends_a_solo_session_the_OPERATOR_keepalive(
        tmp_path, recycle_seams, monkeypatch):
    """The board has work, the drive is on, and the one session running holds
    the operator role — so it is driven on, on the OPERATOR's cadence, instead
    of being compact-exited on the user-conversation line."""
    import time as _time
    sid = "S-u-universal_bsq_session-p1"
    cfg, repo = _full_cfg(tmp_path, sid=sid, window=S.UNIVERSAL_WINDOW,
                          extra_md={"drive": "on"})
    monkeypatch.setattr(IT, "board_pending", lambda cfg, slug: True)
    row = {"sid": sid, "status": "active", "window": S.UNIVERSAL_WINDOW,
           "task_id": "~", "role": "user-conversation", "cwd": str(repo),
           "claude_uuid": "uuid-" + sid, "linux_user": ""}

    acted = IT.maybe_recycle(cfg, "bot-squad", row, now=_time.time(),
                             user_home="/home/x")

    assert acted is True
    assert recycle_seams["keepalive"] == 1
    assert recycle_seams["worker"] == []


def test_maybe_recycle_leaves_a_plain_attendant_on_its_old_line(
        tmp_path, recycle_seams, monkeypatch):
    """HEALTHY CONTROL, and the fixture differs along exactly the tested axis:
    same role, same drive, same board — a `user_session_*` window instead of
    the universal one. It holds only user-conversation, so it gets NO nudge and
    keeps the T-0945 compact-exit line."""
    import time as _time
    sid = "S-u-user_session_alex-p1"
    cfg, repo = _full_cfg(tmp_path, sid=sid, window="user_session_alex",
                          extra_md={"drive": "on"})
    monkeypatch.setattr(IT, "board_pending", lambda cfg, slug: True)
    row = {"sid": sid, "status": "active", "window": "user_session_alex",
           "task_id": "~", "role": "user-conversation", "cwd": str(repo),
           "claude_uuid": "uuid-" + sid, "linux_user": ""}

    IT.maybe_recycle(cfg, "bot-squad", row, now=_time.time(),
                     user_home="/home/x")

    assert recycle_seams["keepalive"] == 0
    assert recycle_seams["worker"] == []


# ---------------------------------------------------------------------------
# declare_roles — the WRITE side ("сессии должны сообщать эти роли системе")
# ---------------------------------------------------------------------------

@pytest.fixture
def no_rename(monkeypatch):
    """Record rename attempts instead of touching tmux. Returns the list of
    ``(sid, target)`` the declaration asked for."""
    calls: list = []
    monkeypatch.setattr(S, "_client_attached", lambda sid: False)
    monkeypatch.setattr(S, "sync_session_name",
                        lambda cfg, slug, sid, name: (
                            calls.append((sid, name)),
                            {"ok": True, "sid": sid, "new_sid": sid,
                             "name": name})[1])
    monkeypatch.setattr(S, "list_panes", lambda: [])
    return calls


def test_declare_roles_records_the_set_and_the_legacy_mirror(tmp_path, no_rename):
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    md = _write_session(tmp_path, "p", sid, window="universal_bsq_session")

    out = S.declare_roles(cfg, "p", sid, roles=["operator", "dev"])

    assert out["roles"] == ["operator", "dev"]
    assert out["label"] == "working-operator"
    assert out["previous"] == list(S.SOLO_ROLES)
    meta = S._read_session_metadata(md)
    assert meta["roles"] == ["operator", "dev"]
    # The single-valued mirror has to move too, or `_role_of` keeps answering
    # from the stale stamp and the declaration only half-takes.
    assert meta["role"] == "operator"


def test_drop_is_resolved_against_the_md_not_a_snapshot(tmp_path, no_rename):
    """«как только отпочковывает, сразу же начинает только всё делегировать» —
    budding is TOTAL, and each bud drops one role off whatever is on disk."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    _write_session(tmp_path, "p", sid, window="universal_bsq_session")

    first = S.declare_roles(cfg, "p", sid, drop=("dev",))
    assert set(first["roles"]) == {"user-conversation", "operator"}
    assert first["label"] == "talking-operator"

    second = S.declare_roles(cfg, "p", sid, drop=("operator",))
    assert second["roles"] == ["user-conversation"]
    assert second["label"] == "attendant"


def test_take_puts_a_role_back(tmp_path, no_rename):
    """`bsq bud absorb` — the deflation half."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-user_session_alex-p1"
    _write_session(tmp_path, "p", sid, window="user_session_alex")
    out = S.declare_roles(cfg, "p", sid, add=("dev",))
    assert set(out["roles"]) == {"user-conversation", "dev"}


def test_declaring_an_empty_set_writes_the_tombstone(tmp_path, no_rename):
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p1"
    md = _write_session(tmp_path, "p", sid, window="operator")
    out = S.declare_roles(cfg, "p", sid, roles=[])
    assert out["roles"] == []
    meta = S._read_session_metadata(md)
    assert meta["roles"] == []
    # The legacy mirror is cleared. It is WRITTEN as the `~` sentinel and READ
    # BACK as None (pyyaml resolves `~` to null) — both are falsy to
    # `_role_of`, which is the property that matters: nothing stale is left
    # answering the single-valued question.
    assert meta.get("role") in (None, "~")
    assert not meta.get("role")
    # ...and it STICKS: the window alone would otherwise re-mint the role.
    assert S.roles_of(meta) == ()
    assert S._role_of(meta) == "operator"   # the fallback the tombstone beats


def test_taking_operator_is_refused_beside_a_live_dedicated_operator(
        tmp_path, no_rename):
    from bot_squad_worker.actions import ActionError
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-operator-p2", window="operator")
    sid = "S-u-user_session_alex-p1"
    _write_session(tmp_path, "p", sid, window="user_session_alex")
    with pytest.raises(ActionError, match="exactly one dispatcher"):
        S.declare_roles(cfg, "p", sid, add=("operator",))


def test_a_solo_session_may_still_declare_the_operator_role_it_already_holds(
        tmp_path, no_rename):
    """HEALTHY CONTROL for the guard above: it fires on TAKING the role, not on
    restating one already held, or a solo session could never write down what
    it has been doing all along."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    _write_session(tmp_path, "p", sid, window="universal_bsq_session")
    out = S.declare_roles(cfg, "p", sid, roles=list(S.SOLO_ROLES))
    assert out["label"] == "solo-session"


def test_an_unknown_role_is_refused(tmp_path, no_rename):
    from bot_squad_worker.actions import ActionError
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p1"
    _write_session(tmp_path, "p", sid, window="operator")
    with pytest.raises(ActionError, match="unknown role"):
        S.declare_roles(cfg, "p", sid, add=("archduke",))


def test_roles_and_an_adjustment_together_are_refused(tmp_path, no_rename):
    from bot_squad_worker.actions import ActionError
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p1"
    _write_session(tmp_path, "p", sid, window="operator")
    with pytest.raises(ActionError, match="not both"):
        S.declare_roles(cfg, "p", sid, roles=["dev"], drop=("operator",))


# --- the name follows the set, and follows BUDDING ---------------------------

def test_the_window_is_renamed_to_match_the_new_set(tmp_path, no_rename):
    """«имя окна и сессии должно ставиться по [комбинации ролей]» — and because
    the bud verbs call this, the rename follows budding rather than being a
    separate housekeeping step nobody runs."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    _write_session(tmp_path, "p", sid, window="universal_bsq_session")

    out = S.declare_roles(cfg, "p", sid, drop=("dev",))

    assert no_rename == [(sid, "talking_operator")]
    assert out["renamed"] is True
    assert out["window"] == "talking_operator"


def test_no_rename_when_the_set_does_not_determine_a_name(tmp_path, no_rename):
    """An attendant keeps `user_session_<who>`: which USER it attends is not in
    the role set, and inventing a name from the set alone would lose it."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    _write_session(tmp_path, "p", sid, window="universal_bsq_session")
    out = S.declare_roles(cfg, "p", sid, roles=["user-conversation"])
    assert no_rename == []
    assert out["renamed"] is False
    assert "derivable" in out["rename_reason"]


def test_no_rename_when_the_name_is_already_right(tmp_path, no_rename):
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p1"
    _write_session(tmp_path, "p", sid, window="operator")
    out = S.declare_roles(cfg, "p", sid, roles=["operator"])
    assert no_rename == []
    assert out["renamed"] is False


def test_an_attached_pane_is_never_renamed_and_the_rename_is_REMEMBERED(
        tmp_path, monkeypatch):
    """T-1056: you do not steal a window from under him. But dropping the
    rename silently would leave the name permanently wrong, so the target is
    stamped and applies on the next declaration."""
    monkeypatch.setattr(S, "_client_attached", lambda sid: True)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    renames: list = []
    monkeypatch.setattr(S, "sync_session_name",
                        lambda *a, **k: renames.append(a) or {"ok": True})
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    md = _write_session(tmp_path, "p", sid, window="universal_bsq_session")

    out = S.declare_roles(cfg, "p", sid, drop=("dev",))

    assert renames == []
    assert out["renamed"] is False
    assert out["pending_window"] == "talking_operator"
    assert S._read_session_metadata(md)["pending_window"] == "talking_operator"


def test_the_attach_check_fails_CLOSED(monkeypatch):
    """A broken attach read must mean "assume he is watching". The wrong
    direction renames a window out from under a human mid-sentence; this
    direction only defers."""
    def _boom(sid, **kw):
        raise RuntimeError("tmux is down")
    monkeypatch.setattr("bot_squad_worker.autocompact._pane_for", _boom)
    assert S._client_attached("S-whatever") is True


# ---------------------------------------------------------------------------
# THE THREE COPIES MUST AGREE
# ---------------------------------------------------------------------------
#
# `sessions.roles_of`/`roles_label` are mirrored in `scripts/cli/bsq`
# (stdlib-only by design — it must not import the worker tree) and, for the
# contract-name half, in `scripts/hooks/derive_role.sh`. The single-role
# derivation already has this net (`test_bsq_role_derivation_mirror.py`); this
# is the same net for the SET, and it exists for the same measured reason: the
# copy that rotted was the unpinned one.

import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader

_REPO = Path(__file__).resolve().parents[2]


def _load_bsq():
    loader = SourceFileLoader("bsq_mod_t0943", str(_REPO / "scripts" / "cli" / "bsq"))
    spec = importlib.util.spec_from_loader("bsq_mod_t0943", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_ROLES_WINDOWS = (
    "universal_bsq_session",
    "user_session_alex",
    "gu_abc-user-conversation",
    "operator",
    "bot-squad-operator",
    "talking_operator",
    "working_operator",
    "dev_add_a_button",
    "feature-TL",
    "nightly-qa",
)


@pytest.mark.parametrize("window", _ROLES_WINDOWS)
def test_bsq_derives_the_same_role_SET_as_the_worker(window):
    bsq = _load_bsq()
    assert tuple(bsq._roles_for(window)) == S.roles_of({"window": window}), window


@pytest.mark.parametrize("window", _ROLES_WINDOWS)
def test_bsq_names_the_same_STATE_as_the_worker(window):
    bsq = _load_bsq()
    roles = S.roles_of({"window": window})
    assert bsq._roles_label(roles) == S.roles_label(roles), window


def test_bsq_honours_a_declaration_the_same_way():
    bsq = _load_bsq()
    for meta in ({"roles": ["operator", "dev"]}, {"roles": []},
                 {"roles": "~"}, {"roles": "[operator, dev]"}):
        assert tuple(bsq._roles_for("universal_bsq_session", meta)) == \
            S.roles_of({**meta, "window": "universal_bsq_session"}), meta


def test_the_bash_hook_resolves_the_same_contract_file():
    """The SessionStart banner points at a contract path; a solo session must
    be sent to `solo-session.md`, not to the user-conversation contract whose
    head told it to hand the work to sessions that did not exist."""
    bsq = _load_bsq()
    script = _REPO / "scripts" / "hooks" / "derive_role.sh"
    for window in _ROLES_WINDOWS:
        out = subprocess.run(
            ["bash", "-c", f'. "{script}"; bsq_contract_role "{window}"'],
            capture_output=True, text=True, timeout=15)
        assert out.returncode == 0, out.stderr
        first_contract = bsq._contract_files_for(S.roles_of({"window": window}))[0]
        assert out.stdout.strip() + ".md" == first_contract, window


def test_every_contract_file_bsq_can_serve_EXISTS():
    """The T-0778 failure mode, re-armed for the file this ticket adds: a
    correctly-derived session landing on a contract that is not on disk."""
    bsq = _load_bsq()
    roles_dir = _REPO / "api" / "app" / "resources" / "roles"
    for name in bsq._ROLE_FILES.values():
        assert (roles_dir / name).exists(), name


def test_the_solo_contract_is_served_ABOVE_its_primary_not_instead_of_it():
    """A preface that REPLACED the primary contract would strand a solo session
    without the conversation mechanics it needs at boot; a preface printed
    below the body it corrects would be the "correction at the head" failure
    inverted. Order is the claim."""
    bsq = _load_bsq()
    assert bsq._contract_files_for(S.SOLO_ROLES) == [
        "solo-session.md", "user-conversation.md"]


def test_the_user_conversation_contract_no_longer_offloads_unconditionally():
    """The clause the live session actually obeyed. It is not deleted — it is
    HIS 2026-07-18 instruction — it is conditioned on the recipient existing."""
    text = (_REPO / "api" / "app" / "resources" / "roles"
            / "user-conversation.md").read_text()
    i = text.index("NOT execute or orchestrate the work itself")
    clause = text[i:i + 400]
    assert "when there is somebody to offload it to" in clause


# ---------------------------------------------------------------------------
# THE REOPEN (2026-09-07): a right label that changed nothing
# ---------------------------------------------------------------------------
#
# A user-conversation session did operator work for three hours; when an
# operator finally appeared the two ran side by side with the mail going to the
# wrong one. `bsq role set operator,user-conversation` then printed
# `talking-operator` — and changed NOTHING, because every consumer resolved the
# old way. `live_user_conversation_sids`, which `ensure_user_conversation` uses
# to find a live attendant, filtered on `_role_of` — the SINGLE-VALUED resolver,
# `role:` plus the window — and never read `roles:` at all.
#
# That is a worse shape than a wrong label: the label is RIGHT and nothing
# downstream observes it. A watchrobot operator nearly shut its live attendant
# down on the strength of that printed label, and stopped only because it read
# the code first.

def _live(monkeypatch, *sids):
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set(sids))
    monkeypatch.setattr(S, "list_panes", lambda: [])


def test_a_declared_talking_operator_IS_a_live_attendant(tmp_path, monkeypatch):
    """THE ACCEPTANCE CONDITION for the reopen's fourth gap. Red before the fix:
    the declaration existed and this function could not see it."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p640"
    _write_session(tmp_path, "p", sid, window="operator",
                   roles=["operator", "user-conversation"])
    _live(monkeypatch, sid)

    rows = S.live_user_conversation_sids(cfg, "p")

    assert [r["sid"] for r in rows] == [sid], (
        "a session that DECLARED user-conversation is an attendant; "
        "ensure_user_conversation must find it instead of spawning a duplicate")


def test_the_healthy_attendant_is_unchanged(tmp_path, monkeypatch):
    """HEALTHY CONTROL FIRST, and it differs along exactly the tested axis: an
    ordinary attendant, no declaration, found the way it always was."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    _write_session(tmp_path, "p", sid, window="universal_bsq_session")
    _live(monkeypatch, sid)
    assert [r["sid"] for r in S.live_user_conversation_sids(cfg, "p")] == [sid]


def test_a_plain_operator_is_still_NOT_an_attendant(tmp_path, monkeypatch):
    """The negative the widening must not swallow. A pure operator holds no
    user-conversation role and must stay invisible here, or every operator
    becomes an attendant and the dup-spawn guard inverts."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p2"
    _write_session(tmp_path, "p", sid, window="operator")
    _live(monkeypatch, sid)
    assert S.live_user_conversation_sids(cfg, "p") == []


def test_a_tombstoned_session_is_not_an_attendant(tmp_path, monkeypatch):
    """`roles: []` means it holds nothing, including this."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-universal_bsq_session-p1"
    _write_session(tmp_path, "p", sid, window="universal_bsq_session", roles=[])
    _live(monkeypatch, sid)
    assert S.live_user_conversation_sids(cfg, "p") == []


def test_a_declared_holder_answers_the_gid_keyed_lookup_too(tmp_path, monkeypatch):
    """`ensure_user_conversation` asks the (slug, gid) question, not the roster
    one, and a declared talking-operator must answer it — FOR THE USER IT IS
    BOUND TO.

    THE CLAIM CHANGED, THE TEST DID NOT MOVE. As first written this asserted
    that a declared holder with NO gid stamped answers for an ARBITRARY gid
    ("gu_abc"). That was the P0: it is the defect written down as a
    requirement, and it is why the shipped regression looked correct to me.
    Holding `user-conversation` says what a session DOES, never WHOSE it is.
    The legitimate requirement underneath — a talking-operator must not be
    invisible to `ensure_user_conversation` — survives, now carried by the
    BINDING recorded when the role was taken. The sibling
    `test_an_unbound_declaration_is_a_candidate_for_nobody` pins the other
    half: no binding, no answer, for anybody."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p640"
    _write_session(tmp_path, "p", sid, window="operator",
                   roles=["operator", "user-conversation"], uc_gid="gu_abc")
    _live(monkeypatch, sid)
    assert S.live_user_conversation_sid(cfg, "p", "gu_abc") == sid
    # ...and NOT for anybody else, which is the assertion the original lacked.
    assert S.live_user_conversation_sid(cfg, "p", "gu_other") is None


def test_binding_a_ticket_to_a_solo_session_is_allowed(tmp_path, monkeypatch):
    """`bsq bud absorb` — a solo session holds `dev` and must be able to take a
    ticket. The guard asked `_role_of(...) != "dev"`, which a solo session
    always fails."""
    assert "dev" in S.roles_of({"window": S.UNIVERSAL_WINDOW})


# --- role_holders: routing by role, for every role in the model -------------

def test_role_holders_finds_every_declared_role(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    _write_session(tmp_path, "p", "S-u-operator-p640", window="operator",
                   roles=["operator", "user-conversation"])
    _write_session(tmp_path, "p", "S-u-dev_x-p9", window="dev_x")
    assert S.role_holders(cfg, "p", "user-conversation") == ["S-u-operator-p640"]
    assert S.role_holders(cfg, "p", "operator") == ["S-u-operator-p640"]
    assert S.role_holders(cfg, "p", "dev") == ["S-u-dev_x-p9"]
    assert S.role_holders(cfg, "p", "qa") == []


def test_user_conversation_is_addressable_on_the_bus(tmp_path, monkeypatch):
    """GAP 1, measured by the operator as a refusal: the one role whose entire
    purpose is being a human's address could not be addressed by role."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    sid = "S-u-universal_bsq_session-p1"
    _write_session(tmp_path, "p", sid, window="universal_bsq_session")
    assert "user-conversation" in I._ROLE_KEYWORDS
    out = I.send(cfg, "p", "S-u-dev_x-p9", "user-conversation", "for the human")
    assert out["ok"] is True
    assert out["delivered_to"] == [sid]


def test_an_unknown_target_is_still_not_a_role(tmp_path, monkeypatch):
    """The resolver must NOT have been widened into something that accepts
    anything — the operator was explicit about that. An unknown name still
    falls through to the literal-SID branch."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    assert "archduke" not in I._ROLE_KEYWORDS
    cfg = _make_cfg(tmp_path)
    assert I._resolve_recipients(cfg, "p", "archduke") == ["archduke"]


# --- DoD 4: the bad state must be VISIBLE -----------------------------------

def _row(sid, roles, activity="running", status="active"):
    return {"sid": sid, "roles": list(roles), "activity": activity,
            "status": status, "role": roles[0] if roles else ""}


def test_the_incident_state_is_reported(tmp_path):
    """A live user-conversation beside a live IDLE operator — the exact state
    that caused this reopen. Today nothing said so."""
    cfg = _make_cfg(tmp_path)
    rows = [_row("S-u-universal_bsq_session-p1", ["user-conversation"]),
            _row("S-u-operator-p640", ["operator"], activity="idle")]
    found = S.role_split_findings(cfg, "p", rows)
    assert len(found) == 1
    assert found[0]["kind"] == S.SPLIT_ATTENDANT_AND_IDLE_OPERATOR
    assert set(found[0]["sids"]) == {"S-u-universal_bsq_session-p1", "S-u-operator-p640"}
    assert "talking-operator" in found[0]["message"].lower()


def test_an_attendant_beside_a_WORKING_operator_is_not_flagged(tmp_path):
    """HEALTHY CONTROL, differing along exactly the tested axis: same two roles,
    same two sessions, operator RUNNING. That is a legitimate row of his own
    table — what budding user-session off is for — and a warning that fires on
    the healthy case is one nobody reads."""
    cfg = _make_cfg(tmp_path)
    rows = [_row("S-u-universal_bsq_session-p1", ["user-conversation"]),
            _row("S-u-operator-p640", ["operator"], activity="running")]
    assert S.role_split_findings(cfg, "p", rows) == []


def test_a_talking_operator_is_not_flagged(tmp_path):
    """The COLLAPSED state is the fix, so it must not be reported as the defect."""
    cfg = _make_cfg(tmp_path)
    rows = [_row("S-u-operator-p640", ["operator", "user-conversation"],
                 activity="idle")]
    assert S.role_split_findings(cfg, "p", rows) == []


def test_a_solo_session_alone_is_not_flagged(tmp_path):
    cfg = _make_cfg(tmp_path)
    rows = [_row("S-u-universal_bsq_session-p1", list(S.SOLO_ROLES))]
    assert S.role_split_findings(cfg, "p", rows) == []


def test_a_DERIVED_dev_does_not_receive_a_dev_broadcast(tmp_path, monkeypatch):
    """The regression this union caused and the reason `declared_only` exists.

    `_derive_role`'s default branch returns "dev", so every task-less session
    with an unmarked window DERIVES the dev role. Unioning derived holders into
    the `dev` fan-out delivered a dev broadcast to every team-lead on the
    project. A DECLARATION may widen who receives a broadcast; a DERIVATION —
    which is just the fallthrough — may not.
    """
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    _write_session(tmp_path, "p", "S-u-tl1-p0", window="tl1")      # derives dev
    _write_session(tmp_path, "p", "S-u-w1-p2", window="w1")        # derives dev
    # ...but neither DECLARED it, so neither is a role-holder for the fan-out.
    assert S.role_holders(cfg, "p", "dev", declared_only=True) == []
    # ...while the derived read still sees both, which is what makes the
    # distinction load-bearing rather than cosmetic.
    assert len(S.role_holders(cfg, "p", "dev")) == 2


def test_a_DECLARED_dev_does_receive_one(tmp_path, monkeypatch):
    """The other direction: a solo session holds `dev` with no ticket, and once
    it says so it is in the dev fan-out."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [])
    _write_session(tmp_path, "p", "S-u-universal_bsq_session-p1",
                   window="universal_bsq_session", roles=list(S.SOLO_ROLES))
    assert S.role_holders(cfg, "p", "dev", declared_only=True) == [
        "S-u-universal_bsq_session-p1"]


# ---------------------------------------------------------------------------
# The ticket fan-out asked the GUARD a ROUTING question
#
# Found live, on the install, 2026-09-07: `bsq ticket note` printed "nobody is
# bound and no live operator — this write nudges no one" on every write while
# S-almdudleer-operator-p640 was live and had DECLARED roles
# [operator, user-conversation]. Measured against the running code:
#     live_operator_sids    -> []
#     operator_role_holders -> ['S-almdudleer-operator-p640']
# The counterfactual was run, not assumed: the pre-T-0943 predicate
# `_role_of(meta) == "operator"` returned [p640], the narrowed one returns [],
# and what the narrowing lost was exactly that session.
#
# The narrowing is RIGHT — it is the continue-vs-respawn singleton, and
# counting a multi-role holder there refuses the `bsq bud operator` handover.
# The defect is that `ticket_watch.resolve_fallbacks` answers "who should hear
# about this ticket" with the SINGLETON's answer. Declaring the truth about
# itself turned off the operator's own ticket notifications.
# ---------------------------------------------------------------------------

def _fanout_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "list_panes", lambda: [])
    return _make_cfg(tmp_path)


def test_ticket_fanout_reaches_a_declared_talking_operator(tmp_path, monkeypatch):
    """THE DEFECT. A session holding operator + user-conversation IS the
    operator for routing purposes — «та сессия, на которой висит роль
    оператора, должна получать nudges». An unheld ticket must reach it."""
    from bot_squad_worker import ticket_watch as TW
    cfg = _fanout_cfg(tmp_path, monkeypatch)
    _write_session(tmp_path, "p", "S-u-operator-p1", window="operator",
                   roles=["operator", "user-conversation"])
    sids, why = TW.recipients_for(cfg, "p", "T-1", rows=[])
    assert sids == ["S-u-operator-p1"]
    assert why == "operator"


def test_ticket_fanout_still_reaches_a_dedicated_operator(tmp_path, monkeypatch):
    """HEALTHY CONTROL: the ordinary single-role case is byte-identical. A
    widening that also changes the ordinary case is not a widening."""
    from bot_squad_worker import ticket_watch as TW
    cfg = _fanout_cfg(tmp_path, monkeypatch)
    _write_session(tmp_path, "p", "S-u-operator-p2", window="operator")
    sids, why = TW.recipients_for(cfg, "p", "T-1", rows=[])
    assert sids == ["S-u-operator-p2"]
    assert why == "operator"


def test_ticket_fanout_prefers_the_dedicated_operator_over_the_parent(
        tmp_path, monkeypatch):
    """Routing is a PREFERENCE, not a union: once the drive has been budded
    off, the ticket follows it and the parent stops being the answer. Two
    dispatchers is the thing T-0472 forbids."""
    from bot_squad_worker import ticket_watch as TW
    cfg = _fanout_cfg(tmp_path, monkeypatch)
    _write_session(tmp_path, "p", "S-u-universal_bsq_session-p1",
                   window="universal_bsq_session")
    _write_session(tmp_path, "p", "S-u-operator-p2", window="operator")
    sids, _why = TW.recipients_for(cfg, "p", "T-1", rows=[])
    assert sids == ["S-u-operator-p2"]


def test_ticket_fanout_is_still_empty_when_nobody_holds_the_operator_role(
        tmp_path, monkeypatch):
    """NEGATIVE CONTROL, and the reason this is a fix and not a widening into
    'somebody': a board with only a dev running still has no destination for an
    unheld ticket, and must say so rather than invent one."""
    from bot_squad_worker import ticket_watch as TW
    cfg = _fanout_cfg(tmp_path, monkeypatch)
    _write_session(tmp_path, "p", "S-u-dev_thing-p9", window="dev_thing")
    sids, _why = TW.recipients_for(cfg, "p", "T-1", rows=[])
    assert sids == []


def test_the_singleton_guard_is_untouched_by_the_routing_fix(tmp_path, monkeypatch):
    """GUARD PRESERVATION. Routing the fan-out must NOT widen the singleton:
    if it did, `bsq bud operator` would start refusing the handover as a
    duplicate. Same fixture as the test above it, opposite expectation — that
    is the whole point of splitting the two questions."""
    cfg = _fanout_cfg(tmp_path, monkeypatch)
    _write_session(tmp_path, "p", "S-u-operator-p1", window="operator",
                   roles=["operator", "user-conversation"])
    assert D.live_operator_sids(cfg, "p") == []
    assert D.operator_role_holders(cfg, "p") == ["S-u-operator-p1"]


# ---------------------------------------------------------------------------
# EXCLUSIVITY: "is anyone already driving this board?"
#
# The second defect from the same narrowing, and the correction to a piece of
# reasoning that both this ticket's author and the operator ratified. The
# original justification for making `live_operator_sids` dedicated-only was
# that counting a multi-role holder would refuse the `bsq bud operator`
# handover. The comment beside the spawn guard in actions.py says the opposite
# and is right: THE HANDOVER DOES NOT COME THROUGH THAT GUARD AT ALL — it calls
# sessions.spawn directly via operator_redrive and releases the seat itself.
# So the narrowing bought nothing there and cost the T-0472 check: an operator
# spawn, or a seat claim, was PERMITTED beside a live talking-operator.
#
# The right shape is a requester-identity argument, not a narrower predicate:
# exclusivity counts ANY operator-role holder, and the SPAWN-PERMISSION
# question excludes the session doing the handover. A predicate that answers
# "is anyone driving" by excluding certain drivers is answering a different
# question — which is this ticket's whole defect class, arriving inside the
# fix for it.
# ---------------------------------------------------------------------------

def test_exclusivity_sees_a_talking_operator_that_the_guard_misses(
        tmp_path, monkeypatch):
    """THE DEFECT, and the two answers side by side so the difference is the
    assertion rather than a claim about it."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-operator-p1", window="operator",
                   roles=["operator", "user-conversation"])
    assert D.live_operator_sids(cfg, "p") == []
    assert D.operator_exclusivity_sids(cfg, "p") == ["S-u-operator-p1"]


def test_exclusivity_excludes_the_requester_so_a_handover_is_never_refused(
        tmp_path, monkeypatch):
    """The requester-identity half. A session may always hand its OWN drive
    over; without this the widening would refuse the handover for real, which
    is the thing the narrowing was wrongly believed to prevent."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-operator-p1", window="operator",
                   roles=["operator", "user-conversation"])
    assert D.operator_exclusivity_sids(
        cfg, "p", exclude_sid="S-u-operator-p1") == []


def test_exclusivity_is_a_union_where_routing_is_a_preference(
        tmp_path, monkeypatch):
    """A dedicated operator running BESIDE a talking one is exactly the state
    exclusivity exists to catch, so it must see BOTH. Routing must still pick
    one — reporting only the preferred sid would hide the violation behind the
    answer."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-operator-p2", window="operator")
    _write_session(tmp_path, "p", "S-u-universal-p1",
                   window="universal_bsq_session")
    assert D.operator_exclusivity_sids(cfg, "p") == [
        "S-u-operator-p2", "S-u-universal-p1"]
    assert D.operator_role_holders(cfg, "p") == ["S-u-operator-p2"]


def test_exclusivity_is_empty_on_a_board_with_only_a_dev(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: this is a widening to WHOEVER HOLDS THE ROLE, not a
    widening to 'somebody'. A board with no operator-role holder still refuses
    nothing."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-dev_thing-p9", window="dev_thing")
    assert D.operator_exclusivity_sids(cfg, "p") == []


def test_exclusivity_still_sees_a_plain_dedicated_operator(tmp_path, monkeypatch):
    """HEALTHY CONTROL: the ordinary single-role board is unchanged."""
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-operator-p2", window="operator")
    assert D.operator_exclusivity_sids(cfg, "p") == ["S-u-operator-p2"]


def test_seat_claim_is_refused_beside_a_live_talking_operator(
        tmp_path, monkeypatch):
    """AT THE CALL SITE, not at the resolver: four live defects on this ticket
    were invisible to a suite that only tested the model of the code.

    The claimant must be the ROOT user-conversation session or `eligibility`
    refuses it first and the test would pass for the wrong reason — the gate
    under test would never be reached."""
    from bot_squad_worker import operator_seat as SEAT
    from bot_squad_worker.actions import ActionError
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    _write_session(tmp_path, "p", "S-u-operator-p1", window="operator",
                   roles=["operator", "user-conversation"])
    _write_session(tmp_path, "p", "S-u-gu_abc-user-conversation-p5",
                   window="gu_abc-user-conversation")
    with pytest.raises(ActionError) as exc:
        SEAT.claim(cfg, "p", "S-u-gu_abc-user-conversation-p5")
    msg = str(exc.value)
    assert "S-u-operator-p1" in msg, msg
    assert "already driving this board" in msg, msg


def test_seat_claim_by_the_driver_itself_is_not_refused_by_its_own_drive(
        tmp_path, monkeypatch):
    """The requester-identity half AT THE CALL SITE, and it is reachable: the
    root is the only session eligible for the seat, and a root that has
    declared `roles: [operator, user-conversation]` is itself an operator-role
    holder. Without the requester exclusion the widened gate would refuse it
    BECAUSE OF ITSELF — a guard that a session can never satisfy."""
    from bot_squad_worker import operator_seat as SEAT
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    sid = "S-u-gu_abc-user-conversation-p5"
    _write_session(tmp_path, "p", sid, window="gu_abc-user-conversation",
                   roles=["operator", "user-conversation"])
    rec = SEAT.claim(cfg, "p", sid)
    assert rec["ok"] is True
    assert (rec["seat"] or {}).get("sid") == sid


# ---------------------------------------------------------------------------
# P0 REGRESSION FROM 993d4cb: identity must never be inferred from a role
#
# My reopen commit added a THIRD disjunct to `live_user_conversation_sid` that
# asked only "does this session hold user-conversation". It carried NO user
# identity while arms one and two are user-scoped, so the per-user lookup
# returned the FIRST live user-conversation session in sorted md order, whoever
# owned it. `live_user_conversation_sid` is the single-attendant invariant
# behind `ensure_user_conversation`: non-None means "this user already has an
# attendant, route the inbound there instead of spawning". So on a box with
# more than one tenant, USER B'S MESSAGE WAS ROUTED INTO USER A'S SESSION.
#
# Two compounding mistakes, and the second is the one that made the blast
# radius wide: the arm asked `roles_of`, WHICH FALLS BACK TO DERIVATION. So it
# fired for sessions that never declared anything — a post-rename attendant
# with no `roles` field, a legacy pre-rename attendant, another user's
# attendant. That is the rule I had written down myself the same day while
# fixing the peer fan-out — A DECLARATION MAY WIDEN A BROADCAST, A DERIVATION
# MAY NOT — and did not apply here.
#
# THE RULE (operator, 2026-09-07): IDENTITY IS NEVER INFERRED FROM A ROLE.
# Holding `user-conversation` says what a session DOES, never WHOSE it is. A
# session with no user binding is not a candidate for ANY user: return None and
# let a fresh attendant spawn. A wrong attendant is a stranger reading
# someone's conversation; a spurious spawn is a wasted process. FAIL CLOSED.
#
# LAST-RESORT ORDERING WAS PROPOSED AND REJECTED BY MEASUREMENT, not argument:
# with the asker having no candidate of their own, ordering never gets a chance
# to prefer anything and control falls straight through to the identity-free
# arm, which hands back the stranger anyway. It converts the leak from ALWAYS
# to ONLY-WHEN-THE-ASKER-HAS-NOTHING-LIVE — rarer, invisible, worse to debug.
# `test_no_candidate_of_their_own_still_refuses_a_strangers_attendant` is that
# measurement, pinned.
# ---------------------------------------------------------------------------

def _uc_md(tmp_path, sid, *, window="", gid="", roles=None, uc_gid=""):
    d = tmp_path / "p" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"sid: {sid}", "status: active", "task_id: ~"]
    if window:
        lines.append(f"window: {window}")
    if gid:
        lines.append(f"global_user_id: {gid}")
    if uc_gid:
        lines.append(f"user_conversation_gid: {uc_gid}")
    if roles is not None:
        lines.append("roles: [" + ", ".join(roles) + "]")
    lines += ["---", ""]
    (d / f"{sid}.md").write_text("\n".join(lines))


def _uc_cfg(tmp_path):
    return types.SimpleNamespace(data_dir=tmp_path, projects={"p": object()})


# --- the four shapes that WERE firing, one test each -------------------------

def test_another_users_post_rename_attendant_is_not_returned(tmp_path, monkeypatch):
    """Shape 1 — the shipped failure. A post-rename attendant carries its user
    in the `global_user_id` FIELD and a user-facing window that no longer
    spells the gid, so only the field can tell two of them apart."""
    a = "S-u-universal_bsq_session-p3"
    _uc_md(tmp_path, a, window="universal_bsq_session", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_b") is None


def test_a_post_rename_attendant_with_no_roles_field_is_not_returned(
        tmp_path, monkeypatch):
    """Shape 2 — and the one that shows the arm was never about declarations:
    this session declares NOTHING. Its `user-conversation` comes from the
    WINDOW NAME via `roles_of`'s derivation fallback."""
    a = "S-u-user_session_flomaster-p4"
    _uc_md(tmp_path, a, window="user_session_flomaster", gid="gu_a")
    assert S.declared_roles(S._read_session_metadata(
        tmp_path / "p" / "sessions" / f"{a}.md")) is None, "fixture must be UNdeclared"
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_b") is None


def test_a_legacy_pre_rename_attendant_of_another_user_is_not_returned(
        tmp_path, monkeypatch):
    """Shape 3 — the legacy `<gid>-user-conversation` window shape. It is
    another user's by construction; arm two must match it only for ITS user."""
    a = "S-u-gu_a-user-conversation-p6"
    _uc_md(tmp_path, a, window="gu_a-user-conversation")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_b") is None


def test_another_users_declared_talking_operator_is_not_returned(
        tmp_path, monkeypatch):
    """Shape 4 — the case 993d4cb was actually written for, asked for the WRONG
    user. Declaring `user-conversation` says what it does, never whose it is."""
    a = "S-u-operator-p7"
    _uc_md(tmp_path, a, window="operator", gid="gu_a",
           roles=["operator", "user-conversation"], uc_gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_b") is None


def test_no_candidate_of_their_own_still_refuses_a_strangers_attendant(
        tmp_path, monkeypatch):
    """THE OPERATOR'S REQUIRED CASE, and the measurement that rejected
    last-resort ordering. The asker has NO gid-matched and NO window-matched
    candidate at all, so ordering never gets to prefer anything: a
    last-resort arm falls straight through and hands back the stranger.
    FAIL CLOSED — None, and let a fresh attendant spawn."""
    a = "S-u-operator-p3"
    _uc_md(tmp_path, a, window="operator", gid="gu_a",
           roles=["operator", "user-conversation"], uc_gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_b") is None


# --- the two that must stay silent ------------------------------------------

def test_a_plain_operator_is_never_an_attendant(tmp_path, monkeypatch):
    """MUST NOT FIRE, and it did not before either — pinned so a later widening
    of the role test cannot quietly recruit it."""
    a = "S-u-operator-p8"
    _uc_md(tmp_path, a, window="operator", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_b") is None
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_a") == a


def test_a_dev_is_never_an_attendant(tmp_path, monkeypatch):
    """MUST NOT FIRE."""
    a = "S-u-dev_thing-p9"
    _uc_md(tmp_path, a, window="dev_thing")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_b") is None


# --- the gap 993d4cb existed to close must STAY closed -----------------------

def test_a_declared_attendant_is_found_for_the_user_it_is_BOUND_to(
        tmp_path, monkeypatch):
    """The intent of 993d4cb, surviving whole — via a BINDING rather than a
    role. A talking-operator that TOOK `user-conversation` records the owning
    gid at that moment, and this lookup reads that binding. Without this the
    fix would be a plain revert and the duplicate-spawn gap would reopen."""
    a = "S-u-operator-p3"
    _uc_md(tmp_path, a, window="operator",
           roles=["operator", "user-conversation"], uc_gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_a") == a


def test_an_unbound_declaration_is_a_candidate_for_nobody(tmp_path, monkeypatch):
    """FAIL CLOSED, stated as an assertion: a session that declared
    `user-conversation` but carries NO binding attends nobody, so it answers
    for nobody — not even when it is the only live session on the project."""
    a = "S-u-operator-p3"
    _uc_md(tmp_path, a, window="operator",
           roles=["operator", "user-conversation"])
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_a") is None


# --- healthy controls: the two original arms are untouched -------------------

def test_the_gid_arm_still_resolves_its_own_user(tmp_path, monkeypatch):
    a = "S-u-universal_bsq_session-p3"
    _uc_md(tmp_path, a, window="universal_bsq_session", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_a") == a


def test_the_legacy_window_arm_still_resolves_its_own_user(tmp_path, monkeypatch):
    a = "S-u-gu_a-user-conversation-p6"
    _uc_md(tmp_path, a, window="gu_a-user-conversation")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.live_user_conversation_sid(_uc_cfg(tmp_path), "p", "gu_a") == a


# ---------------------------------------------------------------------------
# The WRITE side of the P0 fix: taking `user-conversation` records WHOSE
# conversation it is. Without this the resolver's third arm reads a binding
# that nothing ever writes — a mechanism nobody can invoke.
# ---------------------------------------------------------------------------

def test_taking_user_conversation_records_the_owner(tmp_path, no_rename):
    """«та сессия, которая держит на себе общение с юзером» — WHICH user. The
    role says what the session does; this says whose."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p1"
    md = _write_session(tmp_path, "p", sid, window="operator", roles=["operator"])
    S.declare_roles(cfg, "p", sid, add=("user-conversation",),
                    global_user_id="gu_abc")
    meta = S._read_session_metadata(md)
    assert S.user_conversation_binding(meta) == "gu_abc"
    assert meta["roles"] == ["operator", "user-conversation"]


def test_taking_it_with_no_owner_anywhere_leaves_it_UNBOUND(tmp_path, no_rename):
    """FAIL CLOSED, and deliberately not an error: an unbound declaration is a
    legitimate visible state. What it must never be is a silent match for
    whoever asks first."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p1"
    md = _write_session(tmp_path, "p", sid, window="operator", roles=["operator"])
    S.declare_roles(cfg, "p", sid, add=("user-conversation",))
    meta = S._read_session_metadata(md)
    assert S.user_conversation_binding(meta) == ""


def test_the_owner_is_taken_from_the_spawn_stamp_when_not_passed(
        tmp_path, no_rename):
    """«from the thing that has it» — a session already stamped with its user
    at spawn does not need the caller to repeat it."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-user_session_x-p1"
    md = tmp_path / "data" / "p" / "sessions" / f"{sid}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nsid: %s\nstatus: active\ntask_id: ~\n"
                  "window: user_session_x\nglobal_user_id: gu_spawned\n---\n" % sid)
    S.declare_roles(cfg, "p", sid, add=("user-conversation",))
    assert S.user_conversation_binding(S._read_session_metadata(md)) == "gu_spawned"


def test_dropping_user_conversation_clears_the_binding(tmp_path, no_rename):
    """A session that handed the conversation away must stop answering for that
    user — otherwise the binding outlives the role that justified it."""
    cfg = _make_cfg(tmp_path)
    sid = "S-u-operator-p1"
    md = _write_session(tmp_path, "p", sid, window="operator", roles=["operator"])
    S.declare_roles(cfg, "p", sid, add=("user-conversation",),
                    global_user_id="gu_abc")
    assert S.user_conversation_binding(S._read_session_metadata(md)) == "gu_abc"
    S.declare_roles(cfg, "p", sid, drop=("user-conversation",))
    assert S.user_conversation_binding(S._read_session_metadata(md)) == ""


def test_two_live_attendants_sharing_a_window_shape_are_told_apart(
        tmp_path, monkeypatch):
    """TWO USERS WHO SHARE ONE ATTRIBUTE AND DIFFER IN THE BOUNDARY — the shape
    that reads fine and fails in production.

    Both are post-rename attendants, so both carry a user-facing window that no
    longer spells the gid: the WINDOW SHAPE is shared and only the FIELD is the
    boundary. The wrong one sorts first in md order, so a lookup that stops at
    "is this a user-conversation session" answers with it.

    (The same class bit the neighbouring fleet twice today on a different
    surface: a process whose NAME and uid matched theirs while its user
    NAMESPACE did not. Matching one attribute is not identity.)"""
    a = "S-u-universal_bsq_session-p3"       # sorts before b
    b = "S-u-user_session_flomaster-p5"
    _uc_md(tmp_path, a, window="universal_bsq_session", gid="gu_a")
    _uc_md(tmp_path, b, window="user_session_flomaster", gid="gu_b")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a, b})
    cfg = _uc_cfg(tmp_path)
    assert S.live_user_conversation_sid(cfg, "p", "gu_b") == b
    assert S.live_user_conversation_sid(cfg, "p", "gu_a") == a


# --- BOTH ERAS ARE ON DISK AT ONCE ------------------------------------------
# Sessions that already hold `user-conversation` today were written before the
# binding existed, so none of them carries `user_conversation_gid`. A migration
# case nobody wrote down is how the pre-rename shape got missed the first time.
# There is no backfill and there is nothing to backfill: exactly one session md
# on the install carries a `roles:` field at all, and it has no
# `global_user_id`. So both of these are the REAL state of the fleet, not
# hypotheticals.

def test_a_pre_binding_holder_with_a_spawn_stamp_still_resolves_for_its_user(
        tmp_path, monkeypatch):
    """ERA 1, the graceful path: an attendant spawned FOR a user carries
    `global_user_id` from spawn time. It needs no binding and no migration —
    arm one already answers, and answers only for its own user."""
    a = "S-u-universal_bsq_session-p3"
    _uc_md(tmp_path, a, window="universal_bsq_session", gid="gu_a",
           roles=["user-conversation"])          # declared, but NO uc binding
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    cfg = _uc_cfg(tmp_path)
    assert S.live_user_conversation_sid(cfg, "p", "gu_a") == a
    assert S.live_user_conversation_sid(cfg, "p", "gu_b") is None


def test_a_pre_binding_holder_with_no_identity_at_all_answers_for_nobody(
        tmp_path, monkeypatch):
    """ERA 2, and the case the TL asked to see stated rather than implied: a
    session that ALREADY HOLDS the role, declared before the binding existed,
    with NO `user_conversation_gid` AND NO `global_user_id` anywhere on disk.

    There is nothing on that md that names a user, so there is no answer to a
    per-user question. None — and `ensure_user_conversation` spawns a fresh
    attendant. A wrong attendant is a stranger reading someone's conversation;
    a spurious spawn is a wasted process. It re-binds itself the moment anyone
    declares the role with an owner."""
    a = "S-u-operator-p1"
    _uc_md(tmp_path, a, window="operator", roles=["operator", "user-conversation"])
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    cfg = _uc_cfg(tmp_path)
    assert S.live_user_conversation_sid(cfg, "p", "gu_a") is None
    assert S.live_user_conversation_sid(cfg, "p", "gu_b") is None
    # and it is not silently invisible either — the roster still shows it, so
    # `bsq team status` / `role show` can say it is UNBOUND rather than absent.
    assert any(r.get("sid") == a for r in S.live_user_conversation_sids(cfg, "p"))
