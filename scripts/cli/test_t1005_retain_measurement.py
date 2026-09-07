"""T-1005: the measurement retains its own environment, at measurement time.

T-1001 stopped the DEPLOY from reclaiming the image its own gate was measured
in. The deploy is not the only thing that moves `bot-squad-api:latest` — the
documented api recipe opens with `docker compose build bot-squad-api`, which has
the same effect and had nothing retaining anything.

A wrapper verb around the rebuild only protects someone who types it, and a
control that depends on being read is prose. So the retention is attached to the
MEASUREMENT instead: `bsq verify-isolated` already has to resolve the image id
to pin it, and the tag costs one metadata call. The tool that creates the
obligation is the only way to get a certification at all, so this arm cannot be
routed around — and it makes the remaining hole harmless rather than smaller: a
rebuild by someone who never read the doc can now only orphan an image that NO
CERTIFICATION WAS TAKEN IN.

THE ORDERING IS STRUCTURAL, NOT A CONVENTION. The retain lives inside
`_pin_image_in_cmd`, which MUST run before exec because it rewrites the argv. So
"retained before the run" cannot drift to "retained after" without the pin
drifting with it, and a rebuild landing mid-suite cannot orphan the image the
run is about.

T-1027: this arm tags under `pinned-`, not `cert-`. `cert-` became the
T-1001/T-1005 two-deploy survival check's own observable — a name every real
certification would otherwise write into just by running — and the tag is
minted HERE, before the run, when nobody yet knows whether the suite passes,
so a name implying a verdict (`verified-`) would be a claim the tag cannot
back up. `pinned-` says only what is true at mint time.
"""
from __future__ import annotations

import importlib.util
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_t1005m", str(_BSQ_PATH))
bsq = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("bsq_mod_t1005m", _loader))
_loader.exec_module(bsq)

REAL_ID = ("sha256:ed6e988565481254409d0d1cc8238e24d29e01f58465baa45c7b4ad8"
           "d9e4b17f")
REAL_CREATED = "2026-09-06T17:51:32.664462466Z"
REAL_SHA = "760c0fe29136324a0146fbb82e1e15ebb1ae8712"
EXPECTED_TAG = "bot-squad-api:pinned-20260906T175132Z-760c0fe29136"

REAL_RECIPE = [
    "docker", "run", "--rm", "-e", "BUILD_AT_IMPORT=0",
    "-v", "/home/almdudleer/bot-squad-mgmt:/repo", "-w", "/repo/api",
    "--entrypoint", "python", "bot-squad-api:latest", "-m", "pytest", "-q",
]

STUB_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("\t".join(argv) + "\n")
if argv[:2] == ["image", "inspect"]:
    if os.environ.get("STUB_RESOLVES", "1") != "1":
        print("Error: No such image", file=sys.stderr)
        sys.exit(1)
    env = json.loads(os.environ.get("STUB_ENV_JSON", "[]"))
    tags = json.loads(os.environ.get("STUB_REPO_TAGS", "[]"))
    sys.stdout.write("\t".join([
        os.environ["STUB_ID"], os.environ["STUB_CREATED"],
        json.dumps(env), json.dumps(tags)]) + "\n")
    sys.exit(0)
if argv[:1] == ["tag"]:
    if os.environ.get("STUB_TAG_RC", "0") != "0":
        print("Error response from daemon: no space left on device",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def docker(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_ID", REAL_ID)
    monkeypatch.setenv("STUB_CREATED", REAL_CREATED)
    monkeypatch.setenv(
        "STUB_ENV_JSON",
        '["PATH=/usr/bin", "BUILD_AT_IMPORT=1", '
        f'"BOT_SQUAD_GIT_SHA={REAL_SHA}"]')
    monkeypatch.setenv("STUB_REPO_TAGS", '["bot-squad-api:latest"]')

    class D:
        @property
        def calls(self):
            return [ln.split("\t")
                    for ln in log.read_text().splitlines() if ln]

        @property
        def tags(self):
            return [c[1:] for c in self.calls if c[:1] == ["tag"]]
    return D()


# --- Retention rides the measurement ---------------------------------------

def test_pinning_also_retains_the_image(docker):
    cmd = list(REAL_RECIPE)
    line = bsq._pin_image_in_cmd(cmd, None)
    assert docker.tags == [[REAL_ID, EXPECTED_TAG]]
    assert f"RETAINED as {EXPECTED_TAG}" in line
    assert REAL_ID in cmd and "bot-squad-api:latest" not in cmd


def test_the_environment_line_carries_both_the_id_and_the_retain_tag(docker):
    """One line, both coordinates — the line a reader copies out of a run log
    has to answer 'which image' AND 'can I still get it'."""
    line = bsq._pin_image_in_cmd(list(REAL_RECIPE), None)
    assert "bot-squad-api:latest" in line
    assert REAL_ID in line
    assert REAL_CREATED in line
    assert EXPECTED_TAG in line


def test_a_failed_retain_is_reported_and_does_not_refuse_the_run(docker,
                                                                 monkeypatch):
    """Refusing to run a suite because a tag could not be written would trade a
    measurement for a bookkeeping entry. But the failure rides the ENVIRONMENT
    line, because a certification whose image was not retained is exactly as
    fragile as every certification taken before this existed."""
    monkeypatch.setenv("STUB_TAG_RC", "1")
    cmd = list(REAL_RECIPE)
    line = bsq._pin_image_in_cmd(cmd, None)          # no SystemExit
    assert "NOT RETAINED" in line
    assert "UNCHECKABLE" in line
    assert "no space left on device" in line
    assert REAL_ID in cmd                            # the pin still happened


def test_the_measurement_tool_never_reclaims(docker):
    """A measurement tool that deletes images can destroy the thing it was
    asked to preserve. The deploy stays the single reclaimer, with one bounded
    window."""
    bsq._pin_image_in_cmd(list(REAL_RECIPE), None)
    verbs = [c[0] for c in docker.calls]
    assert "rmi" not in verbs
    assert "prune" not in verbs
    assert "rm" not in verbs


def test_a_containerless_run_retains_nothing_and_says_nothing(docker):
    cmd = ["/repo/worker/.venv/bin/python", "-m", "pytest", "-q"]
    assert bsq._pin_image_in_cmd(cmd, None) is None
    assert docker.tags == []


def test_an_unresolvable_image_still_refuses_before_the_run(docker,
                                                           monkeypatch):
    monkeypatch.setenv("STUB_RESOLVES", "0")
    with pytest.raises(SystemExit) as e:
        bsq._pin_image_in_cmd(list(REAL_RECIPE), None)
    assert e.value.code == bsq.VERIFY_UNMEASURED_RC
    assert docker.tags == []


# --- Naming the tag ---------------------------------------------------------

@pytest.mark.parametrize("ref,expect", [
    ("bot-squad-api:latest", "bot-squad-api"),
    ("bot-squad-api", "bot-squad-api"),
    ("registry.example.com:5000/team/api:v2", "registry.example.com:5000/team/api"),
    ("ghcr.io/org/img@sha256:" + "a" * 64, "ghcr.io/org/img"),
    ("sha256:" + "e" * 64, None),
    ("ed6e98856548", None),
])
def test_the_repository_half_of_a_reference(ref, expect):
    """A registry port is a colon that is NOT a tag separator. Getting this
    wrong names the retain tag after a hostname."""
    assert bsq._image_repo(ref) == expect


def test_a_bare_id_falls_back_to_the_image_own_repo_tags():
    """Someone who passes an id has given us no repository — take the image's
    own, rather than inventing one or silently skipping the retain.

    T-1027: this calls `_image_retain_tag` directly, with no `prefix` — the
    bare DEFAULT, which is `cert-`, deploy-exclusive. Not the same claim as
    the `_pin_image_in_cmd` tests above, which go through verify-isolated and
    now expect `pinned-`."""
    tag = bsq._image_retain_tag("sha256:" + "e" * 64, {
        "id": REAL_ID, "created": REAL_CREATED, "git_sha": REAL_SHA,
        "repo_tags": ["bot-squad-api:latest"]})
    assert tag == "bot-squad-api:cert-20260906T175132Z-760c0fe29136"


def test_an_image_with_no_repository_anywhere_cannot_be_retained():
    assert bsq._image_retain_tag("sha256:" + "e" * 64, {
        "id": REAL_ID, "created": REAL_CREATED, "git_sha": REAL_SHA,
        "repo_tags": []}) is None


@pytest.mark.parametrize("created,expect", [
    ("2026-09-06T17:51:32.664462466Z", "20260906T175132Z"),   # nanoseconds
    ("2026-09-06T17:51:32Z", "20260906T175132Z"),             # no fraction
    ("2026-09-06T19:51:32.5+02:00", "20260906T175132Z"),      # offset -> UTC
    ("", None),
    ("not a date", None),
])
def test_the_build_time_stamp(created, expect):
    """Docker emits NANOseconds and `datetime.fromisoformat` accepts at most
    microseconds. An unparseable value returns None so the caller says
    'unknown' — a stamp taken from the clock instead would name a build time
    the image does not have."""
    assert bsq._image_stamp(created) == expect


def test_an_unstamped_image_is_named_by_its_own_id(docker, monkeypatch):
    monkeypatch.setenv("STUB_ENV_JSON",
                       '["PATH=/usr/bin", "BOT_SQUAD_GIT_SHA=unknown"]')
    bsq._pin_image_in_cmd(list(REAL_RECIPE), None)
    assert docker.tags == [[REAL_ID,
                            "bot-squad-api:pinned-20260906T175132Z-ed6e98856548"]]
