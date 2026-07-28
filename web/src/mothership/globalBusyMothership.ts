/**
 * T-0064 — mothership-side data fetcher for the GlobalBusyIndicator.
 * Lives in `mothership/` so the detach build never imports it (Shell.tsx
 * dynamic-imports this module only when VITE_MOTHERSHIP === "1", and Vite
 * tree-shakes the chunk on detach).
 *
 * Fan-out shape: per-server registry → per-server project list → per-
 * project sessions. Failed servers contribute a `{ok:false}` envelope
 * and the indicator's aggregator drops them without blanking the rest.
 */
import { apiFor, fanOut, mothershipApi } from "./api";
import type {
  FanResult,
  ProjectSessions,
} from "../components/globalBusyHelpers";
import type { Project } from "../api";

export async function fanOutInFlight(): Promise<FanResult[]> {
  const servers = await mothershipApi.listServers();
  const ready = servers.filter((s) => s.install_state === "ready");
  const results = await fanOut(
    ready.map((s) => s.id),
    async (serverId): Promise<ProjectSessions[]> => {
      const meta = ready.find((s) => s.id === serverId);
      // T-0130/T-0312: every ready server — including the mothership's own
      // self-server entry — is reached through the proxy. The prior is_self
      // short-circuit to the local single-install API worked around a
      // self-proxy 503 that WS-3-BE fixed in T-0312, so it is now redundant.
      const projects = await apiFor(serverId).call<Project[]>("/api/projects");
      const perProject = await Promise.all(
        projects.map(async (p): Promise<ProjectSessions> => {
          // T-0763: use the NAMED `sessions` method, not the raw `call` escape
          // hatch. The raw call typed the body as SessionRow[] and handed it
          // straight through, but the server returns the T-0601 envelope
          // `{sessions, errors}` — so `ProjectSessions.sessions` was an OBJECT
          // and `inFlightRowsFromProject`'s for..of threw "not iterable" on
          // every tick. The throw landed inside the indicator's own catch, so
          // on a mothership build the fleet busy-indicator silently showed
          // nothing, forever, with no console error. `apiFor(id).sessions()`
          // runs normalizeSessionsPayload — the same normalization the local
          // fetcher already gets from `api.sessions()`.
          const sessions = await apiFor(serverId).sessions(p.slug);
          return {
            serverId,
            serverName: meta?.display_name ?? serverId,
            projectSlug: p.slug,
            sessions,
          };
        }),
      );
      return perProject;
    },
  );
  return results.map((r): FanResult =>
    r.ok
      ? { serverId: r.serverId, ok: true, projects: r.data }
      : { serverId: r.serverId, ok: false, error: r.error },
  );
}
