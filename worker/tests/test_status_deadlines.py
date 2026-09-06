"""T-0950: time-in-state deadlines for blocked_on_user/to_accept/totest.

Mirrors the ``task_chat`` lifecycle-notify test harness (same sidecar-dedupe,
same quiet-hours-defers-not-drops contract), since ``status_deadlines`` reuses
that SSOT (``actions._send_stakeholder_dm``) and that dedup shape."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import status_deadlines
from bot_squad_worker.config import Config


def _write_task(backlog: Path, task_id: str, *, title: str = "",
                 status: str = "blocked_on_user",
                 status_since: str = "", updated: str = "",
                 created: str = "2026-08-01T00:00:00Z") -> Path:
    lines = [
        "---",
        f"id: {task_id}",
        f'title: "{title or f"Task {task_id}"}"',
        f"status: {status}",
        f"created: {created}",
    ]
    if updated:
        lines.append(f"updated: {updated}")
    if status_since:
        lines.append(f"status_since: {status_since}")
    lines += ["---", "", "## Verbatim request", "x", ""]
    p = backlog / f"{task_id}-stub.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


@pytest.fixture
def cfg(tmp_config_dir: Path) -> Config:
    return Config.load(tmp_config_dir)


@pytest.fixture
def backlog(cfg: Config) -> Path:
    d = cfg.data_dir / "test-project" / "backlog"
    d.mkdir(parents=True)
    return d


class _RecorderDm:
    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.calls: list[dict] = []

    def __call__(self, cfg, *, message, urgent=False, tg_chat_id="", **kw):
        self.calls.append({"message": message, "urgent": urgent,
                            "tg_chat_id": tg_chat_id, "slug": kw.get("slug", ""),
                            "kw": kw})
        return {"ok": True, "sent": self.deliver}


@pytest.fixture
def dm(monkeypatch) -> _RecorderDm:
    rec = _RecorderDm()
    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "_send_stakeholder_dm", rec)
    return rec


# ---------------------------------------------------------------------------
# deadline_sec — the per-status policy + env override
# ---------------------------------------------------------------------------


def test_deadline_sec_defaults():
    assert status_deadlines.deadline_sec("blocked_on_user") == 24 * 3600
    assert status_deadlines.deadline_sec("to_accept") == 24 * 3600
    assert status_deadlines.deadline_sec("totest") == 72 * 3600


def test_deadline_sec_not_a_gated_status_is_none():
    assert status_deadlines.deadline_sec("paused") is None
    assert status_deadlines.deadline_sec("in_progress") is None


def test_deadline_sec_env_override(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_DEADLINE_BLOCKED_ON_USER_SEC", "60")
    assert status_deadlines.deadline_sec("blocked_on_user") == 60
    # garbage/non-positive falls back to the default
    monkeypatch.setenv("BOT_SQUAD_DEADLINE_BLOCKED_ON_USER_SEC", "-5")
    assert status_deadlines.deadline_sec("blocked_on_user") == 24 * 3600


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_breach_past_deadline_alerts(cfg, backlog, dm):
    since = "2026-08-01T00:00:00Z"
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    now = status_deadlines._parse_iso(since) + 25 * 3600  # 1h past the 24h deadline
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=now)
    assert out["breaches"] == 1 and out["delivered"] is True
    msg = dm.calls[0]["message"]
    assert "T-0001" in msg and "blocked_on_user" in msg
    assert dm.calls[0]["urgent"] is True
    assert dm.calls[0]["kw"]["msg_type"] == "ticket_deadline"


def test_not_yet_past_deadline_is_silent(cfg, backlog, dm):
    since = "2026-08-01T00:00:00Z"
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    now = status_deadlines._parse_iso(since) + 3600  # only 1h in
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=now)
    assert out["breaches"] == 0 and dm.calls == []


def test_gated_statuses_have_different_deadlines(cfg, backlog, dm):
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="totest", status_since=since)
    # 25h past `since`: breaches blocked_on_user/to_accept's 24h, NOT totest's 72h
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 25 * 3600)
    assert out["breaches"] == 0 and dm.calls == []
    out2 = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 73 * 3600)
    assert out2["breaches"] == 1


def test_non_gated_status_never_alerts(cfg, backlog, dm):
    _write_task(backlog, "T-0001", status="paused",
                status_since="2020-01-01T00:00:00Z")
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project")
    assert out["breaches"] == 0 and dm.calls == []


def test_same_stay_alerts_exactly_once(cfg, backlog, dm):
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    now = epoch + 25 * 3600
    status_deadlines.deadline_check_tick_one(cfg, "test-project", now=now)
    assert len(dm.calls) == 1
    # a later sweep, still the SAME stay (status_since unchanged) — no re-fire
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=now + 3600)
    assert out["breaches"] == 0 and len(dm.calls) == 1


def test_reentry_resets_the_clock(cfg, backlog, dm):
    """Resolve the stay, then re-enter the same gated status later — a FRESH
    status_since must alert again, matching task_chat's "an interesting
    transition always fires" contract."""
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    p = _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 25 * 3600)
    assert len(dm.calls) == 1

    new_since = "2026-08-10T00:00:00Z"
    new_epoch = status_deadlines._parse_iso(new_since)
    p.write_text(
        p.read_text().replace(f"status_since: {since}", f"status_since: {new_since}"),
        encoding="utf-8",
    )
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=new_epoch + 25 * 3600)
    assert out["breaches"] == 1 and len(dm.calls) == 2


def test_missing_status_since_falls_back_to_updated(cfg, backlog, dm):
    _write_task(backlog, "T-0001", status="to_accept", status_since="",
                updated="2026-08-01T00:00:00Z")
    now = status_deadlines._parse_iso("2026-08-01T00:00:00Z") + 25 * 3600
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=now)
    assert out["breaches"] == 1
    assert "since updated" in dm.calls[0]["message"]


def test_missing_every_timestamp_is_unmeasurable_not_a_false_positive(cfg, backlog, dm):
    p = backlog / "T-0001-stub.md"
    p.write_text(
        "---\nid: T-0001\ntitle: t\nstatus: totest\n---\n\n## Verbatim request\n\nx\n",
        encoding="utf-8",
    )
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=4_102_444_800.0)
    assert out["breaches"] == 0 and dm.calls == []


def test_two_breaches_batch_into_one_message(cfg, backlog, dm):
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    _write_task(backlog, "T-0002", status="to_accept", status_since=since)
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 25 * 3600)
    assert out["breaches"] == 2
    assert len(dm.calls) == 1
    assert "T-0001" in dm.calls[0]["message"] and "T-0002" in dm.calls[0]["message"]


def test_undelivered_send_defers_not_drops(cfg, backlog, dm):
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    dm.deliver = False
    now = epoch + 25 * 3600
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=now)
    assert out["breaches"] == 1 and out["delivered"] is False
    assert status_deadlines._read_sidecar(cfg, "test-project") == {}
    dm.deliver = True
    out2 = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=now + 1)
    assert out2["delivered"] is True
    assert "T-0001" in status_deadlines._read_sidecar(cfg, "test-project")


def test_notify_failure_never_raises_out(cfg, backlog, monkeypatch):
    import bot_squad_worker.actions as A

    def _boom(*a, **k):
        raise RuntimeError("transport down")

    monkeypatch.setattr(A, "_send_stakeholder_dm", _boom)
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 25 * 3600)
    assert out["delivered"] is False
    assert status_deadlines._read_sidecar(cfg, "test-project") == {}


def test_resolved_ticket_drops_out_of_sidecar(cfg, backlog, dm):
    """Once a ticket leaves its gated status, it must fall out of the sidecar
    so a LATER re-entry (fresh status_since) is not compared against a stale
    entry that happens to share nothing with it."""
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    p = _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 25 * 3600)
    assert "T-0001" in status_deadlines._read_sidecar(cfg, "test-project")
    p.write_text(
        p.read_text().replace("status: blocked_on_user", "status: in_progress"),
        encoding="utf-8",
    )
    status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 26 * 3600)
    assert status_deadlines._read_sidecar(cfg, "test-project") == {}


def test_corrupt_sidecar_rebaselines_without_blast(cfg, backlog, dm):
    sc = status_deadlines._sidecar_path(cfg, "test-project")
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text("not json", encoding="utf-8")
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    out = status_deadlines.deadline_check_tick_one(cfg, "test-project", now=epoch + 25 * 3600)
    assert out["breaches"] == 1 and out["delivered"] is True


def test_kill_switch_disables_sweep(cfg, backlog, dm, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_TICKET_DEADLINES", "0")
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    out = status_deadlines.deadline_check_tick(cfg)
    assert out["disabled"] is True and dm.calls == []


def test_tick_covers_all_projects_and_contains_errors(cfg, backlog, dm, monkeypatch):
    since = "2026-08-01T00:00:00Z"
    epoch = status_deadlines._parse_iso(since)
    _write_task(backlog, "T-0001", status="blocked_on_user", status_since=since)
    monkeypatch.setattr(status_deadlines.time, "time", lambda: epoch + 3600)  # only 1h in

    def _boom(cfg, slug, now=None):
        raise RuntimeError("boom")

    real = status_deadlines.deadline_check_tick_one
    monkeypatch.setattr(
        status_deadlines, "deadline_check_tick_one",
        lambda cfg, slug, now=None: _boom(cfg, slug) if slug == "other" else real(cfg, slug, now=now),
    )
    cfg.projects["other"] = cfg.projects["test-project"]
    out = status_deadlines.deadline_check_tick(cfg)
    assert "other" not in out["projects"]  # errored, contained
    assert out["projects"]["test-project"]["breaches"] == 0  # not yet 24h old


def test_scheduler_has_ticket_deadline_job(cfg):
    from bot_squad_worker.scheduler import build_scheduler
    sched = build_scheduler(cfg)
    assert "ticket_deadline" in {j.id for j in sched.get_jobs()}
