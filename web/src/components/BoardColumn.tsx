import { useRef, useState } from "react";
import { Task } from "../api";
import { TaskCard, MenuAction, SubtaskRow } from "./TaskCard";

interface BoardColumnProps {
  title: string;
  status: Task["status"];
  // T-0479: the canonical 4-state label this internal column rolls up into
  // (e.g. "Backlog" for planned/open/reopened). Rendered as a small kicker
  // above the column title so the board presents the canonical model while
  // keeping the richer internal statuses. Omitted on the collapsed rail strip.
  canonical?: string;
  tasks: Task[];
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
  onMove: (taskId: string, fromStatus: Task["status"], toStatus: Task["status"]) => void;
  onReorder: (taskId: string, status: Task["status"], targetIndex: number) => void;
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
  canonical,
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
      {canonical && (
        <div className="mc-board-canonical-kicker">{canonical}</div>
      )}
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
            {/* T-0512 (M9): nest this parent's subtasks directly beneath it,
                grouped under the parent regardless of each child's own status. */}
            {kids && kids.length > 0 && (
              <div className="mc-subtask-group" style={{ marginBottom: "0.4rem" }}>
                {sortByPriority(kids).map((k) => (
                  <SubtaskRow key={k.id} task={k} slug={slug} />
                ))}
              </div>
            )}
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
