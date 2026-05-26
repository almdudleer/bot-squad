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
import type { Project, SessionRow } from "../api";

export async function fanOutInFlight(): Promise<FanResult[]> {
  const servers = await mothershipApi.listServers();
  const ready = servers.filter((s) => s.install_state === "ready");
  const results = await fanOut(
    ready.map((s) => s.id),
    async (serverId): Promise<ProjectSessions[]> => {
      const proxy = apiFor(serverId);
      const projects = await proxy.call<Project[]>("/api/projects");
      const perProject = await Promise.all(
        projects.map(async (p): Promise<ProjectSessions> => {
          const sessions = await proxy.call<SessionRow[]>(
            `/api/projects/${encodeURIComponent(p.slug)}/sessions`,
          );
          const meta = ready.find((s) => s.id === serverId);
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
