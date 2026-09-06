"""T-1001: a certification names an IMAGE, and the tag it names is a moving referent.

THE FAILURE THIS PINS is not a wrong number and not an unmeasured run — it is a
correct measurement that nobody can ever check again. Every api certification
this fleet takes runs "in `bot-squad-api:latest`"; every staging deploy rebuilds
that tag. So the deploy those measurements EXISTED TO GATE is the event that
destroys them, every time, by design rather than by accident.

Measured 2026-09-06, and it is the reason this file exists: `docker image
inspect` on `926bf32fcb08` and on `8ceaa2fcd999` — the two images that carried a
whole afternoon's api chain — both return rc 1. Not untagged. ABSENT.

Naming the digest in the report was ALREADY the rule and the fleet followed it
all day. It did not help, because an anchor makes a claim CHECKABLE, not
REPRODUCIBLE. What is tested here is the half naming cannot do: the runner
resolves the tag and SUBSTITUTES THE IMMUTABLE ID INTO THE ARGV before exec, so
the id printed beside the verdict is the id that ran rather than the id the tag
pointed at when someone read it.

THE FIXTURE ARGVS ARE THE DOCUMENTED RECIPE, not argvs composed to suit the
parser — including the `--entrypoint python` that AGENT_INSTRUCTIONS prescribes,
which is the exact trap a naive "first bare token after `run`" parse walks into.
`python` is a real image name on plenty of hosts, so that mistake does not fail
loudly: it anchors, confidently, to the wrong image.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_t1001", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_t1001", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


# --- Real argvs -------------------------------------------------------------

# AGENT_INSTRUCTIONS.md, the api-suite recipe, verbatim in argv form.
REAL_API_RECIPE = [
    "docker", "run", "--rm", "-e", "BUILD_AT_IMPORT=0",
    "-v", "/home/almdudleer/bot-squad-mgmt:/repo", "-w", "/repo/api",
    "--entrypoint", "python", "bot-squad-api:latest", "-m", "pytest", "-q",
]
# The same run as a lane actually types it once containers must be nameable
# (operator, 2026-09-06) and with clustered short flags.
REAL_NAMED_RUN = [
    "docker", "run", "--rm", "--name", "t0966-api-cert", "-it",
    "-e", "BUILD_AT_IMPORT=0", "-v", "/repo:/repo", "-w", "/repo/api",
    "--entrypoint", "python", "bot-squad-api:latest", "-m", "pytest", "-q",
]

FAKE_ID = "sha256:ed6e98856548125476a0d9f1a1e0a6de4d5e0ba0f4f6e1a0b39a0d1e7c2f3a4b5"
FAKE_CREATED = "2026-09-06T17:51:32.443091859Z"


def _naive_image_index(cmd):
    """What a parser written the obvious way returns: the first token after
    `run` that does not start with a dash. Present so the tests below assert a
    DIFFERENCE rather than a value — a control that would go green against a
    broken implementation proves nothing."""
    for i, tok in enumerate(cmd[2:], start=2):
        if not tok.startswith("-"):
            return i
    return None


def _naive_image_index_v2(cmd):
    """The obvious FIX to the obvious parse — skip anything that looks like a
    `KEY=value` flag argument. It gets past the env var and lands on the next
    trap."""
    for i, tok in enumerate(cmd[2:], start=2):
        if not tok.startswith("-") and "=" not in tok:
            return i
    return None


# --- Identifying the image --------------------------------------------------

def test_finds_the_image_in_the_documented_api_recipe():
    idx = bsq._docker_run_image_index(REAL_API_RECIPE)
    assert REAL_API_RECIPE[idx] == "bot-squad-api:latest"


def test_two_naive_parses_anchor_to_two_different_wrong_things():
    """Both traps are live in the recipe we ship, and neither fails loudly.

    The first parse anchors to `BUILD_AT_IMPORT=0` — an env VALUE. The obvious
    fix skips `KEY=value` and lands on the bind mount. Skip path-shaped tokens
    too and the next one waiting is `python`, the `--entrypoint` value, which
    resolves as a real image on plenty of hosts: a confident, checkable,
    completely wrong anchor. Three tokens in one shipped recipe, and only the
    flag table tells them apart.
    """
    n1 = _naive_image_index(REAL_API_RECIPE)
    assert REAL_API_RECIPE[n1] == "BUILD_AT_IMPORT=0"
    n2 = _naive_image_index_v2(REAL_API_RECIPE)
    assert REAL_API_RECIPE[n2] == "/home/almdudleer/bot-squad-mgmt:/repo"
    entrypoint_value = REAL_API_RECIPE.index("--entrypoint") + 1
    ours = bsq._docker_run_image_index(REAL_API_RECIPE)
    assert ours not in (n1, n2, entrypoint_value)
    assert REAL_API_RECIPE[ours] == "bot-squad-api:latest"


def test_finds_the_image_past_a_name_and_clustered_short_flags():
    idx = bsq._docker_run_image_index(REAL_NAMED_RUN)
    assert REAL_NAMED_RUN[idx] == "bot-squad-api:latest"


@pytest.mark.parametrize("cmd,expect", [
    (["docker", "run", "--name=cert", "--entrypoint=python",
      "bot-squad-api:latest", "-m", "pytest"], "bot-squad-api:latest"),
    (["docker", "container", "run", "--rm", "bot-squad-api:latest"],
     "bot-squad-api:latest"),
])
def test_inline_values_and_the_container_run_spelling(cmd, expect):
    assert cmd[bsq._docker_run_image_index(cmd)] == expect


def test_an_unrecognised_flag_refuses_rather_than_guessing():
    """A flag this table has never seen may or may not eat the next token, and
    the two readings name different images. Returning None routes the caller to
    a refusal with `--image`; returning a guess would print a confident anchor
    to the wrong artefact, which is worse than printing none."""
    cmd = ["docker", "run", "--brand-new-flag", "somevalue",
           "bot-squad-api:latest", "-m", "pytest"]
    assert bsq._docker_run_image_index(cmd) is None


def test_a_plain_pytest_argv_is_not_a_docker_run():
    assert bsq._docker_run_index(["python", "-m", "pytest", "-q"]) is None
    assert bsq._docker_run_image_index(["python", "-m", "pytest", "-q"]) is None


# --- Pinning ----------------------------------------------------------------

@pytest.fixture
def resolves(monkeypatch):
    seen = {}

    def _fake(ref):
        seen["ref"] = ref
        return {"id": FAKE_ID, "created": FAKE_CREATED}

    monkeypatch.setattr(bsq, "_resolve_image", _fake)
    return seen


def test_pinning_substitutes_the_id_into_the_argv(resolves):
    cmd = list(REAL_API_RECIPE)
    line = bsq._pin_image_in_cmd(cmd, None)
    assert resolves["ref"] == "bot-squad-api:latest"
    # THE POINT OF THE WHOLE TICKET: the run executes an immutable id, so a
    # rebuild of the tag between two stages of one certification cannot move it.
    assert "bot-squad-api:latest" not in cmd
    assert FAKE_ID in cmd
    # Both coordinates in one line, per the rule the fleet already had: the
    # digest beside the verdict, not in a different message.
    assert "bot-squad-api:latest" in line and FAKE_ID in line
    assert FAKE_CREATED in line


def test_pinning_leaves_a_containerless_run_alone(resolves):
    cmd = ["/repo/worker/.venv/bin/python", "-m", "pytest", "-q", "worker/tests"]
    assert bsq._pin_image_in_cmd(cmd, None) is None
    assert cmd == ["/repo/worker/.venv/bin/python", "-m", "pytest", "-q",
                   "worker/tests"]


def test_an_unresolvable_image_refuses_before_the_run(monkeypatch):
    """The refusal is UP FRONT on purpose: a ten-minute suite that is declared
    unmeasured afterwards has spent a contended host for nothing."""
    monkeypatch.setattr(bsq, "_resolve_image", lambda ref: None)
    with pytest.raises(SystemExit) as e:
        bsq._pin_image_in_cmd(list(REAL_API_RECIPE), None)
    assert e.value.code == bsq.VERIFY_UNMEASURED_RC


def test_an_unparseable_docker_run_refuses_and_names_the_escape_hatch(capsys):
    cmd = ["docker", "run", "--brand-new-flag", "v", "bot-squad-api:latest"]
    with pytest.raises(SystemExit) as e:
        bsq._pin_image_in_cmd(cmd, None)
    assert e.value.code == bsq.VERIFY_UNMEASURED_RC
    assert "--image" in capsys.readouterr().err


def test_an_explicit_image_absent_from_the_argv_refuses(resolves):
    """Pinning a reference the run does not use would certify an image that
    never executed — the exact substitution this ticket is about, arriving
    through the fix instead of through the deploy."""
    with pytest.raises(SystemExit) as e:
        bsq._pin_image_in_cmd(list(REAL_API_RECIPE), "bot-squad-api:t0253-test")
    assert e.value.code == bsq.VERIFY_UNMEASURED_RC


def test_an_explicit_image_rescues_an_argv_the_parser_refuses(resolves):
    cmd = ["docker", "run", "--brand-new-flag", "v", "bot-squad-api:latest",
           "-m", "pytest"]
    line = bsq._pin_image_in_cmd(cmd, "bot-squad-api:latest")
    assert FAKE_ID in cmd and "bot-squad-api:latest" not in cmd
    assert FAKE_ID in line


# --- The verdict carries it -------------------------------------------------

GREEN = "1605 passed, 2 skipped in 484.10s\n"
COLLECTION_ABORT = (
    "!!!!!!!!!!!!! Interrupted: 4 errors during collection !!!!!!!!!!!!!\n"
    "2 skipped, 1 warning, 4 errors in 3.10s\n"
)
ENV_LINE = f"verify-isolated: ENVIRONMENT bot-squad-api:latest = {FAKE_ID}"


def test_a_green_verdict_carries_the_environment():
    lines, rc = bsq._certify_pytest_run(GREEN, 0, REAL_API_RECIPE,
                                        "760c0fe29136", ENV_LINE)
    assert rc == 0
    blob = "\n".join(lines)
    assert "MEASURED 1605" in blob
    assert FAKE_ID in blob


def test_a_REFUSAL_carries_the_environment_too():
    """A refusal is a record as much as a pass is, and the first question about
    a run that would not certify is which image it was in."""
    lines, rc = bsq._certify_pytest_run(COLLECTION_ABORT, 2, REAL_API_RECIPE,
                                        "760c0fe29136", ENV_LINE)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    assert FAKE_ID in "\n".join(lines)


def test_a_containerless_run_SAYS_it_pinned_nothing():
    """Silence would read as "no container involved, nothing to anchor". The
    line says the opposite: the packages came from this box and are not pinned
    by anything here."""
    lines, _ = bsq._certify_pytest_run(GREEN, 0,
                                       ["python", "-m", "pytest"], "760c0fe2")
    blob = "\n".join(lines)
    assert "ENVIRONMENT host" in blob
    assert "does NOT pin" in blob
