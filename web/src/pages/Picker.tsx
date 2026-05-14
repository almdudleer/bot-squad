import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Project } from "../api";
import { Coachmark } from "../onboarding";

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

  useEffect(() => {
    api.projects().then(setProjects).catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="container py-4" style={{ maxWidth: "900px" }}>
      {/* Framework smoke. Real §9.1–9.6 copy lands in T-0014..T-0022. */}
      <Coachmark
        stepId="srv.intro"
        title="Welcome to your server"
        body="A quick tour of the picker and the surrounding views. Skip if you've seen it."
      />

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
            <Link to={`/p/${p.slug}`} className="mc-project-card">
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
