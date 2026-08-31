"""T-0938 — the ticket-update fan-out: a ticket change nudges the sessions bound to it.

The defect this covers is a MISSING channel, not a broken one. Sessions could
already write onto a ticket, but nothing told anyone a ticket had changed — so
"make sure they see it" could only mean pasting the words into a peer inbox, and
the stakeholder stopped using the user-session over the «глухой телефон» that
produced.

Two properties carry the ticket and get the most attention here:

* detection is WRITER-AGNOSTIC — the test that matters most edits the md
  directly, with no worker action involved, because `bsq ticket update` and the
  api's PATCH do exactly that;
* self-notification is suppressed PER SECTION — a session is not told about its
  own write, and is still told about someone else's write in the same window.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker import ticket_watch as TW


# --- harness -----------------------------------------------------------------

def _make_cfg(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    (data_dir / "bot-squad" / "sessions").mkdir(parents=True)
    (data_dir / "bot-squad" / "backlog").mkdir(parents=True)
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir), data_dir


def _write_session(data_dir: Path, sid: str, *, task_id: str | None = None,
                   extra: list[str] | None = None, status: str = "suspended",
                   archived: bool = False) -> None:
    fm = {"sid": sid, "status": status, "window": "demo",
          "cwd": str(data_dir.parent / "repo"), "claude_uuid": "uuid-" + sid}
    if task_id:
        fm["task_id"] = task_id
    if extra:
        fm["extra_task_ids"] = extra
    if archived:
        fm["archived"] = True
    S._write_session_metadata(data_dir / "bot-squad" / "sessions" / f"{sid}.md", fm)


def _write_ticket(data_dir: Path, tid: str, *, status: str = "planned",
                  title: str = "Demo ticket", verbatim: str = "> the ask",
                  summary: str = "", context: str = "ctx", progress: str = "") -> Path:
    path = data_dir / "bot-squad" / "backlog" / f"{tid}-demo.md"
    body = f"## Stakeholder notes\n\n{verbatim}\n\n"
    if summary:
        body += f"## Executive summary\n\n{summary}\n\n"
    body += f"## Context\n\n{context}\n\n## Progress\n\n{progress}\n"
    path.write_text(f"---\nid: {tid}\ntitle: {title}\nstatus: {status}\n---\n\n{body}")
    return path


@pytest.fixture
def bus(monkeypatch):
    """Capture what the fan-out puts on the peer bus, and stub the pane poke."""
    sent: list[tuple[str, str]] = []

    from bot_squad_worker import intersession as _is

    monkeypatch.setattr(
        _is, "send_notice",
        lambda cfg, slug, from_sid, to, text, user=None:
            sent.append((to, text)) or {"ok": True, "delivered_to": [to], "parts": 1})
    monkeypatch.setattr(TW, "_pane_nudge", lambda cfg, sid: True)
    return sent


# --- env knob ----------------------------------------------------------------

def test_enabled_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_TICKET_WATCH", raising=False)
    assert TW.ticket_watch_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_TICKET_WATCH", "0")
    assert TW.ticket_watch_enabled() is False


# --- snapshot / diff (pure) ---------------------------------------------------

def test_snapshot_reads_id_status_title_and_every_section(tmp_path):
    _cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042", status="in_progress", title="A title")
    snap = TW.snapshot_text(path.read_text())
    assert snap["id"] == "T-0042"
    assert snap["status"] == "in_progress"
    assert snap["title"] == "A title"
    assert set(snap["sections"]) == {"verbatim", "summary", "context", "progress"}


def test_snapshot_treats_both_spellings_of_the_stakeholder_heading_alike():
    """T-0767 renamed `## Verbatim request` to `## Stakeholder notes` and left
    both parsing. A digest that saw them as different sections would report a
    heading rename as a content change on 60+ live tickets."""
    old = "---\nid: T-1\nstatus: planned\n---\n\n## Verbatim request\n\n> hi\n"
    new = "---\nid: T-1\nstatus: planned\n---\n\n## Stakeholder notes\n\n> hi\n"
    assert TW.snapshot_text(old)["sections"]["verbatim"] == \
        TW.snapshot_text(new)["sections"]["verbatim"]


def test_snapshot_survives_a_file_with_no_frontmatter():
    snap = TW.snapshot_text("just some text, no frontmatter at all")
    assert snap["id"] == "" and snap["status"] == ""


def test_first_sighting_is_not_an_update(tmp_path):
    _cfg, data = _make_cfg(tmp_path)
    snap = TW.snapshot_text(_write_ticket(data, "T-0042").read_text())
    assert TW.changed_keys(None, snap) == []


def test_changed_keys_names_status_and_each_section(tmp_path):
    _cfg, data = _make_cfg(tmp_path)
    a = TW.snapshot_text(_write_ticket(data, "T-0042").read_text())
    b = TW.snapshot_text(
        _write_ticket(data, "T-0042", status="in_progress", context="NEW").read_text())
    assert TW.changed_keys(a, b) == ["status", "context"]


def test_a_title_edit_is_not_a_change(tmp_path):
    """The title rides the snapshot for the notice text only. Paging every bound
    session over a typo fix in a title is noise, and noise is how a channel dies
    — which is the failure this ticket is about."""
    _cfg, data = _make_cfg(tmp_path)
    a = TW.snapshot_text(_write_ticket(data, "T-0042", title="old").read_text())
    b = TW.snapshot_text(_write_ticket(data, "T-0042", title="new").read_text())
    assert TW.changed_keys(a, b) == []


def test_describe_change_spells_out_the_transition_and_names_the_headings():
    prev = {"status": "planned", "sections": {}}
    cur = {"status": "in_progress", "sections": {}}
    text = TW.describe_change(prev, cur, ["status", "context", "verbatim"])
    assert "status planned -> in_progress" in text
    assert "## Context" in text and "## Stakeholder notes" in text


def test_notice_names_the_ticket_and_the_file_and_carries_no_content():
    text = TW.notice_text("T-0938", "a title", "rewritten: ## Context",
                          "data/bot-squad/backlog/T-0938-x.md")
    assert "T-0938" in text
    assert "data/bot-squad/backlog/T-0938-x.md" in text
    assert "## Context" in text
    # The content itself must NOT ride the notice — a notice carrying the text
    # rebuilds the relay: the reader acts on a copy and the board keeps no trace.
    assert "ctx" not in text


# --- recipients ---------------------------------------------------------------

def test_bound_sids_finds_primary_and_bundled_bindings(tmp_path):
    cfg, data = _make_cfg(tmp_path)
    _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-primary-p1", task_id="T-0042")
    _write_session(data, "S-almdudleer-bot-squad-bundled-p2", task_id="T-0099",
                   extra=["T-0042"])
    _write_session(data, "S-almdudleer-bot-squad-other-p3", task_id="T-0777")
    found = set(TW.bound_sids(cfg, "bot-squad", "T-0042"))
    assert found == {"S-almdudleer-bot-squad-primary-p1",
                     "S-almdudleer-bot-squad-bundled-p2"}


def test_archived_sessions_are_never_recipients(tmp_path):
    cfg, data = _make_cfg(tmp_path)
    _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dead-p1", task_id="T-0042",
                   archived=True)
    assert TW.bound_sids(cfg, "bot-squad", "T-0042") == []


def test_an_unheld_ticket_falls_back_to_the_live_operator(tmp_path, monkeypatch):
    cfg, data = _make_cfg(tmp_path)
    _write_ticket(data, "T-0042")
    from bot_squad_worker import dispatch as _dispatch
    monkeypatch.setattr(_dispatch, "live_operator_sids",
                        lambda cfg, slug: ["S-almdudleer-bot-squad-operator-p4"])
    sids, why = TW.recipients_for(cfg, "bot-squad", "T-0042")
    assert sids == ["S-almdudleer-bot-squad-operator-p4"]
    assert why == "operator"


def test_a_held_ticket_does_not_also_go_to_the_operator(tmp_path, monkeypatch):
    cfg, data = _make_cfg(tmp_path)
    _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    from bot_squad_worker import dispatch as _dispatch
    monkeypatch.setattr(_dispatch, "live_operator_sids",
                        lambda cfg, slug: ["S-almdudleer-bot-squad-operator-p4"])
    sids, why = TW.recipients_for(cfg, "bot-squad", "T-0042")
    assert sids == ["S-almdudleer-bot-squad-dev-p1"] and why == "bound"


# --- the sweep ----------------------------------------------------------------

def test_first_run_records_everything_and_notifies_nobody(tmp_path, bus):
    cfg, data = _make_cfg(tmp_path)
    _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    assert TW._scan_project(cfg, "bot-squad") == []
    assert bus == []
    assert TW.state_path(cfg, "bot-squad").exists()


def test_a_direct_md_edit_notifies_the_bound_session(tmp_path, bus):
    """The property the whole design rests on: NO worker action is involved
    here. `bsq ticket update` patches frontmatter in the CLI process and the api
    PATCHes from the web UI — a writer-side hook would miss both, and the paths
    that route around instrumentation are precisely the ones this exists to
    catch."""
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")

    path.write_text(path.read_text().replace("status: planned", "status: in_progress"))
    out = TW._scan_project(cfg, "bot-squad")

    assert [r["sid"] for r in out] == ["S-almdudleer-bot-squad-dev-p1"]
    assert out[0]["keys"] == ["status"]
    assert len(bus) == 1
    assert "T-0042" in bus[0][1] and "status planned -> in_progress" in bus[0][1]


def test_an_unchanged_ticket_notifies_nothing_on_the_next_pass(tmp_path, bus):
    cfg, data = _make_cfg(tmp_path)
    _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")
    assert TW._scan_project(cfg, "bot-squad") == []
    assert TW._scan_project(cfg, "bot-squad") == []
    assert bus == []


def test_the_author_of_a_section_is_not_told_about_its_own_write(tmp_path, bus):
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    sid = "S-almdudleer-bot-squad-dev-p1"
    _write_session(data, sid, task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")

    path.write_text(path.read_text().replace("ctx", "a new working area"))
    TW.note_author(cfg, "bot-squad", "T-0042", sid, ("context",))
    assert TW._scan_project(cfg, "bot-squad") == []
    assert bus == []


def test_suppression_is_per_section_not_per_ticket(tmp_path, bus):
    """The reason authorship is keyed by section. A session that wrote Context
    must still hear about somebody else's status move landing in the same 60s
    window — suppressing the whole ticket would drop exactly the cross-session
    half the fan-out exists for."""
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    sid = "S-almdudleer-bot-squad-dev-p1"
    _write_session(data, sid, task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")

    text = path.read_text().replace("ctx", "my own working area")
    path.write_text(text.replace("status: planned", "status: in_progress"))
    TW.note_author(cfg, "bot-squad", "T-0042", sid, ("context",))
    out = TW._scan_project(cfg, "bot-squad")

    assert len(out) == 1 and out[0]["keys"] == ["status"]
    assert "## Context" not in bus[0][1]
    assert "status planned -> in_progress" in bus[0][1]


def test_another_sessions_write_still_reaches_you(tmp_path, bus):
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    mine = "S-almdudleer-bot-squad-dev-p1"
    theirs = "S-almdudleer-bot-squad-tl-p2"
    _write_session(data, mine, task_id="T-0042")
    _write_session(data, theirs, task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")

    path.write_text(path.read_text().replace("ctx", "their working area"))
    TW.note_author(cfg, "bot-squad", "T-0042", theirs, ("context",))
    out = TW._scan_project(cfg, "bot-squad")

    assert [r["sid"] for r in out] == [mine]


def test_authorship_is_consumed_by_the_pass_that_saw_the_change(tmp_path, bus):
    """A record that outlived its change would silently suppress the NEXT edit
    of the same section by the same session — turning the fix into the bug."""
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    sid = "S-almdudleer-bot-squad-dev-p1"
    _write_session(data, sid, task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")

    path.write_text(path.read_text().replace("ctx", "first"))
    TW.note_author(cfg, "bot-squad", "T-0042", sid, ("context",))
    assert TW._scan_project(cfg, "bot-squad") == []

    # Same section, same session, no new authorship record: it must notify now.
    path.write_text(path.read_text().replace("first", "second"))
    out = TW._scan_project(cfg, "bot-squad")
    assert [r["sid"] for r in out] == [sid]


def test_a_bulk_edit_is_suppressed_instead_of_storming(tmp_path, bus, caplog):
    cfg, data = _make_cfg(tmp_path)
    sid = "S-almdudleer-bot-squad-dev-p1"
    ids = [f"T-{n:04d}" for n in range(1, TW._MAX_TICKETS_PER_TICK + 3)]
    for tid in ids:
        _write_ticket(data, tid)
    _write_session(data, sid, task_id=ids[0], extra=ids[1:])
    TW._scan_project(cfg, "bot-squad")

    for tid in ids:
        p = data / "bot-squad" / "backlog" / f"{tid}-demo.md"
        p.write_text(p.read_text().replace("status: planned", "status: in_progress"))
    assert TW._scan_project(cfg, "bot-squad") == []
    assert bus == []
    # ...and the burst is not re-detected on the following pass.
    assert TW._scan_project(cfg, "bot-squad") == []


def test_a_deleted_ticket_is_dropped_from_the_state(tmp_path, bus):
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    TW._scan_project(cfg, "bot-squad")
    path.unlink()
    TW._scan_project(cfg, "bot-squad")
    import json
    state = json.loads(TW.state_path(cfg, "bot-squad").read_text())
    assert "T-0042" not in state["tickets"]
    assert state["files"] == {}


def test_kill_switch_stops_the_tick(tmp_path, bus, monkeypatch):
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")
    path.write_text(path.read_text().replace("status: planned", "status: in_progress"))

    monkeypatch.setenv("BOT_SQUAD_TICKET_WATCH", "0")
    TW.tick(cfg)
    assert bus == []


def test_tick_sweeps_every_project_and_swallows_a_bad_one(tmp_path, bus, monkeypatch):
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    TW.tick(cfg)
    path.write_text(path.read_text().replace("status: planned", "status: in_progress"))
    TW.tick(cfg)
    assert len(bus) == 1

    monkeypatch.setattr(TW, "_scan_project",
                        lambda cfg, slug: (_ for _ in ()).throw(RuntimeError("boom")))
    TW.tick(cfg)  # must not raise


def test_note_author_never_raises_on_an_unwritable_state(tmp_path, monkeypatch):
    cfg, _data = _make_cfg(tmp_path)
    monkeypatch.setattr(TW, "_save_state",
                        lambda p, s: (_ for _ in ()).throw(OSError("read-only")))
    TW.note_author(cfg, "bot-squad", "T-0042", "S-x", ("context",))


def test_an_unchanged_backlog_is_not_re_parsed(tmp_path, bus, monkeypatch):
    """The mtime gate is a COST guard, so no behaviour test can see it move —
    which is exactly why it needs its own. Without it the sweep re-parses every
    ticket on the board (851 files / 9MB on the live install) every 60 seconds,
    forever, and nothing would ever go red."""
    cfg, data = _make_cfg(tmp_path)
    for n in range(1, 6):
        _write_ticket(data, f"T-{n:04d}")
    TW._scan_project(cfg, "bot-squad")

    calls: list[int] = []
    real = TW.snapshot_text
    monkeypatch.setattr(TW, "snapshot_text",
                        lambda text: calls.append(1) or real(text))
    TW._scan_project(cfg, "bot-squad")
    assert calls == []

    path = data / "bot-squad" / "backlog" / "T-0003-demo.md"
    path.write_text(path.read_text().replace("ctx", "moved"))
    TW._scan_project(cfg, "bot-squad")
    assert len(calls) == 1  # only the file that actually changed


def test_notification_rides_send_notice_not_send(tmp_path, monkeypatch):
    """A tick has NOWHERE to report a refusal — `intersession.send` refuses
    over-cap text and would end in "the caller believes it sent and the
    recipient got nothing", the one outcome T-0827 forbids. `send_notice`
    splits instead. Pinned by making the wrong function unusable."""
    cfg, _data = _make_cfg(tmp_path)
    from bot_squad_worker import intersession as _is
    seen: list[str] = []
    monkeypatch.setattr(_is, "send",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("used send")))
    monkeypatch.setattr(_is, "send_notice",
                        lambda cfg, slug, from_sid, to, text, user=None:
                            seen.append(to) or {"ok": True, "delivered_to": [to]})
    monkeypatch.setattr(TW, "_pane_nudge", lambda cfg, sid: True)
    assert TW._notify(cfg, "bot-squad", "S-x", "hello") is True
    assert seen == ["S-x"]


def test_a_live_recipient_also_gets_the_pane_nudge(tmp_path, monkeypatch):
    """The inbox line is durable but PASSIVE: a session mid-task reads its inbox
    only when it decides to. Without the `check mail` poke the fan-out would be
    a mailbox nobody opens — the same dead end that made relaying feel
    necessary."""
    cfg, _data = _make_cfg(tmp_path)
    from bot_squad_worker import intersession as _is
    monkeypatch.setattr(_is, "send_notice",
                        lambda cfg, slug, from_sid, to, text, user=None:
                            {"ok": True, "delivered_to": [to]})
    poked: list[str] = []
    monkeypatch.setattr(TW, "_pane_nudge", lambda cfg, sid: poked.append(sid) or True)
    TW._notify(cfg, "bot-squad", "S-x", "hello")
    assert poked == ["S-x"]


def test_an_undelivered_notice_is_not_counted_as_notified(tmp_path, monkeypatch):
    cfg, _data = _make_cfg(tmp_path)
    from bot_squad_worker import intersession as _is
    monkeypatch.setattr(_is, "send_notice",
                        lambda *a, **k: {"ok": False, "delivered_to": []})
    monkeypatch.setattr(TW, "_pane_nudge", lambda cfg, sid: True)
    assert TW._notify(cfg, "bot-squad", "S-x", "hello") is False


def test_a_same_size_status_move_is_still_detected(tmp_path, bus):
    """`totest` and `closed` are both six characters, and so are `paused` and
    `open`+2 — a gate that trusted file SIZE would miss the single most common
    end-of-life transition on the board. The mtime half is what catches it."""
    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042", status="totest")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")

    before = path.stat().st_size
    path.write_text(path.read_text().replace("status: totest", "status: closed"))
    assert path.stat().st_size == before  # the trap this test exists for
    out = TW._scan_project(cfg, "bot-squad")
    assert [r["keys"] for r in out] == [["status"]]
    assert "status totest -> closed" in bus[0][1]


def test_a_restore_that_preserves_mtime_is_still_detected(tmp_path, bus):
    """T-0891's undo path restores a ticket by copying bytes back out of
    `.versions/`, and a copy made with preserved timestamps (`cp -p`, rsync
    --times) can land OLDER bytes under an OLDER mtime. The size half of the
    gate is what keeps that from being invisible."""
    import os

    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042", context="a long working area paragraph")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")
    st = path.stat()

    path.write_text(path.read_text().replace("a long working area paragraph", "x"))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # the restore's rewind
    out = TW._scan_project(cfg, "bot-squad")
    assert [r["keys"] for r in out] == [["context"]]


def test_a_change_that_reached_nobody_is_logged_not_swallowed(tmp_path, bus, caplog,
                                                              monkeypatch):
    """Measured on the live install (2026-08-31): bot-squad has no live operator
    pane, so every unheld ticket's change resolves to an empty recipient list.
    The module refuses to invent a recipient — but a change that told nobody is
    the exact state the relay habit grew in, so it leaves a WARNING behind
    rather than returning quietly."""
    import logging

    cfg, data = _make_cfg(tmp_path)
    path = _write_ticket(data, "T-0042")
    from bot_squad_worker import dispatch as _dispatch
    monkeypatch.setattr(_dispatch, "live_operator_sids", lambda cfg, slug: [])
    TW._scan_project(cfg, "bot-squad")

    path.write_text(path.read_text().replace("status: planned", "status: paused"))
    with caplog.at_level(logging.WARNING, logger="bot_squad_worker.ticket_watch"):
        assert TW._scan_project(cfg, "bot-squad") == []
    assert bus == []
    assert any("reached NOBODY" in r.getMessage() for r in caplog.records)
    assert any("T-0042" in r.getMessage() for r in caplog.records)


def test_an_idle_sweep_does_not_rewrite_the_state_file(tmp_path, bus):
    """The state file is 385KB for the live board's 856 tickets. An
    unconditional save rewrote it every 60s forever just to say nothing had
    changed — a cost no behaviour test can see, so it gets its own."""
    cfg, data = _make_cfg(tmp_path)
    for n in range(1, 5):
        _write_ticket(data, f"T-{n:04d}")
    TW._scan_project(cfg, "bot-squad")
    st = TW.state_path(cfg, "bot-squad")
    stamp = st.stat().st_mtime_ns

    TW._scan_project(cfg, "bot-squad")
    TW._scan_project(cfg, "bot-squad")
    assert st.stat().st_mtime_ns == stamp

    p = data / "bot-squad" / "backlog" / "T-0002-demo.md"
    p.write_text(p.read_text().replace("ctx", "moved"))
    TW._scan_project(cfg, "bot-squad")
    assert st.stat().st_mtime_ns != stamp  # a real change still persists


def test_the_session_roster_is_read_once_per_sweep_not_once_per_ticket(
        tmp_path, bus, monkeypatch):
    """Another COST guard no behaviour test can see. Measured on the live
    install: one roster read is 0.16s over 660 session mds, so resolving
    recipients per ticket would cost up to 25 x 0.16s = 4s of identical
    repeated work inside a 60s tick. It also makes the pass CONSISTENT — every
    ticket resolves against the same roster, so a session appearing or dying
    mid-sweep can't put half a pass on one recipient set and half on another."""
    cfg, data = _make_cfg(tmp_path)
    sid = "S-almdudleer-bot-squad-dev-p1"
    ids = [f"T-{n:04d}" for n in range(1, 6)]
    for tid in ids:
        _write_ticket(data, tid)
    _write_session(data, sid, task_id=ids[0], extra=ids[1:])
    TW._scan_project(cfg, "bot-squad")

    calls: list[int] = []
    real = TW.session_rows
    monkeypatch.setattr(TW, "session_rows",
                        lambda cfg, slug: calls.append(1) or real(cfg, slug))
    for tid in ids:
        p = data / "bot-squad" / "backlog" / f"{tid}-demo.md"
        p.write_text(p.read_text().replace("status: planned", "status: in_progress"))
    out = TW._scan_project(cfg, "bot-squad")

    assert len(out) == len(ids)   # all five really did notify
    assert calls == [1]           # ...off ONE roster read


def test_an_idle_sweep_reads_no_roster_at_all(tmp_path, bus, monkeypatch):
    """The lazy half: a pass with nothing to say must not pay for the roster."""
    cfg, data = _make_cfg(tmp_path)
    _write_ticket(data, "T-0042")
    _write_session(data, "S-almdudleer-bot-squad-dev-p1", task_id="T-0042")
    TW._scan_project(cfg, "bot-squad")

    calls: list[int] = []
    monkeypatch.setattr(TW, "session_rows", lambda cfg, slug: calls.append(1) or [])
    TW._scan_project(cfg, "bot-squad")
    assert calls == []
