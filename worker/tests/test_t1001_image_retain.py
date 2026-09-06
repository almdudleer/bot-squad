"""T-1001 — the deploy must not reclaim the image its own gate was measured in.

`docker compose build` in the staging recipe moves `bot-squad-api:latest` onto a
new image. Every api certification is taken in that tag, so THE DEPLOY THOSE
CERTIFICATIONS EXISTED TO GATE is the event that makes them uncheckable. Not an
accident that happened once: what happens every time the process is followed
correctly. Measured 2026-09-06 — `docker image inspect` on `926bf32fcb08` and
`8ceaa2fcd999`, the two images that carried an afternoon's api chain, both
return rc 1. Not untagged: ABSENT.

THESE TESTS EXECUTE THE SHIPPED BYTES. The block is extracted from
``deploy-recipes/bot-squad/staging.sh`` between the ``T-1001-IMAGE-RETAIN``
markers and run under the recipe's own ``set -euo pipefail``, against a stub
``docker`` that records what it was asked to do. A paraphrase of the shell in
this file would pass forever while the recipe rotted — and this block runs
exactly once per deploy, in an environment nobody watches, which is the worst
place for an untested paraphrase. If the markers disappear the extractor RAISES
rather than skipping (T-0987: a loss that reports as a skip is a loss nobody
reads).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "deploy-recipes" / "bot-squad" / "staging.sh"
MARKER = "T-1001-IMAGE-RETAIN"

OLD_ID = "sha256:8ceaa2fcd9991f0e2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f7081920"
OLD_SHA = "e5b2f55c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f60"
CREATED = "2026-09-06T15:33:21.884213171Z"
EXPECTED_TAG = "bot-squad-api:cert-20260906T153321Z-e5b2f55c1d2e"

# A real api image's env, in full. Length is the point: the block reads it
# through a pipe under `pipefail`, and a consumer that exited on the first
# match would SIGPIPE `sed` and kill the deploy on exactly this input.
REAL_IMAGE_ENV = "\n".join([
    "PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG=C.UTF-8",
    "GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305",
    "PYTHON_VERSION=3.12.11",
    "PYTHON_SHA256=c30bb24b7f1e8a1b66aeb0d3d3ae1f2e9c0f0e0c3f8e0e0a1b2c3d4e5f607182",
    "CONFIG_DIR=/config",
    "DATA_DIR=/data",
    "WORKER_SOCK=/data/_sock/worker.sock",
    "BUILD_AT_IMPORT=1",
    "COOKIE_SECURE=1",
    f"BOT_SQUAD_GIT_SHA={OLD_SHA}",
])


def _extract(marker: str) -> str:
    text = RECIPE.read_text()
    m = re.search(
        rf"^# >>> {re.escape(marker)}.*?\n(.*?)^# <<< {re.escape(marker)}\s*$",
        text, flags=re.S | re.M,
    )
    assert m, (
        f"marker {marker} not found in {RECIPE} — the retention block was "
        "renamed or deleted, so this test would silently stop testing it"
    )
    body = m.group(1)
    assert body.strip(), f"marker {marker} wraps an empty block"
    return body


STUB_DOCKER = r'''#!/usr/bin/env python3
"""A docker that answers from the environment and records every call."""
import os, sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("\t".join(argv) + "\n")

latest = os.environ.get("STUB_LATEST_ID", "")

if argv[:2] == ["image", "inspect"]:
    ref = argv[2]
    fmt = argv[argv.index("--format") + 1] if "--format" in argv else ""
    if ref == "bot-squad-api:latest" and not latest:
        print(f"Error: No such image: {ref}", file=sys.stderr)
        sys.exit(1)
    if fmt == "{{.Id}}":
        print(latest)
    elif fmt == "{{.Created}}":
        print(os.environ.get("STUB_CREATED", ""))
    elif "Config.Env" in fmt:
        print(os.environ.get("STUB_ENV", ""))
    else:
        sys.exit(1)
    sys.exit(0)

if argv[:1] == ["tag"]:
    sys.exit(0 if os.environ.get("STUB_TAG_RC", "0") == "0" else 1)

if argv[:1] == ["images"]:
    print(os.environ.get("STUB_CERT_TAGS", ""), end="")
    sys.exit(0)

if argv[:1] == ["rmi"]:
    sys.exit(0)

print(f"stub docker: unhandled {argv}", file=sys.stderr)
sys.exit(2)
'''


def _run(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")

    script = tmp_path / "block.sh"
    # The recipe's own shell options. Running the block WITHOUT them would let
    # a pipeline failure pass here and kill the deploy in production.
    script.write_text("set -euo pipefail\n" + _extract(MARKER))

    child = dict(os.environ)
    child.update({
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_LATEST_ID": OLD_ID,
        "STUB_CREATED": CREATED,
        "STUB_ENV": REAL_IMAGE_ENV,
        "STUB_CERT_TAGS": "",
    })
    child.update({k: str(v) for k, v in env.items()})
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=child, cwd=str(tmp_path))
    calls = [ln.split("\t") for ln in log.read_text().splitlines() if ln]
    return proc, calls


def _tagged(calls):
    return [c[1:] for c in calls if c[:1] == ["tag"]]


def _removed(calls):
    return [c[1] for c in calls if c[:1] == ["rmi"]]


# --- The retention itself ---------------------------------------------------

def test_the_outgoing_image_is_tagged_before_the_rebuild(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _tagged(calls) == [[OLD_ID, EXPECTED_TAG]]


def test_the_tag_carries_both_the_build_time_and_the_ref(tmp_path):
    """One coordinate is not enough. The timestamp is what makes the set sort
    chronologically with no side bookkeeping; the ref is what lets a
    certification naming a sha be matched to an image without inspecting every
    candidate."""
    _, calls = _run(tmp_path)
    tag = _tagged(calls)[0][1]
    assert "20260906T153321Z" in tag
    assert OLD_SHA[:12] in tag


def test_the_deploy_log_says_what_was_retained_and_where(tmp_path):
    proc, _ = _run(tmp_path)
    assert EXPECTED_TAG in proc.stdout
    assert "re-runnable" in proc.stdout


def test_it_survives_pipefail_on_a_full_image_env(tmp_path):
    """The env is read through `docker … | sed`. A consumer that stopped at the
    first match would SIGPIPE sed, pipefail would promote that to a non-zero
    pipeline, and `set -e` would kill the deploy — on a perfectly normal image.
    The recipe therefore has no `head -1`, and this is the input that proves it.
    """
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert REAL_IMAGE_ENV.count("\n") >= 10


def test_an_unstamped_image_falls_back_to_its_own_id(tmp_path):
    """A locally built image has BOT_SQUAD_GIT_SHA=unknown. It still gets
    retained — under a name derived from the artefact rather than from a ref it
    cannot name."""
    env = REAL_IMAGE_ENV.replace(f"BOT_SQUAD_GIT_SHA={OLD_SHA}",
                                 "BOT_SQUAD_GIT_SHA=unknown")
    proc, calls = _run(tmp_path, STUB_ENV=env)
    assert proc.returncode == 0, proc.stderr
    tag = _tagged(calls)[0][1]
    assert tag == "bot-squad-api:cert-20260906T153321Z-8ceaa2fcd999"


def test_a_first_build_on_a_fresh_host_is_not_an_error(tmp_path):
    proc, calls = _run(tmp_path, STUB_LATEST_ID="")
    assert proc.returncode == 0, proc.stderr
    assert _tagged(calls) == []
    assert "no outgoing" in proc.stdout


def test_a_failed_retain_warns_and_does_not_redden_the_deploy(tmp_path):
    """Neither retaining nor reclaiming may take a healthy release down —
    but the warning has to say what it costs, or the next reader reads past it.
    """
    proc, _ = _run(tmp_path, STUB_TAG_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "UNCHECKABLE" in proc.stderr


# --- Reclamation ------------------------------------------------------------

def _cert_tags(*stamps):
    return "".join(
        f"bot-squad-api:cert-{s}-abcdef012345 aaaa{i}\n"
        for i, s in enumerate(stamps))


def test_it_reclaims_only_beyond_the_window(tmp_path):
    older = _cert_tags("20260901T101010Z", "20260902T101010Z",
                       "20260903T101010Z", "20260904T101010Z",
                       "20260905T101010Z")
    proc, calls = _run(tmp_path, STUB_CERT_TAGS=older)
    assert proc.returncode == 0, proc.stderr
    assert _removed(calls) == [
        "bot-squad-api:cert-20260902T101010Z-abcdef012345",
        "bot-squad-api:cert-20260901T101010Z-abcdef012345",
    ]


def test_a_set_inside_the_window_loses_nothing(tmp_path):
    """The green control for the test above. Without it, a reclamation that
    removed nothing at all would look identical to one that removed the right
    two."""
    proc, calls = _run(tmp_path, STUB_CERT_TAGS=_cert_tags(
        "20260904T101010Z", "20260905T101010Z"))
    assert proc.returncode == 0, proc.stderr
    assert _removed(calls) == []


def test_it_never_reclaims_the_image_it_just_retained(tmp_path):
    """The outgoing image can sort oldest — a rebuild of an unchanged tree
    keeps the original creation time — and reclaiming it would destroy the
    measurements this deploy was gated on, by the code that exists to keep
    them."""
    tags = ("bot-squad-api:cert-20260101T000000Z-e5b2f55c1d2e "
            + OLD_ID[7:19] + "\n") + _cert_tags(
        "20260904T101010Z", "20260905T101010Z", "20260906T101010Z")
    proc, calls = _run(tmp_path, STUB_CERT_TAGS=tags)
    assert proc.returncode == 0, proc.stderr
    assert _removed(calls) == []
    assert "keeping" in proc.stdout


def test_reclamation_is_announced_rather_than_silent(tmp_path):
    """Silence IS the defect. Nobody noticed the afternoon's images going
    because nothing said so."""
    proc, _ = _run(tmp_path, STUB_CERT_TAGS=_cert_tags(
        "20260901T101010Z", "20260902T101010Z", "20260903T101010Z",
        "20260904T101010Z", "20260905T101010Z"))
    assert "RECLAIMING bot-squad-api:cert-20260901T101010Z" in proc.stdout
    assert "no longer re-runnable" in proc.stdout


# --- The extractor ----------------------------------------------------------

def test_the_extractor_raises_when_the_markers_are_gone():
    with pytest.raises(AssertionError):
        _extract("T-1001-NO-SUCH-MARKER")
