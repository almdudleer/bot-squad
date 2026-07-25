/**
 * Tests for the shared non-terminal server display-state derivation (T-0653).
 *
 * Pins two things: (1) a `hold_reason` always wins over the elapsed-time
 * stale heuristic, no matter how old the install, and (2) the badge-class
 * mapping so `/m` and `/m/users` render identical vocabulary for the same
 * server row.
 */
import { describe, expect, test } from "vitest";

import {
  isStaleInstall,
  pendingInstallState,
  pendingStateBadgeClass,
  STALE_INSTALL_MS,
  type ServerStaleInput,
} from "./serverState";

function srv(overrides: Partial<ServerStaleInput> = {}): ServerStaleInput {
  return {
    install_state: "pending",
    created_at: "2026-07-01T00:00:00Z",
    last_seen_at: null,
    hold_reason: null,
    ...overrides,
  };
}

const NOW = new Date("2026-07-25T00:00:00Z");

describe("isStaleInstall", () => {
  test("false when fresh (under 30 min)", () => {
    const created = new Date(NOW.getTime() - STALE_INSTALL_MS + 1000).toISOString();
    expect(isStaleInstall(srv({ created_at: created }), NOW)).toBe(false);
  });

  test("true once past the 30-min threshold with no hold", () => {
    const created = new Date(NOW.getTime() - STALE_INSTALL_MS - 1000).toISOString();
    expect(isStaleInstall(srv({ created_at: created }), NOW)).toBe(true);
  });

  test("uses last_seen_at over created_at when present", () => {
    const created = new Date(NOW.getTime() - 999 * STALE_INSTALL_MS).toISOString();
    const seen = new Date(NOW.getTime() - 1000).toISOString();
    expect(
      isStaleInstall(srv({ created_at: created, last_seen_at: seen }), NOW),
    ).toBe(false);
  });

  test("ready and failed are never stale", () => {
    const created = new Date(NOW.getTime() - 999 * STALE_INSTALL_MS).toISOString();
    expect(isStaleInstall(srv({ install_state: "ready", created_at: created }), NOW)).toBe(false);
    expect(isStaleInstall(srv({ install_state: "failed", created_at: created }), NOW)).toBe(false);
  });

  test("T-0653: a hold_reason overrides staleness no matter how old", () => {
    // The real linza row: created ~5 weeks before `now`, no heartbeat ever.
    const created = "2026-06-20T15:08:31Z";
    expect(
      isStaleInstall(
        srv({ created_at: created, hold_reason: "awaiting stakeholder GO (T-0328)" }),
        NOW,
      ),
    ).toBe(false);
  });
});

describe("pendingInstallState", () => {
  test("held wins even when also past the stale threshold", () => {
    const created = "2026-06-20T15:08:31Z";
    expect(
      pendingInstallState(srv({ created_at: created, hold_reason: "gated on T-0328" }), NOW),
    ).toBe("held");
  });

  test("stalled when old and not held", () => {
    const created = "2026-06-20T15:08:31Z";
    expect(pendingInstallState(srv({ created_at: created }), NOW)).toBe("stalled");
  });

  test("pending when fresh and not held", () => {
    const created = new Date(NOW.getTime() - 1000).toISOString();
    expect(pendingInstallState(srv({ created_at: created }), NOW)).toBe("pending");
  });
});

describe("pendingStateBadgeClass", () => {
  test("held is informational, not alarming (not danger)", () => {
    expect(pendingStateBadgeClass("held")).toBe("mc-badge mc-badge-info");
  });

  test("stalled stays danger", () => {
    expect(pendingStateBadgeClass("stalled")).toBe("mc-badge mc-badge-danger");
  });

  test("pending stays warn", () => {
    expect(pendingStateBadgeClass("pending")).toBe("mc-badge mc-badge-warn");
  });
});
