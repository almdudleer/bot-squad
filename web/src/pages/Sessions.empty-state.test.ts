/**
 * T-0658: `GET /api/projects/{slug}/sessions` fans out to every configured
 * worker socket and folds per-socket failures into an `errors` array without
 * aborting the request — a poll tick where EVERY socket times out still
 * returns `rows: []`. The page's old empty-state check only looked at
 * `sessions.length === 0`, so a transient "every socket unreachable" tick
 * was indistinguishable from "this project genuinely has no sessions".
 * `sessionsEmptyState` is the pure classifier the page renders off of;
 * unit tested here without needing to render the component (this repo has
 * no React Testing Library / jsdom rendering harness — see `isLiveSession`
 * in Sessions.live.test.ts for the same pattern).
 */
import { describe, expect, test } from "vitest";

import { WorkerFanoutError, SessionRow } from "../api";
import { sessionsEmptyState } from "./Sessions";

function row(overrides: Partial<SessionRow> = {}): SessionRow {
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

function fanoutError(user = "alice"): WorkerFanoutError {
  return { user, detail: "connect timeout" };
}

describe("sessionsEmptyState", () => {
  test("still loading (sessions null) → no empty state at all", () => {
    expect(sessionsEmptyState(null, [])).toBeNull();
    expect(sessionsEmptyState(null, [fanoutError()])).toBeNull();
  });

  test("rows present → no empty state, even with stale fanout errors", () => {
    expect(sessionsEmptyState([row()], [])).toBeNull();
    expect(sessionsEmptyState([row()], [fanoutError()])).toBeNull();
  });

  test("genuinely empty project (no rows, no fanout errors) → 'none'", () => {
    expect(sessionsEmptyState([], [])).toBe("none");
  });

  test("all-sockets-timeout poll tick (no rows, fanout errors present) → 'uncertain', NOT 'none'", () => {
    expect(sessionsEmptyState([], [fanoutError("alice"), fanoutError("bob")])).toBe(
      "uncertain",
    );
  });
});
