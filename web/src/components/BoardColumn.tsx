import { useRef, useState } from "react";
import { Task } from "../api";
import { canonicalOf, type CanonicalState } from "../canonicalStatus";
import { TaskCard, MenuAction, SubtaskRow } from "./TaskCard";

// T-0889: a column is keyed by an internal status OR by a canonical state.
// The board now renders the canonical four (he asked for «свести к одному»),
// while other surfaces may still key a column by an internal status. Drag is
// the only consumer that cares which it is, and drag is inert on every current
// caller (T-0674, board is read-first), so the union is safe here rather than
// being a cast waiting to be wrong.
export type ColumnKey = Task["status"] | CanonicalState;

interface BoardColumnProps {
  title: string;
  status: ColumnKey;
  tasks: Task[];
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
  onMove: (taskId: string, fromStatus: Task["status"], toStatus: ColumnKey) => void;
  onReorder: (taskId: string, status: ColumnKey, targetIndex: number) => void;
  // T-0058: rail mode. `null`/undefined = normal column. "collapsed" = thin
  // vertical drop-strip with a rotated label. "expanded" = normal width but
  // clickable header to collapse back. Toggled via onToggleRail.
  railMode?: "collapsed" | "expanded" | null;
  onToggleRail?: () => void;
  // T-0096: forwarded to every TaskCard rendered by this column.
  hideInitiative?: boolean;
  // T-0512 (M9): subtask nesting. `nestedChildIds` are task ids that render
  // ONLY beneath their parent (suppressed as top-level cards here, even when
  // their own status would land them in this column). `subtasksByParent` maps a
  // parent id → its subtasks (any status), rendered as compact rows under the
  // parent card. Both are computed by the parent (Project.tsx) from the visible
  // task set so a child whose parent isn't visible still shows standalone.
  nestedChildIds?: ReadonlySet<string>;
  subtasksByParent?: Record<string, Task[]>;
}

const DRAG_MIME = "application/x-bot-squad-task";

// T-0663: a parent's subtask list grows unboundedly with total subtask count
// (not active count) because every child renders regardless of status. Mirrors
// the T-0058 rail / T-0272 lane collapse-by-default pattern, applied to
// subtask nesting: split done children out so the caller can hide them by
// default behind a "N done" toggle.
export function splitSubtasksByDone(subtasks: Task[]): { active: Task[]; done: Task[] } {
  const active: Task[] = [];
  const done: Task[] = [];
  for (const t of subtasks) {
    (canonicalOf(t.status) === "done" ? done : active).push(t);
  }
  return { active, done };
}

// Sort ascending by priority; null/undefined sort last. Stable by id as tie-breaker.
export function sortByPriority(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => {
    const ap = a.priority;
    const bp = b.priority;
    const aMissing = ap === null || ap === undefined;
    const bMissing = bp === null || bp === undefined;
    if (aMissing && bMissing) return a.id.localeCompare(b.id);
    if (aMissing) return 1;
    if (bMissing) return -1;
    if (ap === bp) return a.id.localeCompare(b.id);
    return (ap as number) - (bp as number);
  });
}

export function BoardColumn({
  title,
  status,
  tasks,
  slug,
  onMenuAction,
  onMove,
  onReorder,
  railMode = null,
  onToggleRail,
  hideInitiative = false,
  nestedChildIds,
  subtasksByParent,
}: BoardColumnProps) {
  const [isOver, setIsOver] = useState(false);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);
  // T-0663: which parent cards have their done subtasks expanded. Done
  // subtasks are collapsed by default (see splitSubtasksByDone); toggling a
  // parent id here reveals them on demand, per-parent.
  const [expandedDoneParents, setExpandedDoneParents] = useState<Set<string>>(new Set());

  function toggleDoneSubtasks(parentId: string) {
    setExpandedDoneParents((prev) => {
      const next = new Set(prev);
      if (next.has(parentId)) next.delete(parentId);
      else next.add(parentId);
      return next;
    });
  }

  // T-0512: suppress subtasks whose parent is visible — they render nested
  // under the parent, not as standalone top-level cards in this column.
  const topLevel = nestedChildIds && nestedChildIds.size > 0
    ? tasks.filter((t) => !nestedChildIds.has(t.id))
    : tasks;
  const sorted = sortByPriority(topLevel);
  const isCollapsed = railMode === "collapsed";

  function parsePayload(e: React.DragEvent<HTMLDivElement>): { id: string; fromStatus: Task["status"] } | null {
    const payload = e.dataTransfer.getData(DRAG_MIME);
    if (!payload) return null;
    try {
      return JSON.parse(payload) as { id: string; fromStatus: Task["status"] };
    } catch {
      return null;
    }
  }

  // Find which slot (0..N) the pointer is over by comparing clientY to each card's vertical center.
  function computeDropIndex(clientY: number): number {
    const refs = cardRefs.current;
    for (let i = 0; i < refs.length; i++) {
      const el = refs[i];
      if (!el) continue;
      const rect = el.getBoundingClientRect();
      const mid = rect.top + rect.height / 2;
      if (clientY < mid) return i;
    }
    return sorted.length;
  }

  function handleDragOver(e: React.DragEvent<HTMLDivElement>) {
    if (!e.dataTransfer.types.includes(DRAG_MIME)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!isOver) setIsOver(true);
    const idx = computeDropIndex(e.clientY);
    if (idx !== dropIndex) setDropIndex(idx);
  }

  function handleDragLeave(e: React.DragEvent<HTMLDivElement>) {
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsOver(false);
    setDropIndex(null);
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    const targetIndex = dropIndex ?? computeDropIndex(e.clientY);
    setIsOver(false);
    setDropIndex(null);
    const parsed = parsePayload(e);
    if (!parsed) return;
    if (parsed.fromStatus === status) {
      onReorder(parsed.id, status, targetIndex);
      return;
    }
    onMove(parsed.id, parsed.fromStatus, status);
  }

  const showIndicatorAt = (idx: number): boolean =>
    isOver && dropIndex === idx;

  // ---- Collapsed rail: thin vertical strip, drop target only ----
  if (isCollapsed) {
    return (
      <div
        className={`mc-board-col mc-board-rail mc-board-rail-collapsed${isOver ? " drag-over" : ""}`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={onToggleRail}
        role="button"
        tabIndex={0}
        aria-label={`Expand ${title} column (${sorted.length} cards)`}
        title={`Expand ${title}`}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggleRail?.();
          }
        }}
      >
        <div className="mc-board-rail-inner">
          <span className="mc-board-rail-chevron" aria-hidden>▸</span>
          <span className="mc-board-rail-label">{title}</span>
          <span className="mc-board-rail-count">{sorted.length}</span>
        </div>
      </div>
    );
  }

  // ---- Normal / expanded rail: full column render ----
  return (
    <div
      className={`mc-board-col${railMode === "expanded" ? " mc-board-rail-expanded" : ""}${isOver ? " drag-over" : ""}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      <div
        className="mc-board-col-header"
        onClick={railMode === "expanded" ? onToggleRail : undefined}
        style={railMode === "expanded" ? { cursor: "pointer" } : undefined}
        title={railMode === "expanded" ? `Collapse ${title}` : undefined}
      >
        {railMode === "expanded" && (
          <span className="mc-board-rail-chevron-inline" aria-hidden>▾</span>
        )}
        <span>{title}</span>
        <span className="mc-board-count">{sorted.length}</span>
      </div>
      {sorted.length === 0 && (
        <div className="mc-empty-col">▢ empty</div>
      )}
      {sorted.map((t, i) => {
        const kids = subtasksByParent?.[t.id];
        return (
          <div
            key={t.id}
            ref={(el) => { cardRefs.current[i] = el; }}
          >
            {showIndicatorAt(i) && <DropIndicator />}
            <TaskCard task={t} slug={slug} onMenuAction={onMenuAction} hideInitiative={hideInitiative} />
            {/* T-0512 (M9): nest this parent's subtasks directly beneath it.
                T-0663: done subtasks collapse behind a "N done" toggle by
                default so a parent with many completed children doesn't
                grow the board unboundedly with total subtask count. */}
            {kids && kids.length > 0 && (() => {
              const { active, done } = splitSubtasksByDone(kids);
              const expanded = expandedDoneParents.has(t.id);
              const visible = expanded ? kids : active;
              return (
                <div className="mc-subtask-group" style={{ marginBottom: "0.4rem" }}>
                  {sortByPriority(visible).map((k) => (
                    <SubtaskRow key={k.id} task={k} slug={slug} />
                  ))}
                  {done.length > 0 && (
                    <button
                      type="button"
                      className="mc-subtask-done-toggle"
                      onClick={() => toggleDoneSubtasks(t.id)}
                      aria-expanded={expanded}
                      aria-label={
                        expanded
                          ? `Hide ${done.length} done subtasks`
                          : `Show ${done.length} done subtasks`
                      }
                    >
                      {expanded ? `▾ hide ${done.length} done` : `▸ ${done.length} done`}
                    </button>
                  )}
                </div>
              );
            })()}
          </div>
        );
      })}
      {showIndicatorAt(sorted.length) && <DropIndicator />}
    </div>
  );
}

function DropIndicator() {
  return (
    <div
      style={{
        height: 2,
        background: "var(--mc-accent, #4ade80)",
        margin: "4px 0",
        borderRadius: 1,
      }}
    />
  );
}

export { DRAG_MIME };
