/**
 * T-0663: done subtasks nested under a parent card grow the board unboundedly
 * with total subtask count instead of active count (stakeholder report).
 * `splitSubtasksByDone` is the pure helper the board uses to partition a
 * parent's children into active vs. done, so the caller can collapse the
 * done ones behind a toggle by default — mirroring the T-0058 rail /
 * T-0272 lane collapse-by-default pattern.
 */
import { describe, expect, test } from "vitest";

import { Task } from "../api";
import { splitSubtasksByDone } from "./BoardColumn";

function task(overrides: Partial<Task> & { id: string }): Task {
  return {
    title: overrides.id,
    status: "open",
    body: "",
    path: `/tmp/${overrides.id}.md`,
    ...overrides,
  };
}

describe("splitSubtasksByDone", () => {
  test("splits closed (canonical done) subtasks from the rest", () => {
    const subtasks = [
      task({ id: "T-C1", status: "closed" }),
      task({ id: "T-C2", status: "in_progress" }),
      task({ id: "T-C3", status: "totest" }),
      task({ id: "T-C4", status: "planned" }),
    ];
    const { active, done } = splitSubtasksByDone(subtasks);
    expect(done.map((t) => t.id)).toEqual(["T-C1"]);
    expect(active.map((t) => t.id).sort()).toEqual(["T-C2", "T-C3", "T-C4"]);
  });

  test("no done subtasks -> done is empty, active is everything", () => {
    const subtasks = [
      task({ id: "T-C1", status: "open" }),
      task({ id: "T-C2", status: "in_progress" }),
    ];
    const { active, done } = splitSubtasksByDone(subtasks);
    expect(done).toEqual([]);
    expect(active).toHaveLength(2);
  });

  test("all done -> active is empty", () => {
    const subtasks = [
      task({ id: "T-C1", status: "closed" }),
      task({ id: "T-C2", status: "closed" }),
    ];
    const { active, done } = splitSubtasksByDone(subtasks);
    expect(active).toEqual([]);
    expect(done).toHaveLength(2);
  });

  test("empty input -> both empty", () => {
    const { active, done } = splitSubtasksByDone([]);
    expect(active).toEqual([]);
    expect(done).toEqual([]);
  });
});
