import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useParams, useLocation } from "react-router-dom";
import { api } from "../api";
import { AutoupdatePill } from "./AutoupdatePill";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { isSuperAdminFromMe } from "./sidebarHelpers";
import { GlobalBusyIndicator } from "./GlobalBusyIndicator";

const PINNED_PROJECT_KEY = "bot-squad:last-project";

// T-0089: the autoupdate pill is consumer-side only — Vite inlines this
// constant so the mothership bundle tree-shakes the component import away.
const IS_MOTHERSHIP_BUILD = import.meta.env.VITE_MOTHERSHIP === "1";

/**
 * Shell — left sidebar navigation present on every authenticated page.
 *
 * T-0170 (sidebar v3) collapses the IA onto the role hierarchy
 * (`vision/roles/role-hierarchy.md`):
 *   • PROJECT  — the prominent `[ PROJECT ]` block: Board / Roadmap / User
 *                Feedback / Use Cases, with a quieter nested AGENTS sub-section
 *                (Agent Sessions / Workflow / Deployment Queue / Analytics).
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
  const [pinnedSlug, setPinnedSlug] = useState<string | null>(() => {
    try {
      return localStorage.getItem(PINNED_PROJECT_KEY);
    } catch {
      return null;
    }
  });

  // Keep pinned project in sync when the URL has a slug
  useEffect(() => {
    if (urlSlug && urlSlug !== pinnedSlug) {
      setPinnedSlug(urlSlug);
      try {
        localStorage.setItem(PINNED_PROJECT_KEY, urlSlug);
      } catch {
        /* ignore quota / disabled */
      }
    }
  }, [urlSlug, pinnedSlug]);

  // Display slug: URL takes precedence; otherwise the pinned slug
  const slug = urlSlug ?? pinnedSlug;
  const hasProject = Boolean(slug);

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
          {/* T-0089: consumer-only autoupdate status pill. Skipped on the
              mothership build so we don't poll a 404 endpoint forever. */}
          {!IS_MOTHERSHIP_BUILD && <AutoupdatePill />}
        </div>

        {/* Project section — shows pinned project even on global routes */}
        {hasProject && slug && (
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
                of its own; it reads as the project's primary links. */}
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
              {/* T-0235 (Pillar C): USER FEEDBACK + USE CASES retired as
                  top-level nav — they now live UNDER the docs section via its
                  sub-nav (Docs · User Feedback · Use Cases). Old deep-links
                  redirect into /docs/<sub> (App.tsx LegacyDocsRedirect). */}
              <li>
                <NavLink
                  to={`/p/${slug}/docs`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  DOCS
                </NavLink>
              </li>
            </ul>

            {/* T-0170: AGENTS is a NESTED sub-section under PROJECT, not a
                peer of it — a quieter, unbracketed header (vs. the bracketed
                `[ PROJECT ]`). These links are about the agents working this
                project: sessions, workflow, deploy queue, analytics. */}
            <div className="mc-sidebar-subsection">Agents</div>
            <ul className="mc-sidebar-nav mc-sidebar-nav-nested">
              <li>
                <NavLink
                  to={`/p/${slug}/sessions`}
                  data-onboarding-anchor="sessions-nav"
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  AGENT SESSIONS
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/workflow`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  AGENT WORKFLOW
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
              {/* T-0296: per-project dev/prod clone health ("Installation ≠
                  Project"). Read is any-authed (the admin-only pull-master
                  action is gated inside the page). */}
              <li>
                <NavLink
                  to={`/p/${slug}/clones`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  CLONES
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
            </ul>
          </>
        )}

        {/* MOTHERSHIP — T-0170. Global-admin-only, and a SINGLE entry now
            (was three rows: All Users / Attached Servers / + Add Server, with
            "Attached Servers" wrongly pointing at the all-projects view at `/`
            — the stakeholder's "attached servers still showing the allprojects
            view" complaint). It links to the consolidated mothership admin page
            (global users + invites + connected servers). Tree-shaken out of
            detach bundles via the VITE_MOTHERSHIP literal gate; further gated
            on super-admin so global members never see it. Distinct from "All
            Projects" (a user surface, reached via the wordmark + switcher). */}
        {IS_MOTHERSHIP_BUILD && isSuperAdmin && (
          <>
            <div className="mc-sidebar-divider" aria-hidden="true" />
            {/* T-0318: MOTHERSHIP is now a subsection header (matching the
                Agents pattern) with its primary destinations as nested rows.
                T-0170 had collapsed this to a SINGLE link → /m/users, which
                buried Releases + Global-users two clicks deep behind inline
                links on the users page. Both are now ≤1 click from the
                sidebar. */}
            <div className="mc-sidebar-subsection">Mothership</div>
            <ul className="mc-sidebar-nav mc-sidebar-nav-nested">
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
      <main className="mc-main">
        <Outlet />
      </main>
    </div>
  );
}
