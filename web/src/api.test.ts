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

import { api, isNotFoundError, isPublicRoute, normalizeSessionsPayload, parseNearDuplicate, shouldRedirectOn401 } from "./api";

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

// ---------------------------------------------------------------------------
// T-0601 (F5): sessions payload normalization — the server now returns
// {sessions, errors}; older servers (behind the mothership proxy) still send
// a bare array. Both client methods must accept both shapes.
// ---------------------------------------------------------------------------
describe("T-0601 sessions fan-out errors", () => {
  afterEach(() => vi.restoreAllMocks());

  const row = { sid: "S-u-w-p1", status: "active" };

  test("normalizeSessionsPayload: envelope → rows + errors", () => {
    expect(
      normalizeSessionsPayload({
        sessions: [row],
        errors: [{ user: "timpo", detail: "connect failed" }],
      }),
    ).toEqual({ rows: [row], errors: [{ user: "timpo", detail: "connect failed" }] });
  });

  test("normalizeSessionsPayload: legacy bare array → rows, no errors", () => {
    expect(normalizeSessionsPayload([row])).toEqual({ rows: [row], errors: [] });
  });

  test("normalizeSessionsPayload: garbage → empty payload", () => {
    expect(normalizeSessionsPayload(null)).toEqual({ rows: [], errors: [] });
    expect(normalizeSessionsPayload("nope")).toEqual({ rows: [], errors: [] });
    expect(normalizeSessionsPayload({})).toEqual({ rows: [], errors: [] });
  });

  test("api.sessions keeps its SessionRow[] contract over the new envelope", async () => {
    mockOnce({ sessions: [row], errors: [{ user: "aqice", detail: "boom" }] });
    await expect(api.sessions("alpha")).resolves.toEqual([row]);
  });

  test("api.sessionsDetail exposes rows AND errors", async () => {
    const spy = mockOnce({ sessions: [row], errors: [{ user: "aqice", detail: "boom" }] });
    await expect(api.sessionsDetail("alpha")).resolves.toEqual({
      rows: [row],
      errors: [{ user: "aqice", detail: "boom" }],
    });
    expect(spy).toHaveBeenCalledWith("/api/projects/alpha/sessions", expect.any(Object));
  });
});

// ---------------------------------------------------------------------------
// T-0601 (F4): the login call is exempt from the global 401-redirect — a
// wrong password must reject (so the Login form shows the error) instead of
// silently reloading /login. These run in node (no `window`): if call() ever
// tried the redirect on the login path it would crash on the missing global
// rather than produce the API-error rejection asserted here.
// ---------------------------------------------------------------------------
describe("T-0601 login 401-exempt", () => {
  afterEach(() => vi.restoreAllMocks());

  test("shouldRedirectOn401 exempts exactly the login path", () => {
    expect(shouldRedirectOn401("/api/auth/login")).toBe(false);
    expect(shouldRedirectOn401("/api/auth/me")).toBe(true);
    expect(shouldRedirectOn401("/api/projects/alpha/sessions")).toBe(true);
  });

  test("api.login rejects with the 401 body instead of redirecting", async () => {
    const spy = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: async () => ({}),
      text: async () => '{"detail":"bad credentials"}',
    } as Response);
    globalThis.fetch = spy as unknown as typeof fetch;
    await expect(api.login("u", "wrong")).rejects.toThrow(
      /API error 401.*bad credentials/,
    );
  });
});

// ---------------------------------------------------------------------------
// T-0609: the COMPOSED redirect path — shouldRedirectOn401 alone can't catch a
// regression where call() stops consulting it (or stops redirecting at all).
// Stub a window so the node run can observe the redirect instead of crashing.
// ---------------------------------------------------------------------------
describe("T-0609 composed call() 401-redirect", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("a 401 on /api/auth/me redirects to /login and rejects", async () => {
    const loc = { href: "" };
    vi.stubGlobal("window", { location: loc });
    const spy = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: async () => ({}),
      text: async () => '{"detail":"not authenticated"}',
    } as Response);
    globalThis.fetch = spy as unknown as typeof fetch;
    await expect(api.me()).rejects.toThrow("not authenticated");
    expect(loc.href).toBe("/login");
    expect(spy).toHaveBeenCalledWith("/api/auth/me", expect.any(Object));
  });
});

// ---------------------------------------------------------------------------
// T-0714: /help is documented (in its own copy) as reachable without login, but
// the Shell's background fetches 401 there and used to bounce the visitor to
// /login. The exemption is keyed on the PAGE route — a 401 on a private page
// must still redirect.
// ---------------------------------------------------------------------------
describe("T-0714 public-route 401 exemption", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("isPublicRoute matches /help (and tolerates a trailing slash) only", () => {
    expect(isPublicRoute("/help")).toBe(true);
    expect(isPublicRoute("/help/")).toBe(true);
    expect(isPublicRoute("/login")).toBe(false);
    expect(isPublicRoute("/p/bot-squad")).toBe(false);
    expect(isPublicRoute("/")).toBe(false);
    expect(isPublicRoute(undefined)).toBe(false);
  });

  test("shouldRedirectOn401 suppresses the bounce on /help, not elsewhere", () => {
    expect(shouldRedirectOn401("/api/auth/me", "/help")).toBe(false);
    expect(shouldRedirectOn401("/api/projects", "/help")).toBe(false);
    expect(shouldRedirectOn401("/api/auth/me", "/p/bot-squad")).toBe(true);
    expect(shouldRedirectOn401("/api/auth/me", "/login")).toBe(true);
  });

  test("call() on /help rejects without navigating away", async () => {
    const loc = { href: "https://host/help", pathname: "/help" };
    vi.stubGlobal("window", { location: loc });
    const spy = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({}),
      text: async () => '{"detail":"not authenticated"}',
    } as Response);
    globalThis.fetch = spy as unknown as typeof fetch;
    await expect(api.me()).rejects.toThrow(/API error 401/);
    expect(loc.href).toBe("https://host/help");
  });

  test("call() on a private page still redirects to /login", async () => {
    const loc = { href: "https://host/p/bot-squad", pathname: "/p/bot-squad" };
    vi.stubGlobal("window", { location: loc });
    const spy = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({}),
      text: async () => '{"detail":"not authenticated"}',
    } as Response);
    globalThis.fetch = spy as unknown as typeof fetch;
    await expect(api.me()).rejects.toThrow("not authenticated");
    expect(loc.href).toBe("/login");
  });
});

// ---------------------------------------------------------------------------
// T-0608: near-duplicate 409 decoder — the create modal branches on this to
// show candidates + "create anyway" instead of a generic error line.
// ---------------------------------------------------------------------------
describe("T-0608 parseNearDuplicate", () => {
  const body409 = JSON.stringify({
    detail: {
      error: "near_duplicate",
      message: "looks like a near-duplicate — retry with force:true to create anyway",
      candidates: [{ id: "T-0042", title: "existing twin" }],
    },
  });

  test("decodes the structured 409 into message + candidates", () => {
    expect(parseNearDuplicate(new Error(`API error 409: ${body409}`))).toEqual({
      message: "looks like a near-duplicate — retry with force:true to create anyway",
      candidates: [{ id: "T-0042", title: "existing twin" }],
    });
  });

  test("null for other statuses, other 409s, and non-JSON bodies", () => {
    expect(parseNearDuplicate(new Error("API error 400: bad"))).toBeNull();
    expect(
      parseNearDuplicate(new Error('API error 409: {"detail":"mothership locked"}')),
    ).toBeNull();
    expect(
      parseNearDuplicate(new Error('API error 409: {"detail":{"error":"other"}}')),
    ).toBeNull();
    expect(parseNearDuplicate(new Error("API error 409: not-json"))).toBeNull();
    expect(parseNearDuplicate("random string")).toBeNull();
  });

  test("drops malformed candidate entries, keeps well-formed ones", () => {
    const mixed = JSON.stringify({
      detail: {
        error: "near_duplicate",
        message: "m",
        candidates: [{ id: "T-1", title: "ok" }, { id: 5 }, null, "x"],
      },
    });
    expect(parseNearDuplicate(new Error(`API error 409: ${mixed}`))).toEqual({
      message: "m",
      candidates: [{ id: "T-1", title: "ok" }],
    });
  });

  test("createTask forwards force:true in the POST body", async () => {
    const spy = mockOnce({ id: "T-9999" });
    await api.createTask("alpha", { title: "t", verbatim_request: "v", force: true });
    expect(spy).toHaveBeenCalledWith(
      "/api/projects/alpha/backlog",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ title: "t", verbatim_request: "v", force: true }),
      }),
    );
  });
});
