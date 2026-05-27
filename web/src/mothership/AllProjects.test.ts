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
  installTokenBadgeClass,
  installTokenLabel,
  installTokenStatus,
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

// T-0113: install-tokens sub-table. The helper distills the registry row
// (install_state + install_token_expires_at) into a discrete enum the
// renderer keys off — keeps the time-dependent expiry check off the React
// render path and out of jsdom.
describe("installTokenStatus (T-0113)", () => {
  const NOW = new Date("2026-05-27T12:00:00Z");

  test("pending + future expiry → active", () => {
    const s = installTokenStatus(
      {
        install_state: "pending",
        install_token_expires_at: "2026-05-28T12:00:00Z",
        is_self: false,
      },
      NOW,
    );
    expect(s.status).toBe("active");
    expect(s.expiresAt).toBe("2026-05-28T12:00:00Z");
  });

  test("pending + past expiry → expired (the token would 410 at /connect)", () => {
    const s = installTokenStatus(
      {
        install_state: "pending",
        install_token_expires_at: "2026-05-26T12:00:00Z",
        is_self: false,
      },
      NOW,
    );
    expect(s.status).toBe("expired");
  });

  test("connected → consumed (token burned at /connect)", () => {
    expect(
      installTokenStatus({
        install_state: "connected",
        install_token_expires_at: null,
        is_self: false,
      }).status,
    ).toBe("consumed");
  });

  test("ready → consumed", () => {
    expect(
      installTokenStatus({
        install_state: "ready",
        install_token_expires_at: null,
        is_self: false,
      }).status,
    ).toBe("consumed");
  });

  test("failed → consumed (whatever was minted is gone, indistinguishable from burned)", () => {
    expect(
      installTokenStatus({
        install_state: "failed",
        install_token_expires_at: null,
        is_self: false,
      }).status,
    ).toBe("consumed");
  });

  test("is_self → n/a (the mothership's own row skips the token lifecycle)", () => {
    const s = installTokenStatus(
      {
        install_state: "ready",
        install_token_expires_at: null,
        is_self: true,
      },
      NOW,
    );
    expect(s.status).toBe("n/a");
  });

  test("pending row missing expires_at defensively maps to n/a (shouldn't happen on disk, but doesn't 500 the page)", () => {
    expect(
      installTokenStatus({
        install_state: "pending",
        install_token_expires_at: null,
        is_self: false,
      }).status,
    ).toBe("n/a");
  });

  test("pending with garbage expiry string falls back to n/a", () => {
    expect(
      installTokenStatus({
        install_state: "pending",
        install_token_expires_at: "not-a-date",
        is_self: false,
      }).status,
    ).toBe("n/a");
  });
});

describe("installTokenLabel + installTokenBadgeClass (T-0113)", () => {
  test("active label embeds the expiry timestamp", () => {
    const label = installTokenLabel({
      status: "active",
      expiresAt: "2026-05-28T12:00:00Z",
    });
    expect(label).toContain("active");
    expect(label).toContain("2026-05-28T12:00:00Z");
  });

  test("expired label embeds the timestamp", () => {
    expect(
      installTokenLabel({
        status: "expired",
        expiresAt: "2026-05-26T12:00:00Z",
      }),
    ).toContain("2026-05-26T12:00:00Z");
  });

  test("consumed label is a fixed string with no timestamp", () => {
    expect(installTokenLabel({ status: "consumed", expiresAt: null })).toBe(
      "install token consumed",
    );
  });

  test("badge class differentiates the three on-screen states", () => {
    const a = installTokenBadgeClass("active");
    const e = installTokenBadgeClass("expired");
    const c = installTokenBadgeClass("consumed");
    expect(new Set([a, e, c]).size).toBeGreaterThan(1);
  });
});
