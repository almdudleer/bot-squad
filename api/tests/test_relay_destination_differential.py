"""T-0749: the API's relay resolver and the WORKER's stores must agree on where
a session reply goes — and this file goes red the moment they don't.

What this pins, and why it is not what the ticket first proposed
---------------------------------------------------------------
Two destination ladders exist (D-0065 enumerates every candidate and confirms
there is no third):

* **A** — ``worker/bot_squad_worker/actions.py::_action_tg_notify``, six rungs
  (T-0723), inputs ``chat_id``/``topic_id``/``ticket_id``/``sid``/``slug``/``topic``.
* **B** — ``api/app/routes_conversations.py::_resolve_relay_target``, four rungs
  (T-0667 + T-0740), inputs ``slug``/``global_user_id``/``thread_id``.

T-0749 proposed driving BOTH over one fixture matrix and asserting identical
destinations. That test cannot be written: ladder A has no ``thread_id`` and no
``global_user_id`` parameter, so it cannot be asked the question ladder B
answers, and their locus rungs are different lookups by design
(``latest_for_slug`` vs the ``slug:gid[:thread_id]`` key). A harness comparing
them would have to invent the mapping between two different parameter sets and
would then pin the invention. See D-0065 §2.

The decision computed twice is one layer BELOW the ladders, and it is where
T-0740's defect actually lived: **the API re-implements the worker's store
reads.** ``_locus_key``'s docstring says it "mirrors
``bot_squad_worker.conversation_locus._key`` exactly"; ``_find_binding``'s says
its "key derivation mirrors ``tg_bindings._key``". Two hand-written copies of a
format the worker OWNS and WRITES. That copy has already failed twice in the
same direction — T-0676 changed the locus key shape, T-0740 found the thread
being discarded outright — and the failure is invisible: the read misses, the
resolver falls through to the user's private DM, and the relay still reports
``relayed: true`` because it did deliver, somewhere.

The mechanism
-------------
Every fixture here is written with the **worker's own writers**
(``tg_bindings.set_binding``, ``conversation_locus.set_locus``) and read back
through the **API's own reader** (``_resolve_relay_target``). Nothing in this
file hand-writes a store key.

That is the whole design, and it is deliberately different from the T-0740
tests in ``test_routes_conversations.py``, which are excellent behaviour pins
but seed the stores by hand — ``_write_binding`` there writes
``f"{chat_id}:{thread_id}"`` under a comment saying it "mirrors what
``tg_bindings.set_binding`` persists". That is a THIRD copy of the format, so
those tests stay GREEN through exactly the worker-side change that breaks
production. This file cannot: change ``tg_bindings._key``, the record shape, or
which fields survive a rebind, and the write lands in the fixture and the api
read stops finding it.

Cross-process constraint (unchanged by this file)
-------------------------------------------------
The worker runs under systemd from ``worker/``; the api runs in docker from
``api/``. Neither package imports the other and neither may — the api reads the
worker's JSON stores directly because ``_relay_to_telegram`` NEVER raises and
NEVER blocks the append, and a worker socket round-trip inside that path would
be a worse defect than the one being fixed (p298 withdrew that bar for T-0740).
The import below is TEST-time only; both store modules are stdlib-clean and the
worker package's ``__init__.py`` is empty, so ``sys.path`` is enough. There is
no runtime dependency and this file adds no production code.

Where it runs
-------------
Skips when the worker tree is absent — which is the dev host's api-only docker
mount. A skip is not coverage, so ``lint.yml`` runs this file from a full
checkout on every push, the T-0775/T-0778 pattern (T-0738's lesson: a guard
only the nightly runs leaves a divergence live for hours).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_TREE = REPO_ROOT / "worker"

pytestmark = pytest.mark.skipif(
    not (WORKER_TREE / "bot_squad_worker" / "tg_bindings.py").is_file(),
    reason=(
        f"worker source not present at {WORKER_TREE} — run from a full checkout "
        f"(lint.yml does; the dev host's api-only docker mount does not)"
    ),
)

if (WORKER_TREE / "bot_squad_worker" / "tg_bindings.py").is_file():
    # APPEND, never insert(0): `worker/tests/` is a real package (it has an
    # `__init__.py`) and `api/tests/` is not, so putting the worker tree FIRST
    # would let a bare `tests` import in this process resolve to the worker's
    # test package. Nothing in the api tree is named `bot_squad_worker`, so the
    # end of the path is enough to reach it and shadows nothing.
    if str(WORKER_TREE) not in sys.path:
        sys.path.append(str(WORKER_TREE))
    from bot_squad_worker import conversation_locus as WLOCUS
    from bot_squad_worker import tg_bindings as WBIND


SLUG = "test-project"
GID = "gu_abc"
DM_CHAT = "404580642"          # the GlobalUser's tg_user_id — ladder B rung 3
TOPIC_CHAT = "-1003761939853"  # a forum supergroup — where topics actually live


# --- the two processes' handles on the SAME data dir -------------------------


def _worker_cfg(tmp_bot_squad: Path):
    """What the worker's store modules need: a ``data_dir``. Both
    ``tg_bindings`` and ``conversation_locus`` read nothing else off cfg."""
    return SimpleNamespace(data_dir=tmp_bot_squad / "data")


def _api_request(tmp_bot_squad: Path, monkeypatch):
    """A Request double over a REAL built app, pointed at the same data dir.

    ``_resolve_relay_target`` reads ``request.app.state.api_config`` and nothing
    else off the request, so this is the whole surface. The app is built for
    real (not stubbed) so the config parsing, the projects table and the users
    store are the production ones.
    """
    from app.main import build_app

    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("WORKER_API_TOKEN", "worker-secret-token-xyz")
    return SimpleNamespace(app=build_app())


def _seed_linked_user(tmp_bot_squad: Path) -> None:
    """A GlobalUser whose DM sits in rung 3, so every topic assertion below is
    non-degenerate: a resolver that silently fell through to the DM (THE
    T-0740 failure) answers ``DM_CHAT``, never the topic chat."""
    from datetime import datetime, timezone

    from app.mothership_users_store import GlobalUser, MothershipUsersStore

    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    store._write_users([
        GlobalUser(
            id=GID,
            username=f"tg:{DM_CHAT}",
            password_hash="",
            created_at=datetime.now(timezone.utc).isoformat(),
            tg_user_id=DM_CHAT,
        )
    ])


def _resolve(request, thread_id):
    from app.routes_conversations import _resolve_relay_target

    return _resolve_relay_target(request, SLUG, GID, thread_id)


# --- the differential --------------------------------------------------------


def test_a_bound_topic_resolves_to_the_chat_the_worker_says_it_is_in(
    tmp_bot_squad: Path, monkeypatch,
):
    """THE T-0740 divergence, driven through the worker's writer.

    The worker binds topic 278 of chat ``TOPIC_CHAT`` to this project. Nobody
    has written there (no locus — by construction, a session opening a fresh
    task topic posts first), and the user has a linked DM. The API must resolve
    the topic; answering ``DM_CHAT`` is the reported bug.

    The worker's own reader is asserted alongside, so a failure names WHICH side
    moved: if ``tg_bindings.resolve`` still finds the record and the API does
    not, the api-side copy of the key derivation has diverged.
    """
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WBIND.set_binding(cfg, TOPIC_CHAT, 278, SLUG)

    assert WBIND.resolve(cfg, TOPIC_CHAT, 278) is not None, (
        "the worker cannot read back its own write — fixture is broken, not the API"
    )

    request = _api_request(tmp_bot_squad, monkeypatch)
    assert _resolve(request, 278) == (TOPIC_CHAT, 278), (
        "the API did not find the binding the worker just wrote; if it answered "
        f"{DM_CHAT!r} this is the T-0740 shape — the thread was discarded and "
        "the reply goes to the private DM while still reporting relayed:true"
    )


def test_a_recorded_locus_resolves_where_the_worker_recorded_it(
    tmp_bot_squad: Path, monkeypatch,
):
    """The T-0676 class: the locus KEY shape.

    The worker records a locus for (slug, gid, thread) via its own writer. The
    API must find that exact entry. When T-0676 changed the key from
    ``slug:gid`` to ``slug:gid:thread_id``, the api-side ``_locus_key`` had to
    be changed in lockstep by hand; this is what makes the next such change
    fail loudly instead of silently falling through to rung 3.
    """
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WLOCUS.set_locus(cfg, SLUG, GID, TOPIC_CHAT, 42)

    assert WLOCUS.get_locus(cfg, SLUG, GID, 42) is not None, "fixture is broken"

    request = _api_request(tmp_bot_squad, monkeypatch)
    assert _resolve(request, 42) == (TOPIC_CHAT, 42)


def test_a_thread_less_locus_resolves_on_the_bare_key(
    tmp_bot_squad: Path, monkeypatch,
):
    """The OTHER key shape the same derivation has to get right: a DM/no-thread
    locus keys to ``slug:gid`` with no third segment. Pinned separately because
    a copy that handled only one of the two shapes would pass the test above."""
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WLOCUS.set_locus(cfg, SLUG, GID, "-100777", None)

    assert WLOCUS.get_locus(cfg, SLUG, GID, None) is not None, "fixture is broken"

    request = _api_request(tmp_bot_squad, monkeypatch)
    assert _resolve(request, None) == ("-100777", None)


def test_the_locus_still_beats_the_binding_when_both_come_from_the_worker(
    tmp_bot_squad: Path, monkeypatch,
):
    """Rung 1 over rung 2, with BOTH stores written by the worker. T-0740's own
    test pins this ordering off hand-written JSON; this one cannot drift from
    the format, so the ordering assertion keeps meaning what it says."""
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WLOCUS.set_locus(cfg, SLUG, GID, "-100777", 7)
    WBIND.set_binding(cfg, "-100999", 7, SLUG)

    request = _api_request(tmp_bot_squad, monkeypatch)
    assert _resolve(request, 7) == ("-100777", 7)


def test_a_general_feed_binding_is_not_a_topic(
    tmp_bot_squad: Path, monkeypatch,
):
    """``thread_id=None`` bound ON PURPOSE (D-0055 §3) is a chat's General feed,
    not a forum topic. The worker keys it with an empty thread segment; a
    reply naming thread 278 must not match it and must fall to the DM."""
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WBIND.set_binding(cfg, TOPIC_CHAT, None, SLUG)

    request = _api_request(tmp_bot_squad, monkeypatch)
    assert _resolve(request, 278) == (DM_CHAT, None)


def test_another_projects_binding_is_never_this_projects_destination(
    tmp_bot_squad: Path, monkeypatch,
):
    """A thread id is only unique WITHIN a chat, so the slug filter is the only
    thing stopping one project's reply landing in another's topic. Written
    through the worker's writer so the ``slug`` field it actually persists is
    the one the API filters on."""
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WBIND.set_binding(cfg, TOPIC_CHAT, 278, "some-other-project")

    request = _api_request(tmp_bot_squad, monkeypatch)
    assert _resolve(request, 278) == (DM_CHAT, None)


def test_a_task_topics_extra_fields_do_not_break_the_read(
    tmp_bot_squad: Path, monkeypatch,
):
    """A T-0660 per-task topic carries ``ticket_id``/``session_id`` beside the
    slug. The API's read must still resolve it — this is the record SHAPE half
    of the contract, distinct from the key half above."""
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WBIND.set_binding(
        cfg, TOPIC_CHAT, 512, SLUG, ticket_id="T-0749", session_id="S-u-dev-p1",
    )

    request = _api_request(tmp_bot_squad, monkeypatch)
    assert _resolve(request, 512) == (TOPIC_CHAT, 512)


def test_every_topic_row_is_distinguishable_from_the_dm_fallback(
    tmp_bot_squad: Path, monkeypatch,
):
    """Anti-vacuity: a resolver that ALWAYS answered the DM must fail this file.

    T-0740 hid behind a matrix that never got past rung 1, so the matrix owes a
    proof that it discriminates. The bound-topic destination and the DM
    destination are asserted DIFFERENT here, and both are reached in the same
    run through the same instrument — so a green file above cannot mean "the
    resolver returns one constant".
    """
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WBIND.set_binding(cfg, TOPIC_CHAT, 278, SLUG)

    request = _api_request(tmp_bot_squad, monkeypatch)
    bound = _resolve(request, 278)
    unbound = _resolve(request, 999)  # no binding, no locus -> rung 3

    assert bound == (TOPIC_CHAT, 278)
    assert unbound == (DM_CHAT, None)
    assert bound != unbound


# --- the contract the append path may never lose (T-0749 DoD item 3) ---------


def test_resolution_never_round_trips_the_worker(tmp_bot_squad: Path, monkeypatch):
    """``_relay_to_telegram`` NEVER raises and NEVER blocks the append, which is
    why the API reads the worker's stores off disk instead of asking the worker.
    Adding a socket round-trip to resolution would be a worse defect than the
    one T-0749 fixes (p298, on T-0740).

    Structural pin, not a behavioural one: the worker router is replaced with an
    object that raises on ANY attribute access, so resolution touching it at all
    — coordinator lookup, client construction, the call itself — fails here.
    """
    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WBIND.set_binding(cfg, TOPIC_CHAT, 278, SLUG)

    class _Trap:
        def __getattr__(self, name):
            raise AssertionError(
                f"destination resolution reached the worker router (.{name}) — "
                "the append path must never round-trip the worker socket"
            )

    request = _api_request(tmp_bot_squad, monkeypatch)
    request.app.state.worker_router = _Trap()

    assert _resolve(request, 278) == (TOPIC_CHAT, 278)


def test_the_relay_never_raises_when_the_worker_call_explodes(
    tmp_bot_squad: Path, monkeypatch,
):
    """The no-raise half of the same contract, at the function boundary.

    ``test_session_append_relay_failure_does_not_fail_append`` covers the
    ``WorkerError`` path through the endpoint; this covers the bare-``Exception``
    branch that exists precisely because a best-effort relay must survive
    anything the transport does — a raise here would 5xx an append that has
    ALREADY durably recorded the reply.
    """
    import asyncio

    from app.routes_conversations import _relay_to_telegram
    from app.worker_client import WorkerClient

    cfg = _worker_cfg(tmp_bot_squad)
    _seed_linked_user(tmp_bot_squad)
    WBIND.set_binding(cfg, TOPIC_CHAT, 278, SLUG)

    async def boom(self, name, params, timeout=None):
        raise RuntimeError("transport exploded in a way nobody predicted")

    monkeypatch.setattr(WorkerClient, "call_action", boom)

    request = _api_request(tmp_bot_squad, monkeypatch)
    relayed, delivery, relay_error = asyncio.run(
        _relay_to_telegram(request, SLUG, GID, "answer", thread_id=278)
    )
    assert relayed is False
    # T-0606 widened the return with WHY it did not deliver. A transport that
    # exploded is still a miss the caller must be able to see — the point of
    # that ticket is that no undelivered reply reports as delivered OR reports
    # nothing at all.
    assert relay_error.get("reason") == "transport_error"
    assert delivery == {}
