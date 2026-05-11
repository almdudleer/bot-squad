import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, RunRow } from "../api";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function RunLog() {
  const { slug = "", id = "" } = useParams();
  const [log, setLog] = useState<string | null>(null);
  const [run, setRun] = useState<RunRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingFull, setLoadingFull] = useState(false);
  const [isFull, setIsFull] = useState(false);

  useEffect(() => {
    // Load run metadata (from runs list, filter by id)
    api.runs(slug, 500).then((rows) => {
      const found = rows.find((r) => r.id === id) ?? null;
      setRun(found);
    }).catch(() => {/* best-effort */});

    // Load log content
    api.runLog(slug, id)
      .then((text) => setLog(text))
      .catch((e: unknown) => setError(String(e)));
  }, [slug, id]);

  async function loadFull() {
    setLoadingFull(true);
    try {
      const text = await api.runLog(slug, id, true);
      setLog(text);
      setIsFull(true);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoadingFull(false);
    }
  }

  const duration = (() => {
    if (!run?.started_at || !run?.ended_at) return null;
    const s = Date.parse(run.started_at);
    const e = Date.parse(run.ended_at);
    if (isNaN(s) || isNaN(e)) return null;
    const diff = Math.floor((e - s) / 1000);
    if (diff < 0) return null;
    if (diff < 60) return `${diff}s`;
    return `${Math.floor(diff / 60)}m ${diff % 60}s`;
  })();

  return (
    <div className="container-fluid py-4">
      {/* Breadcrumb */}
      <nav className="mb-3 small">
        <Link to="/">← Projects</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}`}>Backlog</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/runs`}>Runs</Link>
        <span className="mx-2 text-muted">|</span>
        <code style={{ fontSize: "0.8rem" }}>{id.slice(0, 8)}</code>
      </nav>

      {/* Header */}
      <div className="d-flex flex-wrap gap-3 align-items-center mb-3">
        <h2 className="mb-0" style={{ fontFamily: "monospace", fontSize: "1.1rem" }}>
          {id}
        </h2>
        {run && (
          <>
            <span className={`badge ${
              run.target === "staging" ? "bg-info text-dark"
              : run.target === "prod" ? "bg-danger"
              : "bg-light text-dark border"
            }`}>{run.target}</span>
            <span className={`badge ${
              run.status === "ok" ? "bg-success"
              : run.status === "fail" ? "bg-danger"
              : run.status === "processing" ? "bg-warning text-dark"
              : "bg-secondary"
            }`}>{run.status}</span>
            {duration && (
              <span className="text-muted small">Duration: {duration}</span>
            )}
          </>
        )}
      </div>

      {/* Error */}
      {error && <div className="alert alert-danger">{error}</div>}

      {/* Log not loaded yet */}
      {log === null && !error && (
        <p className="text-muted">Loading log…</p>
      )}

      {/* Log content */}
      {log !== null && (
        <>
          {!isFull && log.length >= 2 * 1024 * 1024 - 100 && (
            <div className="alert alert-warning d-flex justify-content-between align-items-center py-2 px-3 mb-2" style={{ fontSize: "0.85rem" }}>
              <span>Log is truncated to 2 MB.</span>
              <button
                type="button"
                className="btn btn-sm btn-outline-warning ms-3"
                onClick={loadFull}
                disabled={loadingFull}
              >
                {loadingFull ? "Loading…" : "Load full log"}
              </button>
            </div>
          )}
          <pre
            className="bg-dark text-light rounded p-3"
            style={{
              fontSize: "0.8rem",
              maxHeight: "80vh",
              overflowY: "auto",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            }}
          >{log || "(empty log)"}</pre>
        </>
      )}
    </div>
  );
}
