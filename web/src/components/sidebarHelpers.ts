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

/**
 * Section headers in render order, per the locked diagram in
 * `vision/multi-server/nav-restructure.md`. Returns only sections that are
 * visible under the given flags — callers iterate to drive both Shell.tsx
 * markup AND tests that pin the contract order.
 */
export type SidebarSectionKey = "global" | "server" | "attachment" | "mothership";

/**
 * T-0062 — read the super-admin flag from /api/me, falling back to the
 * legacy `is_admin` field until T-0066's users-model split ships the
 * canonical `is_super_admin` boolean. Until then a server-local admin
 * who is ALSO the install owner (the alexey case) is treated as the
 * super-admin. Once T-0066 lands this becomes a pure lookup of
 * `me.is_super_admin`. TODO(T-0066): drop the fallback.
 */
export type MeLike = {
  is_admin?: boolean;
  is_super_admin?: boolean;
};
export function isSuperAdminFromMe(me: MeLike | null | undefined): boolean {
  if (!me) return false;
  if (typeof me.is_super_admin === "boolean") return me.is_super_admin;
  return Boolean(me.is_admin);
}

export function visibleSidebarSections(flags: SidebarFlags): SidebarSectionKey[] {
  const v = sidebarSectionVisibility(flags);
  const ordered: SidebarSectionKey[] = ["global", "server", "attachment", "mothership"];
  return ordered.filter((k) => v[k]);
}

/**
 * T-0063 — operational-status pill. Pulled out of inline JSX so the
 * three-way ternary (unknown / alive / offline) is unit-testable. The
 * pill itself moves out of the Shell top header into the ATTACHMENT
 * section chrome per the contract.
 */
export type WorkerAlive = boolean | null;

export type WorkerStatusPaint = {
  /** Status class for the dot: idle (unknown), active (alive), error (down). */
  dotClass: string;
  /** Short label rendered next to the dot. */
  label: string;
  /** Foreground colour token for the label text. */
  color: string;
};

export function workerStatusPaint(alive: WorkerAlive): WorkerStatusPaint {
  if (alive === null) {
    return {
      dotClass: "mc-dot mc-dot-idle",
      label: "UNKNOWN",
      color: "var(--mc-text-faint)",
    };
  }
  if (alive) {
    return {
      dotClass: "mc-dot mc-dot-active",
      label: "OPERATIONAL",
      color: "var(--mc-green)",
    };
  }
  return {
    dotClass: "mc-dot mc-dot-error",
    label: "WORKER OFFLINE",
    color: "var(--mc-red)",
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
