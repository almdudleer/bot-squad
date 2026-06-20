/**
 * T-0240: pure helpers behind the resource-caps UI. Manual walkthrough
 * (scenarios/T-0240-*.md) passed first; these lock the logic.
 */
import { describe, expect, test } from "vitest";

import { SessionRow, TelemetryResponse } from "../api";
import {
  PARALLEL_SESSION_CEILING,
  ProjectUtilization,
  aggregateUtilization,
  capDisplay,
  capInputError,
  capSoftWarning,
  isOverCap,
  sanitizeCapInput,
  utilizationRatio,
  validateCapInput,
} from "./resourceCaps";

function row(overrides: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-test",
    status: "active",
    window: "w",
    cwd: "/tmp",
    task_id: null,
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  };
}

function telemetry(cumTokens: number): TelemetryResponse {
  return {
    sessions: [],
    quota: { output_tokens_cum_total: cumTokens, rate_limit_429: { count: 0, last_at: null } },
  };
}

describe("capDisplay", () => {
  test("0 = Unlimited", () => expect(capDisplay(0)).toBe("Unlimited"));
  test("finite cap is locale-formatted", () => expect(capDisplay(1500)).toBe("1,500"));
});

describe("validateCapInput", () => {
  test("0 (unlimited) and positive ints are valid", () => {
    expect(validateCapInput(0, "Cap")).toBeNull();
    expect(validateCapInput(5, "Cap")).toBeNull();
  });
  test("negative is rejected", () => {
    expect(validateCapInput(-1, "Max parallel sessions")).toMatch(/non-negative integer/);
  });
  test("non-integer is rejected", () => {
    expect(validateCapInput(1.5, "Cap")).toMatch(/non-negative integer/);
    expect(validateCapInput(Number.NaN, "Cap")).toMatch(/non-negative integer/);
  });
});

// T-0310: input-time validation helpers.
describe("sanitizeCapInput", () => {
  test("strips a typed minus sign (no silent negative)", () =>
    expect(sanitizeCapInput("-5")).toBe("5"));
  test("strips decimals and e-notation", () => {
    expect(sanitizeCapInput("1.5")).toBe("15");
    expect(sanitizeCapInput("1e9")).toBe("19");
  });
  test("empty stays empty (distinguishable from 0)", () =>
    expect(sanitizeCapInput("")).toBe(""));
  test("plain digits pass through", () => expect(sanitizeCapInput("999")).toBe("999"));
});

describe("capInputError", () => {
  test("empty is an error — must NOT silently become 0=unlimited", () => {
    expect(capInputError("", "Max parallel sessions")).toMatch(/enter a value/i);
    expect(capInputError("   ", "Max parallel sessions")).toMatch(/enter a value/i);
  });
  test("explicit 0 is valid (means unlimited)", () =>
    expect(capInputError("0", "Cap")).toBeNull());
  test("positive integer is valid", () => expect(capInputError("8", "Cap")).toBeNull());
  test("non-integer string is rejected", () =>
    expect(capInputError("1.5", "Cap")).toMatch(/non-negative integer/));
});

describe("capSoftWarning", () => {
  test("within ceiling → no warning", () =>
    expect(capSoftWarning(8, PARALLEL_SESSION_CEILING, "Max parallel sessions")).toBeNull());
  test("at ceiling → no warning", () =>
    expect(capSoftWarning(15, PARALLEL_SESSION_CEILING, "Cap")).toBeNull());
  test("unlimited (0) → no warning", () =>
    expect(capSoftWarning(0, PARALLEL_SESSION_CEILING, "Cap")).toBeNull());
  test("above ceiling → warns and names the ceiling", () => {
    const w = capSoftWarning(999999, PARALLEL_SESSION_CEILING, "Max parallel sessions");
    expect(w).toMatch(/above the documented ceiling of 15/);
  });
});

describe("utilizationRatio", () => {
  test("unlimited cap → null (no bar)", () => expect(utilizationRatio(150_000, 0)).toBeNull());
  test("partial fill", () => expect(utilizationRatio(3, 5)).toBeCloseTo(0.6));
  test("over-cap ratio exceeds 1", () => expect(utilizationRatio(3, 2)).toBeCloseTo(1.5));
});

describe("isOverCap", () => {
  test("unlimited is never over", () => expect(isOverCap(999, 0)).toBe(false));
  test("at cap is not over", () => expect(isOverCap(2, 2)).toBe(false));
  test("above cap is over", () => expect(isOverCap(3, 2)).toBe(true));
});

describe("aggregateUtilization", () => {
  test("sums live sessions (reusing isLiveSession) + cumulative tokens across projects", () => {
    const perProject: ProjectUtilization[] = [
      {
        sessions: [
          row({ activity: "running", live: true }),
          row({ activity: "idle", live: true }),
          row({ status: "suspended", activity: "suspended", live: false }),
        ],
        telemetry: telemetry(120_000),
      },
      {
        sessions: [row({ status: "paused", activity: "paused", live: false })], // paused stays live
        telemetry: telemetry(30_000),
      },
    ];
    expect(aggregateUtilization(perProject)).toEqual({ liveSessions: 3, totalTokens: 150_000 });
  });

  test("null per-project fetches contribute 0 rather than sinking the readout", () => {
    const perProject: ProjectUtilization[] = [
      { sessions: [row({ activity: "running", live: true })], telemetry: null },
      { sessions: null, telemetry: telemetry(7_000) },
    ];
    expect(aggregateUtilization(perProject)).toEqual({ liveSessions: 1, totalTokens: 7_000 });
  });

  test("missing output_tokens_cum_total counts as 0", () => {
    const t: TelemetryResponse = { sessions: [], quota: { rate_limit_429: { count: 0, last_at: null } } };
    expect(aggregateUtilization([{ sessions: [], telemetry: t }])).toEqual({
      liveSessions: 0,
      totalTokens: 0,
    });
  });
});
