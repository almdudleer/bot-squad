import { useCallback, useEffect, useState } from "react";
import { api, type HealthResponse } from "../api";

/**
 * WorkerHealthPill — a FAILURE-ONLY chrome chip surfacing worker liveness
 * (T-0456 / next-wave #3).
 *
 * The deploy-time sha assert can't catch a worker that goes stale or dead AFTER
 * deploy (T-0436: "nothing flagged it"). The backend exposes that on
 * `GET /api/health` as `worker.health` — a string list PRESENT ONLY when
 * something is wrong (`dead_heartbeat` = hb missing/>300s stale; `sha_drift` =
 * worker boot sha != API image sha). When healthy the key is ABSENT.
 *
 * So this pill honors the no-green-noise close: it renders NOTHING while
 * healthy, and lights red only when `worker.health` is non-empty. Tooltip names
 * the flag(s). Safe-by-absence — a pre-T-0456 API (no health key) → no pill.
 */
const POLL_MS = 30_000;

const FLAG_LABEL: Record<string, string> = {
  dead_heartbeat: "worker down: dead_heartbeat (no recent heartbeat)",
  sha_drift: "worker stale: sha_drift (worker sha != API image)",
};

export type WorkerHealthView = {
  label: string;
  title: string;
} | null;

/**
 * Pure mapping from a /health payload to the pill view. Returns null (render
 * nothing) when healthy or when the payload lacks a non-empty `worker.health`.
 * Exported for unit testing.
 */
export function workerHealthView(health: HealthResponse | null): WorkerHealthView {
  const flags = health?.worker?.health;
  if (!Array.isArray(flags) || flags.length === 0) return null;
  // dead_heartbeat is the more severe signal — lead the label with it.
  const label = flags.includes("dead_heartbeat") ? "worker down" : "worker stale";
  const title = flags.map((f) => FLAG_LABEL[f] ?? `worker: ${f}`).join("; ");
  return { label, title };
}

export function WorkerHealthPill() {
  const [health, setHealth] = useState<HealthResponse | null>(null);

  const refresh = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      // Transient (e.g. API restarting): keep last good; next poll retries.
      // We do NOT light the pill on a fetch error — that's an API-reachability
      // issue, not a worker-health signal, and would be false-alarm noise.
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  const view = workerHealthView(health);
  if (!view) return null; // healthy → no pill (no green noise)

  return (
    <div className="mc-autoupdate-pill">
      <span
        className="mc-badge mc-badge-danger"
        role="status"
        title={view.title}
      >
        ⚠ {view.label}
      </span>
    </div>
  );
}
