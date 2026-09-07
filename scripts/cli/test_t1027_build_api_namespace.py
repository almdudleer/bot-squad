"""T-1027: `bsq build-api` retains into `handretain-`, never `cert-`.

`cert-` is not storage — it is the OBSERVABLE the T-1001/T-1005 two-deploy
survival check reads, and that check depends on the first `cert-` tag to
appear being one a DEPLOY wrote. `_image_retain_tag` is a pure function of the
image (T-1005: build time + ref, never the clock), so a hand retain that also
wrote `cert-` would compute the exact same tag a deploy later would —
accurate, idempotent, and therefore indistinguishable from real deploy
retention, which is precisely what would mask a broken one. This happened for
real: a lane ran `bsq build-api --no-build` per T-1004's own plan and
contaminated the namespace; the operator removed the tag by hand.

So `cmd_build_api` must retain under a namespace the deploy's reclaim glob
(`bot-squad-api:cert-*`) and the survival check never touch.
"""
from __future__ import annotations

import importlib.util
import os
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_t1027", str(_BSQ_PATH))
bsq = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("bsq_mod_t1027", _loader))
_loader.exec_module(bsq)

REAL_ID = ("sha256:ed6e988565481254409d0d1cc8238e24d29e01f58465baa45c7b4ad8"
           "d9e4b17f")
REAL_CREATED = "2026-09-06T17:51:32.664462466Z"
REAL_SHA = "760c0fe29136324a0146fbb82e1e15ebb1ae8712"
CERT_TAG = "bot-squad-api:cert-20260906T175132Z-760c0fe29136"
HANDRETAIN_TAG = "bot-squad-api:handretain-20260906T175132Z-760c0fe29136"

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


def _build_api_args(**over):
    args = dict(image="bot-squad-api:latest", service="bot-squad-api",
                no_build=True)
    args.update(over)
    return types.SimpleNamespace(**args)


# --- The verb never writes the instrument's namespace -----------------------

def test_build_api_retains_into_handretain_not_cert(docker, capsys):
    bsq.cmd_build_api(_build_api_args())
    assert docker.tags == [[REAL_ID, HANDRETAIN_TAG]]
    out = capsys.readouterr().out
    assert HANDRETAIN_TAG in out


def test_build_api_never_writes_the_cert_glob(docker):
    """The deploy's reclaim loop globs `bot-squad-api:cert-*` and the survival
    check's whole premise is that the first tag there was a deploy's. Neither
    holds if this verb can write one."""
    bsq.cmd_build_api(_build_api_args())
    assert docker.tags, "expected a retain tag to have been written"
    for _, tag in docker.tags:
        assert not tag.split(":", 1)[1].startswith("cert-"), (
            f"build-api wrote into the cert- namespace: {tag}")


def test_a_failed_handretain_still_reports_and_does_not_raise(docker,
                                                               monkeypatch):
    monkeypatch.setenv("STUB_RESOLVES", "0")
    bsq.cmd_build_api(_build_api_args())          # no SystemExit
    assert docker.tags == []


# --- The two namespaces are a real difference, not an assumed one -----------

def test_the_cert_and_handretain_tags_differ_only_by_prefix():
    """A positive control: if `prefix` were ignored, this would still pass —
    so it is not enough that the two tags differ, they must differ BY THE
    PREFIX ONLY, over the same image."""
    info = {"id": REAL_ID, "created": REAL_CREATED, "git_sha": REAL_SHA,
            "repo_tags": ["bot-squad-api:latest"]}
    cert = bsq._image_retain_tag("bot-squad-api:latest", info)
    hand = bsq._image_retain_tag("bot-squad-api:latest", info,
                                  prefix="handretain")
    assert cert == CERT_TAG
    assert hand == HANDRETAIN_TAG
    assert cert != hand
    assert cert.split(":cert-", 1)[1] == hand.split(":handretain-", 1)[1]


def test_retain_image_defaults_to_the_cert_namespace():
    """verify-isolated and the deploy call `_retain_image` with no `prefix` —
    unchanged behaviour is load-bearing, not incidental."""
    info = {"id": REAL_ID, "created": REAL_CREATED, "git_sha": REAL_SHA,
            "repo_tags": ["bot-squad-api:latest"]}
    tag, err = bsq._retain_image("bot-squad-api:latest", info)
    assert err is None
    assert tag == CERT_TAG


def test_retaining_the_same_image_twice_via_build_api_is_one_tag():
    """Idempotence is a real property T-1005 relies on and must survive the
    namespace split: unchanged image bytes retain to the same handretain tag,
    not a new one per run."""
    info = {"id": REAL_ID, "created": REAL_CREATED, "git_sha": REAL_SHA,
            "repo_tags": ["bot-squad-api:latest"]}
    first = bsq._image_retain_tag("bot-squad-api:latest", info,
                                   prefix="handretain")
    second = bsq._image_retain_tag("bot-squad-api:latest", info,
                                    prefix="handretain")
    assert first == second == HANDRETAIN_TAG
