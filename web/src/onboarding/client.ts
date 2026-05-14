import { useEffect, useState } from "react";

import { OnboardingStepId, SKIP_ALL_SENTINEL } from "./stepIds";

type OnboardingState = {
  loaded: boolean;
  skipped: boolean;
  stepsSeen: Set<string>;
};

type ApiState = { steps_seen: string[]; skipped: boolean };

const state: OnboardingState = {
  loaded: false,
  skipped: false,
  stepsSeen: new Set(),
};

const listeners = new Set<() => void>();
let inflightLoad: Promise<void> | null = null;

function notify(): void {
  for (const fn of listeners) fn();
}

function applyServer(payload: ApiState): void {
  state.stepsSeen = new Set(payload.steps_seen);
  state.skipped = payload.skipped;
  state.loaded = true;
  notify();
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`onboarding API ${res.status}`);
  return (await res.json()) as T;
}

export function loadOnboarding(): Promise<void> {
  if (state.loaded) return Promise.resolve();
  if (inflightLoad) return inflightLoad;
  inflightLoad = fetchJson<ApiState>("/api/me/onboarding")
    .then(applyServer)
    .catch(() => {
      // Failing closed = treat as skipped so a broken /me/onboarding never
      // walls a user behind an undismissable overlay. They lose the tour;
      // they don't lose the app.
      state.stepsSeen = new Set([SKIP_ALL_SENTINEL]);
      state.skipped = true;
      state.loaded = true;
      notify();
    })
    .finally(() => {
      inflightLoad = null;
    });
  return inflightLoad;
}

export function hasSeen(stepId: string): boolean {
  return state.skipped || state.stepsSeen.has(stepId);
}

export async function markSeen(stepId: OnboardingStepId): Promise<void> {
  if (hasSeen(stepId)) return;
  // Optimistic — onboarding state is best-effort UI; if the POST fails we'll
  // re-show on next mount, which is recoverable.
  state.stepsSeen.add(stepId);
  notify();
  try {
    const payload = await fetchJson<ApiState>("/api/me/onboarding/seen", {
      method: "POST",
      body: JSON.stringify({ step: stepId }),
    });
    applyServer(payload);
  } catch {
    state.stepsSeen.delete(stepId);
    notify();
  }
}

export async function skipAll(): Promise<void> {
  if (state.skipped) return;
  state.skipped = true;
  state.stepsSeen.add(SKIP_ALL_SENTINEL);
  notify();
  try {
    const payload = await fetchJson<ApiState>("/api/me/onboarding/skip", {
      method: "POST",
    });
    applyServer(payload);
  } catch {
    state.skipped = false;
    state.stepsSeen.delete(SKIP_ALL_SENTINEL);
    notify();
  }
}

export function useOnboardingState(): OnboardingState {
  const [, force] = useState(0);
  useEffect(() => {
    const fn = () => force((n) => n + 1);
    listeners.add(fn);
    void loadOnboarding();
    return () => {
      listeners.delete(fn);
    };
  }, []);
  return state;
}
