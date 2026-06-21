import { describe, it, expect } from "vitest";
import { normalizeId } from "./normalizeId";

// T-0425: TS mirror of p67's shared normalize_id contract (T-0424). These are
// the exact edge cases from the published spec — keep byte-for-byte identical.
describe("normalizeId (T-0424 contract)", () => {
  it("strips exactly one trailing literal lowercase .md", () => {
    expect(normalizeId("ui-polish")).toBe("ui-polish");
    expect(normalizeId("ui-polish.md")).toBe("ui-polish");
    expect(normalizeId("x.md.md")).toBe("x.md");   // not greedy
    expect(normalizeId(".md")).toBe("");
    expect(normalizeId("README.MD")).toBe("README.MD"); // case-sensitive, not stripped
    expect(normalizeId("")).toBe("");
    expect(normalizeId(null)).toBe("");
    expect(normalizeId(undefined)).toBe("");
  });
  it("equates a bare stem with its .md form at the comparison boundary", () => {
    expect(normalizeId("ui-polish") === normalizeId("ui-polish.md")).toBe(true);
  });
});
