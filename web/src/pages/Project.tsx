import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, Task } from "../api";
import { BoardColumn } from "../components/BoardColumn";
import { Modal } from "../components/Modal";
import { MenuAction } from "../components/TaskCard";

const COLUMNS = ["open", "totest", "reopened", "closed"] as const;
const COLUMN_LABELS: Record<typeof COLUMNS[number], string> = {
  open: "Open",
  totest: "To Test",
  reopened: "Reopened",
  closed: "Closed",
};

type ModalKind = "create" | "editBody" | "addComment" | null;

export function Project() {
  const { slug = "" } = useParams();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // modal state
  const [modalKind, setModalKind] = useState<ModalKind>(null);
  const [activeTask, setActiveTask] = useState<Task | null>(null);

  // create form
  const [newTitle, setNewTitle] = useState("");
  const [newBody, setNewBody] = useState("");
  const [newStatus, setNewStatus] = useState<Task["status"]>("open");

  // edit body form
  const [editBody, setEditBody] = useState("");

  // comment form
  const [commentText, setCommentText] = useState("");

  const [saving, setSaving] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const reload = () => {
    api.backlog(slug).then(setTasks).catch((e) => setError(String(e)));
  };

  useEffect(() => {
    reload();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  const grouped = COLUMNS.reduce<Record<string, Task[]>>((acc, c) => ({ ...acc, [c]: [] }), {});
  for (const t of tasks ?? []) {
    if (COLUMNS.includes(t.status as typeof COLUMNS[number])) {
      grouped[t.status].push(t);
    }
  }

  function openCreate() {
    setNewTitle("");
    setNewBody("");
    setNewStatus("open");
    setModalError(null);
    setModalKind("create");
  }

  function closeModal() {
    setModalKind(null);
    setActiveTask(null);
    setModalError(null);
  }

  async function handleCreate() {
    if (!newTitle.trim()) { setModalError("Title is required"); return; }
    setSaving(true);
    setModalError(null);
    try {
      await api.createTask(slug, { title: newTitle.trim(), body: newBody, status: newStatus });
      closeModal();
      reload();
    } catch (e) {
      setModalError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleEditBody() {
    if (!activeTask) return;
    setSaving(true);
    setModalError(null);
    try {
      await api.patchTask(slug, activeTask.id, { body: editBody });
      closeModal();
      reload();
    } catch (e) {
      setModalError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleAddComment() {
    if (!activeTask) return;
    if (!commentText.trim()) { setModalError("Comment cannot be empty"); return; }
    setSaving(true);
    setModalError(null);
    try {
      await api.addComment(slug, activeTask.id, commentText.trim());
      closeModal();
      reload();
    } catch (e) {
      setModalError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleMenuAction(task: Task, action: MenuAction) {
    if (action.kind === "status") {
      try {
        await api.patchTask(slug, task.id, { status: action.status });
        reload();
      } catch (e) {
        setError(String(e));
      }
    } else if (action.kind === "editBody") {
      setActiveTask(task);
      setEditBody(task.body);
      setModalError(null);
      setModalKind("editBody");
    } else if (action.kind === "addComment") {
      setActiveTask(task);
      setCommentText("");
      setModalError(null);
      setModalKind("addComment");
    } else if (action.kind === "delete") {
      if (!confirm(`Delete task ${task.id}: "${task.title}"?`)) return;
      try {
        await api.deleteTask(slug, task.id);
        reload();
      } catch (e) {
        setError(String(e));
      }
    }
  }

  return (
    <div className="container py-4">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>Backlog</h2>
        <button type="button" className="btn btn-primary btn-sm" onClick={openCreate}>
          + New task
        </button>
      </div>

      {error && <div className="alert alert-danger mt-2">{error}</div>}
      {tasks === null && !error && <div className="mc-loading">Loading</div>}

      <div className="row g-3 mt-1">
        {COLUMNS.map((c) => (
          <BoardColumn
            key={c}
            title={COLUMN_LABELS[c]}
            tasks={grouped[c]}
            slug={slug}
            onMenuAction={handleMenuAction}
          />
        ))}
      </div>

      {/* Create task modal */}
      <Modal
        open={modalKind === "create"}
        title="New task"
        onClose={closeModal}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={handleCreate} disabled={saving}>
              {saving ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <div className="mb-3">
          <label className="form-label">Title *</label>
          <input
            className="form-control"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Body</label>
          <textarea
            className="form-control"
            rows={4}
            value={newBody}
            onChange={(e) => setNewBody(e.target.value)}
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Status</label>
          <select
            className="form-select"
            value={newStatus}
            onChange={(e) => setNewStatus(e.target.value as Task["status"])}
          >
            <option value="open">Open</option>
            <option value="totest">To Test</option>
            <option value="reopened">Reopened</option>
            <option value="closed">Closed</option>
          </select>
        </div>
      </Modal>

      {/* Edit body modal */}
      <Modal
        open={modalKind === "editBody"}
        title={`Edit body — ${activeTask?.id ?? ""}`}
        onClose={closeModal}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={handleEditBody} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <textarea
          className="form-control"
          rows={8}
          value={editBody}
          onChange={(e) => setEditBody(e.target.value)}
          autoFocus
        />
      </Modal>

      {/* Add comment modal */}
      <Modal
        open={modalKind === "addComment"}
        title={`Add comment — ${activeTask?.id ?? ""}`}
        onClose={closeModal}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={handleAddComment} disabled={saving}>
              {saving ? "Posting…" : "Post"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <textarea
          className="form-control"
          rows={4}
          placeholder="Write your comment…"
          value={commentText}
          onChange={(e) => setCommentText(e.target.value)}
          autoFocus
        />
      </Modal>
    </div>
  );
}
