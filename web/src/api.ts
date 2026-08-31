import { redirectOn401 } from "./authRedirect";

// T-0714: the 401 -> /login policy now lives in ONE module (see authRedirect.ts
// for why). Re-exported here because this module is the frontend's canonical
// API surface and several callers/tests already import the predicates from it.
export {
  PUBLIC_ROUTES,
  isPublicRoute,
  redirectOn401,
  shouldRedirectOn401,
} from "./authRedirect";

type Json = Record<string, unknown> | unknown[];

// T-0138/T-0139: pages distinguish "the slug/task doesn't exist" (render a
// not-found panel) from transient network errors (offer a retry). The
// `call()` helper packs the HTTP status into the Error message — this is
// the canonical decoder for that shape.
export function isNotFoundError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  return /^API error 404\b/.test(msg);
}

// T-0276: a call() error message packs the HTTP body as
// ``API error <status>: <body>`` and FastAPI bodies are ``{"detail": "..."}``.
// This pulls out the human-readable detail (e.g. the 409 child-guard message
// that names the blocking children) so pages can surface it inline cleanly,
// falling back to the raw message for non-FastAPI/non-JSON bodies.
export function errorDetail(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const m = msg.match(/^API error \d+: (.*)$/s);
  if (m) {
    try {
      const parsed = JSON.parse(m[1]);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* body wasn't JSON — fall through to the raw message */
    }
  }
  return msg;
}

async function call<T = Json>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 401) redirectOn401(path);
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

export type Project = {
  slug: string;
  display_name: string;
  // T-0016 quick-status. Canonical enum locked with T-0025: any unknown
  // string is tolerated for forward-compat but won't be painted as a
  // coloured pill. See docs/architecture/D-0018-quick-status.md.
  status?: "working" | "needs-input" | "idle" | string;
  status_since?: string | null;
};

export type ProjectDetail = Project & {
  deploy_branch: string;
  prod_url: string;
  staging_url: string;
  dev_url: string;
  // T-0156: per-project Telegram binding. tg_chat may be a DM or group id;
  // tg_topic_id is the optional forum-thread id (null unless a group thread).
  tg_chat: string;
  tg_topic_id: number | null;
  counts: { backlog: number; vision: number; feedback: number; sessions: number };
};

export type Task = {
  id: string;
  title: string;
  status:
    | "planned"
    | "open"
    | "in_progress"
    | "paused"
    | "blocked_on_user"
    | "totest"
    | "reopened"
    | "closed";
  body: string;
  // Phase 7: parsed body sections — verbatim is the stakeholder's exact words.
  verbatim?: string;
  // T-0863: `## Executive summary` — ONE paragraph saying where the work
  // stands (progress made + what remains). Written for the stakeholder to read
  // on the board, not for the next session; the detail lives in `context`.
  summary?: string;
  context?: string;
  progress?: string;
  // T-0733: true when the ticket has no `## Verbatim request` heading, so
  // `verbatim` is the whole body via the parser's legacy fallback — a ticket
  // body, NOT a recorded ask. Label it as such; don't claim it's what the
  // stakeholder asked for.
  verbatim_is_legacy?: boolean;
  // Phase 8: int sort key for Kanban ordering. null = unset (sorts last).
  priority?: number | null;
  // T-0038: first-class linkage. `initiative` is a basename under
  // vision/initiatives/. Missing/null = unattached.
  initiative?: string | null;
  parent_task?: string | null;
  blocked_by?: string[] | null;
  // T-0512 (M9 / Part A): when a task is split into subtasks it becomes an
  // ABSTRACT parent. The /backlog list stamps `child_count` (how many subtasks
  // point here via parent_task) and `derived_status` (the parent's canonical
  // state rolled up from its children — see canonicalStatus.deriveParentStatus).
  // Absent on leaf tasks (a task with no children is not abstract).
  child_count?: number;
  derived_status?: "backlog" | "in-progress" | "validating" | "done" | null;
  // T-0172: ticket→doc mentions (list of D-NNNN). Kept in sync with each
  // doc's `related_tickets` by the docs link/unlink endpoints.
  related_docs?: string[] | null;
  // T-0105 + T-0106: append-only list of SIDs that worked on this task,
  // oldest first. Worker stamps on spawn/bind/resume; rendered as a panel
  // on TaskDetail. May be undefined for legacy tasks created before T-0105.
  session_history?: string[];
  // T-0291: sidecar SID→first-touch-ISO map written alongside each
  // session_history append, so a suspended/archived/legacy SID still shows a
  // real first-touch time on TaskDetail instead of `—` when the live-sessions
  // join misses. Absent for tasks last touched before T-0291.
  session_history_ts?: Record<string, string> | null;
  path: string;
  created?: string;
  updated?: string;
  from?: string;
  session?: {
    sid: string;
    status: "active" | "paused" | string;
    // T-0104: activity-derived enum. /backlog doesn't populate this
    // (the worker activity probe lives behind /sessions); FE pages may
    // enrich it client-side by joining with the sessions list. When
    // present it's the canonical display label; when absent the FE
    // falls back to mapping the raw `status` (see utils/sessionStatus).
    activity?: "running" | "idle" | "paused" | "suspended";
    // T-0404: the board enriches this from the live /sessions join so a card
    // can show the needs-input pill (sessionNeedsInput) for its bound process.
    awaiting_input?: boolean | null;
    // T-0437: the board enriches this from the same /sessions join so a card
    // can show a 📌 marker when its bound session is pinned.
    pinned?: boolean;
    pinned_by?: string | null;
  };
};

// T-0206: defend the SPA against a non-string text field slipping through from
// a malformed/legacy backlog md. The root cause (a frontmatter parser that read
// a colon-bearing `title` into a `{"Recheck model switch": "..."}` mapping) is
// fixed server-side, but a single object-valued `title` once rendered as a raw
// React child threw React error #31 (objects-are-not-valid-as-a-React-child)
// and white-screened the ENTIRE board — not just the one bad card. Coerce
// defensively at the data boundary so any future field-shape regression
// degrades to readable text instead of taking the whole page down.
export function coerceTaskText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  // A mapping (the exact #31 shape) → show its keys, which carry the human text
  // for the colon-in-title case (`{"Recheck model switch": "..."}` → the line).
  if (typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>);
    return keys.length ? keys.join(" ") : JSON.stringify(value);
  }
  return String(value);
}

export function normalizeTask(t: Task): Task {
  return { ...t, title: coerceTaskText(t.title as unknown) };
}

export type CreateTaskBody = {
  title: string;
  status?: Task["status"];
  // Either pass `verbatim_request` (preferred — composed into canonical body)
  // or `body` (raw, stored as-is for callers that know the convention).
  verbatim_request?: string;
  body?: string;
  // T-0608: POST /backlog runs the T-0577 near-duplicate gate (T-0600) and
  // 409s with candidates unless force is set. The board's "create anyway"
  // re-posts the same payload with force:true.
  force?: boolean;
};

// T-0608: the structured 409 detail the near-duplicate gate returns —
// {"detail": {"error": "near_duplicate", "message": ..., "candidates":
// [{id, title}, ...]}} packed by call() into `API error 409: <body>`.
export type NearDuplicateDetail = {
  message: string;
  candidates: { id: string; title: string }[];
};

// Decode a thrown call() error into the near-duplicate payload, or null for
// anything else (other 409s, other statuses, non-JSON bodies). Pure —
// exported for unit tests; the board's create modal branches on this.
export function parseNearDuplicate(err: unknown): NearDuplicateDetail | null {
  const msg = err instanceof Error ? err.message : String(err);
  const m = msg.match(/^API error 409: (.*)$/s);
  if (!m) return null;
  try {
    const detail = (JSON.parse(m[1]) as { detail?: unknown }).detail;
    if (!detail || typeof detail !== "object") return null;
    const d = detail as { error?: unknown; message?: unknown; candidates?: unknown };
    if (d.error !== "near_duplicate" || !Array.isArray(d.candidates)) return null;
    return {
      message: typeof d.message === "string" ? d.message : "",
      candidates: d.candidates.filter(
        (c): c is { id: string; title: string } =>
          !!c && typeof (c as { id?: unknown }).id === "string" &&
          typeof (c as { title?: unknown }).title === "string",
      ),
    };
  } catch {
    return null;
  }
}

export type VisionFile = {
  name: string;
  content: string;
  active?: boolean;
  finished?: boolean;
  // T-0354: persistent (standing responsibility) vs one-shot, set on
  // kind:initiative tasks; absent/"one-shot" for non-initiative vision files.
  // T-0295: also derived from a legacy `constant_team:` frontmatter flag on a
  // vision-FILE initiative, so every initiative row carries a kind.
  initiative_kind?: "persistent" | "one-shot";
};

// T-0295 (b): one constant team's tick state, as the worker's constant_team
// tick sees it (GET /api/projects/<slug>/constant-teams). Read-only health —
// no control affordance rides on this. Fields mirror
// api/app/routes_constant_teams.py.
export type ConstantTeam = {
  name: string;
  // Initiative file stem — the join key to a session's `initiative` binding.
  stem: string;
  // "initiatives/<file>.md", or null for an orphan state file with no config.
  initiative: string | null;
  configured: boolean;
  config_source: string | null;
  team_size: number | null;
  team_role: string | null;
  window_prefix: string;
  consume: string | null;
  consume_kind: "log" | "glob" | null;
  queue_depth: number | null;
  cursor_lines: number | null;
  last_spawn_at: number | null;
  last_spawn_iso: string | null;
  state_file: string;
  state_present: boolean;
  retired: boolean;
  finished: boolean;
  // False when the tick refuses to staff this team (retired / finished /
  // no consume source) — `not_staffable_reason` says which.
  staffable: boolean;
  not_staffable_reason: string | null;
};

export type ConstantTeamsResponse = {
  teams: ConstantTeam[];
  config_dir: string;
  state_dir: string;
};

// T-0283 (Pillar C / D-0029): the unified cross-store artifact kind. Every
// nestable artifact (doc, feedback theme) carries a `kind` so the Docs-section
// tree can pick an icon + route per store, and a `parent_doc_id` edge that may
// point at ANY artifact id (cross-store nesting). T-0671: use_case retired.
export type ArtifactKind = "doc" | "feedback";
// Shape of a `/children` row across all three stores (per D-0029): a mother doc
// may list doc children, a feedback theme may list evidence children, etc.
export type ArtifactChild = {
  id: string;
  title: string;
  kind: ArtifactKind;
  status?: string;
  parent_doc_id?: string | null;
};

// T-0283: feedback gains a tolerant frontmatter — `id` (the filename stem) and
// `parent_doc_id`. Legacy files (no frontmatter) read as root artifacts: the
// BE adds these keys, so they are optional for forward/backward compat.
export type FeedbackFile = {
  name: string;
  content: string;
  id?: string;
  parent_doc_id?: string | null;
  kind?: ArtifactKind;
  // item-12: close-state. The BE returns 'open' | 'promoted' | 'dismissed'
  // (absent → open). Closed items are hidden from GET /feedback unless
  // include_closed=true.
  status?: "open" | "promoted" | "dismissed" | string;
};

// T-0172: project docs system. Docs live at
// data/<slug>/docs/<category>/D-NNNN-<slug>.md.
export type DocSummary = {
  id: string;
  title: string;
  category: string;
  status: string;
  related_tickets: string[];
  // T-0234/T-0235: nesting key — the mother doc this artifact is attached to
  // (null/absent for a root/mother doc). The Docs left-rail renders children
  // indented under their mother.
  parent_doc_id?: string | null;
  // T-0283: kind discriminator for the cross-store tree (defaults to "doc").
  kind?: ArtifactKind;
  // T-0756: the doc's FILENAME stem, next to the id the BE derived from it
  // (`artifact_nesting.id_from_stem`). Doc bodies cross-link by relative
  // filename; this is what lets the renderer resolve one by LOOKUP instead of
  // re-deriving a python rule in TypeScript. Optional — a pre-T-0756 backend
  // omits it and `buildDocIndex` falls back to the id.
  stem?: string;
};
export type DocDetail = {
  id: string;
  title?: string;
  category: string;
  status?: string;
  created?: string;
  related_tickets?: string[];
  body: string;
  raw: string;
  path?: string;
  // T-0234/T-0235: nesting — the mother (parent_doc_id) and the attached
  // children the detail pane lists as "Attached artifacts".
  parent_doc_id?: string | null;
  // T-0283/D-0029: `child_artifact_ids` is the cross-store superset (doc + UC +
  // feedback children); `child_doc_ids` is the legacy doc-only key kept as a
  // backward-compatible fallback. Read the superset first.
  child_artifact_ids?: string[];
  child_doc_ids?: string[];
  kind?: ArtifactKind;
};

export type SessionRow = {
  sid: string;
  // T-0636/T-0654: slug-qualified display label ("[<slug>] <sid>"), computed
  // worker-side by sessions.sid_display_label and included on every
  // list_sessions() row. Display-only — routing/keys/attach commands stay on
  // the raw `sid`. Optional so a pre-T-0636 worker doesn't break the type
  // contract; render code falls back to `sid`.
  sid_label?: string;
  // Raw md/zombie-reclassified status. Kept for back-compat and for
  // action-button routing (Pause/Resume/Resurrect read this). Display
  // labels go through `activity` (T-0104) instead.
  status: "active" | "paused" | "suspended";
  // T-0104: worker-derived activity enum (jsonl mtime probe). Authoritative
  // for UI labels: a zombie `status: active` surfaces here as `suspended`,
  // and a live-but-quiet pane surfaces as `idle` rather than `running`.
  // Optional so a pre-T-0104 worker doesn't break the type contract; the
  // UI falls back via `utils/sessionStatus.sessionActivity`.
  activity?: "running" | "idle" | "paused" | "suspended";
  activity_at?: number | null;
  // T-0046/T-0346: worker-flagged "active pane idle at the prompt" — Claude
  // finished its turn and the human hasn't replied (jsonl quiet past
  // IDLE_AT_PROMPT_SECONDS). Feeds the project needs-input rollup
  // (quick_status.aggregate_project_status) and the per-row
  // `sessionNeedsInput` predicate that drives the home needs-input deep-link.
  // Optional: a pre-T-0046 worker omits it (treated as false).
  active_at_prompt?: boolean | null;
  // T-0285: explicit "blocked waiting on the operator" flag, sourced from the
  // tg_stall blocked marker (the agent peer_send-ed an operator and got no
  // reply). Distinct from the `active_at_prompt`/`sessionNeedsInput` heuristic —
  // this is the precise "this one is waiting on you" signal. Optional: a
  // pre-T-0285 worker omits it (treated as false).
  awaiting_input?: boolean | null;
  // T-0232 (Pillar A): worker-stamped liveness — true when the session is
  // alive in tmux (activity ∈ {running, idle}). The sessions VIEW filters on
  // this to show live-only rows; suspended/archived rows are retained in the
  // registry but dropped from the default view. Optional so a pre-T-0232
  // worker doesn't break the contract (UI falls back to the activity probe).
  live?: boolean;
  // T-0803: CLIENT-SIDE ONLY — never sent by the worker. Set by the sessions
  // page when a poll admitted a fan-out failure and this row was carried over
  // from the last good poll rather than re-observed. Marks a row as
  // "last known", so retaining it through a socket gap can never be mistaken
  // for a fresh observation.
  retained_stale?: boolean;
  window: string;
  cwd: string;
  started_at?: string | null;
  last_prompt_at?: string | number | null;
  claude_uuid?: string | null;
  task_id?: string | null;
  initiative?: string | null;
  // Phase 9: multi-binding. A dev may carry extra tasks; a TL extra
  // initiatives. Both are empty lists by default.
  extra_task_ids?: string[];
  extra_initiatives?: string[];
  paused_at?: string | null;
  suspended_at?: string | null;
  archived?: boolean;
  // T-0080: UI username that spawned the session. Empty for legacy
  // pre-T-0080 sessions; backend filters non-admins to only their own.
  owner?: string;
  // T-0157: linux user that owns the session's tmux server (the SID's user
  // segment, also stored explicitly). Drives the "user" group-by + the
  // per-lane user mark for multi-user projects. Empty only if unparseable.
  linux_user?: string;
  // T-0078: which tmux session the pane lives in (`<slug>` for legacy
  // panes, `<slug>-<initiative-stem>` for initiative TLs). Empty when
  // the worker can't determine it (pre-T-0078 md without backfill).
  tmux_session?: string;
  // T-0141: worker-derived authoritative role. Replaces the old
  // "task-less ⟹ teamlead" inference that leaked nearly every agent-teams
  // dive as a teamlead. Optional so a pre-T-0141 worker doesn't break the
  // contract; the UI falls back to the legacy inference when absent.
  // T-0727: deliberately a bare `string`, not a union. This is the WIRE shape —
  // whatever `sessions.py::_derive_role` emits today, including roles this
  // bundle predates. A union here was a third copy of that enum (it still read
  // teamlead|dev|operator, having missed qa, prod-teamlead AND
  // user-conversation), and it made an unknown role unrepresentable — which is
  // absurd for the field whose whole point is that it may be unknown. Narrow it
  // in ONE place: `sessionRole()` (utils/sessionStatus) validates against
  // SessionRoleName and applies the T-0175 dev fallback.
  role?: string;
  // T-0220: set true when the worker neutralized an elevated window-derived
  // role on a SUSPENDED row because the persisted cwd didn't match the
  // project (the role above is already the safe "dev" fallback). Lets the UI
  // surface the validation subtly. Absent/false on healthy rows.
  role_cwd_mismatch?: boolean;
  // T-0128: persisted spawn-time parent — the SID that requested this spawn
  // (operator→TL, TL→dev). Stamped into the session md at spawn time so the
  // session-tree is reliable across worker restarts. Preferred over the
  // task→initiative→TL heuristic when present; empty/absent on legacy sessions
  // (and agent-teams-spawned devs until the worker backfill fills it), in
  // which case the tree falls back to the heuristic.
  parent_sid?: string;
  // T-0437: per-project pin signal — a user marked this session as one they're
  // working closely with. Stamped onto each row by the API from the per-project
  // pin store (it does NOT change orchestration; it's a hint). The Processes
  // view surfaces pinned rows in a distinct section + badge, and the Board card
  // shows a 📌 marker on its bound session. Absent ⟹ not pinned.
  pinned?: boolean;
  pinned_by?: string | null;
  pinned_at?: string | null;
  // T-0444: WHY/WHO an auto-close happened, stamped by the worker on every
  // auto-suspend path (gc_sessions idle/no-pane, gc_drained_member, merge) so
  // the Processes status badge can make surprise auto-cleanup VISIBLE ("visible
  // close"). Absent on user/API suspends + legacy rows.
  suspend_source?: string | null;
  suspend_reason?: string | null;
};

// T-0601 (F5): one per-user worker socket that failed during the sessions
// fan-out. The API returns these alongside the rows so the UI can say "list
// is partial, worker for <user> unreachable" instead of a blank board.
export type WorkerFanoutError = { user: string; detail: string };

// T-0772: what the server says it did to the session list before sending it.
// "own" = the T-0080/T-0321 owner gate filtered it to the caller's own rows;
// "all" = unfiltered (admin). `null` = the server did not say — a legacy
// bare-array response, or a fan-out that failed. An UNKNOWN scope is not "own":
// a consumer must never claim the rows are the caller's when it cannot tell.
export type SessionsScope = "own" | "all" | null;

export type SessionsPayload = {
  rows: SessionRow[];
  errors: WorkerFanoutError[];
  scope: SessionsScope;
};

// Accepts both server shapes: the T-0601 envelope {sessions, errors} and the
// legacy bare array (older single-install servers reached via the mothership
// proxy). Exported for unit tests.
export function normalizeSessionsPayload(body: unknown): SessionsPayload {
  if (Array.isArray(body)) {
    return { rows: body as SessionRow[], errors: [], scope: null };
  }
  if (body && typeof body === "object") {
    const o = body as { sessions?: unknown; errors?: unknown; sessions_scope?: unknown };
    return {
      rows: Array.isArray(o.sessions) ? (o.sessions as SessionRow[]) : [],
      errors: Array.isArray(o.errors) ? (o.errors as WorkerFanoutError[]) : [],
      scope: o.sessions_scope === "own" || o.sessions_scope === "all"
        ? o.sessions_scope
        : null,
    };
  }
  return { rows: [], errors: [], scope: null };
}

// T-0210: per-session resource telemetry record (worker-sampled).
export type TelemetrySession = {
  sid: string;
  role?: string;
  task_id?: string | null;
  context: {
    tokens: number;
    pct: number;        // % of ceiling
    ceiling: number;    // tunable contract ceiling (300_000 since T-0857; 700_000 before), worker-stamped
    model?: string | null;
  };
  // T-0834: files/bytes/tokens_est are the WHOLE memory store — NOT loaded
  // context. Only `loaded` (MEMORY.md) enters a session; `recall` is read only
  // when a session recalls an entry. On the live install the total ran 13x the
  // loaded figure, so anything rendering "context cost" must use `loaded`.
  // `path` identifies the store, which is SHARED by every session on the
  // project path — it is not this session's alone.
  memory: {
    files: number; bytes: number; tokens_est: number;
    loaded?: { files: number; bytes: number; tokens_est: number };
    recall?: { files: number; bytes: number; tokens_est: number };
    path?: string;
  };
  output_tokens_cum?: number;
  rate_limited?: boolean;
  sampled_at?: string;
};

// T-0210: project-level quota burndown rollup. Quota is NOT live-queryable on
// Max plan — burn is estimated from output tokens; the projection only
// resolves when the operator sets an (optional) budget anchor.
export type TelemetryQuota = {
  burn_tokens_per_hr?: number | null;
  output_tokens_cum_total?: number;
  projected_exhaustion_at?: string | null;
  remaining_tokens?: number | null;
  anchor?: { budget_tokens: number; set_at?: string } | null;
  throttled?: boolean;
  rate_limit_429?: { count: number; last_at: string | null };
  sampled_at?: string;
};

// T-0389/audit items 7+22 (backend 4da24e7): the ENFORCED caps ride /telemetry
// under a top-level `caps` object (server-wide; the same in every project's
// payload). Empty {} when the worker is dead. Drives the caps strip
// ('12/15, throttled to 8') + the token meter (output_since_anchor / cap).
export type TelemetryCaps = {
  max_parallel_sessions?: number; // hard ceiling, 0 = unlimited
  effective_limit?: number;       // AIMD-depressed ceiling; 0 = unlimited
  live_sessions?: number;         // current live count (numerator vs ceiling)
  max_total_tokens?: number;      // token budget per quota period, 0 = unlimited
  output_since_anchor?: number;   // ENFORCED token meter numerator
  // T-0448 (#5): the AIMD backoff explainer (passthrough of persisted state) so
  // the 'throttled to N' badge can say WHY + since-when. null/absent = cold/disabled.
  backoff_reason?: string | null;            // e.g. 'pressure: 2 session(s) limited -> decrease to 8'
  backoff_last_pressure_at?: number | null;  // epoch secs of last pressure step
  backoff_last_ramp_at?: number | null;      // epoch secs of last ramp step
};

export type TelemetryResponse = {
  sessions: TelemetrySession[];
  quota: TelemetryQuota;
  caps?: TelemetryCaps;
};

export type RunRow = {
  id: string;
  target: string;
  status: "queued" | "processing" | "ok" | "fail";
  rc: number | null;
  reason: string;
  requested_by: string;
  queued_at: string | null;
  started_at: string | null;
  ended_at: string | null;
};

export type MessageRecord = {
  role: "user" | "assistant" | "tool" | "system";
  ts: string;
  text: string;
  tool_uses?: Array<{ id: string; name: string; input: Record<string, unknown> }>;
  tool_result?: { tool_use_id: string; output: string };
};

export type SchedulerJob = {
  id: string;
  next_run: string | null;
  trigger: string;
};

export type SchedulerState = {
  jobs: SchedulerJob[];
  worker_started_at: string | null;
  last_heartbeat_age_seconds: number | null;
};

export type TickLogEntry = {
  ts: string;
  msg: string;
};

// T-0153: autopilot — prompt-driven, time-boxed autonomous runs per target.
export type AutopilotKind = "team" | "session" | "project";

export type AutopilotConfig = {
  kind: AutopilotKind;
  ref?: string;
  prompt: string;
  early_exit?: string;
  duration_hours?: number;
  stall_minutes?: number;
  watchdog_minutes?: number;
};

export type AutopilotRun = {
  key: string;
  kind: AutopilotKind;
  ref: string;
  target_sid: string;
  prompt: string;
  early_exit: string;
  duration_hours: number;
  stall_minutes: number;
  watchdog_minutes: number;
  enabled: boolean;
  status: "running" | "expired" | "exited" | "stopped";
  exit_reason: string;
  created_by: string;
  started_at: string;
  expires_at: string;
  last_check_at: string | null;
  last_ping_at: string | null;
  pings: number;
  log: TickLogEntry[];
};

export type AutopilotStatus = {
  ok: boolean;
  slug: string;
  autopilots: AutopilotRun[];
};

export type Me = {
  username: string;
  linux_user: string;
  is_admin: boolean;
  tg_chat_id?: string | null;
};

export type MeProfile = {
  username: string;
  linux_user: string;
  is_admin: boolean;
  tg_chat_id: string | null;
};

export type UserRow = {
  username: string;
  linux_user: string;
  is_admin: boolean;
};

// T-0218 — per-project personal override raw value (null = not set / inherit).
export type ProjectTgChatId = {
  slug: string;
  tg_chat_id: string | null;
};

// T-0218 — 3-level resolved view. Each level reports its OWN raw value + a
// `set` flag (explicit override at that level); `effective`/`source` are the
// precedence winner computed server-side (project -> server -> global -> none).
export type NotificationLevelSource = "project" | "server" | "global" | "none";
export type NotificationsResolved = {
  levels: {
    global: { tg_chat_id: string | null; set: boolean };
    server: { server_id: string; tg_chat_id: string | null; set: boolean };
    project: { slug: string; tg_chat_id: string | null; set: boolean };
  };
  effective: { tg_chat_id: string | null; source: NotificationLevelSource };
};

export type SystemSettings = {
  tg: {
    bot_token_set: boolean;
    // T-0171: per-server default chat for the local bot (detached/standalone).
    default_chat_id: string;
    // T-0194: per-installation TG egress proxy (socks5/http/https), or "" for
    // direct. NOT mothership-locked — it's a host-network egress concern.
    proxy_url: string;
    quiet_hours_start_utc: number;
    quiet_hours_end_utc: number;
    // T-0171: when this server is an attached mothership consumer, the per-server
    // bot token + default chat are locked — notifications flow through the
    // mothership's @bot_squad_bot. UI greys the fields + shows a banner.
    managed_by_mothership: boolean;
    mothership_url: string | null;
  };
  session: { ttl: string };
  admin: { coordinator_user: string };
  // T-0239/T-0240: user-settable resource caps the system enforces at
  // spawn-time. 0 = unlimited (fresh/legacy install stays uncapped).
  // max_parallel_sessions caps simultaneously-live sessions; max_total_tokens
  // bounds aggregate usage (against the telemetry quota).
  // T-0408: idle_suspend_sec = suspend an idle-but-live dev after N seconds to
  // reclaim a slot (0 = OFF; in_progress devs are spared, T-0426). Optional for
  // a pre-T-0408 API.
  caps: { max_parallel_sessions: number; max_total_tokens: number; idle_suspend_sec?: number };
};

export type PutSystemSettingsBody = {
  tg?: {
    bot_token?: string;
    default_chat_id?: string;
    proxy_url?: string;
    quiet_hours_start_utc?: number;
    quiet_hours_end_utc?: number;
  };
  session?: { ttl?: string };
  admin?: { coordinator_user?: string };
  // T-0240: partial update OK — an omitted cap keeps its current value.
  caps?: { max_parallel_sessions?: number; max_total_tokens?: number; idle_suspend_sec?: number };
};

export type PutSystemSettingsResult = SystemSettings & {
  ok: boolean;
  restart_required: boolean;
};

// T-0089 — consumer-side autoupdate status surface for the header pill.
// On the mothership build (`VITE_MOTHERSHIP === "1"`) the server returns 404
// for every route; the caller (AutoupdatePill) tree-shakes itself away there.
export type AutoupdateAlert = {
  version: string;
  step: string;
  log_tail: string;
  occurred_at: string;
  retry_command: string;
  force_command: string;
};

// T-0456: /api/health worker-liveness channel. `worker.health` is FAILURE-ONLY
// — present (non-empty) ONLY when there's a problem; absent/empty when healthy
// (no green noise). Flags: 'dead_heartbeat' (worker hb missing/>300s stale) and
// 'sha_drift' (worker boot sha != API image sha). git_sha is the worker's
// frozen-at-boot sha (null until a worker built with the sha-writing heartbeat
// restarts). All fields optional so a pre-T-0456 API doesn't break the type.
export type HealthResponse = {
  ok?: boolean;
  version?: string;
  git_sha?: string | null;
  uptime?: number;
  // T-0824: the DEPLOYED sha — the install tree's HEAD, published by the worker
  // (the API container has no git tree to read). Always present; `git_sha` is
  // null with a `reason` when it could not be read, because an absent key would
  // put a consumer back to guessing. Together with `git_sha` (api container) and
  // `worker.git_sha` (running worker) these are the three sides, and every
  // question this endpoint is asked is a comparison between two of them.
  install?: {
    git_sha?: string | null;
    at?: number;
    reason?: "no_marker" | "unreadable_marker" | "malformed_marker";
  };
  worker?: {
    alive?: boolean;
    last_heartbeat?: number | null;
    git_sha?: string | null;
    health?: string[];
    // T-0739: present only alongside a drift flag — the restart the worker has
    // recorded as owed, so `restart_pending` can be told from bare `sha_drift`.
    restart?: {
      state?: "in_flight" | "deferred";
      since?: number;
      expected_by?: number;
      overdue?: boolean;
      reason?: string;
    } | null;
    // T-0754: present only alongside a drift flag, and only when the API found
    // an in-flight deploy whose recorded target_sha equals the side that has
    // ALREADY converged — so `deploy_pending` can be told from bare `sha_drift`
    // on a deploy that correctly skips the worker restart (no marker exists).
    deploy?: {
      state?: "in_flight" | "queued";
      slug?: string;
      queue_id?: string;
      target_sha?: string;
      /** Which side already reports target_sha; its counterpart is catching up.
       * T-0824 adds "install": the recipe ff-merged the tree and neither process
       * has caught up yet — the window a worker-only deploy lands in first. */
      converged?: "worker" | "api" | "install";
      since?: number;
      last_progress?: number | null;
      expected_by?: number;
      overdue?: boolean;
      reason?: string;
    } | null;
  };
};

export type AutoupdateStatus = {
  installed_version: string | null;
  last_check_at: string | null;
  next_check_at: string | null;
  last_apply_at: string | null;
  // Free-form so a future ``failed:<step>`` outcome doesn't break TS.
  last_apply_outcome: string;
  current_git_sha: string | null;
  paused: boolean;
  alert: AutoupdateAlert | null;
  pending_apply_version: string | null;
  mothership_url: string | null;
  poll_interval_seconds: number;
};

// T-0168: defensive coercion at the autoupdate data boundary, mirroring the
// `normalizeTask` #31 defense above. `/api/autoupdate/status` is consumer-side
// and can misbehave (a non-object body, a null, a payload missing
// `last_apply_outcome`). Read raw into `pickKind`/`pillLabel`, an unguarded
// `status.last_apply_outcome.startsWith(...)` (or `status.alert.version`) throws
// DURING RENDER — and with no error boundary that unmounts the WHOLE Shell, not
// just the pill (regression surfaced in the T-0167 walkthrough). Coerce here so
// a malformed/non-object/null payload becomes `null` (pill renders nothing) and
// every surviving field is forced to a safe type the render logic can't trip on.
function coerceNullableStr(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

export function normalizeAutoupdateStatus(raw: unknown): AutoupdateStatus | null {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const s = raw as Record<string, unknown>;
  const a = s.alert;
  const alert: AutoupdateAlert | null =
    a !== null && typeof a === "object" && !Array.isArray(a)
      ? {
          version: coerceNullableStr((a as Record<string, unknown>).version) ?? "",
          step: coerceNullableStr((a as Record<string, unknown>).step) ?? "",
          log_tail: coerceNullableStr((a as Record<string, unknown>).log_tail) ?? "",
          occurred_at: coerceNullableStr((a as Record<string, unknown>).occurred_at) ?? "",
          retry_command: coerceNullableStr((a as Record<string, unknown>).retry_command) ?? "",
          force_command: coerceNullableStr((a as Record<string, unknown>).force_command) ?? "",
        }
      : null;
  return {
    installed_version: coerceNullableStr(s.installed_version),
    last_check_at: coerceNullableStr(s.last_check_at),
    next_check_at: coerceNullableStr(s.next_check_at),
    last_apply_at: coerceNullableStr(s.last_apply_at),
    last_apply_outcome: coerceNullableStr(s.last_apply_outcome) ?? "",
    current_git_sha: coerceNullableStr(s.current_git_sha),
    paused: s.paused === true,
    alert,
    pending_apply_version: coerceNullableStr(s.pending_apply_version),
    mothership_url: coerceNullableStr(s.mothership_url),
    poll_interval_seconds:
      typeof s.poll_interval_seconds === "number" && Number.isFinite(s.poll_interval_seconds)
        ? s.poll_interval_seconds
        : 0,
  };
}

// T-0147: product-analytics (internal-usage) snapshot.
export type DayCount = { date: string; count: number };
export type Analytics = {
  slug: string;
  generated_at: string;
  window_days: number;
  // T-0360: trailing-week count for the deploys/week chart (differs from
  // window_days). Optional so a pre-T-0360 API doesn't break the type.
  deploy_weeks?: number;
  sessions: {
    total: number;
    archived: number;
    by_status: Record<string, number>;
    per_day: DayCount[];
  };
  tickets: {
    total: number;
    by_status: Record<string, number>;
    closed_per_day: DayCount[];
    time_to_close: {
      closed_measured: number;
      mean_days: number | null;
      median_days: number | null;
    };
  };
  deploys: {
    total: number;
    ok: number;
    fail: number;
    success_rate: number | null;
    last_deploy_at: string | null;
    per_week: { week: string; ok: number; fail: number }[];
  };
};

// T-0511 (M11-F11.4): the unified read-only system-transparency surface — the
// operator state-doc + session tree + backlog + quota, in one read, so a fresh
// operator/stakeholder can see where the system IS without talking to anyone.
export type Transparency = {
  slug: string;
  operator_state: {
    exists: boolean;
    content: string | null;
    updated_at: number | null;
    path: string;
  };
  sessions: SessionRow[];
  // T-0772: whether `sessions` above was owner-filtered. Absent on a pre-T-0772
  // server; see `SessionsScope`.
  sessions_scope?: SessionsScope;
  backlog: {
    counts: Record<string, number>;
    tasks: Task[];
  };
  quota: {
    max_in_progress: number; // 0 = unlimited
    in_progress: number;
    paused: boolean;
    initiatives: Record<
      string,
      { weight: number; priority: number; paused: boolean }
    >;
    // T-0828 / D-0069: the standing per-project DRIVE MODE. Optional because a
    // pre-T-0828 server does not send it — an older api must render as "unknown",
    // never as "no mode set" (that would be a claim about his settings that the
    // server never made).
    drive?: DriveMode;
  };
};

/**
 * T-0828 / D-0069 — the standing drive mode, as the transparency payload
 * carries it. Mirrors `worker/bot_squad_worker/pace.py:_normalized_drive` and
 * its api twin `routes_transparency.py:_normalized_drive`.
 *
 * `configured` is the field that answers the stakeholder's actual question
 * («какой режим драйва щас стоит»): an unset project and one explicitly set to
 * the widest scope BOTH read `scope: "all"`, and only this flag tells them
 * apart. `invalid` carries out-of-set stored values with the raw value intact —
 * the effective axis has already fallen back to its default, and the UI must
 * SAY so rather than render the fallback as the setting.
 */
export type DriveMode = {
  scope: string;
  stop_when: string;
  on_stop: string;
  set_by: string | null;
  set_at: string | null;
  source_text: string | null;
  configured: boolean;
  invalid: Record<string, unknown>;
};

// T-0620/T-0630: web operator controls — pause/resume the re-drive tick and
// read/change the fleet default model. Thin wrappers over the operator_pause/
// operator_resume/fleet_model_get/fleet_model_set worker actions (T-0474/
// T-0630); the response shapes mirror those actions' return values verbatim.
export type OperatorPauseResult = {
  ok: boolean;
  paused: { paused_by: string; reason: string; paused_at: number };
  was_already_paused: boolean;
};
export type OperatorResumeResult = {
  ok: boolean;
  was_paused: boolean;
};
// "" = no override — the fleet falls back to its built-in default.
export type WorkerModel = { model: string };

// T-0365: the project list drives the landing Picker, the switch-project
// dropdown, and HomeRedirect — each fetched /api/projects independently, so the
// list reloaded (and flashed blank) on every visit. `api.projects()` now
// populates a module-level cache; consumers seed their initial state from
// `cachedProjects()` for an instant paint, then call api.projects() to
// revalidate (stale-while-revalidate). Cleared on logout via clearProjectsCache.
//
// T-0763: that cache seeds the FIRST PAINT synchronously — it does not stop two
// consumers mounting in the same tick from each issuing their own request. On
// /p/:slug exactly that happened: useProjectExists is instantiated twice (the
// App.tsx route guard and the Shell's rail suppression), so every page load
// fetched /api/projects TWICE and never again. `_projectsInFlight` shares the
// pending request between concurrent callers.
//
// IN-FLIGHT ONLY — deliberately NOT a TTL cache (contrast mothership/api.ts's
// T-0133 `listServersDeduped`, which adds a 5s TTL). A TTL would change what
// every EXISTING caller gets: `api.projects()` is how Picker, HomeRedirect and
// GlobalBusyIndicator revalidate, and they are entitled to a fresh answer per
// call. Sharing a request that is already on the wire costs no freshness at
// all — the observed waste was purely concurrent, so this removes all of it
// and introduces no staleness window.
let _projectsCache: Project[] | null = null;
let _projectsInFlight: Promise<Project[]> | null = null;
export function cachedProjects(): Project[] | null {
  return _projectsCache;
}
export function clearProjectsCache(): void {
  _projectsCache = null;
  // Drop the shared request too. On logout a response that left BEFORE the
  // logout must not be handed to a post-logout caller, nor repopulate the
  // cache behind it (the currency guard in `projects()` enforces the latter).
  _projectsInFlight = null;
}

export const api = {
  // T-0456: typed so the worker-health pill can read worker.health/git_sha.
  health: () => call<HealthResponse>("/api/health"),
  me: () => call<Me>("/api/auth/me"),
  // T-0763: `fresh` bypasses the in-flight share. Needed by the ONE caller that
  // refetches to observe its own mutation (Picker's reload() after
  // createProject): joining a request that left before the POST would render
  // the list WITHOUT the project just created. Incidental concurrency has no
  // such ordering requirement and must not pay for a second round-trip.
  projects: (opts?: { fresh?: boolean }): Promise<Project[]> => {
    if (!opts?.fresh && _projectsInFlight) return _projectsInFlight;
    const p: Promise<Project[]> = call<Project[]>("/api/projects")
      .then((rows) => {
        // Currency guard: only the request that is STILL the shared one may
        // write the cache. Without it a superseded response (a `fresh` call
        // overtook it, or a logout cleared it) could land last and reinstate
        // a list the app has already moved on from.
        if (_projectsInFlight === p) _projectsCache = rows;
        return rows;
      })
      .finally(() => {
        if (_projectsInFlight === p) _projectsInFlight = null;
      });
    _projectsInFlight = p;
    return p;
  },
  // T-0051: the wizard builds the JSON body itself (see
  // pages/projectCreateWizard.ts::payloadFromWizard) so the API
  // helper accepts the body verbatim. Back-compat: the minimal
  // T-0021 form just sends {slug, display_name, repo_path?}.
  // T-0052: the deep-flow create also spawns a per-project operator
  // session and returns its SID (or a `spawn_error` if the worker spawn
  // failed — the project itself is still created). The wizard's success
  // step uses these to surface the attach command via CopyableTmuxAttach.
  createProject: (body: Record<string, unknown>) =>
    call<
      Project & {
        scaffold?: { ops_linked: string[]; ops_skipped: string[] } | null;
        operator_sid?: string | null;
        spawn_error?: string | null;
      }
    >(
      "/api/projects",
      { method: "POST", body: JSON.stringify(body) },
    ),
  createModesDoc: () =>
    call<{ content: string }>("/api/projects/_/create-modes"),
  project: (slug: string) => call<ProjectDetail>(`/api/projects/${slug}`),
  // T-0156: set per-project Telegram binding (group/DM chat + optional topic).
  setProjectTg: (slug: string, body: { tg_chat: string; tg_topic_id: number | null }) =>
    call<{ slug: string; tg_chat: string; tg_topic_id: number | null }>(
      `/api/projects/${slug}/tg`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  testProjectTg: (slug: string) =>
    call<{ ok: boolean; sent: boolean }>(`/api/projects/${slug}/tg/test`, {
      method: "POST",
    }),
  backlog: (slug: string) =>
    call<Task[]>(`/api/projects/${slug}/backlog`).then((tasks) => tasks.map(normalizeTask)),
  // T-0512 (M9): list the subtasks of a task (every task whose parent_task ===
  // id). 404s if the parent doesn't exist. Rows share the Task shape.
  children: (slug: string, id: string) =>
    call<Task[]>(`/api/projects/${slug}/backlog/${id}/children`).then((tasks) =>
      tasks.map(normalizeTask),
    ),
  vision: (slug: string) => call<VisionFile[]>(`/api/projects/${slug}/vision`),
  // item-12: GET /feedback default-HIDES closed (promoted/dismissed) items;
  // pass includeClosed for the 'Show closed' toggle.
  feedback: (slug: string, includeClosed = false) =>
    call<FeedbackFile[]>(`/api/projects/${slug}/feedback${includeClosed ? "?include_closed=true" : ""}`),
  analytics: (slug: string) => call<Analytics>(`/api/projects/${slug}/analytics`),
  // T-0511 (M11-F11.4): unified read-only system-state view. Used by the flat
  // `api` (like analytics) — single-install, not proxied through mothership.
  transparency: (slug: string) =>
    call<Transparency>(`/api/projects/${slug}/transparency`),
  // T-0620/T-0630: pause takes effect immediately on the 60s re-drive tick;
  // `reason` is optional (surfaced back on the paused-state read). Resume is
  // idempotent no-op if not paused.
  operatorPause: (slug: string, reason?: string) =>
    call<OperatorPauseResult>(`/api/projects/${slug}/operator/pause`, {
      method: "POST",
      body: JSON.stringify(reason ? { reason } : {}),
    }),
  operatorResume: (slug: string) =>
    call<OperatorResumeResult>(`/api/projects/${slug}/operator/resume`, {
      method: "POST",
    }),
  // T-0630: fleet default model, read+write against the allowlisted set
  // (`""` clears the override → builtin default). PUT is admin-only server-side.
  getWorkerModel: (slug: string) =>
    call<WorkerModel>(`/api/projects/${slug}/worker/model`),
  putWorkerModel: (slug: string, model: string) =>
    call<WorkerModel>(`/api/projects/${slug}/worker/model`, {
      method: "PUT",
      body: JSON.stringify({ model }),
    }),
  login: (username: string, password: string) =>
    call("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => call("/api/auth/logout", { method: "POST" }),
  createTask: (slug: string, t: CreateTaskBody) =>
    call<Task>(`/api/projects/${slug}/backlog`, { method: "POST", body: JSON.stringify(t) }),
  patchTask: (slug: string, id: string, t: Partial<Task>) =>
    call<Task>(`/api/projects/${slug}/backlog/${id}`, { method: "PATCH", body: JSON.stringify(t) }),
  patchTaskPriority: (slug: string, id: string, priority: number) =>
    call<Task>(`/api/projects/${slug}/backlog/${id}/priority`, {
      method: "PATCH",
      body: JSON.stringify({ priority }),
    }),
  deleteTask: (slug: string, id: string) =>
    call(`/api/projects/${slug}/backlog/${id}`, { method: "DELETE" }),
  // T-0389/audit item 18: the `## Comments` channel is cut. addComment had ZERO
  // callers (the board "add comment" kebab posts a Progress note via addProgress,
  // T-0238) and the appended ## Comments section was rendered nowhere — comments
  // vanished into a dead md section. Removed; backend /comments route removal
  // coordinated with Team-1.
  addProgress: (slug: string, id: string, sid: string, text: string) =>
    call<{ ok: boolean; task_id: string; line_appended: string }>(
      `/api/projects/${slug}/backlog/${id}/progress`,
      { method: "POST", body: JSON.stringify({ sid, text }) },
    ),
  // T-0295 (b): constant-team tick state (read-only).
  constantTeams: (slug: string) =>
    call<ConstantTeamsResponse>(`/api/projects/${slug}/constant-teams`),
  putVision: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/vision/${name}`, { method: "PUT", body: JSON.stringify({ content }) }),
  newInitiative: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/vision`, { method: "POST", body: JSON.stringify({ kind: "initiative", name, content }) }),
  setActiveInitiative: (slug: string, name: string) =>
    call(`/api/projects/${slug}/vision/active_initiative`, { method: "PUT", body: JSON.stringify({ name }) }),
  activateInitiative: (slug: string, name: string) =>
    call(`/api/projects/${slug}/vision/active_initiatives/${encodeURIComponent(name)}`, { method: "POST" }),
  deactivateInitiative: (slug: string, name: string) =>
    call(`/api/projects/${slug}/vision/active_initiatives/${encodeURIComponent(name)}`, { method: "DELETE" }),
  markInitiativeFinished: (slug: string, name: string) =>
    call(`/api/projects/${slug}/vision/finished_initiatives/${encodeURIComponent(name)}`, { method: "POST" }),
  reopenInitiative: (slug: string, name: string) =>
    call(`/api/projects/${slug}/vision/finished_initiatives/${encodeURIComponent(name)}`, { method: "DELETE" }),
  putFeedback: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/feedback/${name}`, { method: "PUT", body: JSON.stringify({ content }) }),
  promoteFeedback: (slug: string, name: string, title?: string, body?: string) =>
    call<{ task_id: string }>(`/api/projects/${slug}/feedback/${name}/promote`, { method: "POST", body: JSON.stringify({ title, body }) }),
  // item-12: owner-gated close — sets status:dismissed (kept on disk, hidden
  // from the default list). The dominant operator action on a feedback item.
  dismissFeedback: (slug: string, name: string) =>
    call<{ ok: boolean }>(`/api/projects/${slug}/feedback/${encodeURIComponent(name)}/dismiss`, { method: "POST" }),
  // T-0283/D-0029: feedback themes are nestable cross-store artifacts. List a
  // theme's children (evidence docs etc.) and adopt/disown the theme by setting
  // its parent_doc_id (keyed by filename `name`; the parent is any artifact id).
  feedbackChildren: (slug: string, name: string) =>
    call<ArtifactChild[]>(`/api/projects/${slug}/feedback/${encodeURIComponent(name)}/children`),
  setFeedbackParent: (slug: string, name: string, parentDocId: string | null) =>
    call<{ ok: boolean; id: string; parent_doc_id: string | null }>(
      `/api/projects/${slug}/feedback/${encodeURIComponent(name)}/parent`,
      { method: "PUT", body: JSON.stringify({ parent_doc_id: parentDocId }) }),
  // T-0172: project docs.
  docs: (slug: string, category?: string) =>
    call<DocSummary[]>(`/api/projects/${slug}/docs${category ? `?category=${encodeURIComponent(category)}` : ""}`),
  docCategories: (slug: string) =>
    call<string[]>(`/api/projects/${slug}/docs/categories`),
  doc: (slug: string, id: string) =>
    call<DocDetail>(`/api/projects/${slug}/docs/${encodeURIComponent(id)}`),
  // T-0235: createDoc accepts an optional parent so a doc can be born already
  // attached to a mother doc. The parent_doc_id key is only sent when given,
  // so existing flat-doc callers keep their `{category, title}` body.
  createDoc: (slug: string, category: string, title: string, parentDocId?: string | null) =>
    call<{ ok: boolean; id: string; category: string }>(`/api/projects/${slug}/docs`,
      {
        method: "POST",
        body: JSON.stringify(
          parentDocId == null
            ? { category, title }
            : { category, title, parent_doc_id: parentDocId },
        ),
      }),
  putDoc: (slug: string, id: string, content: string) =>
    call(`/api/projects/${slug}/docs/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify({ content }) }),
  // T-0276: delete a doc (scrubs ticket related_docs backlinks on the BE).
  // 409 if it's a mother doc with children (detail names them); D-NNNN is
  // tombstoned, not reclaimed; 404 if missing.
  deleteDoc: (slug: string, id: string) =>
    call<{ ok: boolean; id: string; deleted: boolean }>(`/api/projects/${slug}/docs/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
  linkDoc: (slug: string, id: string, ticket: string) =>
    call<{ ok: boolean; id: string; ticket: string; linked: boolean }>(
      `/api/projects/${slug}/docs/${encodeURIComponent(id)}/link`,
      { method: "POST", body: JSON.stringify({ ticket }) }),
  unlinkDoc: (slug: string, id: string, ticket: string) =>
    call(`/api/projects/${slug}/docs/${encodeURIComponent(id)}/link/${encodeURIComponent(ticket)}`,
      { method: "DELETE" }),
  // T-0234/T-0235/T-0283: nested docs. List a mother's attached children — now
  // cross-store (EXTENDED per D-0029 to include UC/feedback children, each row
  // carrying `kind`), and adopt/disown a doc by setting (or clearing, with
  // null) its parent_doc_id (which may point at any artifact id).
  docChildren: (slug: string, id: string) =>
    call<ArtifactChild[]>(`/api/projects/${slug}/docs/${encodeURIComponent(id)}/children`),
  setDocParent: (slug: string, id: string, parentDocId: string | null) =>
    call<{ ok: boolean; id: string; parent_doc_id: string | null }>(
      `/api/projects/${slug}/docs/${encodeURIComponent(id)}/parent`,
      { method: "PUT", body: JSON.stringify({ parent_doc_id: parentDocId }) }),
  // T-0601 (F5): the server now returns {sessions, errors} so per-user worker
  // fan-out failures surface instead of blanking to "No sessions".
  // `sessions()` keeps its SessionRow[] contract for the many list-only
  // consumers; `sessionsDetail()` exposes the errors for the Processes board.
  // Both normalize the legacy bare-array shape (older servers behind the
  // mothership proxy).
  sessions: (slug: string) =>
    call<unknown>(`/api/projects/${slug}/sessions`).then(
      (body) => normalizeSessionsPayload(body).rows,
    ),
  sessionsDetail: (slug: string) =>
    call<unknown>(`/api/projects/${slug}/sessions`).then(normalizeSessionsPayload),
  // T-0210: resource telemetry (per-session context/memory + quota burndown).
  telemetry: (slug: string) =>
    call<TelemetryResponse>(`/api/projects/${slug}/telemetry`),
  // T-0437: pin/unpin a session within the project (admin-gated server-side via
  // require_project_member). The pin is a user signal surfaced in the UI; it
  // does NOT change orchestration. Pins are per-project and stamped back onto
  // the sessions list (pinned/pinned_by/pinned_at) on the next poll.
  pinSession: (slug: string, sid: string) =>
    call<{ ok: boolean; sid: string; pinned: boolean; pinned_by: string; pinned_at: string }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pin`,
      { method: "POST" },
    ),
  unpinSession: (slug: string, sid: string) =>
    call<{ ok: boolean; sid: string; pinned: boolean; was_pinned: boolean }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pin`,
      { method: "DELETE" },
    ),
  pauseSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pause`, { method: "POST" }),
  suspendSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/suspend`, { method: "POST" }),
  // T-0407: optionally thread the reused task (+ a brief) through resume so the
  // session adopts it as PRIMARY in one round-trip (the worker adopts an empty
  // primary as task_id, T-0166). A bare resume sends an empty body — unchanged.
  resumeSession: (
    slug: string,
    sid: string,
    opts?: { task_id?: string; initial_prompt?: string },
  ) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/resume`, {
      method: "POST",
      body: JSON.stringify(opts ?? {}),
    }),
  // T-0594 (T-0588b): spawnSession / devSpawnRequest / reuseCandidates client
  // methods removed — the Sessions page is read + minimal lifecycle controls;
  // spawning is TG/CLI-driven. The API routes they called remain live (they
  // are the TG/CLI control plane's substrate).
  bindTask: (slug: string, sid: string, task_id: string) =>
    call<{ ok: boolean; sid: string; task_id: string; extras: string[] }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/bind/task`,
      { method: "POST", body: JSON.stringify({ task_id }) },
    ),
  unbindTask: (slug: string, sid: string, task_id: string) =>
    call<{ ok: boolean; sid: string; task_id: string; extras: string[]; changed: boolean }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/unbind/task`,
      { method: "POST", body: JSON.stringify({ task_id }) },
    ),
  bindInitiative: (slug: string, sid: string, initiative: string) =>
    call<{ ok: boolean; sid: string; initiative: string; extras: string[] }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/bind/initiative`,
      { method: "POST", body: JSON.stringify({ initiative }) },
    ),
  unbindInitiative: (slug: string, sid: string, initiative: string) =>
    call<{ ok: boolean; sid: string; initiative: string; extras: string[]; changed: boolean }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/unbind/initiative`,
      { method: "POST", body: JSON.stringify({ initiative }) },
    ),
  archiveSession: (slug: string, sid: string) =>
    call<{ ok: boolean; sid: string; archived: boolean }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/archive`,
      { method: "POST" },
    ),
  unarchiveSession: (slug: string, sid: string) =>
    call<{ ok: boolean; sid: string; archived: boolean }>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/unarchive`,
      { method: "POST" },
    ),
  runs: (slug: string, limit = 50, offset = 0) =>
    call<RunRow[]>(`/api/projects/${slug}/runs?limit=${limit}&offset=${offset}`),
  runLog: (slug: string, id: string, full = false) => {
    const path = `/api/projects/${slug}/runs/${encodeURIComponent(id)}/log${full ? "?full=1" : ""}`;
    return fetch(path, {
      credentials: "include",
    }).then(async (res) => {
      // T-0714: same shared gate as call() — this one hand-rolls the fetch
      // because it returns text, not JSON.
      if (res.status === 401) redirectOn401(path);
      if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
      return res.text();
    });
  },
  sessionMessages: (
    slug: string,
    claudeUuid: string,
    limit = 200,
    offset = 0,
    full = false,
  ) =>
    call<MessageRecord[]>(
      `/api/projects/${slug}/sessions/${encodeURIComponent(claudeUuid)}/messages?limit=${limit}&offset=${offset}${full ? "&full=1" : ""}`
    ),
  scheduler: () => call<SchedulerState>("/api/scheduler"),
  // T-0153: autopilot — prompt-driven, time-boxed autonomous runs per target.
  autopilotStatus: (slug: string) =>
    call<AutopilotStatus>(`/api/projects/${slug}/autopilot`),
  autopilotStart: (slug: string, cfg: AutopilotConfig) =>
    call<{ ok: boolean; key: string; target_sid: string; expires_at: string; spawned: boolean }>(
      `/api/projects/${slug}/autopilot/start`,
      { method: "POST", body: JSON.stringify(cfg) },
    ),
  autopilotStop: (
    slug: string,
    opts: { key?: string; target_sid?: string; reason?: string },
  ) =>
    call<{ ok: boolean; key: string; status: string; exit_reason: string }>(
      `/api/projects/${slug}/autopilot/stop`,
      { method: "POST", body: JSON.stringify(opts) },
    ),
  peerSend: (slug: string, fromSid: string, to: string, text: string) =>
    call<{ ok: boolean; delivered_to: string[] }>(
      `/api/projects/${slug}/peer/send`,
      {
        method: "POST",
        body: JSON.stringify({ from_sid: fromSid, to, text }),
      },
    ),
  peerInboxRead: (slug: string, sid: string) =>
    call<{ ok: boolean; messages: string[]; count: number }>(
      `/api/projects/${slug}/peer/${encodeURIComponent(sid)}/read`,
      { method: "POST" },
    ),
  peerInboxWait: (slug: string, sid: string, timeoutSec: number) =>
    call<{ ok: boolean; ready: boolean; elapsed_sec: number }>(
      `/api/projects/${slug}/peer/${encodeURIComponent(sid)}/wait`,
      { method: "POST", body: JSON.stringify({ timeout: timeoutSec }) },
    ),
  listUsers: () => call<UserRow[]>("/api/users"),
  createUser: (
    username: string,
    password: string,
    linux_user?: string,
    is_admin?: boolean,
  ) =>
    call<UserRow>("/api/users", {
      method: "POST",
      body: JSON.stringify({ username, password, linux_user, is_admin }),
    }),
  patchUser: (
    username: string,
    body: { linux_user?: string; is_admin?: boolean },
  ) =>
    call<UserRow>(`/api/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  resetUserPassword: (username: string, password: string) =>
    call<{ ok: boolean }>(
      `/api/users/${encodeURIComponent(username)}/password`,
      { method: "PUT", body: JSON.stringify({ password }) },
    ),
  deleteUser: (username: string) =>
    call<{ ok: boolean }>(`/api/users/${encodeURIComponent(username)}`, {
      method: "DELETE",
    }),
  getSystemSettings: () => call<SystemSettings>("/api/system-settings"),
  putSystemSettings: (body: PutSystemSettingsBody) =>
    call<PutSystemSettingsResult>("/api/system-settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getMyProfile: () => call<MeProfile>("/api/me"),
  putMyTgChatId: (tg_chat_id: string) =>
    call<MeProfile>("/api/me/tg-chat-id", {
      method: "PUT",
      body: JSON.stringify({ tg_chat_id }),
    }),
  testMyTgChatId: () =>
    call<{ ok: boolean; sent: boolean }>("/api/me/tg-chat-id/test", {
      method: "POST",
    }),

  // T-0218 — 3-level personal notification inheritance (global -> server ->
  // project, most-specific wins). The resolved view is the single read powering
  // the override panel (each level's raw value + `set` flag + the effective
  // winner + its `source`); the per-project route is the NEW per-(user,project)
  // override, mirroring the per-server /me/attachment/<id>/tg-chat-id shape.
  // (Project persistence is a deferred T-0218 follow-up — the GET returns null
  // / inherit until then; the resolved precedence is computed server-side, the
  // FE never recomputes it.)
  notificationsResolved: (serverId: string, slug: string) =>
    call<NotificationsResolved>(
      `/api/me/notifications/resolved?server_id=${encodeURIComponent(
        serverId,
      )}&slug=${encodeURIComponent(slug)}`,
    ),
  putProjectTgChatId: (slug: string, tg_chat_id: string) =>
    call<ProjectTgChatId>(
      `/api/me/project/${encodeURIComponent(slug)}/tg-chat-id`,
      { method: "PUT", body: JSON.stringify({ tg_chat_id }) },
    ),
  testProjectTgChatId: (slug: string) =>
    call<{ ok: boolean; sent: boolean }>(
      `/api/me/project/${encodeURIComponent(slug)}/tg-chat-id/test`,
      { method: "POST" },
    ),
  /** T-0013: resolve the operator tmux session name for the post-install
   * /welcome screen. Server-side constant (env var on the install). */
  welcomeOperator: () =>
    call<{ session: string }>("/api/welcome/operator"),

  // T-0089 — consumer-side autoupdate status + operator levers. All three
  // 404 on the mothership build; the AutoupdatePill component is also
  // tree-shaken there via VITE_MOTHERSHIP, so these methods are only
  // exercised on consumer installs.
  // T-0168: coerce the raw body — a non-object/null/malformed payload becomes
  // `null` so the pill renders nothing instead of throwing during render.
  autoupdateStatus: async (): Promise<AutoupdateStatus | null> =>
    normalizeAutoupdateStatus(await call<unknown>("/api/autoupdate/status")),
  autoupdatePause: (paused: boolean) =>
    call<{ ok: boolean; paused: boolean }>("/api/autoupdate/pause", {
      method: "POST",
      body: JSON.stringify({ paused }),
    }),
  autoupdateCheckNow: () =>
    call<{ ok: boolean; scheduled?: boolean; next_run?: string; ran_inline?: boolean }>(
      "/api/autoupdate/check_now",
      { method: "POST" },
    ),
};

// T-0068: per-project methods exposed via context so per-project pages can be
// rendered against either the global single-install singleton OR the
// mothership's `apiFor(server_id)` proxy without touching the page code. The
// list is exactly what Project.tsx + Sessions.tsx consume today; expand it
// when new per-project pages are migrated.
export type ProjectApi = Pick<
  typeof api,
  | "backlog"
  | "children"
  | "vision"
  | "sessions"
  | "sessionsDetail"
  | "telemetry"
  | "createTask"
  | "patchTask"
  | "patchTaskPriority"
  | "deleteTask"
  | "addProgress"
  | "pinSession"
  | "unpinSession"
  | "pauseSession"
  | "suspendSession"
  | "resumeSession"
  | "bindTask"
  | "archiveSession"
  | "unarchiveSession"
  | "peerSend"
  | "peerInboxRead"
  | "peerInboxWait"
  | "autopilotStatus"
  | "autopilotStart"
  | "autopilotStop"
  | "operatorPause"
  | "operatorResume"
  | "getWorkerModel"
  | "putWorkerModel"
>;
