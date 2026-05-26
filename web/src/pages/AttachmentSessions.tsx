/**
 * T-0061 — "my sessions on this server" view.
 *
 * No flat /api/sessions endpoint exists today (sessions are per-project),
 * so this page fans out over the project list and filters client-side.
 * The filter — ``owner == me.username`` or SID-prefix linux_user match —
 * matches the server-side ownership gate in ``api/app/routes_sessions.py``
 * so the displayed rows are exactly the ones the user has authority over.
 *
 * Cross-server scope is out: the project list comes from this server's
 * backend, so we don't have to look at the SERVER picker here. (When
 * cross-server session views land — T-0068 follow-up — this page can
 * grow a picker-aware fetch path; the filter helper stays the same.)
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Me, type Project, type SessionRow } from "../api";

export type SessionWithProject = SessionRow & { project: Project };

/**
 * Pure filter helper — exported for vitest. A session belongs to ``me``
 * iff its UI owner matches the username (T-0080 sessions), OR its SID
 * prefix's linux_user matches (legacy fallback for sessions spawned
 * before owner was tracked, mirroring _check_sid_ownership's logic).
 */
export function isMySession(
  row: SessionRow,
  me: Pick<Me, "username" | "linux_user">,
): boolean {
  if (row.owner && row.owner === me.username) return true;
  if (row.owner) return false;
  // Legacy fallback: parse `S-<linux_user>-<rest>` exactly the way the
  // backend does in WorkerRouter.user_for_sid (split-once on `-` after
  // the `S-` prefix). Matches the server-side _check_sid_ownership rule.
  if (!row.sid.startsWith("S-")) return false;
  const parts = row.sid.split("-", 3);
  if (parts.length < 3 || !parts[1]) return false;
  return parts[1] === me.linux_user;
}

export function AttachmentSessions() {
  const [me, setMe] = useState<Me | null>(null);
  const [rows, setRows] = useState<SessionWithProject[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const meRow = await api.me();
        if (cancelled) return;
        setMe(meRow);
        const projects = await api.projects();
        if (cancelled) return;
        const out: SessionWithProject[] = [];
        for (const p of projects) {
          try {
            const sessions = await api.sessions(p.slug);
            for (const s of sessions) {
              if (isMySession(s, meRow)) {
                out.push({ ...s, project: p });
              }
            }
          } catch {
            // A single project's session list failing shouldn't blank the
            // whole page — the user often has more projects on this server
            // than on the failing one.
            continue;
          }
        }
        if (!cancelled) setRows(out);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="container py-4">
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        My sessions
      </h2>
      <p
        style={{
          fontSize: "0.75rem",
          color: "var(--mc-text-dim)",
          marginBottom: "1rem",
        }}
      >
        Sessions owned by{" "}
        {me ? (
          <code>
            {me.username}
            {me.linux_user !== me.username ? ` (${me.linux_user})` : ""}
          </code>
        ) : (
          "you"
        )}{" "}
        across all projects on this server.
      </p>

      {error && <div className="alert alert-danger">{error}</div>}
      {!error && rows === null && <div className="mc-loading">Loading</div>}

      {rows !== null && rows.length === 0 && (
        <div style={{ fontSize: "0.85rem", color: "var(--mc-text-dim)" }}>
          No sessions found.
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <table className="table" style={{ fontSize: "0.85rem" }}>
          <thead>
            <tr>
              <th>SID</th>
              <th>Project</th>
              <th>Window</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.project.slug}:${r.sid}`}>
                <td>
                  <Link to={`/p/${r.project.slug}/sessions`}>
                    <code>{r.sid}</code>
                  </Link>
                </td>
                <td>{r.project.display_name}</td>
                <td>
                  <code>{r.window}</code>
                </td>
                <td>{r.activity ?? r.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
