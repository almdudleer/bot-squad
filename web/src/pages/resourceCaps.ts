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
// T-0339: read the liveness predicate straight off the canonical
// `sessionLiveness` helper (utils/sessionStatus) rather than `isLiveSession`
// from Sessions.tsx. `isLiveSession` is only the boolean projection of
// `sessionLiveness` (T-0340), and importing it pulled the whole Sessions page
// into this helper — creating a Sessions → ResourceCapsPanel → resourceCaps →
// Sessions import cycle once the caps panel mounts in the process view. Reading
// the shared util keeps the predicate identical with no cycle.
import { sessionLiveness } from "../utils/sessionStatus";

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

/**
 * T-0282: CAPACITY REACHED — a finite cap is set and usage is at or above it,
 * so nothing further admits. Distinct from `isOverCap` (strictly above): being
 * exactly AT the cap is already the operator-relevant state ("no more spawns"),
 * and it's the common one, since admission clamps at the limit rather than
 * overshooting it.
 */
export function isAtCapacity(used: number, cap: number): boolean {
  return cap > UNLIMITED && used >= cap;
}

/**
 * T-0282: is the WS-4 AIMD backoff governor holding admission BELOW the hard
 * ceiling? An `effective_limit` of 0 means unlimited/no pressure (the worker
 * normalises its 10_000 sentinel to 0 on the wire), so it is never a throttle.
 * A hard cap of 0 means UNLIMITED (∞), not "no throttle" — the shipped default
 * is caps 0/0 with backoff ON, so the governor can depress an otherwise-infinite
 * ceiling to a finite limit, and that IS a throttle the operator must see.
 */
export function isThrottled(hardCap: number, effectiveLimit: number): boolean {
  return effectiveLimit > UNLIMITED && effectiveLimit < (hardCap || Infinity);
}

/**
 * T-0282: the limit spawn admission ACTUALLY gates on — the depressed effective
 * limit while the governor is throttling, else the hard cap. 0 = unlimited
 * (neither is finite), which is why capacity can never be "reached".
 */
export function admissionLimit(hardCap: number, effectiveLimit: number): number {
  return isThrottled(hardCap, effectiveLimit) ? effectiveLimit : hardCap;
}

/**
 * T-0282: whole-percent utilization for the at-a-glance strip. null when the cap
 * is unlimited — an unlimited cap has no percentage, and rendering `0%` there
 * would read as "nothing used" rather than "no limit". Not clamped: >100% is
 * real information (a cap lowered below current usage).
 */
export function utilizationPct(used: number, cap: number): number | null {
  const ratio = utilizationRatio(used, cap);
  return ratio === null ? null : Math.round(ratio * 100);
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
    if (p.sessions)
      liveSessions += p.sessions.filter((s) => sessionLiveness(s) === "live").length;
    if (p.telemetry) totalTokens += p.telemetry.quota.output_tokens_cum_total ?? 0;
  }
  return { liveSessions, totalTokens };
}
