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
/**
 * T-0744: 10s, not the 30s this shipped with. The muted `restart_pending` state
 * is by construction SHORT — a successful worker restart now converges in the
 * 12-30s range (measured windows before the fix: 20/60/40/73s), so a 30s poll
 * could step straight over the window and never render the chip at all. A UI
 * state that is never observed is not much better than the red one it replaced.
 * `GET /api/health` is three stats and a small read, and 10s is already the
 * house cadence for live panels (ResourceCapsPanel, Sessions).
 */
const POLL_MS = 10_000;

const FLAG_LABEL: Record<string, string> = {
  dead_heartbeat: "worker down: dead_heartbeat (no recent heartbeat)",
  sha_drift: "API container behind: sha_drift (worker sha != API image; nothing is coming to fix this on its own)",
  // T-0824. Deliberately NOT worded as "sha drift": this is the running worker
  // executing code that is not what was deployed — the question the endpoint is
  // actually asked, and the one no arrangement of the other two shas could
  // answer. Measured live: the old flag was SILENT here (22 worker modules
  // stale, api and worker equal because neither container moved) and LOUD on a
  // benign api lag. An operator learning the signal by experience learned it
  // backwards.
  worker_stale: "worker NOT running the deployed code: worker_stale (the install tree has moved and this process has not restarted onto it)",
  // The instrument, not the subject. Says the payload CANNOT answer the
  // question — which must not render as the same silence a converged install
  // produces, or the defect returns one level up. Usually a worker older than
  // T-0824; clears on the next worker restart.
  install_sha_unknown: "cannot verify the deployed sha: install_sha_unknown (the worker is not publishing the install tree's HEAD, so 'is the worker running the deployed code?' is unanswerable here)",
  restart_pending: "worker restarting: sha differs but a restart is already pending — expected, converges on its own",
  deploy_pending: "deploy landing: sha differs but a deploy of that exact commit is in flight — expected, converges on its own",
};

/**
 * T-0754: the flags that are an EXPECTED, self-healing state rather than an
 * alarm. `deploy_pending` joins `restart_pending` because it is the same shape
 * — the API's own sha is stale for the seconds its container takes to come up
 * mid-deploy — and because the pill IS the surface where this ticket's harm
 * lands: a human reading an alarming chip on a deploy where nothing is wrong.
 * Measured window on the 75dc01a deploy: 62.3s of it.
 */
const CALM_FLAGS = new Set(["restart_pending", "deploy_pending"]);

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
 * T-0754: the deploy that explains the drift, when one does. Names the commit
 * and which side already reached it — "the worker is on 75dc01a and the API
 * hasn't come up on it yet" is the whole story, and it is the difference
 * between reading this chip as noise and reading it as a deploy in progress.
 */
function deployDetail(health: HealthResponse | null): string {
  const d = health?.worker?.deploy;
  if (!d) return "";
  const sha = typeof d.target_sha === "string" ? d.target_sha.slice(0, 7) : "?";
  const side = d.converged === "api" ? "the API" : "the worker";
  if (d.overdue) {
    const since = typeof d.since === "number" && d.since > 0
      ? new Date(d.since * 1000).toLocaleTimeString()
      : "an earlier request";
    return ` — NOTE: a deploy of ${sha} has been in flight since ${since} and is now OVERDUE; it has not converged`;
  }
  return ` (deploy of ${sha} landing; ${side} is already on it)`;
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
  // Muted ONLY when a self-healing flag is the WHOLE story. Any other flag
  // present (including a future one we don't know) keeps the chip red — an
  // unrecognised problem must never be softened by a restart or deploy that
  // happens to be in flight.
  const pendingOnly = flags.length === 1 && CALM_FLAGS.has(flags[0]);
  // T-0824: `install_sha_unknown` on its own is NOT "worker stale" — the whole
  // content of that flag is that nobody can tell. Labelling it as staleness
  // would assert the very thing the payload just said it cannot establish, and
  // labelling it as healthy would recreate the indistinguishable pair. It gets
  // its own word, and it stays red: an unanswerable question on the deploy path
  // is a real problem, not a softer one.
  const cannotTell = flags.length === 1 && flags[0] === "install_sha_unknown";
  const label = down
    ? "worker down"
    : cannotTell
      ? "deployed sha unknown"
      : !pendingOnly
        ? "worker stale"
        : flags[0] === "deploy_pending"
          ? "deploy landing"
          : "worker restarting";
  // The restart detail rides along on the drift flags in BOTH directions: it
  // says "wait ~Ns" while pending, and "owed since X, OVERDUE" once it isn't.
  // The deploy detail does the same, and both can appear on one bare sha_drift:
  // the API only omits a block it did not look at, so whichever explanations
  // are present are the ones it actually found.
  const drift =
    flags.includes("restart_pending") ||
    flags.includes("deploy_pending") ||
    flags.includes("sha_drift") ||
    // T-0824: a pending restart / landing deploy explains worker staleness for
    // exactly the same reason it explains an api lag, so the detail rides along
    // on this flag too — including the OVERDUE wording, which on this flag is
    // the headline: "a restart was owed since 03:34 and the worker is still on
    // the old code".
    flags.includes("worker_stale");
  const title =
    flags.map((f) => FLAG_LABEL[f] ?? `worker: ${f}`).join("; ") +
    (drift ? restartDetail(health) + deployDetail(health) : "");
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
