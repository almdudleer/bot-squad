import { useEffect, useMemo, useState } from "react";
import { Routes, Route, Link, useNavigate, useParams } from "react-router-dom";
import { mothershipApi, type Checkpoint, type NewServer } from "./api";
import { AllProjects } from "./AllProjects";

/**
 * Mothership centralization-layer routes. Mounted under /m/* in App.tsx
 * only when import.meta.env.VITE_MOTHERSHIP === "1" (Vite inlines that
 * literal at build time, so this module is tree-shaken from the single-
 * install bundle).
 *
 * What lives here (T-0024 scope):
 *   /m/servers/add     — minimal create-server form (POST /api/m/servers)
 *   /m/servers/:id     — install-checkpoint progress view (SSE)
 *
 * The full add-server wizard (the §3.0-3.3 Q-and-A flow that generates
 * the copyable install prompts for each pathway) is T-0031 — it consumes
 * the same /api/m/servers + SSE endpoints but wraps them in a richer UI.
 *
 * T-0025 will land /m (cross-server all-projects view) + T-0023 will land
 * apiFor(serverId) for talking to attached servers.
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

function AddServer() {
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<NewServer | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const out = await mothershipApi.createServer(displayName.trim(), baseUrl.trim());
      setMinted(out);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (minted) {
    const oneLiner = `curl -fsSL "${minted.install_url}" | bash`;
    return (
      <div className="container py-4" style={{ maxWidth: 720 }}>
        <div className="mc-section-title">Mothership · install link issued</div>
        <p style={{ color: "var(--mc-text-dim)", fontSize: "0.85rem" }}>
          Run the one-liner below on the target server. The wizard moves to
          the next step automatically as soon as the installer makes its
          first call back to the mothership.
        </p>
        <pre className="mc-code-block" style={{
          padding: "0.75rem 1rem",
          background: "var(--mc-surface)",
          border: "1px solid var(--mc-border)",
          borderRadius: 4,
          overflowX: "auto",
          fontSize: 12,
          margin: "0.75rem 0",
        }}>
          {oneLiner}
        </pre>
        <p style={{ color: "var(--mc-text-dim)", fontSize: "0.78rem" }}>
          Token expires {minted.expires_at ?? "in 24 hours"}. The bundle GETs
          are idempotent — re-running the installer reuses the same token
          until it expires or the install completes.
        </p>
        <div style={{ display: "flex", gap: "0.5rem", marginTop: "1rem" }}>
          <Link
            to={`/m/servers/${minted.id}`}
            className="mc-badge mc-badge-info"
            style={{ textDecoration: "none", padding: "6px 14px" }}
          >
            watch install progress →
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="container py-4" style={{ maxWidth: 560 }}>
      <div className="mc-section-title">Mothership · add server</div>
      <form onSubmit={onSubmit} style={{ display: "grid", gap: "0.75rem", marginTop: "1rem" }}>
        <label style={{ display: "grid", gap: 4, fontSize: 12, letterSpacing: "0.05em" }}>
          <span style={{ color: "var(--mc-text-dim)", textTransform: "uppercase" }}>display name</span>
          <input
            required
            autoFocus
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="e.g. mothership-staging"
            style={{
              padding: "6px 10px",
              background: "var(--mc-surface)",
              border: "1px solid var(--mc-border)",
              color: "var(--mc-text)",
              fontFamily: "var(--mc-mono)",
              fontSize: 13,
              borderRadius: 3,
            }}
          />
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 12, letterSpacing: "0.05em" }}>
          <span style={{ color: "var(--mc-text-dim)", textTransform: "uppercase" }}>base url</span>
          <input
            required
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://bot-squad.example.com"
            style={{
              padding: "6px 10px",
              background: "var(--mc-surface)",
              border: "1px solid var(--mc-border)",
              color: "var(--mc-text)",
              fontFamily: "var(--mc-mono)",
              fontSize: 13,
              borderRadius: 3,
            }}
          />
        </label>
        {error && (
          <div className="mc-badge mc-badge-danger" style={{ alignSelf: "start" }}>
            {error}
          </div>
        )}
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <button
            type="submit"
            disabled={submitting}
            className="mc-badge mc-badge-info"
            style={{
              padding: "8px 18px",
              cursor: submitting ? "wait" : "pointer",
              background: "transparent",
              fontSize: 12,
            }}
          >
            {submitting ? "issuing…" : "issue install link"}
          </button>
          <button
            type="button"
            onClick={() => navigate("/m")}
            className="mc-badge mc-badge-dim"
            style={{ padding: "8px 18px", cursor: "pointer", background: "transparent", fontSize: 12 }}
          >
            cancel
          </button>
        </div>
      </form>
    </div>
  );
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
      <Route path="servers/add" element={<AddServer />} />
      <Route path="servers/:id" element={<ServerProgress />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
