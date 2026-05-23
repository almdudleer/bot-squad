import { describe, expect, test } from "vitest";

import {
  sidebarSectionVisibility,
  type SidebarFlags,
} from "./sidebarHelpers";

function flags(over: Partial<SidebarFlags> = {}): SidebarFlags {
  return {
    isMothershipBuild: false,
    isSuperAdmin: false,
    isServerAdmin: false,
    ...over,
  };
}

describe("sidebarSectionVisibility", () => {
  test("detach build, anonymous: GLOBAL + SERVER + ATTACHMENT, no MOTHERSHIP, no picker, no admin rows", () => {
    const v = sidebarSectionVisibility(flags());
    expect(v.global).toBe(true);
    expect(v.server).toBe(true);
    expect(v.attachment).toBe(true);
    expect(v.serverPicker).toBe(false);
    expect(v.serverAdminItems).toBe(false);
    expect(v.mothership).toBe(false);
  });

  test("detach build, server-admin: admin rows on, MOTHERSHIP still hidden", () => {
    const v = sidebarSectionVisibility(flags({ isServerAdmin: true, isSuperAdmin: true }));
    expect(v.serverAdminItems).toBe(true);
    // Super-admin is gated on mothership build too — detach never shows MOTHERSHIP.
    expect(v.mothership).toBe(false);
  });

  test("mothership build, non-admin: picker visible but MOTHERSHIP gated off", () => {
    const v = sidebarSectionVisibility(flags({ isMothershipBuild: true }));
    expect(v.serverPicker).toBe(true);
    expect(v.mothership).toBe(false);
  });

  test("mothership build, super-admin: MOTHERSHIP visible", () => {
    const v = sidebarSectionVisibility(
      flags({ isMothershipBuild: true, isSuperAdmin: true }),
    );
    expect(v.mothership).toBe(true);
  });

  test("server admin alone on mothership build does NOT unlock MOTHERSHIP", () => {
    // SERVER-admin (auth.toml) is the per-server admin; MOTHERSHIP is for the
    // cross-server super-admin only. Confused authority is a known foot-gun
    // (T-0062 spec called it out), so we test it lives separately.
    const v = sidebarSectionVisibility(
      flags({ isMothershipBuild: true, isServerAdmin: true }),
    );
    expect(v.mothership).toBe(false);
    expect(v.serverAdminItems).toBe(true);
  });
});
