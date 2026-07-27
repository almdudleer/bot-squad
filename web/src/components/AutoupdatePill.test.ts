/**
 * T-0168: the AutoupdatePill once read `/api/autoupdate/status` raw — an
 * unguarded `status.last_apply_outcome.startsWith(...)` in `pickKind` threw
 * during render on a non-object / null / missing-field payload, and with no
 * error boundary that unmounted the ENTIRE Shell, not just the pill (surfaced
 * in the T-0167 walkthrough; reproduced in scenarios/T-0168 with playwright).
 *
 * These pin the two-layer defense:
 *  - `normalizeAutoupdateStatus` (data boundary) — non-object/null → `null`
 *    (pill renders nothing), every surviving field forced to a safe type.
 *  - `pickKind` / `pillLabel` (render logic) — never throw on any input.
 *
 * The four payload categories the DoD names: {non-object, null, missing-field,
 * valid}.
 */
import { describe, expect, test } from "vitest";

import { api, normalizeAutoupdateStatus, type AutoupdateStatus } from "../api";
import { isAutoupdateUnavailable, pickKind, pillLabel } from "./AutoupdatePill";

const VALID: AutoupdateStatus = {
  installed_version: "1.4.2",
  last_check_at: "2026-06-18T20:00:00Z",
  next_check_at: "2026-06-18T20:30:00Z",
  last_apply_at: null,
  last_apply_outcome: "ok",
  current_git_sha: "abc1234",
  paused: false,
  alert: null,
  pending_apply_version: null,
  mothership_url: null,
  poll_interval_seconds: 30,
};

describe("normalizeAutoupdateStatus — the four DoD payload categories", () => {
  test("NON-OBJECT payloads collapse to null (pill renders nothing)", () => {
    expect(normalizeAutoupdateStatus("oops")).toBeNull();
    expect(normalizeAutoupdateStatus(42)).toBeNull();
    expect(normalizeAutoupdateStatus(true)).toBeNull();
    expect(normalizeAutoupdateStatus(["a", "b"])).toBeNull();
  });

  test("NULL / undefined collapse to null", () => {
    expect(normalizeAutoupdateStatus(null)).toBeNull();
    expect(normalizeAutoupdateStatus(undefined)).toBeNull();
  });

  test("MISSING-FIELD payload → last_apply_outcome defaults to '' (never undefined)", () => {
    const { last_apply_outcome, ...missing } = VALID;
    void last_apply_outcome;
    const norm = normalizeAutoupdateStatus(missing);
    expect(norm).not.toBeNull();
    expect(norm!.last_apply_outcome).toBe("");
    // The exact pre-fix crash site is now safe.
    expect(() => pickKind(norm)).not.toThrow();
  });

  test("VALID payload passes through with fields intact", () => {
    const norm = normalizeAutoupdateStatus(VALID);
    expect(norm).toEqual(VALID);
  });

  test("wrong-typed fields are coerced to safe types, not preserved", () => {
    const norm = normalizeAutoupdateStatus({
      ...VALID,
      installed_version: 123, // number where a string|null is expected
      last_apply_outcome: { nope: 1 }, // object where a string is expected
      paused: "yes", // truthy non-boolean
      poll_interval_seconds: "30", // string where a number is expected
    });
    expect(norm).not.toBeNull();
    expect(norm!.installed_version).toBeNull();
    expect(norm!.last_apply_outcome).toBe("");
    expect(norm!.paused).toBe(false); // only strict `true` is paused
    expect(norm!.poll_interval_seconds).toBe(0);
  });

  test("a non-object `alert` is dropped to null (guards the render's alert.version read)", () => {
    const norm = normalizeAutoupdateStatus({ ...VALID, alert: "broken" });
    expect(norm!.alert).toBeNull();
  });

  test("a malformed `alert` object has its fields coerced to strings", () => {
    const norm = normalizeAutoupdateStatus({
      ...VALID,
      alert: { version: 9, step: null }, // missing/ wrong-typed fields
    });
    expect(norm!.alert).not.toBeNull();
    expect(typeof norm!.alert!.version).toBe("string");
    expect(typeof norm!.alert!.step).toBe("string");
  });
});

describe("pickKind — never throws, classifies normalized status", () => {
  test("null → dim, and does not throw", () => {
    expect(() => pickKind(null)).not.toThrow();
    expect(pickKind(null)).toBe("dim");
  });

  test("defends even against a raw object with a non-string last_apply_outcome", () => {
    // belt-and-suspenders: pickKind is guarded independent of normalize.
    const raw = { ...VALID, last_apply_outcome: undefined } as unknown as AutoupdateStatus;
    expect(() => pickKind(raw)).not.toThrow();
    expect(pickKind(raw)).toBe("ok");
  });

  test("a `failed:` outcome maps to danger", () => {
    expect(pickKind({ ...VALID, last_apply_outcome: "failed: build" })).toBe("danger");
  });

  test("an alert maps to danger; paused maps to warn; pending maps to active", () => {
    expect(
      pickKind({
        ...VALID,
        alert: {
          version: "1.5.0",
          step: "build",
          log_tail: "",
          occurred_at: "",
          retry_command: "",
          force_command: "",
        },
      }),
    ).toBe("danger");
    expect(pickKind({ ...VALID, paused: true })).toBe("warn");
    expect(pickKind({ ...VALID, pending_apply_version: "1.5.0" })).toBe("active");
  });
});

describe("pillLabel — never throws across the payload categories", () => {
  const now = Date.parse("2026-06-18T20:05:00Z");

  test("null → loading placeholder", () => {
    expect(() => pillLabel(null, now)).not.toThrow();
    expect(pillLabel(null, now)).toMatch(/loading/i);
  });

  test("normalized missing-field / valid payloads render without throwing", () => {
    const { last_apply_outcome, ...missing } = VALID;
    void last_apply_outcome;
    expect(() => pillLabel(normalizeAutoupdateStatus(missing), now)).not.toThrow();
    expect(() => pillLabel(normalizeAutoupdateStatus(VALID), now)).not.toThrow();
    expect(pillLabel(VALID, now)).toContain("1.4.2");
  });
});

/**
 * T-0731: the pill's mothership gate is BUILD-time (`VITE_MOTHERSHIP`, via the
 * Shell's tree-shake); the server's is RUNTIME (`MOTHERSHIP=1` → every
 * /api/autoupdate/* route 404s by design). When a bundle built without the flag
 * is served by a mothership API — a local `npm run dev` against the live
 * install, `multiserver_install_docker.sh`'s `--build-arg VITE_MOTHERSHIP=0`, a
 * stale docker layer — the pill polls a route that 404s every 30s forever.
 *
 * These pin the runtime half of the gate: `call()` tags its rejection with the
 * HTTP status, and only a 404 means "this install has no autoupdate — stop
 * asking". Everything else stays a transient failure the poll retries.
 */
describe("T-0731 — 404 means 'not on this install', not 'retry'", () => {
  const apiError = (status: number): Error =>
    new Error(`API error ${status}: nope`);

  test("ONLY 404 latches the poll off — the mothership's by-design answer", () => {
    expect(isAutoupdateUnavailable(apiError(404))).toBe(true);
  });

  test("transient failures do NOT latch it off (the poll must keep retrying)", () => {
    for (const status of [500, 502, 503, 504, 429, 400, 403]) {
      expect(isAutoupdateUnavailable(apiError(status))).toBe(false);
    }
    // A network failure / abort is not a 404 either.
    expect(isAutoupdateUnavailable(new TypeError("Failed to fetch"))).toBe(false);
    expect(isAutoupdateUnavailable(undefined)).toBe(false);
    // A 404 mentioned mid-message is not a 404 status (the decoder anchors).
    expect(isAutoupdateUnavailable(new Error("API error 500: upstream said 404"))).toBe(
      false,
    );
  });

  test("the real /status rejection is classified as unavailable", async () => {
    // Drives `call()` end-to-end so the decoder can't drift from the error
    // shape it decodes — the hand-built fixtures above pin the branches, this
    // pins that they match reality.
    const prev = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response("autoupdate not available on mothership", {
        status: 404,
      })) as unknown as typeof fetch;
    try {
      await expect(api.autoupdateStatus()).rejects.toThrow(/API error 404/);
      const caught = await api.autoupdateStatus().catch((e: unknown) => e);
      expect(isAutoupdateUnavailable(caught)).toBe(true);
    } finally {
      globalThis.fetch = prev;
    }
  });

  test("a 500 from the same route is NOT classified as unavailable", async () => {
    const prev = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response("boom", { status: 500 })) as unknown as typeof fetch;
    try {
      const caught = await api.autoupdateStatus().catch((e: unknown) => e);
      expect(isAutoupdateUnavailable(caught)).toBe(false);
    } finally {
      globalThis.fetch = prev;
    }
  });
});
