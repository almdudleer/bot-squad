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

// T-0512 (Process Paradigm M9 / Part A). When a task is split into subtasks it
// becomes ABSTRACT: its progress is derived from its children — "the task
// becomes more abstract and dependent on actual work being done in terms of its
// subtasks" (SOURCE-VERBATIM Part A). Pure rollup over the children's canonical
// states (SSOT mirrored in api/app/canonical_status.derive_parent_status):
//   all done → done; all validating/done → validating; any started →
//   in-progress; none started → backlog. No children → null (not abstract).
// Unknown child statuses are ignored (defensive).
export function deriveParentStatus(childStatuses: string[]): CanonicalState | null {
  const canon = childStatuses
    .filter((s): s is Task["status"] => s in CANONICAL_STATE)
    .map((s) => CANONICAL_STATE[s]);
  if (canon.length === 0) return null;
  if (canon.every((c) => c === "done")) return "done";
  if (canon.every((c) => c === "validating" || c === "done")) return "validating";
  if (canon.some((c) => c === "in-progress" || c === "validating" || c === "done"))
    return "in-progress";
  return "backlog";
}
