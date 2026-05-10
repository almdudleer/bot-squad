import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, FeedbackFile } from "../api";
import { Modal } from "../components/Modal";

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

  /** Extract the first H1 line from markdown content as a fallback title */
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
    <div className="container py-4">
      <nav className="mb-3">
        <Link to={`/p/${slug}`}>← Board</Link>
      </nav>
      <h2>Feedback — {slug}</h2>
      {error && <div className="alert alert-danger">{error}</div>}
      {files === null && !error && <p>Loading…</p>}
      {files?.map((f) => (
        <section key={f.name} className="mb-4">
          <div className="d-flex justify-content-between align-items-center mb-1">
            <h5 className="text-muted small mb-0">{f.name}</h5>
            <div className="d-flex gap-2">
              {editing?.name !== f.name && (
                <button type="button" className="btn btn-sm btn-outline-secondary" onClick={() => startEdit(f)}>
                  Edit
                </button>
              )}
              <button
                type="button"
                className="btn btn-sm btn-outline-primary"
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
            <pre className="bg-light p-3 small" style={{ whiteSpace: "pre-wrap" }}>
              {f.content}
            </pre>
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
