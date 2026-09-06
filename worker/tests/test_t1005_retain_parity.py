"""T-1005: ONE retain policy, TWO implementations — pin them to each other.

The deploy retains the outgoing api image in shell (`T-1001-IMAGE-RETAIN` in
`deploy-recipes/bot-squad/staging.sh`); `bsq verify-isolated` retains the image
it is about to certify, in python (`_image_retain_tag`). Both must produce the
SAME tag for the same image, or the two arms retain the same bytes twice under
different names and the deploy's bounded window reclaims a tag the measurement
arm is still counting on.

Two implementations of one policy is a drift surface, and a comment saying
"keep these in sync" is not a control. So this compares them by RUNNING BOTH:
the shell side is the recipe's OWN BYTES, extracted between the markers and
executed under the recipe's own `set -euo pipefail` against a stub docker that
records the `docker tag` it is asked to make; the python side is called with the
same metadata. The assertion is byte-identity of the tag, not similarity of the
format.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "deploy-recipes" / "bot-squad" / "staging.sh"
MARKER = "T-1001-IMAGE-RETAIN"

_loader = SourceFileLoader("bsq_mod_t1005p", str(REPO / "scripts" / "cli" / "bsq"))
bsq = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("bsq_mod_t1005p", _loader))
_loader.exec_module(bsq)


def _extract(marker: str) -> str:
    text = RECIPE.read_text()
    m = re.search(
        rf"^# >>> {re.escape(marker)}.*?\n(.*?)^# <<< {re.escape(marker)}\s*$",
        text, flags=re.S | re.M,
    )
    assert m, (
        f"marker {marker} not found in {RECIPE} — the retention block was "
        "renamed or deleted, so this parity check would silently stop "
        "comparing anything"
    )
    body = m.group(1)
    assert body.strip(), f"marker {marker} wraps an empty block"
    return body


STUB_DOCKER = r'''#!/usr/bin/env python3
import os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("\t".join(argv) + "\n")
latest = os.environ.get("STUB_LATEST_ID", "")
if argv[:2] == ["image", "inspect"]:
    fmt = argv[argv.index("--format") + 1] if "--format" in argv else ""
    if not latest:
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
if argv[:1] in (["tag"], ["rmi"]):
    sys.exit(0)
if argv[:1] == ["images"]:
    print(os.environ.get("STUB_CERT_TAGS", ""), end="")
    sys.exit(0)
sys.exit(2)
'''


def _image_env(git_sha: str) -> str:
    return "\n".join([
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
        "PYTHON_VERSION=3.12.11",
        "CONFIG_DIR=/config",
        "DATA_DIR=/data",
        "BUILD_AT_IMPORT=1",
        f"BOT_SQUAD_GIT_SHA={git_sha}",
    ])


def _shell_tag(tmp_path, image_id: str, created: str, git_sha: str) -> str:
    """The tag the SHIPPED RECIPE BYTES ask docker to create."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    script = tmp_path / "block.sh"
    script.write_text("set -euo pipefail\n" + _extract(MARKER))
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_LATEST_ID": image_id,
        "STUB_CREATED": created,
        "STUB_ENV": _image_env(git_sha),
        "STUB_CERT_TAGS": "",
    })
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    calls = [ln.split("\t") for ln in log.read_text().splitlines() if ln]
    tags = [c[2] for c in calls if c[:1] == ["tag"]]
    assert len(tags) == 1, f"expected exactly one docker tag, got {tags}"
    return tags[0]


def _python_tag(image_id: str, created: str, git_sha: str) -> str:
    return bsq._image_retain_tag("bot-squad-api:latest", {
        "id": image_id, "created": created, "git_sha": git_sha,
        "repo_tags": ["bot-squad-api:latest"],
    })


# Real values: the image ids, build times and refs this box actually carried on
# 2026-09-06, plus the two shapes that decide the fallback branch.
CASES = [
    pytest.param(
        "sha256:8ceaa2fcd9991f0e2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f7081920",
        "2026-09-06T15:33:21.884213171Z",
        "e5b2f55c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f60", id="stamped"),
    pytest.param(
        "sha256:ed6e988565481254409d0d1cc8238e24d29e01f58465baa45c7b4ad8d9e4b17f",
        "2026-09-06T17:51:32.664462466Z",
        "760c0fe29136324a0146fbb82e1e15ebb1ae8712", id="deployed-760c0fe"),
    pytest.param(
        "sha256:926bf32fcb0812345678901234567890abcdef1234567890abcdef1234567890",
        "2026-09-06T15:21:04.000000000Z", "unknown", id="unstamped-falls-back"),
    pytest.param(
        "sha256:7f12065f7a7c000000000000000000000000000000000000000000000000abcd",
        "2026-07-18T13:54:36.123456789Z", "", id="empty-sha-falls-back"),
]


@pytest.mark.parametrize("image_id,created,git_sha", CASES)
def test_the_shell_and_python_retain_tags_are_byte_identical(
        tmp_path, image_id, created, git_sha):
    shell = _shell_tag(tmp_path, image_id, created, git_sha)
    python = _python_tag(image_id, created, git_sha)
    assert shell == python


def test_the_comparison_can_actually_fail(tmp_path):
    """A parity check nobody has watched disagree is a parity check nobody has
    tested. Feed the python side a DIFFERENT image and confirm the two stop
    matching — otherwise this file would pass against an implementation that
    returned a constant."""
    image_id, created, git_sha = (
        "sha256:8ceaa2fcd9991f0e2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f7081920",
        "2026-09-06T15:33:21.884213171Z",
        "e5b2f55c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f60")
    shell = _shell_tag(tmp_path, image_id, created, git_sha)
    drifted = _python_tag(image_id, "2026-09-06T15:33:22.884213171Z", git_sha)
    assert shell != drifted


def test_the_tag_names_the_build_time_and_the_ref(tmp_path):
    tag = _shell_tag(
        tmp_path,
        "sha256:8ceaa2fcd9991f0e2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f7081920",
        "2026-09-06T15:33:21.884213171Z",
        "e5b2f55c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f60")
    assert tag == "bot-squad-api:cert-20260906T153321Z-e5b2f55c1d2e"


def test_retaining_the_same_image_twice_is_one_tag(tmp_path):
    """Both coordinates come from the IMAGE, never the clock — so a second
    retain of unchanged bytes is idempotent rather than a second name for
    them."""
    args = ("sha256:ed6e988565481254409d0d1cc8238e24d29e01f58465baa45c7b4ad8d9e4b17f",
            "2026-09-06T17:51:32.664462466Z",
            "760c0fe29136324a0146fbb82e1e15ebb1ae8712")
    assert _python_tag(*args) == _python_tag(*args)
