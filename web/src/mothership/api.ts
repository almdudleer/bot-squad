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
import type { Project } from "../api";

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

export const mothershipApi = {
  listServers: () => call<AttachedServer[]>("/api/m/servers"),

  /** Mint a pending server entry + an install_token (24h TTL). The
   *  install_token plaintext is returned ONLY here — subsequent
   *  `listServers()` calls strip token hashes from the public projection. */
  createServer: (display_name: string, base_url: string) =>
    call<NewServer>("/api/m/servers", {
      method: "POST",
      body: JSON.stringify({ display_name, base_url }),
    }),

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

export type ServerApi = {
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
    call<T>(proxyUrl(serverId, path), init);
  return {
    call: fwd,
    projects: () => fwd<Project[]>("/api/projects"),
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
