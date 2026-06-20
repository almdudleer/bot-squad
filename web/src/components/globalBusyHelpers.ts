/**
 * T-0064 — pure helpers for the GlobalBusyIndicator. The polling loop
 * + popover live in the React component; everything that decides "is
 * this in-flight" / "what counts" / "how do failed peer fetches affect
 * the indicator" lives here so vitest can pin the behaviour without a
 * DOM.
 */
import type { SessionRow } from "../api";

/** A single in-flight row rendered in the popover. */
export type InFlightRow = {
  sid: string;
  taskId: string;
  projectSlug: string;
  /** null on detach (single server) so the renderer omits the server badge. */
  serverId: string | null;
  serverName: string | null;
};

/** Per-server fetched payload (one project's worth of sessions). */
export type ProjectSessions = {
  serverId: string | null;
  serverName: string | null;
  projectSlug: string;
  sessions: SessionRow[];
};

/** Per-server fan-out envelope — `data` carries multiple projects' worth
 *  of sessions, `error` is a single string for any failure (we treat the
 *  whole server as offline rather than tracking per-project failures
 *  separately — keeps the indicator simple). */
export type FanResult =
  | { serverId: string | null; ok: true; projects: ProjectSessions[] }
  | { serverId: string | null; ok: false; error: string };

/**
 * Is this session in-flight for the indicator's purpose?
 *
 * Definition (per T-0064 spec): status==="active" AND task_id != null AND
 * the session belongs to the current user. The BE already filters
 * non-admins' /sessions responses to only their own; we still defensively
 * check `owner` so an admin viewing the page doesn't see a phantom
 * indicator for someone ELSE's running task.
 *
 * When `myUsername` is null (we haven't loaded /me yet) we err on the
 * side of NOT lighting the indicator — false negative is preferable to a
 * false positive that flashes for the wrong user.
 */
export function isMyInFlight(
  s: SessionRow,
  myUsername: string | null,
): boolean {
  if (s.status !== "active") return false;
  if (!s.task_id) return false;
  if (!myUsername) return false;
  // owner is empty for pre-T-0080 legacy sessions; for those we trust the
  // BE-side filter (admins are the only ones who'd see foreign sessions,
  // and pre-T-0080 sessions are rare in practice).
  if (s.owner && s.owner !== myUsername) return false;
  return true;
}

/** Flatten one project's sessions into the renderable rows the popover
 *  consumes. */
export function inFlightRowsFromProject(
  payload: ProjectSessions,
  myUsername: string | null,
): InFlightRow[] {
  const out: InFlightRow[] = [];
  for (const s of payload.sessions) {
    if (!isMyInFlight(s, myUsername)) continue;
    out.push({
      sid: s.sid,
      taskId: s.task_id ?? "",
      projectSlug: payload.projectSlug,
      serverId: payload.serverId,
      serverName: payload.serverName,
    });
  }
  return out;
}

/**
 * Collapse N per-server fan-out envelopes into one indicator-state
 * payload. Failure isolation: a server whose fetch failed contributes
 * zero rows, NOT a wholesale "indicator off" — that way a single peer
 * timeout doesn't blank the indicator for an actually-active local task.
 * The error count is surfaced for an optional "1 peer unreachable" hint.
 */
export type IndicatorState = {
  rows: InFlightRow[];
  /** Number of fan-out envelopes that came back !ok — used to render a
   *  subtle "(N peers unreachable)" line in the popover. */
  failedServerCount: number;
};

export function aggregateIndicator(
  fanResults: FanResult[],
  myUsername: string | null,
): IndicatorState {
  const rows: InFlightRow[] = [];
  let failedServerCount = 0;
  for (const r of fanResults) {
    if (!r.ok) {
      failedServerCount += 1;
      continue;
    }
    for (const p of r.projects) {
      rows.push(...inFlightRowsFromProject(p, myUsername));
    }
  }
  return { rows, failedServerCount };
}

/**
 * T-0170 — what the header work-indicator should paint for a given count.
 *
 * Stakeholder feedback: the old "dot + bare number, dot-only at 0" was
 * unclear. The rules now:
 *   • count === 0 → NOT visible (no lonely dot at all).
 *   • count  >  0 → an explicit word-label ("3 busy") plus a hover
 *     tooltip explaining what the number counts.
 *
 * T-0340 — vocabulary: this indicator counts a DIFFERENT thing from the
 * per-project Sessions/Analytics "live" count. It is the operator's own
 * IN-FLIGHT task sessions (status active + a bound task_id) aggregated
 * ACROSS every project/server — a strict subset of "live" at a different
 * scope, so it legitimately shows a smaller number. It used to say "N
 * running", colliding with the per-session "running" LED on the Sessions
 * board and reading as if it were the same liveness count. We relabel it
 * "N busy" to reserve "running" for the per-session activity sub-state and
 * "live" for the liveness category — same data, unambiguous words.
 *
 * Pure so the rendering decision is unit-tested without a DOM (matches the
 * helpers-not-components test convention used throughout this file).
 */
export type IndicatorView = {
  visible: boolean;
  label: string;
  tooltip: string;
};

export function indicatorView(count: number): IndicatorView {
  if (count <= 0) {
    return { visible: false, label: "", tooltip: "" };
  }
  return {
    visible: true,
    label: `${count} busy`,
    tooltip: `${count} of your agent session${count === 1 ? "" : "s"} busy on a task right now across your projects — click for details`,
  };
}
