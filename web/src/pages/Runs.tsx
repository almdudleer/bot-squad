import { useEffect, useState, useCallback } from "react";
import { Link, useParams } from "react-router-dom";
import { api, RunRow } from "../api";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (isNaN(ts)) return "—";
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function duration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const s = Date.parse(start);
  const e = Date.parse(end);
  if (isNaN(s) || isNaN(e)) return "—";
  const diff = Math.floor((e - s) / 1000);
  if (diff < 0) return "—";
  if (diff < 60) return `${diff}s`;
  return `${Math.floor(diff / 60)}m ${diff % 60}s`;
}

type StatusBadgeProps = { status: RunRow["status"] };
function StatusBadge({ status }: StatusBadgeProps) {
  const map: Record<RunRow["status"], [string, string]> = {
    ok:         ["bg-success",   "ok"],
    fail:       ["bg-danger",    "fail"],
    processing: ["bg-warning text-dark", "processing"],
    queued:     ["bg-secondary", "queued"],
  };
  const [cls, label] = map[status] ?? ["bg-secondary", status];
  return <span className={`badge ${cls}`}>{label}</span>;
}

type TargetBadgeProps = { target: string };
function TargetBadge({ target }: TargetBadgeProps) {
  const cls = target === "staging" ? "bg-info text-dark"
            : target === "prod"    ? "bg-danger"
            : "bg-light text-dark border";
  return <span className={`badge ${cls}`}>{target}</span>;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Runs() {
  const { slug = "" } = useParams();
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const load = useCallback((off: number) => {
    api
      .runs(slug, limit, off)
      .then((rows) => {
        setRuns((prev) =>
          off === 0 ? rows : [...(prev ?? []), ...rows]
        );
        setError(null);
        setOffset(off);
      })
      .catch((e: unknown) => setError(String(e)));
  }, [slug]);

  useEffect(() => {
    load(0);
    const id = setInterval(() => load(0), 15_000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="container py-4">
      {/* Breadcrumb */}
      <nav className="mb-3 small">
        <Link to="/">← Projects</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}`}>Backlog</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/sessions`}>Sessions</Link>
        <span className="mx-2 text-muted">|</span>
        <strong>Runs</strong>
      </nav>

      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 className="mb-0">
          Deploy runs
          <span className="text-muted fw-normal fs-5 ms-2">/ {slug}</span>
        </h2>
        <span className="text-muted small" style={{ fontStyle: "italic" }}>
          <span
            className="spinner-border spinner-border-sm me-1 text-secondary"
            role="status"
            aria-hidden="true"
            style={{ width: "0.7rem", height: "0.7rem", borderWidth: "0.1em" }}
          />
          Auto-refreshing every 15s
        </span>
      </div>

      {/* Error */}
      {error && <div className="alert alert-danger">{error}</div>}

      {/* Loading */}
      {runs === null && !error && (
        <p className="text-muted">Loading runs…</p>
      )}

      {/* Empty state */}
      {runs !== null && runs.length === 0 && (
        <div className="text-center py-5">
          <p className="text-muted fs-5 mb-2">No deploy runs yet.</p>
          <p className="text-muted small">
            Runs appear here once a deploy is queued via a session.
          </p>
        </div>
      )}

      {/* Table */}
      {runs !== null && runs.length > 0 && (
        <>
          <div className="table-responsive">
            <table className="table table-hover table-sm align-middle">
              <thead className="table-light">
                <tr>
                  <th style={{ width: "9rem" }}>ID</th>
                  <th>Target</th>
                  <th>Status</th>
                  <th>Reason</th>
                  <th className="text-muted small">Requested by</th>
                  <th className="text-muted small">Queued</th>
                  <th className="text-muted small">Started</th>
                  <th className="text-muted small">Duration</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <code className="text-body" style={{ fontSize: "0.8rem" }}>
                        {r.id.slice(0, 8)}
                      </code>
                    </td>
                    <td><TargetBadge target={r.target} /></td>
                    <td><StatusBadge status={r.status} /></td>
                    <td
                      className="text-truncate"
                      style={{ maxWidth: "18rem" }}
                      title={r.reason}
                    >
                      {r.reason || <span className="text-muted">—</span>}
                    </td>
                    <td className="text-muted small">
                      <code style={{ fontSize: "0.75rem" }}>{r.requested_by || "—"}</code>
                    </td>
                    <td className="text-muted small">{relTime(r.queued_at)}</td>
                    <td className="text-muted small">{relTime(r.started_at)}</td>
                    <td className="text-muted small">{duration(r.started_at, r.ended_at)}</td>
                    <td>
                      <Link
                        to={`/p/${slug}/runs/${r.id}`}
                        className="btn btn-outline-secondary btn-sm"
                        style={{ fontSize: "0.75rem" }}
                      >
                        Log
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Load more */}
          {runs.length >= limit * Math.ceil((offset + 1) / limit) && (
            <div className="text-center mt-3">
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                onClick={() => load(runs.length)}
              >
                Load more
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
