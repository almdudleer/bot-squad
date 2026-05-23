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

/** T-0060 — localStorage key for the server picker selection. Scoped per
 *  installation; in a multi-user context the key would also be per-user,
 *  but the cookie session already isolates browsers. */
export const SERVER_PICKER_STORAGE_KEY = "srv.picker.server_id";

export type PickerCandidate = {
  /** Server id in the mothership registry. */
  id: string;
  /** True for the mothership's own self-entry; used as the fallback default. */
  isSelf?: boolean;
};

/**
 * Decide which server the picker should land on when the dropdown first
 * renders. Precedence:
 *   1. Stored selection from a prior session, if it still maps to an
 *      attached server (otherwise it's stale and we ignore it).
 *   2. The "current" server id derived from the URL/route (`currentId`)
 *      when present — keeps the picker in sync with where the user
 *      actually is.
 *   3. The self-server (`is_self === true`) — the mothership itself, which
 *      is the unambiguous home base.
 *   4. The first attached server, if any.
 *   5. `null` — no servers attached.
 *
 * Pure function: takes today's stored value + current route hint + the
 * known-attached list, returns the resolved id. The caller writes the
 * resolved id back to localStorage so a fresh user converges to a sticky
 * choice.
 */
export function resolveInitialPickedServer(
  stored: string | null,
  currentId: string | null,
  attached: PickerCandidate[],
): string | null {
  const ids = new Set(attached.map((s) => s.id));
  if (stored && ids.has(stored)) return stored;
  if (currentId && ids.has(currentId)) return currentId;
  const self = attached.find((s) => s.isSelf);
  if (self) return self.id;
  return attached.length > 0 ? attached[0].id : null;
}
