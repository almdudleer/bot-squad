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
// T-0340 — canonical session-LIVENESS vocabulary (the count/rollup word).
//
// Dogfood T-0331 found the liveness vocabulary + counts diverging across
// surfaces: the sidebar said "running", the Sessions board said "alive", and
// Analytics said "active" — three words for the same idea, with "archived"
// folded under "suspended" in one place and split out in another. This is the
// SINGLE source every surface reads off so the labels (and the live count)
// can't drift again.
//
// TWO levels, kept distinct on purpose:
//   • activity sub-state (per row, the green-LED badge): running | idle |
//     paused | suspended — see `sessionActivity` above. "running" is reserved
//     for THIS sub-state and is never used as a category/count word.
//   • liveness CATEGORY (the rollup word, used for counts + section labels):
//       live      = alive in tmux: activity running | idle | paused, and not
//                   archived. Replaces the old "active" / "alive" wording.
//       suspended = registry-retained but the tmux window is closed (not
//                   archived, not live).
//       archived  = the orthogonal `archived` frontmatter flag — takes
//                   precedence (an archived row is never "live").
//
// The precedence below mirrors Sessions.tsx::isLiveSession EXACTLY (paused is
// live even when Team-1's running/idle-only `live` flag says otherwise), so
// `sessionLiveness(s) === "live"` and `isLiveSession(s)` always agree.
// ---------------------------------------------------------------------------
export type SessionLiveness = "live" | "suspended" | "archived";

export function sessionLiveness(
  src:
    | {
        activity?: string | null;
        status?: string | null;
        archived?: boolean | null;
        live?: boolean | null;
      }
    | null
    | undefined,
): SessionLiveness {
  if (src?.archived) return "archived";
  const a = sessionActivity(src);
  if (a === "paused") return "live";
  if (typeof src?.live === "boolean") return src.live ? "live" : "suspended";
  return a === "running" || a === "idle" ? "live" : "suspended";
}

/** Human-readable label for a liveness category (matches the enum literal). */
export function livenessLabel(cat: SessionLiveness): string {
  return cat;
}

// Analytics aggregates are keyed by the raw md `status` (not the activity
// probe), so the rollup happens at the status level: status `active` =
// activity running|idle and status `paused` = paused — both roll up to the
// `live` category. Exported so Analytics counts "live" off the same truth.
export const LIVE_STATUSES: ReadonlySet<string> = new Set(["active", "paused"]);

// ---------------------------------------------------------------------------
// T-0346 — per-session "waiting for the operator's input" predicate.
// T-0375 / audit item 1 (Fork-1 CLOSE 1): re-aligned with the backend canonical
// signal. The PROJECT-level quick-status (quick_status.py) rolls up to
// `needs-input` when there's no working session but ≥1 session is either
// `paused` (Ctrl-C'd, pane still open) OR `awaiting_input` (the PRECISE signal:
// sid in tg_stall.blocked_sids — the agent peer_send'd the operator and is
// blocked on a reply). T-0375 dropped the coarse `active_at_prompt` heuristic
// (it never decayed → a finished autonomous dev parked at ❯ stuck the pill on
// needs-input forever). This per-ROW mirror MUST match: paused OR awaiting_input
// — the row-mirror had drifted, still reading active_at_prompt. Keep in
// lock-step with quick_status.aggregate_project_status.
// ---------------------------------------------------------------------------
export function sessionNeedsInput(
  src:
    | {
        status?: string | null;
        awaiting_input?: boolean | null;
        archived?: boolean | null;
      }
    | null
    | undefined,
): boolean {
  if (!src || src.archived) return false;
  if (src.status === "paused") return true;
  return src.awaiting_input === true;
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
// T-0197: prod-teamlead + qa become first-class spawnable roles. Mirror the
// worker's `_derive_role` enum (operator|prod-teamlead|qa|teamlead|dev).
export type SessionRoleName =
  | "teamlead"
  | "dev"
  | "operator"
  | "prod-teamlead"
  | "qa";

export function sessionRole(
  src: { role?: string | null; task_id?: string | null } | null | undefined,
): SessionRoleName {
  if (!src) return "dev";
  const r = src.role;
  if (
    r === "teamlead" ||
    r === "dev" ||
    r === "operator" ||
    r === "prod-teamlead" ||
    r === "qa"
  )
    return r;
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
    case "prod-teamlead":
      return "Prod-TL";
    case "qa":
      return "QA";
    case "dev":
      return "Dev";
  }
}

// ---------------------------------------------------------------------------
// T-0041 — operator spawn-window normalisation.
//
// A session's role is derived from its WINDOW NAME marker (worker
// sessions.py::_derive_role + the SessionStart hook's bash mirror
// scripts/hooks/derive_role.sh). A window resolves to `operator` iff it is
// exactly "operator" or ends in "-operator" / "_operator" (case-insensitive),
// mirroring the worker's `_OPERATOR_WINDOW_RE = (?:^|[-_])operator$`.
//
// The New-session modal's Operator option normalises the user's window through
// `operatorWindow()` so picking "Operator" ALWAYS spawns a session that resolves
// to operator.md — never a silent dev (the exact bug class T-0041 fixes on the
// hook side).
// ---------------------------------------------------------------------------
const OPERATOR_WINDOW_RE = /(?:^|[-_])operator$/i;

/** True when `window` already carries the operator marker. */
export function isOperatorWindow(window: string): boolean {
  return OPERATOR_WINDOW_RE.test(window.trim());
}

/** Normalise a window so it resolves to the operator role (appends the marker). */
export function operatorWindow(name: string): string {
  const base = name.trim();
  if (!base) return "operator";
  return OPERATOR_WINDOW_RE.test(base) ? base : `${base}-operator`;
}

// ---------------------------------------------------------------------------
// T-0197 — prod-teamlead + qa spawn-window normalisation.
//
// Same pattern as operatorWindow: the New-session modal's Prod-TL / QA options
// normalise the user's window so picking the role ALWAYS spawns a session that
// resolves (via the worker _derive_role + the SessionStart hook) to
// prod-teamlead.md / qa.md, never a silent dev. The regexes mirror the worker's
// _PROD_TL_WINDOW_RE = (?:^|[-_])prod[-_](?:tl|teamlead)$ and
// _QA_WINDOW_RE = (?:^|[-_])qa$.
// ---------------------------------------------------------------------------
const PROD_TL_WINDOW_RE = /(?:^|[-_])prod[-_](?:tl|teamlead)$/i;

/** True when `window` already carries the prod-teamlead marker. */
export function isProdTeamleadWindow(window: string): boolean {
  return PROD_TL_WINDOW_RE.test(window.trim());
}

/** Normalise a window so it resolves to the prod-teamlead role. */
export function prodTeamleadWindow(name: string): string {
  const base = name.trim();
  if (!base) return "prod-tl";
  return PROD_TL_WINDOW_RE.test(base) ? base : `${base}-prod-tl`;
}

const QA_WINDOW_RE = /(?:^|[-_])qa$/i;

/** True when `window` already carries the qa marker. */
export function isQaWindow(window: string): boolean {
  return QA_WINDOW_RE.test(window.trim());
}

/** Normalise a window so it resolves to the qa role (appends the marker). */
export function qaWindow(name: string): string {
  const base = name.trim();
  if (!base) return "qa";
  return QA_WINDOW_RE.test(base) ? base : `${base}-qa`;
}
