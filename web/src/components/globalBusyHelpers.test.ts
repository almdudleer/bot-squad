import { describe, expect, test } from "vitest";

import type { SessionRow } from "../api";
import {
  aggregateIndicator,
  indicatorView,
  isMyInFlight,
  inFlightRowsFromProject,
  type FanResult,
  type ProjectSessions,
} from "./globalBusyHelpers";

function s(over: Partial<SessionRow> = {}): SessionRow {
  return {
    sid: "S-x",
    status: "active",
    window: "w",
    cwd: "/tmp",
    task_id: "T-1",
    owner: "alex",
    ...over,
  };
}

describe("isMyInFlight (T-0064)", () => {
  test("active + task_id + owner matches → true", () => {
    expect(isMyInFlight(s(), "alex")).toBe(true);
  });

  test("paused / suspended sessions don't count", () => {
    expect(isMyInFlight(s({ status: "paused" }), "alex")).toBe(false);
    expect(isMyInFlight(s({ status: "suspended" }), "alex")).toBe(false);
  });

  test("active but no bound task_id doesn't count", () => {
    expect(isMyInFlight(s({ task_id: null }), "alex")).toBe(false);
  });

  test("owner mismatch (admin viewing someone else's session) is excluded", () => {
    expect(isMyInFlight(s({ owner: "someone-else" }), "alex")).toBe(false);
  });

  test("empty owner (legacy pre-T-0080 session) trusts the BE filter", () => {
    expect(isMyInFlight(s({ owner: "" }), "alex")).toBe(true);
  });

  test("unknown myUsername prefers false negative (no flashing wrong user)", () => {
    expect(isMyInFlight(s(), null)).toBe(false);
  });
});

describe("inFlightRowsFromProject", () => {
  function p(overrides: Partial<ProjectSessions> = {}): ProjectSessions {
    return {
      serverId: "srv-1",
      serverName: "Mothership",
      projectSlug: "bot-squad",
      sessions: [],
      ...overrides,
    };
  }

  test("maps each in-flight session into an InFlightRow with badges intact", () => {
    const rows = inFlightRowsFromProject(
      p({ sessions: [s({ sid: "S-a", task_id: "T-9" }), s({ sid: "S-b" })] }),
      "alex",
    );
    expect(rows).toEqual([
      {
        sid: "S-a",
        taskId: "T-9",
        projectSlug: "bot-squad",
        serverId: "srv-1",
        serverName: "Mothership",
      },
      {
        sid: "S-b",
        taskId: "T-1",
        projectSlug: "bot-squad",
        serverId: "srv-1",
        serverName: "Mothership",
      },
    ]);
  });

  test("skips non-in-flight sessions silently", () => {
    const rows = inFlightRowsFromProject(
      p({ sessions: [s({ status: "paused" }), s({ task_id: null })] }),
      "alex",
    );
    expect(rows).toEqual([]);
  });
});

describe("aggregateIndicator (fan-out failure isolation)", () => {
  function ok(
    serverId: string | null,
    projects: ProjectSessions[],
  ): FanResult {
    return { serverId, ok: true, projects };
  }
  function fail(serverId: string | null, error: string): FanResult {
    return { serverId, ok: false, error };
  }

  test("zero in-flight → empty rows, indicator stays off", () => {
    const got = aggregateIndicator([ok("srv-1", [])], "alex");
    expect(got.rows).toEqual([]);
    expect(got.failedServerCount).toBe(0);
  });

  test("one in-flight on one server → rows present", () => {
    const proj: ProjectSessions = {
      serverId: "srv-1",
      serverName: "self",
      projectSlug: "p",
      sessions: [s()],
    };
    const got = aggregateIndicator([ok("srv-1", [proj])], "alex");
    expect(got.rows).toHaveLength(1);
  });

  test("one peer offline + one peer alive: rows from the alive peer survive", () => {
    // Critical regression case from the spec: a failed peer must NOT
    // blank the indicator for the live one.
    const live: ProjectSessions = {
      serverId: "srv-live",
      serverName: "live",
      projectSlug: "p",
      sessions: [s({ sid: "S-keep" })],
    };
    const got = aggregateIndicator(
      [ok("srv-live", [live]), fail("srv-dead", "timeout")],
      "alex",
    );
    expect(got.rows.map((r) => r.sid)).toEqual(["S-keep"]);
    expect(got.failedServerCount).toBe(1);
  });

  test("all peers fail: rows empty, failedServerCount counts them", () => {
    const got = aggregateIndicator(
      [fail("a", "err"), fail("b", "err")],
      "alex",
    );
    expect(got.rows).toEqual([]);
    expect(got.failedServerCount).toBe(2);
  });
});

describe("indicatorView (T-0170)", () => {
  test("count 0 → not visible (no lonely dot)", () => {
    const v = indicatorView(0);
    expect(v.visible).toBe(false);
    expect(v.label).toBe("");
  });

  test("negative/garbage count → not visible", () => {
    expect(indicatorView(-1).visible).toBe(false);
  });

  test("count 1 → singular label + tooltip", () => {
    const v = indicatorView(1);
    expect(v.visible).toBe(true);
    // T-0340: "busy" (not "running") — reserves "running" for the per-session
    // LED and "live" for the per-project liveness count this indicator is NOT.
    expect(v.label).toBe("1 busy");
    expect(v.tooltip).toContain("1 of your agent session ");
    expect(v.tooltip).not.toContain("sessions");
  });

  test("count >1 → plural tooltip + word-labelled count", () => {
    const v = indicatorView(3);
    expect(v.visible).toBe(true);
    expect(v.label).toBe("3 busy");
    expect(v.tooltip).toContain("3 of your agent sessions");
    // explicit word, not a bare number (the stakeholder's complaint)
    expect(v.label).toMatch(/busy/);
  });
});
