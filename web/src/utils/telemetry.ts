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
 * The Sessions/Processes consumer's write rule (T-0726): hold `prev` only for a
 * response that carries NOTHING — every block empty.
 *
 * Deliberately not "sessions is empty" alone. An empty `sessions` array is
 * genuinely ambiguous: it is what the degraded 200 sends, but it is ALSO the
 * honest answer in two healthy cases —
 *
 *  1. a truly idle install with zero live sessions;
 *  2. a NON-ADMIN caller whose owner-gate filtered every row out
 *     (`routes_sessions.py` scopes `sessions` per owner but returns `quota` and
 *     `caps` to everyone) — live-reachable today, not hypothetical.
 *
 * Holding on those would pin a stale reading with no way to fall out of it. The
 * whole-payload test disambiguates exactly, because the degraded shape is
 * `{"sessions": [], "quota": {}, "caps": {}}` from the route's two `except`
 * branches and NOTHING else produces it: on the healthy path `caps` comes from
 * `sessions.py::caps_utilization`, which always returns a populated dict (cap
 * config + live counts) even when zero sessions are live. So "all three empty"
 * means "the worker did not answer", and an empty `sessions` next to populated
 * caps is a real reading that writes through — rows correctly fall to `—`.
 */
export function keepLastGoodTelemetry(
  prev: TelemetryResponse | null,
  incoming: TelemetryResponse | null | undefined,
): TelemetryResponse | null {
  if (!incoming) return prev;
  const carriesNothing =
    !freshTelemetryReading(incoming.sessions) &&
    !freshTelemetryReading(incoming.caps) &&
    !freshTelemetryReading(incoming.quota);
  return carriesNothing ? prev : incoming;
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
