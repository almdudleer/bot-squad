import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fanOut,
  mothershipApi,
  type AttachedServer,
  type FanOutResult,
  type ServerProject,
} from "./api";
import {
  isStaleInstall,
  pendingInstallState,
  pendingStateBadgeClass,
} from "./serverState";
import { canManageGrants } from "./Users";
import { CrossProjectSessions } from "./CrossProjectSessions";
import { api } from "../api";
import { Coachmark } from "../onboarding";
import { STEP_9_3_BULLETS, STEP_9_3_TITLE } from "../onboarding/copy";

/**
 * Fleet / servers overview (T-0025; reframed by T-0357). Mounted at the `/m`
 * index on the mothership build (per docs/architecture/D-0017-mothership-seam.md).
 *
 * T-0357 (de-fleet the chrome): this used to be a SECOND project picker — a flat
 * grid of every project across every server, duplicating the `/` Picker for the
 * self server's projects (the dogfood T-0331 "two doors" incoherence). It is
 * reframed into a SERVERS overview: one row per attached server (state +
 * reachability + project count), drill into a server → its projects → operate.
 * `/` is now the ONLY project-operate door; this is the fleet/admin surface.
 * Project creation moved to the `/` Picker's wizard; server-add stays here.
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

// T-0342: a PENDING/installing server card otherwise lingers forever as
// "install hasn't finished yet" (dogfood F6 — the stale `linza` card). An
// install that hasn't made progress in this long is treated as stalled: the
// card switches to a "stalled" wording and gains a dismiss affordance. T-0653
// moved the derivation (`isStaleInstall`/`STALE_INSTALL_MS`) into
// `serverState.ts`, shared with `/m/users`'s Connected-servers table, so a
// deliberate hold (`hold_reason`) reads identically on both pages instead of
// disagreeing on vocabulary for the same row.

// T-0342: dismissed stale-install cards persist per server id in localStorage
// so a dismissal sticks across reloads (the registry row stays PENDING until
// the install finishes or an admin deletes it; the FE just stops nagging).
const DISMISS_KEY = "bot-squad:dismissed-installs";

export function loadDismissedInstalls(): Set<string> {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr.map(String)) : new Set();
  } catch {
    return new Set();
  }
}

function persistDismissedInstalls(ids: Set<string>): void {
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify([...ids]));
  } catch {
    /* storage unavailable — dismissal is best-effort, in-memory only */
  }
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
  // T-0342: ids of stale install cards the operator dismissed (persisted).
  const [dismissed, setDismissed] = useState<Set<string>>(() =>
    loadDismissedInstalls(),
  );
  // T-0653: viewer's username drives the owner-only Hold/Unhold affordance
  // (mirrors the grants-manage gate — see `canManageGrants`).
  const [username, setUsername] = useState<string | undefined>(undefined);

  function dismissInstall(id: string) {
    setDismissed((prev) => {
      const next = new Set(prev);
      next.add(id);
      persistDismissedInstalls(next);
      return next;
    });
  }

  async function loadSections(cancelledRef: { current: boolean }) {
    try {
      const servers = await mothershipApi.listServers();
      const readyIds = servers
        .filter((s) => s.install_state === "ready")
        .map((s) => s.id);
      const fanResults = await fanOut(readyIds, (id) =>
        mothershipApi.projectsFor(id),
      );
      if (cancelledRef.current) return;
      setSections(buildSections(servers, fanResults));
    } catch (e) {
      if (cancelledRef.current) return;
      setTopError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    const cancelledRef = { current: false };
    setTopError(null);
    setSections(null);
    loadSections(cancelledRef);
    api
      .getMyProfile()
      .then((me) => {
        if (!cancelledRef.current) setUsername(me.username);
      })
      .catch(() => {
        /* anon / failed profile fetch — Hold/Unhold stays hidden */
      });
    return () => {
      cancelledRef.current = true;
    };
  }, []);

  // T-0653: re-fetch the registry after a hold/unhold mutation so the badge
  // reflects the new hold_reason without a hard reload. `invalidateServersCache`
  // (called by `holdServer`/`unholdServer`) ensures this refetch isn't served
  // stale data from the 5s cache.
  function reload() {
    loadSections({ current: false });
  }

  return (
    <div className="container py-4" style={{ maxWidth: "900px" }}>
      {/* §9.3 spotlight — single coachmark, three bullets. Auto-gated to
          mothership builds by virtue of living in this module (App.tsx
          lazy-imports it only when VITE_MOTHERSHIP === "1"). Re-anchored by
          T-0357 to the sidebar's ▦ "Fleet / admin" icon (the old FLEET nav
          item it pointed at is gone). */}
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

      {/* T-0661: cross-project Processes/Sessions glance view, placed at the
          top of the page per the stakeholder's own suggested placement. Fed
          by the SAME `sections` fan-out the Servers list below renders from
          — no duplicate server/project fetch. */}
      <CrossProjectSessions sections={sections} />

      <div className="d-flex align-items-center justify-content-between gap-2 mb-2">
        <div className="mc-section-title" style={{ margin: 0 }}>
          Servers
        </div>
        <Link
          to="/m/servers/add"
          className="mc-badge mc-badge-info"
          style={{ textDecoration: "none", padding: "6px 14px" }}
        >
          + Add server
        </Link>
      </div>
      {/* T-0357: `/` is the project-operate door; this fleet view is about
          servers. Drill into a server to reach its projects. */}
      <p style={{ color: "var(--mc-text-dim)", fontSize: 12, marginBottom: "1.25rem" }}>
        The fleet of attached servers. Open a server to see its projects and
        drill in to operate one.
      </p>

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
        <div style={{ display: "grid", gap: "0.6rem" }}>
          {sections
            .filter((section) => !dismissed.has(section.server.id))
            .map((section) => (
              <ServerRow
                key={section.server.id}
                section={section}
                username={username}
                onDismiss={() => dismissInstall(section.server.id)}
                onReload={reload}
              />
            ))}
        </div>
      )}
    </div>
  );
}

/** T-0357: a server is reachable only when its fan-out result resolved ok. */
function projectCountLabel(section: ServerSection): string {
  if (section.kind === "installing") return "";
  if (!section.result || !section.result.ok) return "unreachable";
  const n = section.result.data.length;
  return `${n} project${n === 1 ? "" : "s"}`;
}

/**
 * One server in the fleet overview (T-0357). The whole row drills into the
 * server-detail view (`/m/servers/:id`), where its projects live and link
 * through to operate. Replaces the old ServerSectionView, which rendered every
 * project inline as a second project picker.
 */
function ServerRow({
  section,
  username,
  onDismiss,
  onReload,
}: {
  section: ServerSection;
  username: string | undefined;
  onDismiss: () => void;
  onReload: () => void;
}) {
  const { server, kind } = section;
  const label = serverHeaderLabel(server);
  const stalled = kind === "installing" && isStaleInstall(server);
  const held = kind === "installing" && Boolean(server.hold_reason);
  const canManage = canManageGrants(server, username);
  const count = projectCountLabel(section);

  // T-0653: owner-only hold/unhold. window.prompt mirrors the existing
  // pause-reason affordance in ObservabilityPanel.tsx — no reason text
  // component exists yet for a one-off free-text admin action.
  async function holdInstall() {
    const reason = window.prompt("Hold reason (why is this install intentionally paused?):");
    if (!reason) return; // cancelled or empty — a hold needs a reason
    await mothershipApi.holdServer(server.id, reason);
    onReload();
  }

  async function unholdInstall() {
    await mothershipApi.unholdServer(server.id);
    onReload();
  }

  return (
    <Link
      to={`/m/servers/${encodeURIComponent(server.id)}`}
      data-testid={server.is_self ? "server-self" : "server-peer"}
      style={{
        display: "block",
        border: "1px solid var(--mc-border)",
        borderRadius: 4,
        padding: "0.7rem 1rem",
        background: "var(--mc-surface)",
        textDecoration: "none",
        color: "inherit",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "0.5rem",
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
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          {count && (
            <span style={{ color: "var(--mc-text-dim)", fontSize: 12 }}>
              {count}
            </span>
          )}
          <ServerHeaderBadge section={section} />
          {/* T-0653: a held install is an ACCURATE, intentional state —
              Dismiss (which would hide that visibility) is offered only for
              a genuinely stalled/unheld card, never a held one. */}
          {stalled && (
            <button
              type="button"
              data-testid={`dismiss-install-${server.id}`}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onDismiss();
              }}
              title="Dismiss this stalled install card"
              aria-label="Dismiss"
              className="btn btn-sm btn-outline-secondary"
              style={{ fontSize: "0.7rem", lineHeight: 1, padding: "2px 8px" }}
            >
              Dismiss
            </button>
          )}
          {kind === "installing" && canManage && !held && (
            <button
              type="button"
              data-testid={`hold-install-${server.id}`}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                void holdInstall();
              }}
              title="Mark this install as deliberately held pending explicit action"
              className="btn btn-sm btn-outline-secondary"
              style={{ fontSize: "0.7rem", lineHeight: 1, padding: "2px 8px" }}
            >
              Hold
            </button>
          )}
          {kind === "installing" && canManage && held && (
            <button
              type="button"
              data-testid={`unhold-install-${server.id}`}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                void unholdInstall();
              }}
              title="Clear the hold — resume normal stale-install tracking"
              className="btn btn-sm btn-outline-secondary"
              style={{ fontSize: "0.7rem", lineHeight: 1, padding: "2px 8px" }}
            >
              Clear hold
            </button>
          )}
        </div>
      </div>
      <div style={{ marginTop: "0.4rem" }}>
        <InstallTokenRow server={server} />
        {kind === "installing" && (
          <div style={{ color: "var(--mc-text-dim)", fontSize: 12 }}>
            {held
              ? `Held: ${server.hold_reason}`
              : stalled
                ? "Install appears stalled — no progress in over 30 min."
                : "Install hasn’t finished yet."}{" "}
            <span style={{ color: "var(--mc-cyan)" }}>watch progress →</span>
          </div>
        )}
      </div>
    </Link>
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
    // T-0653: `failed` is a terminal state, checked before the shared
    // held/stalled/pending derivation (which only covers non-terminal rows).
    if (server.install_state === "failed") {
      return <span className="mc-badge mc-badge-danger">failed</span>;
    }
    const state = pendingInstallState(server);
    return (
      <span
        className={pendingStateBadgeClass(state)}
        title={state === "held" ? server.hold_reason ?? undefined : undefined}
      >
        {state}
      </span>
    );
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

export default AllProjects;
