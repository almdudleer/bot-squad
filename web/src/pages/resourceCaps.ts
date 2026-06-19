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
