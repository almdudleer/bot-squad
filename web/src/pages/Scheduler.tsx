import { useEffect, useState, useCallback } from "react";
import { PageHelp } from "../components/PageHelp";
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
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-1">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>Scheduler</h2>
        <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
          auto-refresh 30s
          {lastRefresh && (
            <span style={{ marginLeft: "0.5rem" }}>
              · {lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            </span>
          )}
        </span>
      </div>

      <PageHelp>
        Time-driven jobs the worker runs (heartbeat, deploy_monitor,
        oauth_refresh, tg_listener).
        Shows worker uptime, last heartbeat, and each job&apos;s next fire time.
      </PageHelp>

      {/* Error */}
      {error && <div className="alert alert-danger">{error}</div>}

      {/* Loading */}
      {state === null && !error && (
        <div className="mc-loading">Loading scheduler state</div>
      )}

      {state !== null && (
        <>
          {/* Status cards */}
          <div className="row g-3 mb-4">
            <div className="col-sm-6 col-md-4">
              <div className="card h-100">
                <div className="card-body">
                  <div className="card-subtitle mb-2">Worker uptime</div>
                  <div className="card-text fs-4 fw-semibold">
                    {uptimeStr(state.worker_started_at)}
                  </div>
                  {state.worker_started_at && (
                    <div style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)", marginTop: "0.25rem" }}>
                      started {relTime(state.worker_started_at)}
                    </div>
                  )}
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-4">
              <div className="card h-100">
                <div className="card-body">
                  <div className="card-subtitle mb-2">Last heartbeat</div>
                  <div className="card-text fs-4 fw-semibold">
                    {heartbeatAge !== null ? `${Math.floor(heartbeatAge)}s ago` : "—"}
                  </div>
                  <div style={{ marginTop: "0.3rem", display: "flex", alignItems: "center", gap: "0.35rem" }}>
                    <span className={heartbeatOk ? "mc-dot mc-dot-active" : "mc-dot mc-dot-error"} />
                    <span className={`mc-badge ${heartbeatOk ? "mc-badge-ok" : "mc-badge-danger"}`}>
                      {heartbeatOk ? "healthy" : "stale"}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-4">
              <div className="card h-100">
                <div className="card-body">
                  <div className="card-subtitle mb-2">Jobs registered</div>
                  <div className="card-text fs-4 fw-semibold">
                    {state.jobs.length}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Jobs table */}
          <div className="mc-section-title mb-2">Scheduled jobs</div>
          {state.jobs.length === 0 ? (
            <div className="mc-empty">
              <div className="mc-empty-icon">◯</div>
              <div>No jobs scheduled.</div>
            </div>
          ) : (
            <div className="table-responsive">
              <table className="table table-hover table-sm align-middle">
                <thead>
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
                        <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-mid)" }}>
                          {job.id}
                        </code>
                      </td>
                      <td>
                        <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
                          {job.trigger}
                        </span>
                      </td>
                      <td>
                        {job.next_run ? (
                          <span
                            title={job.next_run}
                            style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}
                          >
                            {futureTime(job.next_run)}
                          </span>
                        ) : (
                          <span style={{ color: "var(--mc-text-dim)" }}>—</span>
                        )}
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
