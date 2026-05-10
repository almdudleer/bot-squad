import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, FeedbackFile } from "../api";

export function Feedback() {
  const { slug = "" } = useParams();
  const [files, setFiles] = useState<FeedbackFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.feedback(slug).then(setFiles).catch((e) => setError(String(e)));
  }, [slug]);

  return (
    <div className="container py-4">
      <nav className="mb-3">
        <Link to={`/p/${slug}`}>← Board</Link>
      </nav>
      <h2>Feedback — {slug}</h2>
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
