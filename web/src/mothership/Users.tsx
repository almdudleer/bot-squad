import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { mothershipApi, type GlobalUser, type AttachedServer } from "./api";
import { api } from "../api";
import { isSuperAdminFromMe } from "../components/sidebarHelpers";

/**
 * T-0113 — MOTHERSHIP /m/users page. Super-admin-only directory of every
 * GlobalUser the mothership knows about. Currently driven by /api/m/users
 * (T-0066 cookie-auth GET); the BE returns an empty list until a user is
 * minted into the registry (either by /api/auth/attach or the migration
 * recipe). Empty + 403 states are explicit so the page reads cleanly when
 * the registry is empty *or* the viewer lacks the scope.
 *
 * attached-server count is exposed by the FE shape but the BE doesn't fill
 * it yet — tracked in T-0129. Until then the column renders em-dash via
 * ``attachedServerCountText``.
 */

// Lightweight discriminated union the renderer reads. Keeping the load
// state as data (not three booleans) means the render path is a single
// switch, which makes the "page renders for each state" tests trivial.
export type UsersLoadState =
  | { kind: "loading" }
  | { kind: "denied" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; users: GlobalUser[] };

export function attachedServerCountText(u: GlobalUser): string {
  // BE doesn't populate this yet (T-0129). The undefined branch is the
  // common one until the BE follow-on lands; the numeric branch is
  // forward-compatible so the FE doesn't need a redeploy after the BE
  // upgrade.
  if (typeof u.attached_servers === "number") return String(u.attached_servers);
  return "—";
}

export function isAccessDeniedError(err: unknown): boolean {
  // call() wraps non-OK responses as ``API error <status>: <body>``. We
  // key off the status code (not the body text) so a BE message change
  // doesn't silently break gating.
  if (err instanceof Error) {
    return /^API error 403\b/.test(err.message);
  }
  return false;
}

export function Users() {
  const [state, setState] = useState<UsersLoadState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me = await api.getMyProfile();
        if (cancelled) return;
        if (!isSuperAdminFromMe(me)) {
          setState({ kind: "denied" });
          return;
        }
        const users = await mothershipApi.listUsers();
        if (cancelled) return;
        setState({ kind: "loaded", users });
      } catch (err) {
        if (cancelled) return;
        // A 403 from /api/m/users (super-admin gate on the BE) ends up
        // here even when the FE-side check passes — guards against the
        // is_super_admin/is_admin fallback drifting between client and
        // server.
        if (isAccessDeniedError(err)) {
          setState({ kind: "denied" });
          return;
        }
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="container py-4" style={{ maxWidth: 960 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: "1rem",
          marginBottom: "1rem",
        }}
      >
        <div className="mc-section-title">Mothership</div>
        {/* T-0170: the sidebar's three mothership rows collapsed to one
            "Mothership" entry → this page. The add-server / invite flow
            (formerly the "+ ADD SERVER" row) is reachable from here. */}
        <Link to="/m/servers/add" style={{ fontSize: "0.8rem", textDecoration: "none" }}>
          + Add a server / invite →
        </Link>
      </div>

      <div
        style={{
          fontSize: "0.72rem",
          textTransform: "uppercase",
          letterSpacing: "0.1em",
          color: "var(--mc-text-faint)",
          marginBottom: "0.4rem",
        }}
      >
        Global users
      </div>
      <UsersBody state={state} />

      {/* T-0170: connected-servers list — distinct from the "All Projects"
          user view. This is the mothership-admin roster of servers attached
          to botsquad.dev (not necessarily the viewer's own). */}
      <ConnectedServers />
    </div>
  );
}

type ServersLoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; servers: AttachedServer[] };

function ConnectedServers() {
  const [state, setState] = useState<ServersLoadState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    mothershipApi
      .listServers()
      .then((servers) => {
        if (!cancelled) setState({ kind: "loaded", servers });
      })
      .catch((err) => {
        if (!cancelled)
          setState({
            kind: "error",
            message: err instanceof Error ? err.message : String(err),
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section style={{ marginTop: "2.5rem" }}>
      <div
        style={{
          fontSize: "0.72rem",
          textTransform: "uppercase",
          letterSpacing: "0.1em",
          color: "var(--mc-text-faint)",
          marginBottom: "0.4rem",
        }}
      >
        Connected servers
      </div>
      {state.kind === "loading" && <div className="mc-loading">Loading</div>}
      {state.kind === "error" && (
        <div className="alert alert-danger">{state.message}</div>
      )}
      {state.kind === "loaded" && state.servers.length === 0 && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          <div>No servers connected yet.</div>
        </div>
      )}
      {state.kind === "loaded" && state.servers.length > 0 && (
        <table className="table" style={{ fontSize: "0.85rem" }}>
          <thead>
            <tr>
              <th>server</th>
              <th>base url</th>
              <th>owner</th>
              <th>state</th>
              <th>last seen</th>
            </tr>
          </thead>
          <tbody>
            {state.servers.map((s) => (
              <tr key={s.id}>
                <td>
                  {s.display_name}
                  {s.is_self && (
                    <span className="mc-badge mc-badge-info" style={{ marginLeft: "0.4rem" }}>
                      self
                    </span>
                  )}
                </td>
                <td style={{ fontFamily: "var(--mc-mono)" }}>{s.base_url}</td>
                <td style={{ fontFamily: "var(--mc-mono)" }}>{s.owner_user}</td>
                <td>
                  <span
                    className={
                      s.install_state === "ready" || s.install_state === "connected"
                        ? "mc-badge mc-badge-ok"
                        : s.install_state === "failed"
                          ? "mc-badge mc-badge-danger"
                          : "mc-badge mc-badge-active"
                    }
                  >
                    {s.install_state}
                  </span>
                </td>
                <td style={{ color: "var(--mc-text-dim)" }}>{s.last_seen_at ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function UsersBody({ state }: { state: UsersLoadState }) {
  if (state.kind === "loading") {
    return <div className="mc-loading" data-testid="users-loading">Loading</div>;
  }
  if (state.kind === "denied") {
    return (
      <div className="mc-empty" data-testid="users-denied">
        <div className="mc-empty-icon">◇</div>
        <div>Super-admin only.</div>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="alert alert-danger" data-testid="users-error">
        {state.message}
      </div>
    );
  }
  if (state.users.length === 0) {
    return (
      <div className="mc-empty" data-testid="users-empty">
        <div className="mc-empty-icon">◯</div>
        <div>No global users yet — first attached user becomes the seed.</div>
      </div>
    );
  }
  return <UsersTable users={state.users} />;
}

function UsersTable({ users }: { users: GlobalUser[] }) {
  return (
    <table
      className="table"
      data-testid="users-table"
      style={{ fontSize: "0.85rem" }}
    >
      <thead>
        <tr>
          <th>username</th>
          <th>display name</th>
          <th>email</th>
          <th>super-admin</th>
          <th>attached servers</th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => (
          <tr key={u.id} data-testid={`user-row-${u.username}`}>
            <td style={{ fontFamily: "var(--mc-mono)" }}>{u.username}</td>
            <td>{u.display_name || <span style={{ color: "var(--mc-text-dim)" }}>—</span>}</td>
            <td>{u.email || <span style={{ color: "var(--mc-text-dim)" }}>—</span>}</td>
            <td>
              {u.is_super_admin ? (
                <span className="mc-badge mc-badge-info">yes</span>
              ) : (
                <span style={{ color: "var(--mc-text-dim)" }}>no</span>
              )}
            </td>
            <td>{attachedServerCountText(u)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default Users;
