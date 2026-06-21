import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, cachedProjects, type Project } from "../api";
import { useAnchorRect, anchoredBelowLeft } from "./useAnchorRect";

export function statusBadgeClass(status: string): string {
  switch (status) {
    case "working":
      return "mc-badge mc-badge-active";
    case "needs-input":
      return "mc-badge mc-badge-warn";
    case "idle":
      return "mc-badge mc-badge-dim";
    default:
      return "mc-badge mc-badge-dim";
  }
}

export type ProjectSwitcherProps = {
  slug: string;
  onUnpin: () => void;
};

export function ProjectSwitcher({ slug, onUnpin }: ProjectSwitcherProps) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  // T-0365: seed from the shared cache so the dropdown paints its list
  // instantly on open; the effect below still revalidates in the background.
  const [projects, setProjects] = useState<Project[] | null>(() => cachedProjects());
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // T-0140: fixed-position the dropdown so the sidebar overflow clip can't crop
  // it at the rail edge (see useAnchorRect).
  const anchorRect = useAnchorRect(triggerRef, open);

  const close = useCallback(() => setOpen(false), []);

  useEffect(() => {
    if (!open) return;
    setError(null);
    let cancelled = false;
    api
      .projects()
      .then((rows) => {
        if (!cancelled) setProjects(rows);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(e.target as Node)) close();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") close();
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  function goTo(targetSlug: string) {
    close();
    if (targetSlug === slug) return;
    navigate(`/p/${targetSlug}`);
  }

  function goAllProjects() {
    close();
    onUnpin();
    navigate("/");
  }

  return (
    <div className="mc-switcher" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className="mc-switcher-trigger"
        data-onboarding-anchor="switch-project"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        switch project <span className="mc-switcher-caret" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div
          className="mc-switcher-panel"
          role="listbox"
          style={anchoredBelowLeft(anchorRect)}
        >
          {error && (
            <div className="mc-switcher-empty mc-switcher-error">
              Could not load projects.
            </div>
          )}

          {!error && projects === null && (
            <div className="mc-switcher-empty">Loading…</div>
          )}

          {!error && projects !== null && projects.length === 0 && (
            <div className="mc-switcher-empty">No projects.</div>
          )}

          {!error && projects && projects.length > 0 && (
            <ul className="mc-switcher-list">
              {projects.map((p) => (
                <li key={p.slug}>
                  <button
                    type="button"
                    className={
                      "mc-switcher-row" +
                      (p.slug === slug ? " mc-switcher-row-current" : "")
                    }
                    onClick={() => goTo(p.slug)}
                  >
                    <span className="mc-switcher-row-name">
                      {p.display_name}
                    </span>
                    {p.status && (
                      <span
                        className={statusBadgeClass(p.status)}
                        title={
                          p.status_since ? `since ${p.status_since}` : undefined
                        }
                      >
                        {p.status}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <button
            type="button"
            className="mc-switcher-allprojects"
            onClick={goAllProjects}
          >
            ← all projects
          </button>
        </div>
      )}
    </div>
  );
}
