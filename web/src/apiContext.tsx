import { createContext, useContext, type ReactNode } from "react";
import { api, type ProjectApi } from "./api";

/**
 * T-0068: per-project API client context.
 *
 * Single-install pages used to import the global `api` singleton directly.
 * The mothership build now mounts the same pages under `/m/servers/:id/p/:slug`
 * and needs to swap in a proxy-backed client (`apiFor(server_id)`) without
 * touching the page code. Pages call `useApiClient()` and stay agnostic about
 * whether they're talking to the local install or a peer.
 *
 * Default = the global singleton, so any page rendered outside an explicit
 * provider keeps its pre-T-0068 behaviour.
 */
const ApiClientContext = createContext<ProjectApi>(api);

export function ApiClientProvider({
  value,
  children,
}: {
  value: ProjectApi;
  children: ReactNode;
}) {
  return (
    <ApiClientContext.Provider value={value}>
      {children}
    </ApiClientContext.Provider>
  );
}

export function useApiClient(): ProjectApi {
  return useContext(ApiClientContext);
}
