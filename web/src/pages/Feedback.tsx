import { useEffect, useState } from "react";
import { useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { api, FeedbackFile } from "../api";
import { Markdown } from "../components/Markdown";
import { DocsOutletContext } from "./DocsSection";

// T-0337: Feedback is now the DETAIL pane of the unified "Docs & Artifacts"
// view. The shared cross-store tree + filter live in DocsSection; this page
// reads that tree from the Outlet context and renders only the feedback
// detail on the right. Deep-link (?fb=<name>) is unchanged.
// T-0670 (D-0057 §4/§8, T-0637 Lane C): this pane is now pure read/lookup —
// edit/promote/dismiss/reparent all moved to the TG dialog (R5).
export function Feedback() {
  const { slug = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { tree } = useOutletContext<DocsOutletContext>();

  const [files, setFiles] = useState<FeedbackFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<string | null>(null); // feedback filename

  useEffect(() => {
    api.feedback(slug).then(setFiles).catch((e) => setError(String(e)));
  }, [slug]);

  // Deep-link: /p/:slug/docs/feedback?fb=<name> selects that item (from the
  // shared artifact tree's feedback nodes).
  useEffect(() => {
    const f = searchParams.get("fb");
    if (f && f !== selected) setSelected(f);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const selectedFile = files?.find((f) => f.name === selected) ?? null;

  // T-0283: the feedback theme's canonical id (its parent-edge target) is the
  // filename stem; child artifacts (evidence) hang off it cross-store.
  const selfId = selectedFile ? (selectedFile.id ?? selectedFile.name.replace(/\.md$/, "")) : "";
  const childArtifacts = selfId ? (tree.childrenOf.get(selfId) ?? []) : [];

  return (
    <>
      {error && <div className="alert alert-danger py-1 small">{error}</div>}

      {files === null && !error && <div className="mc-loading">Loading</div>}
      {selectedFile === null && files !== null && <div className="text-muted small">Select a feedback item.</div>}

      {selectedFile && (
        <>
          <div className="d-flex justify-content-between align-items-center mb-2">
            <div
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.72rem",
                color: "var(--mc-text-dim)",
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              <span title="feedback" style={{ marginRight: "0.3rem" }}>💬</span>
              {selectedFile.name}
              {/* item-12: close-state badge (promoted=green / dismissed=grey). */}
              {selectedFile.status === "promoted" && (
                <span className="mc-badge mc-badge-ok" style={{ marginLeft: "0.4rem" }}>promoted</span>
              )}
              {selectedFile.status === "dismissed" && (
                <span className="mc-badge mc-badge-dim" style={{ marginLeft: "0.4rem" }}>dismissed</span>
              )}
            </div>
          </div>

          {childArtifacts.length > 0 && (
            <div className="mb-3">
              <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginBottom: "0.25rem" }}>
                Attached evidence ({childArtifacts.length}):
              </div>
              <div className="d-flex flex-wrap gap-2">
                {childArtifacts.map((n) => (
                  <span key={`${n.kind}:${n.id}`} className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }} title={n.title}>
                    📎 {n.id}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* T-0275: render feedback body as markdown. */}
          <Markdown source={selectedFile.content} slug={slug} />
        </>
      )}
    </>
  );
}
