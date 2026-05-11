import { Task } from "../api";
import { TaskCard, MenuAction } from "./TaskCard";

interface BoardColumnProps {
  title: string;
  tasks: Task[];
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
}

export function BoardColumn({ title, tasks, slug, onMenuAction }: BoardColumnProps) {
  return (
    <div className="col-md-3">
      <div className="mc-board-col-header">
        <span>{title}</span>
        <span className="mc-board-count">{tasks.length}</span>
      </div>
      {tasks.length === 0 && (
        <div style={{ fontSize: "0.75rem", color: "var(--mc-text-dim)", padding: "0.5rem 0" }}>
          ▢ empty
        </div>
      )}
      {tasks.map((t) => (
        <TaskCard key={t.id} task={t} slug={slug} onMenuAction={onMenuAction} />
      ))}
    </div>
  );
}
