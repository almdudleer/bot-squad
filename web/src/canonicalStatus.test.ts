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
} from "./canonicalStatus";

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
