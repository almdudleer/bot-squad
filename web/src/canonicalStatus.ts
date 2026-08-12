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
  // T-0889 — see api/app/canonical_status.py for why in-progress.
  paused: "in-progress",
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

// T-0889: labels for the SIX internal statuses. Lives here, beside the canonical
// map, because this module is the status vocabulary — Project.tsx used to carry
// its own COLUMN_LABELS, and two lists of labels for one vocabulary is how the
// board ended up showing «двойные состояния» in the first place.
// `Record<Task["status"], string>` makes tsc refuse a new status without a label.
export const INTERNAL_LABELS: Record<Task["status"], string> = {
  planned: "Planned",
  open: "Open",
  in_progress: "In progress",
  paused: "Paused",
  totest: "To Test",
  reopened: "Reopened",
  closed: "Closed",
};

// T-0889: does this internal status ADD anything to the canonical column it sits
// in? True only when its canonical state has more than one internal status
// rolling into it — today that is `backlog` (planned/open/reopened); the other
// three are 1:1.
//
// This is the whole point of the card badge, and it is DERIVED rather than
// hardcoded: rendering the badge unconditionally would print "In progress" on a
// card inside the "In progress" column, i.e. it would recreate the exact
// duplicate-label defect he reported one level down. Deriving it also means a
// new status (e.g. `paused` rolling into in-progress) automatically starts
// showing the badge on BOTH statuses in that column, with no edit here.
export function statusRefinesItsColumn(status: Task["status"]): boolean {
  const canon = CANONICAL_STATE[status];
  let n = 0;
  for (const s of Object.keys(CANONICAL_STATE) as Task["status"][]) {
    if (CANONICAL_STATE[s] === canon) n++;
  }
  return n > 1;
}

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
