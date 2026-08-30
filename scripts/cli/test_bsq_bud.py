"""T-0932: the `bsq bud` verbs — gradual budding's write side.

The rule the ladder computes is pinned in ``worker/tests/test_budding.py``.
What THIS file pins is the composition the CLI performs, and one ordering that
is easy to "fix" into a bug:

    the shed comes BEFORE the spawn.

``sessions.spawn`` claims a task under ``.task-claim.lock`` and refuses one a
live session already owns. While the parent still holds it, that live session
is the parent — so the intuitive spawn-then-shed order cannot bud AT ALL; it
dies on "already bound to live session <you>". The price of the correct order
is a window where the task has no owner, which is why a failed spawn must
re-bind it. Both halves are asserted here.

`post` is stubbed — per the house rule that `bsq` is not a dry-run surface,
every verb hits the live worker socket, so the param/order contract is tested
here and the live round-trip is a separate walkthrough.

`bsq` is extensionless, so it's loaded via SourceFileLoader (mirrors
test_bsq_pickup.py).
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

SLUG = "proj"
SID = "S-u-gu_x-user-conversation-p9"


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: SLUG)
    monkeypatch.setattr(bsq, "my_sid", lambda: SID)
    monkeypatch.setattr(bsq, "require_sid", lambda explicit: explicit or SID)
    monkeypatch.setattr(bsq, "_my_pane_fields", lambda: ("gu_x-user-conversation", "/tmp"))


def _record(monkeypatch, returns=None, spawn=None):
    """Stub `post` (recording every action+params) and `cmd_spawn`."""
    calls: list[tuple[str, dict]] = []
    returns = returns or {}

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        r = returns.get(action, {"ok": True})
        return r(params) if callable(r) else r

    monkeypatch.setattr(bsq, "post", fake_post)

    def default_spawn(args):
        calls.append(("SPAWN", vars(args)))

    monkeypatch.setattr(bsq, "cmd_spawn", spawn or default_spawn)
    return calls


def _args(**kw):
    base = dict(task=None, window=None, model=None, why=None, fresh=False,
                sid=None, json=False)
    base.update(kw)
    return SimpleNamespace(**base)


# --- bud dev: the order, and what happens when it breaks --------------------

def test_bud_dev_sheds_before_it_spawns(monkeypatch, capsys):
    calls = _record(monkeypatch, returns={
        "list_sessions": {"sessions": [{"sid": SID, "role": "dev"}]},
        "morph_session": {"ok": True, "role": "user-conversation"},
    })

    bsq.cmd_bud_dev(_args(task="T-0001"))

    order = [a for a, _p in calls if a in ("morph_session", "SPAWN")]
    assert order == ["morph_session", "SPAWN"], (
        "spawn ran before the shed — sessions.spawn refuses a task the parent "
        "still holds, so this ordering cannot bud at all")
    morph = next(p for a, p in calls if a == "morph_session")
    assert morph["role"] == "user-conversation"
    assert "task_id" not in morph, "the shed must not re-assert a binding"
    spawn = next(p for a, p in calls if a == "SPAWN")
    assert spawn["ticket"] == "T-0001" and spawn["role"] == "dev"
    assert spawn["owner"] == "budding"


def test_a_failed_spawn_rebinds_the_task_to_the_parent(monkeypatch, capsys):
    """The compensating half. Without it a bud that fails to launch leaves the
    task owned by nobody — worse than not budding."""
    def boom(args):
        raise SystemExit(1)          # what `die()` raises

    calls = _record(monkeypatch, spawn=boom, returns={
        "list_sessions": {"sessions": [{"sid": SID, "role": "teamlead"}]},
        "morph_session": {"ok": True},
    })

    with pytest.raises(SystemExit):
        bsq.cmd_bud_dev(_args(task="T-0001"))

    morphs = [p for a, p in calls if a == "morph_session"]
    assert len(morphs) == 2
    assert morphs[0]["role"] == "user-conversation"
    # restored to the role it actually had, not a hardcoded "dev"
    assert morphs[1]["role"] == "teamlead"
    assert morphs[1]["task_id"] == "T-0001"


def test_bud_dev_defaults_to_the_single_task_you_hold(monkeypatch):
    calls = _record(monkeypatch, returns={
        "budding_decision": {"observation": {"held_task_ids": ["T-0042"]}},
        "list_sessions": {"sessions": []},
        "morph_session": {"ok": True},
    })

    bsq.cmd_bud_dev(_args())

    spawn = next(p for a, p in calls if a == "SPAWN")
    assert spawn["ticket"] == "T-0042"


def test_bud_dev_refuses_to_guess_between_several_tasks(monkeypatch):
    _record(monkeypatch, returns={
        "budding_decision": {"observation": {"held_task_ids": ["T-1", "T-2"]}},
    })

    with pytest.raises(SystemExit):
        bsq.cmd_bud_dev(_args())


# --- bud operator -----------------------------------------------------------

def test_bud_operator_reports_a_refusal_instead_of_pretending(monkeypatch, capsys):
    """The singleton guard is the worker's; the verb must relay its reason
    rather than print a success line over a no-op."""
    _record(monkeypatch, returns={
        "bud_operator": {"ok": True, "spawned": False, "operator": "S-u-operator-p2",
                         "reason": "operator S-u-operator-p2 is already driving"},
    })

    bsq.cmd_bud_operator(_args())

    out = capsys.readouterr().out
    assert "no operator budded" in out
    assert "already driving" in out


def test_bud_operator_does_not_silently_keep_your_task(monkeypatch, capsys):
    """An operator bud does NOT take the parent's dev task. Saying so is the
    point: a session that thinks it handed the work over would stop working it."""
    _record(monkeypatch, returns={
        "bud_operator": {"ok": True, "spawned": True, "operator": "S-u-operator-p3",
                         "reason": "budded off operator S-u-operator-p3"},
        "budding_decision": {"observation": {"held_task_ids": ["T-0007"]}},
    })

    bsq.cmd_bud_operator(_args())

    out = capsys.readouterr().out
    assert "you still hold T-0007" in out
    assert "bsq bud dev T-0007" in out


# --- bud absorb (deflation) -------------------------------------------------

def test_bud_absorb_binds_the_queued_task_to_this_session(monkeypatch):
    monkeypatch.setattr(bsq, "resolve_ticket_ref",
                        lambda ref, slug=None, verb="": (SLUG, ref, Path("/x")))
    calls = _record(monkeypatch, returns={
        "budding_decision": {"observation": {"queued_requests": ["T-0009"]}},
        "morph_session": {"ok": True, "role": "dev"},
    })

    bsq.cmd_bud_absorb(_args())

    morph = next(p for a, p in calls if a == "morph_session")
    assert morph["role"] == "dev" and morph["task_id"] == "T-0009"


def test_bud_absorb_refuses_when_the_queue_is_not_a_single_task(monkeypatch):
    _record(monkeypatch, returns={
        "budding_decision": {"observation": {"queued_requests": ["T-1", "T-2"]}},
    })

    with pytest.raises(SystemExit):
        bsq.cmd_bud_absorb(_args())


# --- bud (read) -------------------------------------------------------------

def test_bud_status_asks_the_worker_and_prints_the_command(monkeypatch, capsys):
    _record(monkeypatch, returns={
        "budding_decision": {
            "level": "L0-solo", "verdict": "bud_dev", "task_id": "T-0001",
            "command": "bsq bud dev T-0001", "reason": "two requests queued",
            "observation": {
                "counts": {"bud_devs": 0, "queued_requests": 2},
                "thresholds": {"request_pressure": 2},
                "root_sids": [SID], "queued_requests": ["T-2", "T-3"],
                "held_task_ids": ["T-0001"], "bud_dev_sids": [],
                "operator_sids": [],
            },
        },
    })

    bsq.cmd_bud_status(_args())

    out = capsys.readouterr().out
    assert "L0-solo" in out
    assert "bud_dev" in out
    assert "run: bsq bud dev T-0001" in out


def test_bud_status_is_a_pure_read(monkeypatch):
    calls = _record(monkeypatch, returns={
        "budding_decision": {"level": "L0-solo", "verdict": "hold",
                             "command": "", "reason": "nothing to do",
                             "observation": {}},
    })

    bsq.cmd_bud_status(_args())

    assert [a for a, _p in calls] == ["budding_decision"], (
        "the read verb must not spawn, morph or write anything")
