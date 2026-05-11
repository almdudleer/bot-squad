import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { api, SchedulerState } from "../api";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (isNaN(ts)) return "—";
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 0) return `in ${Math.abs(diff)}s`;
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function futureTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (isNaN(ts)) return "—";
  const diff = Math.floor((ts - Date.now()) / 1000);
  if (diff < 0) return "now";
  if (diff < 60) return `in ${diff}s`;
  if (diff < 3600) return `in ${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `in ${Math.floor(diff / 3600)}h`;
  return `in ${Math.floor(diff / 86400)}d`;
}

function uptimeStr(startedAt: string | null): string {
  if (!startedAt) return "—";
  const diff = Math.floor((Date.now() - Date.parse(startedAt)) / 1000);
  if (isNaN(diff) || diff < 0) return "—";
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = diff % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Scheduler() {
  const [state, setState] = useState<SchedulerState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const load = useCallback(() => {
    api
      .scheduler()
      .then((s) => {
        setState(s);
        setLastRefresh(new Date());
        setError(null);
      })
      .catch((e: unknown) => setError(String(e)));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [load]);

  const heartbeatAge = state?.last_heartbeat_age_seconds ?? null;
  const heartbeatOk = heartbeatAge !== null && heartbeatAge < 120;

  return (
    <div className="container py-4">
      {/* Breadcrumb / nav */}
      <nav className="mb-3 small">
        <Link to="/">← Projects</Link>
        <span className="mx-2 text-muted">|</span>
        <strong>Scheduler</strong>
      </nav>

      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h2 className="mb-0">Scheduler dashboard</h2>
        <span className="text-muted small" style={{ fontStyle: "italic" }}>
          <span
            className="spinner-border spinner-border-sm me-1 text-secondary"
            role="status"
            aria-hidden="true"
            style={{ width: "0.7rem", height: "0.7rem", borderWidth: "0.1em" }}
          />
          Auto-refreshing every 30s
          {lastRefresh && (
            <span className="ms-2 text-muted">
              · last at {lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            </span>
          )}
        </span>
      </div>

      {/* Error */}
      {error && <div className="alert alert-danger">{error}</div>}

      {/* Loading */}
      {state === null && !error && (
        <p className="text-muted">Loading scheduler state…</p>
      )}

      {state !== null && (
        <>
          {/* Worker status cards */}
          <div className="row g-3 mb-4">
            <div className="col-sm-6 col-md-4">
              <div className="card h-100">
                <div className="card-body">
                  <h6 className="card-subtitle text-muted mb-1">Worker uptime</h6>
                  <p className="card-text fs-4 fw-semibold mb-0">
                    {uptimeStr(state.worker_started_at)}
                  </p>
                  {state.worker_started_at && (
                    <small className="text-muted">
                      started {relTime(state.worker_started_at)}
                    </small>
                  )}
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-4">
              <div className="card h-100">
                <div className="card-body">
                  <h6 className="card-subtitle text-muted mb-1">Last heartbeat</h6>
                  <p className="card-text fs-4 fw-semibold mb-0">
                    {heartbeatAge !== null
                      ? `${Math.floor(heartbeatAge)}s ago`
                      : "—"}
                  </p>
                  <span
                    className={`badge ${heartbeatOk ? "bg-success" : "bg-danger"}`}
                    style={{ fontSize: "0.7rem" }}
                  >
                    {heartbeatOk ? "healthy" : "stale"}
                  </span>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-4">
              <div className="card h-100">
                <div className="card-body">
                  <h6 className="card-subtitle text-muted mb-1">Jobs registered</h6>
                  <p className="card-text fs-4 fw-semibold mb-0">
                    {state.jobs.length}
                  </p>
                </div>
              </div>
            </div>
          </div>

          {/* Jobs table */}
          <h5 className="mb-3">Scheduled jobs</h5>
          {state.jobs.length === 0 ? (
            <p className="text-muted">No jobs scheduled.</p>
          ) : (
            <div className="table-responsive">
              <table className="table table-hover table-sm align-middle">
                <thead className="table-light">
                  <tr>
                    <th>Job ID</th>
                    <th>Trigger</th>
                    <th>Next run</th>
                  </tr>
                </thead>
                <tbody>
                  {state.jobs.map((job) => (
                    <tr key={job.id}>
                      <td>
                        <code style={{ fontSize: "0.85rem" }}>{job.id}</code>
                      </td>
                      <td>
                        <span className="text-muted" style={{ fontSize: "0.85rem", fontFamily: "monospace" }}>
                          {job.trigger}
                        </span>
                      </td>
                      <td className="text-muted" style={{ fontSize: "0.85rem" }}>
                        {job.next_run ? (
                          <span title={job.next_run}>
                            {futureTime(job.next_run)}
                          </span>
                        ) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
