import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Project } from "../api";
import { Coachmark, Typewriter, useOnboardingStep } from "../onboarding";

// Mirror T-0025's statusBadgeClass so single-server and cross-server views
// paint the same colours from the same enum. Unknown strings fall back to
// the dim pill rather than going invisible — same forward-compat policy.
function statusBadgeClass(status: string): string {
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

export function Picker() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // T-0018 / §9.4: the slug of the first project on the picker that hosts a
  // session NOT prefixed with the current user's `S-<linux_user>-…` SID.
  // When set, the §9.4 Coachmark mounts on that card; when null, the beat is
  // suppressed (sole-creator / empty installation never sees the spotlight).
  const [alienSlug, setAlienSlug] = useState<string | null>(null);
  const existingStep = useOnboardingStep("srv.9_4.existing_projects");

  useEffect(() => {
    api.projects().then(setProjects).catch((e) => setError(String(e)));
  }, []);

  // Alien-detection scan: only runs while §9.4 is still pending (gated on
  // existingStep.visible) so dismissing the coachmark also stops the
  // per-project sessions fan-out on subsequent Picker visits. Stops on first
  // alien match — typical onboarding hits it in the first project.
  useEffect(() => {
    if (!existingStep.visible) return;
    if (!projects || projects.length === 0) return;
    let cancelled = false;
    (async () => {
      try {
        const me = await api.me();
        if (cancelled) return;
        const myPrefix = `S-${me.linux_user}-`;
        for (const p of projects) {
          if (cancelled) return;
          try {
            const rows = await api.sessions(p.slug);
            if (cancelled) return;
            const alien = rows.find(
              (s) => !s.archived && !s.sid.startsWith(myPrefix),
            );
            if (alien) {
              setAlienSlug(p.slug);
              return;
            }
          } catch {
            /* permission denied / unreachable — skip this project */
          }
        }
      } catch {
        /* anonymous / me lookup failed — bail */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projects, existingStep.visible]);

  return (
    <div className="container py-4" style={{ maxWidth: "900px" }}>
      {/* §9.1 — first onboarding beat for a fresh server-view visit. Anchors
          on the sidebar HELP link via the data-onboarding-anchor convention
          (see Shell.tsx); future §9.2..9.6 beats reuse the same attribute. */}
      <Coachmark
        stepId="srv.9_1.help_spotlight"
        title="Need a hand?"
        anchorSelector='[data-onboarding-anchor="help-nav"]'
        placement="right"
        body={
          <Typewriter
            text="You can always see the tmux cheatsheet here."
            trailing={
              <>
                {" "}
                <Link to="/help#tmux-cheatsheet">Open it →</Link>
              </>
            }
          />
        }
      />

      {/* §9.4 — discovery spotlight on the first non-self project. Only
          mounts when alien-detection found one (see useEffect above); on a
          sole-creator or empty installation this is silent. The CTA points
          at the project's sessions list, where every row carries the
          copyable `tmux a -t` shipped by T-0006. */}
      {alienSlug && (
        <Coachmark
          stepId="srv.9_4.existing_projects"
          title="Other people's projects"
          anchorSelector='[data-onboarding-anchor="existing-projects"]'
          placement="bottom"
          body={
            <>
              There are already projects from other people on this server. You can
              open any of them and inspect their sessions — every session row has a
              copyable <code>tmux a -t</code> you can run over SSH to attach.{" "}
              <Link to={`/p/${alienSlug}/sessions`}>Open the sessions list →</Link>
            </>
          }
        />
      )}

      <div className="d-flex align-items-center gap-2 mb-4">
        <div className="mc-section-title" style={{ margin: 0 }}>Projects</div>
      </div>

      {error && <div className="alert alert-danger">{error}</div>}

      {projects === null && !error && (
        <div className="mc-loading">Loading</div>
      )}

      {projects !== null && projects.length === 0 && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          <div>No projects configured.</div>
        </div>
      )}

      <div className="row g-3">
        {projects?.map((p) => (
          <div className="col-md-4" key={p.slug}>
            <Link
              to={`/p/${p.slug}`}
              className="mc-project-card"
              data-onboarding-anchor={p.slug === alienSlug ? "existing-projects" : undefined}
            >
              <div className="mc-project-name">{p.display_name}</div>
              <div
                className="mc-project-slug"
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: "0.5rem",
                }}
              >
                <span>{p.slug}</span>
                {p.status && (
                  <span
                    className={statusBadgeClass(p.status)}
                    title={p.status_since ? `since ${p.status_since}` : undefined}
                  >
                    {p.status}
                  </span>
                )}
              </div>
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}
