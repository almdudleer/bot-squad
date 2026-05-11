import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, RunRow } from "../api";

export function RunLog() {
  const { slug = "", id = "" } = useParams();
  const [log, setLog] = useState<string | null>(null);
  const [run, setRun] = useState<RunRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingFull, setLoadingFull] = useState(false);
  const [isFull, setIsFull] = useState(false);

  useEffect(() => {
    api.runs(slug, 500).then((rows) => {
      const found = rows.find((r) => r.id === id) ?? null;
      setRun(found);
    }).catch(() => {/* best-effort */});

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

  const dur = (() => {
    if (!run?.started_at || !run?.ended_at) return null;
    const s = Date.parse(run.started_at);
    const e = Date.parse(run.ended_at);
    if (isNaN(s) || isNaN(e)) return null;
    const diff = Math.floor((e - s) / 1000);
    if (diff < 0) return null;
    if (diff < 60) return `${diff}s`;
    return `${Math.floor(diff / 60)}m ${diff % 60}s`;
  })();

  const statusCls = !run ? "" :
    run.status === "ok" ? "mc-badge-ok" :
    run.status === "fail" ? "mc-badge-danger" :
    run.status === "processing" ? "mc-badge-warn" :
    "mc-badge-dim";

  const targetCls = !run ? "" :
    run.target === "staging" ? "mc-badge-info" :
    run.target === "prod" ? "mc-badge-danger" :
    "mc-badge-dim";

  return (
    <div className="container-fluid py-4">
      {/* Breadcrumb */}
      <nav className="mc-breadcrumb">
        <Link to="/">Projects</Link>
        <span className="mc-bc-sep">/</span>
        <Link to={`/p/${slug}`}>{slug}</Link>
        <span className="mc-bc-sep">/</span>
        <Link to={`/p/${slug}/runs`}>Runs</Link>
        <span className="mc-bc-sep">/</span>
        <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.75rem", color: "var(--mc-text-mid)" }}>
          {id.slice(0, 8)}
        </code>
      </nav>

      {/* Header */}
      <div className="d-flex flex-wrap gap-2 align-items-center mb-3">
        <code
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.82rem",
            color: "var(--mc-text-mid)",
          }}
        >
          {id}
        </code>
        {run && (
          <>
            <span className={`mc-badge ${targetCls}`}>{run.target}</span>
            <span className={`mc-badge ${statusCls}`}>{run.status}</span>
            {dur && (
              <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.75rem", color: "var(--mc-text-dim)" }}>
                {dur}
              </span>
            )}
          </>
        )}
      </div>

      {/* Error */}
      {error && <div className="alert alert-danger">{error}</div>}

      {/* Log not loaded yet */}
      {log === null && !error && (
        <div className="mc-loading">Loading log</div>
      )}

      {/* Log content */}
      {log !== null && (
        <>
          {!isFull && log.length >= 2 * 1024 * 1024 - 100 && (
            <div
              className="alert alert-warning d-flex justify-content-between align-items-center py-2 px-3 mb-2"
              style={{ fontSize: "0.82rem" }}
            >
              <span>Log truncated to 2 MB.</span>
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
          <pre className="mc-log">{log || "(empty log)"}</pre>
        </>
      )}
    </div>
  );
}
