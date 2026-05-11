import { useState } from "react";
import { Task } from "../api";
import { TaskCard, MenuAction } from "./TaskCard";

interface BoardColumnProps {
  title: string;
  status: Task["status"];
  tasks: Task[];
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
  onMove: (taskId: string, fromStatus: Task["status"], toStatus: Task["status"]) => void;
}

const DRAG_MIME = "application/x-bot-squad-task";

export function BoardColumn({ title, status, tasks, slug, onMenuAction, onMove }: BoardColumnProps) {
  const [isOver, setIsOver] = useState(false);

  function handleDragOver(e: React.DragEvent<HTMLDivElement>) {
    if (e.dataTransfer.types.includes(DRAG_MIME)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!isOver) setIsOver(true);
    }
  }

  function handleDragLeave(e: React.DragEvent<HTMLDivElement>) {
    // Only clear if the pointer truly left the column, not when entering a child.
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsOver(false);
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setIsOver(false);
    const payload = e.dataTransfer.getData(DRAG_MIME);
    if (!payload) return;
    try {
      const { id, fromStatus } = JSON.parse(payload) as { id: string; fromStatus: Task["status"] };
      if (fromStatus === status) return;     // no-op drop on same column
      onMove(id, fromStatus, status);
    } catch {
      /* ignore malformed */
    }
  }

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
      {tasks.length === 0 && (
        <div className="mc-empty-col">▢ empty</div>
      )}
      {tasks.map((t) => (
        <TaskCard key={t.id} task={t} slug={slug} onMenuAction={onMenuAction} />
      ))}
    </div>
  );
}

export { DRAG_MIME };
