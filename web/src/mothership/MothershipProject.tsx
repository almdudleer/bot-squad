import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Project } from "../pages/Project";
import { ApiClientProvider } from "../apiContext";
import { apiFor, ProxyError, type ProxyErrorKind } from "./api";

/**
 * T-0068: cross-server per-project view. Mounted at
 * `/m/servers/:server_id/p/:slug` only on the mothership build (see
 * `routes.tsx`). Renders the same `Project` page surface as the local
 * `/p/:slug` route, but threads a proxy-backed `apiFor(server_id)` client
 * through `ApiClientProvider` so every per-project call hits the peer's API
 * via the mothership proxy at `/api/m/servers/:id/api/...`.
 *
 * Auth surface (see ticket DoD):
 *   - bearer missing (503 from the proxy)        → "Connect this server first"
 *   - peer rejected our bearer (401/403 upstream) → "Access denied"
 *   - other errors fall through to the Project page's own error pill
 *
 * Scope-limit: this wrapper hands off to the existing `Project.tsx`
 * unchanged. Internal Project links (`/p/:slug/t/:id`, `/p/:slug/vision`)
 * still point at the *local* route. Cross-server sub-route plumbing is a
 * follow-up to T-0068, not part of this ticket — the cross-server
 * board view is the deliverable.
 */
export function MothershipProject() {
  const { server_id = "", slug = "" } = useParams<{
    server_id: string;
    slug: string;
  }>();

  // Memoise the client so the provider value (and the probe effect's dep
  // array) stays referentially stable across re-renders of this wrapper —
  // otherwise the probe re-fires on every Project state change.
  const apiClient = useMemo(() => apiFor(server_id), [server_id]);

  const [probe, setProbe] = useState<
    | { state: "loading" }
    | { state: "ok" }
    | { state: "denied"; kind: ProxyErrorKind }
    | { state: "error"; message: string }
  >({ state: "loading" });

  useEffect(() => {
    if (!server_id) {
      setProbe({ state: "denied", kind: "not_connected" });
      return;
    }
    let cancelled = false;
    setProbe({ state: "loading" });
    // Cheapest single-shot proxy probe: GET /api/projects (which the peer
    // already exposes). A success means the bearer roundtrip works; a
    // ProxyError tells us which auth state to render. We don't try to
    // validate the slug here — Project's own backlog fetch will surface a
    // 404 in its existing error pill if the slug doesn't exist on the peer.
    apiClient
      .projects()
      .then(() => {
        if (cancelled) return;
        setProbe({ state: "ok" });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof ProxyError) {
          setProbe({ state: "denied", kind: e.kind });
        } else {
          setProbe({
            state: "error",
            message: e instanceof Error ? e.message : String(e),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [server_id, apiClient]);

  if (probe.state === "loading") {
    return <div className="mc-loading">Checking peer access…</div>;
  }
  if (probe.state === "denied") {
    return <PeerAccessError kind={probe.kind} serverId={server_id} />;
  }
  if (probe.state === "error") {
    return (
      <div className="container py-4" style={{ maxWidth: 720 }}>
        <div className="alert alert-danger">
          Failed to reach peer server <code>{server_id}</code>: {probe.message}
        </div>
      </div>
    );
  }

  return (
    <ApiClientProvider value={apiClient}>
      {/* Re-use the page-level header context: Project reads ":slug" from
          useParams, which works because react-router merges the parent
          route's params (server_id + slug) into the same map. */}
      <CrossServerBanner serverId={server_id} slug={slug} />
      <Project />
    </ApiClientProvider>
  );
}

function CrossServerBanner({
  serverId,
  slug,
}: {
  serverId: string;
  slug: string;
}) {
  // Lightweight orientation strip so the user knows they're looking at a
  // peer-server board (not the local install). Kept above the Project page
  // so it doesn't clash with Project's own header.
  return (
    <div
      style={{
        background: "var(--mc-surface-deep)",
        borderBottom: "1px solid var(--mc-border)",
        padding: "0.4rem 1rem",
        fontFamily: "var(--mc-mono)",
        fontSize: "0.72rem",
        color: "var(--mc-text-dim)",
      }}
    >
      <Link
        to="/"
        style={{ color: "var(--mc-text-dim)", textDecoration: "none" }}
      >
        ← All projects
      </Link>
      <span style={{ margin: "0 0.5rem" }}>·</span>
      peer server <code style={{ color: "var(--mc-text)" }}>{serverId}</code>
      <span style={{ margin: "0 0.5rem" }}>·</span>
      project <code style={{ color: "var(--mc-text)" }}>{slug}</code>
    </div>
  );
}

// Pure helper so the bearer-missing copy is unit-testable without a DOM.
// Maps a ProxyErrorKind to its title + body string for the error panel.
// (The CTA link is identical for both kinds — both end with "reconnect
// from the registry" — so it lives in the renderer below.)
export function peerAccessCopy(kind: ProxyErrorKind): {
  title: string;
  body: (serverId: string) => string;
} {
  if (kind === "not_connected") {
    return {
      title: "Connect this server first",
      body: (serverId) =>
        `The mothership doesn’t have a bearer for ${serverId} yet — the peer either hasn’t finished its install handshake, or its bearer was rotated/revoked. Re-run the connect wizard to mint a fresh install token.`,
    };
  }
  return {
    title: "Access denied",
    body: (serverId) =>
      `Peer server ${serverId} rejected the mothership’s bearer. The bearer may have been rotated on the peer side. Reconnect from the registry to issue a new one.`,
  };
}

function PeerAccessError({
  kind,
  serverId,
}: {
  kind: ProxyErrorKind;
  serverId: string;
}) {
  const copy = peerAccessCopy(kind);
  return (
    <div className="container py-4" style={{ maxWidth: 720 }}>
      <div className="mc-section-title">{copy.title}</div>
      <p
        style={{
          color: "var(--mc-text-dim)",
          fontSize: "0.9rem",
          marginTop: "0.75rem",
        }}
      >
        {copy.body(serverId)}
      </p>
      <Link
        to="/m/servers/add"
        className="mc-badge mc-badge-info"
        style={{ textDecoration: "none", padding: "6px 14px" }}
      >
        Connect a server →
      </Link>
    </div>
  );
}

export default MothershipProject;
