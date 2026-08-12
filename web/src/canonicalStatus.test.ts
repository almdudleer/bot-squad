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
  canonicalOf,
  deriveParentStatus,
} from "./canonicalStatus";
import { COLUMNS } from "./pages/Project";

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
// Why this is here and not a comment: Project.tsx groups tasks with
// `if (COLUMNS.includes(t.status))` in four places, so a status missing from
// COLUMNS does not render as "other" or throw — the ticket disappears from the
// board and from the per-column counts, silently. Measured 2026-08-12: 11 of the
// 13 tickets then at `in_progress` were held by no live session, i.e. exactly the
// population a new `paused` status would move. Adding that status to the model
// without adding a column would therefore have removed 11 tickets from his board
// and called it a feature.
//
// This is the ordering constraint expressed where a machine checks it: green
// today (six statuses, six columns), red the moment the two sets diverge in
// either direction.
describe("board columns cover the whole status model", () => {
  test("every canonical-mapped status has a board column", () => {
    const missing = Object.keys(CANONICAL_STATE).filter(
      (s) => !(COLUMNS as readonly string[]).includes(s),
    );
    expect(missing).toEqual([]);
  });

  test("no board column names a status the model does not know", () => {
    const phantom = (COLUMNS as readonly string[]).filter(
      (c) => !(c in CANONICAL_STATE),
    );
    expect(phantom).toEqual([]);
  });
});
