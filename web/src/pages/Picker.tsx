import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Project } from "../api";

export function Picker() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.projects().then(setProjects).catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="container py-4">
      <header className="d-flex justify-content-between align-items-center mb-4">
        <h1 className="m-0">bot-squad</h1>
        <button className="btn btn-outline-secondary btn-sm" onClick={() => api.logout().then(() => location.assign("/login"))}>
          Sign out
        </button>
      </header>
      {error && <div className="alert alert-danger">{error}</div>}
      {projects === null && !error && <p>Loading…</p>}
      <div className="row g-3">
        {projects?.map((p) => (
          <div className="col-md-4" key={p.slug}>
            <Link to={`/p/${p.slug}`} className="card text-decoration-none">
              <div className="card-body">
                <h5 className="card-title">{p.display_name}</h5>
                <small className="text-muted">{p.slug}</small>
              </div>
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}
