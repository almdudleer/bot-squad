/**
 * T-0282 / T-0726 — the ONE keep-last-good rule for
 * `GET /api/projects/<slug>/telemetry`.
 *
 * When the worker behind that route times out or crashes it answers
 * **HTTP 200** with `{"sessions": [], "quota": {}, "caps": {}}`
 * (`api/app/routes_sessions.py`) — an EMPTY payload, not an error any
 * `.catch()` would ever see. Every consumer polls the route at 10s, so writing
 * an empty read straight through turns a rare transient worker blip into a
 * visible flicker for up to a full tick:
 *
 * - the caps strip blanked back to its skeleton (T-0282, fixed there first);
 * - the Sessions Context column blanked EVERY row to its dim `—` placeholder
 *   and collapsed the shared bar denominator to 0 (T-0726) — the same page,
 *   the same payload, the opposite behaviour, because the rule had been
 *   hand-rolled in only one of the two consumers.
 *
 * So the rule lives here once and both consumers call it: an empty object /
 * empty array means "no fresh reading" — hold the previous one; only a
 * populated reading wins. First paint is unaffected: with nothing held yet the
 * consumer's own no-data state (skeleton / dim `—`) still shows.
 */
import type { TelemetryResponse } from "../api";

/**
 * Return `reading` when it is a real reading, or `null` when it is the
 * route's empty-200 filler. `null`/`undefined` are also "no reading".
 */
export function freshTelemetryReading<T extends object>(
  reading: T | null | undefined,
): T | null {
  if (reading == null) return null;
  const empty = Array.isArray(reading)
    ? reading.length === 0
    : Object.keys(reading).length === 0;
  return empty ? null : reading;
}

/**
 * The Sessions/Processes consumer's write rule (T-0726): the per-session
 * telemetry it needs lives in `sessions`, so an empty `sessions` array means
 * the whole response carries nothing worth writing — keep `prev`.
 */
export function keepLastGoodTelemetry(
  prev: TelemetryResponse | null,
  incoming: TelemetryResponse | null | undefined,
): TelemetryResponse | null {
  if (!incoming) return prev;
  return freshTelemetryReading(incoming.sessions) ? incoming : prev;
}

/**
 * T-0230/T-0264: the Context column's shared denominator. The contract ceiling
 * is tunable and worker-stamped per session, so derive it from the payload
 * rather than hard-coding it. Fed the HELD reading (see
 * `keepLastGoodTelemetry`) it no longer collapses to 0 on an empty-200.
 */
export function contextCeilingOf(telemetry: TelemetryResponse | null): number {
  return Math.max(0, ...(telemetry?.sessions ?? []).map((s) => s.context?.ceiling ?? 0));
}
