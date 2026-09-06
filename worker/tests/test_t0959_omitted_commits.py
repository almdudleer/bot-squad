"""T-0959 — a release that leaves committed work behind has to say so.

Two measured facts shape this file.

**The notice existed and reached nobody.** ``deploy.run_next`` has logged
"N commit(s) … will be OMITTED from this release" at WARNING since T-0225. It
fired on every bot-squad staging deploy from 2026-08-18 to 2026-09-06 — nineteen
days — and the divergence it was describing was found by a session tripping over
a missing file, not by anyone reading the journal. A journal is not a human.

**The number it printed was 94% noise.** On 2026-09-06 the shared dev clone was
75 commits ahead of ``origin/bot_squad/dev``. Seventy of those 75 were
byte-identical (same ``git patch-id``) to a commit already on origin, replayed
there by sessions that cut a branch from origin, cherry-picked onto it, pushed,
and never merged back. Four carried content that was genuinely nowhere upstream.
A warning that says "75 commits will be OMITTED" when four are is not a signal a
human can act on; it is a signal a human learns to skip.

So the fix is both halves: count by CONTENT, and put the result where a person
already looks — the deploy's own terminal notice.
"""
from __future__ import annotations

import json as _json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import deploy as D
from bot_squad_worker import intersession as IS
from bot_squad_worker import jobs as J


# ---------------------------------------------------------------------------
# real git repositories — the measurement is about git's own patch equivalence,
# so a fake would only confirm the model that wrote it
# ---------------------------------------------------------------------------
def _git(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {p.stderr}")
    return p


def _commit(repo: Path, name: str, body: str = "x") -> str:
    (repo / name).write_text(body)
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def clones(tmp_path):
    """A bare origin on branch ``work``, an editing clone, and a helper clone
    that can advance origin behind the editing clone's back."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "work")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    _commit(seed, "base.txt")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))

    def _clone(name):
        path = tmp_path / name
        _git(tmp_path, "clone", "-q", str(origin), str(path))
        _git(path, "config", "user.email", "t@t")
        _git(path, "config", "user.name", "t")
        _git(path, "checkout", "-q", "work")
        return path

    return SimpleNamespace(origin=origin, edit=_clone("edit"), other=_clone("other"),
                           tmp=tmp_path, clone=_clone)


# ---------------------------------------------------------------------------
# _unshipped_commits — the unit
# ---------------------------------------------------------------------------
def test_a_clone_in_sync_omits_nothing(clones):
    assert D._unshipped_commits(clones.edit) == []


def test_a_commit_that_is_only_here_is_named_with_its_subject(clones):
    _commit(clones.edit, "stranded.txt")
    out = D._unshipped_commits(clones.edit)
    assert len(out) == 1
    sha, _, subject = out[0].partition(" ")
    assert subject == "add stranded.txt"
    assert _git(clones.edit, "rev-parse", "HEAD").stdout.strip().startswith(sha)


def test_a_commit_replayed_upstream_under_another_sha_is_NOT_omitted(clones):
    """The 70-of-75 case, and the whole reason the old count was unreadable.
    The SHA is local-only; the CONTENT ships. Naming it would be a false alarm,
    and 70 false alarms is how the real one got skipped for nineteen days."""
    local = _commit(clones.edit, "feature.txt", "the work")
    _git(clones.other, "fetch", "-q", str(clones.edit), "work")
    # `-x` appends a "(cherry picked from ...)" trailer: the message — and so
    # the SHA — differs, while the patch-id does not. Without it a replay in
    # the SAME SECOND as the original reproduces the IDENTICAL SHA (same tree,
    # parent, author and committer second), and the fixture silently stops
    # being about a replayed sha at all. Measured: that made this file flaky.
    _git(clones.other, "cherry-pick", "-x", local)
    replayed = _git(clones.other, "rev-parse", "HEAD").stdout.strip()
    _git(clones.other, "push", "-q", "origin", "work")
    assert replayed != local

    assert _git(clones.edit, "rev-list", "--count", "origin/work..HEAD").stdout.strip() == "1", \
        "the SHA-counting instrument still sees it — that is the point"
    assert D._unshipped_commits(clones.edit) == [], \
        "...and the content-counting one correctly does not"


def test_a_mixed_history_reports_only_the_genuinely_unshipped(clones):
    replayed_src = _commit(clones.edit, "replayed.txt", "shipped elsewhere")
    _commit(clones.edit, "stranded-a.txt")
    _commit(clones.edit, "stranded-b.txt")
    _git(clones.other, "fetch", "-q", str(clones.edit), "work")
    _git(clones.other, "cherry-pick", "-x", replayed_src)
    _git(clones.other, "push", "-q", "origin", "work")
    assert _git(clones.other, "rev-parse", "HEAD").stdout.strip() != replayed_src

    out = D._unshipped_commits(clones.edit)
    subjects = {ln.partition(" ")[2] for ln in out}
    assert subjects == {"add stranded-a.txt", "add stranded-b.txt"}


def test_a_stale_remote_tracking_ref_cannot_manufacture_an_omission(clones):
    """``origin/work`` is a LOCAL cache. Without a fetch, a commit the editing
    clone already pushed from elsewhere still looks unshipped — the mirror
    image of the failure this ticket is about, and just as misleading."""
    local = _commit(clones.edit, "pushed.txt")
    _git(clones.other, "fetch", "-q", str(clones.edit), "work")
    _git(clones.other, "push", "-q", str(clones.origin), f"{local}:work")
    cached = _git(clones.edit, "rev-parse", "origin/work").stdout.strip()
    assert cached != local, "fixture broken — the clone already knows"

    assert D._unshipped_commits(clones.edit) == []


def test_it_fails_open_rather_than_wedging_a_deploy(tmp_path):
    """Same contract as ``_local_only_commits``: an unreachable origin or a
    non-repo must not stop releases. It under-reports, loudly in the log, and
    the deploy proceeds."""
    assert D._unshipped_commits(tmp_path / "not-a-repo") == []


def test_a_detached_head_reports_nothing_rather_than_guessing(clones):
    sha = _git(clones.edit, "rev-parse", "HEAD").stdout.strip()
    _commit(clones.edit, "x.txt")
    _git(clones.edit, "checkout", "-q", sha)
    assert D._unshipped_commits(clones.edit) == []


# ---------------------------------------------------------------------------
# the notice a human actually sees
# ---------------------------------------------------------------------------
REQUESTER = "S-almdudleer-dev-p999"


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    """Drive ``_run_project_deploy`` over a REAL ``DeployResult`` and hand back
    the messages each lane received.

    A real dataclass on purpose: ``test_msg_routes.py`` was broken twice by
    hand-mirroring this object (15f4d01), and a mirror is the one place a new
    field can go missing with everything still green.
    """
    from bot_squad_worker import actions as A
    from bot_squad_worker import channels as C
    from bot_squad_worker import dispatch as DISP

    slug = "demo"
    project = SimpleNamespace(slug=slug, tg_chat="404580642", tg_topic_id=None,
                              staging_url="", mothership=False,
                              repo_path=str(tmp_path / "repo"))
    cfg = SimpleNamespace(data_dir=tmp_path / "data", projects={slug: project})
    (Path(cfg.data_dir) / slug / "_chat").mkdir(parents=True)
    log_path = tmp_path / "run.log"
    log_path.write_text("release deployed: abcdef1234\n")
    qfile = tmp_path / "q.json"
    qfile.write_text(_json.dumps({"target": "staging"}))

    lanes: dict[str, list] = {"channel": [], "dm": [], "peer": []}

    class _Chan:
        def send(self, text, *, chat_id="", sid="", urgent=False, topic_id=None, **kw):
            lanes["channel"].append(text)
            return True

    monkeypatch.setattr(D, "list_queued", lambda c, s: [qfile])
    monkeypatch.setattr(D, "is_paused", lambda c, s: None)
    monkeypatch.setattr(D, "is_clean_for_target", lambda c, s, t: True)
    monkeypatch.setattr(C, "get_channel", lambda c, **kw: _Chan())
    monkeypatch.setattr(A, "_send_stakeholder_dm",
                        lambda cfg, **kw: lanes["dm"].append(kw.get("message")) or True)
    monkeypatch.setattr(IS, "send_notice",
                        lambda c, s, frm, to, t: lanes["peer"].append((to, t)) or {"ok": True})
    monkeypatch.setattr(DISP, "live_operator_sids", lambda c, s: ["S-almdudleer-operator-p1"])
    monkeypatch.setattr(J, "_notify_requester", lambda cfg, slug, who, text: None)

    def _run(**fields):
        base = dict(ok=True, returncode=0, queue_id="q-1", log_path=log_path,
                    target="staging", requested_by=REQUESTER, resolved_sha="abcdef1234")
        base.update(fields)
        monkeypatch.setattr(D, "run_next", lambda c, s: D.DeployResult(**base))
        for lane in lanes.values():
            lane.clear()
        J._run_project_deploy(cfg, slug, project)
        return lanes

    return _run


def _said(lanes) -> str:
    said = lanes["channel"] + lanes["dm"] + [t for _, t in lanes["peer"]]
    assert said, "the deploy told nobody anything at all"
    return "\n".join(said)


def test_a_clean_release_says_nothing_about_omissions(deployed):
    text = _said(deployed())
    assert "OMITTED" not in text


def test_a_release_that_left_work_behind_says_so_where_people_read_it(deployed):
    """The fact existed in the journal for nineteen days. It has to be in the
    line that gets read, which is the deploy's own terminal notice."""
    text = _said(deployed(
        omitted_count=2,
        omitted_commits="0b389e4 fix(t0759): the deploy wrapper stopped reddening\n"
                        "0e40426 fix(settings): system_settings.toml comments survive a PUT",
    ))
    assert "OMITTED 2 committed change(s)" in text
    assert "0b389e4" in text and "0e40426" in text
    assert "NOT on the install" in text


def test_the_omission_rides_the_PLAIN_GREEN_success_line(deployed):
    """The branch that matters. A failed deploy gets read; a green one is the
    exact case where "your commit did not ship" would go unnoticed, and it is
    the case that actually happened."""
    lanes = deployed(omitted_count=1, omitted_commits="deadbee fix: a stranded change")
    green = [t for t in lanes["channel"] + lanes["dm"] if "✅" in t]
    assert green, "no plain-success line was emitted at all"
    assert any("OMITTED 1 committed change(s)" in t for t in green)


def test_a_long_omission_list_is_capped_but_says_how_many_it_hid(deployed):
    text = _said(deployed(
        omitted_count=9,
        omitted_commits="\n".join(f"sha{i:04d} subject number {i}" for i in range(9)),
    ))
    assert "OMITTED 9 committed change(s)" in text
    assert "+6 more" in text
    assert "sha0000" in text and "sha0008" not in text


def test_the_field_contract_is_declared_on_the_real_dataclass():
    """T-0880's lesson: a hand-mirrored DeployResult crashed the success path
    on every deploy because two fields existed only on the original."""
    import dataclasses
    declared = {f.name for f in dataclasses.fields(D.DeployResult)}
    assert {"omitted_count", "omitted_commits"} <= declared
    fresh = D.DeployResult(ok=True, returncode=0, queue_id="q", log_path=None)
    assert (fresh.omitted_count, fresh.omitted_commits) == (0, ""), \
        "the defaults must make an unrelated caller's construction silent"
