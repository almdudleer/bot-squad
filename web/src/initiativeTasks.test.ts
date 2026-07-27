/**
 * T-0295 (c): the shared group-by-initiative query.
 *
 * Vision's expanded initiative row and the Board's initiative lane must list
 * the SAME tasks — that's the whole point of extracting this instead of
 * re-deriving "which tasks belong to X" on the Vision page.
 */
import { describe, expect, test } from "vitest";

import type { Task } from "./api";
import {
  sortBoundTasks,
  splitDone,
  taskInitiativeKey,
  tasksForInitiative,
} from "./initiativeTasks";

function task(id: string, initiative: string | null | undefined): Task {
  return {
    id,
    title: id,
    status: "open",
    body: "",
    path: `/b/${id}.md`,
    initiative,
  } as Task;
}

describe("taskInitiativeKey", () => {
  test("trims, and treats missing/null as unattached", () => {
    expect(taskInitiativeKey(task("T-1", "  ui-polish.md "))).toBe("ui-polish.md");
    expect(taskInitiativeKey(task("T-2", null))).toBe("");
    expect(taskInitiativeKey(task("T-3", undefined))).toBe("");
  });
});

describe("tasksForInitiative", () => {
  const tasks = [
    task("T-1", "ui-polish.md"),
    task("T-2", "process-paradigm.md"),
    task("T-3", " ui-polish.md"),
    task("T-4", null),
  ];

  test("returns exactly the tasks bound to that basename", () => {
    expect(tasksForInitiative(tasks, "ui-polish.md").map((t) => t.id)).toEqual(["T-1", "T-3"]);
  });

  test("an initiative with no bound tasks returns empty, not everything", () => {
    expect(tasksForInitiative(tasks, "update-delivery.md")).toEqual([]);
  });

  test("an empty basename matches nothing — never the unattached bucket", () => {
    // Guards the failure where a row with a blank key silently lists every
    // unattached task in the project as "bound to this initiative".
    expect(tasksForInitiative(tasks, "")).toEqual([]);
    expect(tasksForInitiative(tasks, "  ")).toEqual([]);
  });
});

// T-0295 walkthrough finding: in raw id order a mature initiative opens with
// ~128 consecutive DONE rows, burying its live work and its body. Live first,
// done folded — the Board's own convention (T-0058).
describe("sortBoundTasks", () => {
  function withStatus(id: string, status: Task["status"]): Task {
    return { ...task(id, "x.md"), status };
  }

  test("orders in-progress → validating → backlog → done", () => {
    const out = sortBoundTasks([
      withStatus("T-9", "closed"),
      withStatus("T-8", "open"),
      withStatus("T-7", "totest"),
      withStatus("T-6", "in_progress"),
    ]);
    expect(out.map((t) => t.id)).toEqual(["T-6", "T-7", "T-8", "T-9"]);
  });

  test("ties break on id, so ordering is stable and chronological", () => {
    const out = sortBoundTasks([
      withStatus("T-0300", "closed"),
      withStatus("T-0100", "closed"),
      withStatus("T-0200", "closed"),
    ]);
    expect(out.map((t) => t.id)).toEqual(["T-0100", "T-0200", "T-0300"]);
  });

  test("does not mutate the caller's array", () => {
    const input = [withStatus("T-2", "closed"), withStatus("T-1", "open")];
    sortBoundTasks(input);
    expect(input.map((t) => t.id)).toEqual(["T-2", "T-1"]);
  });

  test("planned/reopened roll up into backlog, above done", () => {
    const out = sortBoundTasks([
      withStatus("T-1", "closed"),
      withStatus("T-2", "planned"),
      withStatus("T-3", "reopened"),
    ]);
    expect(out.map((t) => t.id)).toEqual(["T-2", "T-3", "T-1"]);
  });
});

describe("splitDone", () => {
  test("splits on the canonical done state, losing nothing", () => {
    const tasks = [
      { ...task("T-1", "x.md"), status: "closed" as const },
      { ...task("T-2", "x.md"), status: "totest" as const },
      { ...task("T-3", "x.md"), status: "open" as const },
    ];
    const { live, done } = splitDone(tasks);
    expect(live.map((t) => t.id)).toEqual(["T-2", "T-3"]);
    expect(done.map((t) => t.id)).toEqual(["T-1"]);
    expect(live.length + done.length).toBe(tasks.length);
  });

  test("validating is LIVE, not done — a totest task still needs someone", () => {
    const { live } = splitDone([{ ...task("T-1", "x.md"), status: "totest" as const }]);
    expect(live.map((t) => t.id)).toEqual(["T-1"]);
  });
});
