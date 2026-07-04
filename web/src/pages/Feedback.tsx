import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { api, FeedbackFile, errorDetail } from "../api";
import { Modal } from "../components/Modal";
import { Markdown } from "../components/Markdown";
import { ReparentControl } from "../components/ArtifactTree";
import { DocsOutletContext } from "./DocsSection";

interface PromoteState {
  name: string;
  title: string;
  body: string;
}

// T-0337: Feedback is now the DETAIL pane of the unified "Docs & Artifacts"
// view. The shared cross-store tree + filter live in DocsSection; this page
// reads that tree from the Outlet context and renders only the feedback
// detail/editor on the right. Deep-link (?fb=<name>) is unchanged.
export function Feedback() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // T-0572 (Occam pass): `manage` gates every write affordance — read-first.
  const { tree, manage } = useOutletContext<DocsOutletContext>();

  const [files, setFiles] = useState<FeedbackFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const [selected, setSelected] = useState<string | null>(null); // feedback filename
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [promoting, setPromoting] = useState<PromoteState | null>(null);
  const [promoteError, setPromoteError] = useState<string | null>(null);
  const [promoteSaving, setPromoteSaving] = useState(false);

  function reloadFiles() {
    api.feedback(slug).then(setFiles).catch((e) => setError(String(e)));
  }

  useEffect(() => {
    reloadFiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // Deep-link: /p/:slug/docs/feedback?fb=<name> selects that item (from the
  // shared artifact tree's feedback nodes).
  useEffect(() => {
    const f = searchParams.get("fb");
    if (f && f !== selected) { setSelected(f); setDraft(null); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const selectedFile = files?.find((f) => f.name === selected) ?? null;

  function extractTitle(content: string, fallbackName: string): string {
    const match = content.match(/^#\s+(.+)$/m);
    return match ? match[1].trim() : fallbackName.replace(/\.md$/, "").replace(/[-_]/g, " ");
  }

  async function saveEdit() {
    if (!selectedFile || draft === null) return;
    setBusy(true);
    setError(null);
    try {
      await api.putFeedback(slug, selectedFile.name, draft);
      setDraft(null);
      reloadFiles();
      tree.reload();
      setFlash("Saved.");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // T-0283/D-0029: re-parent this feedback theme under any artifact (cross-store,
  // cycle-safe on the BE; surface a 4xx cycle/validation detail inline).
  async function setFbParent(parentId: string | null) {
    if (!selectedFile) return;
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      await api.setFeedbackParent(slug, selectedFile.name, parentId);
      reloadFiles();
      tree.reload();
      setFlash(parentId ? `Attached under ${parentId}.` : "Detached.");
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }

  function openPromote(f: FeedbackFile) {
    setPromoting({ name: f.name, title: extractTitle(f.content, f.name), body: f.content });
    setPromoteError(null);
  }

  // item-12 (Fork-4 close): dismiss is the DOMINANT operator action on a
  // feedback item (most evidence is acknowledged + dropped, not promoted to a
  // task). Owner-gated POST → the BE sets status:dismissed; the item then
  // default-hides from the rail. Refetch both the page files + the shared tree.
  async function handleDismiss(f: FeedbackFile) {
    if (!window.confirm(`Dismiss "${f.name}"? It's kept on disk (status: dismissed) but hidden from the default list.`)) return;
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      await api.dismissFeedback(slug, f.name);
      setSelected(null);
      reloadFiles();
      tree.reload();
      setFlash(`Dismissed ${f.name}.`);
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }

  async function submitPromote() {
    if (!promoting) return;
    if (!promoting.title.trim()) { setPromoteError("Title is required"); return; }
    setPromoteSaving(true);
    setPromoteError(null);
    try {
      const result = await api.promoteFeedback(slug, promoting.name, promoting.title.trim(), promoting.body);
      setPromoting(null);
      navigate(`/p/${slug}/t/${result.task_id}`);
    } catch (e) {
      setPromoteError(String(e));
    } finally {
      setPromoteSaving(false);
    }
  }

  // T-0283: the feedback theme's canonical id (its parent-edge target) is the
  // filename stem; child artifacts (evidence) hang off it cross-store.
  const selfId = selectedFile ? (selectedFile.id ?? selectedFile.name.replace(/\.md$/, "")) : "";
  const childArtifacts = selfId ? (tree.childrenOf.get(selfId) ?? []) : [];

  return (
    <>
      {error && <div className="alert alert-danger py-1 small">{error}</div>}
      {flash && <div className="alert alert-success py-1 small">{flash}</div>}

      {files === null && !error && <div className="mc-loading">Loading</div>}
      {selectedFile === null && files !== null && <div className="text-muted small">Select a feedback item.</div>}

      {selectedFile && draft === null && (
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
            {manage && (
              <div className="d-flex gap-2">
                <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem" }} onClick={() => setDraft(selectedFile.content)}>
                  Edit
                </button>
                <button type="button" className="btn btn-outline-primary btn-sm" style={{ fontSize: "0.72rem" }} onClick={() => openPromote(selectedFile)}>
                  Promote to task
                </button>
                {/* item-12: dismiss = the dominant operator action (acknowledge +
                    drop without making a task). Hidden once already closed. */}
                {selectedFile.status !== "dismissed" && selectedFile.status !== "promoted" && (
                  <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={() => handleDismiss(selectedFile)}>
                    Dismiss
                  </button>
                )}
              </div>
            )}
          </div>

          {/* T-0283/D-0029: cross-store nesting — set/clear this theme's
              parent (PUT /feedback/{name}/parent, cycle-safe).
              T-0572: curation control, manage-gated. */}
          {manage && (
            <ReparentControl
              slug={slug}
              data={tree}
              selfId={selfId}
              currentParentId={selectedFile.parent_doc_id}
              busy={busy}
              onSetParent={setFbParent}
            />
          )}
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

      {selectedFile && draft !== null && (
        <>
          <textarea
            className="form-control mb-2"
            rows={16}
            value={draft}
            style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
          />
          <div className="d-flex gap-2">
            <button type="button" className="btn btn-primary btn-sm" onClick={saveEdit} disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setDraft(null)}>
              Cancel
            </button>
          </div>
        </>
      )}

      {/* Promote modal */}
      <Modal
        open={promoting !== null}
        title="Promote to task"
        onClose={() => setPromoting(null)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setPromoting(null)}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={submitPromote} disabled={promoteSaving}>
              {promoteSaving ? "Promoting…" : "Create task"}
            </button>
          </>
        }
      >
        {promoteError && <div className="alert alert-danger">{promoteError}</div>}
        <div className="mb-3">
          <label className="form-label">Task title *</label>
          <input
            className="form-control"
            value={promoting?.title ?? ""}
            onChange={(e) => promoting && setPromoting({ ...promoting, title: e.target.value })}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Task body</label>
          <textarea
            className="form-control"
            rows={8}
            value={promoting?.body ?? ""}
            onChange={(e) => promoting && setPromoting({ ...promoting, body: e.target.value })}
          />
        </div>
      </Modal>
    </>
  );
}
