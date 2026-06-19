/**
 * T-0128: session-tree builder prefers the persisted `parent_sid` over the
 * legacy task→initiative→TL heuristic, and falls back to the heuristic only
 * for legacy sessions that lack the field.
 *
 * `buildSessionTree` is the pure helper Sessions.tsx uses to indent dev rows
 * under their parent. It takes the rows plus the task→initiative map (the same
 * map the component memoizes from the backlog).
 */
import { describe, expect, test } from "vitest";

import { SessionRow } from "../api";
import { buildSessionTree } from "./Sessions";

function tl(overrides: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-test-tl-p0",
    status: "active",
    window: "tl",
    cwd: "/tmp",
    task_id: null,
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  };
}

function dev(overrides: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-test-dev-p1",
    status: "active",
    window: "dev",
    cwd: "/tmp",
    task_id: "T-0001",
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  };
}

describe("buildSessionTree parent_sid preference (T-0128)", () => {
  test("a dev WITH parent_sid nests under that parent, ignoring the heuristic", () => {
    const lead = tl({ sid: "S-test-tl-p0" });
    // A second TL the heuristic WOULD pick (bound to the task's initiative),
    // proving parent_sid wins over the initiative trace.
    const otherLead = tl({
      sid: "S-test-otherlead-p9",
      window: "otherlead",
      initiative: "alpha.md",
    });
    const d = dev({
      sid: "S-test-dev-p1",
      task_id: "T-0001",
      parent_sid: "S-test-tl-p0",
    });
    // taskInitiative maps the dev's task to alpha.md → heuristic would pick
    // otherLead; parent_sid must override.
    const taskInitiative = new Map<string, string>([["T-0001", "alpha.md"]]);

    const out = buildSessionTree([lead, otherLead, d], taskInitiative);
    const devEntry = out.find((e) => e.row.sid === "S-test-dev-p1");
    expect(devEntry?.level).toBe(1);
    // The dev is nested directly after its parent_sid parent (S-test-tl-p0),
    // NOT after otherLead.
    const leadIdx = out.findIndex((e) => e.row.sid === "S-test-tl-p0");
    expect(out[leadIdx + 1]?.row.sid).toBe("S-test-dev-p1");
  });

  test("a legacy dev WITHOUT parent_sid still uses the task→initiative→TL heuristic", () => {
    const lead = tl({ sid: "S-test-tl-p0", initiative: "alpha.md" });
    const d = dev({ sid: "S-test-dev-p1", task_id: "T-0001" }); // no parent_sid
    const taskInitiative = new Map<string, string>([["T-0001", "alpha.md"]]);

    const out = buildSessionTree([lead, d], taskInitiative);
    const devEntry = out.find((e) => e.row.sid === "S-test-dev-p1");
    expect(devEntry?.level).toBe(1);
    const leadIdx = out.findIndex((e) => e.row.sid === "S-test-tl-p0");
    expect(out[leadIdx + 1]?.row.sid).toBe("S-test-dev-p1");
  });

  test("parent_sid pointing at a non-visible parent falls back to the heuristic", () => {
    const lead = tl({ sid: "S-test-tl-p0", initiative: "alpha.md" });
    const d = dev({
      sid: "S-test-dev-p1",
      task_id: "T-0001",
      parent_sid: "S-test-gone-p7", // not in the rendered slice
    });
    const taskInitiative = new Map<string, string>([["T-0001", "alpha.md"]]);

    const out = buildSessionTree([lead, d], taskInitiative);
    const devEntry = out.find((e) => e.row.sid === "S-test-dev-p1");
    // Falls back to the heuristic → nests under the initiative-bound TL.
    expect(devEntry?.level).toBe(1);
    const leadIdx = out.findIndex((e) => e.row.sid === "S-test-tl-p0");
    expect(out[leadIdx + 1]?.row.sid).toBe("S-test-dev-p1");
  });

  test("a dev with neither parent_sid nor a traceable TL is an orphan at root", () => {
    const d = dev({ sid: "S-test-dev-p1", task_id: "T-0001" });
    const taskInitiative = new Map<string, string>(); // no mapping

    const out = buildSessionTree([d], taskInitiative);
    const devEntry = out.find((e) => e.row.sid === "S-test-dev-p1");
    expect(devEntry?.level).toBe(0);
  });
});

describe("buildSessionTree task-less nesting (T-0222)", () => {
  // A task-less child (qa / prod-TL / ad-hoc) — like a TL it carries no
  // task_id, so isDevRow is false. The distinguishing feature is a genuine
  // persisted parent_sid.
  function taskless(overrides: Partial<SessionRow>): SessionRow {
    return tl({ window: "qa", ...overrides });
  }

  test("a task-less child WITH a resolvable parent_sid nests under that parent", () => {
    const lead = tl({ sid: "S-test-tl-p0" });
    const qa = taskless({ sid: "S-test-qa-p2", parent_sid: "S-test-tl-p0" });
    const taskInitiative = new Map<string, string>();

    const out = buildSessionTree([lead, qa], taskInitiative);
    const qaEntry = out.find((e) => e.row.sid === "S-test-qa-p2");
    // Previously this row flattened to root (level 0) because the tree only
    // nested dev rows; it must now nest one level under its parent_sid parent.
    expect(qaEntry?.level).toBe(1);
    const leadIdx = out.findIndex((e) => e.row.sid === "S-test-tl-p0");
    expect(out[leadIdx + 1]?.row.sid).toBe("S-test-qa-p2");
  });

  test("a genuine root (operator, no parent_sid) stays at root", () => {
    const operator = tl({ sid: "S-test-op-p0", window: "operator" });
    const qa = taskless({ sid: "S-test-qa-p2", parent_sid: "S-test-op-p0" });
    const taskInitiative = new Map<string, string>();

    const out = buildSessionTree([operator, qa], taskInitiative);
    expect(out.find((e) => e.row.sid === "S-test-op-p0")?.level).toBe(0);
    // and the child still nests under it.
    expect(out.find((e) => e.row.sid === "S-test-qa-p2")?.level).toBe(1);
  });

  test("a task-less row whose parent_sid is not visible stays at root", () => {
    const qa = taskless({ sid: "S-test-qa-p2", parent_sid: "S-test-gone-p7" });
    const taskInitiative = new Map<string, string>();

    const out = buildSessionTree([qa], taskInitiative);
    // No heuristic applies to a task-less row → genuine root.
    expect(out.find((e) => e.row.sid === "S-test-qa-p2")?.level).toBe(0);
  });

  test("an unparented TL stays at root while a parented sibling TL nests (lineage depth)", () => {
    // operator → dev-TL (parent_sid=operator) → dev (parent_sid=dev-TL),
    // plus a second TL with NO parent_sid that must remain a root.
    const operator = tl({ sid: "S-test-op-p0", window: "operator" });
    const lead = tl({ sid: "S-test-tl-p0", parent_sid: "S-test-op-p0" });
    const rootless = tl({ sid: "S-test-tl-p9", window: "tl2" }); // no parent_sid
    const d = dev({
      sid: "S-test-dev-p1",
      task_id: "T-0001",
      parent_sid: "S-test-tl-p0",
    });
    const taskInitiative = new Map<string, string>();

    const out = buildSessionTree([operator, lead, rootless, d], taskInitiative);
    const lvl = (sid: string) => out.find((e) => e.row.sid === sid)?.level;
    expect(lvl("S-test-op-p0")).toBe(0); // operator: genuine root
    expect(lvl("S-test-tl-p0")).toBe(1); // parented TL nests under operator
    expect(lvl("S-test-dev-p1")).toBe(2); // dev nests under its TL — 3 levels deep
    expect(lvl("S-test-tl-p9")).toBe(0); // unparented TL stays root
  });

  test("T-0231: roots are ordered operator → team-lead → rest regardless of API order", () => {
    // API returns them shuffled: a dev-less ad-hoc root, then a TL, then the
    // operator last. The hierarchy view must still float the operator to the
    // top, the TL next, then the rest.
    const adhoc = tl({ sid: "S-test-adhoc-p3", window: "adhoc", role: "dev" });
    const lead = tl({ sid: "S-test-tl-p0", role: "teamlead" });
    const operator = tl({ sid: "S-test-op-p9", window: "operator", role: "operator" });
    const d = dev({ sid: "S-test-dev-p1", task_id: "T-0001", parent_sid: "S-test-tl-p0" });
    const taskInitiative = new Map<string, string>();

    const out = buildSessionTree([adhoc, lead, operator, d], taskInitiative);
    // Root rows in emitted order (level 0 only).
    const rootSids = out.filter((e) => e.level === 0).map((e) => e.row.sid);
    expect(rootSids).toEqual(["S-test-op-p9", "S-test-tl-p0", "S-test-adhoc-p3"]);
    // The dev still nests one level under its TL, immediately after it.
    const leadIdx = out.findIndex((e) => e.row.sid === "S-test-tl-p0");
    expect(out[leadIdx + 1]?.row.sid).toBe("S-test-dev-p1");
    expect(out.find((e) => e.row.sid === "S-test-dev-p1")?.level).toBe(1);
  });

  test("T-0231: same-rank roots keep their incoming order (stable sort)", () => {
    const op1 = tl({ sid: "S-test-op-p1", window: "operator", role: "operator" });
    const op2 = tl({ sid: "S-test-op-p2", window: "operator", role: "operator" });
    const out = buildSessionTree([op1, op2], new Map());
    expect(out.map((e) => e.row.sid)).toEqual(["S-test-op-p1", "S-test-op-p2"]);
  });

  test("a parent_sid cycle does not loop — both rows still render", () => {
    const a = tl({ sid: "S-test-a-p0", parent_sid: "S-test-b-p1" });
    const b = tl({ sid: "S-test-b-p1", parent_sid: "S-test-a-p0" });
    const out = buildSessionTree([a, b], new Map());
    // Neither vanishes; the visited guard breaks the cycle.
    expect(out.map((e) => e.row.sid).sort()).toEqual(["S-test-a-p0", "S-test-b-p1"]);
  });
});
