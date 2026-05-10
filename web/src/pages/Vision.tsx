import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, VisionFile } from "../api";

export function Vision() {
  const { slug = "" } = useParams();
  const [files, setFiles] = useState<VisionFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.vision(slug).then(setFiles).catch((e) => setError(String(e)));
  }, [slug]);

  return (
    <div className="container py-4">
      <nav className="mb-3">
        <Link to={`/p/${slug}`}>← Board</Link>
      </nav>
      <h2>Vision — {slug}</h2>
      {error && <div className="alert alert-danger">{error}</div>}
      {files === null && !error && <p>Loading…</p>}
      {files?.map((f) => (
        <section key={f.name} className="mb-4">
          <h5 className="text-muted small">{f.name}</h5>
          <pre className="bg-light p-3 small" style={{ whiteSpace: "pre-wrap" }}>
            {f.content}
          </pre>
        </section>
      ))}
    </div>
  );
}
