/**
 * T-0726 — regression lock for the telemetry keep-last-good rule.
 *
 * The bug: `GET /api/projects/<slug>/telemetry` answers HTTP 200 with an EMPTY
 * body when the worker behind it times out. The caps strip held its last good
 * reading (T-0282); the Sessions Context column, consuming the SAME payload on
 * the SAME page, wrote it straight through — blanking every row to `—` and
 * collapsing the shared bar denominator to 0. These tests pin the Sessions
 * consumer against the exact empty-200 body the route returns.
 *
 * Manual walkthrough (staging, empty-200 injected, 2 poll ticks) passed first
 * per T-0158; this locks it.
 */
import { describe, expect, test } from "vitest";

import type { TelemetryResponse, TelemetrySession } from "../api";
import { contextCeilingOf, freshTelemetryReading, keepLastGoodTelemetry } from "./telemetry";

// The exact body routes_sessions.py returns on a worker timeout.
const EMPTY_200: TelemetryResponse = { sessions: [], quota: {}, caps: {} };

function session(sid: string, tokens: number): TelemetrySession {
  return {
    sid,
    context: { tokens, pct: tokens / 7000, ceiling: 700_000 },
    memory: { files: 0, bytes: 0, tokens_est: 0 },
  };
}

const GOOD: TelemetryResponse = {
  sessions: [session("S-a", 120_000), session("S-b", 300_000)],
  quota: { output_tokens_cum_total: 42 },
  caps: { max_parallel_sessions: 10, live_sessions: 10 },
};

describe("freshTelemetryReading", () => {
  test("a populated object/array is a fresh reading", () => {
    expect(freshTelemetryReading({ a: 1 })).toEqual({ a: 1 });
    expect(freshTelemetryReading([1])).toEqual([1]);
  });

  test("the empty-200 fillers are NOT readings", () => {
    expect(freshTelemetryReading({})).toBeNull();
    expect(freshTelemetryReading([])).toBeNull();
  });

  test("missing is not a reading", () => {
    expect(freshTelemetryReading(null)).toBeNull();
    expect(freshTelemetryReading(undefined)).toBeNull();
  });
});

describe("keepLastGoodTelemetry (Sessions consumer)", () => {
  test("an empty-200 holds the previous reading instead of wiping it", () => {
    expect(keepLastGoodTelemetry(GOOD, EMPTY_200)).toBe(GOOD);
  });

  test("holds across MORE than one poll tick (a flapping worker)", () => {
    let state: TelemetryResponse | null = GOOD;
    state = keepLastGoodTelemetry(state, EMPTY_200);
    state = keepLastGoodTelemetry(state, EMPTY_200);
    state = keepLastGoodTelemetry(state, EMPTY_200);
    expect(state).toBe(GOOD);
  });

  test("first paint is unaffected: no reading yet + empty-200 stays null (dim '—')", () => {
    expect(keepLastGoodTelemetry(null, EMPTY_200)).toBeNull();
  });

  // T-0726 follow-up (raised by the T-0331 dogfood finder on review): an empty
  // `sessions` array alone must NOT mean "no reading" — it is also the honest
  // answer for an idle install and for a non-admin whose owner-gate filtered
  // every row out. Only the all-empty degraded shape holds.
  test("zero live sessions with populated caps is a REAL reading (writes through)", () => {
    const idle: TelemetryResponse = {
      sessions: [],
      quota: { output_tokens_cum_total: 0 },
      caps: { max_parallel_sessions: 0, live_sessions: 0 },
    };
    expect(keepLastGoodTelemetry(GOOD, idle)).toBe(idle);
    expect(contextCeilingOf(keepLastGoodTelemetry(GOOD, idle))).toBe(0);
  });

  test("non-admin owner-gate filtering every row out also writes through", () => {
    // routes_sessions.py scopes `sessions` per owner but returns caps to everyone.
    const scoped: TelemetryResponse = { sessions: [], quota: {}, caps: { live_sessions: 10 } };
    expect(keepLastGoodTelemetry(GOOD, scoped)).toBe(scoped);
  });

  test("a populated payload wins — including the recovery tick", () => {
    expect(keepLastGoodTelemetry(null, GOOD)).toBe(GOOD);
    const next: TelemetryResponse = { ...GOOD, sessions: [session("S-a", 500_000)] };
    expect(keepLastGoodTelemetry(GOOD, next)).toBe(next);
  });
});

describe("contextCeilingOf", () => {
  test("derives the shared denominator from the payload (T-0264)", () => {
    expect(contextCeilingOf(GOOD)).toBe(700_000);
  });

  test("does NOT collapse to 0 on an empty-200 — the held reading still feeds it", () => {
    const held = keepLastGoodTelemetry(GOOD, EMPTY_200);
    expect(contextCeilingOf(held)).toBe(700_000);
  });

  test("no reading yet is 0 (ContextCell renders its dim placeholder anyway)", () => {
    expect(contextCeilingOf(null)).toBe(0);
  });
});
