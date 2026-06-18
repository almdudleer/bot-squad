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
