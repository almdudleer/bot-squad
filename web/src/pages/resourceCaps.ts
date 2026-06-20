/**
 * T-0240 (Pillar A): pure helpers for the resource-caps UI.
 *
 * Caps are a SERVER-WIDE policy (max simultaneously-live sessions + aggregate
 * tokens), but the sessions/telemetry endpoints are project-scoped. So the caps
 * UI fans out over projects and sums client-side. Live-session counting reuses
 * the canonical `isLiveSession` predicate (T-0232); token usage mirrors the
 * telemetry quota (`output_tokens_cum_total`) the backend cap enforces against
 * (T-0239 slice 2).
 */
import { SessionRow, TelemetryResponse } from "../api";
import { isLiveSession } from "./Sessions";

// 0 (or absent) = unlimited, matching the T-0239 API contract.
export const UNLIMITED = 0;

export function capDisplay(cap: number): string {
  return cap === UNLIMITED ? "Unlimited" : cap.toLocaleString();
}

/** Non-negative integer; 0 = unlimited. Mirrors the API's PUT validation. */
export function validateCapInput(value: number, label: string): string | null {
  if (!Number.isInteger(value) || value < 0) {
    return `${label} must be a non-negative integer (0 = unlimited)`;
  }
  return null;
}

// T-0310: documented hard ceiling for simultaneously-live sessions
// (guidance §PART 5; the AIMD backoff sentinel is 10_000). Exceeding it is a
// SOFT warning, not a hard block — an admin may intentionally raise it.
export const PARALLEL_SESSION_CEILING = 15;

/**
 * T-0310: strip a raw cap-input string to digits only. Rejects a typed minus
 * sign, decimal point, or `e`-notation AT INPUT TIME, so the field can never
 * silently hold a negative/fractional value. Empty stays empty (the caller
 * distinguishes "" from "0" — see `capInputError`).
 */
export function sanitizeCapInput(raw: string): string {
  return raw.replace(/[^0-9]/g, "");
}

/**
 * T-0310: inline (pre-Save) error for a raw cap-input string. An EMPTY field is
 * an error — it must NOT silently coerce to 0 (= unlimited), which would
 * uncap the whole system. The explicit `0` is the only way to mean unlimited.
 */
export function capInputError(raw: string, label: string): string | null {
  const t = raw.trim();
  if (t === "") {
    return `${label}: enter a value (type 0 for unlimited).`;
  }
  if (!/^[0-9]+$/.test(t)) {
    return `${label} must be a non-negative integer (0 = unlimited).`;
  }
  return null;
}

/**
 * T-0310: soft over-ceiling warning (non-blocking). Returns null when the value
 * is unlimited (0), invalid, or within the ceiling.
 */
export function capSoftWarning(
  value: number,
  ceiling: number,
  label: string,
): string | null {
  if (!Number.isInteger(value) || value <= UNLIMITED || value <= ceiling) {
    return null;
  }
  return `${label} of ${value.toLocaleString()} is above the documented ceiling of ${ceiling}.`;
}

/** Fill ratio for the Task-Manager bar; null when unlimited (no bar). */
export function utilizationRatio(used: number, cap: number): number | null {
  if (cap <= UNLIMITED) return null;
  return used / cap;
}

/** Over-cap iff a finite cap is set and current usage exceeds it. */
export function isOverCap(used: number, cap: number): boolean {
  return cap > UNLIMITED && used > cap;
}

export type ProjectUtilization = {
  sessions: SessionRow[] | null;
  telemetry: TelemetryResponse | null;
};

/**
 * Sum live-session count + cumulative output tokens across every project.
 * A null slot (a failed/forbidden per-project fetch) contributes 0 rather than
 * sinking the whole readout.
 */
export function aggregateUtilization(
  perProject: ProjectUtilization[],
): { liveSessions: number; totalTokens: number } {
  let liveSessions = 0;
  let totalTokens = 0;
  for (const p of perProject) {
    if (p.sessions) liveSessions += p.sessions.filter(isLiveSession).length;
    if (p.telemetry) totalTokens += p.telemetry.quota.output_tokens_cum_total ?? 0;
  }
  return { liveSessions, totalTokens };
}
