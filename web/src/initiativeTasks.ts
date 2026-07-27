import type { Task } from "./api";
import { canonicalOf, type CanonicalState } from "./canonicalStatus";

// T-0295 (c): the Board's group-by-initiative query, extracted so the Vision
// page's expanded initiative row lists the SAME bound tasks the Board groups
// into that initiative's lane (T-0038/9). One definition, two callers — a
// second hand-rolled copy would be free to drift from the lane it claims to
// mirror.
//
// A task's binding is the raw `initiative` frontmatter value: a basename under
// vision/initiatives ("ui-polish.md"). Empty/missing = unattached.
export function taskInitiativeKey(t: Pick<Task, "initiative">): string {
  return (t.initiative ?? "").trim();
}

// Tasks bound to one initiative, keyed by its basename ("<stem>.md" — what the
// Board lanes key on). Exact match, same as the Board: a task tagged with a
// basename no initiative row owns stays visible in the Board's orphan lane
// rather than being silently folded in here.
export function tasksForInitiative(tasks: Task[], initiativeBasename: string): Task[] {
  const key = initiativeBasename.trim();
  if (!key) return [];
  return tasks.filter((t) => taskInitiativeKey(t) === key);
}

// Live work first, done last — the Board's own lane ordering rationale
// ("keeps the eye on live work"). Found in the T-0295 manual walkthrough: in
// raw id order the ui-polish row opened with 128 consecutive DONE rows, so the
// initiative's live work AND its body sat far below the fold. Stable within a
// stage (ascending id = chronological).
const STAGE_RANK: Record<CanonicalState, number> = {
  "in-progress": 0,
  validating: 1,
  backlog: 2,
  done: 3,
};

export function sortBoundTasks(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => {
    const d = STAGE_RANK[canonicalOf(a.status)] - STAGE_RANK[canonicalOf(b.status)];
    return d !== 0 ? d : a.id.localeCompare(b.id);
  });
}

export function splitDone(tasks: Task[]): { live: Task[]; done: Task[] } {
  return {
    live: tasks.filter((t) => canonicalOf(t.status) !== "done"),
    done: tasks.filter((t) => canonicalOf(t.status) === "done"),
  };
}
