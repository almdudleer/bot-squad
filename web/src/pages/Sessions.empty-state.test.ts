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

  /**
   * T-0772 — the THIRD reading of the same empty list, and this page is where
   * it lands: the T-0080/T-0321 owner gate filtered every row out. Measured on
   * the live install as aqice (is_admin false, confirmed from /api/auth/me
   * after garbage-cookie and no-cookie 401 controls): the board's LIVE SESSIONS
   * card read 0, and clicking it landed here on "No sessions for bot-squad" —
   * a statement about the PROJECT, made from a per-user list, while 6 tasks
   * were in progress.
   */
  describe("T-0772: owner-scoped empty is not an empty project", () => {
    test("scoped list, no rows → 'none-own'", () => {
      expect(sessionsEmptyState([], [], "own")).toBe("none-own");
    });

    test("an ADMIN's empty list is still a genuinely empty project", () => {
      expect(sessionsEmptyState([], [], "all")).toBe("none");
    });

    test("UNKNOWN scope keeps the neutral 'none' — never a false 'you own none'", () => {
      // Legacy bare-array server / mothership proxy. Behaviour identical to
      // before this ticket, which is what the two-arg call sites above assert.
      expect(sessionsEmptyState([], [], null)).toBe("none");
      expect(sessionsEmptyState([], [])).toBe("none");
    });

    test("a DEAD WORKER still wins over the scope — 'uncertain', not 'none-own'", () => {
      // Both produce zero rows. Reporting an unreachable socket as "you own
      // none" would blame the owner gate for a tick where nothing was measured
      // — the same class of false statement this ticket exists to remove.
      expect(sessionsEmptyState([], [fanoutError("aqice")], "own")).toBe("uncertain");
    });

    test("rows present → no empty state regardless of scope", () => {
      expect(sessionsEmptyState([row()], [], "own")).toBeNull();
      expect(sessionsEmptyState([row()], [], "all")).toBeNull();
    });
  });
});
