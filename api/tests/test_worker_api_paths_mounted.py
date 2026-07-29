"""T-0775: every ``/api/...`` path the WORKER names must resolve to a route
this app actually mounts.

Why this test exists
--------------------
Three prompt strings in ``worker/bot_squad_worker/actions.py`` handed a
user-conversation attendant ``GET /api/conversations/<slug>/<gid>/messages``.
``routes_conversations``' routers are included with prefix ``/api/m``
(``app/main.py``), so that path is mounted NOWHERE — a hard 404, measured on
the live install with a valid worker token. The attendant's own role contract
(``resources/roles/user-conversation.md``) had named the correct
``/api/m/worker/conversations/...`` since T-0529, so the git SSOT was right and
the runtime prompt was wrong: an attendant that read its contract worked and
one that trusted its boot prompt did not. That divergence is this repo's top
bug class, and prompt text is where it hides best — no import resolves it, no
type checks it, and the session that hits the dead path takes the contract's
documented "if the API is unreachable, read the store JSONL directly" fallback,
so the failure looks like an expensive choice rather than a broken string.

So the pin is deliberately NOT a hardcoded copy of the right URL — that is the
same class of artefact that broke. It reads the worker's source, extracts the
paths the worker actually emits, and resolves each against the route table of a
real ``build_app()``. A mount prefix that moves, or a new prompt with a typo,
fails here.

Scope: paths in CODE strings only. Docstrings and comments name paths they are
describing (including ones deliberately named as wrong), not paths anyone calls,
and folding them in would force an allowlist — which is the hardcoded copy
again. Method, not the route table, is what this file owns.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from app.main import build_app

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PKG = REPO_ROOT / "worker" / "bot_squad_worker"

# Any /api/... path shape, including f-string placeholders ({slug}) and the
# prose placeholders docstrings use (<version>) — normalised to {} below.
_PATH_RE = re.compile(r"/api/[A-Za-z0-9_\-./{}<>]*")
_PLACEHOLDER_RE = re.compile(r"\{[^}]*\}|<[^>]*>")


def _normalize(path: str) -> str:
    """``/api/releases/<version>`` and ``/api/releases/{version}`` and the
    route's own ``/api/releases/{ver}`` are the same endpoint. Query strings
    are not part of the route (``?thread_id=`` is a parameter of the mounted
    GET, not a different path)."""
    path = path.split("?")[0].rstrip("./`,;:")
    return _PLACEHOLDER_RE.sub("{}", path)


def _code_strings(source: str):
    """Yield ``(lineno, text)`` for every string literal that is CODE.

    Two exclusions, both load-bearing:

    - **docstrings** — prose names paths it is discussing, and this module's own
      subject matter means several of them name paths that are wrong on purpose.
    - **the Constant children of an f-string** — ``ast.walk`` yields both the
      ``JoinedStr`` and its pieces, so without this the fragment before the
      first placeholder (``"...  GET /api/m/worker/conversations/"``) is
      reported as its own truncated path and every real URL gains a phantom
      prefix twin.
    """
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                skip.add(id(body[0].value))
        elif isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            # Placeholders become {} so the result normalises like a route
            # template: f"...{slug}/{gid}/messages" -> "...{}/{}/messages".
            yield node.lineno, "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                for v in node.values
            )


def _scan(files) -> dict[str, list[str]]:
    """``{normalized path: ["<file>:<line>", ...]}`` over the given sources."""
    found: dict[str, list[str]] = {}
    for path in files:
        for lineno, text in _code_strings(path.read_text()):
            for raw in _PATH_RE.findall(text):
                found.setdefault(_normalize(raw), []).append(f"{path.name}:{lineno}")
    return found


def _mounted_paths(tmp_bot_squad: Path, monkeypatch) -> set[str]:
    """Normalised route templates of a real MOTHERSHIP build — the same gate
    ``main.py`` puts the ``/api/m`` routers behind, so the conversation routes
    are present exactly as they are on the live install.

    Both enumerations, unioned, because neither alone is version-stable: on
    fastapi 0.136 (the worker venv) ``app.routes`` holds every route, while on
    0.139 (the api image) it holds 5 and the rest sit behind opaque
    ``_IncludedRouter`` entries with no ``.path``. ``openapi()["paths"]`` finds
    all 121 there — and it is also how the live spec was enumerated when this
    defect was confirmed — but it omits ``include_in_schema=False`` routes,
    which ``app.routes`` would still carry. Taking both means a route has to be
    invisible to BOTH to be missed.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    monkeypatch.setenv("MOTHERSHIP", "1")
    app = build_app()
    direct = {_normalize(r.path) for r in app.routes if hasattr(r, "path")}
    return direct | {_normalize(p) for p in app.openapi()["paths"]}


# The worker tree is a sibling of api/, so it is present in a full checkout
# (CI, and the repo-root docker mount) but NOT under the api-only mount
# `-v "$PWD"/api:/app`. Skipping there is correct — the source genuinely is not
# on disk — but a skip is not coverage, which is why `lint.yml` runs this file
# by name in a job that has the whole repo.
pytestmark = pytest.mark.skipif(
    not WORKER_PKG.is_dir(),
    reason=(
        f"worker source not present at {WORKER_PKG} — run from a full checkout "
        f"(CI, or the repo-root docker mount), not the api-only mount"
    ),
)

CONVERSATION_ROUTE = "/api/m/worker/conversations/{}/{}/messages"
UNMOUNTED_PRE_FIX = "/api/conversations/{}/{}/messages"


def test_every_api_path_the_worker_names_is_mounted(tmp_bot_squad: Path, monkeypatch):
    mounted = _mounted_paths(tmp_bot_squad, monkeypatch)
    found = _scan(sorted(WORKER_PKG.glob("*.py")))

    missing = {p: sites for p, sites in found.items() if p not in mounted}
    assert not missing, (
        "the worker names /api paths this app mounts nowhere — a session handed "
        "one of these gets a 404, not an auth error:\n"
        + "\n".join(f"  {p}  ({', '.join(sorted(set(s)))})" for p, s in sorted(missing.items()))
        + "\n(fix the worker string; do NOT add the path to a route table to "
        "make this pass unless a route is genuinely missing)"
    )


def test_the_scan_actually_finds_the_conversation_paths(tmp_bot_squad: Path, monkeypatch):
    """Control for the test above: a scanner that extracts nothing produces an
    empty ``missing`` and passes exactly like a healthy tree. Pin that it finds
    the real thing, in the several modules that really call it."""
    found = _scan(sorted(WORKER_PKG.glob("*.py")))
    assert CONVERSATION_ROUTE in found, (
        f"scanner found no {CONVERSATION_ROUTE} anywhere in the worker — the "
        f"extraction is broken, not the tree (paths seen: {sorted(found)})"
    )
    files = {site.split(":")[0] for site in found[CONVERSATION_ROUTE]}
    assert "actions.py" in files, f"the T-0775 prompt sites are gone from actions.py: {files}"
    assert len(files) >= 3, f"expected the prompts AND the working callers, got {files}"


def test_scanner_rejects_the_pre_fix_path(tmp_bot_squad: Path, monkeypatch, tmp_path: Path):
    """Positive control (T-0740): a guard that passes with the defect present
    pins nothing. Feed the scanner the exact string the three prompts carried
    before T-0775 and require it to be seen AND rejected."""
    mounted = _mounted_paths(tmp_bot_squad, monkeypatch)
    fake = tmp_path / "actions.py"
    fake.write_text(
        'def _prompt(slug, gid):\n'
        '    """A docstring naming /api/health must NOT be scanned."""\n'
        '    return f"  GET /api/conversations/{slug}/{gid}/messages"\n'
    )
    found = _scan([fake])
    assert UNMOUNTED_PRE_FIX in found, f"scanner missed the defect string: {found}"
    assert "/api/health" not in found, "docstring paths leaked into the scan"
    assert UNMOUNTED_PRE_FIX not in mounted, (
        "the pre-fix path resolves — the route table moved under this test, so "
        "the whole gate above would have passed on the T-0775 defect"
    )


def test_conversation_reads_in_actions_name_only_the_worker_route(tmp_bot_squad: Path, monkeypatch):
    """The ticket's own pin, independent of the general gate: the three
    attendant-facing prompt sites in ``actions.py`` (the T-0676 topic-scoped
    read, the boot prompt, the reuse-path nudge) must all name the worker-token
    route — not the bare prefix (404) and not the session-auth
    ``/api/m/conversations/...`` UI surface, which answers a worker Bearer with
    401 and so trades a loud failure for a quiet one."""
    found = _scan([WORKER_PKG / "actions.py"])
    conversation_paths = {p for p in found if "conversations" in p}
    assert conversation_paths == {CONVERSATION_ROUTE}, (
        f"actions.py names conversation paths other than the worker route: "
        f"{ {p: found[p] for p in conversation_paths} }"
    )
    assert len(set(found[CONVERSATION_ROUTE])) >= 3, (
        f"expected all three T-0775 prompt sites, found {sorted(set(found[CONVERSATION_ROUTE]))}"
    )
