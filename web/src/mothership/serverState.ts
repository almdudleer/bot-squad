import type { AttachedServer } from "./api";

/**
 * T-0653: canonical non-terminal install-state derivation, shared by the
 * fleet card (`/m`, AllProjects.tsx) and the Connected-servers table
 * (`/m/users`, Users.tsx) so the two admin surfaces never disagree on
 * vocabulary for the SAME server row again (T-0331 dogfood: `/m` showed a
 * red "stalled"/"expired" badge for the linza install while `/m/users`
 * showed the same row as "pending" — two names for one state).
 *
 * A server that's been marked ``hold_reason`` (owner-set, see
 * `mothershipApi.holdServer`) is DELIBERATELY held pending explicit
 * stakeholder/owner action — e.g. a multi-week install gated on a stakeholder
 * GO decision tracked elsewhere. That is NOT the same failure class as a
 * genuinely dead/stalled install, so it must never be derived purely from
 * elapsed time: a hold always wins over the stale heuristic below.
 */

// T-0342: a PENDING/installing server card otherwise lingers forever as
// "install hasn't finished yet". An install that hasn't made progress in
// this long is treated as stalled — unless it's been explicitly held (see
// `isStaleInstall` below). We use the freshest activity timestamp we have
// (last_seen_at, else created_at) so a still-progressing install
// (heartbeating last_seen_at) never trips the gate.
export const STALE_INSTALL_MS = 30 * 60 * 1000; // 30 min

export type ServerStaleInput = Pick<
  AttachedServer,
  "install_state" | "created_at" | "last_seen_at" | "hold_reason"
>;

export function isStaleInstall(
  server: ServerStaleInput,
  now: Date = new Date(),
): boolean {
  // Only non-terminal installs can be "stalled"; `failed` already reads as a
  // terminal state via its own badge, and `ready` isn't an installing card.
  if (server.install_state === "ready" || server.install_state === "failed") {
    return false;
  }
  // T-0653: a deliberate hold is never "stalled", regardless of elapsed time.
  if (server.hold_reason) return false;
  const stamp = server.last_seen_at || server.created_at;
  if (!stamp) return false;
  const t = new Date(stamp).getTime();
  if (Number.isNaN(t)) return false;
  return now.getTime() - t >= STALE_INSTALL_MS;
}

/**
 * Display state for a server that hasn't reached `ready`/`failed` yet — the
 * vocabulary both `/m` and `/m/users` render for the same row. Falls back to
 * the raw `install_state` (e.g. "pending"/"connected") when neither held nor
 * stalled, so the fleet card keeps distinguishing those sub-states exactly as
 * it did before T-0653 — only the held/stalled classification is unified.
 */
export type PendingInstallState = "held" | "stalled" | string;

export function pendingInstallState(
  server: ServerStaleInput,
  now: Date = new Date(),
): PendingInstallState {
  if (server.hold_reason) return "held";
  if (isStaleInstall(server, now)) return "stalled";
  return server.install_state;
}

export function pendingStateBadgeClass(state: PendingInstallState): string {
  switch (state) {
    case "held":
      return "mc-badge mc-badge-info";
    case "stalled":
      return "mc-badge mc-badge-danger";
    default:
      return "mc-badge mc-badge-warn";
  }
}
