/**
 * Pure helpers for Shell.tsx's sidebar restructure (T-0059 — T-0065).
 *
 * The Shell component renders four scope sections — GLOBAL / SERVER /
 * ATTACHMENT / MOTHERSHIP — per the locked contract in
 * `vision/multi-server/nav-restructure.md`. The visibility, the picker
 * default, and the admin gates are all decided here so we have a vitest
 * seam without a DOM (matches the convention set by Select.test.ts +
 * ProjectSwitcher.test.ts).
 */

export type SidebarFlags = {
  /** import.meta.env.VITE_MOTHERSHIP === "1". */
  isMothershipBuild: boolean;
  /** Super-admin (T-0066 introduces `is_super_admin` on /api/me;
   *  until then we fall back to `is_admin`). */
  isSuperAdmin: boolean;
  /** Server-local admin (auth.toml `is_admin` on the picked server). */
  isServerAdmin: boolean;
};

export type SidebarVisibility = {
  /** Always true. */
  global: boolean;
  /** Always true. The picker dropdown is only rendered on mothership. */
  server: boolean;
  /** Mothership-only AND super-admin. */
  serverPicker: boolean;
  /** SERVER > users / settings rows. */
  serverAdminItems: boolean;
  /** Always true. ATTACHMENT body is per-attachment (Bundle C fills it). */
  attachment: boolean;
  /** Mothership-only AND super-admin. Tree-shaken on detach. */
  mothership: boolean;
};

export function sidebarSectionVisibility(flags: SidebarFlags): SidebarVisibility {
  return {
    global: true,
    server: true,
    serverPicker: flags.isMothershipBuild,
    serverAdminItems: flags.isServerAdmin,
    attachment: true,
    mothership: flags.isMothershipBuild && flags.isSuperAdmin,
  };
}
