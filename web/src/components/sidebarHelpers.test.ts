import { describe, expect, test } from "vitest";

import {
  ATTACHMENT_SIDEBAR_ITEMS,
  SELF_SERVER_ID,
  SERVER_PICKER_STORAGE_KEY,
  attachmentSidebarItems,
  isSuperAdminFromMe,
  readAttachmentServerId,
  resolveInitialPickedServer,
  sidebarSectionVisibility,
  visibleSidebarSections,
  workerStatusPaint,
  type PickerCandidate,
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

describe("T-0065 — detach build shape (VITE_MOTHERSHIP=0)", () => {
  // Locked contract: detach build looks like today's SYSTEM sidebar
  // plus the new GLOBAL header. GLOBAL + SERVER (no picker) +
  // ATTACHMENT visible, MOTHERSHIP gone. Test by pretending we ran
  // with the env literal flipped — the gating logic lives in pure
  // helpers so we don't need import.meta.env mocking infra.
  test("non-admin user on detach: 3 sections, no picker, no MOTHERSHIP", () => {
    const f = flags({ isMothershipBuild: false });
    const v = sidebarSectionVisibility(f);
    expect(v.global).toBe(true);
    expect(v.server).toBe(true);
    expect(v.attachment).toBe(true);
    expect(v.mothership).toBe(false);
    expect(v.serverPicker).toBe(false);
    expect(visibleSidebarSections(f)).toEqual(["global", "server", "attachment"]);
  });

  test("super-admin on detach still hides MOTHERSHIP (build flag wins)", () => {
    // The MOTHERSHIP module is tree-shaken out of the detach bundle
    // entirely — a logged-in super-admin on a detach install must NOT
    // see a section that has no implementation.
    const v = sidebarSectionVisibility(
      flags({ isMothershipBuild: false, isSuperAdmin: true }),
    );
    expect(v.mothership).toBe(false);
  });

  test("admin on detach still sees SERVER admin items (auth.toml is per-server)", () => {
    const v = sidebarSectionVisibility(
      flags({ isMothershipBuild: false, isServerAdmin: true }),
    );
    expect(v.serverAdminItems).toBe(true);
    expect(v.serverPicker).toBe(false);
  });
});

describe("visibleSidebarSections", () => {
  test("detach build, anon: GLOBAL > SERVER > ATTACHMENT (no MOTHERSHIP)", () => {
    expect(visibleSidebarSections(flags())).toEqual([
      "global",
      "server",
      "attachment",
    ]);
  });

  test("mothership build + super-admin: all four sections in contract order", () => {
    expect(
      visibleSidebarSections(flags({ isMothershipBuild: true, isSuperAdmin: true })),
    ).toEqual(["global", "server", "attachment", "mothership"]);
  });

  test("ATTACHMENT placeholder is always present (T-0061 contract: every attached user has it)", () => {
    // The body is filled by Bundle C; the header is unconditional.
    expect(visibleSidebarSections(flags())).toContain("attachment");
    expect(visibleSidebarSections(flags({ isMothershipBuild: true }))).toContain(
      "attachment",
    );
  });
});

describe("workerStatusPaint (T-0063 — pill moved to ATTACHMENT)", () => {
  test("alive === null → idle/UNKNOWN", () => {
    const p = workerStatusPaint(null);
    expect(p.dotClass).toBe("mc-dot mc-dot-idle");
    expect(p.label).toBe("UNKNOWN");
  });

  test("alive === true → active/OPERATIONAL", () => {
    const p = workerStatusPaint(true);
    expect(p.dotClass).toBe("mc-dot mc-dot-active");
    expect(p.label).toBe("OPERATIONAL");
    expect(p.color).toBe("var(--mc-green)");
  });

  test("alive === false → error/WORKER OFFLINE", () => {
    const p = workerStatusPaint(false);
    expect(p.dotClass).toBe("mc-dot mc-dot-error");
    expect(p.label).toBe("WORKER OFFLINE");
    expect(p.color).toBe("var(--mc-red)");
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

describe("attachmentSidebarItems (T-0061 — locked contract)", () => {
  test("returns the three items from the nav-restructure spec, in order", () => {
    const items = attachmentSidebarItems();
    expect(items.map((i) => i.key)).toEqual([
      "tg-binding",
      "my-sessions",
      "my-worker",
    ]);
    expect(items.map((i) => i.label)).toEqual([
      "TG BINDING",
      "MY SESSIONS",
      "WORKER CONTROLS",
    ]);
  });

  test("routes to /attachment/* paths so detach + mothership share the same shell", () => {
    // Both builds render the same body — the spec calls out that ATTACHMENT
    // is identical on detach AND mothership (only SERVER changes shape).
    for (const item of attachmentSidebarItems()) {
      expect(item.to.startsWith("/attachment/")).toBe(true);
    }
  });

  test("ATTACHMENT_SIDEBAR_ITEMS is the exported constant (stable reference)", () => {
    // Some callers may want to import the constant directly (e.g. tests
    // pinning the spec) — verify the helper returns it untouched so we
    // don't accidentally diverge the two surfaces.
    expect(attachmentSidebarItems()).toBe(ATTACHMENT_SIDEBAR_ITEMS);
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
