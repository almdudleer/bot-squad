/**
 * T-0240: pure helpers behind the resource-caps UI. Manual walkthrough
 * (scenarios/T-0240-*.md) passed first; these lock the logic.
 */
import { describe, expect, test } from "vitest";

import { SessionRow, TelemetryResponse } from "../api";
import {
  PARALLEL_SESSION_CEILING,
  ProjectUtilization,
  admissionLimit,
  aggregateUtilization,
  capDisplay,
  capInputError,
  capSoftWarning,
  isAtCapacity,
  isOverCap,
  isThrottled,
  sanitizeCapInput,
  utilizationPct,
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

// T-0282 — manual walkthrough (scenarios/T-0282-*.md) passed first; these lock
// the strip/meter logic behind the live caps + backoff readout.
describe("isAtCapacity", () => {
  test("unlimited never reaches capacity", () => expect(isAtCapacity(999, 0)).toBe(false));
  test("AT the cap IS capacity reached (unlike isOverCap)", () =>
    expect(isAtCapacity(2, 2)).toBe(true));
  test("above the cap is still capacity reached", () => expect(isAtCapacity(3, 2)).toBe(true));
  test("below the cap is not", () => expect(isAtCapacity(1, 2)).toBe(false));
});

describe("isThrottled", () => {
  test("finite effective limit below a finite cap = throttled", () =>
    expect(isThrottled(15, 8)).toBe(true));
  test("effective_limit 0 means unlimited/no pressure, never a throttle", () =>
    expect(isThrottled(15, 0)).toBe(false));
  test("an UNLIMITED cap depressed to a finite limit IS a throttle (shipped default caps 0/0)", () =>
    expect(isThrottled(0, 1806)).toBe(true));
  test("effective limit equal to the cap is not a throttle", () =>
    expect(isThrottled(15, 15)).toBe(false));
  test("effective limit above the cap is not a throttle", () =>
    expect(isThrottled(8, 15)).toBe(false));
});

describe("admissionLimit", () => {
  test("throttled → the governor's depressed limit gates admission", () =>
    expect(admissionLimit(15, 8)).toBe(8));
  test("not throttled → the hard cap gates admission", () =>
    expect(admissionLimit(15, 0)).toBe(15));
  test("unlimited cap + finite throttle → the throttle", () =>
    expect(admissionLimit(0, 1806)).toBe(1806));
  test("wholly unlimited → 0 (capacity can never be reached)", () =>
    expect(admissionLimit(0, 0)).toBe(0));
});

describe("utilizationPct", () => {
  test("unlimited cap → null (no percentage; must not read as 0%)", () =>
    expect(utilizationPct(150_000, 0)).toBeNull());
  test("rounds to a whole percent", () => expect(utilizationPct(1_700_000, 2_000_000)).toBe(85));
  test("not clamped — a cap lowered under current usage reads >100%", () =>
    expect(utilizationPct(3, 2)).toBe(150));
  test("zero usage against a finite cap is 0%", () => expect(utilizationPct(0, 10)).toBe(0));
});

describe("capacity-reached against a BACKOFF-THROTTLED ceiling (the walkthrough case)", () => {
  // 12 live, hard cap 15, governor throttled to 8: "12/15" alone looks like
  // headroom, but nothing further admits — the strip must go red.
  const hardCap = 15;
  const effLimit = 8;
  const live = 12;
  test("throttle is detected", () => expect(isThrottled(hardCap, effLimit)).toBe(true));
  test("admission is gated by the throttle, not the hard cap", () =>
    expect(admissionLimit(hardCap, effLimit)).toBe(8));
  test("capacity IS reached even though live < hard cap", () =>
    expect(isAtCapacity(live, admissionLimit(hardCap, effLimit))).toBe(true));
  test("and would NOT be reported reached against the hard cap alone", () =>
    expect(isAtCapacity(live, hardCap)).toBe(false));
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
