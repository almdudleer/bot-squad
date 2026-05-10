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
};

export type VisionFile = { name: string; content: string };
export type FeedbackFile = { name: string; content: string };

export const api = {
  health: () => call("/api/health"),
  projects: () => call<Project[]>("/api/projects"),
  project: (slug: string) => call<ProjectDetail>(`/api/projects/${slug}`),
  backlog: (slug: string) => call<Task[]>(`/api/projects/${slug}/backlog`),
  vision: (slug: string) => call<VisionFile[]>(`/api/projects/${slug}/vision`),
  feedback: (slug: string) => call<FeedbackFile[]>(`/api/projects/${slug}/feedback`),
  loginTg: (payload: Record<string, unknown>) =>
    call("/api/auth/tg", { method: "POST", body: JSON.stringify(payload) }),
  logout: () => call("/api/auth/logout", { method: "POST" }),
};
