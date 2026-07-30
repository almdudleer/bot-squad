/**
 * T-0803: the sessions page rendered a 1-second worker restart gap and a
 * 2-hour worker stall identically — first failing poll blanked the table and
 * raised "Session list may be incomplete", which then cleared itself on the
 * next tick. That is the flapping the stakeholder reported.
 *
 * Measured over 20.6 days of worker journal:
 *   - 72 restarts (one per 6.8h), socket dead median 1s / p90 4s;
 *   - 15 stalls where the worker was alive but silent >=30s, max 2h15m.
 * With a 10s poll, a restart gap can produce at most one failing poll in
 * practice while a stall produces many — so "how many consecutive polls
 * failed" is the discriminator, and these tests pin it.
 *
 * Pure-function tests, matching Sessions.empty-state.test.ts (this repo has no
 * jsdom rendering harness).
 */
import { describe, expect, test } from "vitest";

import { SessionRow } from "../api";
import {
  FANOUT_SUSTAINED_POLLS,
  classifyFanoutPhase,
  retainRowsThroughFanoutGap,
} from "./Sessions";

function row(sid: string, overrides: Partial<SessionRow> = {}): SessionRow {
  return {
    sid,
    status: "active",
    window: "w",
    cwd: "/tmp",
    task_id: null,
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  };
}

describe("classifyFanoutPhase", () => {
  test("a clean poll is ok", () => {
    expect(classifyFanoutPhase(0)).toBe("ok");
  });

  test("ONE failing poll is transient — a restart gap must not raise the alarm", () => {
    expect(classifyFanoutPhase(1)).toBe("transient");
  });

  test("two consecutive failing polls is sustained", () => {
    expect(classifyFanoutPhase(2)).toBe("sustained");
    expect(classifyFanoutPhase(37)).toBe("sustained");
  });

  test("the threshold constant is the one the classifier actually uses", () => {
    // Guards against the constant being retuned without the boundary moving.
    expect(classifyFanoutPhase(FANOUT_SUSTAINED_POLLS)).toBe("sustained");
    expect(classifyFanoutPhase(FANOUT_SUSTAINED_POLLS - 1)).toBe("transient");
  });

  test("negative / nonsense streaks degrade to ok rather than accusing a worker", () => {
    expect(classifyFanoutPhase(-1)).toBe("ok");
  });
});

describe("retainRowsThroughFanoutGap", () => {
  test("a CLEAN poll is authoritative — an ended session disappears at once", () => {
    const prev = [row("S-a"), row("S-b")];
    const out = retainRowsThroughFanoutGap(prev, [row("S-a")], false);
    expect(out.map((r) => r.sid)).toEqual(["S-a"]);
    // This is the property that keeps retention from becoming a leak: rows
    // only ever survive a poll that ADMITTED it could not reach a socket.
  });

  test("a FAILED poll carries missing rows over, marked stale", () => {
    const prev = [row("S-a"), row("S-b")];
    const out = retainRowsThroughFanoutGap(prev, [], true);
    expect(out.map((r) => r.sid).sort()).toEqual(["S-a", "S-b"]);
    expect(out.every((r) => r.retained_stale)).toBe(true);
  });

  test("rows the failed poll DID see stay fresh — retention never overwrites an observation", () => {
    const prev = [row("S-a", { status: "paused" }), row("S-b")];
    const out = retainRowsThroughFanoutGap(prev, [row("S-a", { status: "active" })], true);
    const a = out.find((r) => r.sid === "S-a")!;
    expect(a.status).toBe("active");
    expect(a.retained_stale).toBeUndefined();
    const b = out.find((r) => r.sid === "S-b")!;
    expect(b.retained_stale).toBe(true);
  });

  test("no previous rows (first ever poll) yields exactly the fresh rows", () => {
    expect(retainRowsThroughFanoutGap(null, [row("S-a")], true).map((r) => r.sid))
      .toEqual(["S-a"]);
    expect(retainRowsThroughFanoutGap(null, [], true)).toEqual([]);
  });

  test("does not mutate the previous array or its rows", () => {
    const prev = [row("S-a")];
    const snapshot = JSON.parse(JSON.stringify(prev));
    retainRowsThroughFanoutGap(prev, [], true);
    expect(prev).toEqual(snapshot);
  });

  test("repeated failing polls do not duplicate a carried row", () => {
    const prev = [row("S-a")];
    let out = retainRowsThroughFanoutGap(prev, [], true);
    out = retainRowsThroughFanoutGap(out, [], true);
    out = retainRowsThroughFanoutGap(out, [], true);
    expect(out.map((r) => r.sid)).toEqual(["S-a"]);
  });
});

describe("the two measured failure modes, end to end", () => {
  // A 1s restart gap landing inside one 10s poll: exactly one failing tick.
  test("RESTART GAP: list survives and the hard banner never fires", () => {
    const live = [row("S-a"), row("S-b")];
    let rows = live;
    let streak = 0;

    // tick 1: clean
    streak = 0;
    rows = retainRowsThroughFanoutGap(rows, live, false);
    expect(classifyFanoutPhase(streak)).toBe("ok");

    // tick 2: the restart gap — server returns [] plus errors
    streak += 1;
    rows = retainRowsThroughFanoutGap(rows, [], true);
    expect(classifyFanoutPhase(streak)).toBe("transient");
    expect(rows).toHaveLength(2); // the list did NOT blank
    expect(rows.every((r) => r.retained_stale)).toBe(true);

    // tick 3: worker is back
    streak = 0;
    rows = retainRowsThroughFanoutGap(rows, live, false);
    expect(classifyFanoutPhase(streak)).toBe("ok");
    expect(rows.every((r) => !r.retained_stale)).toBe(true);
  });

  test("PROCESS STALL: sustained banner fires and rows are labelled last-known", () => {
    const live = [row("S-a")];
    let rows = live;
    let streak = 0;
    for (let tick = 1; tick <= 6; tick++) {
      streak += 1;
      rows = retainRowsThroughFanoutGap(rows, [], true);
    }
    expect(classifyFanoutPhase(streak)).toBe("sustained");
    expect(rows).toHaveLength(1);
    expect(rows[0].retained_stale).toBe(true);
  });
});
