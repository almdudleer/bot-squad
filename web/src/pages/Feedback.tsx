import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, FeedbackFile } from "../api";
import { Modal } from "../components/Modal";

import { PageHelp } from "../components/PageHelp";
interface EditState {
  name: string;
  draft: string;
}

interface PromoteState {
  name: string;
  title: string;
  body: string;
}

export function Feedback() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();

  const [files, setFiles] = useState<FeedbackFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState<EditState | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [promoting, setPromoting] = useState<PromoteState | null>(null);
  const [promoteError, setPromoteError] = useState<string | null>(null);
  const [promoteSaving, setPromoteSaving] = useState(false);

  function reload() {
    api.feedback(slug).then(setFiles).catch((e) => setError(String(e)));
  }

  useEffect(() => {
    reload();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  function extractTitle(content: string, fallbackName: string): string {
    const match = content.match(/^#\s+(.+)$/m);
    return match ? match[1].trim() : fallbackName.replace(/\.md$/, "").replace(/[-_]/g, " ");
  }

  function startEdit(f: FeedbackFile) {
    setEditing({ name: f.name, draft: f.content });
    setSaveError(null);
  }

  function cancelEdit() {
    setEditing(null);
    setSaveError(null);
  }

  async function saveEdit() {
    if (!editing) return;
    setSaving(true);
    setSaveError(null);
    try {
      await api.putFeedback(slug, editing.name, editing.draft);
      setEditing(null);
      reload();
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  }

  function openPromote(f: FeedbackFile) {
    setPromoting({
      name: f.name,
      title: extractTitle(f.content, f.name),
      body: f.content,
    });
    setPromoteError(null);
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

  return (
    <div className="container py-4" style={{ maxWidth: "860px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Feedback
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        Raw user feedback collected outside any backlog process. Use <strong>Promote
        to task</strong> to lift an item into the board with a <code>from:</code> link back.
        Edit text in place; the file lives at <code>data/&lt;slug&gt;/feedback/</code>.
      </PageHelp>

      {error && <div className="alert alert-danger">{error}</div>}
      {files === null && !error && <div className="mc-loading">Loading</div>}

      {files?.map((f) => (
        <section key={f.name} className="mb-4">
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
              {f.name}
            </div>
            <div className="d-flex gap-2">
              {editing?.name !== f.name && (
                <button
                  type="button"
                  className="btn btn-outline-secondary btn-sm"
                  style={{ fontSize: "0.72rem" }}
                  onClick={() => startEdit(f)}
                >
                  Edit
                </button>
              )}
              <button
                type="button"
                className="btn btn-outline-primary btn-sm"
                style={{ fontSize: "0.72rem" }}
                onClick={() => openPromote(f)}
              >
                Promote to task
              </button>
            </div>
          </div>
          {editing?.name === f.name ? (
            <>
              {saveError && <div className="alert alert-danger py-1 small">{saveError}</div>}
              <textarea
                className="form-control mb-2"
                rows={12}
                value={editing.draft}
                onChange={(e) => setEditing({ ...editing, draft: e.target.value })}
                autoFocus
              />
              <div className="d-flex gap-2">
                <button type="button" className="btn btn-primary btn-sm" onClick={saveEdit} disabled={saving}>
                  {saving ? "Saving…" : "Save"}
                </button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={cancelEdit}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <pre className="mc-pre">{f.content}</pre>
          )}
        </section>
      ))}

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
    </div>
  );
}
