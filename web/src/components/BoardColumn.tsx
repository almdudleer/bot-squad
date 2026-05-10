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
      <h6 className="text-muted text-uppercase small mb-2">
        {title} <span className="badge bg-secondary">{tasks.length}</span>
      </h6>
      {tasks.length === 0 && <p className="small text-muted">empty</p>}
      {tasks.map((t) => (
        <TaskCard key={t.id} task={t} slug={slug} onMenuAction={onMenuAction} />
      ))}
    </div>
  );
}
