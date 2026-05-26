/**
 * T-0068: pin the self-server URL surface so the apiFor() swap can't
 * silently steal traffic away from the local install. These tests are
 * the "sibling" of `mothership/api.test.ts` — for every method on
 * apiFor() exercised there, the corresponding singleton method here must
 * still hit the un-proxied `/api/...` path. If the two diverge in
 * unexpected ways, the cross-server route is no longer transparent and
 * deep-links into `/p/:slug` are at risk.
 */
import { afterEach, describe, expect, test, vi } from "vitest";

import { api } from "./api";

function mockOnce(json: unknown = {}): ReturnType<typeof vi.fn> {
  const spy = vi.fn().mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => json,
    text: async () => "",
  } as Response);
  globalThis.fetch = spy as unknown as typeof fetch;
  return spy;
}

describe("global api (self-server) — URLs stay un-proxied", () => {
  afterEach(() => vi.restoreAllMocks());

  test("backlog hits /api/projects/<slug>/backlog directly", async () => {
    const spy = mockOnce([]);
    await api.backlog("alpha");
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/backlog",
      expect.any(Object),
    );
  });

  test("sessions hits /api/projects/<slug>/sessions directly", async () => {
    const spy = mockOnce([]);
    await api.sessions("alpha");
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/sessions",
      expect.any(Object),
    );
  });

  test("peerSend posts the from_sid envelope to the local route", async () => {
    const spy = mockOnce({ ok: true, delivered_to: [] });
    await api.peerSend("alpha", "S-from", "S-to", "msg");
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/peer/send",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ from_sid: "S-from", to: "S-to", text: "msg" }),
      }),
    );
  });
});
