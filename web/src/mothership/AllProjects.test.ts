/**
 * Tests for the pure helpers behind the cross-server all-projects view (T-0025).
 *
 * The component itself is React, but the section-build + status-mapping logic
 * is pure and lives in the same module. The view's failure-isolation
 * guarantee comes from `fanOut` (covered in api.test.ts); these tests pin the
 * registry-state → render-section translation and the status-enum mapping.
 */
import { describe, expect, test } from "vitest";

import {
  buildSections,
  projectCardLinkFor,
  serverHeaderLabel,
  statusBadgeClass,
  type ServerSection,
} from "./AllProjects";
import type { AttachedServer, FanOutResult, ServerProject } from "./api";

function srv(
  id: string,
  install_state: AttachedServer["install_state"] = "ready",
  extra: Partial<AttachedServer> = {},
): AttachedServer {
  return {
    id,
    display_name: id,
    base_url: `https://${id}.example.com`,
    owner_user: "u",
    created_at: "2026-05-14T00:00:00Z",
    install_state,
    install_token_expires_at: null,
    last_seen_at: null,
    projects_cache: [],
    ...extra,
  };
}

function ok(serverId: string, data: ServerProject[]): FanOutResult<ServerProject[]> {
  return { serverId, ok: true, data };
}

function fail(serverId: string, error: string): FanOutResult<ServerProject[]> {
  return { serverId, ok: false, error };
}

describe("buildSections", () => {
  test("ready servers get their fan-out result", () => {
    const sections = buildSections(
      [srv("a"), srv("b")],
      [
        ok("a", [{ slug: "alpha", display_name: "Alpha", status: "working" }]),
        ok("b", []),
      ],
    );
    expect(sections.map((s) => [s.server.id, s.kind, s.result?.ok])).toEqual([
      ["a", "live", true],
      ["b", "live", true],
    ]);
  });

  test("non-ready servers render as 'installing' regardless of fan-out", () => {
    const sections = buildSections(
      [srv("p", "pending"), srv("c", "connected"), srv("f", "failed")],
      [], // not in the readyIds list, so no results expected
    );
    expect(sections.map((s) => [s.server.id, s.kind])).toEqual([
      ["p", "installing"],
      ["c", "installing"],
      ["f", "installing"],
    ]);
    expect(sections.every((s) => s.result === null)).toBe(true);
  });

  test("a missing fan-out result for a 'ready' server surfaces as unreachable", () => {
    // Defensive — should never happen in practice, but the component renders
    // a synthetic ok=false envelope if the result map is incomplete.
    const [section] = buildSections([srv("z")], []);
    expect(section.kind).toBe("live");
    expect(section.result?.ok).toBe(false);
  });

  test("one server's failure does not poison siblings (regression for T-0023 isolation)", () => {
    const sections = buildSections(
      [srv("a"), srv("b"), srv("c")],
      [
        ok("a", [{ slug: "alpha", display_name: "Alpha", status: "idle" }]),
        fail("b", "504 gateway"),
        ok("c", [{ slug: "gamma", display_name: "Gamma", status: "needs-input" }]),
      ],
    );
    const okFlags = sections.map((s) => s.result?.ok);
    expect(okFlags).toEqual([true, false, true]);
    // Surviving sections still expose their data
    const aData = sections[0].result;
    const cData = sections[2].result;
    expect(aData?.ok && aData.data[0].slug).toBe("alpha");
    expect(cData?.ok && cData.data[0].slug).toBe("gamma");
  });

  test("preserves server order from the registry", () => {
    const ids = ["c", "a", "b"];
    const sections: ServerSection[] = buildSections(
      ids.map((id) => srv(id)),
      ids.map((id) => ok(id, [])),
    );
    expect(sections.map((s) => s.server.id)).toEqual(ids);
  });
});

describe("statusBadgeClass", () => {
  test("maps the canonical enum to distinct mc-badge variants", () => {
    expect(statusBadgeClass("working")).toBe("mc-badge mc-badge-active");
    expect(statusBadgeClass("needs-input")).toBe("mc-badge mc-badge-warn");
    expect(statusBadgeClass("idle")).toBe("mc-badge mc-badge-dim");
  });

  test("unknown status falls back to dim (forward-compatible with future T-0016 amendments)", () => {
    expect(statusBadgeClass("future-state")).toBe("mc-badge mc-badge-dim");
  });
});

describe("serverHeaderLabel (T-0055)", () => {
  test("peer server has no 'this server' suffix", () => {
    const label = serverHeaderLabel(srv("peer"));
    expect(label.suffix).toBeNull();
    expect(label.name).toBe("peer");
    expect(label.url).toBe("https://peer.example.com");
  });

  test("self entry gets the 'this server' suffix marker", () => {
    const label = serverHeaderLabel(srv("home", "ready", { is_self: true }));
    expect(label.suffix).toBe("this server");
    expect(label.name).toBe("home");
  });

  test("is_self=false is treated identically to omitted is_self (boundary check)", () => {
    expect(serverHeaderLabel(srv("a", "ready", { is_self: false })).suffix).toBeNull();
  });
});

describe("buildSections honours is_self transparently", () => {
  test("the self entry sits in the same ordering as peers and renders 'live' when ready", () => {
    const sections = buildSections(
      [
        srv("self", "ready", { is_self: true }),
        srv("peer"),
      ],
      [
        { serverId: "self", ok: true, data: [] },
        { serverId: "peer", ok: true, data: [] },
      ],
    );
    expect(sections[0].server.is_self).toBe(true);
    expect(sections[0].kind).toBe("live");
    expect(sections[1].server.is_self).toBeFalsy();
  });
});

// T-0068: peer-server cards used to be plain divs ("non-clickable"). The
// project-card link helper now returns a real route for them, while the
// self entry keeps the legacy short URL — `projectCardLinkFor` is the
// only place that decision lives.
describe("projectCardLinkFor (T-0068)", () => {
  test("self entry keeps the short /p/<slug> URL", () => {
    const link = projectCardLinkFor({ id: "srv_self", is_self: true }, "alpha");
    expect(link).toBe("/p/alpha");
  });

  test("peer entry routes through /m/servers/<id>/p/<slug>", () => {
    const link = projectCardLinkFor({ id: "srv_peer", is_self: false }, "alpha");
    expect(link).toBe("/m/servers/srv_peer/p/alpha");
  });

  test("URL-encodes server id and slug so weird IDs don't break routing", () => {
    const link = projectCardLinkFor(
      { id: "srv/with spaces", is_self: false },
      "name with spaces",
    );
    expect(link).toBe("/m/servers/srv%2Fwith%20spaces/p/name%20with%20spaces");
  });
});
