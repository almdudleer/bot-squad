import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useParams, useLocation } from "react-router-dom";
import { api } from "../api";

/**
 * Shell — left sidebar navigation present on every authenticated page.
 * 240 px sticky sidebar: wordmark + worker LED, project info, nav links, system links, footer.
 */
export function Shell() {
  const { slug } = useParams<{ slug?: string }>();
  const location = useLocation();
  const [workerAlive, setWorkerAlive] = useState<boolean | null>(null);
  const [username, setUsername] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Determine if we are on a project-level route
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

  // Close sidebar on route change (mobile)
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  function handleSignOut() {
    api.logout().then(() => (window.location.href = "/login")).catch(() => (window.location.href = "/login"));
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

        {/* Project section — only when inside a project */}
        {hasProject && (
          <>
            <div className="mc-sidebar-section">Project</div>
            <div className="mc-sidebar-project">
              <div className="mc-sidebar-project-name">{slug}</div>
              <Link to="/" className="mc-sidebar-project-link">
                ← switch project
              </Link>
            </div>

            <div className="mc-sidebar-section">Nav</div>
            <ul className="mc-sidebar-nav">
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
                  VISION
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/feedback`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  FEEDBACK
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/sessions`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  SESSIONS
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/runs`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  RUNS
                </NavLink>
              </li>
              <li>
                <NavLink
                  to={`/p/${slug}/autonomous`}
                  className={({ isActive }) => (isActive ? "active" : undefined)}
                >
                  <span className="mc-nav-diamond">◆</span>
                  AUTONOMOUS
                </NavLink>
              </li>
            </ul>
          </>
        )}

        {/* System section */}
        <div className="mc-sidebar-section">System</div>
        <ul className="mc-sidebar-nav">
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
            <a href="/help" target="_blank" rel="noopener noreferrer">
              <span className="mc-nav-diamond">◇</span>
              HELP
            </a>
          </li>
        </ul>

        {/* Footer */}
        <div className="mc-sidebar-footer">
          {username && (
            <div className="mc-sidebar-user">{username} @ bot-squad</div>
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
