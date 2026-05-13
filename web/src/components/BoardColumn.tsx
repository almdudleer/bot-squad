import { useRef, useState } from "react";
import { Task } from "../api";
import { TaskCard, MenuAction } from "./TaskCard";

interface BoardColumnProps {
  title: string;
  status: Task["status"];
  tasks: Task[];
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
  onMove: (taskId: string, fromStatus: Task["status"], toStatus: Task["status"]) => void;
  onReorder: (taskId: string, status: Task["status"], targetIndex: number) => void;
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
  tasks,
  slug,
  onMenuAction,
  onMove,
  onReorder,
}: BoardColumnProps) {
  const [isOver, setIsOver] = useState(false);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);

  const sorted = sortByPriority(tasks);

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

  return (
    <div
      className={`col-md-3 mc-board-col${isOver ? " drag-over" : ""}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      <div className="mc-board-col-header">
        <span>{title}</span>
        <span className="mc-board-count">{tasks.length}</span>
      </div>
      {sorted.length === 0 && (
        <div className="mc-empty-col">▢ empty</div>
      )}
      {sorted.map((t, i) => (
        <div
          key={t.id}
          ref={(el) => { cardRefs.current[i] = el; }}
        >
          {showIndicatorAt(i) && <DropIndicator />}
          <TaskCard task={t} slug={slug} onMenuAction={onMenuAction} />
        </div>
      ))}
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
