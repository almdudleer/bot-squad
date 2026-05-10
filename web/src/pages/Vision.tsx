import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, VisionFile } from "../api";
import { Modal } from "../components/Modal";

interface EditState {
  name: string;
  draft: string;
}

export function Vision() {
  const { slug = "" } = useParams();
  const [files, setFiles] = useState<VisionFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState<EditState | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // New initiative modal
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState("");
  const [newContent, setNewContent] = useState("");
  const [newError, setNewError] = useState<string | null>(null);
  const [newSaving, setNewSaving] = useState(false);

  function reload() {
    api.vision(slug).then(setFiles).catch((e) => setError(String(e)));
  }

  useEffect(() => {
    reload();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  function startEdit(f: VisionFile) {
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
      await api.putVision(slug, editing.name, editing.draft);
      setEditing(null);
      reload();
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function createInitiative() {
    if (!newName.trim()) { setNewError("Name is required"); return; }
    setNewSaving(true);
    setNewError(null);
    try {
      await api.newInitiative(slug, newName.trim(), newContent || `# ${newName.trim()}\n`);
      setShowNew(false);
      setNewName("");
      setNewContent("");
      reload();
    } catch (e) {
      setNewError(String(e));
    } finally {
      setNewSaving(false);
    }
  }

  return (
    <div className="container py-4">
      <nav className="mb-3">
        <Link to={`/p/${slug}`}>← Board</Link>
      </nav>
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2>Vision — {slug}</h2>
        <button type="button" className="btn btn-sm btn-primary" onClick={() => { setShowNew(true); setNewError(null); }}>
          + New initiative
        </button>
      </div>
      {error && <div className="alert alert-danger">{error}</div>}
      {files === null && !error && <p>Loading…</p>}
      {files?.map((f) => (
        <section key={f.name} className="mb-4">
          <div className="d-flex justify-content-between align-items-center mb-1">
            <h5 className="text-muted small mb-0">{f.name}</h5>
            {editing?.name !== f.name && (
              <button type="button" className="btn btn-sm btn-outline-secondary" onClick={() => startEdit(f)}>
                Edit
              </button>
            )}
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

      {/* New initiative modal */}
      <Modal
        open={showNew}
        title="New initiative"
        onClose={() => setShowNew(false)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setShowNew(false)}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={createInitiative} disabled={newSaving}>
              {newSaving ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {newError && <div className="alert alert-danger">{newError}</div>}
        <div className="mb-3">
          <label className="form-label">Name *</label>
          <input
            className="form-control"
            placeholder="e.g. API gateway rollout"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Initial content</label>
          <textarea
            className="form-control"
            rows={5}
            placeholder="(optional — defaults to a heading)"
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
          />
        </div>
      </Modal>
    </div>
  );
}
