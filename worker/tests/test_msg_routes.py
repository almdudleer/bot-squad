"""Per-message-TYPE outbound destination map (T-0799).

Stakeholder ask, verbatim: «пускай будет конфигурируемые chat_id все типы
сообщений, но по дефолту всё ЛС» — configurable ``chat_id`` for every automated
message type, defaulting to today's DM behaviour.

The tests are in four groups, and the load-bearing ones are the last two:

1. the STORE + the resolution ladder;
2. the LOSS GUARD on the config surface (the T-0771 rule applied here);
3. **DEFAULT IS A NO-OP** — the property the whole design rests on. Exercised
   against the real emitters, not just ``route()``, because "the default
   reproduces today's behaviour" is a claim about what ``jobs``/``task_chat``/
   ``tg_stall`` deliver, not about a lookup function;
4. **THE ENUMERATION GUARD** — a new automated pager that names no type, or a
   registered type nobody sends, goes RED. Without it the map silently stops
   covering the surface it was built for, which is the failure this ticket
   exists to prevent recurring.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot_squad_worker
from bot_squad_worker import msg_routes

PKG = Path(bot_squad_worker.__file__).parent


def _cfg(tmp_path: Path, slug: str = "demo", tg_chat: str = "404580642"):
    """A config with one project whose ``tg_chat`` is a personal DM — the live
    shape (both real projects point at chat 404580642, his bot DM, which is why
    every automated message lands there today)."""
    project = SimpleNamespace(
        slug=slug, tg_chat=tg_chat, tg_topic_id=None, staging_url="", mothership=False,
    )
    return SimpleNamespace(data_dir=tmp_path / "data", projects={slug: project})


# ---------------------------------------------------------------------------
# 1. Store + resolution ladder
# ---------------------------------------------------------------------------

def test_every_registered_type_declares_a_known_urgency():
    for key, t in msg_routes.TYPES.items():
        assert t.key == key, f"{key} disagrees with its own record key"
        assert t.urgency in msg_routes.URGENCIES, f"{key} has urgency {t.urgency!r}"
        assert t.summary, f"{key} has no human summary (bsq msg-route list prints it)"


def test_both_urgency_bands_are_populated():
    """A taxonomy that put everything in one band would be no taxonomy — he is
    buying the SPLIT (siren on the DM, routine to Logs)."""
    bands = {u: [k for k, t in msg_routes.TYPES.items() if t.urgency == u]
             for u in msg_routes.URGENCIES}
    assert bands[msg_routes.URGENT], "no type is urgent — the DM would carry nothing"
    assert bands[msg_routes.LOG], "no type is routine — there would be nothing to move"


def test_keys_offers_every_type_plus_both_classes():
    assert msg_routes.keys() == sorted(msg_routes.TYPES) + ["class:urgent", "class:log"]


def test_validate_key_rejects_a_typo_naming_the_valid_set():
    with pytest.raises(ValueError) as e:
        msg_routes.validate_key("deploy_statuss")
    assert "deploy_status" in str(e.value)


def test_no_store_is_the_identity(tmp_path: Path):
    """THE default property, at the seam: with nothing configured, route() hands
    back exactly what the caller computed."""
    cfg = _cfg(tmp_path)
    for key in msg_routes.TYPES:
        r = msg_routes.route(cfg, key, slug="demo", chat_id="404580642", topic_id=None)
        assert (r.chat_id, r.topic_id, r.applied, r.source) == (
            "404580642", None, False, "default")


def test_an_exact_type_entry_replaces_the_destination(tmp_path: Path):
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "task_lifecycle",
                         chat_id="-1003761939853", topic_id=517)
    r = msg_routes.route(cfg, "task_lifecycle", slug="demo",
                         chat_id="404580642", topic_id=None)
    assert (r.chat_id, r.topic_id, r.applied, r.source) == (
        "-1003761939853", 517, True, "task_lifecycle")


def test_a_class_entry_moves_every_type_in_that_class(tmp_path: Path):
    """`class:log` is how "send the routine stuff to [BS] Logs" is one command
    instead of one per type."""
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "class:log",
                         chat_id="-1003761939853", topic_id=517)
    for key, t in msg_routes.TYPES.items():
        r = msg_routes.route(cfg, key, slug="demo", chat_id="404580642", topic_id=None)
        if t.urgency == msg_routes.LOG:
            assert (r.chat_id, r.topic_id, r.source) == ("-1003761939853", 517, "class:log")
        else:
            assert (r.chat_id, r.topic_id, r.source) == ("404580642", None, "default"), (
                f"{key} is urgent-class and must NOT follow class:log")


def test_an_exact_type_entry_beats_its_class(tmp_path: Path):
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "class:log", chat_id="-100111", topic_id=1)
    msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="-100222", topic_id=2)
    r = msg_routes.route(cfg, "task_lifecycle", slug="demo", chat_id="404580642")
    assert (r.chat_id, r.topic_id, r.source) == ("-100222", 2, "task_lifecycle")


def test_a_topic_only_entry_keeps_the_default_chat(tmp_path: Path):
    """"Move this type into a thread of the chat it already uses" — a real and
    useful destination, so an empty chat_id is a value, not a missing field."""
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "deploy_status", topic_id=42)
    r = msg_routes.route(cfg, "deploy_status", slug="demo", chat_id="404580642")
    assert (r.chat_id, r.topic_id, r.applied) == ("404580642", 42, True)


def test_a_chat_only_entry_does_not_inherit_the_defaults_thread(tmp_path: Path):
    """The whole-pair rule. A thread id only means anything inside the chat that
    owns it, so carrying the default's thread into a routed chat would address a
    thread in the WRONG chat (the hazard tg_notify's rung-4 comment names)."""
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "deploy_status", chat_id="-1003761939853")
    r = msg_routes.route(cfg, "deploy_status", slug="demo",
                         chat_id="404580642", topic_id=99)
    assert (r.chat_id, r.topic_id) == ("-1003761939853", None)


def test_route_without_a_slug_is_the_default(tmp_path: Path):
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "class:log", chat_id="-100111", topic_id=1)
    r = msg_routes.route(cfg, "task_lifecycle", slug="", chat_id="404580642")
    assert (r.chat_id, r.applied) == ("404580642", False)


def test_an_unreadable_store_degrades_to_the_default(tmp_path: Path):
    """A corrupt routing file must cost the ROUTE, never the message."""
    cfg = _cfg(tmp_path)
    p = msg_routes.routes_path(cfg, "demo")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    r = msg_routes.route(cfg, "task_lifecycle", slug="demo", chat_id="404580642")
    assert (r.chat_id, r.applied) == ("404580642", False)


def test_a_non_integer_topic_id_on_disk_is_dropped_not_sent_to(tmp_path: Path):
    cfg = _cfg(tmp_path)
    p = msg_routes.routes_path(cfg, "demo")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"task_lifecycle": {"chat_id": "-100", "topic_id": "General"}}))
    assert msg_routes.load(cfg, "demo") == {}
    r = msg_routes.route(cfg, "task_lifecycle", slug="demo", chat_id="404580642")
    assert r.applied is False


def test_unknown_type_defaults_to_the_log_class_not_the_siren():
    """An unregistered type must not silently join the channel he puts a siren
    on. LOG is the safe side of this particular default."""
    assert msg_routes.urgency("something_new") == msg_routes.LOG


def test_clear_route_restores_the_default(tmp_path: Path):
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="-100111", topic_id=1)
    assert msg_routes.clear_route(cfg, "demo", "task_lifecycle") is True
    assert msg_routes.clear_route(cfg, "demo", "task_lifecycle") is False  # idempotent
    r = msg_routes.route(cfg, "task_lifecycle", slug="demo", chat_id="404580642")
    assert (r.chat_id, r.applied) == ("404580642", False)


def test_describe_reports_the_class_entry_that_governs_a_type(tmp_path: Path):
    """Reading the store cannot tell you which types a class entry governs;
    `bsq msg-route list` has to, so describe() resolves per type."""
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "class:log", chat_id="-100111", topic_id=1)
    rows = {r["msg_type"]: r for r in msg_routes.describe(cfg, "demo")}
    assert rows["task_lifecycle"]["source"] == "class:log"
    assert rows["deploy_failed"]["source"] == "default"
    assert rows["class:log"]["chat_id"] == "-100111"


# ---------------------------------------------------------------------------
# 2. The loss guard on the config surface
# ---------------------------------------------------------------------------

def test_set_route_refuses_to_implicitly_drop_a_stored_topic(tmp_path: Path):
    """Re-pointing a type's chat while forgetting its topic would move it to the
    new chat's GENERAL FEED and look exactly like a successful re-point. Refuse
    (T-0771's rule), naming the value at risk."""
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="-100111", topic_id=517)
    with pytest.raises(msg_routes.LossyRouteError) as e:
        msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="-100222")
    assert e.value.fields == {"topic_id": 517}
    # And nothing was written.
    assert msg_routes.load(cfg, "demo")["task_lifecycle"] == {
        "chat_id": "-100111", "topic_id": 517}


def test_set_route_refuses_to_implicitly_drop_a_stored_chat(tmp_path: Path):
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="-100111", topic_id=517)
    with pytest.raises(msg_routes.LossyRouteError) as e:
        msg_routes.set_route(cfg, "demo", "task_lifecycle", topic_id=9)
    assert e.value.fields == {"chat_id": "-100111"}


def test_an_explicit_empty_form_is_allowed_through(tmp_path: Path):
    """The escape from the guard is to SAY you meant it: an explicit empty is a
    real destination (the project's own chat / no thread), not a missing value."""
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="-100111", topic_id=517)
    rec = msg_routes.set_route(cfg, "demo", "task_lifecycle",
                               chat_id="-100222", topic_id=None)
    assert rec == {"chat_id": "-100222", "topic_id": None}


def test_set_route_needs_at_least_one_field(tmp_path: Path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        msg_routes.set_route(cfg, "demo", "task_lifecycle")


def test_an_all_empty_entry_pins_a_type_to_the_default_against_its_class(tmp_path: Path):
    """"Everything routine to [BS] Logs, EXCEPT keep the task notices in the
    DM." Found by walking the scenario through, not by reading the code: with a
    `class:log` entry in place there was no way to exempt one type short of
    re-typing the DM's chat id, so an explicitly-empty entry has to be a real
    (matching) route rather than a refused no-op."""
    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "class:log", chat_id="-1003761939853", topic_id=517)
    msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="", topic_id=None)

    pinned = msg_routes.route(cfg, "task_lifecycle", slug="demo", chat_id="404580642")
    assert (pinned.chat_id, pinned.topic_id) == ("404580642", None)
    assert (pinned.applied, pinned.source) == (True, "task_lifecycle"), (
        "the exemption must be visible as a route, not look like an absent one")
    # Its sibling log-class types are unaffected.
    other = msg_routes.route(cfg, "autopilot_notice", slug="demo", chat_id="404580642")
    assert (other.chat_id, other.topic_id) == ("-1003761939853", 517)
    # And clearing the pin hands the type back to its class.
    msg_routes.clear_route(cfg, "demo", "task_lifecycle")
    back = msg_routes.route(cfg, "task_lifecycle", slug="demo", chat_id="404580642")
    assert (back.chat_id, back.source) == ("-1003761939853", "class:log")


def test_set_route_rejects_an_unknown_key(tmp_path: Path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        msg_routes.set_route(cfg, "demo", "class:whenever", chat_id="-100")


def test_change_summary_names_what_moved_and_says_nothing_when_nothing_did():
    assert msg_routes.change_summary(
        {"chat_id": "-100111", "topic_id": 1}, {"chat_id": "-100222", "topic_id": 1},
    ) == {"chat_id": {"from": "-100111", "to": "-100222"}}
    assert msg_routes.change_summary(
        {"chat_id": "-100111", "topic_id": 1}, {"chat_id": "-100111", "topic_id": 1},
    ) == {}


# ---------------------------------------------------------------------------
# 3. Default is a no-op — measured on the real emitters
# ---------------------------------------------------------------------------

class _Recorder:
    """Records every send. Fixed-keyword signature on purpose: a page that
    started passing an unexpected kwarg would fail here rather than silently
    pass through a mock."""

    def __init__(self) -> None:
        self.sends: list[dict] = []

    def send(self, *, chat_id="", text="", sid="", user="", urgent=False,
             topic_id=None, **extra):
        self.sends.append({"chat_id": chat_id, "text": text, "urgent": urgent,
                           "topic_id": topic_id, **extra})
        return True


@pytest.fixture()
def dm_page(tmp_path, monkeypatch):
    """Drive the real ``_send_stakeholder_dm`` against a recording TG client."""
    from bot_squad_worker import actions as A

    rec = _Recorder()
    monkeypatch.setattr(A, "_get_tg_client", lambda cfg: rec)
    monkeypatch.setattr(A, "_get_max_client", lambda cfg: rec)
    return rec


def test_ssot_without_a_msg_type_never_consults_the_map(tmp_path, dm_page, monkeypatch):
    """The immunity that keeps `bsq tg ping` / `bsq topic say` / the relay
    working: no type named ⇒ the map is not even read."""
    from bot_squad_worker import actions as A

    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "class:log", chat_id="-100999", topic_id=7)
    msg_routes.set_route(cfg, "demo", "class:urgent", chat_id="-100999", topic_id=8)

    def _boom(*a, **k):  # pragma: no cover — must not be reached
        raise AssertionError("an untyped send consulted the destination map")

    monkeypatch.setattr(msg_routes, "route", _boom)
    A._send_stakeholder_dm(cfg, message="hi", tg_chat_id="404580642", slug="demo")
    assert dm_page.sends[-1]["chat_id"] == "404580642"


def test_ssot_with_a_msg_type_and_no_route_is_byte_identical(tmp_path, dm_page):
    from bot_squad_worker import actions as A

    cfg = _cfg(tmp_path)
    A._send_stakeholder_dm(cfg, message="hi", tg_chat_id="404580642",
                           slug="demo", msg_type="task_lifecycle")
    assert (dm_page.sends[-1]["chat_id"], dm_page.sends[-1]["topic_id"]) == (
        "404580642", None)


def test_ssot_applies_a_configured_route(tmp_path, dm_page):
    from bot_squad_worker import actions as A

    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "task_lifecycle",
                         chat_id="-1003761939853", topic_id=517)
    A._send_stakeholder_dm(cfg, message="📋 T-0123 → totest", tg_chat_id="404580642",
                           slug="demo", msg_type="task_lifecycle")
    assert (dm_page.sends[-1]["chat_id"], dm_page.sends[-1]["topic_id"]) == (
        "-1003761939853", 517)


def test_route_slug_reads_the_map_without_claiming_the_project(tmp_path, dm_page):
    """The three install-wide pagers (oauth/autoupdate/outbound_liveness) claim
    no project on purpose but still resolve a chat from one."""
    from bot_squad_worker import actions as A

    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "oauth_expired", chat_id="-100777", topic_id=3)
    A._send_stakeholder_dm(cfg, message="❌ oauth", tg_chat_id="404580642",
                           msg_type="oauth_expired", route_slug="demo")
    assert (dm_page.sends[-1]["chat_id"], dm_page.sends[-1]["topic_id"]) == ("-100777", 3)


def test_a_routed_thread_pins_the_send_to_tg(tmp_path, monkeypatch):
    """A forum thread is a TG-only address — MAX has no notion of one, so a
    failover would deliver a message he routed to [BS] Logs into his MAX DM."""
    from bot_squad_worker import actions as A

    cfg = _cfg(tmp_path)
    cfg.max_default_chat_id = "max-dm"
    msg_routes.set_route(cfg, "demo", "task_lifecycle", chat_id="-100111", topic_id=5)

    tg = _Recorder()
    max_client = _Recorder()

    def _tg_explodes(_cfg):
        class _Boom:
            def send(self, **kw):
                raise RuntimeError("tg down")
        return _Boom()

    monkeypatch.setattr(A, "_get_tg_client", _tg_explodes)
    monkeypatch.setattr(A, "_get_max_client", lambda c: max_client)
    with pytest.raises(RuntimeError):
        A._send_stakeholder_dm(cfg, message="x", tg_chat_id="404580642",
                               slug="demo", msg_type="task_lifecycle")
    assert max_client.sends == [], "a thread-routed page must not fail over to MAX"
    assert tg.sends == []


def test_tg_notify_with_an_explicit_chat_ignores_the_needs_input_route(
    tmp_path, monkeypatch, dm_page,
):
    """`bsq tg ping --chat X` spelled the destination out. A map that overrode
    it would be a bug, not a feature."""
    from bot_squad_worker import actions as A

    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "needs_input", chat_id="-100999", topic_id=9)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(
        A, "_resolve_tmux_session", lambda cfg, slug, sid: "tmux-sess")
    A._action_tg_notify({
        "message": "answer me", "chat_id": "404580642", "slug": "demo",
        "needs_input": True, "sid": "S-demo-dev-p1",
    })
    assert dm_page.sends[-1]["chat_id"] == "404580642"


def test_tg_notify_needs_input_without_an_address_follows_the_route(
    tmp_path, monkeypatch, dm_page,
):
    from bot_squad_worker import actions as A

    cfg = _cfg(tmp_path)
    msg_routes.set_route(cfg, "demo", "needs_input", chat_id="-100999", topic_id=9)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(
        A, "_resolve_tmux_session", lambda cfg, slug, sid: "tmux-sess")
    A._action_tg_notify({
        "message": "answer me", "slug": "demo", "needs_input": True,
        "sid": "S-demo-dev-p1",
    })
    assert (dm_page.sends[-1]["chat_id"], dm_page.sends[-1]["topic_id"]) == (
        "-100999", 9)


# --- the deploy sender: two types out of one call site, T-0188 intact --------

def _deploy_cfg(tmp_path, monkeypatch, *, ok=True, killed_reason=""):
    """A cfg + patched deploy module that runs ``jobs._run_project_deploy``
    through to its notices without touching a real queue or recipe."""
    from bot_squad_worker import deploy as D

    cfg = _cfg(tmp_path, slug="demo")
    queue_head = tmp_path / "q.json"
    queue_head.write_text(json.dumps({"target": "staging"}))
    monkeypatch.setattr(D, "list_queued", lambda c, s: [queue_head])
    monkeypatch.setattr(D, "is_paused", lambda c, s: None)
    monkeypatch.setattr(D, "is_clean_for_target", lambda c, s, t: True)
    monkeypatch.setattr(
        D, "run_next",
        lambda c, s: SimpleNamespace(
            ok=ok, returncode=0 if ok else 1, collapsed_count=1,
            resolved_sha="abc123def456", worker_restart_status="",
            worker_stale=False, worker_boot_sha="", killed_reason=killed_reason,
            log_path="/tmp/x.log",
        ),
    )
    return cfg


def _run_deploy(cfg, monkeypatch, rec):
    from bot_squad_worker import channels as C, jobs as J

    monkeypatch.setattr(C, "get_channel", lambda c, **kw: rec)
    J._run_project_deploy(cfg, "demo", cfg.projects["demo"])


class _ChannelRecorder(_Recorder):
    def send(self, text, *, chat_id="", sid="", urgent=False, topic_id=None, **extra):
        self.sends.append({"text": text, "chat_id": chat_id, "urgent": urgent,
                           "topic_id": topic_id})
        return True


def test_deploy_notices_default_to_todays_destination(tmp_path, monkeypatch):
    cfg = _deploy_cfg(tmp_path, monkeypatch)
    rec = _ChannelRecorder()
    _run_deploy(cfg, monkeypatch, rec)
    assert [s["chat_id"] for s in rec.sends] == ["404580642", "404580642"]
    assert all(s["topic_id"] is None for s in rec.sends)


def test_a_routed_deploy_success_still_fires_in_quiet_hours(tmp_path, monkeypatch):
    """T-0188 THROUGH the reclassification (DoD item 5). `deploy_status` is
    LOG-class, so it is the archetypal thing he will move to [BS] Logs — and
    `urgent=True` must survive that move untouched, because urgent= is the
    quiet-hours BYPASS and has never selected a destination. T-0188 was about
    the gate silently DROPPING deploy alerts; a moved destination is not a drop.
    """
    cfg = _deploy_cfg(tmp_path, monkeypatch)
    msg_routes.set_route(cfg, "demo", "deploy_status",
                         chat_id="-1003761939853", topic_id=517)
    rec = _ChannelRecorder()
    _run_deploy(cfg, monkeypatch, rec)
    assert rec.sends, "the deploy monitor sent nothing"
    for s in rec.sends:
        assert s["chat_id"] == "-1003761939853" and s["topic_id"] == 517
        assert s["urgent"] is True, (
            "T-0188 reversed: a deploy notice lost its quiet-hours bypass when "
            "its DESTINATION was reclassified"
        )


def test_deploy_failure_and_success_are_separately_routable(tmp_path, monkeypatch):
    """Classification by URGENCY, not by emitting subsystem (DoD item 4): one
    sender, and ❌ FAILED must be able to stay on the siren while ✅ SUCCESS
    moves to the log."""
    cfg = _deploy_cfg(tmp_path, monkeypatch, ok=False)
    msg_routes.set_route(cfg, "demo", "deploy_status",
                         chat_id="-1003761939853", topic_id=517)
    rec = _ChannelRecorder()
    _run_deploy(cfg, monkeypatch, rec)
    start = [s for s in rec.sends if "starting" in s["text"]]
    failed = [s for s in rec.sends if "FAILED" in s["text"]]
    assert start and failed
    assert start[0]["chat_id"] == "-1003761939853", "🚚 start is deploy_status"
    assert failed[0]["chat_id"] == "404580642", (
        "❌ FAILED followed deploy_status' route — it must be deploy_failed, or "
        "moving routine deploy chatter would also silence real failures"
    )


# ---------------------------------------------------------------------------
# 4. The enumeration guard
# ---------------------------------------------------------------------------

#: Call sites of ``_send_stakeholder_dm`` that legitimately name NO message
#: type, keyed ``(module, enclosing function)``. Both are conversational rather
#: than automated: the caller has already spelled its destination out, so a map
#: entry would override a stated address.
UNTYPED_SEND_ALLOWED = {
    # The tg_notify ACTION: `bsq tg ping`, `bsq topic say`, the T-0569 relay,
    # admin test pings (T-0665). It tags exactly one type — the `needs_input`
    # page — and does so conditionally, so it is exempt from the blanket check.
    ("actions.py", "_action_tg_notify"),
}

#: Modules whose ``channels.get_channel(...).send`` is an AUTOMATED push and so
#: must resolve through the map, with the function that does it. Every other
#: ``get_channel`` caller is a reply-in-place (inbound command replies, the
#: voice-note ACK, the peer-bus DM mirror) and deliberately stays put.
ROUTED_CHANNEL_SENDERS = {
    ("jobs.py", "_run_project_deploy"),
    ("actions.py", "_action_pause_deploys"),
    ("actions.py", "_action_resume_deploys"),
}


def _msg_type_literals(src: str) -> set[str]:
    """Every string literal passed as ``msg_type=`` anywhere in ``src``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "msg_type":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if sub.value:
                        found.add(sub.value)
    return found


def _routed_type_literals(src: str) -> set[str]:
    """Types named as a positional/keyword arg to ``msg_routes.route(...)`` or
    to a local ``_tg_safe(text, "<type>")``-style sender."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", ""))
        if name not in ("route", "_tg_safe"):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value in msg_routes.TYPES:
                    found.add(arg.value)
    return found


def _all_declared_types() -> set[str]:
    declared: set[str] = set()
    for py in sorted(PKG.glob("*.py")):
        if py.name == "msg_routes.py":
            continue
        src = py.read_text()
        declared |= _msg_type_literals(src) | _routed_type_literals(src)
    return declared


def test_every_registered_type_is_actually_sent_by_some_call_site():
    """A registered type nobody emits is a promise `bsq msg-route list` makes
    and the code does not keep — he would move it and nothing would change."""
    orphans = sorted(set(msg_routes.TYPES) - _all_declared_types())
    assert not orphans, (
        f"registered message type(s) no call site names: {orphans} — either tag "
        f"the emitter with msg_type=, or drop the type from msg_routes.TYPES"
    )


def test_every_named_type_is_registered():
    """The reverse: a typo'd `msg_type="deploy_statuss"` would silently resolve
    to the default for ever."""
    unknown = sorted(_all_declared_types() - set(msg_routes.TYPES))
    assert not unknown, (
        f"call site(s) name unregistered message type(s): {unknown} — add them "
        f"to msg_routes.TYPES (with an urgency class) or fix the spelling"
    )


def _untyped_ssot_calls(module: str, src: str) -> list[tuple[str, int]]:
    """``(enclosing function, lineno)`` for each ``_send_stakeholder_dm(...)``
    call that names no ``msg_type=``."""
    offenders: list[tuple[str, int]] = []

    def walk(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if isinstance(child, ast.Call):
                name = (child.func.attr if isinstance(child.func, ast.Attribute)
                        else getattr(child.func, "id", ""))
                # `notify(...)` counts ONLY in outbound_liveness, which imports
                # the SSOT under that alias so the module can be driven with a
                # fake. Scoping it to that one module rather than matching the
                # name everywhere: `notify` is an obvious name for an unrelated
                # local, and a guard that flagged one would block a peer for a
                # send it has nothing to do with.
                aliases = {"_send_stakeholder_dm"}
                if module == "outbound_liveness.py":
                    aliases.add("notify")
                if name in aliases:
                    if not any(kw.arg == "msg_type" for kw in child.keywords):
                        if (module, func) not in UNTYPED_SEND_ALLOWED:
                            offenders.append((func, child.lineno))
            walk(child, func)

    walk(ast.parse(src), "<module>")
    return offenders


def test_every_automated_pager_names_its_message_type():
    """THE guard. A new automated page added anywhere in the worker goes RED
    here until it declares which type it is — which is the only way the map can
    stay a complete description of what reaches him.

    It leans on the T-0394 SSOT collapse (test_notify_ssot_guard.py): every
    personal page goes through ``_send_stakeholder_dm``, so checking that one
    function's call sites is exhaustive rather than best-effort.
    """
    offenders: dict[str, list[tuple[str, int]]] = {}
    for py in sorted(PKG.glob("*.py")):
        if py.name == "msg_routes.py":
            continue
        bad = _untyped_ssot_calls(py.name, py.read_text())
        if bad:
            offenders[py.name] = bad
    assert not offenders, (
        f"stakeholder page(s) with no msg_type=: {offenders} — name the "
        f"automated type (msg_routes.TYPES) so its destination is configurable, "
        f"or document a genuinely conversational send in UNTYPED_SEND_ALLOWED"
    )


def test_the_automated_channel_senders_resolve_through_the_map():
    """The other seam. ``channels.get_channel(...).send`` bypasses the page SSOT
    entirely, so the three automated pushes that use it are pinned by name — a
    refactor that dropped the ``msg_routes.route`` call from one of them would
    otherwise silently hardcode its destination again."""
    for module, func in sorted(ROUTED_CHANNEL_SENDERS):
        src = (PKG / module).read_text()
        tree = ast.parse(src)
        target = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func),
            None,
        )
        assert target is not None, f"{module}:{func} no longer exists"
        # A CALL to `<something>.route(...)`, not merely the name appearing.
        # Matching the name was this check's first form and it was too wide:
        # stubbing the resolution out while leaving the `from ... import
        # msg_routes as _msg_routes` line above it kept the guard GREEN while
        # the destination went back to being hardcoded. Measured, not imagined —
        # that exact mutation passed (T-0777's scope-your-extractor rule).
        calls_route = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "route"
            and getattr(n.func.value, "id", "").endswith("msg_routes")
            for n in ast.walk(target)
        )
        assert calls_route, (
            f"{module}:{func} sends an automated message through the channel "
            f"abstraction without CALLING msg_routes.route — its destination is "
            f"hardcoded again (T-0799)"
        )
