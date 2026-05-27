type Json = Record<string, unknown> | unknown[];

async function call<T = Json>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("not authenticated");
  }
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
  // coloured pill. See vision/multi-server/quick-status.md.
  status?: "working" | "needs-input" | "idle" | string;
  status_since?: string | null;
};

export type ProjectDetail = Project & {
  deploy_branch: string;
  prod_url: string;
  staging_url: string;
  dev_url: string;
  counts: { backlog: number; vision: number; feedback: number; sessions: number };
};

export type Task = {
  id: string;
  title: string;
  status: "planned" | "open" | "in_progress" | "totest" | "reopened" | "closed";
  body: string;
  // Phase 7: parsed body sections — verbatim is the stakeholder's exact words.
  verbatim?: string;
  context?: string;
  progress?: string;
  // Phase 8: int sort key for Kanban ordering. null = unset (sorts last).
  priority?: number | null;
  // T-0038: first-class linkage. `initiative` is a basename under
  // vision/initiatives/. Missing/null = unattached.
  initiative?: string | null;
  parent_task?: string | null;
  blocked_by?: string[] | null;
  // T-0105 + T-0106: append-only list of SIDs that worked on this task,
  // oldest first. Worker stamps on spawn/bind/resume; rendered as a panel
  // on TaskDetail. May be undefined for legacy tasks created before T-0105.
  session_history?: string[];
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
  };
};

export type CreateTaskBody = {
  title: string;
  status?: Task["status"];
  // Either pass `verbatim_request` (preferred — composed into canonical body)
  // or `body` (raw, stored as-is for callers that know the convention).
  verbatim_request?: string;
  body?: string;
};

export type VisionFile = { name: string; content: string; active?: boolean; finished?: boolean };
export type FeedbackFile = { name: string; content: string };

export type SessionRow = {
  sid: string;
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
  // T-0078: which tmux session the pane lives in (`<slug>` for legacy
  // panes, `<slug>-<initiative-stem>` for initiative TLs). Empty when
  // the worker can't determine it (pre-T-0078 md without backfill).
  tmux_session?: string;
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

export type AutonomousState = {
  ok: boolean;
  slug: string;
  enabled: boolean;
  status: "idle" | "working" | "reviewing" | "sleeping";
  current_task_id: string | null;
  current_pane_id: string | null;
  current_started_at: string | null;
  last_tick_at: string | null;
  sleep_start_hour: number;
  sleep_end_hour: number;
  fail_counts: Record<string, number>;
  tick_log: TickLogEntry[];
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

export type SystemSettings = {
  tg: {
    bot_token_set: boolean;
    quiet_hours_start_utc: number;
    quiet_hours_end_utc: number;
  };
  session: { ttl: string };
  admin: { coordinator_user: string };
};

export type PutSystemSettingsBody = {
  tg?: {
    bot_token?: string;
    quiet_hours_start_utc?: number;
    quiet_hours_end_utc?: number;
  };
  session?: { ttl?: string };
  admin?: { coordinator_user?: string };
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

export const api = {
  health: () => call("/api/health"),
  me: () => call<Me>("/api/auth/me"),
  projects: () => call<Project[]>("/api/projects"),
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
  repoAgentsMd: (slug: string) =>
    call<{ content: string }>(`/api/projects/${slug}/repo-agents-md`),
  putRepoAgentsMd: (slug: string, content: string) =>
    call(`/api/projects/${slug}/repo-agents-md`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
  backlog: (slug: string) => call<Task[]>(`/api/projects/${slug}/backlog`),
  vision: (slug: string) => call<VisionFile[]>(`/api/projects/${slug}/vision`),
  feedback: (slug: string) => call<FeedbackFile[]>(`/api/projects/${slug}/feedback`),
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
  addComment: (slug: string, id: string, body: string) =>
    call<Task>(`/api/projects/${slug}/backlog/${id}/comments`, { method: "POST", body: JSON.stringify({ body }) }),
  addProgress: (slug: string, id: string, sid: string, text: string) =>
    call<{ ok: boolean; task_id: string; line_appended: string }>(
      `/api/projects/${slug}/backlog/${id}/progress`,
      { method: "POST", body: JSON.stringify({ sid, text }) },
    ),
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
  sessions: (slug: string) =>
    call<SessionRow[]>(`/api/projects/${slug}/sessions`),
  pauseSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pause`, { method: "POST" }),
  suspendSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/suspend`, { method: "POST" }),
  resumeSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/resume`, { method: "POST" }),
  spawnSession: (slug: string, window: string, initial_prompt?: string, task_id?: string, initiative?: string) =>
    call(`/api/projects/${slug}/sessions`, {
      method: "POST",
      body: JSON.stringify({ window, initial_prompt, task_id, initiative }),
    }),
  devSpawnRequest: (slug: string, tl_sid: string, task_id: string | undefined, instructions: string) =>
    call<{ ok: boolean; delivered_to: string[] }>(
      `/api/projects/${slug}/dev-spawn-request`,
      {
        method: "POST",
        body: JSON.stringify({ tl_sid, task_id, instructions }),
      },
    ),
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
  runLog: (slug: string, id: string, full = false) =>
    fetch(`/api/projects/${slug}/runs/${encodeURIComponent(id)}/log${full ? "?full=1" : ""}`, {
      credentials: "include",
    }).then(async (res) => {
      if (res.status === 401) { window.location.href = "/login"; throw new Error("not authenticated"); }
      if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
      return res.text();
    }),
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
  autonomousStatus: (slug: string) =>
    call<AutonomousState>(`/api/projects/${slug}/autonomous`),
  autonomousEnable: (slug: string, sleepStartHour?: number, sleepEndHour?: number) =>
    call(`/api/projects/${slug}/autonomous/enable`, {
      method: "POST",
      body: JSON.stringify({
        sleep_start_hour: sleepStartHour,
        sleep_end_hour: sleepEndHour,
      }),
    }),
  autonomousDisable: (slug: string) =>
    call(`/api/projects/${slug}/autonomous/disable`, { method: "POST" }),
  autonomousLog: (slug: string) =>
    call<TickLogEntry[]>(`/api/projects/${slug}/autonomous/log`),
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
  /** T-0013: resolve the operator tmux session name for the post-install
   * /welcome screen. Server-side constant (env var on the install). */
  welcomeOperator: () =>
    call<{ session: string }>("/api/welcome/operator"),

  // T-0089 — consumer-side autoupdate status + operator levers. All three
  // 404 on the mothership build; the AutoupdatePill component is also
  // tree-shaken there via VITE_MOTHERSHIP, so these methods are only
  // exercised on consumer installs.
  autoupdateStatus: () => call<AutoupdateStatus>("/api/autoupdate/status"),
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
  | "vision"
  | "sessions"
  | "createTask"
  | "patchTask"
  | "patchTaskPriority"
  | "deleteTask"
  | "addComment"
  | "pauseSession"
  | "suspendSession"
  | "resumeSession"
  | "archiveSession"
  | "unarchiveSession"
  | "spawnSession"
  | "devSpawnRequest"
  | "peerSend"
>;
