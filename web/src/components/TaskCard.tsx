import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Task } from "../api";
import { relativeTime } from "../utils/relativeTime";
import { DRAG_MIME } from "./BoardColumn";

export type MenuAction =
  | { kind: "status"; status: Task["status"] }
  | { kind: "editBody" }
  | { kind: "addComment" }
  | { kind: "delete" };

interface TaskCardProps {
  task: Task;
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
}

const STATUS_OPTIONS: { value: Task["status"]; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "totest", label: "To Test" },
  { value: "reopened", label: "Reopened" },
  { value: "closed", label: "Closed" },
];

function countComments(body: string): number {
  return (body.match(/^### /gm) ?? []).length;
}

export function TaskCard({ task, slug, onMenuAction }: TaskCardProps) {
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const dragSuppressClickRef = useRef(false);
  const commentCount = countComments(task.body);
  const updated = relativeTime(task.updated);

  function handleDragStart(e: React.DragEvent<HTMLDivElement>) {
    e.dataTransfer.setData(
      DRAG_MIME,
      JSON.stringify({ id: task.id, fromStatus: task.status }),
    );
    e.dataTransfer.effectAllowed = "move";
    setIsDragging(true);
    dragSuppressClickRef.current = true;
  }

  function handleDragEnd() {
    setIsDragging(false);
    // Clear the suppress flag on the next tick so the click from the same gesture is ignored
    setTimeout(() => { dragSuppressClickRef.current = false; }, 50);
  }

  function handleCardClick() {
    if (dragSuppressClickRef.current) return;   // ignore click that's part of a drag
    navigate(`/p/${slug}/t/${task.id}`);
  }

  return (
    <div
      className={`mc-task-card${isDragging ? " dragging" : ""}`}
      draggable
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
      onClick={handleCardClick}
    >
      <div className="d-flex justify-content-between align-items-start">
        <div className="mc-task-id">{task.id}</div>
        <div
          data-no-nav
          onClick={(e) => e.stopPropagation()}
          style={{ position: "relative" }}
        >
          <button
            type="button"
            style={{
              background: "none",
              border: "none",
              padding: "0 0.1rem",
              cursor: "pointer",
              color: "var(--mc-text-dim)",
              fontSize: "1rem",
              lineHeight: 1,
            }}
            onClick={() => setMenuOpen((v) => !v)}
            onBlur={() => setTimeout(() => setMenuOpen(false), 150)}
          >
            ⋯
          </button>
          {menuOpen && (
            <div
              style={{
                position: "absolute",
                right: 0,
                top: "100%",
                zIndex: 1050,
                minWidth: "160px",
                background: "var(--mc-surface-raised)",
                border: "1px solid var(--mc-border-mid)",
                borderRadius: "3px",
                boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
              }}
            >
              <div
                style={{
                  fontSize: "0.65rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.07em",
                  color: "var(--mc-text-dim)",
                  padding: "0.4rem 0.625rem",
                  borderBottom: "1px solid var(--mc-border)",
                }}
              >
                Change status
              </div>
              {STATUS_OPTIONS.filter((s) => s.value !== task.status).map((s) => (
                <button
                  key={s.value}
                  type="button"
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    background: "none",
                    border: "none",
                    padding: "0.3rem 0.625rem",
                    fontSize: "0.78rem",
                    color: "var(--mc-text-mid)",
                    cursor: "pointer",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.color = "var(--mc-text)")}
                  onMouseLeave={(e) => (e.currentTarget.style.color = "var(--mc-text-mid)")}
                  onMouseDown={() => {
                    setMenuOpen(false);
                    onMenuAction(task, { kind: "status", status: s.value });
                  }}
                >
                  → {s.label}
                </button>
              ))}
              <div style={{ borderTop: "1px solid var(--mc-border)", margin: "0.2rem 0" }} />
              {(["editBody", "addComment"] as const).map((kind) => (
                <button
                  key={kind}
                  type="button"
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    background: "none",
                    border: "none",
                    padding: "0.3rem 0.625rem",
                    fontSize: "0.78rem",
                    color: "var(--mc-text-mid)",
                    cursor: "pointer",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.color = "var(--mc-text)")}
                  onMouseLeave={(e) => (e.currentTarget.style.color = "var(--mc-text-mid)")}
                  onMouseDown={() => { setMenuOpen(false); onMenuAction(task, { kind }); }}
                >
                  {kind === "editBody" ? "Edit body" : "Add comment"}
                </button>
              ))}
              <div style={{ borderTop: "1px solid var(--mc-border)", margin: "0.2rem 0" }} />
              <button
                type="button"
                style={{
                  display: "block",
                  width: "100%",
                  textAlign: "left",
                  background: "none",
                  border: "none",
                  padding: "0.3rem 0.625rem",
                  fontSize: "0.78rem",
                  color: "var(--mc-accent-danger)",
                  cursor: "pointer",
                }}
                onMouseDown={() => { setMenuOpen(false); onMenuAction(task, { kind: "delete" }); }}
              >
                Delete
              </button>
            </div>
          )}
        </div>
      </div>
      <div className="mc-task-title">{task.title}</div>
      <div className="mc-task-meta">
        {updated && <span>{updated}</span>}
        {commentCount > 0 && (
          <span
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.65rem",
              color: "var(--mc-text-dim)",
              background: "var(--mc-surface-raised)",
              border: "1px solid var(--mc-border)",
              borderRadius: "2px",
              padding: "0 4px",
            }}
          >
            {commentCount} comments
          </span>
        )}
        {task.session && (
          <span
            data-no-nav
            title={`${task.session.status} session ${task.session.sid} — click to view`}
            onClick={(e) => {
              e.stopPropagation();
              navigate(`/p/${slug}/sessions`);
            }}
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.65rem",
              color:
                task.session.status === "active"
                  ? "var(--mc-accent-success, #4ade80)"
                  : "var(--mc-text-dim)",
              background: "var(--mc-surface-raised)",
              border: "1px solid",
              borderColor:
                task.session.status === "active"
                  ? "var(--mc-accent-success, #4ade80)"
                  : "var(--mc-border)",
              borderRadius: "2px",
              padding: "0 4px",
              cursor: "pointer",
            }}
          >
            {task.session.status === "active" ? "● running" : "◌ paused"}
          </span>
        )}
{/* Assign-session affordance lives on the task detail page, not the card. */}
      </div>
    </div>
  );
}
