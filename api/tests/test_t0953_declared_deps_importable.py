"""T-0953: every dependency api/pyproject.toml declares must import in THIS image.

`bot-squad-api:latest` bakes deps at build time. Before T-1004 that meant a
new `dependencies = [...]` entry only reached the image on the next rebuild —
and nothing said so: `tomlkit` sat in `pyproject.toml` (T-0912) while the
cached image predated it, and every containerised run failed 947 tests at
`ModuleNotFoundError`, one anonymous failure per test file that happened to
import it, rather than one named failure that says what's actually wrong.

T-1004's lock closes a NARROWER gap than this one: `api/Dockerfile` installs
`-r requirements.lock` and then `--no-deps -e .`, so `pip freeze` can only
ever equal the lock — the two are compared to each other, not to what
`pyproject.toml` actually declares. A dependency added to `dependencies`/
`optional-dependencies.dev` without a matching `requirements.lock`
regeneration installs NOTHING for it and the lock-drift guard stays green,
because nothing in that guard reads `pyproject.toml` at all. This test is
that missing comparison.

Method matches the operator's constraint on this class of check (T-0953):
build the expected-imports list from what the CONTAINER actually has
installed, not by grepping `import` statements out of app code — a grep
sees only what today's app code happens to use directly and misses a
dependency nothing imports yet, or one only a transitive path reaches.
`importlib.metadata.packages_distributions()` inverts the installed
distributions' own metadata into {import name: [distribution names]}, which
is the same source `pip freeze` reads from — so this check and the T-1004
lock diff are asking the SAME clean-venv question from two directions.
"""

from __future__ import annotations

import importlib
import re
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared_dependency_specs() -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text())
    project = data["project"]
    specs = list(project.get("dependencies", []))
    for extra_specs in project.get("optional-dependencies", {}).values():
        specs.extend(extra_specs)
    return specs


def _distribution_name(spec: str) -> str:
    # "uvicorn[standard]>=0.29" / "pytest-asyncio>=0.23" / "foo>=1; python_version<'3.12'"
    spec = spec.split(";", 1)[0].strip()
    return re.split(r"[\[<>=!~ ]", spec, 1)[0]


def _dist_to_imports(pkg_dist_map: dict[str, list[str]]) -> dict[str, set[str]]:
    """Invert `packages_distributions()`'s {import name: [dist names]} into
    {normalized dist name: {import names}} — pulled out of the test below so
    the RED CONTROL can feed it a synthetic map instead of the real installed
    set, and so drive the exact same code the real check runs.
    """
    dist_to_imports: dict[str, set[str]] = {}
    for import_name, dists in pkg_dist_map.items():
        for dist in dists:
            dist_to_imports.setdefault(_normalize(dist), set()).add(import_name)
    return dist_to_imports


def _find_problems(specs, dist_to_imports, import_fn=importlib.import_module) -> list[str]:
    """The actual comparison, as a pure function of its inputs. Real test
    below calls it with the real installed set and the real importer; the red
    control calls it with a synthetic map and a fake importer so it can prove
    a failure WITHOUT needing a broken image to fail against.
    """
    problems = []
    for spec in specs:
        dep = _distribution_name(spec)
        import_names = dist_to_imports.get(_normalize(dep))
        if not import_names:
            problems.append(f"{dep!r} (from {spec!r}): no installed distribution — "
                             f"declared in pyproject.toml but never reached this image")
            continue
        errors = []
        for mod in sorted(import_names):
            try:
                import_fn(mod)
                break
            except Exception as exc:  # noqa: BLE001 — report the real cause, not just "failed"
                errors.append(f"{mod}: {exc!r}")
        else:
            problems.append(f"{dep!r} (from {spec!r}): installed but none of "
                             f"{sorted(import_names)} import cleanly — {errors}")
    return problems


def test_the_declared_set_is_not_empty():
    """CHECK THE INSTRUMENT CAN SEE A ONE BEFORE BELIEVING ITS ZERO (same
    control T-1004's own parser test uses). If `_declared_dependency_specs`
    silently returned nothing, the real test below would pass over an empty
    set and report full coverage of a set it never looked at."""
    specs = _declared_dependency_specs()
    assert len(specs) >= 10, f"only {len(specs)} declared specs parsed"
    assert any(s.startswith("tomlkit") for s in specs)


def test_the_check_can_actually_fail_on_a_missing_distribution():
    """RED CONTROL, no container needed: a dependency with NO installed
    distribution at all — the exact tomlkit incident — must be named, not
    swallowed."""
    problems = _find_problems(
        specs=["totally-not-installed-xyz>=1"],
        dist_to_imports={},
    )
    assert len(problems) == 1
    assert "totally-not-installed-xyz" in problems[0]
    assert "no installed distribution" in problems[0]


def test_the_check_can_actually_fail_on_a_broken_import():
    """RED CONTROL: installed (present in the distribution map) but the
    import itself raises — a distribution can be on disk and still not
    actually load (e.g. an ABI mismatch), which a presence-only check would
    miss."""
    def _always_raises(mod):
        raise ImportError(f"synthetic failure importing {mod}")

    problems = _find_problems(
        specs=["some-pkg>=1"],
        dist_to_imports={"some-pkg": {"some_pkg"}},
        import_fn=_always_raises,
    )
    assert len(problems) == 1
    assert "some-pkg" in problems[0] and "synthetic failure" in problems[0]


def test_the_check_passes_a_healthy_synthetic_case():
    """GREEN CONTROL alongside the two red ones above: a distribution that IS
    present and DOES import must not be flagged."""
    problems = _find_problems(
        specs=["some-pkg>=1"],
        dist_to_imports={"some-pkg": {"some_pkg"}},
        import_fn=lambda mod: None,
    )
    assert problems == []


def test_every_declared_dependency_is_importable():
    """Every name in `dependencies` + `optional-dependencies` must resolve
    to an installed distribution whose import(s) actually succeed here —
    the exact failure mode that let tomlkit go missing for an entire image
    generation without ever naming itself.
    """
    dist_to_imports = _dist_to_imports(packages_distributions())
    problems = _find_problems(_declared_dependency_specs(), dist_to_imports)

    assert not problems, (
        "declared-dependency drift — pyproject.toml names a package this image "
        "cannot actually use (rebuild bot-squad-api:latest and/or regenerate "
        "api/requirements.lock, see T-1004):\n" + "\n".join(f"  - {p}" for p in problems)
    )
