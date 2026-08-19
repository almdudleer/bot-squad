"""T-0909: `bsq spawn` must REFUSE a dev dispatch that has not stated its model.

WHAT THIS PINS, AND WHY IT IS A GATE AND NOT A PARAGRAPH. The policy ("size the
model to the ticket, prefer sonnet on simple work") shipped as prose in
`operator.md` on 2026-08-11 (T-0866) and every spawn was given an explicit
`--model` flag the same day (T-0871). The worker journal for the eight days that
followed:

    104  role=dev model=opus   (explicit)     <- an operator TYPED --model opus
     92  role=dev model=opus   (config:dev)   <- nobody typed anything
      2  role=dev model=sonnet (explicit)

One dev dispatch in 198 stepped down. The flag existed, was used, and was used
to pin the premium tier. So the arms below are not style checks — each one is a
path that measurably stayed open while the prose said it was closed:

  * NO `--model` at all      -> the 92 silent-default dispatches
  * `--model opus`, no why   -> the 104 reflexive premium dispatches
  * a `--why` too short to be a reason -> the way a required field becomes a
    formality once someone learns to type "x"

NEGATIVE CONTROLS. Every guard here is proved able to FAIL, not just able to
pass: `test_negative_control_*` mutates ONE property of the gate on a scratch
copy of `bsq` and asserts the refusal disappears. A guard nobody has watched
fail is a guard nobody has tested.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"


def _load(path: Path, name: str = "bsq_mod"):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bsq = _load(_BSQ_PATH)


def _args(**kw):
    """A spawn Namespace with the fields the gate reads."""
    base = dict(role="dev", model=None, why=None, provider=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _gate(mod, **kw):
    return mod._gate_model_choice(_args(**kw), "T-0909")


# --- the arms that must be REFUSED -----------------------------------------

def test_no_model_is_refused():
    """The 92-dispatch hole: omitting --model silently took the opus default."""
    with pytest.raises(SystemExit):
        _gate(bsq)


def test_premium_without_reason_is_refused():
    """The 104-dispatch hole: --model opus typed reflexively, no justification."""
    with pytest.raises(SystemExit):
        _gate(bsq, model="opus")


def test_premium_with_token_reason_is_refused():
    """A required field that accepts "x" is not a required field."""
    with pytest.raises(SystemExit):
        _gate(bsq, model="opus", why="x")
    with pytest.raises(SystemExit):
        _gate(bsq, model="opus", why="opus")


@pytest.mark.parametrize("pinned", ["claude-opus-4-8", "opus[1m]", "claude-opus-5",
                                    "fable", "claude-fable-5"])
def test_pinned_premium_ids_are_gated_too(pinned):
    """The gate matches the CLASS, so it cannot be sidestepped by spelling the
    model as a pinned id — the exact shape T-0694 showed reaches `claude`."""
    with pytest.raises(SystemExit):
        _gate(bsq, model=pinned)


# --- the arms that must PASS (the cheap path stays frictionless) ------------

def test_sonnet_passes_with_no_reason():
    """The step-down model is the frictionless path — that asymmetry IS the
    control. Requiring paperwork for both would just tax dispatch."""
    assert _gate(bsq, model="sonnet") == ""


def test_premium_with_a_real_reason_passes_and_returns_it():
    reason = "cause unknown, touches the spawn/recycle lifecycle"
    assert _gate(bsq, model="opus", why=reason) == reason


def test_non_dev_roles_are_not_gated():
    """teamlead/qa/user-conversation already default to a step-down model in
    system_settings.toml; gating them would add friction with no spend behind
    it. An operator dispatch is system-owned, not a per-ticket judgement."""
    for role in ("teamlead", "qa", "operator", "prod-teamlead"):
        assert _gate(bsq, role=role) == ""


def test_codex_dispatch_is_not_gated(monkeypatch):
    """The pricing table here is Claude's. A codex model must not be measured
    against it, and must not be refused for failing to name a Claude class."""
    monkeypatch.setattr(bsq, "_spawn_provider", lambda a: "codex")
    assert _gate(bsq, model="luna") == ""


# --- the class table -------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("sonnet", "sonnet"), ("opus", "opus"), ("fable", "fable"),
    ("claude-opus-5", "opus"), ("opus[1m]", "opus"), ("claude-sonnet-5", "sonnet"),
    ("", ""), ("luna", ""), ("codex", ""),
])
def test_model_class(value, expected):
    assert bsq._model_class(value) == expected


def test_premium_table_mirrors_the_worker():
    """`scripts/cli/bsq` never imports the worker package (deliberate — see the
    `_derive_role` mirror note in the CLI). So the pricing table exists twice,
    and this is the check that keeps the two copies from drifting apart."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
    from bot_squad_worker import model_dispatch

    assert set(bsq._PREMIUM_MODEL_CLASSES) == set(model_dispatch.PREMIUM_CLASSES)
    assert set(bsq._ECONOMY_MODEL_CLASSES) == set(model_dispatch.ECONOMY_CLASSES)
    for v in ("sonnet", "opus", "fable", "claude-opus-4-8", "opus[1m]", "", "luna"):
        assert bsq._model_class(v) == model_dispatch.model_class(v), v


# --- negative controls: prove each guard CAN fail --------------------------
#
# Each one takes a byte copy of `bsq`, removes ONE property of the gate, and
# asserts the refusal it was responsible for disappears. The named test above
# each mutation is the one that goes red if that property is ever dropped.

def _mutated(tmp_path: Path, old: str, new: str, name: str):
    dst = tmp_path / f"bsq_{name}"
    src = _BSQ_PATH.read_text(encoding="utf-8")
    assert src.count(old) == 1, f"anchor {old!r} not unique ({src.count(old)})"
    dst.write_text(src.replace(old, new), encoding="utf-8")
    return _load(dst, f"bsq_mut_{name}")


def test_negative_control_dropping_the_missing_model_check(tmp_path):
    """Mutation: the empty-`--model` branch returns instead of refusing.
    Goes red: test_no_model_is_refused."""
    mut = _mutated(
        tmp_path,
        '    if not model:\n        die(f"spawn {ticket}: a dev dispatch must STATE its model',
        '    if not model:\n        return why\n        die(f"spawn {ticket}: a dev dispatch must STATE its model',
        "nomodel")
    assert _gate(mut) == ""           # the refusal is GONE — the guard was real
    with pytest.raises(SystemExit):   # ...and the other arms still hold
        _gate(mut, model="opus")


def test_negative_control_dropping_the_premium_reason_requirement(tmp_path):
    """Mutation: `opus` drops out of the premium table.
    Goes red: test_premium_without_reason_is_refused."""
    mut = _mutated(tmp_path,
                   '_PREMIUM_MODEL_CLASSES = ("opus", "fable")',
                   '_PREMIUM_MODEL_CLASSES = ("fable",)',
                   "nopremium")
    assert _gate(mut, model="opus") == ""
    with pytest.raises(SystemExit):   # fable still gated -> the arm is specific
        _gate(mut, model="fable")


def test_negative_control_dropping_the_reason_length_floor(tmp_path):
    """Mutation: the minimum-length floor drops to 0.
    Goes red: test_premium_with_token_reason_is_refused."""
    mut = _mutated(tmp_path,
                   "_MODEL_REASON_MIN_LEN = 15",
                   "_MODEL_REASON_MIN_LEN = 0",
                   "nofloor")
    assert _gate(mut, model="opus", why="x") == "x"


def test_negative_control_widening_the_gated_roles(tmp_path):
    """Mutation: the gate covers every role.
    Goes red: test_non_dev_roles_are_not_gated — the arm proving the gate is
    SCOPED, not blanket. Without it, "refuses everything" would pass all the
    refusal arms above and quietly tax every teamlead dispatch."""
    mut = _mutated(tmp_path,
                   '_MODEL_GATED_ROLES = ("dev",)',
                   '_MODEL_GATED_ROLES = ("dev", "teamlead", "qa", "operator", "prod-teamlead")',
                   "allroles")
    with pytest.raises(SystemExit):
        _gate(mut, role="teamlead")


# --- the wiring: the gate is actually REACHED by cmd_spawn -----------------

def test_gate_is_called_from_the_fresh_spawn_path():
    """A correct gate that no path reaches is not a control
    ([[feedback_prove_the_path_is_reached]]). This pins the call site, and that
    it sits in the FRESH-spawn branch — after the expert-resume return, which
    makes no new model decision."""
    src = _BSQ_PATH.read_text(encoding="utf-8")
    call = "_model_reason = _gate_model_choice(args, ticket)"
    assert src.count(call) == 1
    assert src.index(call) > src.index("_resume_expert(slug, expert")
    assert src.index(call) < src.index("res = _post_spawn_session(params)")
    # ...and the reason it returns is what actually rides to the worker.
    assert 'params["model_reason"] = _model_reason' in src


def test_cluster_launch_carries_the_model_decision():
    """`cluster-launch` reuses cmd_spawn, so it would otherwise be a hole in
    the gate — a dev dispatch with model hardcoded to None."""
    src = _BSQ_PATH.read_text(encoding="utf-8")
    assert 'model=getattr(args, "model", None),' in src
    assert 'why=getattr(args, "why", None),' in src
    parser = bsq.build_parser() if hasattr(bsq, "build_parser") else None
    if parser is not None:
        ns = parser.parse_args(["cluster-launch", "T-0001", "--model", "sonnet"])
        assert ns.model == "sonnet"


# --- the deploy-skew window ------------------------------------------------
#
# `bsq` is a symlink into the dev clone: a CLI edit is live for every session on
# this host immediately, while the worker only gets the matching change at the
# next deploy. `_action_spawn_session` rejects unknown params BY NAME — measured
# against the live worker while writing this ticket:
#
#     worker rejected spawn_session: spawn_session got unexpected params: ['model_reason']
#
# so an unconditional new param would have broken every dispatch on the host for
# the length of that window. These pin the fallback that closes it.

def test_old_worker_falls_back_to_the_pre_t0909_param_shape(monkeypatch):
    calls = []

    def fake_post(action, params, **kw):
        calls.append(dict(params))
        if len(calls) == 1:
            raise bsq.WorkerError(
                "worker rejected spawn_session: spawn_session got unexpected "
                "params: ['dispatched_by', 'model_reason']")
        return {"sid": "S-x"}

    monkeypatch.setattr(bsq, "post", fake_post)
    res = bsq._post_spawn_session(
        {"slug": "p", "window": "w", "model": "opus",
         "model_reason": "why", "dispatched_by": "bsq spawn"})

    assert res == {"sid": "S-x"}
    assert len(calls) == 2
    assert set(bsq._T0909_SPAWN_PARAMS) <= set(calls[0])      # tried the new shape
    assert not set(bsq._T0909_SPAWN_PARAMS) & set(calls[1])   # retried without it
    assert calls[1]["model"] == "opus"                        # ...losing nothing else


def test_the_fallback_does_not_swallow_a_REAL_spawn_failure(monkeypatch):
    """A blanket retry would turn "operator already running" or a tmux failure
    into a silent second attempt. Only the param-shape error degrades."""
    def fake_post(action, params, **kw):
        raise bsq.WorkerError("operator already running for 'p': S-1")

    monkeypatch.setattr(bsq, "post", fake_post)
    with pytest.raises(SystemExit):
        bsq._post_spawn_session({"slug": "p", "window": "operator"})


def test_the_fallback_strips_exactly_what_it_sends():
    """The strip list and the send list are the same tuple, so they cannot
    drift into "sends three params, strips two"."""
    src = _BSQ_PATH.read_text(encoding="utf-8")
    for name in bsq._T0909_SPAWN_PARAMS:
        assert f'params["{name}"]' in src or f'params["{name}"] =' in src, name


# --- the compliance view is injected at every operator session start --------

def _render(monkeypatch, capsys, payload, **kw):
    import types
    monkeypatch.setattr(bsq, "post", lambda a, p, **k: payload)
    ns = types.SimpleNamespace(limit=25, role="dev", kinds="spawn", slug=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    bsq.cmd_model_compliance(ns)
    return capsys.readouterr().out


def test_reasonless_premium_dispatches_are_counted_not_listed(monkeypatch, capsys):
    """Found by walking this through by hand before automating it (T-0158):
    listing every premium dispatch put 20 lines of "(none recorded)" into the
    operator's session-start banner. The pre-gate and automated dispatches are
    the majority and have nothing to read — they are a count, not a list."""
    payload = {
        "ok": True, "n": 22, "economy": 2, "premium": 20, "unknown": 0,
        "economy_pct": 9.1, "by_source": {"explicit": 13, "config:dev": 9},
        "oldest_ts": "2026-08-11T00:00:00", "newest_ts": "2026-08-19T00:00:00",
        "summary": "model mix: 2/22 …",
        "premium_reasons": (
            [{"ts": "2026-08-1x", "task_id": "T-old", "model": "opus",
              "source": "config:dev", "reason": ""} for _ in range(19)]
            + [{"ts": "2026-08-19", "task_id": "T-0909", "model": "opus",
                "source": "explicit", "reason": "cause unknown, spawn lifecycle"}]
        ),
    }
    out = _render(monkeypatch, capsys, payload)

    assert "(none recorded)" not in out
    assert "premium with NO stated reason: 19" in out
    assert "T-0909" in out and "cause unknown" in out
    # A banner injected at every operator session start has to stay small.
    assert len(out.strip().splitlines()) <= 10, out


def test_at_most_N_stated_reasons_are_listed(monkeypatch, capsys):
    payload = {
        "ok": True, "n": 12, "economy": 0, "premium": 12, "unknown": 0,
        "economy_pct": 0.0, "by_source": {"explicit": 12},
        "oldest_ts": "", "newest_ts": "", "summary": "s",
        "premium_reasons": [
            {"ts": "t", "task_id": f"T-{i:04d}", "model": "opus",
             "source": "explicit", "reason": f"reason number {i}"}
            for i in range(12)
        ],
    }
    out = _render(monkeypatch, capsys, payload)
    listed = [ln for ln in out.splitlines() if ln.strip().startswith("- ")]
    assert len(listed) == bsq._MODEL_COMPLIANCE_REASONS_SHOWN
    assert "reason number 11" in out    # newest kept
    assert "reason number 0" not in out  # oldest dropped


def test_an_empty_ledger_prints_no_percentage(monkeypatch, capsys):
    """The defect this whole ticket started from, at the render layer: a
    measurement of nothing must not come out looking like a measurement."""
    out = _render(monkeypatch, capsys, {
        "ok": True, "n": 0, "economy": 0, "premium": 0, "unknown": 0,
        "economy_pct": None, "by_source": {}, "premium_reasons": [],
        "oldest_ts": "", "newest_ts": "",
        "summary": "model mix: no dispatches recorded yet (ledger empty)",
    })
    assert "no dispatches recorded" in out
    assert "%" not in out
