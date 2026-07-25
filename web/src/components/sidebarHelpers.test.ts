import { describe, expect, test } from "vitest";

import {
  SELF_SERVER_ID,
  SERVER_PICKER_STORAGE_KEY,
  isFleetArea,
  isMoreOpsRoute,
  isSuperAdminFromMe,
  readAttachmentServerId,
  resolveInitialPickedServer,
  resolveRailContext,
  type PickerCandidate,
} from "./sidebarHelpers";

describe("isFleetArea (T-0357)", () => {
  test("matches the /m index and any /m/* sub-route", () => {
    expect(isFleetArea("/m")).toBe(true);
    expect(isFleetArea("/m/users")).toBe(true);
    expect(isFleetArea("/m/releases")).toBe(true);
    expect(isFleetArea("/m/servers/srv_x/p/foo")).toBe(true);
  });

  test("does NOT match project / global routes that merely start with /m", () => {
    // Guard against a loose startsWith('/m') prefix bug: /me, /members, /p/m
    // are not the fleet area.
    expect(isFleetArea("/")).toBe(false);
    expect(isFleetArea("/me")).toBe(false);
    expect(isFleetArea("/members")).toBe(false);
    expect(isFleetArea("/p/watchrobot")).toBe(false);
    expect(isFleetArea("/p/m")).toBe(false);
  });
});

describe("resolveRailContext (T-0357 — never show fleet + project chrome at once)", () => {
  test("fleet-capable super-admin in the fleet area → 'fleet' rail (even with a pin)", () => {
    // The incoherence #3 fix: a pinned project no longer leaks the per-project
    // rail over the cross-server fleet body.
    expect(
      resolveRailContext({ pathname: "/m", hasProject: true, isFleetCapable: true }),
    ).toBe("fleet");
    expect(
      resolveRailContext({ pathname: "/m/users", hasProject: true, isFleetCapable: true }),
    ).toBe("fleet");
  });

  test("fleet-capable super-admin inside a project → 'project' rail", () => {
    expect(
      resolveRailContext({ pathname: "/p/watchrobot", hasProject: true, isFleetCapable: true }),
    ).toBe("project");
  });

  test("GUARD 1: a non-fleet-capable operator NEVER lands in 'fleet' — pure single-brain", () => {
    // Even if somehow at /m, a non-super-admin sees no admin rail. With a pin
    // they keep their project rail; without one, nothing.
    expect(
      resolveRailContext({ pathname: "/m", hasProject: true, isFleetCapable: false }),
    ).toBe("project");
    expect(
      resolveRailContext({ pathname: "/m", hasProject: false, isFleetCapable: false }),
    ).toBe("none");
    expect(
      resolveRailContext({ pathname: "/p/watchrobot", hasProject: true, isFleetCapable: false }),
    ).toBe("project");
  });

  test("no project + not the fleet area → 'none' (the / picker renders no rail)", () => {
    expect(
      resolveRailContext({ pathname: "/", hasProject: false, isFleetCapable: true }),
    ).toBe("none");
    expect(
      resolveRailContext({ pathname: "/help", hasProject: false, isFleetCapable: false }),
    ).toBe("none");
  });
});

describe("isMoreOpsRoute (T-0637 — More/Ops rail collapse)", () => {
  test("matches the Docs, Analytics and Deployment Queue routes at any depth", () => {
    expect(isMoreOpsRoute("/p/watchrobot/docs")).toBe(true);
    expect(isMoreOpsRoute("/p/watchrobot/docs/feedback")).toBe(true);
    expect(isMoreOpsRoute("/p/watchrobot/analytics")).toBe(true);
    expect(isMoreOpsRoute("/p/watchrobot/runs")).toBe(true);
    expect(isMoreOpsRoute("/p/watchrobot/runs/run_123")).toBe(true);
  });

  test("does NOT match the primary Board/Roadmap/Processes routes", () => {
    expect(isMoreOpsRoute("/p/watchrobot")).toBe(false);
    expect(isMoreOpsRoute("/p/watchrobot/vision")).toBe(false);
    expect(isMoreOpsRoute("/p/watchrobot/sessions")).toBe(false);
  });

  test("does NOT loosely match unrelated routes sharing a prefix", () => {
    // Guard against a loose substring match: /analytics-foo, /docsomething,
    // or /p/analytics (a slug literally named "analytics") must not
    // false-positive.
    expect(isMoreOpsRoute("/p/watchrobot/analytics-foo")).toBe(false);
    expect(isMoreOpsRoute("/p/watchrobot/docsomething")).toBe(false);
    expect(isMoreOpsRoute("/p/analytics")).toBe(false);
  });
});

describe("isSuperAdminFromMe (T-0062 with T-0066 fallback)", () => {
  test("explicit is_super_admin=true wins regardless of is_admin", () => {
    expect(isSuperAdminFromMe({ is_super_admin: true, is_admin: false })).toBe(true);
  });

  test("explicit is_super_admin=false wins regardless of is_admin", () => {
    // Once T-0066 ships, a server-local admin who isn't the mothership owner
    // returns false here — they should NOT see the MOTHERSHIP section.
    expect(isSuperAdminFromMe({ is_super_admin: false, is_admin: true })).toBe(false);
  });

  test("falls back to is_admin when is_super_admin is absent (pre-T-0066)", () => {
    expect(isSuperAdminFromMe({ is_admin: true })).toBe(true);
    expect(isSuperAdminFromMe({ is_admin: false })).toBe(false);
  });

  test("null / undefined me → false (anonymous can't be super-admin)", () => {
    expect(isSuperAdminFromMe(null)).toBe(false);
    expect(isSuperAdminFromMe(undefined)).toBe(false);
  });
});

describe("readAttachmentServerId (T-0061)", () => {
  function fakeStorage(value: string | null): Pick<Storage, "getItem"> {
    return { getItem: (k) => (k === SERVER_PICKER_STORAGE_KEY ? value : null) };
  }

  test("returns the SELF sentinel when no picker selection is stored", () => {
    expect(readAttachmentServerId(fakeStorage(null))).toBe(SELF_SERVER_ID);
  });

  test("returns the SELF sentinel for an empty stored value", () => {
    expect(readAttachmentServerId(fakeStorage(""))).toBe(SELF_SERVER_ID);
  });

  test("returns the stored picker selection when present", () => {
    expect(readAttachmentServerId(fakeStorage("srv_abc123"))).toBe("srv_abc123");
  });

  test("returns the SELF sentinel when no storage exists at all (node env)", () => {
    expect(readAttachmentServerId(null)).toBe(SELF_SERVER_ID);
  });
});

describe("resolveInitialPickedServer", () => {
  function attached(...ids: string[]): PickerCandidate[] {
    return ids.map((id, i) => ({ id, isSelf: i === 0 }));
  }

  test("respects a valid stored selection", () => {
    expect(resolveInitialPickedServer("b", null, attached("a", "b", "c"))).toBe("b");
  });

  test("drops a stale stored selection (server no longer attached)", () => {
    // If a server is detached after the user picked it, we don't want to
    // silently load against a missing id — fall through to current/self.
    expect(resolveInitialPickedServer("gone", null, attached("a", "b"))).toBe("a");
  });

  test("uses currentId when no stored selection", () => {
    expect(resolveInitialPickedServer(null, "c", attached("a", "b", "c"))).toBe("c");
  });

  test("ignores currentId that isn't in attached set", () => {
    expect(resolveInitialPickedServer(null, "unknown", attached("a", "b"))).toBe("a");
  });

  test("prefers the self server when no other signal", () => {
    expect(resolveInitialPickedServer(null, null, attached("self", "peer"))).toBe(
      "self",
    );
  });

  test("falls back to first attached when no self entry exists", () => {
    const ids: PickerCandidate[] = [{ id: "a" }, { id: "b" }];
    expect(resolveInitialPickedServer(null, null, ids)).toBe("a");
  });

  test("returns null when nothing is attached", () => {
    expect(resolveInitialPickedServer(null, null, [])).toBeNull();
    expect(resolveInitialPickedServer("anything", "anything", [])).toBeNull();
  });
});
