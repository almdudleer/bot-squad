"""T-0764: pin the TOP-LEVEL SHAPE of every endpoint's success response so a
change to it fails a test *at the moment it is made*.

WHY THIS EXISTS. ``call<T>(path)`` in ``web/src`` is a TYPE ASSERTION, not a
validation — TypeScript erases it, so the shape a caller claims is never
compared against the shape the server sends. T-0601 changed
``list_sessions``'s return annotation from ``list[dict]`` to ``dict`` (the
``{sessions, errors}`` envelope). Every caller kept compiling, kept
type-checking, and the mothership fleet busy-indicator threw
``not iterable`` into its own catch on every tick — clean console, no failing
test, a dead feature on the stakeholder's own install for as long as the
envelope existed (fixed in 75dc01a; this file is about the class).

This test is the FORCING FUNCTION at the API end: the same commit that
flipped that annotation would have gone RED here and told its author to look
at the web callers. Its companion at the web end is
``web/src/apiShapeContract.test.ts``, which reads the very file this test
pins and compares each ``call<T>()`` site's asserted kind against it.

WHAT IS PINNED — and this is the whole limit of the net, stated in words so
nobody reads coverage into it:

  * The pin records the success status code and the TOP-LEVEL KIND of the
    JSON body — array vs object vs a bare scalar vs "any" — and, for objects
    that actually DECLARE fields, their property and required names.
  * Today ZERO of the 150 pinned responses declare fields: FastAPI renders a
    ``-> dict`` annotation as ``{"type": "object", "additionalProperties":
    true}``, recorded here as ``object(loose)`` (120 of 150; 18 are arrays,
    9 are ``any``, 3 have no JSON body). So for those the pin sees NOTHING
    below the top level. A caller asserting the wrong FIELDS of a loose
    object sails through TypeScript, through this test, and through the
    web-side companion.
  * That includes ``GET /api/projects/{slug}/sessions`` — the very endpoint
    whose bug started this ticket. It was caught only because the caller
    said ARRAY. Had T-0601 renamed a key inside the envelope instead of
    adding one, nothing here would have moved.
  * ``title`` is deliberately excluded from the descriptor: FastAPI derives
    it from the handler's function name, so including it would turn every
    rename into a shape change and train readers to refresh the pin without
    reading the diff.

Tightening the response models so the schema stops saying "object, anything
goes" is what would extend this net downward. That is not this ticket.

THE GENERATION ENVIRONMENT IS DECLARED, NOT INHERITED (T-0768). ``build_app``
registers the SPA file-server catch-all ``GET /{full_path}`` CONDITIONALLY, on
``Path(os.environ.get("WEB_DIST", "/app/web/dist")).exists()``. The
``bot-squad-api`` image BAKES a bundle at that default path, so whether the
route exists — and therefore whether it lands in the generated schema —
depended on a detail of how the suite was invoked: the documented api-only
mount (``-v $PWD/api:/app``) SHADOWS ``/app`` and hides the bundle, while the
equally-documented repo-root mount (``-v $PWD:/repo -w /repo/api``) leaves it
visible. Same commit, same code, two answers; and the failure text said
"regenerate the pin", which merely moves the breakage to the other mount.
``_env`` therefore pins ``WEB_DIST`` at a path that cannot exist, so this file
generates from ONE declared environment on every host, and
``_assert_pinned_environment`` reports the CAUSE by name if that assumption
ever stops holding.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app

# The pinned artifact. Lives under api/ so the docker test mount
# (`-v $PWD/api:/app`) reaches it, and is read by the web suite via a
# relative path — see web/src/apiShapeContract.test.ts.
PIN_PATH = Path(__file__).resolve().parents[1] / "response_shapes.json"

REGEN_CMD = (
    "docker run --rm -e BUILD_AT_IMPORT=0 -e UPDATE_RESPONSE_SHAPES=1 "
    '-v "$PWD"/api:/app -w /app --entrypoint python bot-squad-api:latest '
    "-m pytest -q tests/test_response_shape_contract.py"
)

# The SPA file-server catch-all. NOT an API endpoint — it serves index.html —
# so its absence costs this guard no coverage. It is named here because it is
# the one route in the app whose EXISTENCE depends on the environment rather
# than on the code, which is what made this pin mount-sensitive (T-0768).
SPA_CATCHALL = "GET /{full_path}"

# A path that cannot exist, handed to WEB_DIST so the catch-all is
# deterministically NOT registered while shapes are generated. Same posture as
# the rest of the api suite (see test_routes_mothership.py and ~15 others).
ABSENT_WEB_DIST = "nonexistent-web-dist"


# ---------------------------------------------------------------------------
# The reducer: OpenAPI 200 schema -> one readable descriptor string
# ---------------------------------------------------------------------------


def describe_schema(schema: dict | None, components: dict, _seen: frozenset = frozenset()) -> str:
    """Reduce a JSON-Schema node to a compact shape descriptor.

    Descriptors are strings on purpose — they diff sharply in the pin file
    and read as their own failure message ("pinned array(...), now
    object(loose)").

    Every "we cannot tell" outcome is NAMED rather than collapsed to a
    silent default, so a reader of a failure knows which kind of blindness
    they are looking at.
    """
    if schema is None:
        return "no-schema"
    if not isinstance(schema, dict):
        return f"unparseable({type(schema).__name__})"
    if schema == {}:
        # FastAPI emits this for a handler with no return annotation (and for
        # the mothership proxy catch-all). It is a real statement: "any JSON".
        return "any"

    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name in _seen:
            return f"ref({name}:cycle)"
        target = components.get(name)
        if target is None:
            return f"ref({name}:unresolved)"
        return f"ref({name})=" + describe_schema(target, components, _seen | {name})

    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            parts = [describe_schema(s, components, _seen) for s in schema[combinator]]
            return f"{combinator}(" + "|".join(parts) + ")"

    t = schema.get("type")
    if t == "array":
        return "array(" + describe_schema(schema.get("items"), components, _seen) + ")"
    if t == "object":
        props = schema.get("properties") or {}
        if not props:
            # `additionalProperties: true` with no declared properties — the
            # `-> dict` annotation. Nothing below the top level is visible.
            return "object(loose)" if schema.get("additionalProperties") is True else "object(empty)"
        fields = ",".join(
            f"{k}:{describe_schema(v, components, _seen)}" for k, v in sorted(props.items())
        )
        required = ",".join(sorted(schema.get("required") or []))
        return f"object(props={{{fields}}} required={{{required}}})"
    if t is None:
        return "untyped(" + ",".join(sorted(schema)) + ")"
    return str(t)


HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def response_shapes(spec: dict) -> dict[str, str]:
    """``{"GET /api/health": "200 object(loose)", ...}`` for every routed op.

    The SUCCESS response is pinned, whatever its 2xx code — three endpoints
    here answer 201/202/204 and pinning only ``200`` would have recorded them
    as "no response", i.e. blind exactly where a create call's caller sits.
    The code is part of the descriptor so a 200 -> 204 change (which breaks
    any caller doing ``.then(r => r.field)``) also shows up.
    """
    components = (spec.get("components") or {}).get("schemas") or {}
    out: dict[str, str] = {}
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method.lower() not in HTTP_METHODS:
                continue
            key = f"{method.upper()} {path}"
            responses = op.get("responses") or {}
            codes = sorted(c for c in responses if str(c).startswith("2"))
            if not codes:
                out[key] = "no-2xx-response"
                continue
            code = codes[0]
            ok = responses[code]
            content = ok.get("content")
            if not content:
                out[key] = f"{code} no-body"
                continue
            json_ct = content.get("application/json")
            if json_ct is None:
                out[key] = f"{code} non-json(" + ",".join(sorted(content)) + ")"
                continue
            out[key] = f"{code} " + describe_schema(json_ct.get("schema"), components)
    return out


# ---------------------------------------------------------------------------
# Building the schema
# ---------------------------------------------------------------------------


def _env(monkeypatch, root: Path, mothership: str) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(root / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("MOTHERSHIP", mothership)
    # T-0768. DECLARED, never inherited: without this the answer depends on
    # whether the runner can see a built web bundle at WEB_DIST's default
    # (/app/web/dist, which the bot-squad-api image bakes), i.e. on the docker
    # mount. `monkeypatch.setenv` overrides whatever the ambient environment
    # says, which is the point — an inherited value is exactly the bug.
    monkeypatch.setenv("WEB_DIST", str(root / ABSENT_WEB_DIST))


def _assert_pinned_environment(actual: dict[str, str]) -> None:
    """Fail naming the CAUSE if this run is not the environment the pin assumes.

    Without this, a build that registers the SPA catch-all reports as an
    ordinary shape diff ("NEW GET /{full_path} -> 200 no-body") — which reads
    as an API change and invites the one remedy that makes things worse:
    regenerating, which bakes an environment-dependent route into the pin and
    moves the failure to every environment that does NOT have a bundle.
    """
    if SPA_CATCHALL in actual:
        pytest.fail(
            f"NOT the generation environment this pin assumes: {SPA_CATCHALL} is in "
            "the generated schema.\n\n"
            "That route is the SPA file-server catch-all, registered by "
            "app/main.py only when Path(WEB_DIST) EXISTS. This file sets WEB_DIST "
            f"to <tmp>/{ABSENT_WEB_DIST} precisely so it is never registered here, "
            "so seeing it means the registration condition in app/main.py changed "
            "(or something re-set WEB_DIST after the fixture did).\n\n"
            "This is NOT a pin-refresh situation. Regenerating would record a route "
            "whose existence depends on the environment rather than on the code, and "
            "the pin would then fail wherever no web bundle is present (the api-only "
            "docker mount, and CI). Fix the environment or the condition instead."
        )


def _shapes_for(monkeypatch, root: Path, mothership: str) -> dict[str, str]:
    _env(monkeypatch, root, mothership)
    shapes = response_shapes(build_app().openapi())
    _assert_pinned_environment(shapes)
    return shapes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_response_shapes_match_pin(tmp_bot_squad: Path, monkeypatch) -> None:
    """The generated shapes must equal the committed pin.

    Generated from the MOTHERSHIP=1 build, which
    ``test_single_install_is_a_shape_subset`` proves is a strict superset —
    one file therefore covers both builds.
    """
    actual = _shapes_for(monkeypatch, tmp_bot_squad, "1")

    if os.environ.get("UPDATE_RESPONSE_SHAPES") == "1":
        PIN_PATH.write_text(
            json.dumps(
                {
                    "_generated_by": "api/tests/test_response_shape_contract.py"
                    " (UPDATE_RESPONSE_SHAPES=1) — do not hand-edit",
                    "_what_this_pins": "success status code + TOP-LEVEL shape of"
                    " each endpoint's JSON response. object(loose) means the route"
                    " is annotated `-> dict`, i.e. NOTHING below the top level is"
                    " visible here — field-level drift passes this pin and passes"
                    " web/src/apiShapeContract.test.ts. See the module docstring.",
                    "_build": "MOTHERSHIP=1 (superset of the single-install build)"
                    " + WEB_DIST pointed at a path that does not exist, so the SPA"
                    " file-server catch-all GET /{full_path} is deliberately absent."
                    " Both are set by the test fixture, NOT inherited: the catch-all"
                    " is registered only when a web bundle is visible, which used to"
                    " make this pin depend on the docker mount (T-0768).",
                    "shapes": dict(sorted(actual.items())),
                },
                indent=2,
            )
            + "\n"
        )
        pytest.fail(
            f"pin REGENERATED at {PIN_PATH}. Read the diff, then check the web "
            "callers of any endpoint whose shape moved (web/src/api.ts, "
            "web/src/mothership/api.ts) — a raw call<T>() naming the old shape "
            "keeps compiling and fails only at runtime."
        )

    assert PIN_PATH.exists(), (
        f"{PIN_PATH} is missing — the shape pin cannot be checked. Regenerate "
        f"with:\n  {REGEN_CMD}"
    )
    pinned: dict[str, str] = json.loads(PIN_PATH.read_text())["shapes"]

    added = sorted(set(actual) - set(pinned))
    removed = sorted(set(pinned) - set(actual))
    changed = sorted(k for k in set(actual) & set(pinned) if actual[k] != pinned[k])

    if added or removed or changed:
        lines = ["API response shapes moved away from the pin.", ""]
        for k in changed:
            lines.append(f"  CHANGED {k}\n            pinned: {pinned[k]}\n               now: {actual[k]}")
        for k in added:
            lines.append(f"  NEW     {k} -> {actual[k]}")
        for k in removed:
            lines.append(f"  GONE    {k} (was {pinned[k]})")
        lines += [
            "",
            "If this change is intended, the web callers of these endpoints may now",
            "be asserting a shape the server no longer sends — a raw call<T>() will",
            "keep compiling and fail only at runtime, inside its nearest catch.",
            "Check web/src/api.ts and web/src/mothership/api.ts, then regenerate:",
            f"  {REGEN_CMD}",
        ]
        pytest.fail("\n".join(lines))


def test_single_install_is_a_shape_subset(tmp_bot_squad: Path, monkeypatch) -> None:
    """The MOTHERSHIP=0 build must serve a SUBSET of the pinned paths, with
    identical shapes on every path it shares.

    One web bundle talks to both builds, so an endpoint answering differently
    depending on ``MOTHERSHIP`` would be invisible to a pin taken from either
    build alone. (T-0763's actual defect was two fetchers for one endpoint
    that had diverged on one line.)
    """
    single = _shapes_for(monkeypatch, tmp_bot_squad, "0")
    mother = _shapes_for(monkeypatch, tmp_bot_squad, "1")

    extra = sorted(set(single) - set(mother))
    assert not extra, f"single-install build serves paths the mothership build does not: {extra}"

    diverged = {k: (single[k], mother[k]) for k in single if single[k] != mother[k]}
    assert not diverged, f"same endpoint, different shape per build: {diverged}"


def test_pin_is_deterministic(tmp_bot_squad: Path, monkeypatch) -> None:
    """Two generations of the same app must agree.

    A pin that churns on its own would be refreshed reflexively, which is the
    same failure as no pin at all.
    """
    a = _shapes_for(monkeypatch, tmp_bot_squad, "1")
    b = _shapes_for(monkeypatch, tmp_bot_squad, "1")
    assert a == b


def _built_bundle(root: Path) -> Path:
    """A directory that looks enough like `web/dist` to satisfy main.py."""
    bundle = root / "web" / "dist"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "index.html").write_text("<!doctype html><title>bundle</title>")
    return bundle


def test_a_visible_web_bundle_really_does_register_the_catch_all(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """POSITIVE CONTROL for the absence asserted below.

    "The catch-all is not in the pin" is equally consistent with a route that
    can never appear at all — in which case the next test would pass while
    guarding nothing. So drive `build_app` with a bundle that DOES exist and
    assert the route shows up, with the exact descriptor the repo-root docker
    mount produced when this was found: ``200 no-body``.

    `_env` is called first and its WEB_DIST is then deliberately overridden,
    because this test is about the OTHER environment.
    """
    _env(monkeypatch, tmp_bot_squad, "1")
    monkeypatch.setenv("WEB_DIST", str(_built_bundle(tmp_bot_squad)))

    shapes = response_shapes(build_app().openapi())

    assert shapes[SPA_CATCHALL] == "200 no-body"


def test_generation_is_independent_of_the_ambient_web_dist(
    tmp_bot_squad: Path, monkeypatch
) -> None:
    """T-0768: the same commit must yield the same shapes under both docker
    mounts.

    The mount is not the variable — WEB_DIST is; the api-only mount merely
    hides the image's baked bundle from it. So the mount is reproduced HERE by
    driving that variable directly: generate once with a bundle visible (the
    repo-root mount) and once without (the api-only mount) and require the two
    to be identical. RED before this ticket, where the first generation gained
    ``GET /{full_path}``.
    """
    monkeypatch.setenv("WEB_DIST", str(_built_bundle(tmp_bot_squad)))
    as_repo_root_mount = _shapes_for(monkeypatch, tmp_bot_squad, "1")

    monkeypatch.delenv("WEB_DIST")
    as_api_only_mount = _shapes_for(monkeypatch, tmp_bot_squad, "1")

    assert as_repo_root_mount == as_api_only_mount
    assert SPA_CATCHALL not in as_repo_root_mount


def test_the_environment_guard_names_the_variable() -> None:
    """The guard must report the CAUSE, not a shape diff.

    A future change to main.py's registration condition is the one way the
    catch-all can come back; when it does, the message has to say WEB_DIST and
    say "do not regenerate", because the pin's own failure text says the
    opposite and following it inverts which environment is broken.
    """
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _assert_pinned_environment({SPA_CATCHALL: "200 no-body", "GET /api/health": "200 object(loose)"})

    msg = str(excinfo.value)
    assert "WEB_DIST" in msg
    assert "NOT a pin-refresh situation" in msg

    # ...and it stays out of the way of an ordinary shape change.
    _assert_pinned_environment({"GET /api/health": "200 array(object(loose))"})


def test_reducer_tells_the_T_0601_flip_apart() -> None:
    """POSITIVE CONTROL for the instrument itself: reproduce T-0601's exact
    edit on a two-route toy app and assert the descriptors differ.

    Without this, "shapes match the pin" is equally consistent with a reducer
    that returns the same string for everything.
    """
    before = FastAPI()

    @before.get("/api/projects/{slug}/sessions")
    def _list_before(slug: str) -> list[dict]:  # pre-T-0601
        return []

    after = FastAPI()

    @after.get("/api/projects/{slug}/sessions")
    def _list_after(slug: str) -> dict:  # post-T-0601 envelope
        return {"sessions": [], "errors": []}

    key = "GET /api/projects/{slug}/sessions"
    b = response_shapes(before.openapi())[key]
    a = response_shapes(after.openapi())[key]

    assert b == "200 array(object(loose))"
    assert a == "200 object(loose)"
    assert a != b


def test_reducer_names_its_blindness() -> None:
    """The descriptor must SAY "loose" rather than quietly reporting a shape
    it cannot see — an absent field list must not read as "no fields"."""
    components = {"Row": {"type": "object", "properties": {"sid": {"type": "string"}}, "required": ["sid"]}}
    assert describe_schema({"type": "object", "additionalProperties": True}, {}) == "object(loose)"
    assert describe_schema({}, {}) == "any"
    assert describe_schema(None, {}) == "no-schema"
    assert describe_schema({"$ref": "#/components/schemas/Nope"}, {}) == "ref(Nope:unresolved)"
    # ...and where fields ARE declared it reports them, which is the only
    # place this net reaches below the top level.
    assert describe_schema({"$ref": "#/components/schemas/Row"}, components) == (
        "ref(Row)=object(props={sid:string} required={sid})"
    )


def test_the_endpoint_this_ticket_came_from_is_pinned() -> None:
    """The T-0601 envelope is on the pin, as an object.

    Named explicitly so that a future edit dropping this endpoint from the
    pin cannot pass quietly.
    """
    pinned: dict[str, str] = json.loads(PIN_PATH.read_text())["shapes"]
    assert pinned["GET /api/projects/{slug}/sessions"] == "200 object(loose)"


def test_the_pin_carries_no_environment_dependent_route() -> None:
    """The COMMITTED file must not contain the SPA catch-all.

    The guard above protects a generation run; this protects the artifact. It
    is the one that catches the trap this ticket is about — someone hits the
    red test, follows its "regenerate" advice under a mount where the bundle
    is visible, and commits a pin that now fails for everyone whose
    environment has no bundle (the documented api-only mount, and CI).
    """
    pinned: dict[str, str] = json.loads(PIN_PATH.read_text())["shapes"]
    assert SPA_CATCHALL not in pinned, (
        f"{SPA_CATCHALL} is in the committed pin. It is the SPA file-server "
        "catch-all, which app/main.py registers only when a web bundle exists at "
        "WEB_DIST — so a pin containing it passes only where a bundle is present. "
        "This pin is generated with WEB_DIST pointed at nothing on purpose; the "
        "file was almost certainly regenerated in the wrong environment. See the "
        "module docstring (T-0768)."
    )


def test_pinned_json_response_is_reachable_by_the_web_suite() -> None:
    """The web-side companion reads this file by relative path
    (``web/src/..`` -> ``api/response_shapes.json``). If it moves, that test
    must not silently stop checking anything — it asserts existence and fails
    rather than skipping.

    The directory NAME is not asserted here: the docker recipe mounts ``api``
    at ``/app``, so this file's parent is ``api`` on the host and ``app``
    inside the image. What is pinned is that it sits one level above
    ``tests/`` under whatever that root is called.
    """
    assert PIN_PATH.name == "response_shapes.json"
    assert PIN_PATH == Path(__file__).resolve().parent.parent / "response_shapes.json"
    doc = json.loads(PIN_PATH.read_text())
    assert doc["shapes"], "pin is empty"


def test_client_side_smoke_openapi_still_renders(tmp_bot_squad: Path, monkeypatch) -> None:
    """The pin is generated from ``app.openapi()``; keep proving the served
    route agrees (T-0369 pinned that it renders at all)."""
    _env(monkeypatch, tmp_bot_squad, "1")
    app = build_app()
    with TestClient(app) as client:
        served = client.get("/openapi.json")
    assert served.status_code == 200, served.text
    assert response_shapes(served.json()) == response_shapes(app.openapi())
