import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Task } from "../api";
import { relativeTime } from "../utils/relativeTime";

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
  const commentCount = countComments(task.body);
  const updated = relativeTime(task.updated);

  return (
    <div
      className="card mb-2 task-card"
      style={{ cursor: "pointer" }}
      onClick={() => navigate(`/p/${slug}/t/${task.id}`)}
    >
      <div className="card-body p-2 position-relative">
        <div className="d-flex justify-content-between align-items-start">
          <div className="small text-muted">{task.id}</div>
          <div data-no-nav onClick={(e) => e.stopPropagation()} className="position-relative">
            <button
              type="button"
              className="btn btn-sm btn-link text-muted p-0 lh-1"
              style={{ fontSize: "1.2rem" }}
              onClick={() => setMenuOpen((v) => !v)}
              onBlur={() => setTimeout(() => setMenuOpen(false), 150)}
            >
              ⋯
            </button>
            {menuOpen && (
              <div
                className="card border shadow-sm position-absolute end-0"
                style={{ zIndex: 1050, minWidth: "160px", top: "100%" }}
              >
                <div className="card-body p-1">
                  <div className="small text-muted px-2 py-1">Change status</div>
                  {STATUS_OPTIONS.filter((s) => s.value !== task.status).map((s) => (
                    <button
                      key={s.value}
                      type="button"
                      className="btn btn-sm btn-light w-100 text-start px-2 py-1"
                      onMouseDown={() => {
                        setMenuOpen(false);
                        onMenuAction(task, { kind: "status", status: s.value });
                      }}
                    >
                      → {s.label}
                    </button>
                  ))}
                  <hr className="my-1" />
                  <button
                    type="button"
                    className="btn btn-sm btn-light w-100 text-start px-2 py-1"
                    onMouseDown={() => {
                      setMenuOpen(false);
                      onMenuAction(task, { kind: "editBody" });
                    }}
                  >
                    Edit body
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-light w-100 text-start px-2 py-1"
                    onMouseDown={() => {
                      setMenuOpen(false);
                      onMenuAction(task, { kind: "addComment" });
                    }}
                  >
                    Add comment
                  </button>
                  <hr className="my-1" />
                  <button
                    type="button"
                    className="btn btn-sm btn-danger w-100 text-start px-2 py-1"
                    onMouseDown={() => {
                      setMenuOpen(false);
                      onMenuAction(task, { kind: "delete" });
                    }}
                  >
                    Delete
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
        <div className="fw-medium mt-1">{task.title}</div>
        <div className="d-flex gap-2 mt-1 align-items-center">
          {updated && <span className="small text-muted">{updated}</span>}
          {commentCount > 0 && (
            <span className="badge bg-secondary rounded-pill small">{commentCount}</span>
          )}
        </div>
      </div>
    </div>
  );
}
