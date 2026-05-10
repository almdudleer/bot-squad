import { Task } from "../api";

export function BoardColumn({ title, tasks }: { title: string; tasks: Task[] }) {
  return (
    <div className="col-md-3">
      <h6 className="text-muted text-uppercase small mb-2">
        {title} <span className="badge bg-secondary">{tasks.length}</span>
      </h6>
      {tasks.length === 0 && <p className="small text-muted">empty</p>}
      {tasks.map((t) => (
        <div key={t.id} className="card mb-2">
          <div className="card-body p-2">
            <div className="small text-muted">{t.id}</div>
            <div className="fw-medium">{t.title}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
