/**
 * T-0127: unit tests for the pure peer-inbox helpers.
 *
 * Covers parseInboxLine (well-formed, tab-in-body, malformed/blank),
 * mergeMessages (dedupe, preserve-read, cap), and uiSidFor.
 */
import { describe, expect, it } from "vitest";

import {
  mergeMessages,
  parseInboxLine,
  uiSidFor,
  type StoredMsg,
} from "./peerInbox";

describe("uiSidFor", () => {
  it("builds the stable UI SID from a username", () => {
    expect(uiSidFor("alice")).toBe("S-alice-ui-p0");
    expect(uiSidFor("stakeholder")).toBe("S-stakeholder-ui-p0");
  });
});

describe("parseInboxLine", () => {
  it("parses a well-formed three-field line", () => {
    const line = "2026-06-18T10:00:00Z\t[from S-bob-dev-p1]\thello there";
    expect(parseInboxLine(line)).toEqual({
      ts: "2026-06-18T10:00:00Z",
      from: "S-bob-dev-p1",
      body: "hello there",
    });
  });

  it("preserves tabs inside the body (split into at most 3 fields)", () => {
    const line = "2026-06-18T10:00:00Z\t[from S-bob-dev-p1]\tcol1\tcol2\tcol3";
    expect(parseInboxLine(line)).toEqual({
      ts: "2026-06-18T10:00:00Z",
      from: "S-bob-dev-p1",
      body: "col1\tcol2\tcol3",
    });
  });

  it("falls back to the raw fromTag when it isn't [from ...] shaped", () => {
    const line = "2026-06-18T10:00:00Z\tweird-sender\tbody";
    expect(parseInboxLine(line)).toEqual({
      ts: "2026-06-18T10:00:00Z",
      from: "weird-sender",
      body: "body",
    });
  });

  it("is tolerant: a bare timestamp with no from/body still parses", () => {
    expect(parseInboxLine("2026-06-18T10:00:00Z")).toEqual({
      ts: "2026-06-18T10:00:00Z",
      from: "",
      body: "",
    });
  });

  it("returns null for a blank or whitespace-only line", () => {
    expect(parseInboxLine("")).toBeNull();
    expect(parseInboxLine("   ")).toBeNull();
    expect(parseInboxLine("\t\t")).toBeNull();
  });
});

describe("mergeMessages", () => {
  const line = "2026-06-18T10:00:00Z\t[from S-bob-dev-p1]\thello";

  it("appends new messages as unread", () => {
    const out = mergeMessages([], [line]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ from: "S-bob-dev-p1", body: "hello", read: false });
  });

  it("dedupes the same line seen twice (within one batch)", () => {
    const out = mergeMessages([], [line, line]);
    expect(out).toHaveLength(1);
  });

  it("dedupes an incoming line against the existing list", () => {
    const first = mergeMessages([], [line]);
    const second = mergeMessages(first, [line]);
    expect(second).toHaveLength(1);
  });

  it("preserves the read flag of an already-existing message on re-merge", () => {
    const first = mergeMessages([], [line]);
    const marked: StoredMsg[] = first.map((m) => ({ ...m, read: true }));
    const second = mergeMessages(marked, [line]);
    expect(second).toHaveLength(1);
    expect(second[0].read).toBe(true);
  });

  it("drops unparseable lines", () => {
    const out = mergeMessages([], ["", "   ", line]);
    expect(out).toHaveLength(1);
  });

  it("caps to the newest `cap` messages", () => {
    const lines = Array.from(
      { length: 10 },
      (_, i) => `2026-06-18T10:00:0${i}Z\t[from S-x-p1]\tmsg ${i}`,
    );
    const out = mergeMessages([], lines, 3);
    expect(out).toHaveLength(3);
    // newest kept → msg 7, 8, 9 in chronological order
    expect(out.map((m) => m.body)).toEqual(["msg 7", "msg 8", "msg 9"]);
  });
});
