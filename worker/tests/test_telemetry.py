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


def test_context_and_memory_levels():
    assert T.context_level(399_999) == "none"
    assert T.context_level(400_000) == "warn"
    assert T.context_level(499_999) == "warn"
    assert T.context_level(500_000) == "urgent"
    assert T.memory_level(T.MEMORY_WARN_TOKENS - 1) == "none"
    assert T.memory_level(T.MEMORY_WARN_TOKENS) == "warn"


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


def test_memory_stats_missing_dir_is_zero(tmp_path: Path):
    assert T.memory_stats(tmp_path / "nope") == {"files": 0, "bytes": 0, "tokens_est": 0}


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


def test_context_alert_fires_once_per_crossing(tmp_path, fake_session):
    cfg = _make_cfg(tmp_path)
    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])  # ~70k → none
    T.sample(cfg, "proj")
    assert fake_session["sent"] == []  # no crossing yet

    # cross into warn (>400k)
    with f.open("a") as fh:
        fh.write(_assistant((2, 420000, 100), 100) + "\n")
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
        fh.write(_assistant((2, 430000, 100), 100) + "\n")
    T.sample(cfg, "proj")
    assert len(fake_session["sent"]) == before


def test_alerts_are_targeted_to_operator_and_tl_never_broadcast(tmp_path, fake_session, monkeypatch):
    """A context crossing pings the specific operator SID + the session's TL SID."""
    cfg = _make_cfg(tmp_path)
    # add an operator row + a TL for the dev
    fake_session["rows"].append({
        "sid": "S-almdudleer-operator-p1", "status": "active",
        "claude_uuid": "op-uuid", "linux_user": "almdudleer",
        "role": "operator", "task_id": None, "tmux_session": "proj",
    })
    import bot_squad_worker.teams as _teams
    monkeypatch.setattr(_teams, "tl_for_sid",
                        lambda cfg, slug, sid: "S-almdudleer-TL-p9")
    # operator's transcript so its own sampling doesn't crash (optional)
    _write_transcript(fake_session["home"], "op-uuid", [_assistant((1, 1, 0), 1)])

    f = _write_transcript(fake_session["home"], fake_session["uuid"],
                          [_assistant((2, 70000, 100), 500)])
    T.sample(cfg, "proj")
    with f.open("a") as fh:
        fh.write(_assistant((2, 510000, 100), 100) + "\n")  # urgent crossing
    T.sample(cfg, "proj")

    targets = {sid for sid, _ in fake_session["sent"]}
    assert "S-almdudleer-operator-p1" in targets   # operator SID targeted
    assert "S-almdudleer-TL-p9" in targets         # session's TL targeted
    assert "S-almdudleer-dev-p5" not in targets    # affected session not self-pinged
    assert "all" not in targets and "teamlead" not in targets  # never a role/broadcast
    assert any("COMPACT NOW" in text for _, text in fake_session["sent"])


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
