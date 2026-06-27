import type { Task } from "./api";

// Canonical 4-state task model (T-0479, Process Paradigm M3).
//
// The stakeholder's task model has FOUR states: backlog → in-progress →
// validating → done. The system stores SIX internal statuses (see
// Task["status"]); they are a refinement, each rolling up into exactly one
// canonical state. This is a non-destructive surfacing layer — no task is
// renamed. SSOT mirrored in api/app/canonical_status.py (keep in lockstep);
// the closed-set invariant is asserted in canonicalStatus.test.ts.

export type CanonicalState = "backlog" | "in-progress" | "validating" | "done";

// Internal status → canonical state.
export const CANONICAL_STATE: Record<Task["status"], CanonicalState> = {
  planned: "backlog",
  open: "backlog",
  reopened: "backlog",
  in_progress: "in-progress",
  totest: "validating",
  closed: "done",
};

// Display order — the user's stated lifecycle, left → right.
export const CANONICAL_STATES: readonly CanonicalState[] = [
  "backlog",
  "in-progress",
  "validating",
  "done",
];

export const CANONICAL_LABELS: Record<CanonicalState, string> = {
  backlog: "Backlog",
  "in-progress": "In progress",
  validating: "Validating",
  done: "Done",
};

export function canonicalOf(status: Task["status"]): CanonicalState {
  return CANONICAL_STATE[status];
}
