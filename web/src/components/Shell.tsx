import { lazy, Suspense, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useParams, useLocation } from "react-router-dom";
import { api } from "../api";
import { AutoupdatePill } from "./AutoupdatePill";
import { ProjectSwitcher } from "./ProjectSwitcher";
import {
  attachmentSidebarItems,
  isSuperAdminFromMe,
} from "./sidebarHelpers";
import { GlobalBusyIndicator } from "./GlobalBusyIndicator";

const PINNED_PROJECT_KEY = "bot-squad:last-project";
// T-0140: remember whether the collapsed "MORE" drawer (rarely-used server /
// attachment / mothership surfaces) is expanded, so the choice survives reloads.
const MORE_OPEN_KEY = "bot-squad:sidebar-more-open";

// T-0089: the autoupdate pill is consumer-side only — Vite inlines this
// constant so the mothership bundle tree-shakes the component import away.
const IS_MOTHERSHIP_BUILD = import.meta.env.VITE_MOTHERSHIP === "1";

// T-0060: server picker is mothership-only. Lazy + literal-gated so the
// detach bundle never imports the chunk (same pattern as App.tsx's
// MothershipRoutes — Vite resolves the conditional to `null` at build).
const ServerPicker = IS_MOTHERSHIP_BUILD
  ? lazy(() =>
      import("../mothership/ServerPicker").then((m) => ({
        default: m.ServerPicker,
      })),
    )
  : null;

/**
 * Shell — left sidebar navigation present on every authenticated page.
 * 240 px sticky sidebar: wordmark + worker LED, project info, nav links, system links, footer.
 *
 * Project pinning contract (one pin or none, exactly one way each):
 *   • PIN     — visiting any /p/:slug/* URL pins that slug. The only practical
 *               way to trigger this is by clicking a project in the picker.
 *   • UNPIN   — the "← all projects" row inside the ProjectSwitcher dropdown
 *               (the only un-pin affordance other than sign-out).
 *   • The pin survives navigating to /, /scheduler, /help, etc. — the
 *               [PROJECT] block stays so per-project links remain one click away.
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
  // T-0140: collapsed-by-default "MORE" drawer.
  const [moreOpen, setMoreOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem(MORE_OPEN_KEY) === "1";
    } catch {
      return false;
    }
  });
  // T-0060: which server the SERVER section + (later) ATTACHMENT scope
  // belongs to. Only meaningful on mothership; on detach the single
  // installation is implicit and the picker isn't rendered.
  const [pickedServerId, setPickedServerId] = useState<string | null>(null);
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

  function toggleMore() {
    setMoreOpen((v) => {
      const next = !v;
      try {
        localStorage.setItem(MORE_OPEN_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
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

        {/* Header strip. T-0063 evacuated the per-worker operational-status
            pill (it was misscoped — per-user-per-server, now under
            ATTACHMENT). The freed space hosts the new cross-server GLOBAL
            busy indicator (T-0064): one dot for "is any task in-flight
            RIGHT NOW for any of my projects on any server"; hover for the
            list with project + server badges. */}
        <div className="mc-sidebar-header">
          {/* T-0167: Help relocated out of the sidebar body into a `?` icon
              sitting next to the wordmark; the GLOBAL section (All Projects /
              My Profile / Help) is gone — All Projects is reachable via the
              wordmark + project switcher, My Profile moved to the footer. */}
          <div className="mc-sidebar-header-top">
            <Link to="/" className="mc-wordmark">BOT·SQUAD</Link>
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

            {/* PRODUCT — about the product (T-0167). First section, so no
                header label needed. */}
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
                  ROADMAP
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/feedback`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  USER FEEDBACK
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/usecases`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  USE CASES
                </NavLink>
              </li>
            </ul>

            {/* Divider between PRODUCT (above) and AGENTS (below) */}
            <div className="mc-sidebar-divider" aria-hidden="true" />

            {/* AGENTS — about the agents working the product (T-0167). */}
            <div className="mc-sidebar-section">AGENTS</div>
            <ul className="mc-sidebar-nav">
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

        {/* MORE — T-0140. The server/attachment/mothership scopes were three
            always-expanded sections (~10 rarely-used rows) that the stakeholder
            flagged as the bulk of the sidebar's clutter. They now collapse
            behind one default-collapsed drawer; the daily-driver GLOBAL +
            Project nav above stays one click away. The operational-status pill
            moved to the footer so worker health is never hidden by the collapse. */}
        <button
          type="button"
          className={`mc-sidebar-section mc-sidebar-more-toggle${moreOpen ? " open" : ""}`}
          aria-expanded={moreOpen}
          onClick={toggleMore}
        >
          MORE
          <span className="mc-sidebar-more-caret" aria-hidden="true">
            {moreOpen ? "▾" : "▸"}
          </span>
        </button>
        {moreOpen && (
          <div className="mc-sidebar-more">
            {/* SERVER scope. On mothership the picker selects which server the
                rows below + ATTACHMENT pages scope to (T-0060); tree-shaken on
                detach via the IS_MOTHERSHIP_BUILD literal gate. */}
            {ServerPicker && (
              <div className="mc-sidebar-more-picker">
                <span className="mc-sidebar-more-picker-label">SERVER</span>
                <Suspense
                  fallback={<span className="mc-srv-picker-loading">▾ …</span>}
                >
                  <ServerPicker
                    currentServerId={null}
                    onChange={setPickedServerId}
                  />
                </Suspense>
              </div>
            )}
            <ul
              className="mc-sidebar-nav"
              data-picked-server-id={pickedServerId ?? ""}
            >
              <li>
                <NavLink
                  to="/scheduler"
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  SCHEDULER
                </NavLink>
              </li>
              {IS_MOTHERSHIP_BUILD && (
                <li>
                  <NavLink
                    to="/m/releases"
                    data-onboarding-anchor="releases-nav"
                    className={({ isActive }) => (isActive ? "active" : undefined)}
                  >
                    RELEASES
                  </NavLink>
                </li>
              )}
              {isAdmin && (
                <>
                  <li>
                    <NavLink
                      to="/users"
                      className={({ isActive }) => (isActive ? "active" : undefined)}
                    >
                      USERS
                    </NavLink>
                  </li>
                  <li>
                    <NavLink
                      to="/system-settings"
                      className={({ isActive }) => (isActive ? "active" : undefined)}
                    >
                      SETTINGS
                    </NavLink>
                  </li>
                </>
              )}
            </ul>

            {/* ATTACHMENT — per-user-per-server (T-0061). Locked-order rows
                from attachmentSidebarItems(); the status pill moved to the
                footer (T-0140). */}
            <div className="mc-sidebar-more-sub">attachment</div>
            <ul className="mc-sidebar-nav" aria-live="polite">
              {attachmentSidebarItems().map((item) => (
                <li key={item.key}>
                  <NavLink
                    to={item.to}
                    data-onboarding-anchor={item.onboardingAnchor}
                    className={({ isActive }) => (isActive ? "active" : undefined)}
                  >
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>

            {/* MOTHERSHIP — super-admin only on mothership builds (T-0062).
                Tree-shaken out of detach bundles via the literal gate. */}
            {IS_MOTHERSHIP_BUILD && isSuperAdmin && (
              <>
                <div className="mc-sidebar-more-sub">mothership</div>
                <ul className="mc-sidebar-nav">
                  <li>
                    <NavLink
                      to="/m/users"
                      className={({ isActive }) => (isActive ? "active" : undefined)}
                    >
                      ALL USERS
                    </NavLink>
                  </li>
                  <li>
                    <NavLink
                      to="/"
                      end
                      className={({ isActive }) => (isActive ? "active" : undefined)}
                    >
                      ATTACHED SERVERS
                    </NavLink>
                  </li>
                  <li>
                    <NavLink
                      to="/m/servers/add"
                      className={({ isActive }) => (isActive ? "active" : undefined)}
                    >
                      + ADD SERVER
                    </NavLink>
                  </li>
                </ul>
              </>
            )}
          </div>
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
