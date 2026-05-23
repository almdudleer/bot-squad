/**
 * T-0064 — top-header indicator showing whether ANY task is in-flight
 * RIGHT NOW for the logged-in user across all their projects on all
 * attached servers. Hover (or tap on mobile) reveals the per-task list.
 *
 * Polls every 8 seconds. On mothership builds the data fetch fans out
 * across attached servers via the lazy-imported
 * `mothership/globalBusyMothership` helper (the import lives behind the
 * VITE_MOTHERSHIP literal so detach builds tree-shake it). On detach the
 * fetcher iterates local /api/projects + per-project /sessions.
 *
 * Visible to every logged-in user — not super-admin gated. The pure
 * helpers + aggregator live in `globalBusyHelpers.ts` so vitest can
 * cover the in-flight definition + fan-out failure isolation without a
 * DOM (per the project's no-DOM-test convention).
 */
import { useEffect, useRef, useState } from "react";
import { api, type Project, type SessionRow } from "../api";
import {
  aggregateIndicator,
  type FanResult,
  type IndicatorState,
  type ProjectSessions,
} from "./globalBusyHelpers";

const POLL_INTERVAL_MS = 8000;
const IS_MOTHERSHIP_BUILD = import.meta.env.VITE_MOTHERSHIP === "1";

async function fetchLocalInFlight(): Promise<FanResult[]> {
  const projects = await api.projects() as Project[];
  const perProject = await Promise.all(
    projects.map(async (p): Promise<ProjectSessions> => {
      const sessions = (await api.sessions(p.slug)) as SessionRow[];
      return {
        serverId: null,
        serverName: null,
        projectSlug: p.slug,
        sessions,
      };
    }),
  );
  return [{ serverId: null, ok: true, projects: perProject }];
}

export type GlobalBusyIndicatorProps = {
  /** From the Shell's /me load. When null we haven't identified the
   *  user yet — isMyInFlight returns false in that window. */
  myUsername: string | null;
};

export function GlobalBusyIndicator({ myUsername }: GlobalBusyIndicatorProps) {
  const [state, setState] = useState<IndicatorState>({
    rows: [],
    failedServerCount: 0,
  });
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const fanResults = IS_MOTHERSHIP_BUILD
          ? await (await import("../mothership/globalBusyMothership")).fanOutInFlight()
          : await fetchLocalInFlight();
        if (cancelled) return;
        setState(aggregateIndicator(fanResults, myUsername));
      } catch {
        // Top-level fetch failure (rare — only the registry listServers
        // or local /projects would throw outside a fan-out envelope).
        // Leave previous state in place rather than blinking off.
      }
    }
    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [myUsername]);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const lit = state.rows.length > 0;
  const cls = "mc-global-busy" + (lit ? " mc-global-busy-on" : "");

  return (
    <div
      className={cls}
      ref={rootRef}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        className="mc-global-busy-trigger"
        aria-label={
          lit
            ? `${state.rows.length} task${state.rows.length === 1 ? "" : "s"} in flight`
            : "no tasks in flight"
        }
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="mc-global-busy-dot" aria-hidden="true">●</span>
        {lit && <span className="mc-global-busy-count">{state.rows.length}</span>}
      </button>
      {open && (
        <div className="mc-global-busy-panel" role="dialog">
          {state.rows.length === 0 ? (
            <div className="mc-global-busy-empty">No tasks in flight.</div>
          ) : (
            <ul className="mc-global-busy-list">
              {state.rows.map((r) => (
                <li key={`${r.serverId ?? "."}/${r.projectSlug}/${r.sid}`}>
                  <div className="mc-global-busy-row">
                    <span className="mc-badge mc-badge-active">
                      {r.projectSlug}
                    </span>
                    {IS_MOTHERSHIP_BUILD && r.serverName && (
                      <span className="mc-badge mc-badge-info">
                        {r.serverName}
                      </span>
                    )}
                    <span className="mc-global-busy-task">{r.taskId}</span>
                    <span className="mc-global-busy-sid">{r.sid}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {state.failedServerCount > 0 && (
            <div className="mc-global-busy-warn">
              {state.failedServerCount} peer
              {state.failedServerCount === 1 ? "" : "s"} unreachable
            </div>
          )}
        </div>
      )}
    </div>
  );
}
