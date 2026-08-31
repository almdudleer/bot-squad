"""T-0932 — gradual budding: the energy ladder (L0 solo / L1 work-split /
L2 orchestration-split), its triggers, and the de-differentiation morph.

What these pin, and why each is a rule rather than a description:

  * the ladder READS T-0855's topology verdict for the operator rung instead of
    growing a second set of thresholds (a session and the scheduler that
    disagree about the rung is the failure this whole feature would cause);
  * the dev rung's trigger reads TASK STATES (T-0931's SSOT), not counters, and
    counts a queued request by the LIVE ROSTER, not by a board label a dead dev
    left behind;
  * a morph back to ``user-conversation`` SHEDS the task — without which
    ``graceful_exit.work_done`` exits the ROOT session the moment its own bud
    reaches ``totest`` (see the ★ note in budding.py);
  * the tick only ever SUGGESTS, is cooldown-guarded, and never writes into a
    pane a human is watching or typing in (T-0926/T-0930).
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import budding
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from bot_squad_worker.config import Config
from bot_squad_worker.sessions import _write_session_metadata


def _make_cfg(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True)
    (data_dir / "test-project" / "sessions").mkdir(parents=True)
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir,
                                 tg_bot_token="")


def _task(cfg, task_id: str, status: str = "open") -> None:
    (cfg.data_dir / "test-project" / "backlog" / f"{task_id}-thing.md").write_text(
        f"---\nid: {task_id}\ntitle: T\nstatus: {status}\ninitiative: ~\n---\nbody\n"
    )


def _session(cfg, sid, *, window, task_id="~", status="active", role=None,
             **extra) -> Path:
    meta = {
        "sid": sid, "status": status, "window": window, "cwd": "/tmp",
        "claude_uuid": "uuid-" + sid[-3:], "task_id": task_id,
        "initiative": "~", "started_at": "2026-05-12T00:00:00Z",
    }
    if role:
        meta["role"] = role
    meta.update(extra)
    p = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    _write_session_metadata(p, meta)
    return p


ROOT = "S-u-gu_x-user-conversation-p9"


def _root(cfg, *, task_id="~", role=None, **extra):
    return _session(cfg, ROOT, window="gu_x-user-conversation",
                    task_id=task_id, role=role, **extra)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: "/home/u")
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: None)
    # No live tmux: keeps live_operator_sids' pane scan (T-0523) hermetic.
    monkeypatch.setattr(S, "list_panes", lambda: [])


# --- the ladder's rungs -----------------------------------------------------

def test_level_is_solo_when_one_session_does_everything(tmp_path):
    """L0: the root holds the task itself. No dev, no operator — the cheapest
    shape and the one every project starts in."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001")
    _root(cfg, task_id="T-0001", role="dev")

    obs = budding.observe(cfg, "test-project")

    assert obs["level"] == budding.LEVEL_SOLO
    assert obs["root_sids"] == [ROOT]
    assert obs["held_task_ids"] == ["T-0001"]
    assert obs["queued_requests"] == []


def test_level_is_work_split_once_a_dev_bud_holds_the_task(tmp_path):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001")
    _root(cfg)
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0001")

    obs = budding.observe(cfg, "test-project")

    assert obs["level"] == budding.LEVEL_WORK_SPLIT
    assert obs["bud_dev_sids"] == ["S-u-d1-dev-p1"]


def test_root_identity_survives_the_morph_that_hides_it(tmp_path):
    """The root is identified by its IMMUTABLE SID window, never by its stored
    role — budding is precisely the thing that makes that role oscillate, so a
    role-based test would lose the root at the moment it matters."""
    cfg = _make_cfg(tmp_path)
    _root(cfg, task_id="T-0001", role="dev")  # morphed into the work

    assert budding.is_root_session(ROOT, {"role": "dev"}) is True
    assert budding.is_root_session("S-u-d1-dev-p1", {"role": "dev"}) is False


# --- L0 -> L1: the dev rung -------------------------------------------------

def test_queued_user_requests_past_the_threshold_suggest_budding_a_dev(tmp_path):
    """«если сильно много параллельных запросов от юзера» — the session is
    head-down on T-0001 while two more requests sit unheld."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "planned")
    _root(cfg, task_id="T-0001", role="dev")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["verdict"] == budding.BUD_DEV
    assert d["task_id"] == "T-0001"
    assert d["command"] == "bsq bud dev T-0001"
    assert "T-0002" in d["reason"]


def test_one_queued_request_is_a_backlog_not_pressure(tmp_path):
    """The threshold is 2 on purpose: one request behind the one you are doing
    is an ordinary board, and a suggestion that fires on it is noise."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _root(cfg, task_id="T-0001", role="dev")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["verdict"] == budding.HOLD
    assert "under the pressure threshold" in d["reason"]


def test_a_task_a_live_session_already_holds_is_not_queued_pressure(tmp_path):
    """Pressure counts what NOBODY is doing. Two open tasks, but a live dev
    holds one, so only one is queued — under the threshold."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "in_progress")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev")
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0002")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["observation"]["queued_requests"] == ["T-0003"]
    assert d["verdict"] == budding.HOLD


def test_an_in_progress_label_left_by_a_dead_dev_still_counts_as_queued(tmp_path):
    """The mirror image of the test above, and the reason the roster — not the
    board label — is the source: a task labelled in_progress that no live
    session holds is work nobody is doing, i.e. exactly queued pressure."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "in_progress")   # stale label, holder is gone
    _task(cfg, "T-0003", "in_progress")   # stale label, holder is gone
    _root(cfg, task_id="T-0001", role="dev")
    _session(cfg, "S-u-dead-dev-p1", window="dead-dev", task_id="T-0002",
             status="archived")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["observation"]["queued_requests"] == ["T-0002", "T-0003"]
    assert d["verdict"] == budding.BUD_DEV


def test_parked_and_finished_tasks_are_not_user_pressure(tmp_path):
    """A closed task is done, a totest task is handed over for review, and a
    blocked_on_user task is waiting on HIM (T-0931) — none of the three is
    something he is waiting on a session for."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "closed")
    _task(cfg, "T-0003", "totest")
    _task(cfg, "T-0004", "blocked_on_user")
    _task(cfg, "T-0005", "paused")
    _root(cfg, task_id="T-0001", role="dev")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["observation"]["queued_requests"] == []
    assert d["verdict"] == budding.HOLD


def test_pressure_states_track_the_t0931_state_ssot(tmp_path):
    """The trigger reads T-0931's states, never a private copy — so a state
    added or reclassified there moves the budding trigger with it."""
    from bot_squad_worker import task_states as ts
    active, parked = frozenset(ts.ACTIVE_STATES), frozenset(ts.PARKED_STATES)
    assert budding._state_sets() == (active, parked)
    assert "blocked_on_user" in parked, (
        "T-0931's headline state vanished — a dev blocked on the user would "
        "start counting as queued pressure again")
    # The two deliberate adjustments, spelled out (see pressure_states()).
    assert budding.pressure_states() == frozenset(active | {"planned"}) - {"totest"}
    assert "totest" not in budding.pressure_states()
    assert "closed" not in budding.pressure_states()
    assert "planned" in budding.pressure_states()


# --- L1 -> L2: the operator rung -------------------------------------------

def test_orchestration_load_suggests_budding_an_operator(tmp_path, monkeypatch):
    """The trigger IS T-0855's verdict — not a second threshold. Push the
    direct-dispatch ceiling down to 1 and the same read that gates the
    scheduler's re-drive now tells the root to bud an operator."""
    from bot_squad_worker import dispatch

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(dispatch, "direct_max_devs", lambda: 1)
    _task(cfg, "T-0001", "in_progress")
    _root(cfg)
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0001")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["verdict"] == budding.BUD_OPERATOR
    assert d["command"] == "bsq bud operator"
    assert d["observation"]["topology"]["tier"] == "operator"


def test_no_operator_bud_while_the_flow_stays_under_the_ceiling(tmp_path):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _root(cfg)
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0001")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["verdict"] == budding.HOLD


def test_the_ladder_never_suggests_killing_the_operator(tmp_path, monkeypatch):
    """L2 -> L1 deflation is attrition (the operator's own graceful exit), not
    a session-driven move: a session has no authority over a peer's life."""
    from bot_squad_worker import dispatch

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(dispatch, "live_operator_sids",
                        lambda _cfg, _slug: ["S-u-operator-p2"])
    _root(cfg)

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["level"] == budding.LEVEL_ORCH_SPLIT
    assert d["verdict"] == budding.HOLD
    assert "de-escalates by attrition" in d["reason"]


# --- deflation --------------------------------------------------------------

def test_deflation_suggests_absorbing_the_last_queued_task(tmp_path):
    """L1 -> L0: one request left, nothing running — a second process for it is
    pure overhead."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0009", "open")
    _root(cfg)

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["verdict"] == budding.ABSORB
    assert d["task_id"] == "T-0009"
    assert d["command"] == "bsq bud absorb T-0009"


def test_no_absorb_while_a_dev_is_still_running(tmp_path):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0009", "open")
    _task(cfg, "T-0010", "in_progress")
    _root(cfg)
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0010")

    d = budding.decide_budding(cfg, "test-project", ROOT)

    assert d["verdict"] == budding.HOLD


# --- the morph: de-differentiation -----------------------------------------

def test_morphing_back_to_user_conversation_sheds_the_task(tmp_path):
    """«отделение себя в юзер-сессию». The shed is what keeps the root alive:
    ``graceful_exit.work_done`` tests task_id BEFORE the role, so a root that
    kept the binding would be exited when its own bud reached totest."""
    from bot_squad_worker import graceful_exit

    cfg = _make_cfg(tmp_path)
    _root(cfg, task_id="T-0001", role="dev")

    out = S.morph_session(cfg, "test-project", ROOT, "user-conversation")

    assert out["role"] == "user-conversation"
    assert out["task_id"] is None
    meta = S._read_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / f"{ROOT}.md")
    assert not meta.get("task_id")   # "~" on disk, None once parsed
    # The consequence, asserted rather than described:
    assert graceful_exit.work_done("user-conversation", None, "totest", 0) is False
    assert graceful_exit.work_done("user-conversation", "T-0001", "totest", 0) is True


def test_a_user_conversation_morph_refuses_to_carry_a_task(tmp_path):
    cfg = _make_cfg(tmp_path)
    _root(cfg, task_id="T-0001", role="dev")

    with pytest.raises(ActionError, match="must not carry a dev task"):
        S.morph_session(cfg, "test-project", ROOT, "user-conversation",
                        task_id="T-0001")


def test_the_full_round_trip_solo_to_split_and_back(tmp_path):
    """The journey in one test: solo session holding the work -> sheds it to a
    bud and narrows to the conversation -> the bud finishes and is not replaced
    -> the root re-absorbs the last piece of work. L0 -> L1 -> L0."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev")

    assert budding.decide_budding(cfg, "test-project", ROOT)["verdict"] == budding.BUD_DEV

    # the bud takes the task; the parent sheds it and narrows
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0001")
    S.morph_session(cfg, "test-project", ROOT, "user-conversation")
    obs = budding.observe(cfg, "test-project")
    assert obs["level"] == budding.LEVEL_WORK_SPLIT
    assert obs["held_task_ids"] == ["T-0001"]     # the BUD holds it now

    # the bud finishes and exits; two of the three tasks are done
    _task(cfg, "T-0001", "closed")
    _task(cfg, "T-0002", "closed")
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0001",
             status="archived")

    d = budding.decide_budding(cfg, "test-project", ROOT)
    assert d["observation"]["level"] == budding.LEVEL_SOLO
    assert d["verdict"] == budding.ABSORB
    assert d["task_id"] == "T-0003"

    S.morph_session(cfg, "test-project", ROOT, "dev", task_id="T-0003")
    assert S._role_of(S._read_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / f"{ROOT}.md")) == "dev"


# --- the suggestion tick ----------------------------------------------------

class _Pane:
    def __init__(self, window, pane_id):
        self.window, self.pane_id = window, pane_id


def _arm_tick(monkeypatch, *, attached=False, composer=True):
    from bot_squad_worker import autocompact, recycle_gate

    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(S, "list_panes",
                        lambda: [_Pane("gu_x-user-conversation", "%9")])
    monkeypatch.setattr(S, "compute_sid", lambda user, window, pane_id: ROOT)
    monkeypatch.setattr(S, "_deliver_prompt",
                        lambda pane_id, text, **kw: delivered.append((pane_id, text)))
    monkeypatch.setattr(recycle_gate, "is_attached", lambda *a, **k: attached)
    monkeypatch.setattr(autocompact, "_capture_pane", lambda pane_id: "❯ ")
    monkeypatch.setattr(autocompact, "composer_free", lambda *a, **k: composer)
    return delivered


def test_the_tick_suggests_and_does_not_act(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev")
    delivered = _arm_tick(monkeypatch)

    out = budding.budding_check(cfg, "test-project")

    assert [s["verdict"] for s in out["suggested"]] == [budding.BUD_DEV]
    assert len(delivered) == 1
    assert "bsq bud dev T-0001" in delivered[0][1]
    # Nothing was performed: the session still holds its task and its role.
    meta = S._read_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / f"{ROOT}.md")
    assert meta["task_id"] == "T-0001"
    assert meta["role"] == "dev"
    assert meta["budding_suggested_at"]


def test_the_tick_does_not_repeat_inside_the_cooldown(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev")
    delivered = _arm_tick(monkeypatch)

    budding.budding_check(cfg, "test-project")
    budding.budding_check(cfg, "test-project")

    assert len(delivered) == 1


def test_the_tick_never_types_into_a_pane_a_human_is_watching(tmp_path, monkeypatch):
    """T-0926/T-0930: an automatic write into the pane he is looking at is the
    exact failure two live incidents were filed over. A suggestion can wait."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev")
    delivered = _arm_tick(monkeypatch, attached=True)

    out = budding.budding_check(cfg, "test-project")

    assert out["suggested"] == []
    assert delivered == []


def test_the_tick_never_types_over_half_typed_text(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev")
    delivered = _arm_tick(monkeypatch, composer=False)

    assert budding.budding_check(cfg, "test-project")["suggested"] == []
    assert delivered == []


def test_a_pinned_session_takes_no_budding_suggestion(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev", pinned=True)
    delivered = _arm_tick(monkeypatch)

    assert budding.budding_check(cfg, "test-project")["suggested"] == []
    assert delivered == []


def test_the_kill_switch_disables_the_tick(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _root(cfg, task_id="T-0001", role="dev")
    delivered = _arm_tick(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_BUDDING", "0")

    out = budding.budding_check(cfg, "test-project")

    assert out["disabled"] is True
    assert delivered == []


def test_only_root_sessions_are_swept(tmp_path, monkeypatch):
    """A plain dev under pressure is not asked to bud — it has a TL/operator
    above it, and the ladder is about the session that owns the conversation."""
    cfg = _make_cfg(tmp_path)
    _task(cfg, "T-0001", "in_progress")
    _task(cfg, "T-0002", "open")
    _task(cfg, "T-0003", "open")
    _session(cfg, "S-u-d1-dev-p1", window="d1-dev", task_id="T-0001")
    delivered = _arm_tick(monkeypatch)

    assert budding.budding_check(cfg, "test-project")["suggested"] == []
    assert delivered == []


def test_unknown_project_is_refused(tmp_path):
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ActionError):
        budding.observe(cfg, "nope")
