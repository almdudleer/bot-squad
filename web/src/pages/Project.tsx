import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, Task } from "../api";
import { BoardColumn, sortByPriority } from "../components/BoardColumn";
import { Modal } from "../components/Modal";
import { MenuAction } from "../components/TaskCard";

import { PageHelp } from "../components/PageHelp";
const COLUMNS = ["open", "in_progress", "totest", "reopened", "closed"] as const;
const COLUMN_LABELS: Record<typeof COLUMNS[number], string> = {
  open: "Open",
  in_progress: "In progress",
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
      // newBody is the stakeholder's verbatim request — composed by the API
      // into a canonical body with the `## Verbatim request` heading.
      await api.createTask(slug, {
        title: newTitle.trim(),
        verbatim_request: newBody,
        status: newStatus,
      });
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

  async function handleMove(taskId: string, fromStatus: Task["status"], toStatus: Task["status"]) {
    if (fromStatus === toStatus) return;
    // Optimistic update so the card moves immediately
    setTasks((prev) =>
      prev ? prev.map((t) => (t.id === taskId ? { ...t, status: toStatus } : t)) : prev,
    );
    try {
      await api.patchTask(slug, taskId, { status: toStatus });
      reload();
    } catch (e) {
      setError(String(e));
      reload();   // revert to server truth
    }
  }

  // Phase 8: within-column reorder. Sparse-int scheme: pick a value that
  // slots the dragged task between its new neighbors; renumber the column
  // when there's no gap to expand into.
  async function handleReorder(taskId: string, status: Task["status"], targetIndex: number) {
    const current = tasks;
    if (!current) return;
    const columnSorted = sortByPriority(current.filter((t) => t.status === status));
    const without = columnSorted.filter((t) => t.id !== taskId);
    const dragged = columnSorted.find((t) => t.id === taskId);
    if (!dragged) return;

    // Translate the visual target index into the position within `without`.
    // If dragging downward in the same column the index above the original
    // position shifts by one. computeDropIndex used the pre-removal indices,
    // so clamp to the post-removal range.
    const oldIndex = columnSorted.findIndex((t) => t.id === taskId);
    let insertAt = targetIndex;
    if (oldIndex !== -1 && targetIndex > oldIndex) insertAt = targetIndex - 1;
    if (insertAt === oldIndex) return;     // dropped on itself — no-op
    if (insertAt < 0) insertAt = 0;
    if (insertAt > without.length) insertAt = without.length;

    const before = insertAt > 0 ? without[insertAt - 1] : null;
    const after = insertAt < without.length ? without[insertAt] : null;
    const beforeP = before && typeof before.priority === "number" ? before.priority : null;
    const afterP = after && typeof after.priority === "number" ? after.priority : null;

    // Compute the new priority. If we can't pick a clean gap, renumber.
    let newPriority: number | null = null;
    let needsRenumber = false;
    if (beforeP === null && afterP === null) {
      // Empty column (or all neighbors null) → start at 100.
      newPriority = 100;
    } else if (beforeP === null && afterP !== null) {
      // Dropping above all: need top - 100 ≥ 0.
      if (afterP > 100) newPriority = afterP - 100;
      else needsRenumber = true;
    } else if (beforeP !== null && afterP === null) {
      // Dropping below all (priority-wise).
      newPriority = beforeP + 100;
    } else if (beforeP !== null && afterP !== null) {
      if (afterP - beforeP >= 2) newPriority = Math.floor((beforeP + afterP) / 2);
      else needsRenumber = true;
    }

    // Compose the post-move order locally for optimistic UI + renumber.
    const newOrder = [...without.slice(0, insertAt), dragged, ...without.slice(insertAt)];

    if (needsRenumber) {
      // Renumber 100, 200, 300, ...
      const updates = newOrder.map((t, i) => ({ id: t.id, priority: (i + 1) * 100 }));
      setTasks((prev) => {
        if (!prev) return prev;
        const byId = new Map(updates.map((u) => [u.id, u.priority]));
        return prev.map((t) => (byId.has(t.id) ? { ...t, priority: byId.get(t.id) as number } : t));
      });
      try {
        await Promise.all(
          updates.map((u) => api.patchTaskPriority(slug, u.id, u.priority)),
        );
        reload();
      } catch (e) {
        setError(String(e));
        reload();
      }
      return;
    }

    if (newPriority === null) return;
    const finalPriority = newPriority;
    setTasks((prev) =>
      prev ? prev.map((t) => (t.id === taskId ? { ...t, priority: finalPriority } : t)) : prev,
    );
    try {
      await api.patchTaskPriority(slug, taskId, finalPriority);
      reload();
    } catch (e) {
      setError(String(e));
      reload();
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
      <PageHelp>
        Open work for this project across four statuses. <strong>Drag</strong> a card
        to change status, <strong>click</strong> a card for full detail, or <strong>⋯</strong>
        for the quick menu (status / edit body / comment / delete).
      </PageHelp>

      {error && <div className="alert alert-danger mt-2">{error}</div>}
      {tasks === null && !error && <div className="mc-loading">Loading</div>}

      <div className="row g-3 mt-1">
        {COLUMNS.map((c) => (
          <BoardColumn
            key={c}
            title={COLUMN_LABELS[c]}
            status={c}
            tasks={grouped[c]}
            slug={slug}
            onMenuAction={handleMenuAction}
            onMove={handleMove}
            onReorder={handleReorder}
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
          <label className="form-label">Verbatim request — what you literally want (preserved exactly)</label>
          <textarea
            className="form-control"
            rows={5}
            value={newBody}
            onChange={(e) => setNewBody(e.target.value)}
            placeholder="Write your request in your own words. Sessions cannot rewrite this."
          />
          <div
            style={{
              fontSize: "0.7rem",
              color: "var(--mc-text-dim)",
              marginTop: "0.25rem",
            }}
          >
            This is the source-of-truth artifact for the task. Sessions cannot rewrite
            it; they append progress notes below.
          </div>
        </div>
        <div className="mb-3">
          <label className="form-label">Status</label>
          <select
            className="form-select"
            value={newStatus}
            onChange={(e) => setNewStatus(e.target.value as Task["status"])}
          >
            <option value="open">Open</option>
            <option value="in_progress">In progress</option>
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
