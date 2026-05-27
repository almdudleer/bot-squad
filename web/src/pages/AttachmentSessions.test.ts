/**
 * T-0061 — filter helper for "my sessions" on the ATTACHMENT page. Matches
 * the server-side ownership gate in ``api/app/routes_sessions.py`` so the
 * displayed rows are exactly the ones the user can act on.
 */
import { describe, expect, test } from "vitest";

import type { SessionRow } from "../api";
import { isMySession } from "./AttachmentSessions";

function row(over: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-almdudleer-test-p1",
    status: "active",
    window: "dev",
    cwd: "/tmp",
    ...over,
  } as SessionRow;
}

describe("isMySession (T-0061)", () => {
  test("post-T-0080 sessions: match by UI owner username", () => {
    expect(
      isMySession(row({ owner: "alice" }), { username: "alice", linux_user: "alice" }),
    ).toBe(true);
    expect(
      isMySession(row({ owner: "bob" }), { username: "alice", linux_user: "alice" }),
    ).toBe(false);
  });

  test("post-T-0080: owner present + mismatched → not mine, even if SID prefix matches", () => {
    // Once owner is set, the SID prefix is no longer authoritative —
    // multiple UI users can share a linux_user, and owner is the real
    // identity boundary. Mirror the server-side _check_sid_ownership rule.
    expect(
      isMySession(
        row({ sid: "S-almdudleer-test-p1", owner: "other-ui-user" }),
        { username: "alice", linux_user: "almdudleer" },
      ),
    ).toBe(false);
  });

  test("legacy pre-T-0080 sessions: owner empty → fall back to SID linux_user prefix", () => {
    expect(
      isMySession(
        row({ sid: "S-almdudleer-test-p1", owner: "" }),
        { username: "alice", linux_user: "almdudleer" },
      ),
    ).toBe(true);
    expect(
      isMySession(
        row({ sid: "S-bob-test-p2", owner: "" }),
        { username: "alice", linux_user: "almdudleer" },
      ),
    ).toBe(false);
  });

  test("legacy: owner field absent entirely (not even empty) → SID-prefix fallback", () => {
    const r = row({ sid: "S-almdudleer-tl-p3" });
    delete (r as Partial<SessionRow>).owner;
    expect(isMySession(r, { username: "x", linux_user: "almdudleer" })).toBe(true);
  });

  test("unparseable SID with empty owner → not mine (defensive)", () => {
    expect(
      isMySession(
        row({ sid: "weird-format", owner: "" }),
        { username: "alice", linux_user: "almdudleer" },
      ),
    ).toBe(false);
  });
});
