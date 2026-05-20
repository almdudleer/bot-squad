import { useEffect, useMemo, useState } from "react";
import { Routes, Route, Link, Navigate, useParams } from "react-router-dom";
import { mothershipApi, type AttachedServer, type Checkpoint } from "./api";
import { AddServerWizard } from "./AddServerWizard";
import { Releases } from "./Releases";

/**
 * T-0013: Chapter I §8 specifies that the mothership add-server flow
 * should land the user on the target server's `/welcome` screen once the
 * installer reports success, instead of leaving them parked on
 * `/m/servers/:id`. The installer's terminal checkpoint is `print_attach`
 * (see scripts/install/install.sh:STEPS) — that's the trigger.
 *
 * Pure helpers so they're unit-testable without a DOM.
 */
export function isInstallComplete(events: Checkpoint[]): boolean {
  // Last-write-wins per checkpoint, mirrors `coalesce` below. We look for
  // print_attach=done; the bash installer only emits this once every
  // upstream step has succeeded.
  let final: Checkpoint["status"] | null = null;
  for (const ev of events) {
    if (ev.checkpoint === "print_attach") final = ev.status;
  }
  return final === "done";
}

export function welcomeUrlFor(baseUrl: string): string {
  // Strip a single trailing slash so we don't emit `//welcome`. The
  // base_url is validated server-side at server-mint time (T-0024 schema),
  // so we don't re-validate here.
  return `${baseUrl.replace(/\/$/, "")}/welcome`;
}

/**
 * Mothership centralization-layer routes. Mounted under /m/* in App.tsx
 * only when import.meta.env.VITE_MOTHERSHIP === "1" (Vite inlines that
 * literal at build time, so this module is tree-shaken from the single-
 * install bundle).
 *
 * What lives here:
 *   /m                 — redirect to `/` (unified all-projects view at the
 *                        root, T-0055). Kept as a 301-style stub so any
 *                        stray internal bookmarks pre-T-0055 still resolve.
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
  const [server, setServer] = useState<AttachedServer | null>(null);
  const [handedOff, setHandedOff] = useState(false);

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

  // T-0013: fetch the registry entry once so we know the target base_url
  // for the post-install /welcome handoff. The registry is small and the
  // call is cheap; we don't need a per-server detail endpoint.
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    mothershipApi
      .listServers()
      .then((all) => {
        if (cancelled) return;
        const match = all.find((s) => s.id === id);
        if (match) setServer(match);
      })
      .catch(() => {
        // Non-fatal: the user can still navigate manually. The progress
        // view degrades gracefully without the registry entry.
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  // T-0013: when print_attach=done lands, hand off to the target's
  // /welcome screen (cross-origin to the install_state-ready server). Guard
  // with `handedOff` so a flaky SSE replay can't bounce the user twice.
  useEffect(() => {
    if (handedOff) return;
    if (!server) return;
    if (!isInstallComplete(events)) return;
    setHandedOff(true);
    globalThis.window?.location?.replace(welcomeUrlFor(server.base_url));
  }, [events, server, handedOff]);

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
      {/* T-0055: /m is no longer the all-projects view — the unified `/`
          owns that. Redirect for backwards-compat with any internal
          bookmarks; the wizard + install-progress sub-routes still live
          here because the install flow URLs are quoted in scripts/docs. */}
      <Route index element={<Navigate to="/" replace />} />
      <Route path="servers/add" element={<AddServerWizard />} />
      <Route path="servers/:id" element={<ServerProgress />} />
      {/* T-0087: mothership-only releases tab. Route lives here so the
          whole module + its chunk gets tree-shaken from detached builds
          via the same VITE_MOTHERSHIP gate in App.tsx. */}
      <Route path="releases" element={<Releases />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
