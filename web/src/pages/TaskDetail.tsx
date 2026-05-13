import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, SessionRow, Task } from "../api";

const STATUS_OPTIONS: { value: Task["status"]; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "totest", label: "To Test" },
  { value: "reopened", label: "Reopened" },
  { value: "closed", label: "Closed" },
];

type ProgressEntry = { ts: string; sid: string; text: string };

function parseProgressList(progress: string): ProgressEntry[] {
  if (!progress) return [];
  // Each line: "- <ts> · <sid> · <text>"
  return progress
    .split("\n")
    .map((ln) => ln.trim())
    .filter((ln) => ln.startsWith("- "))
    .map((ln) => {
      const rest = ln.slice(2);
      const parts = rest.split(" · ");
      if (parts.length < 3) return { ts: "", sid: "", text: rest };
      return { ts: parts[0], sid: parts[1], text: parts.slice(2).join(" · ") };
    });
}

export function TaskDetail() {
  const { slug = "", id = "" } = useParams();
  const navigate = useNavigate();

  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [activeDevs, setActiveDevs] = useState<SessionRow[]>([]);

  const [editingTitle, setEditingTitle] = useState(false);
  const [titleValue, setTitleValue] = useState("");
  const titleRef = useRef<HTMLInputElement>(null);

  const [statusValue, setStatusValue] = useState<Task["status"]>("open");

  // Section edit state — each section saves independently.
  const [editingVerbatim, setEditingVerbatim] = useState(false);
  const [verbatimValue, setVerbatimValue] = useState("");

  const [editingContext, setEditingContext] = useState(false);
  const [contextValue, setContextValue] = useState("");

  const [progressText, setProgressText] = useState("");
  const [progressError, setProgressError] = useState<string | null>(null);
  const [progressSaving, setProgressSaving] = useState(false);

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
        setVerbatimValue(found.verbatim ?? "");
        setContextValue(found.context ?? "");
      })
      .catch((e) => setError(String(e)));
    // Phase 9: surface active devs so the user can bind this task to an
    // already-running dev (multi-binding). Silent on error.
    api.sessions(slug)
      .then((rows) => {
        setActiveDevs(rows.filter((s) => {
          if (s.status !== "active") return false;
          const tid = (s.task_id ?? "").trim();
          return Boolean(tid) && tid !== "~";
        }));
      })
      .catch(() => setActiveDevs([]));
  }

  async function bindToDev(sid: string) {
    if (!task) return;
    try {
      await api.bindTask(slug, sid, task.id);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    }
  }

  async function unassignSession() {
    if (!task || !task.session) return;
    // unbind_task only works on extras — for the session's primary task
    // we'd be erasing the session's identity, which the worker refuses.
    if (!window.confirm(`Unbind ${task.id} from ${task.session.sid}?`)) return;
    try {
      await api.unbindTask(slug, task.session.sid, task.id);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    }
  }

  useEffect(() => {
    loadTask();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, id]);

  useEffect(() => {
    if (editingTitle && titleRef.current) titleRef.current.focus();
  }, [editingTitle]);

  function composeBody(verbatim: string, context: string, progress: string): string {
    const parts: string[] = [];
    const v = verbatim.trim();
    const c = context.trim();
    const p = progress.trim();
    if (v) parts.push(`## Verbatim request\n\n${v}\n`);
    if (c) parts.push(`## Context\n\n${c}\n`);
    if (p) parts.push(`## Progress\n\n${p}\n`);
    return parts.join("\n");
  }

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

  async function saveVerbatim() {
    if (!task) return;
    setSaving(true);
    setActionError(null);
    try {
      const body = composeBody(verbatimValue, task.context ?? "", task.progress ?? "");
      await api.patchTask(slug, id, { body });
      setEditingVerbatim(false);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function saveContext() {
    if (!task) return;
    setSaving(true);
    setActionError(null);
    try {
      const body = composeBody(task.verbatim ?? "", contextValue, task.progress ?? "");
      await api.patchTask(slug, id, { body });
      setEditingContext(false);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function postProgress() {
    if (!progressText.trim()) { setProgressError("Progress note cannot be empty"); return; }
    setProgressSaving(true);
    setProgressError(null);
    try {
      // SID for stakeholder-added notes — keep this short + identifiable.
      await api.addProgress(slug, id, "S-stakeholder", progressText.trim());
      setProgressText("");
      loadTask();
    } catch (e) {
      setProgressError(String(e));
    } finally {
      setProgressSaving(false);
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
        <div className="alert alert-danger">{error}</div>
      </div>
    );
  }

  if (!task) {
    return (
      <div className="container py-4">
        <div className="mc-loading">Loading</div>
      </div>
    );
  }

  const progressEntries = parseProgressList(task.progress ?? "").reverse();

  return (
    <div className="container py-4" style={{ maxWidth: "800px" }}>
      {/* Back link */}
      <div className="mb-2">
        <Link
          to={`/p/${slug}`}
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.72rem",
            color: "var(--mc-text-dim)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            textDecoration: "none",
          }}
        >
          ← back to board
        </Link>
      </div>

      {actionError && <div className="alert alert-danger">{actionError}</div>}

      {/* Title row */}
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
      </div>

      {/* Two-row control strip — keep the status dropdown and the session
          control on separate rows so the page doesn't look crowded. */}
      <div className="mb-2 d-flex align-items-center gap-2" style={{ fontSize: "0.78rem" }}>
        <label
          htmlFor="task-status"
          style={{
            color: "var(--mc-text-dim)",
            fontFamily: "var(--mc-mono)",
            minWidth: "5.5rem",
            margin: 0,
          }}
        >
          status:
        </label>
        <select
          id="task-status"
          className="form-select form-select-sm"
          style={{ width: "auto", minWidth: "10rem" }}
          value={statusValue}
          onChange={(e) => saveStatus(e.target.value as Task["status"])}
          disabled={saving}
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s.value} value={s.value}>{s.label}</option>
          ))}
        </select>
      </div>

      {/* Unified dev-session select: one control replaces the three older
          affordances (current SID line, "Assign session" button, "or bind"
          select). Selecting __new__ opens the new-session flow; selecting
          any other entry binds the task to that existing dev. */}
      <div className="mb-3 d-flex align-items-center gap-2" style={{ fontSize: "0.78rem" }}>
        <label
          htmlFor="task-session"
          style={{
            color: "var(--mc-text-dim)",
            fontFamily: "var(--mc-mono)",
            minWidth: "5.5rem",
            margin: 0,
          }}
        >
          dev session:
        </label>
        <select
          id="task-session"
          className="form-select form-select-sm"
          style={{ width: "auto", minWidth: "16rem", maxWidth: "30rem" }}
          value={task.session ? task.session.sid : ""}
          onChange={(e) => {
            const value = e.target.value;
            if (value === "__new__") {
              // Re-select the current binding so the dropdown stays sane
              // if the user navigates back without spawning.
              navigate(
                `/p/${slug}/sessions?role=dev&task=${encodeURIComponent(task.id)}`,
              );
              return;
            }
            if (!value) return;
            bindToDev(value);
          }}
          disabled={saving}
        >
          <option value="" disabled>
            — none —
          </option>
          <option value="__new__">+ Create new dev session…</option>
          {/* Surface the current binding even if it's not in activeDevs
              (e.g. paused/suspended) so the select reflects reality. */}
          {task.session && !activeDevs.some((d) => d.sid === task.session!.sid) && (
            <option value={task.session.sid}>
              {task.session.sid} ({task.session.status})
            </option>
          )}
          {activeDevs.map((d) => (
            <option key={d.sid} value={d.sid}>
              {d.window || d.sid} ({d.sid})
            </option>
          ))}
        </select>
        {task.session && (
          <button
            type="button"
            className="btn btn-outline-secondary btn-sm"
            style={{ fontSize: "0.7rem" }}
            title="Clear the dev-session binding for this task"
            onClick={unassignSession}
            disabled={saving}
          >
            Unassign
          </button>
        )}
        {task.session && (
          <span
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.7rem",
              color:
                task.session.status === "active"
                  ? "var(--mc-accent-success, #4ade80)"
                  : "var(--mc-text-dim)",
            }}
            title={`session status: ${task.session.status}`}
          >
            {task.session.status === "active" ? "●" : "◌"} {task.session.status}
          </span>
        )}
      </div>

      {/* Verbatim request — source-of-truth section */}
      <div className="mb-4">
        <div className="d-flex justify-content-between align-items-center mb-2">
          <div className="mc-section-title" style={{ margin: 0 }}>
            Verbatim request — source of truth
          </div>
          {!editingVerbatim && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.72rem" }}
              onClick={() => { setVerbatimValue(task.verbatim ?? ""); setEditingVerbatim(true); }}
            >
              Edit
            </button>
          )}
        </div>
        <div
          style={{
            fontSize: "0.7rem",
            color: "var(--mc-text-dim)",
            marginBottom: "0.4rem",
          }}
        >
          The anti-broken-phone record. Sessions do not edit this — only the
          stakeholder. They append progress notes below.
        </div>
        {editingVerbatim ? (
          <>
            <textarea
              className="form-control"
              rows={6}
              value={verbatimValue}
              onChange={(e) => setVerbatimValue(e.target.value)}
              autoFocus
            />
            <div className="mt-2 d-flex gap-2">
              <button type="button" className="btn btn-primary btn-sm" onClick={saveVerbatim} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => { setEditingVerbatim(false); setVerbatimValue(task.verbatim ?? ""); }}
              >
                Cancel
              </button>
            </div>
          </>
        ) : (
          <pre
            className="mc-pre"
            style={{
              borderLeft: "3px solid var(--mc-accent-success, #4ade80)",
              paddingLeft: "0.75rem",
            }}
          >
            {task.verbatim?.trim() ? task.verbatim : (
              <span style={{ color: "var(--mc-text-dim)", fontStyle: "italic" }}>
                (no verbatim request recorded)
              </span>
            )}
          </pre>
        )}
      </div>

      {/* Context — optional TL clarification */}
      <div className="mb-4">
        <div className="d-flex justify-content-between align-items-center mb-2">
          <div className="mc-section-title" style={{ margin: 0 }}>Context (optional)</div>
          {!editingContext && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.72rem" }}
              onClick={() => { setContextValue(task.context ?? ""); setEditingContext(true); }}
            >
              Edit
            </button>
          )}
        </div>
        {editingContext ? (
          <>
            <textarea
              className="form-control"
              rows={4}
              value={contextValue}
              onChange={(e) => setContextValue(e.target.value)}
              autoFocus
              placeholder="Short TL clarification — keep it brief."
            />
            <div className="mt-2 d-flex gap-2">
              <button type="button" className="btn btn-primary btn-sm" onClick={saveContext} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => { setEditingContext(false); setContextValue(task.context ?? ""); }}
              >
                Cancel
              </button>
            </div>
          </>
        ) : task.context?.trim() ? (
          <pre className="mc-pre">{task.context}</pre>
        ) : (
          <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>(no context yet)</p>
        )}
      </div>

      {/* Progress — read-only list, append-only via the worker action */}
      <div className="mb-4">
        <div className="mc-section-title">Progress ({progressEntries.length})</div>
        {progressEntries.length === 0 && (
          <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>No progress notes yet.</p>
        )}
        {progressEntries.map((p, i) => (
          <div
            key={i}
            style={{
              background: "var(--mc-surface-raised)",
              border: "1px solid var(--mc-border)",
              borderRadius: "3px",
              padding: "0.4rem 0.625rem",
              marginBottom: "0.35rem",
              fontSize: "0.78rem",
              color: "var(--mc-text-mid)",
            }}
          >
            <span
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.68rem",
                color: "var(--mc-text-dim)",
                marginRight: "0.5rem",
              }}
            >
              {p.ts} · {p.sid}
            </span>
            {p.text}
          </div>
        ))}

        <div className="mt-3">
          {progressError && <div className="alert alert-danger py-1 small">{progressError}</div>}
          <input
            type="text"
            className="form-control form-control-sm"
            placeholder="Add progress note (cap 240 chars)…"
            maxLength={240}
            value={progressText}
            onChange={(e) => setProgressText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); postProgress(); } }}
          />
          <button
            type="button"
            className="btn btn-sm btn-primary mt-2"
            onClick={postProgress}
            disabled={progressSaving}
          >
            {progressSaving ? "Posting…" : "Add progress note"}
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
