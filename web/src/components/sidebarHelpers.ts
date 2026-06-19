/**
 * Pure helpers backing the Shell sidebar + the server-attachment surface.
 *
 * History: this module once encoded a four-scope sidebar model (GLOBAL /
 * SERVER / ATTACHMENT / MOTHERSHIP, the locked `D-0019-nav-restructure`
 * contract). Sidebar v3 (T-0170) collapsed the IA onto the role hierarchy
 * and stopped rendering that model entirely, so the dead abstraction
 * (`sidebarSectionVisibility`, `visibleSidebarSections`,
 * `ATTACHMENT_SIDEBAR_ITEMS`, the ATTACHMENT worker-status pill) was
 * removed in T-0224. What remains are the helpers v3 still imports:
 *   • the super-admin gate read off `/api/me` (Shell + mothership pages);
 *   • the server-picker storage key + initial-selection resolver
 *     (mothership/ServerPicker);
 *   • the attachment server-id read used by the per-attachment pages.
 * They keep a no-DOM vitest seam (matches Select.test.ts +
 * ProjectSwitcher.test.ts).
 */

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

/** T-0060 — localStorage key for the server picker selection. Scoped per
 *  installation; in a multi-user context the key would also be per-user,
 *  but the cookie session already isolates browsers. */
export const SERVER_PICKER_STORAGE_KEY = "srv.picker.server_id";

/**
 * T-0061 — sentinel passed to ``/api/me/attachment/<server_id>/...`` when
 * no picker selection is known. The backend resolves it to this install's
 * own server-id (or falls through to UserMeta on detach builds with no
 * mothership registry), so this value is safe to send regardless of build.
 */
export const SELF_SERVER_ID = "self";

/**
 * Resolve the server-id ATTACHMENT-scoped API calls should target. Reads
 * the same localStorage key the picker writes (T-0060) so the picker's
 * choice flows transparently to non-Shell pages. ``null``/empty/missing
 * → ``"self"`` sentinel which the backend resolves server-side.
 *
 * The ``storage`` arg is injectable so node-env tests (vitest, no jsdom)
 * can pass a stub without crashing on a missing ``localStorage`` global.
 */
type StorageLike = Pick<Storage, "getItem">;

export function readAttachmentServerId(storage?: StorageLike | null): string {
  const s =
    storage ?? (typeof localStorage !== "undefined" ? localStorage : null);
  if (s === null) return SELF_SERVER_ID;
  try {
    const v = s.getItem(SERVER_PICKER_STORAGE_KEY);
    if (v && v.length > 0) return v;
  } catch {
    /* ignore quota / disabled */
  }
  return SELF_SERVER_ID;
}

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
