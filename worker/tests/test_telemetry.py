"""T-0210: resource-telemetry sampler tests.

Covers the pure parsing/threshold helpers, incremental offset-based transcript
reads, the per-session + quota persistence, and crossing-only TARGETED
alerting (operator SID + the session's TL SID — never a broadcast).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_squad_worker.config import Config, Project
from bot_squad_worker import telemetry as T


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_usage_window_tokens_sums_input_cache_read_and_creation():
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 60000,
        "cache_creation_input_tokens": 500,
        "output_tokens": 999,  # NOT part of the window
    }
    assert T._usage_window_tokens(usage) == 60600


def _assistant(window_parts, output, model="claude-opus-4-8"):
    inp, cr, cc = window_parts
    return json.dumps({
        "type": "assistant",
        "message": {"model": model, "usage": {
            "input_tokens": inp, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc, "output_tokens": output,
        }},
    })


def test_scan_lines_uses_last_assistant_window_and_sums_output():
    lines = [
        _assistant((2, 100, 0), 50),
        json.dumps({"type": "user", "message": {"role": "user"}}),
        _assistant((2, 420000, 1000), 200),
    ]
    scan = T.scan_lines(lines)
    assert scan["last_window"] == 420000 + 1000 + 2
    assert scan["model"] == "claude-opus-4-8"
    assert scan["output_sum"] == 250
    assert scan["saw_429"] is False


def test_scan_lines_detects_429_rate_limit():
    lines = [json.dumps({
        "type": "assistant", "error": "rate_limit", "apiErrorStatus": 429,
        "isApiErrorMessage": True,
    })]
    assert T.scan_lines(lines)["saw_429"] is True


def test_scan_lines_skips_partial_or_garbage_lines():
    scan = T.scan_lines(['{"type": "assist', "", _assistant((1, 10, 0), 5)])
    assert scan["last_window"] == 11
    assert scan["output_sum"] == 5


def _boundary(post_tokens, pre_tokens=95_555):
    meta = {"trigger": "manual", "preTokens": pre_tokens}
    if post_tokens is not None:
        meta["postTokens"] = post_tokens
    return json.dumps({
        "type": "system", "subtype": "compact_boundary",
        "content": "Conversation compacted", "compactMetadata": meta,
    })


def test_scan_lines_reports_compact_boundary_post_tokens():
    """T-0722: a /compact writes its boundary into the same transcript, and
    the last usage line BEFORE it is the pre-compact window."""
    scan = T.scan_lines([_assistant((95_498, 0, 0), 20), _boundary(11_015)])
    assert scan["last_window"] == 95_498       # pre-compact — stale
    assert scan["compact_post_window"] == 11_015
    assert scan["usage_after_compact"] is False


def test_scan_lines_flags_usage_after_the_compact_boundary():
    scan = T.scan_lines([
        _assistant((95_498, 0, 0), 20), _boundary(11_015),
        _assistant((38_000, 0, 0), 7),
    ])
    assert scan["last_window"] == 38_000
    assert scan["compact_post_window"] == 11_015
    assert scan["usage_after_compact"] is True


def test_scan_lines_compact_fields_default_when_no_boundary():
    scan = T.scan_lines([_assistant((1, 10, 0), 5)])
    assert scan["compact_post_window"] is None
    assert scan["usage_after_compact"] is False


def test_scan_lines_boundary_without_post_tokens_measures_none():
    scan = T.scan_lines([_assistant((120_000, 0, 0), 5), _boundary(None)])
    assert scan["compact_post_window"] is None
    assert scan["last_window"] == 120_000


def test_context_and_memory_levels(monkeypatch):
    # T-0857 (stakeholder 2026-08-11): default ceiling 300k (was 700k, T-0210),
    # warn at the same 0.8 warn:urgent ratio → 240k. No env override set.
    monkeypatch.delenv("BOT_SQUAD_CONTEXT_CEILING", raising=False)
    assert T.context_level(239_999) == "none"
    assert T.context_level(240_000) == "warn"
    assert T.context_level(299_999) == "warn"
    assert T.context_level(300_000) == "urgent"
    # T-0834: the memory level is taken on LOADED tokens (MEMORY.md), not the
    # whole store.
    monkeypatch.delenv("BOT_SQUAD_MEMORY_LOADED_WARN_TOKENS", raising=False)
    assert T.memory_loaded_level(T.MEMORY_LOADED_WARN_TOKENS - 1) == "none"
    assert T.memory_loaded_level(T.MEMORY_LOADED_WARN_TOKENS) == "warn"


def test_memory_loaded_warn_env_override(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_MEMORY_LOADED_WARN_TOKENS", "9000")
    assert T.memory_loaded_warn() == 9_000
    assert T.memory_loaded_level(8_999) == "none"
    assert T.memory_loaded_level(9_000) == "warn"
    # garbage / non-positive can never disable the alert
    monkeypatch.setenv("BOT_SQUAD_MEMORY_LOADED_WARN_TOKENS", "nope")
    assert T.memory_loaded_warn() == T.MEMORY_LOADED_WARN_TOKENS
    monkeypatch.setenv("BOT_SQUAD_MEMORY_LOADED_WARN_TOKENS", "0")
    assert T.memory_loaded_warn() == T.MEMORY_LOADED_WARN_TOKENS


def test_context_ceiling_default_is_300k(monkeypatch):
    """T-0857: lowered 700k → 300k on the stakeholder's decision. The warn line
    is DERIVED from the ceiling (0.8), so it moves with it — the failure mode
    this pins is a ceiling change leaving an absolute warn behind at 560k, which
    at a 300k ceiling would sit above 'urgent' and never fire."""
    monkeypatch.delenv("BOT_SQUAD_CONTEXT_CEILING", raising=False)
    assert T.context_ceiling() == 300_000
    assert T.context_warn() == 240_000      # 0.8 ratio preserved
    assert T.context_urgent() == 300_000
    assert T.context_warn() < T.context_urgent()


def test_context_ceiling_env_override_scales_warn(monkeypatch):
    # tunable without a redeploy: env knob overrides the ceiling and the warn
    # level scales with it (same 0.8 ratio).
    monkeypatch.setenv("BOT_SQUAD_CONTEXT_CEILING", "900000")
    assert T.context_ceiling() == 900_000
    assert T.context_warn() == 720_000
    assert T.context_level(719_999) == "none"
    assert T.context_level(720_000) == "warn"
    assert T.context_level(900_000) == "urgent"


def test_context_ceiling_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_CONTEXT_CEILING", "not-a-number")
    assert T.context_ceiling() == 300_000


def test_crossed_only_on_strictly_worse_level():
    assert T.crossed("none", "warn") is True
    assert T.crossed("warn", "urgent") is True
    assert T.crossed("none", "urgent") is True
    assert T.crossed("warn", "warn") is False
    assert T.crossed("urgent", "warn") is False  # improvement is not a crossing
    assert T.crossed("warn", "none") is False


def test_read_chunk_incremental_only_advances_past_complete_lines(tmp_path: Path):
    f = tmp_path / "t.jsonl"
    f.write_text("line1\nline2\n")
    text, off = T._read_chunk(f, 0)
    assert text == "line1\nline2\n"
    assert off == len("line1\nline2\n")
    # append a partial (no trailing newline) line — offset must NOT advance
    with f.open("a") as fh:
        fh.write("partial-no-newline")
    text2, off2 = T._read_chunk(f, off)
    assert text2 == ""
    assert off2 == off
    # complete the line — now it is read
    with f.open("a") as fh:
        fh.write("\n")
    text3, off3 = T._read_chunk(f, off)
    assert text3 == "partial-no-newline\n"
    assert off3 == off + len("partial-no-newline\n")


def test_memory_stats_counts_files_and_estimates_tokens(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("x" * 400)
    (mem / "a.md").write_text("y" * 400)
    (mem / "ignore.txt").write_text("z" * 4000)  # not .md
    stats = T.memory_stats(mem)
    assert stats["files"] == 2
    assert stats["bytes"] == 800
    assert stats["tokens_est"] == 200  # 800 // 4


def test_memory_stats_splits_loaded_index_from_recall_only(tmp_path: Path):
    """T-0834 defect 1: the flat total is the WHOLE store, and only MEMORY.md is
    loaded into a session. The two must be reported as separate, labelled
    figures — the old alert named the loaded one while measuring the total, and
    overstated by 13x on the live install.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("x" * 400)          # the loaded index
    for i in range(12):
        (mem / f"fact{i}.md").write_text("y" * 400)    # recall-only
    stats = T.memory_stats(mem)
    assert stats["files"] == 13 and stats["tokens_est"] == 1300      # whole store
    assert stats["loaded"] == {"files": 1, "bytes": 400, "tokens_est": 100}
    assert stats["recall"] == {"files": 12, "bytes": 4800, "tokens_est": 1200}
    # the split is the point: the total is 13x the loaded figure here, and the
    # test says so in the same shape the live measurement did.
    assert stats["tokens_est"] == 13 * stats["loaded"]["tokens_est"]
    assert stats["path"] == str(mem)


def test_memory_stats_store_with_no_index_reports_zero_loaded(tmp_path: Path):
    """A store carrying only recall files loads NOTHING into a session. It must
    read as zero loaded, not fall back to the total."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "fact.md").write_text("y" * 4000)
    stats = T.memory_stats(mem)
    assert stats["loaded"]["tokens_est"] == 0
    assert stats["recall"]["tokens_est"] == 1000
    assert T.memory_loaded_level(stats["loaded"]["tokens_est"]) == "none"


def test_memory_stats_missing_dir_is_zero(tmp_path: Path):
    zero = {"files": 0, "bytes": 0, "tokens_est": 0}
    assert T.memory_stats(tmp_path / "nope") == {
        **zero, "loaded": zero, "recall": zero, "path": str(tmp_path / "nope"),
    }


def test_shared_memory_stats_counts_project_dev_clone_memory(tmp_path: Path):
    """T-0502: the SHARED, git-ignored project memory dir is counted from the
    project's repo_path (dev clone) — distinct from the per-session dir."""
    cfg = _make_cfg(tmp_path)  # repo_path = tmp_path / "repo"
    mem = tmp_path / "repo" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("x" * 400)
    (mem / "fact.md").write_text("y" * 400)
    (mem / "ignore.txt").write_text("z" * 4000)  # non-.md, not counted
    stats = T.shared_memory_stats(cfg, "proj")
    assert (stats["files"], stats["bytes"], stats["tokens_est"]) == (2, 800, 200)
    assert stats["loaded"] == {"files": 1, "bytes": 400, "tokens_est": 100}


def test_shared_memory_stats_unknown_slug_or_missing_dir_is_zero(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    zero = {"files": 0, "bytes": 0, "tokens_est": 0}
    for stats in (T.shared_memory_stats(cfg, "no-such-slug"),   # unknown project
                  T.shared_memory_stats(cfg, "proj")):          # dir absent
        assert (stats["files"], stats["bytes"], stats["tokens_est"]) == (0, 0, 0)
        assert stats["loaded"] == zero and stats["recall"] == zero


def test_compute_burn_over_window():
    # +3600 tokens over 1800s = 7200 tok/hr
    assert T.compute_burn([[0, 1000], [1800, 4600]]) == pytest.approx(7200.0)
    assert T.compute_burn([[0, 1000]]) is None  # need >= 2 samples


def test_project_exhaustion_requires_anchor_and_positive_burn():
    assert T.project_exhaustion(None, 1000, 5000) == (None, None)
    rem, eta = T.project_exhaustion({"budget_tokens": 100_000}, 20_000, 8_000)
    assert rem == 80_000
    assert eta is not None and eta.endswith("Z")
    # no burn → remaining known, eta unknown
    assert T.project_exhaustion({"budget_tokens": 100_000}, 20_000, None) == (80_000, None)


def test_find_transcript_globs_by_uuid(tmp_path: Path):
    proj = tmp_path / ".claude" / "projects" / "-some-encoded-cwd"
    proj.mkdir(parents=True)
    uuid = "abc12345-0000-0000-0000-000000000000"
    (proj / f"{uuid}.jsonl").write_text("")
    assert T.find_transcript(str(tmp_path), uuid) == proj / f"{uuid}.jsonl"
    assert T.find_transcript(str(tmp_path), "missing-uuid") is None


# ---------------------------------------------------------------------------
# Sampling + persistence (integration with a fake home + monkeypatched sessions)
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path) -> Config:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    proj = Project(
        slug="proj", display_name="Proj", repo_path=tmp_path / "repo",
        deploy_branch="dev", master_branch="master", prod_url="", staging_url="",
        dev_url="", deploy_targets=("staging",), tg_chat="CHAT123",
    )
    return Config(config_dir=config_dir, projects={"proj": proj})


def _write_transcript(home: Path, uuid: str, lines: list[str]) -> Path:
    proj = home / ".claude" / "projects" / "-encoded"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{uuid}.jsonl"
    f.write_text("".join(l + "\n" for l in lines))
    return f


@pytest.fixture
def fake_session(tmp_path, monkeypatch):
    """A fake LIVE session whose transcript lives under a fake home."""
    home = tmp_path / "home"
    home.mkdir()
    uuid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(T, "_human_tg", lambda *a, **k: None)
    # capture targeted peer sends
    sent: list[tuple] = []
    monkeypatch.setattr(T, "_peer_to", lambda cfg, slug, sid, text: sent.append((sid, text)))
    import bot_squad_worker.sessions as _sessions
    monkeypatch.setattr(_sessions, "_get_user_home", lambda: str(home))
    monkeypatch.setattr(_sessions, "_get_current_user", lambda: "almdudleer")

    rows = [{
        "sid": "S-almdudleer-dev-p5", "status": "active", "claude_uuid": uuid,
        "linux_user": "almdudleer", "role": "dev", "task_id": "T-1",
        "tmux_session": "proj",
    }]
    monkeypatch.setattr(_sessions, "list_sessions", lambda cfg, slug: rows)
    # no team → tl_for_sid None, _all_tls empty
    monkeypatch.setattr(T, "_all_tls", lambda cfg, slug: [])
    import bot_squad_worker.teams as _teams
    monkeypatch.setattr(_teams, "tl_for_sid", lambda cfg, slug, sid: None)
    return {"home": home, "uuid": uuid, "rows": rows, "sent": sent}


def test_sample_writes_record_with_context_and_memory(tmp_path, fake_session):
    cfg = _make_cfg(tmp_path)
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    # memory dir next to the transcript
    (f.parent / "memory").mkdir()
    (f.parent / "memory" / "MEMORY.md").write_text("m" * 1200)

    out = T.sample(cfg, "proj")
    assert out["sampled"] == 1
    rec = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                      / "S-almdudleer-dev-p5.json").read_text())
    assert rec["context"]["tokens"] == 70102
    assert rec["context"]["model"] == "claude-opus-4-8"
    assert rec["memory"]["files"] == 1
    assert rec["memory"]["tokens_est"] == 300
    # first sample tail-reads → offset at EOF, output accrual starts at 0
    assert rec["output_tokens_cum"] == 0
    assert rec["transcript_offset"] == f.stat().st_size


def test_sample_incremental_accrues_output_and_carries_context(tmp_path, fake_session):
    cfg = _make_cfg(tmp_path)
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    T.sample(cfg, "proj")  # first sample: offset → EOF, cum 0

    # session generates two more turns
    with f.open("a") as fh:
        fh.write(_assistant((2, 90000, 200), 300) + "\n")
        fh.write(_assistant((2, 95000, 200), 700) + "\n")
    T.sample(cfg, "proj")
    rec = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                      / "S-almdudleer-dev-p5.json").read_text())
    assert rec["context"]["tokens"] == 95202  # latest window
    assert rec["output_tokens_cum"] == 1000   # 300 + 700 accrued incrementally

    # a tick with NO new lines carries context forward (no reset to 0)
    T.sample(cfg, "proj")
    rec2 = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                       / "S-almdudleer-dev-p5.json").read_text())
    assert rec2["context"]["tokens"] == 95202
    assert rec2["output_tokens_cum"] == 1000


def test_sample_carries_compact_phase_across_ticks(tmp_path, fake_session):
    # T-0467: the compact handoff state machine spans ticks — _sample_one must
    # carry rec['compact'] forward (like alert_fired_at), else the 'writing'
    # phase is lost between samples and a handoff can never finalize.
    cfg = _make_cfg(tmp_path)
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    T.sample(cfg, "proj")
    rec_path = (cfg.data_dir / "proj" / "_worker" / "telemetry"
                / "S-almdudleer-dev-p5.json")
    rec = json.loads(rec_path.read_text())
    rec["compact"] = {"phase": "writing", "armed_at": 1.0, "arm_mtime": 0.0,
                      "artifact_path": "/x/T-0001.md"}
    rec_path.write_text(json.dumps(rec))

    # next tick (no new transcript lines) must preserve the compact phase
    T.sample(cfg, "proj")
    rec2 = json.loads(rec_path.read_text())
    assert rec2["compact"]["phase"] == "writing"
    assert rec2["compact"]["artifact_path"] == "/x/T-0001.md"


def test_context_alert_fires_once_per_crossing(tmp_path, fake_session):
    cfg = _make_cfg(tmp_path)
    # Sizes are DERIVED from the live thresholds, not hardcoded: this test is
    # about the crossing/dedupe machinery, so a ceiling change (T-0857 moved it
    # 700k → 300k) must not silently turn the "warn" sample into an "urgent" one
    # and leave the test asserting a property it is no longer exercising.
    # `_assistant` window = input + cache_read + cache_creation = 2 + N + 100.
    def _at(window: int) -> int:
        return window - 102

    quiet, warn_1, warn_2 = (T.context_warn() // 4,
                             T.context_warn() + 20_000,
                             T.context_warn() + 30_000)
    assert quiet < T.context_warn() <= warn_1 < warn_2 < T.context_urgent(), (
        "sample sizes no longer straddle the thresholds they are named for")

    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, _at(quiet), 100), 500)])  # → none
    T.sample(cfg, "proj")
    assert fake_session["sent"] == []  # no crossing yet

    # cross into warn (>= 0.8 × the ceiling)
    with f.open("a") as fh:
        fh.write(_assistant((2, _at(warn_1), 100), 100) + "\n")
    T.sample(cfg, "proj")
    warn_alerts = [s for s in fake_session["sent"] if "context high" in s[1]]
    assert len(warn_alerts) == 0  # no team/operator target in this fixture
    # but the human_tg path is exercised; verify dedupe via record state
    rec = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                      / "S-almdudleer-dev-p5.json").read_text())
    assert rec["last_alert"]["context"] == "warn"

    # staying in warn must NOT re-alert (record level unchanged, no new crossing)
    before = len(fake_session["sent"])
    with f.open("a") as fh:
        fh.write(_assistant((2, _at(warn_2), 100), 100) + "\n")
    T.sample(cfg, "proj")
    assert len(fake_session["sent"]) == before


def _add_operator_row(fake_session, sid="S-almdudleer-operator-p1", status="active",
                      uuid="op-uuid"):
    fake_session["rows"].append({
        "sid": sid, "status": status,
        "claude_uuid": uuid, "linux_user": "almdudleer",
        "role": "operator", "task_id": None, "tmux_session": "proj",
    })


def _big_index(f: Path, chars: int = 200_000) -> Path:
    """Give the transcript's memory store a MEMORY.md over the loaded warn line."""
    mem = f.parent / "memory"
    mem.mkdir(exist_ok=True)
    (mem / "MEMORY.md").write_text("m" * chars)
    return mem


def test_memory_alert_goes_to_the_operator_only_never_broadcast(tmp_path, fake_session, monkeypatch):
    """T-0834 defect 3: the advice is addressed to whoever can act.

    The store is the operator's to curate; a dev session complying with
    "consider pruning" would be editing another actor's durable state. So the
    TL and the affected session get nothing, and the targeting guardrail
    (stakeholder 2026-06-18: never a role/broadcast target) still holds.
    """
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    import bot_squad_worker.teams as _teams
    monkeypatch.setattr(_teams, "tl_for_sid",
                        lambda cfg, slug, sid: "S-almdudleer-TL-p9")
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])

    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    _big_index(f)
    T.sample(cfg, "proj")

    mem_sent = [(sid, t) for sid, t in fake_session["sent"] if "memory store" in t]
    targets = [sid for sid, _ in mem_sent]
    assert targets == ["S-almdudleer-operator-p1"]  # operator, and ONLY the operator
    assert "S-almdudleer-TL-p9" not in targets      # cannot act on it — not told
    assert "S-almdudleer-dev-p5" not in targets     # nor the session it used to name
    assert "all" not in targets and "teamlead" not in targets  # never a role/broadcast


def test_memory_alert_fires_once_for_a_store_many_sessions_share(tmp_path, fake_session):
    """T-0834 defect 2 + 4, the measured one: six sessions reading ONE store
    produce ONE alert, not six.

    All six transcripts sit in the same project dir, so they read the same
    ``memory/`` — which is why the live install reported the byte-identical
    ``~40,138 tok across 61 files`` for nine different SIDs. Alerting per
    session sends one fact once per reader.
    """
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    _big_index(f)
    for i in range(5):
        uuid = f"peer-uuid-{i}"
        _write_transcript(fake_session["home"], uuid, [_assistant((2, 70000, 100), 500)])
        fake_session["rows"].append({
            "sid": f"S-almdudleer-drive-p{i}", "status": "active", "claude_uuid": uuid,
            "linux_user": "almdudleer", "role": "dev", "task_id": None,
            "tmux_session": "proj",
        })

    T.sample(cfg, "proj")

    mem_sent = [t for _, t in fake_session["sent"] if "memory store" in t]
    assert len(mem_sent) == 1, f"one store, one alert — got {len(mem_sent)}"
    # and it says how many are reading it, instead of naming one of them
    assert "7 session(s) are reading it" in mem_sent[0]  # 6 devs + the operator


def test_memory_alert_does_not_refire_while_the_store_stays_over(tmp_path, fake_session):
    """Crossing-only + cooldown, held at STORE granularity — the state lives in
    _memory.json, so sessions appearing and being reaped can't re-arm it."""
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    mem = _big_index(f)
    T.sample(cfg, "proj")
    assert len([t for _, t in fake_session["sent"] if "memory store" in t]) == 1

    # store grows further, and a NEW session joins it — still no second alert
    (mem / "MEMORY.md").write_text("m" * 260_000)
    _write_transcript(fake_session["home"], "late-uuid", [_assistant((2, 1, 0), 1)])
    fake_session["rows"].append({
        "sid": "S-almdudleer-late-p9", "status": "active", "claude_uuid": "late-uuid",
        "linux_user": "almdudleer", "role": "dev", "task_id": None,
        "tmux_session": "proj",
    })
    T.sample(cfg, "proj")
    T.sample(cfg, "proj")
    assert len([t for _, t in fake_session["sent"] if "memory store" in t]) == 1


def test_memory_alert_state_is_keyed_by_store_not_by_session(tmp_path, fake_session):
    """The alert level is written per store path in _memory.json, and nothing
    memory-shaped is left decaying in the per-session records."""
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    mem = _big_index(f)
    T.sample(cfg, "proj")

    tdir = cfg.data_dir / "proj" / "_worker" / "telemetry"
    state = json.loads((tdir / "_memory.json").read_text())
    assert list(state) == [str(mem)]
    assert state[str(mem)]["level"] == "warn"
    assert "memory:warn" in state[str(mem)]["fired_at"]
    rec = json.loads((tdir / "S-almdudleer-dev-p5.json").read_text())
    assert "memory" not in rec["last_alert"]


def test_a_session_with_no_transcript_contributes_no_store(tmp_path, fake_session):
    """A session whose transcript wasn't found carries zeroes and an EMPTY path.
    That is an absence of a reading, not a store, and must not appear in the
    state file — a "" key there is an invented store that would then carry its
    own level and cooldown."""
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    mem = _big_index(f)
    fake_session["rows"].append({
        "sid": "S-almdudleer-ghost-p7", "status": "active", "claude_uuid": "no-such-uuid",
        "linux_user": "almdudleer", "role": "dev", "task_id": None,
        "tmux_session": "proj",
    })
    T.sample(cfg, "proj")
    T.sample(cfg, "proj")

    state = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                        / "_memory.json").read_text())
    assert list(state) == [str(mem)]  # exactly one store; no "" key
    assert state[str(mem)]["level"] == "warn"
    assert len([t for _, t in fake_session["sent"] if "memory store" in t]) == 1


def test_an_emptied_store_reads_none_not_its_old_level(tmp_path, fake_session):
    """Zero files at a REAL path is a reading of "nothing loaded", not a missing
    reading. It must drop a warned store straight to none.

    Distinct from the pathless case above, and it has to go warn → empty in one
    step: a draft of this walked warn → small → empty, and the small step had
    already set none, so a guard that skipped empty stores entirely stayed
    invisible.
    """
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    mem = _big_index(f)
    T.sample(cfg, "proj")
    spath = cfg.data_dir / "proj" / "_worker" / "telemetry" / "_memory.json"
    assert json.loads(spath.read_text())[str(mem)]["level"] == "warn"

    (mem / "MEMORY.md").unlink()  # store emptied outright, still at its path
    T.sample(cfg, "proj")
    state = json.loads(spath.read_text())
    assert str(mem) in state, "an emptied store is still a store"
    assert state[str(mem)]["level"] == "none"


def test_pruning_the_store_rearms_the_crossing(tmp_path, fake_session):
    """The signal must come BACK after a prune. An empty/shrunk store is a real
    reading of 'nothing loaded' — it drops the level to none, so the next real
    growth alerts again instead of being swallowed by a level that never reset.
    """
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    mem = _big_index(f)
    T.sample(cfg, "proj")
    assert len([t for _, t in fake_session["sent"] if "memory store" in t]) == 1

    spath = cfg.data_dir / "proj" / "_worker" / "telemetry" / "_memory.json"

    (mem / "MEMORY.md").write_text("m" * 100)  # pruned, well under the line
    T.sample(cfg, "proj")
    state = json.loads(spath.read_text())
    assert state[str(mem)]["level"] == "none"  # re-armed

    # It grows back — and the alert fires AGAIN, which is the whole point of
    # re-arming. Backdate the fire epoch past the cooldown rather than sleeping:
    # the crossing is what's under test here, the cooldown has its own test.
    state[str(mem)]["fired_at"]["memory:warn"] = 0
    spath.write_text(json.dumps(state))
    (mem / "MEMORY.md").write_text("m" * 200_000)
    T.sample(cfg, "proj")
    assert json.loads(spath.read_text())[str(mem)]["level"] == "warn"
    sent = [t for _, t in fake_session["sent"] if "memory store" in t]
    assert len(sent) == 2, "a real prune-then-regrow must alert again"


def test_alerts_never_reach_dead_operator_sessions(tmp_path, fake_session):
    """T-0834 defect 4, the MEASURED amplifier: the operator target list was
    built from every row, with no status filter, while the sampling loop three
    lines above it filtered status=='active'.

    Live measurement 2026-07-30, one alert: 42 inbox writes across 19 operator
    SIDs, 18 dead — and 24 of those dead inboxes redirect to the live operator's
    successor, so one alert landed 24 times in the one inbox a human reads.
    """
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session, "S-almdudleer-operator-live-p9", "active", "op-live")
    for i, status in enumerate(("suspended", "historical", "archived")):
        _add_operator_row(fake_session, f"S-almdudleer-operator-dead-p{i}", status,
                          f"op-dead-{i}")
    _write_transcript(fake_session["home"], "op-live", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    _big_index(f)
    T.sample(cfg, "proj")

    targets = {sid for sid, _ in fake_session["sent"]}
    assert "S-almdudleer-operator-live-p9" in targets
    assert not [s for s in targets if "dead" in s]


def test_memory_alert_names_the_store_not_a_session(tmp_path, fake_session):
    """T-0834 defect 2 + 1, in the text the operator actually reads: it names the
    store's path, labels the loaded figure as the loaded one, and labels the
    whole-store total as NOT loaded context."""
    cfg = _make_cfg(tmp_path)
    _add_operator_row(fake_session)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    mem = _big_index(f)
    (mem / "recall-one.md").write_text("r" * 40_000)
    T.sample(cfg, "proj")

    texts = [t for _, t in fake_session["sent"] if "memory store" in t]
    assert len(texts) == 1
    text = texts[0]
    assert str(mem) in text                       # the store is named as itself
    assert "S-almdudleer-dev-p5" not in text      # NOT attributed to a session
    assert "~50,000 tok" in text                  # the LOADED figure (200k // 4)
    assert "10,000 tok" in text                   # the recall-only figure, separate
    assert "60,000 tok" in text                   # the whole store, labelled as such
    assert "SHARED" in text and "NOT loaded context" in text
    # the old message's two wrong words are gone
    assert "footprint high" not in text


def test_read_telemetry_returns_sessions_and_quota_without_internal_fields(tmp_path, fake_session):
    cfg = _make_cfg(tmp_path)
    _write_transcript(fake_session["home"], fake_session["uuid"],
                      [_assistant((2, 70000, 100), 500)])
    T.sample(cfg, "proj")
    wire = T.read_telemetry(cfg, "proj")
    assert len(wire["sessions"]) == 1
    assert wire["sessions"][0]["sid"] == "S-almdudleer-dev-p5"
    # quota wire view strips rolling samples + alert bookkeeping
    assert "samples" not in wire["quota"]
    assert "last_alert" not in wire["quota"]
    assert "burn_tokens_per_hr" in wire["quota"]


def test_read_telemetry_includes_shared_memory_block(tmp_path, fake_session):
    """T-0502: read_telemetry surfaces the project-level shared memory count."""
    cfg = _make_cfg(tmp_path)
    mem = (tmp_path / "repo" / "memory")
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("m" * 400)
    wire = T.read_telemetry(cfg, "proj")
    sm = wire["shared_memory"]
    assert (sm["files"], sm["bytes"], sm["tokens_est"]) == (1, 400, 100)
    assert sm["loaded"] == {"files": 1, "bytes": 400, "tokens_est": 100}  # T-0834


def test_read_telemetry_includes_enforced_caps_block(tmp_path, fake_session):
    """T-0335 items 7 + 22: read_telemetry surfaces the ENFORCED caps
    utilization so the UI meter measures what spawn actually gates on."""
    cfg = _make_cfg(tmp_path)
    wire = T.read_telemetry(cfg, "proj")
    assert "caps" in wire
    caps = wire["caps"]
    for key in ("max_parallel_sessions", "effective_limit", "live_sessions",
                "max_total_tokens", "output_since_anchor"):
        assert key in caps
        assert isinstance(caps[key], int)


# ---------------------------------------------------------------------------
# T-0332: over-firing fixes — debounce, quiet-hours urgency, fresh-429 suppress
# ---------------------------------------------------------------------------

def test_cooldown_ok_pure(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_ALERT_COOLDOWN_SEC", raising=False)
    # absent key → always OK to fire
    assert T.cooldown_ok({}, "context:warn", now=1000.0) is True
    # within the window → suppressed
    fired = {"context:warn": 1000.0}
    assert T.cooldown_ok(fired, "context:warn", now=1000.0 + 10, window=3600) is False
    # past the window → OK again
    assert T.cooldown_ok(fired, "context:warn", now=1000.0 + 3601, window=3600) is True
    # a DIFFERENT key (escalation to urgent) is never gated by warn's timer
    assert T.cooldown_ok(fired, "context:urgent", now=1000.0 + 10, window=3600) is True


def test_alert_urgent_severity_mapping():
    # only genuine human-DECISION alerts bypass the quiet-hours gate
    assert T.alert_urgent("quota", "urgent") is True
    assert T.alert_urgent("throttle", "urgent") is True
    # context never alerts a human (auto-compacts); memory is non-urgent
    assert T.alert_urgent("context", "urgent") is False
    assert T.alert_urgent("memory", "warn") is False


def _capture_human_tg(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        T, "_human_tg",
        lambda cfg, slug, text, urgent=True: calls.append({"text": text, "urgent": urgent}),
    )
    return calls


def test_context_never_pings_the_human(tmp_path, fake_session, monkeypatch):
    """T-0333: context crossings are self-healing — the human is NEVER pinged
    about context (warn OR urgent). The system auto-compacts instead."""
    cfg = _make_cfg(tmp_path)
    calls = _capture_human_tg(monkeypatch)
    # auto-compact is stubbed (no tmux) — we only assert the human isn't pinged
    monkeypatch.setattr(T.autocompact, "maybe_compact", lambda *a, **k: False)
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    T.sample(cfg, "proj")
    with f.open("a") as fh:
        fh.write(_assistant((2, 580000, 100), 100) + "\n")  # warn
    T.sample(cfg, "proj")
    with f.open("a") as fh:
        fh.write(_assistant((2, 710000, 100), 100) + "\n")  # urgent
    T.sample(cfg, "proj")
    assert not [c for c in calls if "context" in c["text"].lower()
                or "COMPACT NOW" in c["text"]]


def test_urgent_context_triggers_autocompact_not_a_human_ping(tmp_path, fake_session, monkeypatch):
    """The over-ceiling session is auto-compacted (closed loop), not handed to a human."""
    cfg = _make_cfg(tmp_path)
    calls = _capture_human_tg(monkeypatch)
    compacted: list[tuple] = []
    monkeypatch.setattr(
        T.autocompact, "maybe_compact",
        lambda cfg, slug, rec, level, now: compacted.append((rec["sid"], level)) or False,
    )
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    T.sample(cfg, "proj")
    with f.open("a") as fh:
        fh.write(_assistant((2, 710000, 100), 100) + "\n")  # urgent (>=700k)
    T.sample(cfg, "proj")
    assert ("S-almdudleer-dev-p5", "urgent") in compacted
    assert calls == []  # human got nothing


def test_memory_alert_does_not_ping_the_human(tmp_path, fake_session, monkeypatch):
    """T-0387: the memory advisory is a ROUTINE resource alert — it must NOT ping
    the human (same closed-loop rule as T-0333 context). It stays an internal
    operator advisory: the human channel is silent, the operator still gets it.
    T-0834 narrowed the addressee to the operator and kept this rule."""
    cfg = _make_cfg(tmp_path)
    calls = _capture_human_tg(monkeypatch)
    _add_operator_row(fake_session)
    import bot_squad_worker.teams as _teams
    monkeypatch.setattr(_teams, "tl_for_sid",
                        lambda cfg, slug, sid: "S-almdudleer-TL-p9")
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    _big_index(f)  # over the loaded warn line
    T.sample(cfg, "proj")
    # The human is NOT pinged about a routine memory advisory:
    assert [c for c in calls if "memory store" in c["text"].lower()] == []
    # ...but the internal operator advisory is still delivered:
    assert any("memory store" in text for _, text in fake_session["sent"])


def test_fresh_tail_read_does_not_redetect_429(tmp_path, fake_session, monkeypatch):
    """A 429 already in the tail at first-sample (e.g. after a compact spawns a new
    transcript) must NOT be re-counted — only 429s on the incremental read are new."""
    cfg = _make_cfg(tmp_path)
    _capture_human_tg(monkeypatch)
    rl_line = json.dumps({"error": "rate_limit", "apiErrorStatus": 429})
    _write_transcript(fake_session["home"], fake_session["uuid"],
                      [_assistant((2, 70000, 100), 500), rl_line])
    T.sample(cfg, "proj")  # fresh tail-read sees the 429 but must not count it
    rec = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                      / "S-almdudleer-dev-p5.json").read_text())
    assert rec["rate_limited"] is False
    quota = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                        / "_quota.json").read_text())
    assert quota["rate_limit_429"]["count"] == 0


def test_quota_sampler_write_preserves_alert_fired_at_no_429_spam(
    tmp_path, fake_session, monkeypatch,
):
    """T-0619: the sampler's ``_update_quota`` write must preserve
    ``alert_fired_at`` exactly like ``last_alert`` — it is the dict
    ``cooldown_ok`` uses to gate the 3h "RATE LIMITED" cooldown. Before the
    fix, every quota-write tick (incl. ticks with no new 429) dropped
    ``alert_fired_at``, so a second genuine 429 arriving within the cooldown
    window re-fired the urgent TG alert (74 pings on 2026-07-05).

    Sequence: baseline sample (no 429) -> first 429 (fires ONE alert) -> a
    plain sampler tick with NO new 429 (the "write in between" that used to
    clobber the cooldown state) -> a second 429 still inside the cooldown
    window. Exactly one alert must fire across the whole sequence.
    """
    cfg = _make_cfg(tmp_path)
    calls = _capture_human_tg(monkeypatch)
    # Distinct sampled_at/last_429_at per call so the "new 429" check
    # (`rl.last_at != last_seen`) sees a genuinely fresh timestamp each time —
    # real wall-clock precision (1s) is too coarse for a fast test.
    iso_values = iter(f"2026-07-05T20:{i:02d}:00Z" for i in range(60))
    monkeypatch.setattr(T, "_now_iso", lambda: next(iso_values))

    lines = [_assistant((2, 70000, 100), 500)]
    _write_transcript(fake_session["home"], fake_session["uuid"], lines)
    T.sample(cfg, "proj")  # baseline tail-read, no 429 yet
    assert calls == []

    rl_line = json.dumps({"error": "rate_limit", "apiErrorStatus": 429})
    lines.append(rl_line)
    _write_transcript(fake_session["home"], fake_session["uuid"], lines)
    T.sample(cfg, "proj")  # first real (incremental) 429 -> fires the alert
    rate_limited_calls = [c for c in calls if "RATE LIMITED" in c["text"]]
    assert len(rate_limited_calls) == 1

    T.sample(cfg, "proj")  # plain sampler tick, no new 429 in between

    lines.append(rl_line)
    _write_transcript(fake_session["home"], fake_session["uuid"], lines)
    T.sample(cfg, "proj")  # second real 429, still inside the 3h cooldown

    rate_limited_calls = [c for c in calls if "RATE LIMITED" in c["text"]]
    assert len(rate_limited_calls) == 1, (
        "cooldown must suppress the second 429 alert; got "
        f"{len(rate_limited_calls)} RATE LIMITED pings"
    )
    quota = json.loads((cfg.data_dir / "proj" / "_worker" / "telemetry"
                        / "_quota.json").read_text())
    assert quota["alert_fired_at"].get("throttle:urgent") is not None


def test_human_tg_routes_tg_primary_single_delivery(tmp_path, monkeypatch):
    """T-0394 → T-0610 inversion: telemetry human pages route via the
    _send_stakeholder_dm SSOT — TG-primary into #team-queries, ONE delivery,
    MAX reserve-only."""
    import dataclasses, types
    from bot_squad_worker import actions as A, tg_topics
    cfg = dataclasses.replace(_make_cfg(tmp_path), max_default_chat_id="MAXID", max_recipient_kind="chat_id")
    tg_topics.save(cfg, "proj", {"team_queries": 555})
    max_calls, tg_calls = [], []
    monkeypatch.setattr(A, "_MAX", types.SimpleNamespace(send=lambda **k: (max_calls.append(k) or True)))
    monkeypatch.setattr(A, "_TG", types.SimpleNamespace(send=lambda **k: (tg_calls.append(k) or True)))
    T._human_tg(cfg, "proj", "quota almost out", urgent=True)
    assert len(tg_calls) == 1 and tg_calls[0]["topic_id"] == 555
    assert max_calls == []  # one page = one delivery (T-0610)


# ── T-0447 (#4): cascade-reap the per-SID telemetry sample on archive ─────────

def test_reap_record_removes_sample(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = T._record_path(cfg, "proj", "S-x")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    out = T.reap_record(cfg, "proj", "S-x")
    assert out == p
    assert not p.exists()


def test_reap_record_missing_is_none(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert T.reap_record(cfg, "proj", "S-ghost") is None


def test_reap_record_empty_sid_is_none(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert T.reap_record(cfg, "proj", "") is None


def test_read_telemetry_includes_lifecycle_block(tmp_path, fake_session):
    """T-0470: read_telemetry surfaces the hook-driven lifecycle event summary
    (stall/timeout/recycle), filtered to genuinely-live sessions."""
    from bot_squad_worker import lifecycle_events as LE
    cfg = _make_cfg(tmp_path)
    LE.emit(cfg, "proj", "S-almdudleer-dev-p5", LE.SESSION_TIMEOUT, now=1.0)
    LE.emit(cfg, "proj", "S-almdudleer-dev-p5", LE.SESSION_RECYCLED, now=2.0)
    LE.emit(cfg, "proj", "S-ghost-dead-p9", LE.SESSION_RECYCLED, now=3.0)  # not live
    wire = T.read_telemetry(cfg, "proj")
    assert "lifecycle" in wire
    assert set(wire["lifecycle"]) == {"S-almdudleer-dev-p5"}  # ghost filtered out
    lc = wire["lifecycle"]["S-almdudleer-dev-p5"]
    assert lc["counts"] == {LE.SESSION_TIMEOUT: 1, LE.SESSION_RECYCLED: 1}
