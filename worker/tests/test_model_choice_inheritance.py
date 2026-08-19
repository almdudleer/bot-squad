"""T-0909 follow-up: a relaunch must not silently re-price the ticket.

WHAT THE MEASUREMENT FOUND. T-0909 put a gate on `bsq spawn` so a dev dispatch
has to state its model. The first live ledger line after that shipped came in
with `source=config:dev` and an EMPTY `dispatched_by` — i.e. it never went
through the gate. Attributing nine days of the worker journal by the producer's
own completion line (`autocompact: compact handoff complete for <sid> —
relaunched fresh`):

    236  role=dev spawns total
    123  of them on the role default (config:dev -> opus)
     87  of those 123 (71%) were AUTOCOMPACT RELAUNCHES

`autocompact._relaunch` carried task_id / initiative / parent_sid / owner
forward — everything the reconcilers need to see continuity — and dropped the
MODEL. So a dispatch deliberately sent to Sonnet came back as Opus at the first
cache-window recycle, roughly an hour later, and the gate's effect decayed to
nothing on exactly the long-lived sessions that cost the most.

THIS IS NOT A NEW POLICY AND IT CHANGES NO DEFAULT. `operator_redrive` has done
the identical carry-forward since T-0678, for the identical stated reason ("a
full respawn mints a BRAND-NEW SID ... without this the override would silently
revert to the fleet/role default"). This is that fix applied to its unfixed twin
— the path that produces 71% of role-defaulted dev traffic.

The asymmetry between `model` and `effort` is the subtle part and has its own
arms below: `model` is stamped ONLY when explicit (T-0678), so its presence is
proof of a choice; `effort` is stamped UNCONDITIONALLY (T-0871), so presence
proves nothing and inheriting it blindly would freeze today's role default onto
every future incarnation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import autocompact as AC
from bot_squad_worker import model_dispatch as MD
from tests.test_explicit_model_effort import (  # noqa: F401
    _make_cfg, _session_md, _spawn_cmd, _stub_tmux,
)


# ---------------------------------------------------------------------------
# _inherited_model_choice — what carries, and what deliberately does not
# ---------------------------------------------------------------------------

def test_an_explicit_model_carries():
    """The whole point: a dispatch sent to Sonnet stays on Sonnet."""
    model, effort, reason = AC._inherited_model_choice(
        {"model": "sonnet", "model_source": "explicit"})
    assert model == "sonnet"


def test_a_role_defaulted_predecessor_carries_NOTHING():
    """`model` is absent from the md exactly when nobody chose one (T-0678), so
    the successor re-reads the role default — unchanged behaviour, and the
    property that makes this fix carry no policy content."""
    model, effort, reason = AC._inherited_model_choice(
        {"model_source": "config:dev", "effort": "high"})
    assert model is None and effort is None and reason is None


def test_the_stated_reason_travels_with_the_premium_model():
    """Otherwise a justified Opus reappears in the compliance number as an
    unexplained one, and the justification has to be re-typed every hour."""
    model, _, reason = AC._inherited_model_choice(
        {"model": "opus", "model_source": "explicit",
         "model_reason": "cause unknown, spawn lifecycle"})
    assert model == "opus"
    assert reason == "cause unknown, spawn lifecycle"


def test_a_reason_without_a_model_does_not_travel_alone():
    """A stale `model_reason` on a role-defaulted md would attach a
    justification to a choice nobody made."""
    _, _, reason = AC._inherited_model_choice({"model_reason": "leftover"})
    assert reason is None


def test_an_EXPLICIT_effort_carries():
    model, effort, _ = AC._inherited_model_choice(
        {"model": "sonnet", "effort": "low", "effort_source": "explicit"})
    assert effort == "low"


def test_a_DEFAULTED_effort_does_NOT_carry():
    """`effort` is stamped unconditionally (T-0871), so "high" on the md does
    not mean anybody asked for it. Inheriting it would freeze today's role
    default onto every successor forever — the staleness T-0678 avoided for
    `model` — and a later `[effort]` config change would never reach the
    sessions that outlive one cache window."""
    _, effort, _ = AC._inherited_model_choice(
        {"model": "sonnet", "effort": "high", "effort_source": "config:dev"})
    assert effort is None


@pytest.mark.parametrize("sentinel", ["~", "", None])
def test_the_registry_unset_sentinel_is_not_a_model(sentinel):
    model, _, _ = AC._inherited_model_choice({"model": sentinel})
    assert model is None


# ---------------------------------------------------------------------------
# the wiring — spawn stamps the provenance the inheritance reads
# ---------------------------------------------------------------------------

def test_spawn_stamps_effort_source_so_the_two_are_distinguishable(
        tmp_path, monkeypatch):
    """The inheritance above is only possible if the md records WHY the effort
    is what it is. Without this stamp, an explicit and a defaulted "high" are
    the same bytes ([[feedback_explicit_unknown_not_silent_none]])."""
    _spawn_cmd(tmp_path, monkeypatch, "w", model="sonnet", effort="low")
    md = _session_md(tmp_path)
    assert md.get("effort_source") == "explicit"
    assert md.get("effort") == "low"


def test_a_defaulted_spawn_stamps_a_NON_explicit_effort_source(
        tmp_path, monkeypatch):
    _spawn_cmd(tmp_path, monkeypatch, "w", settings='[effort]\ndev = "high"\n')
    md = _session_md(tmp_path)
    assert md.get("effort_source") == "config:dev"
    assert md.get("effort_source") != "explicit"


# ---------------------------------------------------------------------------
# end to end — the round trip a real recycle makes
# ---------------------------------------------------------------------------

def _relaunch_cmd(tmp_path, monkeypatch, meta: dict) -> tuple:
    """Drive the REAL `_relaunch` over a session md and return the launch
    command + the ledger records it produced."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    from bot_squad_worker.sessions import _write_session_metadata, _session_file
    sid = "S-alice-w-p2"
    base = {"sid": sid, "status": "active", "window": "w", "cwd": str(repo),
            "task_id": "T-0909", "role": "dev"}
    base.update(meta)
    _write_session_metadata(_session_file(cfg.data_dir, "test-project", sid), base)
    captured: list[str] = []
    _stub_tmux(monkeypatch, repo, "w", captured)
    AC._relaunch(cfg, "test-project", {"sid": sid, "role": "dev",
                                       "task_id": "T-0909", "window": "w"},
                 lambda role, task_id, aid: "boot prompt")
    assert captured, "expected a tmux new-window call"
    return captured[0], MD.read(cfg.data_dir)


def test_a_recycle_keeps_the_sonnet_dispatch_on_sonnet(tmp_path, monkeypatch):
    """The defect, end to end: this command used to come back `--model opus`."""
    cmd, recs = _relaunch_cmd(tmp_path, monkeypatch, {
        "model": "sonnet", "model_source": "explicit"})
    assert "--model sonnet" in cmd
    assert "--model opus" not in cmd
    assert recs[-1]["model"] == "sonnet"
    assert recs[-1]["source"] == "explicit"


def test_a_recycle_of_a_defaulted_session_still_takes_the_role_default(
        tmp_path, monkeypatch):
    """The other side of the same coin — proving the fix is a carry-forward and
    not a blanket downgrade. Without this arm, "always spawn sonnet" would pass
    the test above."""
    cmd, recs = _relaunch_cmd(tmp_path, monkeypatch, {"model_source": "config:dev"})
    assert "--model opus" in cmd
    assert recs[-1]["source"] != "explicit"


def test_a_recycle_keeps_the_justification_for_a_premium_model(
        tmp_path, monkeypatch):
    cmd, recs = _relaunch_cmd(tmp_path, monkeypatch, {
        "model": "opus", "model_source": "explicit",
        "model_reason": "cross-module design, cause unknown"})
    assert "--model opus" in cmd
    assert recs[-1]["reason"] == "cross-module design, cause unknown"


def test_the_relaunch_is_attributable_in_the_ledger(tmp_path, monkeypatch):
    """The blank `dispatched_by` on the first live line is what sent this
    investigation down a nine-day journal grep. It is a field now."""
    _, recs = _relaunch_cmd(tmp_path, monkeypatch, {"model": "sonnet",
                                                    "model_source": "explicit"})
    assert recs[-1]["dispatched_by"] == "autocompact-relaunch"


def test_compliance_breaks_the_number_down_BY_PRODUCER(tmp_path):
    """The reading that would have answered "why is the default still winning"
    in one command instead of a hand-rolled journal attribution."""
    MD.record(tmp_path, kind="spawn", role="dev", model="opus",
              source="config:dev", dispatched_by="autocompact-relaunch")
    MD.record(tmp_path, kind="spawn", role="dev", model="sonnet",
              source="explicit", dispatched_by="bsq spawn")
    MD.record(tmp_path, kind="spawn", role="dev", model="opus", source="config:dev")
    comp = MD.compliance(tmp_path)
    assert comp["by_producer"] == {
        "autocompact-relaunch": 1, "bsq spawn": 1, "unattributed": 1}


# ---------------------------------------------------------------------------
# negative controls — each mutation names the test that goes red
# ---------------------------------------------------------------------------

def _mutated_autocompact(tmp_path: Path, old: str, new: str, name: str):
    import importlib.util

    src = Path(AC.__file__).read_text(encoding="utf-8")
    assert src.count(old) == 1, f"anchor {old!r} not unique ({src.count(old)})"
    dst = tmp_path / f"ac_{name}.py"
    dst.write_text(src.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"ac_{name}", dst)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_negative_control_relaunch_drops_the_model_again(tmp_path):
    """Mutation: the inheritance returns nothing, i.e. the pre-fix behaviour.
    Goes red: test_a_recycle_keeps_the_sonnet_dispatch_on_sonnet."""
    mut = _mutated_autocompact(
        tmp_path,
        '    model = _clean(meta.get("model"))',
        '    model = None',
        "nomodel")
    assert mut._inherited_model_choice(
        {"model": "sonnet", "model_source": "explicit"})[0] is None
    assert AC._inherited_model_choice(
        {"model": "sonnet", "model_source": "explicit"})[0] == "sonnet"


def test_negative_control_effort_inherited_unconditionally(tmp_path):
    """Mutation: effort carries whether or not it was chosen.
    Goes red: test_a_DEFAULTED_effort_does_NOT_carry — and every recycled
    session would freeze the `[effort]` value that happened to be configured on
    the day it was first spawned."""
    mut = _mutated_autocompact(
        tmp_path,
        '    if str(meta.get("effort_source") or "").strip() == "explicit":\n'
        '        effort = _clean(meta.get("effort"))',
        '    effort = _clean(meta.get("effort"))',
        "alleffort")
    defaulted = {"model": "sonnet", "effort": "high", "effort_source": "config:dev"}
    assert mut._inherited_model_choice(defaulted)[1] == "high"
    assert AC._inherited_model_choice(defaulted)[1] is None


def test_negative_control_inheritance_becomes_a_blanket_downgrade(tmp_path):
    """Mutation: the relaunch forces sonnet regardless of what the predecessor
    ran. Goes red: test_a_recycle_of_a_defaulted_session_still_takes_the_role_default.

    This is the arm that keeps the change honest. A blanket downgrade would
    satisfy every "did it get cheaper" assertion while quietly overriding a
    justified premium dispatch and re-litigating T-0866's role default — which
    this ticket is explicitly not allowed to do."""
    mut = _mutated_autocompact(
        tmp_path,
        '    model = _clean(meta.get("model"))',
        '    model = _clean(meta.get("model")) or "sonnet"',
        "downgrade")
    assert mut._inherited_model_choice({"model_source": "config:dev"})[0] == "sonnet"
    assert AC._inherited_model_choice({"model_source": "config:dev"})[0] is None


def test_the_choice_survives_generation_after_generation(tmp_path, monkeypatch):
    """The real-world shape: `watchrobot/t0757-gate` and `watchrobot/phase2-merge`
    each ran ELEVEN relaunch generations in nine days. A carry-forward that only
    survived one hop would still lose the choice by generation 3, so this drives
    the chain through the successor's OWN md rather than asserting on hop one.

    The mechanism it depends on: `spawn` stamps `model` on the successor's md
    whenever the model was explicit — and an inherited model IS explicit — so
    generation N+1 reads what generation N wrote.
    """
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    from bot_squad_worker.sessions import _write_session_metadata, _session_file
    from bot_squad_worker.sessions import _read_session_metadata

    sid = "S-alice-w-p2"
    _write_session_metadata(
        _session_file(cfg.data_dir, "test-project", sid),
        {"sid": sid, "status": "active", "window": "w", "cwd": str(repo),
         "task_id": "T-0909", "role": "dev",
         "model": "sonnet", "model_source": "explicit",
         "model_reason": "one-file copy change"})

    seen = []
    for _ in range(3):
        captured: list[str] = []
        _stub_tmux(monkeypatch, repo, "w", captured)
        AC._relaunch(cfg, "test-project",
                     {"sid": sid, "role": "dev", "task_id": "T-0909", "window": "w"},
                     lambda role, task_id, aid: "boot prompt")
        seen.append(captured[0])
        # The successor's md becomes the next generation's predecessor.
        mds = sorted((cfg.data_dir / "test-project" / "sessions").glob("*.md"))
        sid = _read_session_metadata(mds[-1])["sid"]

    assert all("--model sonnet" in c for c in seen), seen
    assert not any("--model opus" in c for c in seen), seen
    recs = MD.read(cfg.data_dir)
    assert [r["model"] for r in recs] == ["sonnet"] * 3
    assert recs[-1]["reason"] == "one-file copy change"
