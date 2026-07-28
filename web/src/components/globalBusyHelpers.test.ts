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

/**
 * T-0763 — the property that lets GlobalBusyIndicator stop refetching when the
 * username arrives.
 *
 * The defect: the poll effect was keyed on `myUsername`, which the Shell loads
 * asynchronously from /api/me. The null -> username flip tore the effect down
 * and re-ran a whole second cross-server fan-out on every page load. The
 * component now keeps the RAW FanResult[] and re-derives the view, which is
 * only sound if `myUsername` is a pure post-processing input — i.e. the SAME
 * fetched data can answer for a different user with no new request. These
 * tests state that as an assertion rather than leaving it as an assumption.
 *
 * The effect-dependency side itself cannot be tested here: this repo has no
 * jsdom (see Markdown.mermaid.test.tsx), so effects never run. It is covered
 * by the browser walkthrough on the ticket instead.
 */
describe("T-0763: aggregateIndicator is a pure re-derivation over myUsername", () => {
  const fan = (): FanResult[] => [
    {
      serverId: "srv_1",
      ok: true,
      projects: [
        {
          serverId: "srv_1",
          serverName: "staging",
          projectSlug: "bot-squad",
          sessions: [s({ sid: "S-a", owner: "alex" }), s({ sid: "S-b", owner: "robin" })],
        } as ProjectSessions,
      ],
    },
  ];

  test("the same fetched data answers for a different user — no refetch needed", () => {
    const data = fan();
    expect(aggregateIndicator(data, "alex").rows.map((r) => r.sid)).toEqual(["S-a"]);
    expect(aggregateIndicator(data, "robin").rows.map((r) => r.sid)).toEqual(["S-b"]);
  });

  test("the null -> username flip the Shell performs re-derives from data in hand", () => {
    const data = fan();
    // Pre-/api/me: nothing claimed as mine (isMyInFlight prefers a false
    // negative over flashing another user's work).
    expect(aggregateIndicator(data, null).rows).toEqual([]);
    // Post-/api/me: the row appears WITHOUT the fan-out being re-run.
    expect(aggregateIndicator(data, "alex").rows).toHaveLength(1);
  });

  test("aggregating does not mutate the fan-out it reads (safe to keep in state)", () => {
    const data = fan();
    const snapshot = JSON.parse(JSON.stringify(data));
    aggregateIndicator(data, "alex");
    aggregateIndicator(data, "robin");
    expect(data).toEqual(snapshot);
  });

  test("the initial empty fan-out renders the same state the old useState seed did", () => {
    expect(aggregateIndicator([], null)).toEqual({ rows: [], failedServerCount: 0 });
  });
});

/**
 * T-0763 — a malformed payload must not take the page down.
 *
 * FOUND BY THE BROWSER CHECK, NOT BY A UNIT TEST, and it is the reason the
 * walkthrough was worth doing: moving aggregation out of the poll's try/catch
 * and into a render-time useMemo turned an exception that used to be SWALLOWED
 * into one that unmounted the whole Shell — every poll on the page stopped, not
 * just this indicator's. The exception was real at HEAD too (the mothership
 * fan-out handed over the un-normalized T-0601 `{sessions, errors}` envelope,
 * so `for..of` hit an object); it was simply invisible, which is why the fleet
 * busy-indicator has been showing nothing on mothership builds.
 *
 * The envelope itself is fixed at source in globalBusyMothership.ts. This pins
 * the containment: aggregation is now render-time, so it must never throw for
 * ANY payload shape.
 */
describe("T-0763: aggregation never throws on a malformed payload", () => {
  const badShapes: Array<[string, unknown]> = [
    ["the T-0601 envelope handed over un-normalized", { sessions: [], errors: [] }],
    ["null sessions", null],
    ["undefined sessions", undefined],
    ["a bare string", "nope"],
  ];

  for (const [label, sessions] of badShapes) {
    test(`inFlightRowsFromProject drops the project instead of throwing: ${label}`, () => {
      const payload = {
        serverId: "srv_1",
        serverName: "staging",
        projectSlug: "bot-squad",
        sessions,
      } as unknown as ProjectSessions;
      expect(() => inFlightRowsFromProject(payload, "alex")).not.toThrow();
      expect(inFlightRowsFromProject(payload, "alex")).toEqual([]);
    });
  }

  test("one malformed project does not blank the WELL-FORMED ones beside it", () => {
    const fan: FanResult[] = [
      {
        serverId: "srv_1",
        ok: true,
        projects: [
          {
            serverId: "srv_1",
            serverName: "staging",
            projectSlug: "broken",
            sessions: { sessions: [s()] },
          } as unknown as ProjectSessions,
          {
            serverId: "srv_1",
            serverName: "staging",
            projectSlug: "bot-squad",
            sessions: [s({ sid: "S-ok" })],
          } as ProjectSessions,
        ],
      },
    ];
    let out!: ReturnType<typeof aggregateIndicator>;
    expect(() => { out = aggregateIndicator(fan, "alex"); }).not.toThrow();
    expect(out.rows.map((r) => r.sid)).toEqual(["S-ok"]);
  });
});
