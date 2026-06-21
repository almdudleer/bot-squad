import { useEffect, useState, useCallback } from "react";
import { PageHelp } from "../components/PageHelp";
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

// T-0362: requested_by is stored as the raw requester SID
// (S-<user>-<window>-p<N>). Humanize it to "<window> (<user>)" for the table;
// the full SID stays in the cell's title tooltip. Non-SID values pass through.
export function humanizeRequester(raw: string): string {
  if (!raw) return "—";
  const m = raw.match(/^S-([^-]+)-(.+)-p\d+$/);
  if (!m) return raw;
  const [, user, window] = m;
  return `${window} (${user})`;
}

function StatusBadge({ status }: { status: RunRow["status"] }) {
  const map: Record<RunRow["status"], string> = {
    ok:         "mc-badge-ok",
    fail:       "mc-badge-danger",
    processing: "mc-badge-warn",
    queued:     "mc-badge-dim",
  };
  return (
    <span className={`mc-badge ${map[status] ?? "mc-badge-dim"}`}>
      {status}
    </span>
  );
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
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
          Deployment queue
          <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
        </h2>
        <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
          auto-refresh 15s
        </span>
      </div>
      <PageHelp>
        Deploy history for this project. Agents queue a deploy with
        <code> ops/bot-squad-bin/deploy &lt;target&gt; &quot;&lt;reason&gt;&quot;</code>;
        the worker&apos;s <code>deploy_monitor</code> fires it when the repo tree is clean.
        Click any row for the full stdout/stderr.
      </PageHelp>

      {/* Error */}
      {error && <div className="alert alert-danger">{error}</div>}

      {/* Loading */}
      {runs === null && !error && (
        <div className="mc-loading">Loading runs</div>
      )}

      {/* Empty state */}
      {runs !== null && runs.length === 0 && (
        <div className="mc-empty">
          <div className="mc-empty-icon">▢</div>
          <div>No deploy runs yet.</div>
          <div style={{ fontSize: "0.75rem", marginTop: "0.4rem", color: "var(--mc-text-dim)" }}>
            Queue a deploy via <code>ops/bot-squad-bin/deploy &lt;target&gt; "&lt;reason&gt;"</code>
          </div>
        </div>
      )}

      {/* Table */}
      {runs !== null && runs.length > 0 && (
        <>
          <div className="table-responsive">
            <table className="table table-hover table-sm align-middle">
              <thead>
                <tr>
                  {/* T-0362: cut dead/dup columns — Target (constant: this
                      install deploys staging-only), Started (an mtime proxy
                      that duplicated Queued), and Duration (no precise start is
                      stored, so it was always "—"/bogus). Full detail lives in
                      the per-run log. */}
                  <th style={{ width: "6rem" }}>ID</th>
                  <th>Status</th>
                  <th>Reason</th>
                  <th>Requested by</th>
                  <th>Queued</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} style={r.status === "ok" ? { opacity: 0.55 } : undefined}>
                    <td>
                      <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.75rem", color: "var(--mc-text-mid)" }}>
                        {r.id.slice(0, 8)}
                      </code>
                    </td>
                    <td><StatusBadge status={r.status} /></td>
                    <td
                      className="text-truncate"
                      style={{ maxWidth: "18rem", fontSize: "0.83rem" }}
                      title={r.reason}
                    >
                      {r.reason || <span style={{ color: "var(--mc-text-dim)" }}>—</span>}
                    </td>
                    <td
                      style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}
                      title={r.requested_by || undefined}
                    >
                      {humanizeRequester(r.requested_by)}
                    </td>
                    <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.75rem", color: "var(--mc-text-dim)" }}>
                      {relTime(r.queued_at)}
                    </td>
                    <td>
                      <Link
                        to={`/p/${slug}/runs/${r.id}`}
                        className="mc-badge mc-badge-dim"
                        style={{ textDecoration: "none" }}
                      >
                        log
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
