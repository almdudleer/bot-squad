import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fanOut,
  mothershipApi,
  type AttachedServer,
  type FanOutResult,
  type ServerProject,
} from "./api";

/**
 * Cross-server all-projects view (T-0025). Mounted at /m on the mothership
 * build, and at / on the mothership build via App.tsx's VITE_MOTHERSHIP swap
 * (per vision/architecture/mothership-seam.md).
 *
 * Quick-status enum (working|needs-input|idle) is locked here as the canonical
 * shape — coordinated with the multi_server TL on 2026-05-14. T-0016 will
 * later swap the source-of-truth into the same projects_cache slot transparently;
 * a fourth state requires a peer-coordinated contract amendment.
 */

export type ServerStatus = "working" | "needs-input" | "idle" | string;

export type ServerSection = {
  server: AttachedServer;
  /**
   * `live` if the registry says the server is `ready` AND projectsFor()
   * resolved. `pending`/`installing` if the install hasn't completed; the
   * render points the user at the install-progress page.
   */
  kind: "live" | "installing";
  result: FanOutResult<ServerProject[]> | null;
};

export function buildSections(
  servers: AttachedServer[],
  fanResults: FanOutResult<ServerProject[]>[],
): ServerSection[] {
  const byServerId = new Map(fanResults.map((r) => [r.serverId, r]));
  return servers.map((server) => {
    if (server.install_state !== "ready") {
      return { server, kind: "installing", result: null };
    }
    const result = byServerId.get(server.id) ?? {
      serverId: server.id,
      ok: false as const,
      error: "no result",
    };
    return { server, kind: "live", result };
  });
}

export function statusBadgeClass(status: ServerStatus): string {
  switch (status) {
    case "working":
      return "mc-badge mc-badge-active";
    case "needs-input":
      return "mc-badge mc-badge-warn";
    case "idle":
      return "mc-badge mc-badge-dim";
    default:
      return "mc-badge mc-badge-dim";
  }
}

export function AllProjects() {
  const [sections, setSections] = useState<ServerSection[] | null>(null);
  const [topError, setTopError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTopError(null);
    setSections(null);
    (async () => {
      try {
        const servers = await mothershipApi.listServers();
        const readyIds = servers
          .filter((s) => s.install_state === "ready")
          .map((s) => s.id);
        const fanResults = await fanOut(readyIds, (id) =>
          mothershipApi.projectsFor(id),
        );
        if (cancelled) return;
        setSections(buildSections(servers, fanResults));
      } catch (e) {
        if (cancelled) return;
        setTopError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="container py-4" style={{ maxWidth: "1100px" }}>
      <div className="d-flex align-items-center justify-content-between gap-2 mb-4">
        <div className="mc-section-title" style={{ margin: 0 }}>
          All projects
        </div>
        <Link
          to="/m/servers/add"
          className="mc-badge mc-badge-info"
          style={{ textDecoration: "none", padding: "6px 14px" }}
        >
          + Add server
        </Link>
      </div>

      {topError && <div className="alert alert-danger">{topError}</div>}

      {!topError && sections === null && (
        <div className="mc-loading">Loading</div>
      )}

      {!topError && sections !== null && sections.length === 0 && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          <div>No servers attached yet.</div>
          <div style={{ marginTop: "0.75rem" }}>
            <Link
              to="/m/servers/add"
              className="mc-badge mc-badge-info"
              style={{ textDecoration: "none", padding: "6px 14px" }}
            >
              Add a server →
            </Link>
          </div>
        </div>
      )}

      {!topError && sections !== null && sections.length > 0 && (
        <div style={{ display: "grid", gap: "1.25rem" }}>
          {sections.map((section) => (
            <ServerSectionView key={section.server.id} section={section} />
          ))}
        </div>
      )}
    </div>
  );
}

function ServerSectionView({ section }: { section: ServerSection }) {
  const { server, kind, result } = section;
  return (
    <section
      style={{
        border: "1px solid var(--mc-border)",
        borderRadius: 4,
        padding: "0.75rem 1rem",
        background: "var(--mc-surface)",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "0.5rem",
          marginBottom: "0.75rem",
          flexWrap: "wrap",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: "0.5rem",
            flexWrap: "wrap",
          }}
        >
          <strong style={{ fontFamily: "var(--mc-mono)" }}>
            {server.display_name}
          </strong>
          <span style={{ color: "var(--mc-text-dim)", fontSize: 12 }}>
            {server.base_url}
          </span>
        </div>
        <ServerHeaderBadge section={section} />
      </header>
      <ServerSectionBody server={server} kind={kind} result={result} />
    </section>
  );
}

function ServerHeaderBadge({ section }: { section: ServerSection }) {
  const { server, kind, result } = section;
  if (kind === "installing") {
    const cls =
      server.install_state === "failed"
        ? "mc-badge mc-badge-danger"
        : "mc-badge mc-badge-warn";
    return <span className={cls}>{server.install_state}</span>;
  }
  if (result && result.ok) {
    return <span className="mc-badge mc-badge-ok">reachable</span>;
  }
  return (
    <span
      className="mc-badge mc-badge-danger"
      title={result && !result.ok ? result.error : undefined}
    >
      unreachable
    </span>
  );
}

function ServerSectionBody({
  server,
  kind,
  result,
}: {
  server: AttachedServer;
  kind: ServerSection["kind"];
  result: FanOutResult<ServerProject[]> | null;
}) {
  if (kind === "installing") {
    return (
      <div style={{ color: "var(--mc-text-dim)", fontSize: 13 }}>
        Install hasn’t finished yet.{" "}
        <Link to={`/m/servers/${encodeURIComponent(server.id)}`}>
          watch progress →
        </Link>
      </div>
    );
  }
  if (!result || !result.ok) {
    return (
      <div style={{ color: "var(--mc-text-dim)", fontSize: 13 }}>
        {(result && !result.ok && result.error) ||
          "Unable to reach this server."}
      </div>
    );
  }
  if (result.data.length === 0) {
    return (
      <div style={{ color: "var(--mc-text-dim)", fontSize: 13 }}>
        No projects on this server yet.
      </div>
    );
  }
  return (
    <div className="row g-2">
      {result.data.map((p) => (
        <div className="col-md-4" key={p.slug}>
          <div className="mc-project-card" style={{ cursor: "default" }}>
            <div className="mc-project-name">{p.display_name}</div>
            <div
              className="mc-project-slug"
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "0.5rem",
              }}
            >
              <span>{p.slug}</span>
              <span className={statusBadgeClass(p.status)}>{p.status}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default AllProjects;
