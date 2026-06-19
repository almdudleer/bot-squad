/**
 * T-0232 (Pillar A): the sessions VIEW shows LIVE-only rows — alive in tmux
 * (running / idle / paused). Suspended + archived rows are dropped from the
 * view (retained internally). `isLiveSession` is the pure predicate the
 * Sessions table filters on; exported for unit testing.
 *
 * Liveness precedence:
 *   - archived               → never live (it lives in the Archived section)
 *   - paused                 → live (Ctrl-C interrupt, pane still open + Resume-able)
 *   - explicit `live` flag    → preferred when present (Team-1 stamps running/idle)
 *   - else activity probe     → running/idle live, suspended dead
 */
import { describe, expect, test } from "vitest";

import { SessionRow } from "../api";
import { isLiveSession } from "./Sessions";

function row(overrides: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-test-p0",
    status: "active",
    window: "w",
    cwd: "/tmp",
    task_id: null,
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  };
}

describe("isLiveSession", () => {
  test("running row is live", () => {
    expect(isLiveSession(row({ activity: "running", live: true }))).toBe(true);
  });

  test("idle row is live", () => {
    expect(isLiveSession(row({ status: "active", activity: "idle", live: true }))).toBe(true);
  });

  test("suspended row is NOT live", () => {
    expect(
      isLiveSession(row({ status: "suspended", activity: "suspended", live: false })),
    ).toBe(false);
  });

  test("archived row is never live even if flagged live", () => {
    expect(
      isLiveSession(row({ activity: "running", live: true, archived: true })),
    ).toBe(false);
  });

  test("paused row stays live (Resume-able pane) despite live=false", () => {
    expect(
      isLiveSession(row({ status: "paused", activity: "paused", live: false })),
    ).toBe(true);
  });

  test("zombie status=active but activity=suspended is NOT live", () => {
    // T-0104: the worker reclassifies a dead pane's activity to suspended even
    // when the raw md status is still 'active'. Live filter must follow activity.
    expect(
      isLiveSession(row({ status: "active", activity: "suspended", live: false })),
    ).toBe(false);
  });

  test("pre-T-0232 worker (no `live` field) falls back to activity probe", () => {
    expect(isLiveSession(row({ activity: "running" }))).toBe(true);
    expect(isLiveSession(row({ activity: "suspended", status: "suspended" }))).toBe(false);
  });
});
