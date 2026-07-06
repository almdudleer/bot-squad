/**
 * Mothership FE client (T-0023). Two surfaces:
 *
 *   `mothershipApi` — calls the centralization layer itself (server
 *      registry, cached project list). These live at `/api/m/*` on
 *      botsquad.dev.
 *
 *   `apiFor(serverId)` — calls a *target* server's single-install API
 *      via the proxy at `/api/m/servers/{id}/api/{rest}`. Every method
 *      mirrors the same-named method on `web/src/api.ts` so per-project
 *      pages can be ported by swapping `api.X(...)` → `apiFor(id).X(...)`
 *      with no other change. Methods are added on demand (T-0025 needs
 *      `projects` only; future mothership pages bring their own).
 *
 *   `fanOut(serverIds, fn)` — runs the same per-server call across N
 *      servers in parallel and returns a per-server envelope
 *      `{ serverId, ok, data | error }`. A failed server NEVER poisons a
 *      sibling result; T-0025's all-projects page consumes this shape
 *      directly.
 *
 * Detach-safety: this module is imported only from `mothership/`. The
 * default build's `App.tsx` resolves `MothershipRoutes` to `null` when
 * `VITE_MOTHERSHIP !== "1"`, so Vite tree-shakes the lazy chunk and the
 * single-install bundle emits zero mothership code.
 */
import type {
  AutopilotConfig,
  AutopilotStatus,
  CreateTaskBody,
  OperatorPauseResult,
  OperatorResumeResult,
  Project,
  ProjectApi,
  Task,
  TelemetryResponse,
  VisionFile,
  WorkerModel,
} from "../api";
import { normalizeSessionsPayload } from "../api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Per-server project status as cached in the mothership registry. */
export type ServerProject = {
  slug: string;
  display_name: string;
  /** Cached quick-status; the live shape is T-0016's. "idle" is the
   *  default until a sync stream lands. */
  status: "working" | "needs-input" | "idle" | string;
};

/** Public projection of an attached-server registry entry. Hash fields are
 *  stripped server-side; mothership UI never sees plaintext bearers. */
export type AttachedServer = {
  id: string;
  display_name: string;
  base_url: string;
  owner_user: string;
  created_at: string;
  install_state: "pending" | "connected" | "ready" | "failed" | string;
  install_token_expires_at: string | null;
  last_seen_at: string | null;
  projects_cache: ServerProject[];
  /**
   * T-0055: ``true`` on the mothership's own self-registered entry. Lets
   * the unified all-projects view at ``/`` mark "this server" distinctly
   * from attached peers. Server-side default is ``false``; the wire
   * projection always includes the key (registry rows pre-T-0055 still
   * deserialise via the dataclass default).
   */
  is_self?: boolean;
};

/** Envelope returned for each server in a `fanOut` call. */
export type FanOutResult<T> =
  | { serverId: string; ok: true; data: T }
  | { serverId: string; ok: false; error: string };

/** One-shot response from `POST /api/m/servers` — the plaintext
 *  install_token is returned ONCE here and never again. */
export type NewServer = {
  id: string;
  install_token: string;
  install_url: string;
  instructions_url: string;
  expires_at: string | null;
};

/** Invite role at mint time. The BE accepts the literal strings
 *  `"admin"` and `"non-admin"` only; anything else is rejected with a
 *  400. Mirrors the role check in `routes_mothership.create_invite`. */
export type InviteRole = "admin" | "non-admin";

/** One-shot response from `POST /api/m/servers/{srv_id}/invites` — the
 *  plaintext `invite_token` is returned ONCE here and never again (only
 *  the SHA-256 is persisted). T-0026 minted the endpoint; the FE
 *  surfacing is T-0125. */
export type MintedInvite = {
  server_id: string;
  invite_token: string;
  install_url: string;
  instructions_url: string;
  target_username: string;
  role: InviteRole;
  expires_at: string | null;
};

/** Checkpoint event over the per-server SSE channel (and as persisted in
 *  the JSONL log). The server stamps `received_at`; the installer's `ts`
 *  is advisory. */
export type Checkpoint = {
  checkpoint: string;
  status: "begin" | "done" | "failed";
  hostname: string | null;
  ts: string | null;
  received_at: string;
};

/** One active per-server access grant (T-0221 / T-0292). The owner of a
 *  server may grant other mothership users explicit, revocable access to
 *  see + enter it. Revoked rows are dropped server-side, so every row the
 *  FE sees is active. Mirrors `_active_grants` in routes_mothership.py. */
export type Grant = {
  username: string;
  granted_by: string;
  granted_at: string;
};

/** Envelope returned by all three grant-lifecycle routes — the full active
 *  grant list after the mutation, so the FE refreshes from the same call. */
export type GrantsResponse = {
  server_id: string;
  grants: Grant[];
};

/** Public projection of a GlobalUser registry row (T-0066 / T-0113).
 *  ``password_hash`` is stripped server-side. ``attached_servers`` is a
 *  follow-on (T-0129 BE) — until then the FE renders an em-dash. */
export type GlobalUser = {
  id: string;
  username: string;
  display_name: string;
  email: string;
  timezone: string;
  is_super_admin: boolean;
  created_at: string;
  attached_servers?: number;
};

// ---------------------------------------------------------------------------
// Release-feed surface (T-0087)
// ---------------------------------------------------------------------------

/** One manifest entry produced by prod.sh (T-0081), enriched server-side
 *  with `tarball_url` for direct download. Mirrors the shape returned by
 *  /api/releases/latest and /api/releases/_all. */
export type ReleaseEntry = {
  version: string;
  git_sha: string;
  created_at: string;
  tarball_path: string;
  tarball_url: string;
  sha256: string;
  notes: string;
};

/** Per-install telemetry row from /api/releases/_telemetry (T-0088).
 *  `installed_version` is null until the consumer's autoupdate poller has
 *  reported once. */
export type ReleaseTelemetryRow = {
  install_id: string;
  install_name: string;
  installed_version: string | null;
  last_check_at: string | null;
  last_apply_at: string | null;
  last_apply_outcome: string | null;
  current_git_sha: string | null;
};

/** Envelope from /api/releases/_notes_draft on success. The version is
 *  computed by the API mirroring prod.sh's vYYYY.MM.DD.N counter. */
export type NotesDraftResult = {
  ok: boolean;
  version: string;
  path: string;
};

/** Envelope from /api/projects/{slug}/deploy on success. Matches the
 *  worker's `deploy` action envelope verbatim. */
export type DeployResult = {
  ok: boolean;
  queue_id: string;
  queued_at: number;
};

// ---------------------------------------------------------------------------
// Low-level call helper
// ---------------------------------------------------------------------------

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("not authenticated");
  }
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Centralization-layer surface
// ---------------------------------------------------------------------------

// T-0133: in-flight dedup + short-TTL cache for `/api/m/servers`. The
// detail page mounts multiple components that each fetch the registry
// (ServerPicker, ServerProgress, GlobalBusyIndicator's mothership
// fan-out, AllProjects); without coalescing, a single page-load fires
// 3–4 identical requests in the first second and `networkidle` never
// settles. The cache is tiny (the public registry projection is small)
// and the TTL is short enough that an admin who just added a server in
// another tab sees it on the next mount. `createServer` and the per-
// page reload helpers invalidate the cache explicitly so a UI-driven
// mutation is reflected immediately. Module-level state is fine —
// React's component tree is single-rooted per build.
const SERVERS_CACHE_TTL_MS = 5_000;
let _serversCache: { at: number; data: AttachedServer[] } | null = null;
let _serversInFlight: Promise<AttachedServer[]> | null = null;

export function invalidateServersCache(): void {
  _serversCache = null;
  _serversInFlight = null;
}

async function listServersDeduped(): Promise<AttachedServer[]> {
  const now = Date.now();
  if (_serversCache && now - _serversCache.at < SERVERS_CACHE_TTL_MS) {
    return _serversCache.data;
  }
  if (_serversInFlight) return _serversInFlight;
  _serversInFlight = (async () => {
    try {
      const data = await call<AttachedServer[]>("/api/m/servers");
      _serversCache = { at: Date.now(), data };
      return data;
    } finally {
      _serversInFlight = null;
    }
  })();
  return _serversInFlight;
}

export const mothershipApi = {
  /** Cached + in-flight-deduped registry list. Multiple components on
   *  the same page see a single underlying fetch; the TTL is short
   *  (5s) so an external mutation is picked up on the next mount.
   *  `createServer` and `invalidateServersCache` clear the cache
   *  explicitly when the FE knows the registry changed. */
  listServers: () => listServersDeduped(),

  /** Mint a pending server entry + an install_token (24h TTL). The
   *  install_token plaintext is returned ONLY here — subsequent
   *  `listServers()` calls strip token hashes from the public projection.
   *  T-0133: invalidate the cache so the AllProjects / ServerPicker
   *  reload after a mint sees the new row immediately. */
  createServer: async (display_name: string, base_url: string) => {
    const out = await call<NewServer>("/api/m/servers", {
      method: "POST",
      body: JSON.stringify({ display_name, base_url }),
    });
    invalidateServersCache();
    return out;
  },

  /** T-0125: mint an invite token for an additional Linux user to join an
   *  existing install. The plaintext `invite_token` is returned ONCE in
   *  this response body — the registry only stores the SHA-256. Caller
   *  must surface the URL/expiry to the inviter immediately; there is no
   *  recovery path if the response is dropped. */
  createInvite: (
    serverId: string,
    target_username: string,
    role: InviteRole,
  ) =>
    call<MintedInvite>(
      `/api/m/servers/${encodeURIComponent(serverId)}/invites`,
      {
        method: "POST",
        body: JSON.stringify({ target_username, role }),
      },
    ),

  /** T-0414: deregister (delete) a server — the close for `createServer`.
   *  Owner-gated on the BE via the `require_manage` SSOT (T-0390/T-0412),
   *  so a non-owner 403s and the `is_self` self-server 409s (it self-
   *  resurrects on next boot). Invalidates the servers cache so the row
   *  disappears from the next `listServers()` without a hard reload. */
  deleteServer: async (serverId: string): Promise<{ removed: string }> => {
    const out = await call<{ removed: string }>(
      `/api/m/servers/${encodeURIComponent(serverId)}`,
      { method: "DELETE" },
    );
    invalidateServersCache();
    return out;
  },

  /** T-0113: list every GlobalUser. Super-admin only (403 otherwise) — the
   *  Users page surfaces the 403 as a "super-admin only" empty state. */
  listUsers: () => call<GlobalUser[]>("/api/m/users"),

  /** T-0292: list a server's ACTIVE access grants. OWNER-ONLY on the BE
   *  (`_require_owner`, 403 otherwise) — NOT a global-admin power: there is
   *  no god-mode here, a grantee/admin who isn't the owner gets a 403. The
   *  Users page only calls this for servers the viewer owns. */
  listGrants: async (serverId: string): Promise<Grant[]> => {
    const out = await call<GrantsResponse>(
      `/api/m/servers/${encodeURIComponent(serverId)}/grants`,
    );
    return out.grants;
  },

  /** T-0292: grant `username` access to a server. Owner-only. Returns the
   *  refreshed active-grant list (post-mutation), so the caller swaps state
   *  from the response without a follow-up list call. */
  createGrant: async (serverId: string, username: string): Promise<Grant[]> => {
    const out = await call<GrantsResponse>(
      `/api/m/servers/${encodeURIComponent(serverId)}/grants`,
      { method: "POST", body: JSON.stringify({ username }) },
    );
    return out.grants;
  },

  /** T-0292: revoke `username`'s grant on a server. Owner-only. Idempotent
   *  server-side; returns the refreshed active-grant list. */
  revokeGrant: async (serverId: string, username: string): Promise<Grant[]> => {
    const out = await call<GrantsResponse>(
      `/api/m/servers/${encodeURIComponent(serverId)}/grants/${encodeURIComponent(username)}`,
      { method: "DELETE" },
    );
    return out.grants;
  },

  projectsFor: (serverId: string) =>
    call<ServerProject[]>(
      `/api/m/servers/${encodeURIComponent(serverId)}/projects`,
    ),

  refreshProjects: (serverId: string) =>
    call<ServerProject[]>(
      `/api/m/servers/${encodeURIComponent(serverId)}/projects/refresh`,
      { method: "POST" },
    ),
};

// ---------------------------------------------------------------------------
// Release-feed client (T-0087)
// ---------------------------------------------------------------------------
//
// All four endpoints are mounted on the release-feed router; the two
// underscore-prefixed mothership ones (`_all`, `_notes_draft`) are gated
// server-side by MOTHERSHIP=1 + cookie auth. The detached single-install
// build's UI never imports this module (lazy-loaded under VITE_MOTHERSHIP),
// so reaching them from there would be a routing bug.

export const releasesApi = {
  /** Full release history newest-first. Returns an empty array if the
   *  manifest hasn't been cut yet (vs. 404 — the UI distinguishes
   *  "no releases" from "endpoint missing"). */
  all: () => call<ReleaseEntry[]>("/api/releases/_all"),

  /** Per-install version + apply-outcome snapshot for the installs grid. */
  telemetry: () => call<ReleaseTelemetryRow[]>("/api/releases/_telemetry"),

  /** Stage release notes for the next prod.sh cut. The server writes the
   *  notes to `data/bot-squad/releases/<v>.md` where <v> is the next
   *  vYYYY.MM.DD.N computed identically to prod.sh step 2. */
  saveNotesDraft: (notes: string) =>
    call<NotesDraftResult>("/api/releases/_notes_draft", {
      method: "POST",
      body: JSON.stringify({ notes }),
    }),

  /** Queue a deploy job for `bot-squad` on the mothership. The worker's
   *  prod.sh recipe (T-0081) consumes the staged notes file on cut. */
  cutDeploy: (reason: string) =>
    call<DeployResult>("/api/projects/bot-squad/deploy", {
      method: "POST",
      body: JSON.stringify({ target: "prod", reason }),
    }),
};

// ---------------------------------------------------------------------------
// Per-server proxy client — apiFor(serverId)
// ---------------------------------------------------------------------------

const API_PREFIX = "/api/";

function proxyUrl(serverId: string, apiPath: string): string {
  if (!apiPath.startsWith(API_PREFIX)) {
    throw new Error(
      `apiFor(${serverId}).call: path must start with /api/ (got ${apiPath})`,
    );
  }
  return `/api/m/servers/${encodeURIComponent(serverId)}/api/${apiPath.slice(
    API_PREFIX.length,
  )}`;
}

// T-0068: typed error so the cross-server route's wrapper can render
// "Connect this server first" vs. "Access denied" without scraping a string.
// Kinds map onto distinct mothership-proxy + upstream auth outcomes:
//   - not_connected: the mothership has no bearer for this peer (503 from
//     the proxy's bearer lookup; peer install hasn't burned its
//     install_token yet).
//   - access_denied: the peer rejected the bearer (401/403 upstream).
// "Other" upstream errors (502/5xx/etc.) flow through as a plain Error so
// the page can surface them via its existing error state.
export type ProxyErrorKind = "not_connected" | "access_denied";

export class ProxyError extends Error {
  kind: ProxyErrorKind;
  status: number;
  constructor(kind: ProxyErrorKind, status: number) {
    super(kind);
    this.kind = kind;
    this.status = status;
    this.name = "ProxyError";
  }
}

/**
 * Per-server `call()` variant. The shared `call` in this module redirects
 * to /login on any 401 — appropriate for mothership-direct endpoints but
 * wrong for the proxy, where 401 means "the *peer* rejected our bearer"
 * (the mothership session is fine; you wouldn't want a peer-side bearer
 * rotation to log you out of the mothership UI). We classify auth-relevant
 * statuses and rethrow other errors verbatim.
 */
async function proxyCall<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 503) {
    // The mothership-side proxy raises 503 with detail "server bearer not
    // available (server not connected)" — see routes_mothership.py.
    throw new ProxyError("not_connected", 503);
  }
  if (res.status === 401 || res.status === 403) {
    throw new ProxyError("access_denied", res.status);
  }
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

export type ServerApi = ProjectApi & {
  /** Generic escape hatch: takes the *upstream* single-install path
   *  (e.g. "/api/projects/foo/backlog") and routes it through the
   *  proxy. Use this for surface that hasn't been added as a named
   *  method below yet. */
  call: <T>(path: string, init?: RequestInit) => Promise<T>;
  /** Upstream `GET /api/projects`. Mirrors `api.projects()`. */
  projects: () => Promise<Project[]>;
};

export function apiFor(serverId: string): ServerApi {
  // async wrapper so a bad-path throw from proxyUrl surfaces as a
  // promise rejection — callers always await this, never inspect the
  // sync result.
  const fwd = async <T>(path: string, init?: RequestInit): Promise<T> =>
    proxyCall<T>(proxyUrl(serverId, path), init);
  return {
    call: fwd,
    projects: () => fwd<Project[]>("/api/projects"),

    // T-0068: full per-project surface (mirrors `api` 1:1 for the methods
    // Project.tsx + Sessions.tsx call). Every method's path matches the
    // singleton in `web/src/api.ts` — keep them in sync when adding new
    // upstream endpoints.
    backlog: (slug) => fwd<Task[]>(`/api/projects/${slug}/backlog`),
    // T-0512 (M9): subtasks of a task (parent_task === id).
    children: (slug, id) => fwd<Task[]>(`/api/projects/${slug}/backlog/${id}/children`),
    vision: (slug) => fwd<VisionFile[]>(`/api/projects/${slug}/vision`),
    // T-0601 (F5): normalize both server shapes (bare array from older
    // installs, {sessions, errors} envelope from current) — same contract as
    // the singleton in web/src/api.ts.
    sessions: (slug) =>
      fwd<unknown>(`/api/projects/${slug}/sessions`).then(
        (body) => normalizeSessionsPayload(body).rows,
      ),
    sessionsDetail: (slug) =>
      fwd<unknown>(`/api/projects/${slug}/sessions`).then(normalizeSessionsPayload),
    telemetry: (slug) => fwd<TelemetryResponse>(`/api/projects/${slug}/telemetry`),
    createTask: (slug, t: CreateTaskBody) =>
      fwd<Task>(`/api/projects/${slug}/backlog`, {
        method: "POST",
        body: JSON.stringify(t),
      }),
    patchTask: (slug, id, t) =>
      fwd<Task>(`/api/projects/${slug}/backlog/${id}`, {
        method: "PATCH",
        body: JSON.stringify(t),
      }),
    patchTaskPriority: (slug, id, priority) =>
      fwd<Task>(`/api/projects/${slug}/backlog/${id}/priority`, {
        method: "PATCH",
        body: JSON.stringify({ priority }),
      }),
    deleteTask: (slug, id) =>
      fwd(`/api/projects/${slug}/backlog/${id}`, { method: "DELETE" }),
    // T-0389/audit item 18: `## Comments` channel cut (dead addComment + orphaned
    // /comments route). The board comment kebab posts a Progress note instead.
    addProgress: (slug, id, sid, text) =>
      fwd<{ ok: boolean; task_id: string; line_appended: string }>(
        `/api/projects/${slug}/backlog/${id}/progress`,
        { method: "POST", body: JSON.stringify({ sid, text }) },
      ),
    // T-0437: pin/unpin proxied to the attached server like the other session
    // ops.
    pinSession: (slug, sid) =>
      fwd<{ ok: boolean; sid: string; pinned: boolean; pinned_by: string; pinned_at: string }>(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pin`,
        { method: "POST" },
      ),
    unpinSession: (slug, sid) =>
      fwd<{ ok: boolean; sid: string; pinned: boolean; was_pinned: boolean }>(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pin`,
        { method: "DELETE" },
      ),
    pauseSession: (slug, sid) =>
      fwd(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pause`,
        { method: "POST" },
      ),
    suspendSession: (slug, sid) =>
      fwd(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/suspend`,
        { method: "POST" },
      ),
    resumeSession: (slug, sid) =>
      fwd(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/resume`,
        { method: "POST" },
      ),
    // T-0389/audit item 6: bindTask is now on the shared ProjectApi surface so
    // the reuse/resume flow can thread the new task into the session.
    bindTask: (slug, sid, task_id) =>
      fwd<{ ok: boolean; sid: string; task_id: string; extras: string[] }>(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/bind/task`,
        { method: "POST", body: JSON.stringify({ task_id }) },
      ),
    archiveSession: (slug, sid) =>
      fwd<{ ok: boolean; sid: string; archived: boolean }>(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/archive`,
        { method: "POST" },
      ),
    unarchiveSession: (slug, sid) =>
      fwd<{ ok: boolean; sid: string; archived: boolean }>(
        `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/unarchive`,
        { method: "POST" },
      ),
    // T-0594 (T-0588b): spawnSession / devSpawnRequest / reuseCandidates
    // mirrors removed with the ProjectApi surface cut (Sessions page is
    // read + minimal lifecycle controls; spawning is TG/CLI-driven).
    peerSend: (slug, fromSid, to, text) =>
      fwd<{ ok: boolean; delivered_to: string[] }>(
        `/api/projects/${slug}/peer/send`,
        {
          method: "POST",
          body: JSON.stringify({ from_sid: fromSid, to, text }),
        },
      ),
    peerInboxRead: (slug, sid) =>
      fwd<{ ok: boolean; messages: string[]; count: number }>(
        `/api/projects/${slug}/peer/${encodeURIComponent(sid)}/read`,
        { method: "POST" },
      ),
    peerInboxWait: (slug, sid, timeoutSec) =>
      fwd<{ ok: boolean; ready: boolean; elapsed_sec: number }>(
        `/api/projects/${slug}/peer/${encodeURIComponent(sid)}/wait`,
        { method: "POST", body: JSON.stringify({ timeout: timeoutSec }) },
      ),
    // T-0153: autopilot — mirrors the singleton in web/src/api.ts.
    autopilotStatus: (slug) =>
      fwd<AutopilotStatus>(`/api/projects/${slug}/autopilot`),
    autopilotStart: (slug, cfg: AutopilotConfig) =>
      fwd<{ ok: boolean; key: string; target_sid: string; expires_at: string; spawned: boolean }>(
        `/api/projects/${slug}/autopilot/start`,
        { method: "POST", body: JSON.stringify(cfg) },
      ),
    autopilotStop: (slug, opts: { key?: string; target_sid?: string; reason?: string }) =>
      fwd<{ ok: boolean; key: string; status: string; exit_reason: string }>(
        `/api/projects/${slug}/autopilot/stop`,
        { method: "POST", body: JSON.stringify(opts) },
      ),
    // T-0620/T-0630: operator pause/resume + fleet model read/write, mirrors
    // the singleton in web/src/api.ts.
    operatorPause: (slug, reason?: string) =>
      fwd<OperatorPauseResult>(`/api/projects/${slug}/operator/pause`, {
        method: "POST",
        body: JSON.stringify(reason ? { reason } : {}),
      }),
    operatorResume: (slug) =>
      fwd<OperatorResumeResult>(`/api/projects/${slug}/operator/resume`, {
        method: "POST",
      }),
    getWorkerModel: (slug) =>
      fwd<WorkerModel>(`/api/projects/${slug}/worker/model`),
    putWorkerModel: (slug, model: string) =>
      fwd<WorkerModel>(`/api/projects/${slug}/worker/model`, {
        method: "PUT",
        body: JSON.stringify({ model }),
      }),
  };
}

// ---------------------------------------------------------------------------
// fanOut — failure-isolated parallel calls
// ---------------------------------------------------------------------------

export async function fanOut<T>(
  serverIds: string[],
  fn: (serverId: string) => Promise<T>,
): Promise<FanOutResult<T>[]> {
  return Promise.all(
    serverIds.map(async (serverId) => {
      try {
        const data = await fn(serverId);
        return { serverId, ok: true as const, data };
      } catch (e) {
        const error =
          e instanceof Error
            ? e.message
            : typeof e === "string"
              ? e
              : String(e);
        return { serverId, ok: false as const, error };
      }
    }),
  );
}
