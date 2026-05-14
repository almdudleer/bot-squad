import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Project } from "../api";
import { Coachmark } from "../onboarding";

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
              <div className="mc-project-slug">{p.slug}</div>
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}
