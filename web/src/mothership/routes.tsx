import { useEffect, useMemo, useState } from "react";
import { Routes, Route, Link, Navigate, useParams } from "react-router-dom";
import {
  mothershipApi,
  type AttachedServer,
  type Checkpoint,
  type InviteRole,
  type MintedInvite,
} from "./api";
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
      {/* T-0055: /m is no longer the all-projects view — the unified `/`
          owns that. Redirect for backwards-compat with any internal
          bookmarks; the wizard + install-progress sub-routes still live
          here because the install flow URLs are quoted in scripts/docs. */}
      <Route index element={<Navigate to="/" replace />} />
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
