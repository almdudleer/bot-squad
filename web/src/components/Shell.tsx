import { Suspense, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useParams, useLocation } from "react-router-dom";
import { api } from "../api";
import { AutoupdatePill } from "./AutoupdatePill";
import { WorkerHealthPill } from "./WorkerHealthPill";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { RouteSkeleton } from "./RouteSkeleton";
import { isMoreOpsRoute, isSuperAdminFromMe, resolveRailContext } from "./sidebarHelpers";
import { GlobalBusyIndicator } from "./GlobalBusyIndicator";
import { useProjectExists } from "./useProjectExists";

const PINNED_PROJECT_KEY = "bot-squad:last-project";

// T-0089: the autoupdate pill is consumer-side only — Vite inlines this
// constant so the mothership bundle tree-shakes the component import away.
const IS_MOTHERSHIP_BUILD = import.meta.env.VITE_MOTHERSHIP === "1";

/**
 * Shell — left sidebar navigation present on every authenticated page.
 *
 * T-0170 (sidebar v3) collapses the IA onto the role hierarchy
 * (`vision/roles/role-hierarchy.md`); T-0637 (D-0057 §4/§8, wave 3 declutter)
 * further collapsed the per-project rail from 6 destinations to 3 + a low-
 * emphasis group:
 *   • PROJECT  — the prominent `[ PROJECT ]` block, primary tier: Board /
 *                Roadmap / Processes (Agent Sessions) — D-0057's own
 *                "measured target" is exactly this trio, nothing more. A
 *                quieter "More / Ops" disclosure holds Docs (the durable
 *                read-first artifact tree — User Feedback + Use Cases no
 *                longer get their own rail entries, R5/R7), Analytics and
 *                Deployment Queue — lower-frequency lookup / ops-history
 *                surfaces, not daily at-a-glance state (R3), collapsed by
 *                default.
 *   • MOTHERSHIP — a single "Mothership" entry, global-admin only (global
 *                users + invites + connected servers). Tree-shaken off
 *                detach builds via the VITE_MOTHERSHIP literal gate.
 *   • Server settings live behind a gear in the header (admin-gated → the
 *                server settings page), parallel to the project gear (T-0167).
 * The v2 "MORE" drawer + "Attachment" sub-group + "All Projects" sidebar
 * entry are gone — their children re-homed to the server-settings page and
 * My Profile, and the server picker dropped (per-attachment scoping now
 * defaults to self).
 *
 * Project pinning contract (one pin or none, exactly one way each):
 *   • PIN     — visiting any /p/:slug/* URL pins that slug.
 *   • UNPIN   — the "← all projects" row inside the ProjectSwitcher dropdown
 *               (the only un-pin affordance other than sign-out).
 *   • The pin survives navigating to /, /help, etc. — the [PROJECT] block
 *               stays so per-project links remain one click away.
 */
export function Shell() {
  const { slug: urlSlug } = useParams<{ slug?: string }>();
  const location = useLocation();
  const [username, setUsername] = useState<string | null>(null);
  const [linuxUser, setLinuxUser] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState<boolean>(false);
  // T-0062: super-admin gates the MOTHERSHIP section. Read from /api/me;
  // pre-T-0066 the field doesn't exist server-side so isSuperAdminFromMe
  // falls back to is_admin. Drop the fallback once users-model-split lands.
  const [isSuperAdmin, setIsSuperAdmin] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // T-0637: the "More / Ops" rail disclosure (Analytics + Deployment Queue).
  // Collapsed by default; starts open when the initial route already lives
  // inside it so a direct deep-link doesn't hide its own active entry.
  const [moreOpsOpen, setMoreOpsOpen] = useState(() => isMoreOpsRoute(location.pathname));
  const [pinnedSlug, setPinnedSlug] = useState<string | null>(() => {
    try {
      return localStorage.getItem(PINNED_PROJECT_KEY);
    } catch {
      return null;
    }
  });

  // Display slug: URL takes precedence; otherwise the pinned slug
  const slug = urlSlug ?? pinnedSlug;
  const hasProject = Boolean(slug);

  // T-0602 (N3): does the display slug name a real (user-visible) project?
  // false suppresses the project nav rail — a bogus /p/<slug> must not wear
  // full project chrome pretending the project exists. null (unresolved /
  // list unavailable) keeps the rail, so chrome never flickers on slow loads.
  const projectExists = useProjectExists(slug);

  // Keep pinned project in sync when the URL has a slug. T-0602: only pin
  // slugs confirmed to exist, so a bogus deep-link doesn't overwrite the
  // user's real pinned project.
  useEffect(() => {
    if (urlSlug && urlSlug !== pinnedSlug && projectExists === true) {
      setPinnedSlug(urlSlug);
      try {
        localStorage.setItem(PINNED_PROJECT_KEY, urlSlug);
      } catch {
        /* ignore quota / disabled */
      }
    }
  }, [urlSlug, pinnedSlug, projectExists]);

  // T-0357 (de-fleet the chrome). Fleet/admin chrome belongs to super-admins on
  // a mothership build only; a non-admin / single-project operator must see ZERO
  // fleet chrome — pure single-brain. `railContext` then guarantees the
  // per-project rail and the fleet/admin rail are NEVER shown at the same time
  // (the dogfood T-0331 incoherence: a pin leaked the watchrobot rail over the
  // cross-server fleet body on `/m/*`).
  const isFleetCapable = IS_MOTHERSHIP_BUILD && isSuperAdmin;
  const railContext = resolveRailContext({
    pathname: location.pathname,
    hasProject,
    isFleetCapable,
  });

  // Poll the health endpoint every 30 s — kept only for the username fallback
  // now that the worker-status pill is gone (T-0167 removed the OPERATIONAL
  // footer readout the stakeholder flagged as meaningless).
  useEffect(() => {
    let cancelled = false;
    function checkHealth() {
      api
        .health()
        .then((h) => {
          if (cancelled) return;
          const u = (h as Record<string, unknown>)?.username;
          if (typeof u === "string") setUsername(u);
        })
        .catch(() => {
          /* health unavailable — username still comes from /api/me */
        });
    }
    checkHealth();
    const id = setInterval(checkHealth, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Identify the logged-in user once on mount so the footer can show
  // who is acting on which Linux user's tmux.
  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((m) => {
        if (cancelled) return;
        setUsername(m.username);
        setLinuxUser(m.linux_user);
        setIsAdmin(Boolean(m.is_admin));
        setIsSuperAdmin(isSuperAdminFromMe(m));
      })
      .catch(() => {
        /* anonymous — login redirect handled elsewhere */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Close sidebar on route change (mobile)
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  // T-0637: auto-open "More / Ops" on in-app navigation into Analytics /
  // Deployment Queue. One-way (never auto-closes) so a manual toggle-open
  // elsewhere in the rail isn't fought on every route change.
  useEffect(() => {
    if (isMoreOpsRoute(location.pathname)) setMoreOpsOpen(true);
  }, [location.pathname]);

  function handleSignOut() {
    try {
      localStorage.removeItem(PINNED_PROJECT_KEY);
    } catch {
      /* ignore */
    }
    api
      .logout()
      .then(() => (window.location.href = "/login"))
      .catch(() => (window.location.href = "/login"));
  }

  // The ONLY way to unpin (other than sign-out).
  function unpinProject() {
    try {
      localStorage.removeItem(PINNED_PROJECT_KEY);
    } catch {
      /* ignore */
    }
    setPinnedSlug(null);
  }

  return (
    <div className="mc-layout">
      {/* Mobile toggle */}
      <button
        type="button"
        className="mc-sidebar-toggle"
        onClick={() => setSidebarOpen((v) => !v)}
        aria-label="Toggle navigation"
      >
        ☰
      </button>

      {/* Overlay for mobile */}
      <div
        className={`mc-sidebar-overlay${sidebarOpen ? " open" : ""}`}
        onClick={() => setSidebarOpen(false)}
      />

      {/* ── Sidebar ─────────────────────────────────────────── */}
      <nav className={`mc-sidebar${sidebarOpen ? " open" : ""}`}>

        {/* Header strip. Hosts the wordmark (→ home / all-projects), the
            cross-project work indicator (T-0064/T-0170), and an icon cluster:
            a server-settings gear (admin-gated — the server-level half of the
            old ATTACHMENT/SERVER groups now lives behind it, T-0170) plus the
            help `?` (T-0167). */}
        <div className="mc-sidebar-header">
          <div className="mc-sidebar-header-top">
            <Link to="/" className="mc-wordmark">BOT·SQUAD</Link>
            <div className="mc-sidebar-header-icons">
              {/* T-0357: the ONLY door from a project's brain into the fleet/
                  admin area. FLEET used to be a peer nav item at the bottom of
                  EVERY per-project rail (it advertised the fleet from inside a
                  single brain — the dogfood T-0331 incoherence). It is demoted
                  to this single, visually-separate header affordance, gated on
                  super-admin + mothership so a plain operator never sees it. */}
              {isFleetCapable && (
                <NavLink
                  to="/m"
                  className={({ isActive }) =>
                    isActive
                      ? "mc-sidebar-fleet-icon active"
                      : "mc-sidebar-fleet-icon"
                  }
                  aria-label="Fleet / admin"
                  title="Fleet / admin"
                  data-onboarding-anchor="all-projects-nav"
                >
                  ▦
                </NavLink>
              )}
              {/* T-0170: server settings reached via a gear here, parallel to
                  the per-project settings gear (T-0167). Admin-gated because
                  /system-settings is server-admin only; non-admins never see
                  it. This is the re-home target for the old SERVER-scope rows
                  (server users / scheduler are linked from that page) and the
                  detached-install TG-bot config (T-0171). */}
              {isAdmin && (
                <NavLink
                  to="/system-settings"
                  className={({ isActive }) =>
                    isActive
                      ? "mc-sidebar-server-gear active"
                      : "mc-sidebar-server-gear"
                  }
                  // T-0224: the header gear (server/system-wide settings) and
                  // the [PROJECT] gear are visually identical, so spell out the
                  // scope in title/aria-label to disambiguate them at a glance.
                  aria-label="Server / system settings"
                  title="Server / system settings"
                >
                  ⚙
                </NavLink>
              )}
              <Link
                to="/help"
                className="mc-sidebar-help-icon"
                aria-label="Help"
                title="Help"
                data-onboarding-anchor="help-nav"
              >
                ?
              </Link>
            </div>
          </div>
          <GlobalBusyIndicator myUsername={username} />
          {/* T-0456: failure-only worker-health pill — renders nothing while
              healthy, lights red on dead_heartbeat / sha_drift. Consumer-only
              like the autoupdate pill (local /api/health). */}
          {!IS_MOTHERSHIP_BUILD && <WorkerHealthPill />}
          {/* T-0089: consumer-only autoupdate status pill. Skipped on the
              mothership build so we don't poll a 404 endpoint forever. */}
          {!IS_MOTHERSHIP_BUILD && <AutoupdatePill />}
        </div>

        {/* Project section — shows pinned project even on global routes, but
            NOT in the fleet/admin area (T-0357: railContext keeps the
            per-project rail and the admin rail mutually exclusive), and NOT
            for a slug the project list positively excludes (T-0602 N3). */}
        {railContext === "project" && slug && projectExists !== false && (
          <>
            <div className="mc-sidebar-section">Project</div>
            <div className="mc-sidebar-project">
              {/* T-0167: Project Settings left the nav list and is now a gear
                  icon sitting next to the project name. */}
              <div className="mc-sidebar-project-name-row">
                <div className="mc-sidebar-project-name">{slug}</div>
                <NavLink
                  to={`/p/${slug}/settings`}
                  className={({ isActive }) =>
                    isActive
                      ? "mc-sidebar-project-gear active"
                      : "mc-sidebar-project-gear"
                  }
                  aria-label="Project settings"
                  title="Project settings"
                >
                  ⚙
                </NavLink>
              </div>
              <ProjectSwitcher slug={slug} onUnpin={unpinProject} />
            </div>

            {/* Product nav — the body of the [ PROJECT ] section. No header
                of its own; it reads as the project's primary links.
                T-0637 (D-0057 §4/§8, TL review 2026-07-25): the everyday
                rail collapsed from 6 destinations to EXACTLY this primary
                Board/Roadmap/Processes trio — D-0057's own "measured
                target" line names only these three; Docs is a separate,
                lower-frequency lookup surface and moved into "More / Ops"
                below alongside Analytics/Deployment Queue rather than
                sitting here as a quiet 4th item. */}
            <ul className="mc-sidebar-nav">
              <li>
                <NavLink
                  to={`/p/${slug}`}
                  end
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  BOARD
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/vision`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  VISION
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/sessions`}
                  data-onboarding-anchor="sessions-nav"
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  PROCESSES
                </NavLink>
              </li>
            </ul>

            {/* T-0637 (D-0057 §4, Q3 + TL review 2026-07-25): "More / Ops" —
                a low-emphasis rail disclosure (not a settings-gear demotion)
                holding the surfaces that aren't daily at-a-glance state
                (R3): Docs (the durable read-first artifact tree — User
                Feedback + Use Cases have no rail entry of their own,
                folded into the single cross-store tree, T-0337; old
                deep-links redirect via App.tsx LegacyDocsRedirect),
                Analytics, and the Deployment Queue. Collapsed by default;
                auto-opens on navigation into any of the three (see the
                isMoreOpsRoute effect above) so a direct deep-link never
                hides its own active entry. */}
            <button
              type="button"
              className="mc-sidebar-more-ops-toggle"
              aria-expanded={moreOpsOpen}
              onClick={() => setMoreOpsOpen((v) => !v)}
            >
              <span>More / Ops</span>
              <span className="mc-sidebar-more-caret">{moreOpsOpen ? "▾" : "▸"}</span>
            </button>
            {moreOpsOpen && (
              <ul className="mc-sidebar-nav mc-sidebar-nav-nested">
                <li>
                  <NavLink
                    to={`/p/${slug}/docs`}
                    className={({ isActive }) => (isActive ? "active" : undefined)}
                  >
                    DOCS
                  </NavLink>
                </li>
                {/* T-0147: internal-usage analytics dashboard. */}
                <li>
                  <NavLink
                    to={`/p/${slug}/analytics`}
                    data-onboarding-anchor="analytics-nav"
                    className={({ isActive }) => (isActive ? "active" : undefined)}
                  >
                    ANALYTICS
                  </NavLink>
                </li>
                <li>
                  <NavLink
                    to={`/p/${slug}/runs`}
                    className={({ isActive }) => (isActive ? "active" : undefined)}
                  >
                    DEPLOYMENT QUEUE
                  </NavLink>
                </li>
              </ul>
            )}
          </>
        )}

        {/* FLEET/ADMIN RAIL — T-0357 (de-fleet the chrome). When the operator is
            in the fleet/admin area (`/m/*`), the sidebar swaps the per-project
            rail for THIS admin rail — it is about the FLEET (servers + global
            concerns), not about any one project. This replaces both (a) the old
            always-on FLEET nav item that bolted onto every per-project rail and
            (b) the leaked watchrobot project rail that used to sit over the
            cross-server fleet body (the dogfood T-0331 incoherence). Reached via
            the header ▦ "Fleet / admin" affordance; gated on super-admin +
            mothership build via railContext === "fleet". */}
        {railContext === "fleet" && (
          <>
            <div className="mc-sidebar-section">Fleet</div>
            <ul className="mc-sidebar-nav">
              <li>
                <NavLink
                  to="/m"
                  end
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  SERVERS
                </NavLink>
              </li>
              <li>
                <NavLink
                  to="/m/users"
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  GLOBAL USERS
                </NavLink>
              </li>
              <li>
                <NavLink
                  to="/m/releases"
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  RELEASES
                </NavLink>
              </li>
            </ul>
          </>
        )}

        {/* Footer — T-0167: the OPERATIONAL worker-status pill is gone (the
            stakeholder flagged it as meaningless near the profile). The
            bottom-most row is now a clickable "My Profile" link showing the
            user, with a smaller secondary-weight sign-out icon beside it. */}
        <div className="mc-sidebar-footer">
          <div className="mc-sidebar-profile-row">
            <NavLink
              to="/me"
              className={({ isActive }) =>
                isActive
                  ? "mc-sidebar-profile active"
                  : "mc-sidebar-profile"
              }
            >
              <span className="mc-sidebar-profile-label">My Profile</span>
              {username && (
                <span className="mc-sidebar-profile-user">
                  {username}
                  {linuxUser && linuxUser !== username
                    ? ` (${linuxUser})`
                    : ""}
                </span>
              )}
            </NavLink>
            <button
              type="button"
              className="mc-sidebar-signout-icon"
              onClick={handleSignOut}
              aria-label="Sign out"
              title="Sign out"
            >
              ⏻
            </button>
          </div>
        </div>
      </nav>

      {/* ── Main content ──────────────────────────────────── */}
      {/* T-0365: the route components are lazy-loaded (App.tsx) to shrink the
          first-paint bundle; this Suspense paints an instant content skeleton
          while a route chunk loads, instead of a blank frame. */}
      <main className="mc-main">
        <Suspense fallback={<RouteSkeleton />}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
