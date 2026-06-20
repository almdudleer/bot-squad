import { useEffect, useMemo, useState } from "react";
import { Routes, Route, Link, useParams } from "react-router-dom";
import {
  mothershipApi,
  type AttachedServer,
  type Checkpoint,
  type Grant,
  type InviteRole,
  type MintedInvite,
  type ServerProject,
} from "./api";
import { AllProjects } from "./AllProjects";
import { AddServerWizard } from "./AddServerWizard";
import { MothershipProject } from "./MothershipProject";
import { Releases } from "./Releases";
import { Users } from "./Users";
import { api } from "../api";
import { isSuperAdminFromMe } from "../components/sidebarHelpers";

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
 * Contract: docs/architecture/D-0017-mothership-seam.md.
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

/** T-0311: badge class for a registry install_state (vs. statusBadge, which
 *  is for per-checkpoint stream status). ready/connected = live & healthy. */
function installStateBadge(state: string): string {
  switch (state) {
    case "ready":
    case "connected":
      return "mc-badge mc-badge-ok";
    case "failed":
      return "mc-badge mc-badge-danger";
    default:
      // pending / unknown — still installing.
      return "mc-badge mc-badge-warn";
  }
}

/** Render an ISO timestamp in the viewer's locale, falling back to a literal
 *  em-dash for nulls and to the raw string for anything unparseable. */
function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
}

/** One label/value row in the server-detail card. */
function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "150px 1fr",
        gap: "0.75rem",
        alignItems: "baseline",
        padding: "0.5rem 0",
        borderBottom: "1px solid var(--mc-border)",
        fontSize: 13,
      }}
    >
      <span
        style={{
          color: "var(--mc-text-dim)",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          fontSize: 11,
        }}
      >
        {label}
      </span>
      <span style={{ wordBreak: "break-word" }}>{children}</span>
    </div>
  );
}

/**
 * T-0311: real server-detail view for an already-installed server
 * (install_state ready/connected). The old `/m/servers/:id` only ever
 * rendered the install-checkpoint SSE stream, which is meaningless for a
 * live server (no new checkpoints arrive) — operators landed on a perpetual
 * "waiting for the first checkpoint" empty state. Here we surface what the
 * registry projection actually carries (display name, base_url, owner,
 * install_state, last-seen, cached projects, owner-only grants) plus links
 * to drill into each project via the proxy route.
 *
 * BE gap (noted, not fixed here per FE-only scope): the AttachedServer
 * projection carries no installed release/version field, so we can't show
 * "attached release/version" yet — that needs a registry field fed by the
 * peer's autoupdate telemetry (cf. ReleaseTelemetryRow.installed_version).
 */
function ServerDetail({ server }: { server: AttachedServer }) {
  const id = server.id;
  const [projects, setProjects] = useState<ServerProject[]>(
    server.projects_cache ?? [],
  );
  const [refreshing, setRefreshing] = useState(false);
  // Owner-only: a 403 simply means the viewer isn't the owner, so we hide
  // the grants section rather than surfacing an error (mirrors listGrants'
  // _require_owner contract in routes_mothership.py).
  const [grants, setGrants] = useState<Grant[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    mothershipApi
      .listGrants(id)
      .then((g) => {
        if (!cancelled) setGrants(g);
      })
      .catch(() => {
        if (!cancelled) setGrants(null);
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  async function onRefreshProjects() {
    setRefreshing(true);
    try {
      setProjects(await mothershipApi.refreshProjects(id));
    } catch {
      // Non-fatal: keep the cached list on a refresh failure.
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div className="container py-4" style={{ maxWidth: 720 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div className="mc-section-title">Mothership · server detail</div>
        <span className={installStateBadge(server.install_state)}>
          {server.install_state}
        </span>
      </div>

      <h2 style={{ fontSize: "1.15rem", margin: "0.75rem 0 0.25rem" }}>
        {server.display_name}
        {server.is_self && (
          <span
            className="mc-badge mc-badge-info"
            style={{ marginLeft: "0.6rem", fontSize: 11 }}
          >
            this server
          </span>
        )}
      </h2>

      <div style={{ marginTop: "1rem" }}>
        <DetailRow label="server id">
          <code style={{ fontFamily: "var(--mc-mono)" }}>{server.id}</code>
        </DetailRow>
        <DetailRow label="base url">
          <a href={server.base_url} target="_blank" rel="noreferrer" style={{ fontFamily: "var(--mc-mono)" }}>
            {server.base_url}
          </a>
        </DetailRow>
        <DetailRow label="owner">
          <code style={{ fontFamily: "var(--mc-mono)" }}>{server.owner_user}</code>
        </DetailRow>
        <DetailRow label="install state">{server.install_state}</DetailRow>
        <DetailRow label="last seen">{fmtTs(server.last_seen_at)}</DetailRow>
        <DetailRow label="created">{fmtTs(server.created_at)}</DetailRow>
        <DetailRow label="release / version">
          <span style={{ color: "var(--mc-text-dim)" }}>
            not reported by the registry yet
          </span>
        </DetailRow>
      </div>

      <section style={{ marginTop: "1.75rem" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>
            Projects {projects.length > 0 && `(${projects.length})`}
          </div>
          <button
            type="button"
            onClick={onRefreshProjects}
            disabled={refreshing}
            className="mc-badge mc-badge-info"
            style={{ padding: "2px 12px", cursor: refreshing ? "wait" : "pointer", background: "transparent", fontSize: 11 }}
          >
            {refreshing ? "refreshing…" : "refresh"}
          </button>
        </div>
        {projects.length === 0 ? (
          <div className="mc-empty" style={{ marginTop: "0.9rem" }}>
            <div className="mc-empty-icon">◇</div>
            <div>No projects synced from this server yet.</div>
          </div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, marginTop: "0.75rem", display: "grid", gap: "0.4rem" }}>
            {projects.map((p) => (
              <li key={p.slug}>
                <Link
                  to={`/m/servers/${encodeURIComponent(id)}/p/${encodeURIComponent(p.slug)}`}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr auto",
                    alignItems: "center",
                    padding: "0.55rem 0.8rem",
                    background: "var(--mc-surface)",
                    border: "1px solid var(--mc-border)",
                    borderRadius: 3,
                    textDecoration: "none",
                  }}
                >
                  <span>
                    {p.display_name}{" "}
                    <code style={{ fontFamily: "var(--mc-mono)", fontSize: 12, color: "var(--mc-text-dim)" }}>
                      {p.slug}
                    </code>
                  </span>
                  <span className="mc-badge mc-badge-info" style={{ fontSize: 11 }}>
                    {p.status}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      {grants && grants.length > 0 && (
        <section style={{ marginTop: "1.75rem" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: "0.5rem" }}>
            Access grants ({grants.length})
          </div>
          <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: "0.4rem" }}>
            {grants.map((g) => (
              <li
                key={g.username}
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr auto",
                  alignItems: "center",
                  padding: "0.5rem 0.8rem",
                  background: "var(--mc-surface)",
                  border: "1px solid var(--mc-border)",
                  borderRadius: 3,
                  fontSize: 13,
                }}
              >
                <code style={{ fontFamily: "var(--mc-mono)" }}>{g.username}</code>
                <span style={{ color: "var(--mc-text-dim)", fontSize: 11 }}>
                  by {g.granted_by} · {fmtTs(g.granted_at)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <InviteUserPanel serverId={id} />
    </div>
  );
}

function ServerProgress() {
  const { id } = useParams<{ id: string }>();
  const [events, setEvents] = useState<Checkpoint[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [server, setServer] = useState<AttachedServer | null>(null);
  const [loadingServer, setLoadingServer] = useState(true);
  const [handedOff, setHandedOff] = useState(false);

  // T-0013/T-0311: fetch the registry entry FIRST so we know whether this
  // server is still installing (→ checkpoint stream) or already live (→
  // real detail view). The registry is small and the call is cheap.
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setLoadingServer(true);
    mothershipApi
      .listServers()
      .then((all) => {
        if (cancelled) return;
        setServer(all.find((s) => s.id === id) ?? null);
      })
      .catch(() => {
        // Non-fatal: degrade to the install-progress view (handles the
        // freshly-minted-server-not-in-cache-yet race too).
      })
      .finally(() => {
        if (!cancelled) setLoadingServer(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  // T-0311: a live server (ready/connected) never emits new checkpoints, so
  // opening the SSE stream just parks the operator on a perpetual "waiting"
  // empty state. Only stream while an install is still in flight
  // (pending/failed/unknown, or while we don't yet know).
  const isLive =
    !!server &&
    (server.install_state === "ready" || server.install_state === "connected");
  const shouldStream = !!id && !loadingServer && !isLive;

  useEffect(() => {
    if (!shouldStream || !id) return;
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
  }, [id, shouldStream]);

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

  // T-0311: loading state while we resolve install_state from the registry.
  if (loadingServer) {
    return (
      <div className="container py-4" style={{ maxWidth: 720 }}>
        <div className="mc-section-title">Mothership · server</div>
        <div className="mc-empty" style={{ marginTop: "1.5rem" }}>
          <div className="mc-empty-icon">◇</div>
          <div>Loading server…</div>
        </div>
      </div>
    );
  }

  // T-0311: already-installed → real detail view (the fix).
  if (isLive && server) {
    return <ServerDetail server={server} />;
  }

  // Install still in flight (pending/failed) or registry entry unavailable:
  // keep the checkpoint-progress stream + post-install /welcome handoff.
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

      {id && <InviteUserPanel serverId={id} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// T-0125: per-server "invite a user" surface. Mounts inside ServerProgress
// so the inviter can mint + watch the same checkpoint stream they're
// already on. Super-admin gate: the mothership router itself is gated on
// VITE_MOTHERSHIP, but the create_invite endpoint is auth'd (not super-
// admin gated server-side as of T-0026), so we double-up the gate on the
// FE by checking /api/me. `isSuperAdminFromMe` falls back to `is_admin`
// pre-T-0066.
// ---------------------------------------------------------------------------

function InviteUserPanel({ serverId }: { serverId: string }) {
  const [allowed, setAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getMyProfile()
      .then((me) => {
        if (cancelled) return;
        setAllowed(isSuperAdminFromMe(me));
      })
      .catch(() => {
        // Treat fetch failure as "not allowed" — the panel is hidden, the
        // user can still watch the install progress above.
        if (!cancelled) setAllowed(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!allowed) return null;
  return <InviteUserForm serverId={serverId} />;
}

function InviteUserForm({ serverId }: { serverId: string }) {
  const [targetUsername, setTargetUsername] = useState("");
  const [role, setRole] = useState<InviteRole>("non-admin");
  const [minting, setMinting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<MintedInvite | null>(null);

  async function onMint(e: React.FormEvent) {
    e.preventDefault();
    setMinting(true);
    setError(null);
    try {
      const out = await mothershipApi.createInvite(
        serverId,
        targetUsername.trim(),
        role,
      );
      setMinted(out);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMinting(false);
    }
  }

  return (
    <section
      style={{
        marginTop: "2rem",
        border: "1px solid var(--mc-border)",
        borderRadius: 4,
        padding: "1rem 1.1rem",
        background: "var(--mc-surface)",
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: "0.25rem" }}>
        Invite a user to this server
      </div>
      <div style={{ fontSize: 12, color: "var(--mc-text-dim)", marginBottom: "0.7rem" }}>
        Mint a one-shot invite URL for an additional Linux user. The URL is
        single-use, expires in 24 h, and is shown exactly once below —
        copy it before navigating away.
      </div>
      <form
        onSubmit={onMint}
        style={{ display: "grid", gap: "0.6rem", maxWidth: 420 }}
      >
        <label style={{ display: "grid", gap: 4, fontSize: 12, letterSpacing: "0.05em" }}>
          <span style={{ color: "var(--mc-text-dim)", textTransform: "uppercase" }}>
            target linux username
          </span>
          <input
            required
            value={targetUsername}
            placeholder="e.g. alice"
            onChange={(e) => setTargetUsername(e.target.value)}
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
          <span style={{ color: "var(--mc-text-dim)", textTransform: "uppercase" }}>
            role
          </span>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as InviteRole)}
            style={{
              padding: "6px 10px",
              background: "var(--mc-surface)",
              border: "1px solid var(--mc-border)",
              color: "var(--mc-text)",
              fontFamily: "var(--mc-mono)",
              fontSize: 13,
              borderRadius: 3,
            }}
          >
            <option value="non-admin">non-admin</option>
            <option value="admin">admin (bot-squad group)</option>
          </select>
        </label>
        {error && (
          <div className="mc-badge mc-badge-danger" style={{ alignSelf: "start" }}>
            {error}
          </div>
        )}
        <div>
          <button
            type="submit"
            disabled={minting || !targetUsername.trim()}
            className="mc-badge mc-badge-info"
            style={{
              padding: "8px 18px",
              cursor: minting ? "wait" : "pointer",
              background: "transparent",
              fontSize: 12,
            }}
          >
            {minting ? "minting…" : "mint invite →"}
          </button>
        </div>
      </form>
      {minted && <MintedInviteResult serverId={serverId} minted={minted} />}
    </section>
  );
}

function MintedInviteResult(props: {
  serverId: string;
  minted: MintedInvite;
}) {
  const { minted, serverId } = props;
  const [copied, setCopied] = useState(false);
  return (
    <div style={{ marginTop: "0.9rem", display: "grid", gap: "0.5rem" }}>
      <div style={{ fontSize: 12, color: "var(--mc-text-dim)" }}>
        Invite minted for{" "}
        <code style={{ fontFamily: "var(--mc-mono)" }}>{minted.target_username}</code>{" "}
        as <code style={{ fontFamily: "var(--mc-mono)" }}>{minted.role}</code>.
        Expires{" "}
        <code style={{ fontFamily: "var(--mc-mono)" }}>
          {minted.expires_at ?? "—"}
        </code>
        . Share the URL below; it's one-shot.
      </div>
      <div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: "0.25rem",
            gap: "0.5rem",
          }}
        >
          <span
            style={{
              fontSize: 11,
              color: "var(--mc-text-dim)",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
            }}
          >
            invite url (one-shot, 24h)
          </span>
          <button
            type="button"
            onClick={() => {
              navigator.clipboard
                .writeText(minted.install_url)
                .then(() => {
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1500);
                })
                .catch(() => setCopied(false));
            }}
            className={copied ? "mc-badge mc-badge-ok" : "mc-badge mc-badge-info"}
            style={{
              padding: "2px 10px",
              cursor: "pointer",
              background: "transparent",
              fontSize: 11,
            }}
          >
            {copied ? "copied" : "copy"}
          </button>
        </div>
        <pre
          className="mc-code-block"
          data-testid="minted-invite-url"
          style={{
            padding: "0.6rem 0.8rem",
            margin: 0,
            background: "var(--mc-bg)",
            border: "1px solid var(--mc-border)",
            borderRadius: 3,
            overflowX: "auto",
            fontSize: 12,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {minted.install_url}
        </pre>
      </div>
      <div style={{ fontSize: 12 }}>
        <Link
          to={`/m/servers/${encodeURIComponent(serverId)}`}
          style={{ textDecoration: "none" }}
        >
          Watch invitee join in real time on this checkpoint stream →
        </Link>
      </div>
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
      {/* T-0336 (reframe-operator-paradigm): the cross-server AllProjects
          fleet console lives HERE now, under the admin `/m/` area — it is no
          longer the operator's default landing frame (`/` is the single-
          project brain via HomeRedirect). Reached via the super-admin FLEET
          sidebar entry. (Was T-0055: `/m` redirected to the unified `/`.) */}
      <Route index element={<AllProjects />} />
      <Route path="servers/add" element={<AddServerWizard />} />
      <Route path="servers/:id" element={<ServerProgress />} />
      {/* T-0068: cross-server per-project view. AllProjects' peer-server
          cards link here so a click on a peer project pulls the same
          board UI but against the peer's API via the mothership proxy. */}
      <Route
        path="servers/:server_id/p/:slug"
        element={<MothershipProject />}
      />
      {/* T-0087: mothership-only releases tab. Route lives here so the
          whole module + its chunk gets tree-shaken from detached builds
          via the same VITE_MOTHERSHIP gate in App.tsx. */}
      <Route path="releases" element={<Releases />} />
      {/* T-0113: super-admin directory of GlobalUsers. Backed by
          /api/m/users (T-0066). Empty until the first registry mint. */}
      <Route path="users" element={<Users />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
