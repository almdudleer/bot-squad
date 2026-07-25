import { useEffect, useState } from "react";
import { Link, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { api, DocDetail } from "../api";
import { Markdown } from "../components/Markdown";
import { DocsOutletContext } from "./DocsSection";

// T-0337: Docs is now the DETAIL pane of the unified "Docs & Artifacts" view.
// The shared left rail (type filter + the cross-store tree) lives in
// DocsSection; this page reads that one tree from the Outlet context and
// renders only the doc detail on the right. Deep-link (?doc=D-NNNN) is
// unchanged.
// T-0670 (D-0057 §4/§8, T-0637 Lane C): this pane is now pure read/lookup —
// create/edit/delete/reparent/link-ticket all moved to the TG dialog (R5).
export function Docs() {
  const { slug = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { tree } = useOutletContext<DocsOutletContext>();

  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocDetail | null>(null);

  // Deep-link: /p/:slug/docs?doc=D-NNNN opens that doc (from ticket pages + the
  // shared tree's doc nodes).
  useEffect(() => {
    const d = searchParams.get("doc");
    if (d && d !== selected) open(d);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, slug]);

  async function open(id: string) {
    setSelected(id);
    setDetail(null);
    try {
      setDetail(await api.doc(slug, id));
    } catch (e) {
      setError(String(e));
    }
  }

  // T-0283/D-0029: read the cross-store superset, falling back to the legacy
  // doc-only key.
  const childIds = detail ? (detail.child_artifact_ids ?? detail.child_doc_ids ?? []) : [];

  return (
    <>
      {error && <div className="alert alert-danger py-1 small">{error}</div>}

      {selected === null && <div className="text-muted small">Select an artifact.</div>}

      {selected !== null && detail && (
        <>
          <div className="d-flex justify-content-between align-items-center mb-2">
            <div style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", fontWeight: 600 }}>
              <span title="doc" style={{ marginRight: "0.3rem" }}>📄</span>
              {detail.id}
              <span style={{ color: "var(--mc-text-dim)", marginLeft: "0.5rem" }}>{detail.category}</span>
            </div>
          </div>

          {childIds.length > 0 && (
            <div className="mb-3">
              <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginBottom: "0.25rem" }}>
                Attached artifacts ({childIds.length}):
              </div>
              <div className="d-flex flex-wrap gap-2">
                {childIds.map((cid) => {
                  const n = tree.byId.get(cid);
                  return (
                    <button key={cid} type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }} onClick={() => open(cid)} disabled={!!n && n.kind !== "doc"} title={n ? n.title : cid}>
                      📎 {cid}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Related tickets — the doc→ticket half of the bidirectional mention */}
          <div className="mb-3">
            <div className="mc-section-title" style={{ margin: "0 0 0.4rem 0" }}>Related tickets</div>
            <div className="d-flex flex-wrap gap-2 align-items-center mb-2">
              {(detail.related_tickets ?? []).length === 0 && (
                <span style={{ fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>none yet</span>
              )}
              {(detail.related_tickets ?? []).map((t) => (
                <span key={t} className="d-inline-flex align-items-center gap-1" style={{ fontSize: "0.74rem" }}>
                  <Link to={`/p/${slug}/t/${t}`} style={{ fontFamily: "var(--mc-mono)" }}>{t}</Link>
                </span>
              ))}
            </div>
          </div>

          {/* T-0275: render the doc body as markdown. */}
          <Markdown source={detail.raw} slug={slug} />
        </>
      )}
    </>
  );
}
