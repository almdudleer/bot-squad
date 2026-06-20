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
  indicatorView,
  type FanResult,
  type IndicatorState,
  type ProjectSessions,
} from "./globalBusyHelpers";
import { useAnchorRect, anchoredBelowLeft } from "./useAnchorRect";

const POLL_INTERVAL_MS = 8000;
const IS_MOTHERSHIP_BUILD = import.meta.env.VITE_MOTHERSHIP === "1";

// T-0065: hoist the mothership dynamic import to a module-top conditional
// so Rollup's tree-shaker can prove the import is unreachable on detach
// (mirrors App.tsx's MothershipRoutes pattern). An inline ternary inside
// poll() left the chunk in the detach bundle.
const fetchMothershipFanOut = IS_MOTHERSHIP_BUILD
  ? () =>
      import("../mothership/globalBusyMothership").then((m) =>
        m.fanOutInFlight(),
      )
  : null;

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
  const triggerRef = useRef<HTMLButtonElement>(null);
  // T-0140: render the panel with position:fixed off the trigger rect so the
  // sidebar's overflow clip can't crop it at the rail's right edge.
  const anchorRect = useAnchorRect(triggerRef, open);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const fanResults = fetchMothershipFanOut
          ? await fetchMothershipFanOut()
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

  // T-0170: the indicator is meaningless when nothing is in flight — the
  // stakeholder flagged the "lonely dot" at 0 as confusing. The pure
  // `indicatorView` decides visibility + the explicit "N busy" label (T-0340:
  // "busy", not "running" — this is the cross-project in-flight count, a
  // different scope from the per-project "live" count) + the explanatory
  // tooltip (unit-tested in globalBusyHelpers.test.ts).
  const view = indicatorView(state.rows.length);
  if (!view.visible) return null;
  const { label, tooltip: explain } = view;

  return (
    <div
      className="mc-global-busy mc-global-busy-on"
      ref={rootRef}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        ref={triggerRef}
        className="mc-global-busy-trigger"
        title={explain}
        aria-label={explain}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="mc-global-busy-dot" aria-hidden="true">●</span>
        <span className="mc-global-busy-count">{label}</span>
      </button>
      {open && (
        <div
          className="mc-global-busy-panel"
          role="dialog"
          style={anchoredBelowLeft(anchorRect)}
        >
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
