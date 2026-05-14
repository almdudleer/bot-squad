import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useParams, useLocation } from "react-router-dom";
import { api } from "../api";
import { ProjectSwitcher } from "./ProjectSwitcher";

const PINNED_PROJECT_KEY = "bot-squad:last-project";

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
  const [workerAlive, setWorkerAlive] = useState<boolean | null>(null);
  const [username, setUsername] = useState<string | null>(null);
  const [linuxUser, setLinuxUser] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState<boolean>(false);
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

  // Poll worker health every 30 s
  useEffect(() => {
    let cancelled = false;
    function checkHealth() {
      api
        .health()
        .then((h) => {
          if (cancelled) return;
          const alive = !!(h as Record<string, unknown>)?.ok;
          setWorkerAlive(alive);
          const u = (h as Record<string, unknown>)?.username;
          if (typeof u === "string") setUsername(u);
        })
        .catch(() => {
          if (!cancelled) setWorkerAlive(false);
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

  const dotCls =
    workerAlive === null
      ? "mc-dot mc-dot-idle"
      : workerAlive
      ? "mc-dot mc-dot-active"
      : "mc-dot mc-dot-error";

  const workerLabel =
    workerAlive === null ? "UNKNOWN" : workerAlive ? "OPERATIONAL" : "WORKER OFFLINE";

  const workerLabelColor =
    workerAlive === null
      ? "var(--mc-text-faint)"
      : workerAlive
      ? "var(--mc-green)"
      : "var(--mc-red)";

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

        {/* Header strip */}
        <div className="mc-sidebar-header">
          <Link to="/" className="mc-wordmark">BOT·SQUAD</Link>
          <div className="mc-worker-status">
            <span className={dotCls} />
            <span style={{ color: workerLabelColor }}>{workerLabel}</span>
          </div>
        </div>

        {/* Project section — shows pinned project even on global routes */}
        {hasProject && slug && (
          <>
            <div className="mc-sidebar-section">Project</div>
            <div className="mc-sidebar-project">
              <div className="mc-sidebar-project-name">{slug}</div>
              <ProjectSwitcher slug={slug} onUnpin={unpinProject} />
            </div>

            <ul className="mc-sidebar-nav">
              {/* Management — planning content */}
              <li>
                <NavLink
                  to={`/p/${slug}`}
                  end
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  BOARD
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/vision`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  ROADMAP
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/feedback`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  USER FEEDBACK
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/workflow`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  WORKFLOW
                </NavLink>
              </li>
            </ul>

            {/* Separator between management (above) and execution (below) */}
            <div className="mc-sidebar-divider" aria-hidden="true" />

            <ul className="mc-sidebar-nav">
              {/* Execution — running things */}
              <li>
                <NavLink
                  to={`/p/${slug}/sessions`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">▸</span>
                  AGENT SESSIONS
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/runs`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">▸</span>
                  DEPLOYMENT QUEUE
                </NavLink>
              </li>
              {/* AUTONOMOUS TEAM — link hidden 2026-05-12, autonomous work frozen.
                  Route still exists; restore this <li> when re-enabling. */}
            </ul>
          </>
        )}

        {/* System section */}
        <div className="mc-sidebar-section">System</div>
        <ul className="mc-sidebar-nav">
          <li>
            <NavLink
              to="/"
              end
              data-onboarding-anchor="all-projects-nav"
              className={({ isActive }) => (isActive ? "active" : undefined)}
            >
              <span className="mc-nav-diamond">◇</span>
              ALL PROJECTS
            </NavLink>
          </li>
          <li>
            <NavLink
              to="/scheduler"
              className={({ isActive }) => (isActive ? "active" : undefined)}
            >
              <span className="mc-nav-diamond">◇</span>
              SCHEDULER
            </NavLink>
          </li>
          <li>
            <NavLink
              to="/me"
              className={({ isActive }) => (isActive ? "active" : undefined)}
            >
              <span className="mc-nav-diamond">◇</span>
              MY PROFILE
            </NavLink>
          </li>
          <li>
            <NavLink
              to="/help"
              data-onboarding-anchor="help-nav"
              className={({ isActive }) => (isActive ? "active" : undefined)}
            >
              <span className="mc-nav-diamond">◇</span>
              HELP
            </NavLink>
          </li>
          {/* Mothership-only — vision/architecture/mothership-seam.md sidebar nav. */}
          {import.meta.env.VITE_MOTHERSHIP === "1" && (
            <li>
              <NavLink
                to="/m/servers/add"
                className={({ isActive }) => (isActive ? "active" : undefined)}
              >
                <span className="mc-nav-diamond">◇</span>
                SERVERS
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
                  <span className="mc-nav-diamond">◇</span>
                  USERS
                </NavLink>
              </li>
              <li>
                <NavLink
                  to="/system-settings"
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◇</span>
                  SETTINGS
                </NavLink>
              </li>
            </>
          )}
        </ul>

        {/* Footer */}
        <div className="mc-sidebar-footer">
          {username && (
            <div className="mc-sidebar-user">
              {username}
              {linuxUser && linuxUser !== username ? ` (${linuxUser})` : ""}
              {" "}@ bot-squad
            </div>
          )}
          <button
            type="button"
            className="mc-sidebar-signout"
            onClick={handleSignOut}
          >
            Sign out
          </button>
        </div>
      </nav>

      {/* ── Main content ──────────────────────────────────── */}
      <main className="mc-main">
        <Outlet />
      </main>
    </div>
  );
}
