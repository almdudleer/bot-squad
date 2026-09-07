"""T-1044: a ticket picked up for live work must leave `planned` on the board.

THE DEFECT. Three code paths put a live session on a ticket, and none of them
wrote the ticket's ``status``. They bound the session (session-md primary /
``extra_task_ids``) and stamped the forensic fields on the ticket
(``session_history``, ``session_history_ts``, ``initiative``) — and left the
board label at whatever it was, which for a freshly filed ticket is
``planned``. Measured on the live board 2026-09-06/07: **4 of 7 live devs sat
on `planned` tickets**, and T-1038 / T-0991 each ran a full lane at
``planned``.

That is not cosmetic. ``planned`` is in ``task_states.PARKED_STATES``, so every
frontmatter-only instrument read work-in-flight as not-started, and a liveness
predicate keyed on the label (``task_alive`` vs ``PARKED_STATES``) would have
exited the working fleet — which is exactly the workaround T-0948 had to adopt
(key liveness on the roster binding, not the label).

THE THREE DOORS, each covered by a test below:

* :func:`sessions.spawn` with a ``task_id`` — ``bsq spawn --fresh``, the API,
  ``recovery``'s respawn, ``autocompact``'s re-drive.
* :func:`sessions.resume` adopting a primary (T-0166) — this is what a plain
  ``bsq spawn <ticket>`` does BY DEFAULT (T-0150 auto-resumes a
  high-confidence expert instead of spawning fresh), so it is the most
  travelled of the three.
* :func:`sessions.bind_task` — a ``[BIND_TASK from stakeholder]``, and the
  in-place repair of an unbound dev's empty primary.

WHAT THE FIX MUST NOT DO is as load-bearing as what it must: a re-drive that
lands on already-delivered or deliberately-parked work must not relabel it.
The eligibility gate is therefore DERIVED from ``task_states.TRANSITIONS``
(plus one justified exclusion) rather than hand-listed, and
:func:`test_stamp_eligibility_is_derived_from_the_transition_graph` pins that
derivation so a state added to the graph later cannot silently fall out of
coverage.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import bot_squad_worker.sessions as S
from bot_squad_worker import frontmatter as _fm
from bot_squad_worker import task_states
from bot_squad_worker.sessions import (
    PaneInfo,
    PICKUP_STAMP_EXCLUDE,
    _stamp_task_in_progress,
    _write_session_metadata,
    bind_task,
    resume,
    spawn,
)

SLUG = "test-project"


# ---------------------------------------------------------------------------
# Fixtures — self-contained on purpose: this file must be runnable on its own
# (a suite transferred as an extract, a single-file re-run) without importing
# another test module's private helpers.
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / SLUG / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / SLUG / "backlog").mkdir(parents=True, exist_ok=True)
    (data_dir / SLUG / "vision" / "initiatives").mkdir(parents=True, exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        f'[projects.{SLUG}]\n'
        f'slug = "{SLUG}"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(
        projects=cfg.projects, data_dir=data_dir, config_dir=cfg_dir,
        tg_bot_token="",
    )


class _FakeTmux:
    """Stateful enough for spawn(): records launch commands, fabricates a
    discoverable pane per ``new-window`` so a real SID is computed."""

    def __init__(self) -> None:
        self.panes: list[PaneInfo] = []
        self.launched: list[str] = []
        self._n = 0

    def run(self, args, **kwargs):
        import subprocess
        if args[:2] == ["tmux", "new-window"]:
            win = cwd = ""
            for i, a in enumerate(args):
                if a == "-n" and i + 1 < len(args):
                    win = args[i + 1]
                if a == "-c" and i + 1 < len(args):
                    cwd = args[i + 1]
            self.launched.append(args[-1])
            self._n += 1
            self.panes.append(PaneInfo(
                pane_id=f"%{self._n}", window=win, pid=str(1000 + self._n),
                cwd=cwd, command="claude",
            ))
        return subprocess.CompletedProcess(args, 0, "", "")

    def list_panes(self):
        return list(self.panes)


@pytest.fixture
def tmux(monkeypatch):
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_run", fake.run)
    monkeypatch.setattr(S, "list_panes", fake.list_panes)
    monkeypatch.setattr(S, "_ensure_project_tmux_session", lambda *a, **k: None)
    monkeypatch.setattr(S, "_enforce_parallel_cap", lambda cfg, slug=None: None)
    monkeypatch.setattr(S, "_enforce_token_cap", lambda cfg: None)
    return fake


def _backlog(cfg) -> Path:
    return cfg.data_dir / SLUG / "backlog"


def _seed_task(cfg, task_id: str, status: str = "planned") -> Path:
    p = _backlog(cfg) / f"{task_id}-thing.md"
    p.write_text(
        f"---\nid: {task_id}\ntitle: a thing\nstatus: {status}\n"
        f"updated: 2026-01-01T00:00:00Z\n---\n\n## Context\n\nbody\n"
    )
    return p


def _meta(cfg, task_id: str) -> dict:
    p = next(_backlog(cfg).glob(f"{task_id}-*.md"))
    meta, _ = _fm.parse_or_none(p.read_text())
    return dict(meta or {})


def _status(cfg, task_id: str) -> str:
    return str(_meta(cfg, task_id).get("status") or "")


def _seed_session(cfg, sid: str, **fields) -> Path:
    meta = {"sid": sid, "status": "active", "window": "dev"}
    meta.update(fields)
    p = S._session_file(cfg.data_dir, SLUG, sid)
    _write_session_metadata(p, meta)
    return p


def _resume_fake_run(repo, new_pane="%20", window="expert"):
    """Minimal tmux fake for a no-prompt resurrect (new window + one pane)."""
    import subprocess
    new_window_called = [False]
    launched: list[str] = []

    def _cp(args, out=""):
        return subprocess.CompletedProcess(args, 0, out, "")

    def fake_run(args, **kwargs):
        if "capture-pane" in args:
            return _cp(args, "❯ \n")
        if "new-window" in args:
            new_window_called[0] = True
            if "-lc" in args:
                launched.append(args[args.index("-lc") + 1])
            return _cp(args)
        if "list-panes" in args:
            if new_window_called[0]:
                return _cp(args, f"{new_pane}|{window}|4250|{repo}|claude\n")
            return _cp(args)
        return _cp(args)

    fake_run.launched = launched
    return fake_run


# ---------------------------------------------------------------------------
# Door 1 — spawn()
# ---------------------------------------------------------------------------

def test_spawn_stamps_a_planned_ticket_in_progress(tmp_path, tmux):
    """The T-1038 / T-0991 specimen: a lane runs while the board says planned."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-1038", status="planned")

    res = spawn(cfg, SLUG, "dev", task_id="T-1038")

    assert res.get("sid"), "spawn should have produced a SID"
    assert _status(cfg, "T-1038") == "in_progress"
    # The binding is still recorded — the label write must not have displaced it.
    assert res["sid"] in _fm.as_list(_meta(cfg, "T-1038").get("session_history"))


def test_spawn_stamps_an_open_ticket_too(tmp_path, tmux):
    """`open` is the other label the operator's report named (`planned`/`open`)."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0100", status="open")
    spawn(cfg, SLUG, "dev", task_id="T-0100")
    assert _status(cfg, "T-0100") == "in_progress"


def test_spawn_bumps_updated_when_it_stamps(tmp_path, tmux):
    """A status move that leaves `updated` at its old value is invisible to the
    staleness readers (`pickup`, `status_deadlines`) that key on it."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0101", status="planned")
    before = _meta(cfg, "T-0101")["updated"]
    spawn(cfg, SLUG, "dev", task_id="T-0101")
    assert _meta(cfg, "T-0101")["updated"] != before


def test_spawn_without_a_task_touches_no_ticket(tmp_path, tmux):
    """HEALTHY CASE: a task-less spawn (TL, attendant, constant-team) is not a
    pickup and must relabel nothing on the board."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0102", status="planned")
    spawn(cfg, SLUG, "dev")
    assert _status(cfg, "T-0102") == "planned"


# ---------------------------------------------------------------------------
# Door 2 — resume() adopting a primary (what plain `bsq spawn <ticket>` does)
# ---------------------------------------------------------------------------

def test_resume_adopting_a_primary_stamps_it_in_progress(tmp_path, monkeypatch):
    """T-0150 + T-0166: `bsq spawn <ticket>` auto-resumes a high-confidence
    expert and that resume ADOPTS the ticket as its primary. Fixing only
    spawn() would have left the DEFAULT dispatch path unfixed."""
    repo = tmp_path / "repo"
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0991", status="planned")
    _seed_session(
        cfg, "S-u-expert-p7", status="suspended", window="expert",
        cwd=str(repo), claude_uuid="u7", task_id="~", last_task_id="T-0001",
        suspended_at="2026-05-10T12:00:00Z",
    )

    monkeypatch.setattr(S, "_run", _resume_fake_run(repo))
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    resume(cfg, SLUG, "S-u-expert-p7", task_id="T-0991")
    assert _status(cfg, "T-0991") == "in_progress"


def test_resume_without_adoption_leaves_other_labels_alone(tmp_path, monkeypatch):
    """A resume of a session that ALREADY holds its primary is not a pickup —
    nothing was taken, so no label moves. In particular a ticket the session
    holds at `to_accept` (delivered, awaiting the operator) must not be dragged
    back to `in_progress` by a routine re-drive."""
    repo = tmp_path / "repo"
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0200", status="to_accept")
    _seed_session(
        cfg, "S-u-expert-p8", status="suspended", window="expert",
        cwd=str(repo), claude_uuid="u8", task_id="T-0200",
        suspended_at="2026-05-10T12:00:00Z",
    )

    monkeypatch.setattr(S, "_run", _resume_fake_run(repo))
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    resume(cfg, SLUG, "S-u-expert-p8", task_id="T-0200")
    assert _status(cfg, "T-0200") == "to_accept"


# ---------------------------------------------------------------------------
# Door 3 — bind_task()
# ---------------------------------------------------------------------------

def test_bind_task_stamps_a_planned_ticket_in_progress(tmp_path, tmux):
    """`[BIND_TASK from stakeholder]` puts a live dev on a second ticket."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0001", status="in_progress")
    _seed_task(cfg, "T-0300", status="planned")
    _seed_session(cfg, "S-u-dev-p1", task_id="T-0001")

    bind_task(cfg, SLUG, "S-u-dev-p1", "T-0300")
    assert _status(cfg, "T-0300") == "in_progress"


def test_bind_task_adopting_an_empty_primary_stamps_too(tmp_path, tmux):
    """T-0525's in-place repair of an unbound dev is a pickup as much as a
    spawn is — the session had nothing and now holds this ticket."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0301", status="planned")
    _seed_session(cfg, "S-u-dev-p2", task_id="~", window="dev")

    bind_task(cfg, SLUG, "S-u-dev-p2", "T-0301")
    assert _status(cfg, "T-0301") == "in_progress"


# ---------------------------------------------------------------------------
# The gate — what must NOT be relabelled
# ---------------------------------------------------------------------------

#: status on disk -> status after a pickup lands on it. Written as a literal
#: map, not derived from the code under test, so this table is an independent
#: statement of intent rather than a restatement of the implementation.
_EXPECTED: dict[str, str] = {
    "planned": "in_progress",          # the reported defect
    "open": "in_progress",             # queued, now taken
    "reopened": "in_progress",         # live again, now taken
    "paused": "in_progress",           # task_gc parked it; a holder is back
    "in_progress": "in_progress",      # no-op
    "blocked_on_user": "blocked_on_user",  # excluded: only the answer clears it
    "to_accept": "to_accept",          # delivered — no edge, never clobbered
    "totest": "totest",                # the human's queue — hands off
    "closed": "closed",                # terminal
}


@pytest.mark.parametrize("before,after", sorted(_EXPECTED.items()))
def test_stamp_gate_publishes_the_resulting_status(tmp_path, before, after):
    """Assert the VALUE the ticket ends at, per starting status — not a boolean
    'it was refused'. A gate reported as a verdict cannot be re-checked."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0400", status=before)
    _stamp_task_in_progress(_backlog(cfg), "T-0400")
    assert _status(cfg, "T-0400") == after


def test_stamp_covers_every_status_in_the_enum():
    """The table above must not silently stop covering the enum — a status added
    to `TICKET_STATUSES` with no expectation here is an untested pickup case."""
    assert set(_EXPECTED) == set(task_states.TICKET_STATUSES)


def test_stamp_eligibility_is_derived_from_the_transition_graph():
    """CONDITION THE GUARD ON THE PROPERTY. Eligibility is
    ``is_valid_transition(cur, 'in_progress')`` minus an explicit, justified
    exclusion — not a hand-listed set that rots when the graph changes. This
    pins the derivation itself, so a new state with a legal edge to
    `in_progress` is covered without anyone remembering to edit the stamp.
    """
    for status in task_states.TICKET_STATUSES:
        eligible = (
            status != "in_progress"
            and status not in PICKUP_STAMP_EXCLUDE
            and task_states.is_valid_transition(status, "in_progress")
        )
        expected_moves = _EXPECTED[status] != status
        assert eligible == expected_moves, (
            f"{status!r}: derived eligibility {eligible} disagrees with the "
            f"expectation table ({status!r} -> {_EXPECTED[status]!r})"
        )


def test_the_exclusion_set_only_holds_states_the_graph_would_have_allowed():
    """An exclusion naming a state that has no edge to `in_progress` anyway is
    dead prose pretending to be a guard — it would read as protection while
    protecting nothing."""
    for status in PICKUP_STAMP_EXCLUDE:
        assert task_states.is_valid_transition(status, "in_progress"), (
            f"{status!r} is excluded from the pickup stamp but the transition "
            "graph already forbids the move — the exclusion is doing no work"
        )


def test_stamp_leaves_updated_alone_when_it_refuses(tmp_path):
    """A refused stamp must be a NO-OP on the file, not a rewrite that bumps
    `updated` — otherwise every re-drive of a delivered ticket resets the
    staleness clock the operator's queue is ranked by."""
    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0401", status="to_accept")
    before = _meta(cfg, "T-0401")["updated"]
    _stamp_task_in_progress(_backlog(cfg), "T-0401")
    assert _meta(cfg, "T-0401")["updated"] == before


def test_stamp_on_a_missing_ticket_is_a_quiet_no_op(tmp_path):
    """Best-effort by contract: the binding is the source of truth and a label
    write must never raise into a spawn that has already opened a pane."""
    cfg = _make_cfg(tmp_path)
    assert _stamp_task_in_progress(_backlog(cfg), "T-9999") is None


def test_stamp_on_an_unparseable_ticket_is_a_quiet_no_op(tmp_path):
    cfg = _make_cfg(tmp_path)
    (_backlog(cfg) / "T-0402-broken.md").write_text("no frontmatter here\n")
    assert _stamp_task_in_progress(_backlog(cfg), "T-0402") is None


# ---------------------------------------------------------------------------
# Door 4 — morph_session()
# ---------------------------------------------------------------------------

def test_morph_to_dev_with_a_task_stamps_it_in_progress(tmp_path, tmux):
    """T-0509's own words for what a morph is: "it can take on a task and
    become dev". Adopting a primary is a pickup whether the session arrived
    by spawn or by walking in as a user session."""
    from bot_squad_worker.sessions import morph_session

    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0500", status="planned")
    _seed_session(cfg, "S-u-user_session-p9", window="user_session",
                  role="user-conversation", task_id="~")

    morph_session(cfg, SLUG, "S-u-user_session-p9", "dev", task_id="T-0500")
    assert _status(cfg, "T-0500") == "in_progress"


@pytest.mark.parametrize("role", ["operator", "user-conversation"])
def test_a_morph_that_cannot_hold_a_ticket_stamps_nothing(tmp_path, tmux, role):
    """The two roles whose morph branch CLEARS the primary are not pickups, and
    the label must not move for them.

    What the run actually shows is stronger than the branch: morph REFUSES a
    ticket-carrying morph into either role outright (T-0523 for the operator,
    T-0932 for a user-conversation), so the clearing branch is unreachable with
    a real `task_id`. The stamp's exclusion of those roles is therefore
    defence-in-depth — this pins the behaviour that is actually load-bearing,
    which is the refusal, and that the ticket is untouched by it."""
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker.sessions import morph_session

    cfg = _make_cfg(tmp_path)
    _seed_task(cfg, "T-0501", status="planned")
    _seed_session(cfg, "S-u-user_session-p10", window="user_session",
                  role="user-conversation", task_id="~")

    with pytest.raises(ActionError):
        morph_session(cfg, SLUG, "S-u-user_session-p10", role, task_id="T-0501")
    assert _status(cfg, "T-0501") == "planned"


# ---------------------------------------------------------------------------
# The write must not damage the ticket it labels
# ---------------------------------------------------------------------------

#: A real 204-char title from the live board (T-1003). Composed from the
#: PRODUCER rather than hand-typed: a title this long is what pyyaml's default
#: 80-column width folds, and a hand-written short one would pin nothing.
_LONG_TITLE = (
    "the api/worker task_states byte-identity guard can never run in the same "
    "invocation as the pace mirror guard: they derive the worker tree from "
    "parents[3] vs parents[2], so each mount satisfies exactly one"
)


def _find_bsq_cli() -> Path | None:
    """Locate `scripts/cli/bsq` by walking UP from this file.

    Not `parents[2]`: that is right in the clone and wrong everywhere else. A
    frozen extract of `worker/` has no sibling `scripts/`, and the first cut of
    this test failed there — which made a mutation control on the width pin
    unreadable, because the healthy case was already red. Returns None when the
    CLI is genuinely absent; the load-bearing assertion below does not need it.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "cli" / "bsq"
        if candidate.is_file():
            return candidate
    return None


def _seed_long_title_ticket(cfg) -> Path:
    p = _backlog(cfg) / "T-0600-long.md"
    p.write_text(
        f'---\nid: T-0600\ntitle: "{_LONG_TITLE}"\nstatus: planned\n'
        f"updated: 2026-01-01T00:00:00Z\n---\n\nbody\n"
    )
    return p


def test_a_stamp_never_folds_a_frontmatter_value_across_lines(tmp_path):
    """T-1044's own amplification hazard, pinned WITHOUT leaving this tree.

    Stamping rewrites the ticket through `frontmatter.dump`, and pyyaml folds a
    scalar past 80 columns by default. The fold is valid YAML and the shared
    reader round-trips it — but the board's other readers are line-based
    ("key: value lines only", `scripts/cli/bsq`), so a folded title reaches
    them truncated at the fold with the opening quote still attached. Measured
    on T-1003's real 204-char title before the width pin: 204 chars in, 84 out,
    beginning ``"'the api/worker …``.

    The property those readers actually need is that every frontmatter line
    STARTS a key — no continuation lines. That is what this asserts, and it
    holds in any tree, extract included.
    """
    cfg = _make_cfg(tmp_path)
    p = _seed_long_title_ticket(cfg)
    _stamp_task_in_progress(_backlog(cfg), "T-0600")

    text = p.read_text()
    block = text.split("---\n", 2)[1]
    folded = [ln for ln in block.splitlines()
              if ln and not ln[0].isalnum() and not ln.startswith("  ")]
    continuations = [ln for ln in block.splitlines() if ln.startswith(" ")]
    assert _status(cfg, "T-0600") == "in_progress"
    assert not folded, f"non-key frontmatter lines: {folded}"
    # `session_history_ts` is a legitimate nested block; this ticket has none,
    # so ANY indented line here is a fold.
    assert not continuations, (
        f"the dumped scalar was folded across lines: {continuations}"
    )


def test_bsq_own_reader_reads_the_full_title_after_a_stamp(tmp_path):
    """The same defect at the SURFACE that reported it — `bsq`'s own reader,
    executed from the CLI file rather than reimplemented here, because the
    whole point is that its reader disagrees with the pyyaml writer.

    Skipping when the CLI is absent hides nothing: the assertion above holds
    the finding, and this one corroborates it on the real consumer.
    """
    import importlib.util

    cli = _find_bsq_cli()
    if cli is None:
        pytest.skip("scripts/cli/bsq not in this tree (frozen worker extract)")

    def read_frontmatter(path: Path) -> dict:
        spec = importlib.util.spec_from_loader("_bsq_cli_under_test", None)
        mod = importlib.util.module_from_spec(spec)
        # T-1049: supply `__file__`. `exec` does not define it, and the real
        # CLI needs it — `read_frontmatter` resolves a sibling module via
        # `Path(__file__).resolve().parent` (T-1047's reader fix). Without it
        # the exec raises NameError and this test goes red for a reason
        # unrelated to the code under test. Caught before that change landed.
        mod.__dict__["__file__"] = str(cli)
        src = cli.read_text().replace('if __name__ == "__main__":\n    main()', "")
        try:
            exec(compile(src, str(cli), "exec"), mod.__dict__)  # noqa: S102
        except SyntaxError as exc:
            # `scripts/cli/bsq` is a LIVE shared file every session executes; a
            # peer mid-edit hands us a half-written module. That is a fact about
            # the tree, not about the stamp, and it must not read as this test
            # failing. Skipping here hides nothing — the fold assertion in
            # `test_a_stamp_never_folds_a_frontmatter_value_across_lines` is
            # what holds the finding; this test only corroborates it on the
            # real consumer.
            pytest.skip(f"scripts/cli/bsq is not importable right now: {exc}")
        return mod.read_frontmatter(path)

    cfg = _make_cfg(tmp_path)
    p = _seed_long_title_ticket(cfg)
    assert read_frontmatter(p)["title"] == _LONG_TITLE, "control: pre-stamp read"

    _stamp_task_in_progress(_backlog(cfg), "T-0600")

    after = read_frontmatter(p)
    assert after["status"] == "in_progress"
    assert after["title"] == _LONG_TITLE, (
        f"bsq read {len(str(after['title']))} of {len(_LONG_TITLE)} title chars "
        "— the dumped scalar was folded across lines"
    )


def test_a_stamp_leaves_the_body_and_every_other_field_untouched(tmp_path):
    """The ticket body carries `## Stakeholder notes` — human-only text — and
    the other frontmatter keys are other sessions' writes. A label move must
    touch `status` and `updated`, and nothing else."""
    cfg = _make_cfg(tmp_path)
    p = _backlog(cfg) / "T-0601-rich.md"
    p.write_text(
        "---\n"
        "id: T-0601\n"
        f'title: "{_LONG_TITLE}"\n'
        "status: planned\n"
        "session_history: [S-a-dev-p1, S-a-dev-p2]\n"
        "session_history_ts:\n"
        "  S-a-dev-p1: 2026-09-01T00:00:00Z\n"
        "  S-a-dev-p2: 2026-09-02T00:00:00Z\n"
        "blocked_by: [T-0002]\n"
        "priority: p1\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "---\n\n"
        "## Stakeholder notes\n\nего слова, дословно\n\n## Context\n\nstate\n"
    )
    before_meta, before_body = _fm.parse_or_none(p.read_text())

    _stamp_task_in_progress(_backlog(cfg), "T-0601")

    after_meta, after_body = _fm.parse_or_none(p.read_text())
    assert after_body == before_body
    moved = {k for k in set(before_meta) | set(after_meta)
             if before_meta.get(k) != after_meta.get(k)}
    assert moved == {"status", "updated"}, f"unexpected field drift: {moved}"
