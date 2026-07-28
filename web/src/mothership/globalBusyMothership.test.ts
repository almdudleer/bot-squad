/**
 * T-0763 — the fleet busy-indicator's fan-out normalizes the sessions payload.
 *
 * THE DEFECT THIS PINS, and it was invisible rather than absent: `fanOutInFlight`
 * fetched per-project sessions through the raw `call<SessionRow[]>` escape
 * hatch, which asserts a shape instead of establishing one. The server answers
 * with the T-0601 envelope `{sessions, errors}`, so every ProjectSessions this
 * function produced carried an OBJECT where the aggregator's `for..of` expected
 * an array. It threw on every tick, inside GlobalBusyIndicator's catch — so on
 * a mothership build the indicator silently reported nothing, forever, with a
 * clean console. `apiFor(id).sessions()` applies normalizeSessionsPayload, the
 * same normalization the LOCAL fetcher already got from `api.sessions()`.
 *
 * The first test is the one that goes red on the raw-call version; the legacy
 * bare-array case is the negative guard — normalization must not start
 * mangling the older single-install shape the proxy still has to carry.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { fanOutInFlight } from "./globalBusyMothership";
import { invalidateServersCache } from "./api";

function routedFetch(sessionsBody: unknown) {
  return vi.fn(async (url: string) => {
    const body = url.endsWith("/api/m/servers")
      ? [{ id: "srv_a", display_name: "A", install_state: "ready" }]
      : url.endsWith("/api/projects")
        ? [{ slug: "bot-squad" }]
        : sessionsBody;
    return {
      ok: true,
      status: 200,
      text: async () => "",
      json: async () => body,
    } as Response;
  });
}

describe("fanOutInFlight sessions normalization (T-0763)", () => {
  beforeEach(() => invalidateServersCache());
  afterEach(() => {
    vi.restoreAllMocks();
    invalidateServersCache();
  });

  test("the {sessions, errors} envelope arrives as an ARRAY of rows", async () => {
    globalThis.fetch = routedFetch({
      sessions: [{ sid: "S-a", status: "active", task_id: "T-1", owner: "alex" }],
      errors: [],
    }) as unknown as typeof fetch;

    const results = await fanOutInFlight();
    expect(results).toHaveLength(1);
    const r = results[0];
    if (!r.ok) throw new Error("fan-out envelope should be ok");
    expect(Array.isArray(r.projects[0].sessions)).toBe(true);
    expect(r.projects[0].sessions.map((s) => s.sid)).toEqual(["S-a"]);
  });

  // NEGATIVE GUARD — older single-install servers behind the proxy still answer
  // with a bare array. Normalizing must pass it through untouched.
  test("a legacy bare array is carried through unchanged", async () => {
    globalThis.fetch = routedFetch([
      { sid: "S-legacy", status: "active", task_id: "T-2", owner: "alex" },
    ]) as unknown as typeof fetch;

    const results = await fanOutInFlight();
    const r = results[0];
    if (!r.ok) throw new Error("fan-out envelope should be ok");
    expect(r.projects[0].sessions.map((s) => s.sid)).toEqual(["S-legacy"]);
  });

  test("project/server identity still rides along with the rows", async () => {
    globalThis.fetch = routedFetch({ sessions: [], errors: [] }) as unknown as typeof fetch;
    const results = await fanOutInFlight();
    const r = results[0];
    if (!r.ok) throw new Error("fan-out envelope should be ok");
    expect(r.projects[0]).toMatchObject({
      serverId: "srv_a",
      serverName: "A",
      projectSlug: "bot-squad",
    });
  });
});
