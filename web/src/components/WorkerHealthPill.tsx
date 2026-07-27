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
 *
 * T-0739 adds a THIRD state between "healthy" and "red". After T-0717 the drift
 * you see right after a deploy is usually the expected tail of a restart that
 * fired or was deferred — self-healing, nothing to do. Rendering that in the
 * same red chip as a genuinely stalled worker is how an alarm gets trained away.
 * So `restart_pending` renders MUTED ("worker restarting"), and only a real
 * `sha_drift`/`dead_heartbeat` stays red. Still no green noise: healthy renders
 * nothing at all, and the muted chip disappears the moment the restart lands.
 */
const POLL_MS = 30_000;

const FLAG_LABEL: Record<string, string> = {
  dead_heartbeat: "worker down: dead_heartbeat (no recent heartbeat)",
  sha_drift: "worker stale: sha_drift (worker sha != API image; nothing is coming to fix this on its own)",
  restart_pending: "worker restarting: sha differs but a restart is already pending — expected, converges on its own",
};

export type WorkerHealthView = {
  label: string;
  title: string;
  /** false → muted chip: an expected, self-healing state, not an alarm. */
  danger: boolean;
} | null;

function restartDetail(health: HealthResponse | null): string {
  const r = health?.worker?.restart;
  if (!r) return "";
  const kind = r.state === "in_flight" ? "restart in flight" : "restart deferred";
  // OVERDUE: the drift is flagged sha_drift, but a marker still explains WHY —
  // "a restart has been owed since 03:34 and never landed" is the one line
  // whoever R-0005 pages actually needs, and it's what nobody had on the
  // T-0717 night. Saying "NO restart is pending" here would be a lie.
  if (r.overdue) {
    const since = typeof r.since === "number" && r.since > 0
      ? new Date(r.since * 1000).toLocaleTimeString()
      : "an earlier deploy";
    const noun = r.state === "in_flight" ? "restart was launched" : "restart was deferred";
    return ` — NOTE: a ${noun} at ${since} and is now OVERDUE; it did not land on its own`;
  }
  if (typeof r.expected_by !== "number") return ` (${kind})`;
  const secs = Math.max(0, Math.round(r.expected_by - Date.now() / 1000));
  return ` (${kind}; converges within ~${secs}s)`;
}

/**
 * Pure mapping from a /health payload to the pill view. Returns null (render
 * nothing) when healthy or when the payload lacks a non-empty `worker.health`.
 * Exported for unit testing.
 */
export function workerHealthView(health: HealthResponse | null): WorkerHealthView {
  const flags = health?.worker?.health;
  if (!Array.isArray(flags) || flags.length === 0) return null;
  // dead_heartbeat is the more severe signal — lead the label with it.
  const down = flags.includes("dead_heartbeat");
  // Muted ONLY when restart_pending is the whole story. Any other flag present
  // (including a future one we don't know) keeps the chip red — an unrecognised
  // problem must never be softened by a restart that happens to be pending.
  const pendingOnly = flags.length === 1 && flags[0] === "restart_pending";
  const label = down ? "worker down" : pendingOnly ? "worker restarting" : "worker stale";
  // The restart detail rides along on the drift flags in BOTH directions: it
  // says "wait ~Ns" while pending, and "owed since X, OVERDUE" once it isn't.
  const drift = flags.includes("restart_pending") || flags.includes("sha_drift");
  const title =
    flags.map((f) => FLAG_LABEL[f] ?? `worker: ${f}`).join("; ") +
    (drift ? restartDetail(health) : "");
  return { label, title, danger: !pendingOnly };
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
        className={view.danger ? "mc-badge mc-badge-danger" : "mc-badge mc-badge-dim"}
        role="status"
        title={view.title}
      >
        {view.danger ? "⚠" : "⟳"} {view.label}
      </span>
    </div>
  );
}
