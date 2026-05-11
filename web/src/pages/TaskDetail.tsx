import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, Task } from "../api";

const STATUS_OPTIONS: { value: Task["status"]; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "totest", label: "To Test" },
  { value: "reopened", label: "Reopened" },
  { value: "closed", label: "Closed" },
];

function parseComments(body: string): { preamble: string; comments: { heading: string; text: string }[] } {
  const commentsIdx = body.indexOf("\n## Comments");
  if (commentsIdx === -1) {
    return { preamble: body, comments: [] };
  }
  const preamble = body.slice(0, commentsIdx).trim();
  const commentsSection = body.slice(commentsIdx + 1);
  const parts = commentsSection.split(/\n(?=### )/);
  const commentBlocks = parts.slice(1).map((block) => {
    const nl = block.indexOf("\n");
    return nl === -1
      ? { heading: block.trim(), text: "" }
      : { heading: block.slice(0, nl).replace(/^### /, ""), text: block.slice(nl + 1).trim() };
  });
  return { preamble, comments: commentBlocks };
}

export function TaskDetail() {
  const { slug = "", id = "" } = useParams();
  const navigate = useNavigate();

  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [editingTitle, setEditingTitle] = useState(false);
  const [titleValue, setTitleValue] = useState("");
  const titleRef = useRef<HTMLInputElement>(null);

  const [statusValue, setStatusValue] = useState<Task["status"]>("open");

  const [editingBody, setEditingBody] = useState(false);
  const [bodyValue, setBodyValue] = useState("");

  const [commentText, setCommentText] = useState("");
  const [commentError, setCommentError] = useState<string | null>(null);
  const [commentSaving, setCommentSaving] = useState(false);

  const [actionError, setActionError] = useState<string | null>(null);

  function loadTask() {
    setError(null);
    api
      .backlog(slug)
      .then((tasks) => {
        const found = tasks.find((t) => t.id === id);
        if (!found) { setError(`Task ${id} not found`); return; }
        setTask(found);
        setTitleValue(found.title);
        setStatusValue(found.status);
        setBodyValue(found.body);
      })
      .catch((e) => setError(String(e)));
  }

  useEffect(() => {
    loadTask();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, id]);

  useEffect(() => {
    if (editingTitle && titleRef.current) titleRef.current.focus();
  }, [editingTitle]);

  async function saveTitle() {
    if (!task || titleValue.trim() === task.title) { setEditingTitle(false); return; }
    if (!titleValue.trim()) { setEditingTitle(false); return; }
    setSaving(true);
    setActionError(null);
    try {
      await api.patchTask(slug, id, { title: titleValue.trim() });
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
      setEditingTitle(false);
    }
  }

  async function saveStatus(newStatus: Task["status"]) {
    setStatusValue(newStatus);
    setSaving(true);
    setActionError(null);
    try {
      await api.patchTask(slug, id, { status: newStatus });
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function saveBody() {
    setSaving(true);
    setActionError(null);
    try {
      await api.patchTask(slug, id, { body: bodyValue });
      setEditingBody(false);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function postComment() {
    if (!commentText.trim()) { setCommentError("Comment cannot be empty"); return; }
    setCommentSaving(true);
    setCommentError(null);
    try {
      await api.addComment(slug, id, commentText.trim());
      setCommentText("");
      loadTask();
    } catch (e) {
      setCommentError(String(e));
    } finally {
      setCommentSaving(false);
    }
  }

  async function deleteTask() {
    if (!task) return;
    if (!confirm(`Delete task ${task.id}: "${task.title}"?`)) return;
    try {
      await api.deleteTask(slug, id);
      navigate(`/p/${slug}`);
    } catch (e) {
      setActionError(String(e));
    }
  }

  if (error) {
    return (
      <div className="container py-4">
        <nav className="mc-breadcrumb">
          <Link to={`/p/${slug}`}>← Board</Link>
        </nav>
        <div className="alert alert-danger">{error}</div>
      </div>
    );
  }

  if (!task) {
    return (
      <div className="container py-4">
        <nav className="mc-breadcrumb">
          <Link to={`/p/${slug}`}>← Board</Link>
        </nav>
        <div className="mc-loading">Loading</div>
      </div>
    );
  }

  const { preamble, comments } = parseComments(task.body);

  return (
    <div className="container py-4" style={{ maxWidth: "800px" }}>
      <nav className="mc-breadcrumb">
        <Link to={`/p/${slug}`}>← Board</Link>
        <span className="mc-bc-sep">/</span>
        <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
          {task.id}
        </code>
      </nav>

      {actionError && <div className="alert alert-danger">{actionError}</div>}

      {/* Title + status row */}
      <div className="d-flex align-items-start gap-3 mb-3">
        <div className="flex-grow-1">
          {editingTitle ? (
            <input
              ref={titleRef}
              className="form-control fw-semibold"
              style={{ fontSize: "1.05rem" }}
              value={titleValue}
              onChange={(e) => setTitleValue(e.target.value)}
              onBlur={saveTitle}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); saveTitle(); }
                if (e.key === "Escape") { setEditingTitle(false); setTitleValue(task.title); }
              }}
              disabled={saving}
            />
          ) : (
            <h4
              style={{
                cursor: "text",
                marginBottom: 0,
                fontSize: "1.05rem",
                fontWeight: 600,
                color: "var(--mc-text)",
              }}
              title="Click to edit title"
              onClick={() => setEditingTitle(true)}
            >
              {task.title}
            </h4>
          )}
        </div>
        <div style={{ minWidth: "140px" }}>
          <select
            className="form-select form-select-sm"
            value={statusValue}
            onChange={(e) => saveStatus(e.target.value as Task["status"])}
            disabled={saving}
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s.value} value={s.value}>{s.label}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Body */}
      <div className="mb-4">
        <div className="d-flex justify-content-between align-items-center mb-2">
          <div className="mc-section-title" style={{ margin: 0 }}>Body</div>
          {!editingBody && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.72rem" }}
              onClick={() => { setBodyValue(task.body); setEditingBody(true); }}
            >
              Edit
            </button>
          )}
        </div>
        {editingBody ? (
          <>
            <textarea
              className="form-control"
              rows={10}
              value={bodyValue}
              onChange={(e) => setBodyValue(e.target.value)}
              autoFocus
            />
            <div className="mt-2 d-flex gap-2">
              <button type="button" className="btn btn-primary btn-sm" onClick={saveBody} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => { setEditingBody(false); setBodyValue(task.body); }}
              >
                Cancel
              </button>
            </div>
          </>
        ) : (
          <pre className="mc-pre">
            {preamble || <span style={{ color: "var(--mc-text-dim)", fontStyle: "italic" }}>(no body)</span>}
          </pre>
        )}
      </div>

      {/* Comments */}
      <div className="mb-4">
        <div className="mc-section-title">Comments ({comments.length})</div>
        {comments.length === 0 && (
          <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>No comments yet.</p>
        )}
        {comments.map((c, i) => (
          <div
            key={i}
            style={{
              background: "var(--mc-surface-raised)",
              border: "1px solid var(--mc-border)",
              borderLeft: "2px solid var(--mc-border-mid)",
              borderRadius: "3px",
              padding: "0.625rem 0.75rem",
              marginBottom: "0.5rem",
            }}
          >
            <div
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.7rem",
                color: "var(--mc-text-dim)",
                marginBottom: "0.3rem",
              }}
            >
              {c.heading}
            </div>
            <pre
              style={{
                margin: 0,
                fontSize: "0.8rem",
                whiteSpace: "pre-wrap",
                color: "var(--mc-text-mid)",
                fontFamily: "var(--mc-sans)",
              }}
            >
              {c.text}
            </pre>
          </div>
        ))}

        {/* Add comment */}
        <div className="mt-3">
          {commentError && <div className="alert alert-danger py-1 small">{commentError}</div>}
          <textarea
            className="form-control"
            rows={3}
            placeholder="Add a comment…"
            value={commentText}
            onChange={(e) => setCommentText(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-sm btn-primary mt-2"
            onClick={postComment}
            disabled={commentSaving}
          >
            {commentSaving ? "Posting…" : "Post comment"}
          </button>
        </div>
      </div>

      {/* Actions */}
      <div
        className="d-flex gap-2 align-items-center"
        style={{ borderTop: "1px solid var(--mc-border)", paddingTop: "1rem" }}
      >
        <button type="button" className="btn btn-sm btn-outline-danger" onClick={deleteTask}>
          Delete task
        </button>
        {task.from && (
          <Link to={`/p/${slug}/feedback`} className="btn btn-sm btn-outline-secondary">
            View source feedback ({task.from})
          </Link>
        )}
        {task.updated && (
          <span
            style={{
              marginLeft: "auto",
              fontFamily: "var(--mc-mono)",
              fontSize: "0.7rem",
              color: "var(--mc-text-dim)",
            }}
          >
            updated {new Date(task.updated).toLocaleString()}
          </span>
        )}
      </div>
    </div>
  );
}
