import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, VisionFile } from "../api";
import { PageHelp } from "../components/PageHelp";

/**
 * Workflow — stakeholder-set governance. Holds the project constitution
 * (and any future "how we operate" docs). Agent-immutable; read-only here.
 */
export function Workflow() {
  const { slug = "" } = useParams();
  const [files, setFiles] = useState<VisionFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .vision(slug)
      .then((list) => setFiles(list.filter((f) => f.name === "constitution.md")))
      .catch((e) => setError(String(e)));
  }, [slug]);

  return (
    <div className="container py-4" style={{ maxWidth: "860px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Workflow
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        Stakeholder-set governance for the project: who has authority, how
        decisions are made, what is reversible vs not, the operating
        philosophy. <strong>Agent-immutable</strong> — only the stakeholder
        edits this on disk. Future docs that describe <em>how we work</em>
        (vs <em>what we&apos;re building</em>) belong here too.
      </PageHelp>

      {error && <div className="alert alert-danger">{error}</div>}
      {files === null && !error && <div className="mc-loading">Loading</div>}
      {files !== null && files.length === 0 && !error && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          <div>No workflow documents yet.</div>
          <div className="mc-empty-hint">
            Drop a <code>constitution.md</code> into <code>ops/bot-squad/vision/</code> (or
            <code> /home/www/bot-squad/data/{slug}/vision/</code>) to populate this tab.
          </div>
        </div>
      )}

      {files?.map((f) => (
        <section key={f.name} className="mb-4">
          <div
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.72rem",
              color: "var(--mc-text-dim)",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              marginBottom: "0.5rem",
            }}
          >
            {f.name}
            <span style={{ marginLeft: "0.5rem", color: "var(--mc-amber)" }}>
              [stakeholder-only]
            </span>
          </div>
          <pre className="mc-pre">{f.content}</pre>
        </section>
      ))}
    </div>
  );
}
