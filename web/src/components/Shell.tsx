import { useEffect, useState } from "react";
import { Link, Outlet, useParams } from "react-router-dom";
import { api } from "../api";

/**
 * Shell — sticky top header bar present on every authenticated page.
 * Renders: BOT-SQUAD wordmark + worker status dot | project name | user + sign-out + help
 */
export function Shell() {
  const { slug } = useParams<{ slug?: string }>();
  const [workerAlive, setWorkerAlive] = useState<boolean | null>(null);
  const [username, setUsername] = useState<string | null>(null);

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
          // Extract username if returned
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

  function handleSignOut() {
    api.logout().then(() => (window.location.href = "/login")).catch(() => (window.location.href = "/login"));
  }

  const dotCls =
    workerAlive === null ? "mc-dot mc-dot-idle" : workerAlive ? "mc-dot mc-dot-active" : "mc-dot mc-dot-error";

  return (
    <>
      <header className="mc-shell-header">
        {/* Wordmark */}
        <Link to="/" className="mc-wordmark">
          <span className={dotCls} title={workerAlive === null ? "unknown" : workerAlive ? "worker alive" : "worker offline"} />
          BOT-SQUAD
        </Link>

        {/* Project name (center) */}
        <div className="mc-shell-project">
          {slug && (
            <>
              <span style={{ color: "var(--mc-border-mid)", marginRight: "0.35rem" }}>/</span>
              <span>{slug}</span>
            </>
          )}
        </div>

        {/* Right side */}
        <div className="mc-shell-right">
          {username && <span className="mc-shell-username">{username}</span>}
          <Link to="/help">help</Link>
          <span className="mc-shell-sep">·</span>
          <button
            type="button"
            onClick={handleSignOut}
            style={{
              background: "none",
              border: "none",
              padding: 0,
              cursor: "pointer",
              color: "var(--mc-text-dim)",
              fontSize: "0.78rem",
              fontFamily: "var(--mc-sans)",
            }}
          >
            sign out
          </button>
        </div>
      </header>

      <Outlet />
    </>
  );
}
