/**
 * T-0628 (D-0056): TelemetryPanel dissolves — its per-session context-token
 * cell merges into the Sessions main table. These pure helpers carried over
 * verbatim from the old TelemetryPanel (fmtTokens / context % bucketing);
 * porting them here so the merge keeps coverage of the ok/warn/danger
 * vocabulary and the k/M/B token-tier rollup.
 */
import { describe, expect, test } from "vitest";

import { contextBadgeKind, fmtContextTokens } from "./Sessions";

describe("contextBadgeKind", () => {
  test("under 80% is ok", () => {
    expect(contextBadgeKind(0)).toBe("ok");
    expect(contextBadgeKind(79.9)).toBe("ok");
  });

  test("80-99% is warn", () => {
    expect(contextBadgeKind(80)).toBe("warn");
    expect(contextBadgeKind(99.9)).toBe("warn");
  });

  test("100%+ is danger", () => {
    expect(contextBadgeKind(100)).toBe("danger");
    expect(contextBadgeKind(150)).toBe("danger");
  });
});

describe("fmtContextTokens", () => {
  test("small counts render as-is", () => {
    expect(fmtContextTokens(0)).toBe("0");
    expect(fmtContextTokens(999)).toBe("999");
  });

  test("thousands roll into k tier", () => {
    expect(fmtContextTokens(1500)).toBe("1.5k");
    expect(fmtContextTokens(150000)).toBe("150k");
  });

  test("millions roll into M tier (T-0267 regression: 7.5M not 7479k)", () => {
    expect(fmtContextTokens(7479374)).toBe("7.5M");
    expect(fmtContextTokens(150000000)).toBe("150M");
  });

  test("billions roll into B tier", () => {
    expect(fmtContextTokens(1500000000)).toBe("1.5B");
  });
});
