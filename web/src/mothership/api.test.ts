/**
 * Tests for the mothership FE client (T-0023).
 *
 * The FE consumer of the per-server proxy at `/api/m/servers/{id}/api/{path}`.
 * Mocks `globalThis.fetch` rather than spinning up a real server — fast and
 * keeps the tests honest about what URLs and methods we emit.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { apiFor, fanOut, mothershipApi } from "./api";

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
    globalThis.fetch = mockFetchSequence([
      {
        json: async () => [
          { id: "srv_a", display_name: "A", install_state: "connected" },
        ],
      },
    ]) as unknown as typeof fetch;
  });
  afterEach(() => vi.restoreAllMocks());

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
