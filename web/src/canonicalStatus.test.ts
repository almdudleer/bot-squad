/**
 * T-0479: closed-set invariants for the canonical 4-state mapping. Mirrors the
 * backend test_canonical_status.py — every internal status must map, the range
 * must be exactly the stakeholder's four states, and the decided mapping is
 * pinned so a silent re-bucket goes red.
 */
import { describe, expect, test } from "vitest";
import {
  CANONICAL_LABELS,
  CANONICAL_STATE,
  CANONICAL_STATES,
  INTERNAL_LABELS,
  canonicalOf,
  deriveParentStatus,
  statusRefinesItsColumn,
} from "./canonicalStatus";
import { BOARD_COLUMNS } from "./pages/Project";

// The six internal statuses, kept in sync with Task["status"] in api.ts.
const INTERNAL_STATUSES = [
  "planned",
  "open",
  "in_progress",
  "totest",
  "reopened",
  "closed",
] as const;

describe("canonical 4-state mapping", () => {
  test("maps exactly the six internal statuses", () => {
    expect(new Set(Object.keys(CANONICAL_STATE))).toEqual(
      new Set(INTERNAL_STATUSES),
    );
  });

  test("range is exactly the four canonical states", () => {
    expect(new Set(Object.values(CANONICAL_STATE))).toEqual(
      new Set(CANONICAL_STATES),
    );
  });

  test("canonical states are the stakeholder's four, in lifecycle order", () => {
    expect(CANONICAL_STATES).toEqual([
      "backlog",
      "in-progress",
      "validating",
      "done",
    ]);
  });

  test("every canonical state has a label", () => {
    expect(new Set(Object.keys(CANONICAL_LABELS))).toEqual(
      new Set(CANONICAL_STATES),
    );
  });

  test("decided mapping is stable", () => {
    expect(CANONICAL_STATE).toEqual({
      planned: "backlog",
      open: "backlog",
      reopened: "backlog",
      in_progress: "in-progress",
      totest: "validating",
      closed: "done",
    });
  });

  test("canonicalOf helper", () => {
    expect(canonicalOf("totest")).toBe("validating");
    expect(canonicalOf("reopened")).toBe("backlog");
  });
});

// T-0512 (M9 / Part A): parent-abstract status derivation. Mirrors the backend
// derive_parent_status — keep the two in lockstep.
describe("deriveParentStatus (abstract parent rollup)", () => {
  test("no usable children → null (not abstract)", () => {
    expect(deriveParentStatus([])).toBeNull();
    expect(deriveParentStatus(["bogus", "junk"])).toBeNull();
  });

  test("done only when EVERY child is done", () => {
    expect(deriveParentStatus(["closed", "closed"])).toBe("done");
    expect(deriveParentStatus(["closed", "open"])).toBe("in-progress");
    expect(deriveParentStatus(["closed", "in_progress"])).toBe("in-progress");
  });

  test("backlog only when NONE has started", () => {
    expect(deriveParentStatus(["planned", "open", "reopened"])).toBe("backlog");
  });

  test("any started subtask rolls up to in-progress", () => {
    expect(deriveParentStatus(["open", "in_progress"])).toBe("in-progress");
    expect(deriveParentStatus(["open", "closed"])).toBe("in-progress");
  });

  test("validating when all work is complete (validating/done), >=1 verifying", () => {
    expect(deriveParentStatus(["totest", "totest"])).toBe("validating");
    expect(deriveParentStatus(["totest", "closed"])).toBe("validating");
    expect(deriveParentStatus(["totest", "in_progress"])).toBe("in-progress");
  });

  test("ignores unknown child statuses but uses the rest", () => {
    expect(deriveParentStatus(["closed", "garbage"])).toBe("done");
  });

  test("result is always a real canonical state", () => {
    for (const combo of [
      ["open"], ["in_progress"], ["totest"], ["closed"],
      ["open", "closed"], ["totest", "closed"], ["planned", "in_progress"],
    ]) {
      expect(CANONICAL_STATES).toContain(deriveParentStatus(combo));
    }
  });
});

// T-0889: the board must be able to RENDER every status the model knows about.
//
// Why this is here and not a comment: Project.tsx buckets tasks by
// `CANONICAL_STATE[t.status]` in FOUR places (the ungrouped board, the
// initiative lane's counts and its board, and ListBoard), and each one drops a
// task whose status has no bucket. A status missing from the model therefore
// does not render as "other" and does not throw — the ticket disappears from the
// board AND from the per-column counts, silently. Measured 2026-08-12: 11 of the
// 13 tickets then at `in_progress` were held by no live session, i.e. exactly the
// population a new `paused` status would move. Adding that status to the model
// without a column would have removed 11 tickets from his board and called it a
// feature.
//
// RE-EXPRESSED when the board collapsed to the canonical four (same commit).
// The columns are now canonical STATES, not internal statuses, so the invariant
// is one hop longer: every internal status must map to a canonical state, and
// every canonical state must have a column. The hazard is unchanged and so is
// the guarantee — a status the board cannot render still fails here, loudly.
describe("board columns cover the whole status model", () => {
  test("every internal status lands in a canonical state that HAS a column", () => {
    const unrenderable = Object.keys(CANONICAL_STATE).filter((s) => {
      const canon = CANONICAL_STATE[s as keyof typeof CANONICAL_STATE];
      return !(BOARD_COLUMNS as readonly string[]).includes(canon);
    });
    expect(unrenderable).toEqual([]);
  });

  test("no board column is a canonical state the model never produces", () => {
    const produced = new Set(Object.values(CANONICAL_STATE));
    const phantom = (BOARD_COLUMNS as readonly string[]).filter(
      (c) => !produced.has(c as never),
    );
    expect(phantom).toEqual([]);
  });

  test("the board renders exactly one label per column — no duplicate pairs", () => {
    // His actual report was «двойные состояния»: Backlog/Planned, Backlog/Open,
    // Backlog/Reopened, In progress/In progress, Validating/To Test. Pin that
    // the column set is the canonical four and each has exactly one label, so a
    // second label source cannot be reintroduced without this going red.
    const labels = (BOARD_COLUMNS as readonly string[]).map(
      (c) => CANONICAL_LABELS[c as keyof typeof CANONICAL_LABELS],
    );
    expect(labels).toEqual(["Backlog", "In progress", "Validating", "Done"]);
    expect(new Set(labels).size).toBe(labels.length);
  });
});

// T-0889: the card badge must not restate the column it sits in — doing so
// would recreate the duplicate he reported, one level down.
describe("internal-status badge only shows where it refines the column", () => {
  test("the three statuses sharing Backlog DO show a badge", () => {
    for (const s of ["planned", "open", "reopened"] as const) {
      expect(statusRefinesItsColumn(s)).toBe(true);
    }
  });

  test("the 1:1 statuses do NOT — that would print the column's own name", () => {
    for (const s of ["in_progress", "totest", "closed"] as const) {
      expect(statusRefinesItsColumn(s)).toBe(false);
    }
  });

  test("every internal status has a label, so a badge can never render blank", () => {
    for (const s of Object.keys(CANONICAL_STATE) as (keyof typeof CANONICAL_STATE)[]) {
      expect(INTERNAL_LABELS[s]).toBeTruthy();
    }
  });
});
