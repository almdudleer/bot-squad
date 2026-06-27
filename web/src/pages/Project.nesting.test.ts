/**
 * T-0512 (M9 / Part A): subtask nesting. `buildNesting` is the pure helper the
 * board uses to group subtasks under their parent — it returns the
 * parent→children map and the set of child ids to SUPPRESS as top-level cards
 * (they render nested beneath the parent instead). A child whose parent isn't in
 * the visible set keeps showing standalone, so nothing silently vanishes.
 */
import { describe, expect, test } from "vitest";

import { Task } from "../api";
import { buildNesting } from "./Project";

function task(overrides: Partial<Task> & { id: string }): Task {
  return {
    title: overrides.id,
    status: "open",
    body: "",
    path: `/tmp/${overrides.id}.md`,
    ...overrides,
  };
}

describe("buildNesting", () => {
  test("nests children under a visible parent and suppresses them top-level", () => {
    const tasks = [
      task({ id: "T-PAR", status: "in_progress" }),
      task({ id: "T-C1", status: "in_progress", parent_task: "T-PAR" }),
      task({ id: "T-C2", status: "closed", parent_task: "T-PAR" }),
      task({ id: "T-STD", status: "open" }),
    ];
    const { subtasksByParent, nestedChildIds } = buildNesting(tasks);

    // Both children grouped under the parent...
    expect(subtasksByParent["T-PAR"].map((t) => t.id).sort()).toEqual([
      "T-C1",
      "T-C2",
    ]);
    // ...and suppressed from the top-level columns.
    expect(nestedChildIds.has("T-C1")).toBe(true);
    expect(nestedChildIds.has("T-C2")).toBe(true);
    // The standalone task and the parent itself stay top-level.
    expect(nestedChildIds.has("T-STD")).toBe(false);
    expect(nestedChildIds.has("T-PAR")).toBe(false);
  });

  test("a child whose parent is NOT in the visible set renders standalone", () => {
    // Parent filtered out of this view → child must not be suppressed (else it
    // would vanish from the board entirely).
    const tasks = [
      task({ id: "T-C1", status: "open", parent_task: "T-MISSING" }),
      task({ id: "T-STD", status: "open" }),
    ];
    const { subtasksByParent, nestedChildIds } = buildNesting(tasks);
    expect(nestedChildIds.size).toBe(0);
    expect(subtasksByParent).toEqual({});
  });

  test("no parent_task anywhere → empty nesting", () => {
    const { subtasksByParent, nestedChildIds } = buildNesting([
      task({ id: "T-1" }),
      task({ id: "T-2" }),
    ]);
    expect(nestedChildIds.size).toBe(0);
    expect(Object.keys(subtasksByParent)).toHaveLength(0);
  });
});
