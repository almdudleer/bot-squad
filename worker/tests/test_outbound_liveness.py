"""T-0759: does a silent decay of the outbound log announce itself?

Why these exist beyond "the code works". T-0755's defect was not a crash — a
recording path fell out of use and nobody noticed for over a month. Since
T-0746 the same silence also degrades ``echo_guard`` rung 2, the only rung that
catches a copy-paste or hide-sender forward, so a decay re-attributes our own
text to the stakeholder with nothing failing.

Every test here is written to answer one question the operator set: **what
would make the green say no?** So the alarm paths are driven by FORCING the
inputs (touch a witness, point the check at the wrong directory) rather than by
waiting for a real decay — and the negative guards below matter as much, because
a detector that cries wolf on a healthy install gets muted and is then exactly
as useful as no detector.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import outbound_liveness as OL
from bot_squad_worker import outbound_log as OB


CHAT = "-1003761939853"
SID = "S-almdudleer-operator-p298"


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=tmp_path, projects={})


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _spool_record(tmp_path: Path, *, when: float, text: str = "x" * 40) -> None:
    OB.record(tmp_path, channel="tg", chat_id=CHAT, text=text,
              route_sid=SID, timestamp=_iso(when))


def _witness(tmp_path: Path, *, when: float, name: str = "w1") -> Path:
    """A debounce marker — the transport's own evidence that a send landed."""
    d = tmp_path / "_worker" / "tg_debounce"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.touch()
    os.utime(p, (when, when))
    return p


def _reply_map(tmp_path: Path, *, when: float) -> None:
    d = tmp_path / "_worker"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tg_reply_map.json").write_text(
        json.dumps({f"{CHAT}:1": {"sid": SID, "ts": when}}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_drops():
    """DROPS is process-global; a leaked count would make every later check
    read DECAYED for the wrong reason."""
    before = dict(OB.DROPS)
    yield
    OB.DROPS.update(before)


# ---------------------------------------------------------------------------
# The verdict — and above all, that it CAN say no
# ---------------------------------------------------------------------------

def test_decayed_when_a_send_is_witnessed_and_nothing_records_it(tmp_path: Path) -> None:
    """THE alarm. If this cannot go red, everything else is decoration."""
    now = time.time()
    _spool_record(tmp_path, when=now - 4 * 3600)
    _witness(tmp_path, when=now - 60)

    v = OL.check(_cfg(tmp_path), now=now)

    assert v["state"] == OL.DECAYED
    assert "NOT being recorded" in v["reason"]
    assert v["lag_s"] > OL.DECAY_LAG_S


def test_decay_does_not_clear_itself_on_a_second_look(tmp_path: Path) -> None:
    """A decay that heals when you look twice would train a reader to wait."""
    now = time.time()
    _spool_record(tmp_path, when=now - 4 * 3600)
    _witness(tmp_path, when=now - 60)

    assert OL.check(_cfg(tmp_path), now=now)["state"] == OL.DECAYED
    assert OL.check(_cfg(tmp_path), now=now + 60)["state"] == OL.DECAYED


def test_a_dropped_record_is_itself_decay(tmp_path: Path) -> None:
    """``outbound_log.record`` swallows its failures by contract (a log error
    must not break a send), so the drop counter is the only place that failure
    surfaces at all."""
    now = time.time()
    _spool_record(tmp_path, when=now - 60)
    _witness(tmp_path, when=now - 60)
    OB.DROPS["record"] += 1

    v = OL.check(_cfg(tmp_path), now=now)

    assert v["state"] == OL.DECAYED
    assert "dropped 1 delivered" in v["reason"]


# ---------------------------------------------------------------------------
# The negative guards — a healthy install must NOT be called broken
# ---------------------------------------------------------------------------

def test_ok_when_the_witnessed_send_is_in_the_spool(tmp_path: Path) -> None:
    now = time.time()
    _spool_record(tmp_path, when=now - 120)
    _witness(tmp_path, when=now - 120)

    v = OL.check(_cfg(tmp_path), now=now)

    assert v["state"] == OL.OK
    assert v["lag_s"] == 0


def test_a_declared_opt_out_accounts_for_its_witness(tmp_path: Path) -> None:
    """THE measured false positive, from the live install (2026-07-27).

    The newest debounce witness was 19:48:55Z against a newest spool record of
    18:35:40Z — a 73-minute gap that was entirely healthy: the 19:48:55 send was
    ``task_chat``'s lifecycle notice, which passes ``record_outbound=False``
    because it writes that same line into that same thread itself. Without the
    declared opt-out this module's FIRST act after deploy would have been a
    false alarm on a working install.
    """
    now = time.time()
    _spool_record(tmp_path, when=now - 4400)          # 18:35:40 analogue
    OB.note_unspooled(tmp_path)
    marker = OB.unspooled_marker(tmp_path)
    os.utime(marker, (now - 120, now - 120))          # 19:48:55 analogue
    _witness(tmp_path, when=now - 120)                # its debounce marker

    v = OL.check(_cfg(tmp_path), now=now)

    assert v["state"] == OL.OK, v["reason"]


def test_idle_is_not_ok_and_is_not_an_alarm(tmp_path: Path) -> None:
    """"We did not send" and "we sent and recorded it" are different claims and
    must not collapse into one green."""
    now = time.time()
    _spool_record(tmp_path, when=now - 30 * 3600)
    _witness(tmp_path, when=now - 30 * 3600)

    v = OL.check(_cfg(tmp_path), now=now)

    assert v["state"] == OL.IDLE
    assert "no send by any measure" in v["reason"]


def test_a_lag_inside_the_grace_window_is_not_decay(tmp_path: Path) -> None:
    """The drain runs every 30s and an mtime is not an ISO stamp; a detector
    that fires on ordinary skew gets muted."""
    now = time.time()
    _spool_record(tmp_path, when=now - 300)
    _witness(tmp_path, when=now - 60)

    assert OL.check(_cfg(tmp_path), now=now)["state"] == OL.OK


# ---------------------------------------------------------------------------
# BLIND — the ticket's own failure mode, hiding inside the detector
# ---------------------------------------------------------------------------

def test_the_wrong_data_dir_reads_blind_not_silent(tmp_path: Path) -> None:
    """``spool_dir`` appends ``_worker/outbound`` itself, so a caller passing
    ``…/data/_worker`` reads ``…/data/_worker/_worker/outbound`` — which does
    not exist, and returns ZERO RECORDS WITH NO ERROR. A monitor built on that
    argument reports "no sends" forever and does it most convincingly during a
    real outage. Measured on the live install: 21 records via the right path,
    0 via the wrong one, no exception either way."""
    now = time.time()
    _spool_record(tmp_path, when=now - 60)
    _witness(tmp_path, when=now - 60)

    v = OL.check(SimpleNamespace(data_dir=tmp_path / "_worker", projects={}), now=now)

    assert v["state"] == OL.BLIND
    assert v["state"] not in (OL.OK, OL.IDLE)
    assert "_worker/_worker/outbound" in v["reason"]


def test_zero_records_is_blind_not_idle(tmp_path: Path) -> None:
    """A fresh install and a mis-pointed reader are byte-identical from here."""
    (tmp_path / "_worker" / "outbound").mkdir(parents=True)
    _witness(tmp_path, when=time.time() - 60)

    v = OL.check(_cfg(tmp_path))

    assert v["state"] == OL.BLIND
    assert "not evidence of absence" in v["reason"]


def test_no_witness_source_is_blind_not_a_verdict_about_sends(tmp_path: Path) -> None:
    """With no witness readable, "nothing was sent" and "nothing was recorded"
    cannot be told apart — which is the whole defect, so neither may be said."""
    _spool_record(tmp_path, when=time.time() - 60)

    v = OL.check(_cfg(tmp_path))

    assert v["state"] == OL.BLIND
    assert "cannot tell" in v["reason"]


def test_check_reports_blind_rather_than_raising(tmp_path: Path) -> None:
    """A health check that can kill its own tick is one more way for the
    silence to come back."""
    v = OL.check(SimpleNamespace(projects={}))  # no data_dir at all

    assert v["state"] == OL.BLIND


def test_every_verdict_carries_its_positive_control(tmp_path: Path) -> None:
    """``scan`` rides on the HEALTHY answers too — a number without the proof
    that the read was live is the quiet zero this module exists to stop."""
    now = time.time()
    _spool_record(tmp_path, when=now - 120)
    _witness(tmp_path, when=now - 120)
    _reply_map(tmp_path, when=now - 120)

    scan = OL.check(_cfg(tmp_path), now=now)["scan"]

    assert scan["spool_records"] == 1
    assert scan["spool_files"] == 1
    assert scan["reply_map_entries"] == 1
    assert scan["debounce_files"] == 1
    assert scan["spool_dir"].endswith("_worker/outbound")
    assert scan["witness_sources"] == ["reply_map", "tg_debounce"]


# ---------------------------------------------------------------------------
# The witnesses
# ---------------------------------------------------------------------------

def test_reply_map_and_debounce_are_both_read_as_witnesses(tmp_path: Path) -> None:
    now = time.time()
    _reply_map(tmp_path, when=now - 500)
    _witness(tmp_path, when=now - 100)

    w = OL.witnesses(tmp_path)

    assert w["reply_map"]["entries"] == 1
    assert w["tg_debounce"]["files"] == 1
    assert w["newest"] == pytest.approx(now - 100, abs=2)


def test_a_missing_witness_dir_is_unavailable_not_quiet(tmp_path: Path) -> None:
    w = OL.witnesses(tmp_path)

    assert w["any_available"] is False
    assert w["tg_debounce"]["files"] == 0
    assert w["max_debounce"]["available"] is False


def test_max_debounce_counts_as_a_witness(tmp_path: Path) -> None:
    """MAX is the primary page channel on this install (T-0610), so a
    TG-only witness set would go blind exactly where the traffic is."""
    now = time.time()
    d = tmp_path / "_worker" / "max_debounce"
    d.mkdir(parents=True)
    (d / "m1").touch()
    os.utime(d / "m1", (now - 30, now - 30))
    _spool_record(tmp_path, when=now - 4 * 3600)

    v = OL.check(_cfg(tmp_path), now=now)

    assert v["state"] == OL.DECAYED
    assert "max_debounce" in v["scan"]["witness_sources"]


# ---------------------------------------------------------------------------
# The announcement — driven, not waited for
# ---------------------------------------------------------------------------

def _verdict(state: str) -> dict:
    return {"state": state, "reason": "because", "scan": {"spool_files": 1}}


def test_an_alarm_is_not_announced_until_it_has_held(tmp_path: Path) -> None:
    """The alarm page is ITSELF a send: if the recording is in fact working it
    lands in the spool and the next check reads healthy, so an instant page
    would be a 🔴 followed by a ✅ a minute later — which trains a reader to
    ignore both."""
    now = time.time()
    state, plan = OL.decide_announcement({}, _verdict(OL.DECAYED), now=now)

    assert plan["announce"] is False
    assert state["state"] == OL.DECAYED  # recorded even while unannounced


def test_an_alarm_that_holds_is_announced_once(tmp_path: Path) -> None:
    now = time.time()
    s1, _ = OL.decide_announcement({}, _verdict(OL.DECAYED), now=now)
    s2, plan = OL.decide_announcement(
        s1, _verdict(OL.DECAYED), now=now + OL.PERSIST_S + 1)

    assert plan["announce"] is True
    assert plan["kind"] == "DECAYED"
    assert "🔴" in plan["headline"]

    _, again = OL.decide_announcement(
        s2, _verdict(OL.DECAYED), now=now + OL.PERSIST_S + 300)
    assert again["announce"] is False


def test_an_ongoing_alarm_re_announces_only_after_the_nag_window(tmp_path: Path) -> None:
    now = time.time()
    s1, _ = OL.decide_announcement({}, _verdict(OL.DECAYED), now=now)
    s2, _ = OL.decide_announcement(s1, _verdict(OL.DECAYED), now=now + OL.PERSIST_S + 1)

    _, plan = OL.decide_announcement(
        s2, _verdict(OL.DECAYED), now=now + OL.PERSIST_S + OL.REANNOUNCE_S + 2)

    assert plan["kind"] == "DECAYED-STILL"
    assert "(still)" in plan["headline"]


def test_blind_gets_its_own_announcement_vocabulary(tmp_path: Path) -> None:
    """p330's T-0740 precedent: BLIND says in WORDS that zero is not evidence
    of absence, rather than being folded into the decay alarm."""
    now = time.time()
    s1, _ = OL.decide_announcement({}, _verdict(OL.BLIND), now=now)
    _, plan = OL.decide_announcement(s1, _verdict(OL.BLIND), now=now + OL.PERSIST_S + 1)

    assert plan["kind"] == "BLIND"
    assert "NOT evidence of absence" in plan["headline"]


def test_recovery_is_announced_once_and_only_after_a_real_alarm(tmp_path: Path) -> None:
    now = time.time()
    s1, _ = OL.decide_announcement({}, _verdict(OL.DECAYED), now=now)
    s2, fired = OL.decide_announcement(
        s1, _verdict(OL.DECAYED), now=now + OL.PERSIST_S + 1)
    assert fired["announce"] is True

    s3, cleared = OL.decide_announcement(
        s2, _verdict(OL.OK), now=now + OL.PERSIST_S + 60)
    assert cleared["kind"] == "CLEARED"
    assert "✅" in cleared["headline"]

    _, quiet = OL.decide_announcement(s3, _verdict(OL.OK), now=now + OL.PERSIST_S + 120)
    assert quiet["announce"] is False


def test_an_alarm_that_never_fired_needs_no_recovery_page(tmp_path: Path) -> None:
    """Otherwise a single flickering sample pages a ✅ for a breach nobody was
    ever told about."""
    now = time.time()
    s1, _ = OL.decide_announcement({}, _verdict(OL.DECAYED), now=now)
    _, plan = OL.decide_announcement(s1, _verdict(OL.OK), now=now + 60)

    assert plan["announce"] is False


def test_a_healthy_install_never_pages_on_its_first_tick(tmp_path: Path) -> None:
    now = time.time()
    _, plan = OL.decide_announcement({}, _verdict(OL.OK), now=now)

    assert plan["announce"] is False


# ---------------------------------------------------------------------------
# The tick — state on disk, and what the page actually says
# ---------------------------------------------------------------------------

def test_tick_persists_the_reading_even_when_it_says_nothing(tmp_path: Path) -> None:
    now = time.time()
    _spool_record(tmp_path, when=now - 4 * 3600)
    _witness(tmp_path, when=now - 60)
    pages: list[dict] = []

    OL.tick(_cfg(tmp_path), now=now, notify=lambda cfg, **kw: pages.append(kw))

    assert pages == []
    state = json.loads(OL.state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["state"] == OL.DECAYED
    assert state["scan"]["spool_records"] == 1


def test_tick_pages_urgently_and_names_no_project(tmp_path: Path) -> None:
    """The outbound log is ONE install-wide file tree. Tagging the page with
    whichever slug sorts first would name an owner with no more to do with it
    than any other — the call ``jobs.oauth_refresh`` and ``autoupdate_apply``
    already made. Urgent because the quiet-hours gate DROPS a non-urgent page,
    and an alarm about a silence that is itself silently dropped is this
    ticket's own bug wearing a different hat."""
    now = time.time()
    _spool_record(tmp_path, when=now - 4 * 3600)
    _witness(tmp_path, when=now - 60)
    pages: list[dict] = []
    cfg = SimpleNamespace(data_dir=tmp_path,
                          projects={"bot-squad": SimpleNamespace(tg_chat="404580642")})

    OL.tick(cfg, now=now, notify=lambda c, **kw: pages.append(kw))
    OL.tick(cfg, now=now + OL.PERSIST_S + 1, notify=lambda c, **kw: pages.append(kw))

    assert len(pages) == 1
    assert pages[0]["urgent"] is True
    assert pages[0]["sid"] == "outbound_log"
    assert "slug" not in pages[0]
    assert pages[0]["tg_chat_id"] == "404580642"
    assert "🔴" in pages[0]["message"]


def test_a_page_failure_never_kills_the_tick(tmp_path: Path) -> None:
    now = time.time()
    _spool_record(tmp_path, when=now - 4 * 3600)
    _witness(tmp_path, when=now - 60)

    def _boom(cfg, **kw):
        raise RuntimeError("telegram is down")

    OL.tick(_cfg(tmp_path), now=now, notify=_boom)
    v = OL.tick(_cfg(tmp_path), now=now + OL.PERSIST_S + 1, notify=_boom)

    assert v["state"] == OL.DECAYED  # the verdict still came back
