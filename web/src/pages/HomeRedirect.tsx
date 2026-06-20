import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { api, Project } from "../api";
import { Picker } from "./Picker";

// Must match Shell.tsx's pin key — the operator's "current brain".
const PINNED_PROJECT_KEY = "bot-squad:last-project";

/**
 * HomeRedirect — the landing route `/` (T-0336, reframe-operator-paradigm).
 *
 * bot-squad IS a single project brain operated by one operator, NOT a fleet
 * console. So `/` resolves to the project's process/Task-Manager (sessions)
 * view — "what's running / what needs me" — instead of the cross-server
 * mothership AllProjects console (which is demoted to the admin `/m/` area).
 *
 * Resolution order:
 *   1. A pinned project (localStorage) → that project's sessions view.
 *   2. Exactly one project on the install → straight into its brain.
 *   3. Zero or many projects with no pin → the LOCAL single-server project
 *      picker (a plain project list, NOT the cross-server fleet console).
 *   4. API unreachable → the Picker (it surfaces the error inline).
 *
 * The mothership build no longer renders AllProjects here; it lives under
 * `/m/` (mothership/routes.tsx index), reachable via the admin FLEET entry.
 */
export function HomeRedirect() {
  const pinned = (() => {
    try {
      return localStorage.getItem(PINNED_PROJECT_KEY);
    } catch {
      return null;
    }
  })();

  const [projects, setProjects] = useState<Project[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (pinned) return; // pinned project wins — no lookup needed
    let cancelled = false;
    api
      .projects()
      .then((p) => {
        if (!cancelled) setProjects(p);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [pinned]);

  if (pinned) {
    return <Navigate to={`/p/${pinned}/sessions`} replace />;
  }
  if (failed) {
    return <Picker />;
  }
  if (projects === null) {
    return <div className="mc-loading">Loading…</div>;
  }
  if (projects.length === 1) {
    return <Navigate to={`/p/${projects[0].slug}/sessions`} replace />;
  }
  // Zero or many projects, no pin → local project list (not the fleet console).
  return <Picker />;
}
