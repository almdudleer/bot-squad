import { useEffect, useMemo, useState } from "react";
import { Routes, Route, Link, useParams } from "react-router-dom";
import { type Checkpoint } from "./api";
import { AllProjects } from "./AllProjects";
import { AddServerWizard } from "./AddServerWizard";

/**
 * Mothership centralization-layer routes. Mounted under /m/* in App.tsx
 * only when import.meta.env.VITE_MOTHERSHIP === "1" (Vite inlines that
 * literal at build time, so this module is tree-shaken from the single-
 * install bundle).
 *
 * What lives here:
 *   /m                 — cross-server all-projects view (T-0025).
 *   /m/servers/add     — the §3.0–§3.3 Q&A wizard (T-0031), which mints
 *                        an install token via POST /api/m/servers and
 *                        auto-transitions to /m/servers/:id on the first
 *                        SSE checkpoint.
 *   /m/servers/:id     — install-checkpoint progress view (T-0024).
 *
 * Contract: vision/architecture/mothership-seam.md.
 */

function statusBadge(status: Checkpoint["status"]): string {
  switch (status) {
    case "done":
      return "mc-badge mc-badge-ok";
    case "failed":
      return "mc-badge mc-badge-danger";
    default:
      return "mc-badge mc-badge-active";
  }
}

function statusLabel(status: Checkpoint["status"]): string {
  return status === "begin" ? "in-progress" : status;
}

/** Coalesce a stream of {checkpoint, status} events into one row per
 * checkpoint, preserving install order and showing the latest status.
 * Disk-replay events and live broadcast events flow through the same
 * collapse — duplicates from the documented replay/subscribe race are
 * naturally absorbed.
 */
function coalesce(events: Checkpoint[]): Checkpoint[] {
  const byName = new Map<string, Checkpoint>();
  for (const ev of events) {
    byName.set(ev.checkpoint, ev);
  }
  return Array.from(byName.values());
}

function ServerProgress() {
  const { id } = useParams<{ id: string }>();
  const [events, setEvents] = useState<Checkpoint[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!id) return;
    const url = `/api/m/servers/${encodeURIComponent(id)}/checkpoints`;
    const es = new EventSource(url, { withCredentials: true });
    es.onopen = () => {
      setConnected(true);
      setStreamError(null);
    };
    es.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as Checkpoint;
        setEvents((prev) => [...prev, ev]);
      } catch {
        // Ignore malformed frame; the server stamps every event via
        // append_checkpoint so malformed JSON would itself be a bug.
      }
    };
    es.onerror = () => {
      // Browsers retry on their own; we just surface the disconnected
      // state in the UI so the user knows the stream isn't live.
      setConnected(false);
      setStreamError("disconnected — retrying");
    };
    return () => es.close();
  }, [id]);

  const rows = useMemo(() => coalesce(events), [events]);

  return (
    <div className="container py-4" style={{ maxWidth: 720 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div className="mc-section-title">Mothership · install progress</div>
        <span className={connected ? "mc-badge mc-badge-ok" : "mc-badge mc-badge-warn"}>
          {connected ? "live" : streamError ?? "connecting…"}
        </span>
      </div>
      <p style={{ color: "var(--mc-text-dim)", fontSize: "0.85rem", marginTop: "0.5rem" }}>
        Server <code style={{ fontFamily: "var(--mc-mono)" }}>{id}</code>. Checkpoints
        appear as the installer reports them — pending entries materialise on
        first contact, then transition through in-progress → done (or failed).
      </p>

      {rows.length === 0 ? (
        <div className="mc-empty" style={{ marginTop: "1.5rem" }}>
          <div className="mc-empty-icon">◇</div>
          <div>Waiting for the first checkpoint from the installer…</div>
        </div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, marginTop: "1rem", display: "grid", gap: "0.4rem" }}>
          {rows.map((ev) => (
            <li
              key={ev.checkpoint}
              style={{
                display: "grid",
                gridTemplateColumns: "1fr auto",
                alignItems: "center",
                padding: "0.5rem 0.75rem",
                background: "var(--mc-surface)",
                border: "1px solid var(--mc-border)",
                borderRadius: 3,
              }}
            >
              <span style={{ fontFamily: "var(--mc-mono)", fontSize: 13 }}>
                {ev.checkpoint}
              </span>
              <span className={statusBadge(ev.status)}>{statusLabel(ev.status)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NotFound() {
  return (
    <div className="container py-4" style={{ maxWidth: 720 }}>
      <div className="mc-section-title">Mothership</div>
      <div className="mc-empty" style={{ marginTop: "1.5rem" }}>
        <div className="mc-empty-icon">◇</div>
        <div>Unknown mothership route.</div>
        <div style={{ marginTop: "0.5rem" }}>
          <Link to="/m/servers/add">Add a server →</Link>
        </div>
      </div>
    </div>
  );
}

export default function MothershipRoutes() {
  return (
    <Routes>
      <Route index element={<AllProjects />} />
      <Route path="servers/add" element={<AddServerWizard />} />
      <Route path="servers/:id" element={<ServerProgress />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
