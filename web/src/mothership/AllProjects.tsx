import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fanOut,
  mothershipApi,
  type AttachedServer,
  type FanOutResult,
  type ServerProject,
} from "./api";
import { api } from "../api";
import { Modal } from "../components/Modal";
import { Coachmark } from "../onboarding";
import { STEP_9_3_BULLETS, STEP_9_3_TITLE } from "../onboarding/copy";

function deriveSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .replace(/-+$/g, "");
}

interface NewProjectState {
  display_name: string;
  slug: string;
  slug_touched: boolean;
  repo_path: string;
}

/**
 * Cross-server all-projects view (T-0025). Mounted at /m on the mothership
 * build, and at / on the mothership build via App.tsx's VITE_MOTHERSHIP swap
 * (per docs/architecture/D-0017-mothership-seam.md).
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

// T-0068: pure helper so the AllProjects card-link routing decision is
// testable without a DOM. Self-server keeps the short `/p/:slug` URL (so
// bookmarks from the single-install era still work); peer servers route
// through the cross-server view at `/m/servers/:id/p/:slug`.
export function projectCardLinkFor(
  server: Pick<AttachedServer, "id" | "is_self">,
  slug: string,
): string {
  if (server.is_self) return `/p/${encodeURIComponent(slug)}`;
  return `/m/servers/${encodeURIComponent(server.id)}/p/${encodeURIComponent(slug)}`;
}

// T-0113: install-tokens sub-table state per attached server. The mothership
// store only ever keeps ONE outstanding install token per server (minted at
// /api/m/servers, burned at /installer/connect — see mothership_store.py),
// so this is a single-row "table" — we surface it as a one-liner under the
// section header rather than a separate <table>. Per-server token revoke +
// re-mint endpoints aren't on the BE yet (filed under T-0129); until they
// land the FE shows status only.
export type InstallTokenStatus = "active" | "expired" | "consumed" | "n/a";

export function installTokenStatus(
  server: Pick<
    AttachedServer,
    "install_state" | "install_token_expires_at" | "is_self"
  >,
  now: Date = new Date(),
): { status: InstallTokenStatus; expiresAt: string | null } {
  // Self entries skip the install-token lifecycle entirely (see
  // mothership_store.register_self_if_missing). Render nothing for them.
  if (server.is_self) return { status: "n/a", expiresAt: null };
  if (server.install_state === "pending") {
    const exp = server.install_token_expires_at;
    if (!exp) return { status: "n/a", expiresAt: null };
    const expDate = new Date(exp);
    if (Number.isNaN(expDate.getTime())) {
      return { status: "n/a", expiresAt: exp };
    }
    if (now.getTime() >= expDate.getTime()) {
      return { status: "expired", expiresAt: exp };
    }
    return { status: "active", expiresAt: exp };
  }
  // connected / ready / failed — token was burned (or never minted in the
  // failed-pre-connect case, which is indistinguishable from the FE).
  return { status: "consumed", expiresAt: null };
}

export function installTokenLabel(
  state: { status: InstallTokenStatus; expiresAt: string | null },
): string {
  switch (state.status) {
    case "active":
      return `install token active · expires ${state.expiresAt}`;
    case "expired":
      return `install token expired (${state.expiresAt})`;
    case "consumed":
      return "install token consumed";
    case "n/a":
      return "";
  }
}

export function installTokenBadgeClass(status: InstallTokenStatus): string {
  switch (status) {
    case "active":
      return "mc-badge mc-badge-active";
    case "expired":
      return "mc-badge mc-badge-danger";
    case "consumed":
      return "mc-badge mc-badge-dim";
    case "n/a":
      return "mc-badge mc-badge-dim";
  }
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

/**
 * Label fragments for the per-server section header (T-0055).
 *
 * The self entry (the mothership's own registry row) gets a "(this server)"
 * suffix so an operator with both their mothership and one attached peer can
 * tell which is which at a glance. The display_name + base_url are
 * untouched. Pure-function so the renderer stays trivial and we have a
 * unit-test seam without React.
 */
export function serverHeaderLabel(server: AttachedServer): {
  name: string;
  url: string;
  suffix: string | null;
} {
  // Auto-self-registered entries set display_name to the hostname, so
  // display_name + base_url duplicate each other in the card. Drop the
  // URL line when it adds no information.
  let host = "";
  try {
    host = new URL(server.base_url).hostname;
  } catch {
    host = "";
  }
  const redundant =
    server.display_name === host || server.display_name === server.base_url;
  return {
    name: server.display_name,
    url: redundant ? "" : server.base_url,
    suffix: server.is_self ? "this server" : null,
  };
}

export function AllProjects() {
  const [sections, setSections] = useState<ServerSection[] | null>(null);
  const [topError, setTopError] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [creating, setCreating] = useState<NewProjectState | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSaving, setCreateSaving] = useState(false);

  function reload() {
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
        setSections(buildSections(servers, fanResults));
      } catch (e) {
        setTopError(e instanceof Error ? e.message : String(e));
      }
    })();
  }

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
    api.me().then((m) => setIsAdmin(Boolean(m.is_admin))).catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  async function submitCreate() {
    if (!creating) return;
    if (!creating.display_name.trim() || !creating.slug.trim()) {
      setCreateError("display name and slug required");
      return;
    }
    setCreateSaving(true);
    setCreateError(null);
    try {
      // T-0051: createProject now takes the JSON body directly. The
      // mothership AllProjects view only uses the minimal back-compat
      // shape (no mode field); the deep wizard lives on the per-server
      // Picker (see pages/Picker.tsx).
      const body: Record<string, unknown> = {
        slug: creating.slug.trim(),
        display_name: creating.display_name.trim(),
      };
      const repo = creating.repo_path.trim();
      if (repo) body.repo_path = repo;
      await api.createProject(body);
      setCreating(null);
      reload();
    } catch (e) {
      setCreateError(String(e));
    } finally {
      setCreateSaving(false);
    }
  }

  return (
    <div className="container py-4" style={{ maxWidth: "1100px" }}>
      {/* §9.3 spotlight — single coachmark, three bullets. Auto-gated to
          mothership builds by virtue of living in this module (App.tsx
          lazy-imports it only when VITE_MOTHERSHIP === "1"). */}
      <Coachmark
        stepId="srv.9_3.cross_server"
        title={STEP_9_3_TITLE}
        anchorSelector='[data-onboarding-anchor="all-projects-nav"]'
        placement="right"
        body={
          <ul className="mb-0 ps-3">
            {STEP_9_3_BULLETS.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        }
      />

      <div className="d-flex align-items-center justify-content-between gap-2 mb-4">
        <div className="mc-section-title" style={{ margin: 0 }}>
          All projects
        </div>
        <div className="d-flex align-items-center gap-2">
          {isAdmin && (
            <button
              type="button"
              className="btn btn-outline-primary btn-sm"
              style={{ fontSize: "0.72rem" }}
              data-onboarding-anchor="create-project"
              onClick={() =>
                setCreating({
                  display_name: "",
                  slug: "",
                  slug_touched: false,
                  repo_path: "",
                })
              }
            >
              + New project
            </button>
          )}
          <Link
            to="/m/servers/add"
            className="mc-badge mc-badge-info"
            style={{ textDecoration: "none", padding: "6px 14px" }}
          >
            + Add server
          </Link>
        </div>
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

      <Modal
        open={creating !== null}
        title="New project (this server)"
        onClose={() => setCreating(null)}
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setCreating(null)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submitCreate}
              disabled={createSaving}
            >
              {createSaving ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {createError && <div className="alert alert-danger">{createError}</div>}
        <div className="mb-3">
          <label className="form-label">Display name *</label>
          <input
            className="form-control"
            value={creating?.display_name ?? ""}
            onChange={(e) => {
              if (!creating) return;
              const display_name = e.target.value;
              setCreating({
                ...creating,
                display_name,
                slug: creating.slug_touched
                  ? creating.slug
                  : deriveSlug(display_name),
              });
            }}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Slug *</label>
          <input
            className="form-control"
            style={{ fontFamily: "var(--mc-mono)" }}
            value={creating?.slug ?? ""}
            onChange={(e) =>
              creating &&
              setCreating({ ...creating, slug: e.target.value, slug_touched: true })
            }
            placeholder="lowercase, letters/digits/-/_"
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Repo path</label>
          <input
            className="form-control"
            style={{ fontFamily: "var(--mc-mono)" }}
            value={creating?.repo_path ?? ""}
            onChange={(e) =>
              creating && setCreating({ ...creating, repo_path: e.target.value })
            }
            placeholder="optional — fill in projects.toml later"
          />
        </div>
      </Modal>
    </div>
  );
}

function ServerSectionView({ section }: { section: ServerSection }) {
  const { server, kind, result } = section;
  const label = serverHeaderLabel(server);
  return (
    <section
      data-testid={server.is_self ? "server-self" : "server-peer"}
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
          <strong style={{ fontFamily: "var(--mc-mono)" }}>{label.name}</strong>
          {label.url && (
            <span style={{ color: "var(--mc-text-dim)", fontSize: 12 }}>
              {label.url}
            </span>
          )}
          {label.suffix && (
            <span
              className="mc-badge mc-badge-info"
              title="The mothership server itself"
            >
              {label.suffix}
            </span>
          )}
        </div>
        <ServerHeaderBadge section={section} />
      </header>
      <InstallTokenRow server={server} />
      <ServerSectionBody server={server} kind={kind} result={result} />
    </section>
  );
}

function InstallTokenRow({ server }: { server: AttachedServer }) {
  const state = installTokenStatus(server);
  // Self entries + nothing-to-show cases collapse — keeps the all-projects
  // view clean for the common case (ready peers + the self entry) while
  // still surfacing the pending/expired states the operator needs.
  if (state.status === "n/a") return null;
  if (state.status === "consumed") return null;
  return (
    <div
      data-testid={`install-token-${server.id}`}
      data-token-status={state.status}
      style={{
        fontSize: 12,
        color: "var(--mc-text-dim)",
        marginBottom: "0.5rem",
        display: "flex",
        gap: "0.5rem",
        alignItems: "center",
      }}
    >
      <span className={installTokenBadgeClass(state.status)}>
        {state.status}
      </span>
      <span>{installTokenLabel(state)}</span>
    </div>
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
      {result.data.map((p) => {
        const card = (
          <div className="mc-project-card" style={{ cursor: "pointer" }}>
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
        );
        // T-0068: peer-server cards now link to the cross-server board
        // route. The self-server keeps its short `/p/:slug` URL so
        // bookmarks/deep-links from the single-install era still resolve.
        const to = projectCardLinkFor(server, p.slug);
        return (
          <div className="col-md-4" key={p.slug}>
            <Link
              to={to}
              style={{ textDecoration: "none", color: "inherit" }}
            >
              {card}
            </Link>
          </div>
        );
      })}
    </div>
  );
}

export default AllProjects;
