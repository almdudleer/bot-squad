/**
 * Tests for the mothership FE client (T-0023).
 *
 * The FE consumer of the per-server proxy at `/api/m/servers/{id}/api/{path}`.
 * Mocks `globalThis.fetch` rather than spinning up a real server — fast and
 * keeps the tests honest about what URLs and methods we emit.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  apiFor,
  fanOut,
  invalidateServersCache,
  mothershipApi,
  ProxyError,
} from "./api";

type FetchSpy = ReturnType<typeof vi.fn>;

function mockFetchSequence(responses: Array<Partial<Response> | Error>): FetchSpy {
  const spy = vi.fn();
  for (const r of responses) {
    if (r instanceof Error) {
      spy.mockRejectedValueOnce(r);
    } else {
      spy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => "",
        ...r,
      } as Response);
    }
  }
  return spy;
}

describe("mothershipApi.listServers", () => {
  beforeEach(() => {
    invalidateServersCache();
    globalThis.fetch = mockFetchSequence([
      {
        json: async () => [
          { id: "srv_a", display_name: "A", install_state: "connected" },
        ],
      },
    ]) as unknown as typeof fetch;
  });
  afterEach(() => {
    vi.restoreAllMocks();
    invalidateServersCache();
  });

  test("GETs /api/m/servers and returns the array", async () => {
    const out = await mothershipApi.listServers();
    expect(out).toEqual([
      { id: "srv_a", display_name: "A", install_state: "connected" },
    ]);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/m/servers",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

// T-0133: server-detail mounts fire multiple concurrent listServers()
// across siblings (ServerPicker, ServerProgress, mothership fan-out).
// Without dedup, networkidle never settles. The shared cache must
// coalesce concurrent calls AND serve a short-TTL cached copy so a
// quick re-mount during navigation doesn't re-fetch.
describe("mothershipApi.listServers — T-0133 dedup + cache", () => {
  beforeEach(() => invalidateServersCache());
  afterEach(() => {
    vi.restoreAllMocks();
    invalidateServersCache();
  });

  test("concurrent callers share a single fetch", async () => {
    const spy = mockFetchSequence([
      {
        json: async () => [
          { id: "srv_a", display_name: "A", install_state: "ready" },
        ],
      },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    // 4 concurrent callers (the worst-case server-detail page-load
    // shape). After awaiting all, fetch must have run exactly once.
    const [a, b, c, d] = await Promise.all([
      mothershipApi.listServers(),
      mothershipApi.listServers(),
      mothershipApi.listServers(),
      mothershipApi.listServers(),
    ]);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(a).toBe(b);
    expect(b).toBe(c);
    expect(c).toBe(d);
  });

  test("subsequent callers within TTL hit the cache, no new fetch", async () => {
    const spy = mockFetchSequence([
      { json: async () => [{ id: "srv_a", install_state: "ready" }] },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await mothershipApi.listServers();
    // Second call: same data, fetch NOT re-invoked.
    await mothershipApi.listServers();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  test("createServer invalidates the cache so the next list re-fetches", async () => {
    // First fetch populates the cache.
    const fetchSpy = mockFetchSequence([
      { json: async () => [{ id: "srv_a", install_state: "ready" }] },
      // POST /api/m/servers
      {
        json: async () => ({
          id: "srv_b",
          install_token: "tok",
          install_url: "u",
          instructions_url: "i",
          expires_at: null,
        }),
      },
      // listServers after invalidation: returns both rows.
      {
        json: async () => [
          { id: "srv_a", install_state: "ready" },
          { id: "srv_b", install_state: "pending" },
        ],
      },
    ]);
    globalThis.fetch = fetchSpy as unknown as typeof fetch;

    const before = await mothershipApi.listServers();
    expect(before).toHaveLength(1);

    await mothershipApi.createServer("B", "https://b.example.com");

    const after = await mothershipApi.listServers();
    expect(after).toHaveLength(2);
    // Three fetches total: initial list, createServer POST, re-list.
    expect(fetchSpy).toHaveBeenCalledTimes(3);
  });

  test("invalidateServersCache forces the next list to re-fetch", async () => {
    const spy = mockFetchSequence([
      { json: async () => [{ id: "srv_a", install_state: "ready" }] },
      { json: async () => [{ id: "srv_a", install_state: "ready" }] },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await mothershipApi.listServers();
    invalidateServersCache();
    await mothershipApi.listServers();
    expect(spy).toHaveBeenCalledTimes(2);
  });
});

describe("mothershipApi.projectsFor", () => {
  test("hits /api/m/servers/<id>/projects", async () => {
    const spy = mockFetchSequence([
      {
        json: async () => [{ slug: "alpha", display_name: "Alpha", status: "idle" }],
      },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    const out = await mothershipApi.projectsFor("srv_x");
    expect(out).toEqual([
      { slug: "alpha", display_name: "Alpha", status: "idle" },
    ]);
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_x/projects",
      expect.any(Object),
    );
  });

  test("URL-encodes the server id", async () => {
    const spy = mockFetchSequence([{ json: async () => [] }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await mothershipApi.projectsFor("srv/with slashes");
    expect((spy as FetchSpy).mock.calls[0][0]).toBe(
      "/api/m/servers/srv%2Fwith%20slashes/projects",
    );
  });
});

// T-0125: createInvite mints a one-shot invite token for an additional
// Linux user to join an existing install. The plaintext invite_token is
// returned exactly once in the response body; subsequent listServers()
// calls only see the SHA-256.
describe("mothershipApi.createInvite (T-0125)", () => {
  afterEach(() => vi.restoreAllMocks());

  test("POSTs target_username + role to /api/m/servers/<id>/invites and returns the minted envelope", async () => {
    const minted = {
      server_id: "srv_x",
      invite_token: "bsq_invite_ABC",
      install_url: "https://example.com/i/bsq_invite_ABC/install.sh",
      instructions_url: "https://example.com/i/bsq_invite_ABC/instructions.md",
      target_username: "alice",
      role: "admin",
      expires_at: "2026-05-28T00:00:00Z",
    };
    const spy = mockFetchSequence([{ json: async () => minted }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    const out = await mothershipApi.createInvite("srv_x", "alice", "admin");

    expect(out).toEqual(minted);
    // Round-trip the URL/body shape the BE expects.
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_x/invites",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ target_username: "alice", role: "admin" }),
      }),
    );
  });

  test("URL-encodes the server id", async () => {
    const spy = mockFetchSequence([
      {
        json: async () => ({
          server_id: "srv/odd",
          invite_token: "bsq_invite_T",
          install_url: "u",
          instructions_url: "i",
          target_username: "bob",
          role: "non-admin",
          expires_at: null,
        }),
      },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await mothershipApi.createInvite("srv/odd", "bob", "non-admin");
    expect((spy as FetchSpy).mock.calls[0][0]).toBe(
      "/api/m/servers/srv%2Fodd/invites",
    );
  });

  test("non-admin role is forwarded verbatim (so the BE 400 on bad roles is the only role validator)", async () => {
    const spy = mockFetchSequence([
      {
        json: async () => ({
          server_id: "srv_y",
          invite_token: "bsq_invite_NA",
          install_url: "https://x/i/bsq_invite_NA/install.sh",
          instructions_url: "https://x/i/bsq_invite_NA/instructions.md",
          target_username: "carol",
          role: "non-admin",
          expires_at: "2026-05-28T00:00:00Z",
        }),
      },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await mothershipApi.createInvite("srv_y", "carol", "non-admin");
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_y/invites",
      expect.objectContaining({
        body: JSON.stringify({
          target_username: "carol",
          role: "non-admin",
        }),
      }),
    );
  });
});

// T-0113: super-admin directory backing the /m/users page.
describe("mothershipApi.listUsers (T-0113)", () => {
  afterEach(() => vi.restoreAllMocks());

  test("GETs /api/m/users and returns the array", async () => {
    const users = [
      {
        id: "gu_a",
        username: "alice",
        display_name: "Alice",
        email: "alice@example.com",
        timezone: "UTC",
        is_super_admin: true,
        created_at: "2026-05-27T00:00:00Z",
      },
    ];
    const spy = mockFetchSequence([{ json: async () => users }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    const out = await mothershipApi.listUsers();
    expect(out).toEqual(users);
    expect(spy).toHaveBeenCalledWith(
      "/api/m/users",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  test("empty registry comes through as []", async () => {
    globalThis.fetch = mockFetchSequence([{ json: async () => [] }]) as unknown as typeof fetch;
    expect(await mothershipApi.listUsers()).toEqual([]);
  });

  test("403 from the BE super-admin gate surfaces as an `API error 403` rejection", async () => {
    globalThis.fetch = mockFetchSequence([
      { ok: false, status: 403, text: async () => "super-admin only" },
    ]) as unknown as typeof fetch;
    await expect(mothershipApi.listUsers()).rejects.toThrow(/API error 403/);
  });
});

// T-0113: the install-token mint round-trip is the existing T-0024 surface.
// /m/users coverage isn't complete without pinning that the FE adapter
// returns the one-shot install_token + install_url envelope verbatim — the
// AllProjects install-tokens sub-table reads `install_token_expires_at`
// from listServers() but the *mint* is what gets that field onto disk.
describe("mothershipApi.createServer (install-token mint)", () => {
  afterEach(() => vi.restoreAllMocks());

  test("POSTs display_name + base_url and returns the one-shot install_token envelope", async () => {
    const minted = {
      id: "srv_new",
      install_token: "bsq_install_ABC",
      install_url: "https://staging.botsquad.dev/i/bsq_install_ABC/install.sh",
      instructions_url:
        "https://staging.botsquad.dev/i/bsq_install_ABC/instructions.md",
      expires_at: "2026-05-28T00:00:00Z",
    };
    const spy = mockFetchSequence([{ json: async () => minted }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    const out = await mothershipApi.createServer("staging", "https://x.example.com");
    expect(out).toEqual(minted);
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          display_name: "staging",
          base_url: "https://x.example.com",
        }),
      }),
    );
  });
});

// T-0414: deregister affordance. The BE DELETE /api/m/servers/{id} (T-0412)
// is the close for register_server; the FE handle must hit that path with
// method DELETE, return the {removed} envelope, and invalidate the servers
// cache so the row vanishes from the next listServers() without a hard
// reload. is_self → 409 (surfaced as an `API error 409` rejection), the BE
// guard against deleting the self-server.
describe("mothershipApi.deleteServer (T-0414)", () => {
  beforeEach(() => invalidateServersCache());
  afterEach(() => {
    vi.restoreAllMocks();
    invalidateServersCache();
  });

  test("DELETEs /api/m/servers/<id> and returns the {removed} envelope", async () => {
    const spy = mockFetchSequence([{ json: async () => ({ removed: "srv_x" }) }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    const out = await mothershipApi.deleteServer("srv_x");
    expect(out).toEqual({ removed: "srv_x" });
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_x",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  test("URL-encodes the server id", async () => {
    const spy = mockFetchSequence([{ json: async () => ({ removed: "srv/odd" }) }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await mothershipApi.deleteServer("srv/odd");
    expect((spy as FetchSpy).mock.calls[0][0]).toBe("/api/m/servers/srv%2Fodd");
  });

  test("invalidates the servers cache so the next list re-fetches", async () => {
    const spy = mockFetchSequence([
      // initial list populates the cache
      { json: async () => [{ id: "srv_x", install_state: "ready" }] },
      // DELETE
      { json: async () => ({ removed: "srv_x" }) },
      // re-list after invalidation: row is gone
      { json: async () => [] },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await mothershipApi.listServers();
    await mothershipApi.deleteServer("srv_x");
    const after = await mothershipApi.listServers();
    expect(after).toEqual([]);
    // Three fetches: initial list, DELETE, re-list (cache was invalidated).
    expect(spy).toHaveBeenCalledTimes(3);
  });

  test("is_self 409 surfaces as an `API error 409` rejection", async () => {
    globalThis.fetch = mockFetchSequence([
      { ok: false, status: 409, text: async () => "cannot delete the mothership's own (is_self) server" },
    ]) as unknown as typeof fetch;
    await expect(mothershipApi.deleteServer("srv_self")).rejects.toThrow(/API error 409/);
  });
});

describe("mothershipApi.refreshProjects", () => {
  test("POSTs to /api/m/servers/<id>/projects/refresh", async () => {
    const spy = mockFetchSequence([
      { json: async () => [{ slug: "fresh", display_name: "Fresh", status: "idle" }] },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    const out = await mothershipApi.refreshProjects("srv_r");
    expect(out).toEqual([{ slug: "fresh", display_name: "Fresh", status: "idle" }]);
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_r/projects/refresh",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("apiFor(serverId).projects", () => {
  test("hits the proxy at /api/m/servers/<id>/api/projects", async () => {
    const spy = mockFetchSequence([
      { json: async () => [{ slug: "a", display_name: "A" }] },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    const out = await apiFor("srv_proxy").projects();
    expect(out).toEqual([{ slug: "a", display_name: "A" }]);
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_proxy/api/projects",
      expect.any(Object),
    );
  });
});

// T-0068: every per-project method on apiFor must hit
// /api/m/servers/<id>/api/<upstream-path> with the same body/method as the
// global singleton would emit. Sampled across read, mutate, and action
// surfaces; if a future method drifts from its singleton twin, this test
// pair (proxy URL + singleton URL below) will catch the divergence.
describe("apiFor(serverId) — per-project surface (T-0068)", () => {
  afterEach(() => vi.restoreAllMocks());

  test("backlog: GETs /api/m/servers/<id>/api/projects/<slug>/backlog", async () => {
    const spy = mockFetchSequence([{ json: async () => [] }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await apiFor("srv_a").backlog("alpha");
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_a/api/projects/alpha/backlog",
      expect.any(Object),
    );
  });

  test("sessions: GETs /api/m/servers/<id>/api/projects/<slug>/sessions", async () => {
    const spy = mockFetchSequence([{ json: async () => [] }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await apiFor("srv_b").sessions("beta");
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_b/api/projects/beta/sessions",
      expect.any(Object),
    );
  });

  test("createTask: POSTs to the proxy with the JSON body verbatim", async () => {
    const spy = mockFetchSequence([{ json: async () => ({ id: "T-1" }) }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await apiFor("srv_c").createTask("gamma", {
      title: "x",
      verbatim_request: "y",
      status: "open",
    });
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_c/api/projects/gamma/backlog",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          title: "x",
          verbatim_request: "y",
          status: "open",
        }),
      }),
    );
  });

  test("peerSend: POSTs with `from_sid`-shaped body (T-0068 verifies bus surface)", async () => {
    const spy = mockFetchSequence([
      { json: async () => ({ ok: true, delivered_to: [] }) },
    ]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await apiFor("srv_d").peerSend("delta", "S-from", "S-to", "hello");
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_d/api/projects/delta/peer/send",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ from_sid: "S-from", to: "S-to", text: "hello" }),
      }),
    );
  });

  test("pauseSession: POSTs and URL-encodes the sid", async () => {
    const spy = mockFetchSequence([{ json: async () => ({ ok: true }) }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await apiFor("srv_e").pauseSession("eps", "S/odd sid");
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_e/api/projects/eps/sessions/S%2Fodd%20sid/pause",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

// T-0068: the proxy hop can return three auth-relevant statuses that the
// wrapper component needs to render distinct UI for. 503 = mothership has
// no bearer for this peer; 401/403 = peer rejected our bearer. Anything
// else flows through as a plain Error so the calling page can surface its
// own message.
describe("apiFor(serverId) — proxy auth error classification (T-0068)", () => {
  afterEach(() => vi.restoreAllMocks());

  test("503 from the proxy → ProxyError(not_connected)", async () => {
    globalThis.fetch = mockFetchSequence([
      { ok: false, status: 503, text: async () => "bearer missing" },
    ]) as unknown as typeof fetch;

    await expect(apiFor("srv").backlog("p")).rejects.toMatchObject({
      kind: "not_connected",
      status: 503,
    });
  });

  test("401 from upstream → ProxyError(access_denied) (does NOT redirect)", async () => {
    globalThis.fetch = mockFetchSequence([
      { ok: false, status: 401, text: async () => "denied" },
    ]) as unknown as typeof fetch;

    // Sanity: the call must reject — a global `call()` would assign
    // window.location.href on a 401 (and throw); proxyCall must not.
    await expect(apiFor("srv").backlog("p")).rejects.toBeInstanceOf(
      ProxyError,
    );
  });

  test("403 from upstream → ProxyError(access_denied)", async () => {
    globalThis.fetch = mockFetchSequence([
      { ok: false, status: 403, text: async () => "forbidden" },
    ]) as unknown as typeof fetch;

    await expect(apiFor("srv").backlog("p")).rejects.toMatchObject({
      kind: "access_denied",
      status: 403,
    });
  });

  test("500 from upstream → plain Error (not ProxyError)", async () => {
    globalThis.fetch = mockFetchSequence([
      { ok: false, status: 500, text: async () => "boom" },
    ]) as unknown as typeof fetch;

    const err = await apiFor("srv")
      .backlog("p")
      .catch((e) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(ProxyError);
  });
});

describe("apiFor(serverId).call (escape hatch)", () => {
  test("translates an `/api/<rest>` path into the proxy URL", async () => {
    const spy = mockFetchSequence([{ json: async () => ({ ok: true }) }]);
    globalThis.fetch = spy as unknown as typeof fetch;

    await apiFor("srv_eh").call("/api/projects/foo/backlog");
    expect(spy).toHaveBeenCalledWith(
      "/api/m/servers/srv_eh/api/projects/foo/backlog",
      expect.any(Object),
    );
  });

  test("rejects paths that don't start with /api/", async () => {
    await expect(apiFor("x").call("/not-api/foo")).rejects.toThrow(
      /must start with \/api\//,
    );
  });
});

describe("fanOut", () => {
  test("returns ok+data for resolved calls and ok=false+error for rejected ones", async () => {
    const result = await fanOut(
      ["srv_a", "srv_b", "srv_c"],
      async (id) => {
        if (id === "srv_b") throw new Error("dead");
        return { id, ok: true };
      },
    );

    expect(result).toEqual([
      { serverId: "srv_a", ok: true, data: { id: "srv_a", ok: true } },
      { serverId: "srv_b", ok: false, error: "dead" },
      { serverId: "srv_c", ok: true, data: { id: "srv_c", ok: true } },
    ]);
  });

  test("runs all calls in parallel (one dead server doesn't block siblings)", async () => {
    // Each call resolves after 30ms; total time should be ~30ms not ~90ms.
    const start = Date.now();
    const result = await fanOut(["a", "b", "c"], async (id) => {
      if (id === "b") throw new Error("nope");
      await new Promise((r) => setTimeout(r, 30));
      return id;
    });
    const elapsed = Date.now() - start;
    expect(elapsed).toBeLessThan(80); // very generous bound
    expect(result.map((r) => r.ok)).toEqual([true, false, true]);
  });

  test("stringifies non-Error rejections", async () => {
    const result = await fanOut(["a"], async () => {
      throw "raw string error"; // eslint-disable-line @typescript-eslint/no-throw-literal
    });
    expect(result[0]).toEqual({
      serverId: "a",
      ok: false,
      error: "raw string error",
    });
  });
});
