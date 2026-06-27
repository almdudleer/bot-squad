import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Task } from "../api";
import {
  CANONICAL_LABELS,
  CANONICAL_STATE,
  type CanonicalState,
} from "../canonicalStatus";
import { relativeTime } from "../utils/relativeTime";
import {
  isRunning,
  sessionActivity,
  sessionGlyph,
  sessionLabel,
  sessionNeedsInput,
} from "../utils/sessionStatus";
import { DRAG_MIME } from "./BoardColumn";

export type MenuAction =
  | { kind: "status"; status: Task["status"] }
  | { kind: "editBody" }
  | { kind: "addComment" }
  | { kind: "setInitiative" }
  | { kind: "delete" };

interface TaskCardProps {
  task: Task;
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
  // T-0096: when the board is already filtered to a single initiative or
  // grouped by initiative, the chip is redundant noise. Parent decides;
  // the card does not infer.
  hideInitiative?: boolean;
}

const STATUS_OPTIONS: { value: Task["status"]; label: string }[] = [
  { value: "planned", label: "Planned" },
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "totest", label: "To Test" },
  { value: "reopened", label: "Reopened" },
  { value: "closed", label: "Closed" },
];

// T-0238: the working area (agent log + user comments) is the progress-notes
// feed — each note is a line "- <ts> · <sid> · <text>". Count those so the
// card reflects working-area activity at a glance (the old `### ` body
// comments are an orphaned legacy channel never rendered on TaskDetail).
export function countNotes(progress: string | undefined): number {
  if (!progress) return 0;
  return progress
    .split("\n")
    .filter((ln) => ln.trim().startsWith("- ")).length;
}

// T-0512 (M9): per-canonical-state dot colour, shared by the abstract-parent
// badge and the nested subtask rows so the board reads consistently.
const CANONICAL_DOT: Record<CanonicalState, string> = {
  backlog: "var(--mc-text-dim)",
  "in-progress": "var(--mc-amber, #fbbf24)",
  validating: "var(--mc-amber, #fbbf24)",
  done: "var(--mc-accent-success, #4ade80)",
};

export function TaskCard({ task, slug, onMenuAction, hideInitiative = false }: TaskCardProps) {
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const dragSuppressClickRef = useRef(false);
  const noteCount = countNotes(task.progress);
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
              {(["editBody", "addComment", "setInitiative"] as const).map((kind) => (
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
                  {kind === "editBody"
                    ? "Edit context"
                    : kind === "addComment"
                      ? "Add comment"
                      : "Set initiative…"}
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
        {/* T-0512 (M9 / Part A): a task split into subtasks is ABSTRACT — its
            status is no longer set independently but DERIVED from its children
            ("dependent on actual work being done in terms of its subtasks").
            Surface the derived canonical state + subtask count so the board
            shows the parent reflecting its children. */}
        {!!task.child_count && task.child_count > 0 && (() => {
          const derived = (task.derived_status ?? null) as CanonicalState | null;
          const dot = derived ? CANONICAL_DOT[derived] : "var(--mc-text-dim)";
          const label = derived ? CANONICAL_LABELS[derived] : "—";
          return (
            <span
              title={`Abstract task — derived from ${task.child_count} subtask${
                task.child_count === 1 ? "" : "s"
              }: ${label}`}
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.65rem",
                color: dot,
                background: "var(--mc-surface-raised)",
                border: `1px solid ${dot}`,
                borderRadius: "2px",
                padding: "0 4px",
              }}
            >
              ⛓ {label} · {task.child_count} subtask{task.child_count === 1 ? "" : "s"}
            </span>
          );
        })()}
        {/* T-0038 stakeholder follow-up #3: surface the bound initiative on
            every card. Click-through goes to the roadmap. Active/draft/done
            tag lives on the swimlane header, not here — keep card noise
            low. T-0096: parent may suppress when the board view already
            disambiguates initiative (single-init filter or group-by). */}
        {!hideInitiative && task.initiative && task.initiative !== "~" && (
          <span
            data-no-nav
            title={`Initiative: ${task.initiative}`}
            onClick={(e) => {
              e.stopPropagation();
              navigate(`/p/${slug}/vision#${encodeURIComponent(task.initiative!)}`);
            }}
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.65rem",
              color: "var(--mc-text-mid)",
              background: "var(--mc-surface-raised)",
              border: "1px solid var(--mc-border)",
              borderRadius: "2px",
              padding: "0 4px",
              cursor: "pointer",
            }}
          >
            ▸ {task.initiative.replace(/\.md$/, "")}
          </span>
        )}
        {noteCount > 0 && (
          <span
            title="Working-area entries (agent notes + your comments)"
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
            {/* T-0366 #7: ⚙ is reserved for settings — notes use 💬. */}
            💬 {noteCount} {noteCount === 1 ? "note" : "notes"}
          </span>
        )}
        {task.session && (() => {
          // T-0104: unify the card label with the detail page and the
          // Sessions board via the shared formatter. Prefers the
          // worker-derived `activity` field when present (the project
          // page enriches task.session.activity by joining /sessions);
          // falls back to mapping the raw md `status` otherwise.
          const act = sessionActivity(task.session);
          const green = isRunning(act);
          // T-0437: mark the bound-session chip when the user has pinned that
          // session ("working closely with this one"). Read-only here — the
          // pin/unpin toggle lives in the Processes view.
          const pinned = !!task.session.pinned;
          return (
            <span
              data-no-nav
              title={`${act} session ${task.session.sid}${pinned ? " · 📌 pinned" : ""} — click to view`}
              onClick={(e) => {
                e.stopPropagation();
                // T-0099: deep-link to the bound session's row.
                navigate(`/p/${slug}/sessions?sid=${encodeURIComponent(task.session!.sid)}`);
              }}
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.65rem",
                color: green
                  ? "var(--mc-accent-success, #4ade80)"
                  : "var(--mc-text-dim)",
                background: "var(--mc-surface-raised)",
                border: "1px solid",
                borderColor: green
                  ? "var(--mc-accent-success, #4ade80)"
                  : "var(--mc-border)",
                borderRadius: "2px",
                padding: "0 4px",
                cursor: "pointer",
              }}
            >
              {pinned && <span style={{ marginRight: "2px" }}>📌</span>}
              {sessionGlyph(act)} {sessionLabel(act)}
            </span>
          );
        })()}
        {/* T-0404 (PASS-2 P2-07): badge a card whose bound process is blocked
            waiting on the operator, so the board itself shows WHICH task needs
            input — not just the Sessions list. Reuses the shared
            sessionNeedsInput predicate (board enriches task.session with the
            live awaiting_input). */}
        {task.session && sessionNeedsInput(task.session) && (
          <span
            title="This task's process is waiting on you (blocked on an operator reply)."
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.65rem",
              color: "var(--mc-amber)",
              background: "var(--mc-surface-raised)",
              border: "1px solid var(--mc-amber)",
              borderRadius: "2px",
              padding: "0 4px",
              marginLeft: "0.25rem",
            }}
          >
            ⏳ needs input
          </span>
        )}
{/* Assign-session affordance lives on the task detail page, not the card. */}
      </div>
    </div>
  );
}

// T-0512 (M9): a compact row for a subtask rendered NESTED beneath its parent
// card on the board. Because a parent's children may sit in different status
// columns, the nested row carries its OWN canonical-state dot so its progress
// is legible out of column context. Click-through opens the subtask detail.
export function SubtaskRow({ task, slug }: { task: Task; slug: string }) {
  const navigate = useNavigate();
  const canonical = CANONICAL_STATE[task.status];
  const dot = CANONICAL_DOT[canonical];
  return (
    <div
      className="mc-subtask-row"
      onClick={() => navigate(`/p/${slug}/t/${task.id}`)}
      title={`${task.id} · ${CANONICAL_LABELS[canonical]} — open subtask`}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.4rem",
        padding: "0.2rem 0.4rem 0.2rem 0.85rem",
        cursor: "pointer",
        fontSize: "0.72rem",
        borderLeft: "2px solid var(--mc-border)",
        marginLeft: "0.35rem",
      }}
    >
      <span
        aria-hidden
        style={{
          flex: "0 0 auto",
          width: "7px",
          height: "7px",
          borderRadius: "50%",
          background: dot,
        }}
      />
      <span
        className="mc-task-id"
        style={{ flex: "0 0 auto", fontSize: "0.65rem" }}
      >
        {task.id}
      </span>
      <span
        style={{
          flex: "1 1 auto",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          color: "var(--mc-text-mid)",
        }}
      >
        {task.title}
      </span>
      <span
        style={{
          flex: "0 0 auto",
          fontFamily: "var(--mc-mono)",
          fontSize: "0.6rem",
          color: dot,
        }}
      >
        {CANONICAL_LABELS[canonical]}
      </span>
    </div>
  );
}
