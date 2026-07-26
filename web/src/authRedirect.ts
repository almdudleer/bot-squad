/**
 * T-0714 — the ONE 401 -> /login gate for the whole frontend.
 *
 * Why this module exists at all: the policy used to live inside `api.ts`'s
 * `call()`, but `call()` is not unique — `mothership/api.ts`, `attachmentApi.ts`
 * and one inline `fetch` in `api.ts` each carry their own copy of the same
 * six lines. T-0714 was fixed once in `api.ts` alone and shipped still broken,
 * because on a MOTHERSHIP build the Shell's GlobalBusyIndicator reaches the
 * mirror's `call()` on every mount and that copy still bounced anonymously.
 * (D-0057 §7 flagged `api.ts` + the mothership mirror as hotspot H1; everyone
 * read H1 as a concurrent-EDIT risk, and it bit as a duplicate-IMPLEMENTATION
 * gap instead.)
 *
 * So: the transport stays duplicated (the copies differ in typing, caching and
 * proxying), but the *policy* lives here exactly once. Every 401 handler calls
 * `redirectOn401(path)` — a new copy of `call()` that forgets to is the only
 * way to regress this, and there is nothing left to keep in sync.
 */

/**
 * Routes that must render for an anonymous visitor. `/help` says so in its own
 * first paragraph ("always reachable at /help — no login required") and the
 * server does serve it unauthenticated — but the Shell wraps every route, so
 * its background fetches (/api/health, /api/auth/me, /api/projects,
 * /api/autoupdate/status, and on mothership builds the server fan-out) all 401
 * and the FIRST one to land used to navigate the whole app to /login. Every one
 * of those callers already handles its own rejection (empty rail, no username,
 * dropped fan-out server), so suppressing the *navigation* is enough.
 */
export const PUBLIC_ROUTES: readonly string[] = ["/help"];

export function isPublicRoute(pathname: string | null | undefined): boolean {
  if (!pathname) return false;
  // Tolerate a trailing slash; "/" itself is not public.
  const clean = pathname.replace(/\/+$/, "");
  return PUBLIC_ROUTES.includes(clean);
}

/**
 * T-0601 (F4): the ONE call that must not trigger the global 401-redirect is
 * the login attempt itself — redirecting there turned a wrong password into a
 * silent form reload.
 * T-0609 breadcrumb: /api/auth/attach is the next candidate for this
 * exemption — it 401s on bad GLOBAL credentials while the caller's LOCAL
 * session cookie is still valid, so redirecting would bounce a logged-in user
 * to /login over a typo. No web caller goes through `call()` for it yet; add
 * the exemption here when one lands.
 * T-0714: the second exemption is keyed on the PAGE route, not the API path —
 * the same endpoints must still bounce an expired session off a private page.
 */
export function shouldRedirectOn401(
  path: string,
  pagePath?: string | null,
): boolean {
  // T-0601 (F4): the login attempt itself is exempt on every route.
  if (path === "/api/auth/login") return false;
  const page =
    pagePath ??
    (typeof window !== "undefined" ? window.location?.pathname : undefined);
  return !isPublicRoute(page);
}

/**
 * Call this from every `call()`/fetch wrapper the instant a 401 lands, passing
 * the API path that produced it.
 *
 * Navigates to /login and throws when the bounce applies. When the bounce is
 * exempt it returns normally and the caller falls through to its usual
 * `!res.ok` path — so an exempt 401 still rejects, but with the response body
 * attached (the login form needs that detail) and without leaving the page.
 */
export function redirectOn401(path: string, pagePath?: string | null): void {
  if (!shouldRedirectOn401(path, pagePath)) return;
  window.location.href = "/login";
  throw new Error("not authenticated");
}
