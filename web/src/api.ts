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
  status: "open" | "totest" | "reopened" | "closed";
  body: string;
  path: string;
  created?: string;
  updated?: string;
  from?: string;
};

export type VisionFile = { name: string; content: string };
export type FeedbackFile = { name: string; content: string };

export type SessionRow = {
  sid: string;
  status: "active" | "paused";
  window: string;
  cwd: string;
  started_at?: string | null;
  last_prompt_at?: string | number | null;
  claude_uuid?: string | null;
  linked_tasks: string[];
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

export const api = {
  health: () => call("/api/health"),
  projects: () => call<Project[]>("/api/projects"),
  project: (slug: string) => call<ProjectDetail>(`/api/projects/${slug}`),
  backlog: (slug: string) => call<Task[]>(`/api/projects/${slug}/backlog`),
  vision: (slug: string) => call<VisionFile[]>(`/api/projects/${slug}/vision`),
  feedback: (slug: string) => call<FeedbackFile[]>(`/api/projects/${slug}/feedback`),
  login: (username: string, password: string) =>
    call("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => call("/api/auth/logout", { method: "POST" }),
  createTask: (slug: string, t: Partial<Task>) =>
    call<Task>(`/api/projects/${slug}/backlog`, { method: "POST", body: JSON.stringify(t) }),
  patchTask: (slug: string, id: string, t: Partial<Task>) =>
    call<Task>(`/api/projects/${slug}/backlog/${id}`, { method: "PATCH", body: JSON.stringify(t) }),
  deleteTask: (slug: string, id: string) =>
    call(`/api/projects/${slug}/backlog/${id}`, { method: "DELETE" }),
  addComment: (slug: string, id: string, body: string) =>
    call<Task>(`/api/projects/${slug}/backlog/${id}/comments`, { method: "POST", body: JSON.stringify({ body }) }),
  putVision: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/vision/${name}`, { method: "PUT", body: JSON.stringify({ content }) }),
  newInitiative: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/vision`, { method: "POST", body: JSON.stringify({ kind: "initiative", name, content }) }),
  putFeedback: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/feedback/${name}`, { method: "PUT", body: JSON.stringify({ content }) }),
  promoteFeedback: (slug: string, name: string, title?: string, body?: string) =>
    call<{ task_id: string }>(`/api/projects/${slug}/feedback/${name}/promote`, { method: "POST", body: JSON.stringify({ title, body }) }),
  sessions: (slug: string) =>
    call<SessionRow[]>(`/api/projects/${slug}/sessions`),
  pauseSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pause`, { method: "POST" }),
  resumeSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/resume`, { method: "POST" }),
  spawnSession: (slug: string, window: string, initial_prompt?: string) =>
    call(`/api/projects/${slug}/sessions`, {
      method: "POST",
      body: JSON.stringify({ window, initial_prompt }),
    }),
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
};
