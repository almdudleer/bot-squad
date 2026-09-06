"""T-0958 — the compact trigger's own instrument must MOVE.

The T-0960 fix (``sessions._proc_uuid_is_resume_only`` + the md-vs-proc
mismatch check in ``list_sessions``) is guarded end-to-end by
``test_t0960_resume_uuid.py`` — but every assertion there stops at
``list_sessions``'s output field: it proves the RESOLVED uuid is correct, not
that a squeezed session's reading actually comes back lower. Separately,
``test_telemetry.py``'s T-0908 regression
(``test_sample_after_idle_compact_reads_post_window_not_stale_pre_compact``)
proves the opposite half — GIVEN the correct uuid, a post-compact read is not
stale — but its ``fake_session`` fixture monkeypatches ``list_sessions``
wholesale to a hardcoded row, so it never exercises real uuid resolution.

Neither test alone catches a regression in the WIRING between them: if
``telemetry.sample`` ever stopped trusting ``list_sessions``'s resolved
``claude_uuid`` — or if the resolution regressed by some other route than the
one T-0960 fixed — both of those suites would stay green while the number
above idle_timeout's ceiling gates froze again, exactly as it did for
``S-almdudleer-operator-p513`` in the original report (T-0958).

This test drives the REAL, un-mocked ``sessions.list_sessions`` (a --resume'd
pane, cmdline mismatched against the md, same shape as the live bug) straight
into the REAL ``telemetry.sample``, then squeezes the transcript with a
``/compact`` boundary and asserts the reading idle_timeout's own
``_context_tokens`` seam reads is LOWER afterward. If either half regresses —
resolution or wiring — this is the test that goes red.
"""

from __future__ import annotations

import subprocess

from bot_squad_worker import idle_timeout
from bot_squad_worker import telemetry as T
from bot_squad_worker.sessions import _write_session_metadata
from tests.test_sessions import _make_cfg
from tests.test_telemetry import _assistant, _boundary, _write_transcript

ANCESTOR = "29018a66-4948-4f2b-9d1b-6e087ae5140e"   # what --resume names
LIVE = "0c671a5d-6a8d-43d2-b3cb-1030196a02c3"        # the transcript actually written


def test_a_resumed_sessions_context_reading_drops_after_a_real_compact(
    tmp_path, monkeypatch,
):
    import bot_squad_worker.sessions as S

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    cfg.config_dir = tmp_path / "config"  # telemetry's quota-anchor read needs it
    sid = "S-testuser-operator-p513"

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sessions_dir / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "operator", "cwd": str(repo),
        "claude_uuid": LIVE, "task_id": "~",
        "started_at": "2026-09-03T10:00:00Z",
    })

    fake_panes = f"%513|operator|3001|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_panes, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    # a --resume'd pane: /proc reports the ANCESTOR uuid, exactly the shape
    # measured live on S-almdudleer-operator-p513
    monkeypatch.setattr(S, "_pane_claude_uuid_from_proc",
                        lambda pid, home, children=None: ANCESTOR)
    monkeypatch.setattr(S, "_proc_children_map", lambda: {3001: []})
    monkeypatch.setattr(S, "_proc_cmdline", lambda pid: [
        "claude", "--dangerously-skip-permissions", "--resume", ANCESTOR,
    ])

    # the transcript that is ACTUALLY being written — under LIVE, never ANCESTOR
    f = _write_transcript(tmp_path, LIVE, [_assistant((95_498, 0, 0), 20)])

    out = T.sample(cfg, "test-project")
    assert out["sampled"] == 1, "list_sessions must resolve LIVE, not ANCESTOR, or nothing is sampled"
    before = idle_timeout._context_tokens(cfg, "test-project", sid)
    assert before == 95_498, (
        "the seam idle_timeout reads must see the pre-compact window, proving "
        "it followed the md-resolved uuid (T-0960) all the way through sample()"
    )

    # the session idles, gets compacted, and (T-0908 shape) nothing runs after
    with f.open("a") as fh:
        fh.write(_boundary(11_015) + "\n")
    T.sample(cfg, "test-project")
    after = idle_timeout._context_tokens(cfg, "test-project", sid)

    assert after == 11_015, (
        "a /compact must move the reading idle_timeout's ceiling triggers "
        "read — this is the exact live bug: the number held at 410082 across "
        "two real compacts because a --resume mismatch tailed a dead file"
    )
    assert after < before
