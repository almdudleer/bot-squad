"""T-0929 — the ultimate switch, and the pin that keeps it ultimate.

> "there are many ways in which the system operates around this mechanism. But
> that should be the ultimate switch." — the stakeholder, 2026-08-28.

The audit that opened this ticket found the pause flag was read by THREE of the
worker's driving ticks and ignored by ten. The fix is one gate
(:func:`automation.gate`) plus one registry (:data:`automation.MECHANISMS`).

**A registry that is only documentation is not a control.** It drifts the first
time someone adds a tick, and the drift is silent in exactly the way this ticket
is about. So the load-bearing test here is a SOURCE SCAN: for every row marked
``gated: True`` the named module must actually contain a gate call. A row can be
added, but it cannot be added without wiring.

What this file does NOT cover, said here because a green run is otherwise read
as "the switch stops everything": the scan proves each gated module CALLS the
gate, not that the call sits on the path that would have done the work. Those
are pinned per module by the behaviour tests below (a paused project's tick
returns its paused shape and touches nothing).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bot_squad_worker import automation, pace

WORKER_PKG = Path(automation.__file__).resolve().parent

#: Either public entry point counts as wiring — `gate` is the logging form every
#: tick uses, `allowed` the bare predicate for a refusal path (autopilot.start).
_GATE_CALL = re.compile(r"_?automation\.(gate|allowed)\s*\(")


class _Cfg:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.projects = {"proj": object()}


@pytest.fixture()
def cfg(tmp_path: Path) -> _Cfg:
    (tmp_path / "proj" / "_worker").mkdir(parents=True)
    return _Cfg(tmp_path)


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------

def test_the_denominator_the_registry_is_not_empty_and_most_of_it_is_gated():
    """Guard the guard: every assertion below is a for-loop over MECHANISMS, so
    an empty (or accidentally all-ungated) registry would make this whole file
    pass while proving nothing. Cf. the T-0828 mirror test's own denominator."""
    assert len(automation.MECHANISMS) >= 20, len(automation.MECHANISMS)
    assert len(automation.GATED_KEYS) >= 12, automation.GATED_KEYS


def test_every_row_is_well_formed_and_keys_are_unique():
    keys = [m["key"] for m in automation.MECHANISMS]
    assert len(keys) == len(set(keys)), "duplicate mechanism key"
    for m in automation.MECHANISMS:
        assert set(m) == {"key", "module", "gated", "why"}, m
        assert isinstance(m["gated"], bool), m
        assert m["why"].strip(), m
        assert (WORKER_PKG / m["module"]).is_file(), \
            f"{m['key']}: no such module {m['module']}"


@pytest.mark.parametrize(
    "mech", [m for m in automation.MECHANISMS if m["gated"]],
    ids=[m["key"] for m in automation.MECHANISMS if m["gated"]],
)
def test_every_gated_mechanism_actually_calls_the_gate(mech):
    """THE pin. A `gated: True` row whose module never calls the gate is the
    exact defect T-0929 reports: a mechanism the switch is documented to stop and
    does not."""
    src = (WORKER_PKG / mech["module"]).read_text(encoding="utf-8")
    assert _GATE_CALL.search(src), (
        f"{mech['key']}: {mech['module']} is registered as gated but never calls "
        f"automation.gate()/allowed() — the switch does not reach it"
    )
    assert f'"{mech["key"]}"' in src, (
        f"{mech['key']}: {mech['module']} calls the gate but not under this "
        f"mechanism key, so a skip logs the wrong name"
    )


def test_the_scan_can_fail(tmp_path: Path):
    """A check nobody has watched fail is a check nobody has tested (T-0777).
    Run the same predicate over a module that does NOT call the gate."""
    ungated = WORKER_PKG / "frontmatter.py"
    assert ungated.is_file()
    assert not _GATE_CALL.search(ungated.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The gate predicate
# ---------------------------------------------------------------------------

def test_gate_is_open_by_default_and_closed_once_paused(cfg):
    from bot_squad_worker import operator_redrive as ord_

    assert automation.allowed(cfg, "proj") is True
    assert automation.gate(cfg, "proj", "unit-test") is True

    ord_.pause(cfg, "proj", by="test", reason="unit")
    assert automation.allowed(cfg, "proj") is False
    assert automation.gate(cfg, "proj", "unit-test") is False

    ord_.resume(cfg, "proj")
    assert automation.allowed(cfg, "proj") is True


def test_gate_fails_open_when_the_pause_read_raises(cfg, monkeypatch):
    """A stuck-OFF system lies about what is happening just as much as a stuck-ON
    one. `off` is a file that EXISTS, so a failed read is "not confirmed paused"."""
    from bot_squad_worker import operator_redrive as ord_

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(ord_, "is_paused", boom)
    assert automation.allowed(cfg, "proj") is True


# ---------------------------------------------------------------------------
# The mechanisms, behaviourally — a paused project's tick does nothing
# ---------------------------------------------------------------------------

def test_autopilot_tick_stands_down_while_the_switch_is_off(cfg, monkeypatch):
    from bot_squad_worker import autopilot, operator_redrive as ord_

    called = []
    monkeypatch.setattr(autopilot, "list_states",
                        lambda *a, **k: called.append(1) or [])
    out = autopilot.tick(cfg, "proj")
    assert out.get("paused") is not True
    assert called, "control: an unpaused tick DOES walk the autopilot states"

    # pause() ITSELF walks list_states (it ends every running autopilot), so the
    # probe is cleared AFTER the pause or it catches the pause, not the tick.
    ord_.pause(cfg, "proj", by="test", reason="unit")
    called.clear()
    assert autopilot.tick(cfg, "proj") == {"actions": [], "paused": True}
    assert not called, "a paused project's autopilot watchdog still walked states"


def test_autopilot_start_refuses_while_the_switch_is_off(cfg):
    from bot_squad_worker import autopilot, operator_redrive as ord_
    from bot_squad_worker.actions import ActionError

    ord_.pause(cfg, "proj", by="test", reason="unit")
    with pytest.raises(ActionError) as ei:
        autopilot.start(cfg, "proj", kind="project", ref="proj", prompt="go")
    assert "OFF" in str(ei.value)


def test_operator_redrive_tick_reports_paused(cfg):
    from bot_squad_worker import operator_redrive as ord_

    ord_.pause(cfg, "proj", by="test", reason="unit")
    assert ord_.tick(cfg, "proj")["action"] == "paused"


# ---------------------------------------------------------------------------
# The named drive states (pace.py)
# ---------------------------------------------------------------------------

def test_the_states_he_named_all_exist_and_off_is_one_of_them():
    """His list, verbatim: "work on one task", "finish up (i.e. close all in
    progress)", "and the main one with the set of tasks which need to be done" —
    plus the off switch."""
    assert set(pace.DRIVE_STATES) == {"one_task", "finish_up", "all_tasks", "off"}
    # every settable state except `off` has axes; `off` deliberately has none
    assert set(pace.DRIVE_STATE_AXES) == {"one_task", "finish_up", "all_tasks"}
    assert "off" not in pace.DRIVE_STATE_AXES
    for state in pace.DRIVE_STATES:
        assert pace.DRIVE_STATE_LABELS[state].strip()


def test_setting_a_state_applies_its_axes_and_releases_the_switch(cfg):
    from bot_squad_worker import operator_redrive as ord_

    ord_.pause(cfg, "proj", by="test", reason="unit")
    out = pace.set_drive_state(cfg, "proj", "finish_up", set_by="test")

    assert out["state"] == "finish_up"
    assert out["paused"] is False, "picking a mode IS turning the drive on"
    assert out["drive"]["scope"] == "in_progress"
    assert out["drive"]["stop_when"] == "scope_exhausted"
    assert automation.allowed(cfg, "proj") is True


def test_one_task_is_a_wip_cap_of_one(cfg):
    out = pace.set_drive_state(cfg, "proj", "one_task", set_by="test")
    assert out["state"] == "one_task"
    assert out["max_in_progress"] == 1
    assert pace.max_in_progress(cfg, "proj") == 1


def test_off_engages_the_pause_and_keeps_the_previous_state_for_the_way_back(cfg):
    pace.set_drive_state(cfg, "proj", "finish_up", set_by="test")
    off = pace.set_drive_state(cfg, "proj", "off", set_by="test",
                               source_text="stop everything")

    assert off["state"] == "off"
    assert off["paused"] is True
    assert automation.allowed(cfg, "proj") is False
    # the CONFIGURED state survives, so resume returns him to what he chose
    assert off["drive"]["state"] == "finish_up"

    from bot_squad_worker import operator_redrive as ord_
    ord_.resume(cfg, "proj")
    assert pace.read_automation(cfg, "proj")["state"] == "finish_up"


def test_off_is_never_written_into_the_drive_block(cfg):
    """One bit, not two. A stored `off` could disagree with the flag file — the
    "changes state without my confirmation" class this ticket is about."""
    pace.set_drive_state(cfg, "proj", "off", set_by="test")
    raw = json.loads((cfg.data_dir / "proj" / "_worker" / "pace" / "pace.json")
                     .read_text()) if (
        cfg.data_dir / "proj" / "_worker" / "pace" / "pace.json").exists() else {}
    assert (raw.get("drive") or {}).get("state") != "off"


def test_a_hand_edited_off_is_reported_invalid_not_believed(cfg):
    pace.set_drive_state(cfg, "proj", "all_tasks", set_by="test")
    p = cfg.data_dir / "proj" / "_worker" / "pace" / "pace.json"
    raw = json.loads(p.read_text())
    raw["drive"]["state"] = "off"
    p.write_text(json.dumps(raw))

    view = pace.read_automation(cfg, "proj")
    assert view["paused"] is False, "a json edit must not be able to fake the switch"
    assert view["drive"]["invalid"]["state"] == "off"
    assert view["state"] == "all_tasks"


def test_an_unsettable_state_is_rejected_never_coerced(cfg):
    for bad in ("custom", "opne", "", "ALL_TASKS"):
        with pytest.raises(pace.DriveModeError):
            pace.set_drive_state(cfg, "proj", bad)


def test_a_wordless_write_clears_the_previous_states_words(cfg):
    """PROVENANCE BELONGS TO THE REQUEST THAT PRODUCED THIS STATE.

    Caught in the walkthrough: set `finish_up` from his words, then click "One
    task" in the UI (no words), and the card read
    "one_task … from «закончить всё что в опен»" — a button-chosen state
    attributed to words that asked for a different one. The words go; who and
    when stay."""
    pace.set_drive_state(cfg, "proj", "finish_up", set_by="tg",
                         source_text="закончить всё что в опен")
    assert pace.read_automation(cfg, "proj")["drive"]["source_text"] == \
        "закончить всё что в опен"

    out = pace.set_drive_state(cfg, "proj", "one_task", set_by="web")
    assert out["drive"]["source_text"] is None
    assert out["drive"]["set_by"] == "web"
    assert out["drive"]["set_at"]


def test_a_pre_t0929_config_reads_as_the_state_its_axes_mean(cfg):
    """T-0828 shipped the axes as the user surface and he SET them
    («закончить всё что в опен»). Those projects must read as a named state."""
    pace.set_drive(cfg, "proj", scope="in_progress", stop_when="scope_exhausted",
                   set_by="test", source_text="закончить всё что в опен")
    assert pace.read_automation(cfg, "proj")["state"] == "finish_up"


def test_axes_that_match_no_named_state_read_as_custom_not_as_a_lie(cfg):
    pace.set_drive(cfg, "proj", scope="open_reopened", set_by="test")
    view = pace.read_automation(cfg, "proj")
    assert view["state"] == pace.DRIVE_STATE_CUSTOM
    assert view["label"] == pace.DRIVE_STATE_LABELS[pace.DRIVE_STATE_CUSTOM]


def test_an_unconfigured_project_is_all_tasks_and_running(cfg):
    """Ships as a no-op by construction: a project that never set anything keeps
    exactly today's behaviour, and says so rather than reading as `custom`."""
    view = pace.read_automation(cfg, "proj")
    assert view["state"] == "all_tasks"
    assert view["paused"] is False
    assert view["drive"]["configured"] is False


# ---------------------------------------------------------------------------
# The visibility half
# ---------------------------------------------------------------------------

def test_snapshot_carries_the_caps_and_targets_that_go_with_the_state(cfg):
    """"I need a few well-defined states, WITH QUOTA CAPS AND TARGETS." The cap
    lives in pace.json and the target in system_settings.toml — two stores, which
    is exactly why one view has to carry both."""
    pace.set_drive_state(cfg, "proj", "one_task", set_by="test")
    q = automation.snapshot(cfg, "proj")["quota"]
    assert q["max_in_progress"] == 1, "one_task IS a cap of one"
    # No target set and no quota anchor on a fresh project: an EXPLICIT unknown,
    # never a 0 that reads like a real measurement.
    assert q["weekly_target_pct"] is None
    assert q["spend_pct"] is None
    assert q["verdict"] is None
    assert set(q) == {"max_in_progress", "weekly_target_pct", "spend_pct", "verdict"}


def test_snapshot_quota_survives_an_unreadable_signal(cfg, monkeypatch):
    """The caps ride the same read as "is anything running". An unreadable
    telemetry file must not take the SWITCH's own status down with it."""
    from bot_squad_worker import operator_redrive as ord_

    def boom(*_a, **_k):
        raise OSError("telemetry gone")

    monkeypatch.setattr(ord_, "_burn_signal", boom)
    snap = automation.snapshot(cfg, "proj")
    assert snap["running"] is True
    assert snap["quota"]["spend_pct"] is None


def test_snapshot_answers_is_anything_automatic_running(cfg):
    snap = automation.snapshot(cfg, "proj")
    assert snap["running"] is True
    assert snap["state"] == "all_tasks"
    assert {m["key"] for m in snap["mechanisms"]} == {
        m["key"] for m in automation.MECHANISMS}
    assert all(m["active"] for m in snap["mechanisms"])

    pace.set_drive_state(cfg, "proj", "off", set_by="test")
    snap = automation.snapshot(cfg, "proj")
    assert snap["running"] is False
    assert snap["state"] == "off"
    gated = {m["key"]: m["active"] for m in snap["mechanisms"] if m["gated"]}
    assert gated and not any(gated.values()), gated
    # the ungated rows must STILL read active — the panel may not imply the
    # switch stopped something it does not touch
    assert all(m["active"] for m in snap["mechanisms"] if not m["gated"])
