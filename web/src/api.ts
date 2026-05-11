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
};
