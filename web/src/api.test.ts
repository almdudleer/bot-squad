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

import { api, isNotFoundError } from "./api";

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

  // T-0138 / T-0139: pages branch on this decoder to render a "not-found"
  // panel vs a transient-network retry. Lock the contract here so a future
  // tweak to `call()`'s thrown-message shape can't silently break the
  // not-found UX on /p/:slug and /p/:slug/t/:id.
  test("isNotFoundError matches the exact 404 shape from call()", () => {
    expect(isNotFoundError(new Error("API error 404: unknown project: zzz"))).toBe(true);
    expect(isNotFoundError(new Error("API error 404"))).toBe(true);
    expect(isNotFoundError(new Error("API error 500: boom"))).toBe(false);
    expect(isNotFoundError(new Error("not authenticated"))).toBe(false);
    expect(isNotFoundError(new TypeError("Failed to fetch"))).toBe(false);
    expect(isNotFoundError(null)).toBe(false);
    expect(isNotFoundError(undefined)).toBe(false);
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

  // T-0235: nested-docs wrappers over the T-0234 backend endpoints.
  test("docChildren hits GET /docs/<id>/children", async () => {
    const spy = mockOnce([]);
    await api.docChildren("alpha", "D-0001");
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/docs/D-0001/children",
      expect.any(Object),
    );
  });

  test("setDocParent PUTs the parent_doc_id to /docs/<id>/parent", async () => {
    const spy = mockOnce({ ok: true, id: "D-0002", parent_doc_id: "D-0001" });
    await api.setDocParent("alpha", "D-0002", "D-0001");
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/docs/D-0002/parent",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ parent_doc_id: "D-0001" }),
      }),
    );
  });

  test("setDocParent with null detaches (disown)", async () => {
    const spy = mockOnce({ ok: true, id: "D-0002", parent_doc_id: null });
    await api.setDocParent("alpha", "D-0002", null);
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/docs/D-0002/parent",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ parent_doc_id: null }),
      }),
    );
  });

  test("createDoc forwards an optional parent_doc_id", async () => {
    const spy = mockOnce({ ok: true, id: "D-0003", category: "product" });
    await api.createDoc("alpha", "product", "Child doc", "D-0001");
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/docs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ category: "product", title: "Child doc", parent_doc_id: "D-0001" }),
      }),
    );
  });
});
