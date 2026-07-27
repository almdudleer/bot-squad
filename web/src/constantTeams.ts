import type { ConstantTeam, SessionRow } from "./api";

// T-0295 (b): constant-team health helpers — pure, so the Vision page can be
// tested without a browser.
//
// The tick state endpoint deliberately does NOT compute member count: the live
// session roster lives behind the worker and the Vision page already loads it.
// So the join happens here, mirroring the worker's `_live_member_count`
// (worker/bot_squad_worker/constant_teams.py) — the SAME pattern
// `computeTlBindings` uses to mirror `_find_owner`. Keep in lockstep:
//
//   - only active/paused sessions count (a suspended member is not staffing);
//   - a session belongs to the team if its `initiative` binding stem matches
//     the team stem (primary signal) OR its tmux window starts with the team's
//     window prefix (fallback for a member spawned before the binding settled).

const LIVE_STATUSES = new Set(["active", "paused"]);

// "initiatives/prod-support.md" | "prod-support.md" | "prod-support" →
// "prod-support". Mirrors the worker's `Path(...).stem` on the binding value.
export function initiativeStem(value: string | null | undefined): string {
  const raw = (value ?? "").trim();
  if (!raw || raw === "~") return "";
  const base = raw.slice(raw.lastIndexOf("/") + 1);
  return base.replace(/\.md$/, "");
}

export function constantTeamMembers(
  sessions: SessionRow[],
  team: Pick<ConstantTeam, "stem" | "window_prefix">,
): SessionRow[] {
  const prefix = (team.window_prefix ?? "").trim();
  return sessions.filter((s) => {
    if (!LIVE_STATUSES.has(String(s.status ?? ""))) return false;
    if (initiativeStem(s.initiative) === team.stem) return true;
    return Boolean(prefix) && String(s.window ?? "").startsWith(prefix);
  });
}

// Find the constant-team entry backing an initiative row, keyed by the row's
// basename ("<stem>.md"). Returns undefined when the initiative has no
// constant-team tick config — the honest "not staffed by the tick" case, which
// is what EVERY task-backed persistent initiative looks like today (T-0480
// moved initiatives into backlog tasks; the tick still reads
// vision/initiatives/*.md and was never repointed).
export function constantTeamFor(
  teams: ConstantTeam[],
  initiativeBasename: string,
): ConstantTeam | undefined {
  const stem = initiativeStem(initiativeBasename);
  if (!stem) return undefined;
  return teams.find((t) => t.stem === stem);
}

// Tick-state entries that don't map to any initiative row on the page — a
// state file whose config was deleted/archived, or a retired team. Surfaced in
// their own block instead of being dropped: an unreferenced json on disk that
// the UI never mentions is exactly the blind spot this endpoint exists to fix.
export function unmatchedConstantTeams(
  teams: ConstantTeam[],
  initiativeNames: string[],
): ConstantTeam[] {
  const stems = new Set(initiativeNames.map(initiativeStem).filter(Boolean));
  return teams.filter((t) => !stems.has(t.stem));
}
