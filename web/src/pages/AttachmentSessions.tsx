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
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Me, type Project, type SessionRow } from "../api";
// T-0319: reuse the canonical live-only predicate from the main Sessions page
// so "My sessions" hides suspended/dead rows by default, matching the
// product-wide live-only model (T-0232). Imported (not duplicated) so the two
// views can never drift on what counts as "live".
import { isLiveSession } from "./Sessions";

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
  // T-0319: live-only by default; the toggle reveals suspended/archived rows
  // on demand (mirrors the main Sessions page "show suspended" toggle).
  const [showSuspended, setShowSuspended] = useState(false);

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

  // T-0319: the table renders LIVE rows (running/idle/paused in tmux) by
  // default; suspended/archived rows are kept in `rows` but dropped from the
  // view unless `showSuspended` is on. `isLiveSession` is the same predicate
  // the per-project Sessions board uses, so the two views agree on liveness.
  const liveRows = useMemo(
    () => (rows ?? []).filter((r) => isLiveSession(r)),
    [rows],
  );
  const hiddenSuspendedCount = (rows?.length ?? 0) - liveRows.length;
  const displayRows: SessionWithProject[] | null =
    rows === null ? null : showSuspended ? rows : liveRows;

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

      {/* T-0319: live-only by default. When suspended/archived rows are being
          hidden, surface a subtle count + toggle so they stay reachable
          without re-cluttering the view — matching the Sessions page. */}
      {rows !== null && (hiddenSuspendedCount > 0 || showSuspended) && (
        <div style={{ marginBottom: "0.75rem" }}>
          <button
            type="button"
            className={`btn btn-sm ${showSuspended ? "btn-secondary" : "btn-outline-secondary"}`}
            style={{ fontSize: "0.72rem", padding: "0.15rem 0.55rem" }}
            onClick={() => setShowSuspended((v) => !v)}
            title={
              showSuspended
                ? "Hide suspended sessions (show live only)"
                : "Reveal suspended (non-live) sessions"
            }
          >
            {showSuspended
              ? "hide suspended"
              : `${hiddenSuspendedCount} suspended hidden — show`}
          </button>
        </div>
      )}

      {displayRows !== null && displayRows.length === 0 && (
        <div style={{ fontSize: "0.85rem", color: "var(--mc-text-dim)" }}>
          {!showSuspended && hiddenSuspendedCount > 0
            ? "No live sessions. Use “show” above to reveal suspended ones."
            : "No sessions found."}
        </div>
      )}

      {displayRows !== null && displayRows.length > 0 && (
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
            {displayRows.map((r) => (
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
