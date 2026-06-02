// T-0104 — shared session-status formatter.
//
// One canonical enum used by every UI surface that shows a session
// state: `running | idle | paused | suspended`. The Sessions board
// card and the task detail page used to render different labels
// ("running" vs "active") for the same row; this util makes them
// agree.
//
// Source-of-truth precedence:
//   1. `activity` (worker-derived, jsonl mtime probe via /sessions).
//   2. Fallback: map raw md `status` (used when the row came from
//      /backlog's task.session, which doesn't yet enrich with the
//      activity probe). md=active is best-approximated as "running";
//      Sessions page sees the real derivation.

export type SessionActivity = "running" | "idle" | "paused" | "suspended";

const ACTIVITY_VALUES: ReadonlySet<string> = new Set([
  "running",
  "idle",
  "paused",
  "suspended",
]);

export function sessionActivity(
  src: { activity?: string | null; status?: string | null } | null | undefined,
): SessionActivity {
  if (!src) return "suspended";
  const a = src.activity;
  if (a && ACTIVITY_VALUES.has(a)) return a as SessionActivity;
  const s = src.status;
  if (s === "active") return "running";
  if (s === "paused") return "paused";
  return "suspended";
}

/** Single-character glyph paired with the label in inline pills. */
export function sessionGlyph(activity: SessionActivity): string {
  switch (activity) {
    case "running":
      return "●";
    case "idle":
      return "○";
    case "paused":
      return "◌";
    case "suspended":
      return "◌";
  }
}

/** Human-readable label (lowercase, matches the enum literal). */
export function sessionLabel(activity: SessionActivity): string {
  return activity;
}

/** True when the activity is the green "live & writing" state. */
export function isRunning(activity: SessionActivity): boolean {
  return activity === "running";
}

// ---------------------------------------------------------------------------
// T-0141 — authoritative session role.
//
// The role was historically inferred client-side as "no task_id ⟹ teamlead",
// which leaked nearly every task-less agent-teams session as a teamlead. The
// worker now derives an authoritative `role` (sessions.py::_derive_role); the
// UI prefers it and, when the field is absent (a pre-T-0141 worker), defaults
// to dev — T-0175: never re-introduce the teamlead leak via the fallback.
// ---------------------------------------------------------------------------
export type SessionRoleName = "teamlead" | "dev" | "operator";

export function sessionRole(
  src: { role?: string | null; task_id?: string | null } | null | undefined,
): SessionRoleName {
  if (!src) return "dev";
  const r = src.role;
  if (r === "teamlead" || r === "dev" || r === "operator") return r;
  // T-0175: a missing/unknown role defaults to dev, never teamlead. The old
  // "task-less ⟹ teamlead" fallback leaked nearly every finished dev (task_id
  // cleared to ~) as a teamlead.
  return "dev";
}

/** Human-readable role label for the sessions table Role column. */
export function sessionRoleLabel(role: SessionRoleName): string {
  switch (role) {
    case "teamlead":
      return "Teamlead";
    case "operator":
      return "Operator";
    case "dev":
      return "Dev";
  }
}
