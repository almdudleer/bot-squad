/**
 * T-0889 — the two canonical-status SSOTs must agree, checked by a machine.
 *
 * `api/app/canonical_status.py` and `web/src/canonicalStatus.ts` carry the SAME
 * internal-status → canonical-state mapping, and each tells the reader to "keep
 * both in lockstep". Until this file that sentence was the ONLY thing holding
 * them together: prose, not a control.
 *
 * ── THE GAP, MEASURED (2026-08-12, on a scratch copy of the tree) ─────────
 * Added `paused` to the PYTHON side alone and left `web/src/canonicalStatus.ts`
 * untouched. Python has real lockstep guards and they all fired, each naming
 * the next surface to update, exactly as they should:
 *   `_VALID_STATUSES` → `canonical_status.CANONICAL_STATE` → the pinned literal
 *   in `test_canonical_status.py` → `scripts/lint/backlog_frontmatter.py`'s
 *   `VALID_STATUSES` (caught by `test_lint_module_constant_matches_api`).
 * With all four updated — which is simply what an implementer does — the api
 * side is green again: 23 passed across the ONLY two api test files that
 * reference a status set.
 *
 * The full web suite at that point: 687 passed, 0 failed. It had no opinion at
 * all. The status existed for the API and did not exist for the UI, and nothing
 * in the repo said so — the "half-exists" failure T-0889's enum audit is about.
 *
 * The lesson is not that Python was unguarded; it is that every guard stopped
 * at the language boundary. The web side of the mirror had none.
 *
 * ── WHAT THIS CLOSES ─────────────────────────────────────────────────────
 * With this file the chain runs end to end, so T-0889's required ordering is
 * enforced instead of documented:
 *
 *   _VALID_STATUSES
 *     ──(api/tests/test_canonical_status.py)──▶  api CANONICAL_STATE
 *     ──(THIS FILE)───────────────────────────▶  web CANONICAL_STATE
 *     ──(tsc: Record<Task["status"], …>)──────▶  Task["status"]
 *     ──(canonicalStatus.test.ts, T-0889)─────▶  board COLUMNS
 *
 * Add a status at the API end and you cannot stop until it has a board column.
 * That matters here specifically: 11 of the 13 tickets at `in_progress` on
 * 2026-08-12 were held by no live session, i.e. exactly the population `paused`
 * will move, and a status with no column is dropped from the board silently.
 *
 * ── WHAT THIS DOES NOT SEE ───────────────────────────────────────────────
 * The three mirrored CONSTANTS only. `derive_parent_status` /
 * `deriveParentStatus` are mirrored logic, not data, and are covered by
 * matching case tables on each side; this file does not compare them.
 *
 * The Python is read as TEXT through Vite's `?raw` loader rather than parsed by
 * a Python runtime, so the extractors below are deliberately strict: each one
 * THROWS if it cannot find its literal or finds it empty. A reformatted
 * `canonical_status.py` therefore fails loudly and gets the extractor updated —
 * it can never degrade into a green test that compared nothing.
 */
import { describe, expect, test } from "vitest";

import {
  CANONICAL_LABELS,
  CANONICAL_STATE,
  CANONICAL_STATES,
} from "./canonicalStatus";
// A module-resolution failure here aborts collection loudly, which is the
// intended behaviour if the API-side SSOT is moved or renamed.
import PY_SOURCE from "../../api/app/canonical_status.py?raw";

// ---------------------------------------------------------------------------
// Extractors — strict by design (see header)
// ---------------------------------------------------------------------------

function fail(what: string): never {
  throw new Error(
    `canonicalStatusMirror: could not read ${what} from ` +
      `api/app/canonical_status.py. If that file was reformatted, UPDATE this ` +
      `extractor — do not delete the check; it is the only thing keeping the ` +
      `Python and TypeScript status models in lockstep (T-0889).`,
  );
}

/** Parse a module-level `NAME: dict[str, str] = {"k": "v", ...}` literal. */
function pyDict(name: string): Record<string, string> {
  const block = new RegExp(`^${name}\\s*:[^=\\n]*=\\s*\\{([\\s\\S]*?)\\}`, "m").exec(
    PY_SOURCE,
  );
  if (!block) fail(`the ${name} literal`);
  const pairs = [...block[1].matchAll(/"([^"]+)"\s*:\s*"([^"]+)"/g)];
  if (pairs.length === 0) fail(`any entry of ${name}`);
  return Object.fromEntries(pairs.map((m) => [m[1], m[2]]));
}

/** Parse a module-level `NAME: tuple[str, ...] = ("a", "b", ...)` literal. */
function pyTuple(name: string): string[] {
  const block = new RegExp(`^${name}\\s*:[^=\\n]*=\\s*\\(([\\s\\S]*?)\\)`, "m").exec(
    PY_SOURCE,
  );
  if (!block) fail(`the ${name} literal`);
  const items = [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  if (items.length === 0) fail(`any item of ${name}`);
  return items;
}

const PY_CANONICAL_STATE = pyDict("CANONICAL_STATE");
const PY_CANONICAL_LABELS = pyDict("CANONICAL_LABELS");
const PY_CANONICAL_STATES = pyTuple("CANONICAL_STATES");

// ---------------------------------------------------------------------------

describe("canonical status: python and typescript SSOTs are in lockstep", () => {
  // Stated as its own test so a reader can see that "green" here means the
  // extractors actually read something, not that they matched nothing.
  test("the extractors read a non-empty model out of canonical_status.py", () => {
    expect(Object.keys(PY_CANONICAL_STATE).length).toBeGreaterThan(0);
    expect(Object.keys(PY_CANONICAL_LABELS).length).toBeGreaterThan(0);
    expect(PY_CANONICAL_STATES.length).toBeGreaterThan(0);
  });

  test("internal status → canonical state is identical on both sides", () => {
    expect(PY_CANONICAL_STATE).toEqual(CANONICAL_STATE);
  });

  test("canonical labels are identical on both sides", () => {
    expect(PY_CANONICAL_LABELS).toEqual(CANONICAL_LABELS);
  });

  test("canonical states are identical, and in the same lifecycle order", () => {
    expect(PY_CANONICAL_STATES).toEqual([...CANONICAL_STATES]);
  });
});
