/**
 * T-0113 — tests for the pure helpers behind the MOTHERSHIP /m/users page.
 *
 * The React component itself is exercised via the testable helpers: load-
 * state shape, the attached-server-count fallback (BE-incomplete until
 * T-0129), and the 403-gate detection. We keep the helpers exported so
 * the gating semantics are pinned without a DOM harness.
 */
import { describe, expect, test } from "vitest";

import { attachedServerCountText, isAccessDeniedError } from "./Users";
import type { GlobalUser } from "./api";

function gu(overrides: Partial<GlobalUser> = {}): GlobalUser {
  return {
    id: "gu_test",
    username: "alice",
    display_name: "",
    email: "",
    timezone: "UTC",
    is_super_admin: false,
    created_at: "2026-05-27T00:00:00Z",
    ...overrides,
  };
}

describe("attachedServerCountText", () => {
  test("renders an em-dash when the BE hasn't populated the field yet (T-0129)", () => {
    expect(attachedServerCountText(gu())).toBe("—");
  });

  test("renders the numeric count when present", () => {
    expect(attachedServerCountText(gu({ attached_servers: 3 }))).toBe("3");
  });

  test("renders 0 as the literal string '0' (not the em-dash fallback)", () => {
    // Regression: the BE distinguishes "no attachments" from "field not
    // populated"; the FE must not collapse 0 into the em-dash branch.
    expect(attachedServerCountText(gu({ attached_servers: 0 }))).toBe("0");
  });
});

describe("isAccessDeniedError", () => {
  test("matches the call() helper's 403 wrapper", () => {
    expect(
      isAccessDeniedError(new Error("API error 403: super-admin only")),
    ).toBe(true);
  });

  test("does not match 401 (call() redirects to /login before throwing)", () => {
    expect(
      isAccessDeniedError(new Error("API error 401: missing auth")),
    ).toBe(false);
  });

  test("does not match a 500 (the page renders the error message instead)", () => {
    expect(isAccessDeniedError(new Error("API error 500: boom"))).toBe(false);
  });

  test("does not match a network error string", () => {
    expect(isAccessDeniedError(new Error("Failed to fetch"))).toBe(false);
  });

  test("non-Error values are not treated as access-denied", () => {
    expect(isAccessDeniedError("API error 403: x")).toBe(false);
    expect(isAccessDeniedError(null)).toBe(false);
    expect(isAccessDeniedError(undefined)).toBe(false);
  });
});
