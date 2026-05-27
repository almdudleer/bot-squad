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
import { api, type Project, type SessionRow } from "../api";

export async function fanOutInFlight(): Promise<FanResult[]> {
  const servers = await mothershipApi.listServers();
  const ready = servers.filter((s) => s.install_state === "ready");
  const results = await fanOut(
    ready.map((s) => s.id),
    async (serverId): Promise<ProjectSessions[]> => {
      const meta = ready.find((s) => s.id === serverId);
      // T-0130: the mothership's own self-server entry can't be reached
      // through the proxy (no peer bearer for itself — 503). Use the
      // local single-install API directly; the data is the same.
      const isSelf = meta?.is_self === true;
      const projects = isSelf
        ? await api.projects()
        : await apiFor(serverId).call<Project[]>("/api/projects");
      const perProject = await Promise.all(
        projects.map(async (p): Promise<ProjectSessions> => {
          const sessions = isSelf
            ? await api.sessions(p.slug)
            : await apiFor(serverId).call<SessionRow[]>(
                `/api/projects/${encodeURIComponent(p.slug)}/sessions`,
              );
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
