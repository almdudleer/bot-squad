"""T-0589: backlog visibility in the TG chat — digest + lifecycle notifications.

Automated AFTER the manual walkthrough (scenarios/T-0589): sandbox worker +
recorder driver observed the real bsq→socket→task_digest round-trip, the
baseline/transition/batch/dedupe sweep behavior, the quiet-hours defer against
the REAL 17-05 UTC window, and the thread append. These tests pin what was
seen live.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_squad_worker import task_chat
from bot_squad_worker.config import Config


def _write_task(backlog: Path, task_id: str, *, title: str = "",
                status: str = "open", priority: str = "",
                provenance: str = "stakeholder:2026-07-05") -> Path:
    lines = [
        "---",
        f"id: {task_id}",
        f'title: "{title or f"Task {task_id}"}"',
        f"status: {status}",
        "created: 2026-07-05T10:00:00Z",
        f"provenance: {provenance}",
    ]
    if priority:
        lines.append(f'priority: "{priority}"')
    lines += ["---", "", "## Verbatim request", "x", ""]
    p = backlog / f"{task_id}-stub.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _flip(backlog: Path, task_id: str, new_status: str) -> None:
    p = backlog / f"{task_id}-stub.md"
    out = [
        f"status: {new_status}" if line.startswith("status:") else line
        for line in p.read_text(encoding="utf-8").splitlines()
    ]
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


@pytest.fixture
def cfg(tmp_config_dir: Path) -> Config:
    return Config.load(tmp_config_dir)


@pytest.fixture
def backlog(cfg: Config) -> Path:
    d = cfg.data_dir / "test-project" / "backlog"
    d.mkdir(parents=True)
    return d


class _RecorderDm:
    """Stands in for actions._send_stakeholder_dm: records every page and
    answers with a scripted delivery outcome (True → {ok, sent:True};
    False → the quiet-hours shape {ok:True, sent:False})."""

    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.calls: list[dict] = []

    def __call__(self, cfg, *, message, urgent=False, tg_chat_id="", **kw):
        # `slug` is captured (T-0758 follow-up) because it is what makes the
        # transport stamp the project onto the notice — see the sender-tag test
        # below. Everything else still lands in `kw` untouched.
        self.calls.append({"message": message, "urgent": urgent,
                           "tg_chat_id": tg_chat_id, "slug": kw.get("slug", ""),
                           "kw": kw})
        return {"ok": True, "sent": self.deliver,
                "channel": "tg" if self.deliver else "tg"}


@pytest.fixture
def dm(monkeypatch) -> _RecorderDm:
    rec = _RecorderDm()
    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "_send_stakeholder_dm", rec)
    # Thread append is exercised separately; keep the sweep hermetic here.
    monkeypatch.setattr(task_chat, "_append_thread", lambda *a, **k: False)
    return rec


# ---------------------------------------------------------------------------
# Digest composition
# ---------------------------------------------------------------------------


def test_digest_counts_and_headlines_ordering(cfg, backlog):
    _write_task(backlog, "T-0001", priority="P1", status="open")
    _write_task(backlog, "T-0002", priority="P2", status="in_progress")
    _write_task(backlog, "T-0003", priority="P1", status="totest")
    _write_task(backlog, "T-0004", status="planned")
    _write_task(backlog, "T-0005", priority="P2", status="closed")
    out = task_chat.compose_digest(cfg, "test-project")
    head, *lines = out["text"].splitlines()
    assert head.startswith("Бэклог test-project: ")
    # canonical order, only non-zero statuses, closed in the tail parens
    assert "1 in_progress · 1 totest · 1 open · 1 planned (1 closed)" in head
    # in-flight first, then P-rank; closed NEVER a headline
    assert [ln.split()[1] for ln in lines] == ["T-0002", "T-0003", "T-0001"]
    assert out["counts"]["closed"] == 1


def test_digest_headline_cap_with_explicit_overflow(cfg, backlog):
    for i in range(1, 10):
        _write_task(backlog, f"T-{i:04d}", priority="P2", status="open")
    lines = task_chat.compose_digest(cfg, "test-project")["text"].splitlines()
    # 1 counts line + 6 headlines + explicit "+N ещё" (no silent truncation)
    assert len(lines) == 8
    assert lines[-1].startswith("+3 ещё")


def test_digest_cuts_long_titles(cfg, backlog):
    _write_task(backlog, "T-0001", priority="P1", title="t" * 100)
    line = task_chat.compose_digest(cfg, "test-project")["text"].splitlines()[1]
    assert "…" in line and len(line) < 90


def test_digest_empty_backlog_is_sane(cfg):
    out = task_chat.compose_digest(cfg, "test-project")  # dir doesn't even exist
    assert out["text"] == "Бэклог test-project: пусто"
    assert out["counts"] == {}


def test_digest_survives_corrupt_md(cfg, backlog):
    (backlog / "T-9999-broken.md").write_text("status: no-frontmatter-fence")
    _write_task(backlog, "T-0001", priority="P1")
    out = task_chat.compose_digest(cfg, "test-project")
    assert "T-0001" in out["text"]


# ---------------------------------------------------------------------------
# task_digest action (allowlist round-trip)
# ---------------------------------------------------------------------------


def test_task_digest_action_round_trip(cfg, backlog, monkeypatch):
    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    _write_task(backlog, "T-0001", priority="P1")
    out = A.dispatch("task_digest", {"slug": "test-project"})
    assert out["ok"] is True and "T-0001" in out["text"] and out["counts"]["open"] == 1


def test_task_digest_action_validation(cfg, monkeypatch):
    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(A.ActionError, match="unknown project slug"):
        A.dispatch("task_digest", {"slug": "nope"})
    with pytest.raises(A.ActionError, match="unexpected params"):
        A.dispatch("task_digest", {"slug": "test-project", "bogus": 1})
    with pytest.raises(A.ActionError, match="missing required param"):
        A.dispatch("task_digest", {})


def test_task_digest_registered_coordinator_only():
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY
    assert "task_digest" in ACTION_REGISTRY
    assert ACTION_MODES["task_digest"] == "coordinator_only"


# ---------------------------------------------------------------------------
# Lifecycle sweep
# ---------------------------------------------------------------------------


def test_first_sight_baselines_silently(cfg, backlog, dm):
    _write_task(backlog, "T-0001", status="in_progress")
    _write_task(backlog, "T-0002", status="closed")
    out = task_chat.lifecycle_tick_one(cfg, "test-project")
    # even "interesting" statuses seen for the first time are NOT announced —
    # deploying this feature must not blast the existing backlog history.
    assert out["transitions"] == 0 and dm.calls == []
    assert task_chat._read_sidecar(cfg, "test-project") == {
        "T-0001": "in_progress", "T-0002": "closed"}


def test_transition_notifies_once_and_stamps(cfg, backlog, dm):
    _write_task(backlog, "T-0001", title="Voice ask")
    task_chat.lifecycle_tick_one(cfg, "test-project")  # baseline
    _flip(backlog, "T-0001", "in_progress")
    out = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out["transitions"] == 1 and out["delivered"] is True
    assert len(dm.calls) == 1
    msg = dm.calls[0]["message"]
    assert "T-0001" in msg and "in_progress" in msg and "Voice ask" in msg
    assert dm.calls[0]["urgent"] is False  # quiet-hours-gated class, by design
    # dedupe: unchanged status never re-fires
    out2 = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out2["transitions"] == 0 and len(dm.calls) == 1


def test_lifecycle_notice_declares_its_project(cfg, backlog, dm):
    """T-0758 follow-up. The notice used to page with no `slug`, so the
    transport had nothing to build a sender tag from and it went out bare —
    and `📋 T-0001 → in_progress` does not say WHICH project, because ticket
    ids are not unique across them (measured: 189 of watchrobot's 192 ids also
    exist in bot-squad). The tag is still stamped at the transport; this pins
    only that the caller states the identity the transport needs."""
    _write_task(backlog, "T-0001", title="Voice ask")
    task_chat.lifecycle_tick_one(cfg, "test-project")  # baseline
    _flip(backlog, "T-0001", "in_progress")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    assert dm.calls[0]["slug"] == "test-project"
    # And it stays a RECORD opt-out: `_append_thread` already writes this exact
    # text into the same thread, so the transport must not mirror it twice.
    assert dm.calls[0]["kw"]["record_outbound"] is False


def test_non_stakeholder_provenance_ignored(cfg, backlog, dm):
    _write_task(backlog, "T-0001", provenance="corpus:process-paradigm")
    _write_task(backlog, "T-0002", provenance="T-0587")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    _flip(backlog, "T-0001", "closed")
    _flip(backlog, "T-0002", "in_progress")
    out = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out["transitions"] == 0 and dm.calls == []
    assert task_chat._read_sidecar(cfg, "test-project") == {}


def test_mixed_provenance_counts_as_stakeholder(cfg, backlog, dm):
    _write_task(backlog, "T-0001", provenance="T-0587, stakeholder:2026-07-04")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    _flip(backlog, "T-0001", "totest")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    assert len(dm.calls) == 1 and "totest" in dm.calls[0]["message"]


def test_batch_two_transitions_one_message(cfg, backlog, dm):
    _write_task(backlog, "T-0001")
    _write_task(backlog, "T-0002")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    _flip(backlog, "T-0001", "totest")
    _flip(backlog, "T-0002", "closed")
    out = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out["transitions"] == 2
    assert len(dm.calls) == 1  # digest-style: ONE message
    msg = dm.calls[0]["message"]
    assert "T-0001" in msg and "T-0002" in msg and msg.count("\n") == 1


def test_uninteresting_transitions_update_state_silently(cfg, backlog, dm):
    _write_task(backlog, "T-0001", status="planned")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    _flip(backlog, "T-0001", "open")  # planned→open: not in NOTIFY_STATUSES
    out = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out["transitions"] == 0 and dm.calls == []
    assert task_chat._read_sidecar(cfg, "test-project")["T-0001"] == "open"
    # ...so the NEXT interesting transition still fires exactly once
    _flip(backlog, "T-0001", "in_progress")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    assert len(dm.calls) == 1


def test_undelivered_send_defers_not_drops(cfg, backlog, dm):
    """The quiet-hours contract (walked live against the real 17-05 window):
    a suppressed send leaves the sidecar UNSTAMPED, so the same transition
    retries every tick and lands on the first deliverable one."""
    _write_task(backlog, "T-0001")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    _flip(backlog, "T-0001", "closed")
    dm.deliver = False  # quiet hours: {ok: True, sent: False}
    out = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out["transitions"] == 1 and out["delivered"] is False
    assert task_chat._read_sidecar(cfg, "test-project")["T-0001"] == "open"
    out2 = task_chat.lifecycle_tick_one(cfg, "test-project")  # still quiet
    assert out2["transitions"] == 1 and len(dm.calls) == 2
    dm.deliver = True  # quiet hours end
    out3 = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out3["delivered"] is True
    assert task_chat._read_sidecar(cfg, "test-project")["T-0001"] == "closed"
    # delivered → done; no fourth send
    task_chat.lifecycle_tick_one(cfg, "test-project")
    assert len(dm.calls) == 3


def test_notify_failure_never_raises_out(cfg, backlog, monkeypatch):
    """A channel outage inside _send_stakeholder_dm must neither kill the
    sweep nor stamp the sidecar (the transition retries)."""
    import bot_squad_worker.actions as A

    def _boom(*a, **k):
        raise RuntimeError("transport down")

    monkeypatch.setattr(A, "_send_stakeholder_dm", _boom)
    monkeypatch.setattr(task_chat, "_append_thread", lambda *a, **k: False)
    _write_task(backlog, "T-0001")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    _flip(backlog, "T-0001", "closed")
    out = task_chat.lifecycle_tick_one(cfg, "test-project")
    assert out["delivered"] is False
    assert task_chat._read_sidecar(cfg, "test-project")["T-0001"] == "open"


def test_vanished_task_dropped_silently(cfg, backlog, dm):
    p = _write_task(backlog, "T-0001")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    p.unlink()  # gc-archived / renamed
    task_chat.lifecycle_tick_one(cfg, "test-project")
    assert dm.calls == []
    assert task_chat._read_sidecar(cfg, "test-project") == {}


def test_corrupt_sidecar_rebaselines_without_blast(cfg, backlog, dm):
    _write_task(backlog, "T-0001", status="totest")
    task_chat._sidecar_path(cfg, "test-project").parent.mkdir(
        parents=True, exist_ok=True)
    task_chat._sidecar_path(cfg, "test-project").write_text("{not json")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    assert dm.calls == []  # worst case = silent re-baseline, never a blast
    assert task_chat._read_sidecar(cfg, "test-project") == {"T-0001": "totest"}


def test_kill_switch_disables_sweep(cfg, backlog, dm, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_TASK_NOTIFY", "0")
    _write_task(backlog, "T-0001")
    out = task_chat.lifecycle_tick(cfg)
    assert out == {"ok": True, "disabled": True}
    assert not task_chat._sidecar_path(cfg, "test-project").exists()


def test_lifecycle_tick_covers_all_projects_and_contains_errors(cfg, backlog, dm, monkeypatch):
    _write_task(backlog, "T-0001")
    calls = []
    real = task_chat.lifecycle_tick_one

    def _one(cfg_, slug):
        calls.append(slug)
        raise RuntimeError("one bad project")

    monkeypatch.setattr(task_chat, "lifecycle_tick_one", _one)
    out = task_chat.lifecycle_tick(cfg)
    assert out["ok"] is True and calls == ["test-project"]
    monkeypatch.setattr(task_chat, "lifecycle_tick_one", real)


def test_scheduler_has_task_lifecycle_job(cfg):
    from bot_squad_worker.scheduler import build_scheduler
    # build_scheduler builds but does not start; just assert the job is wired.
    sched = build_scheduler(cfg)
    assert "task_lifecycle" in {j.id for j in sched.get_jobs()}


# ---------------------------------------------------------------------------
# Thread append (delivered lines land in the stakeholder's conversation)
# ---------------------------------------------------------------------------


def test_thread_gid_resolves_via_users_store(cfg, monkeypatch):
    m = cfg.data_dir / "_mothership"
    m.mkdir(parents=True, exist_ok=True)
    (m / "users.json").write_text(json.dumps({"users": [
        {"id": "gu_other", "tg_user_id": "111"},
        {"id": "gu_stake", "tg_user_id": "0"},  # test-project tg_chat == "0"
    ]}))
    assert task_chat._thread_gid(cfg, "test-project") == "gu_stake"


def test_thread_gid_empty_when_unresolvable(cfg):
    assert task_chat._thread_gid(cfg, "test-project") == ""  # no users.json
    assert task_chat._thread_gid(cfg, "unknown-slug") == ""


def test_append_thread_posts_system_author(cfg, monkeypatch):
    monkeypatch.setattr(task_chat, "_thread_gid", lambda c, s: "gu_stake")
    monkeypatch.setenv("WORKER_API_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("WORKER_API_TOKEN", "tok")
    posted = {}

    import httpx

    def _post(url, json=None, headers=None, timeout=None):
        posted.update({"url": url, "json": json, "headers": headers})

        class R:
            def raise_for_status(self):
                pass
        return R()

    monkeypatch.setattr(httpx, "post", _post)
    assert task_chat._append_thread(cfg, "test-project", "line") is True
    assert posted["url"].endswith(
        "/api/m/worker/conversations/test-project/gu_stake/messages")
    # NOT session:* — the relay must not re-deliver what tg already carried.
    assert posted["json"]["author"] == "system:task-lifecycle"
    assert posted["headers"]["Authorization"] == "Bearer tok"


def test_append_thread_noop_without_linkage(cfg, monkeypatch):
    monkeypatch.delenv("WORKER_API_BASE_URL", raising=False)
    monkeypatch.delenv("MOTHERSHIP_BASE_URL", raising=False)
    monkeypatch.delenv("WORKER_API_TOKEN", raising=False)
    assert task_chat._append_thread(cfg, "test-project", "line") is False


def test_delivered_transition_appends_thread(cfg, backlog, monkeypatch):
    import bot_squad_worker.actions as A
    monkeypatch.setattr(
        A, "_send_stakeholder_dm",
        lambda *a, **k: {"ok": True, "sent": True, "channel": "tg"})
    appended = []
    monkeypatch.setattr(
        task_chat, "_append_thread",
        lambda c, s, text: appended.append(text) or True)
    _write_task(backlog, "T-0001")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    _flip(backlog, "T-0001", "totest")
    task_chat.lifecycle_tick_one(cfg, "test-project")
    assert len(appended) == 1 and "T-0001" in appended[0]
