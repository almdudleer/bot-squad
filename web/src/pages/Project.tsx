import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Task } from "../api";
import { BoardColumn } from "../components/BoardColumn";

const COLUMNS = ["open", "totest", "reopened", "closed"] as const;
const COLUMN_LABELS: Record<typeof COLUMNS[number], string> = {
  open: "Open",
  totest: "To Test",
  reopened: "Reopened",
  closed: "Closed",
};

export function Project() {
  const { slug = "" } = useParams();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.backlog(slug).then(setTasks).catch((e) => setError(String(e)));
  }, [slug]);

  const grouped = COLUMNS.reduce<Record<string, Task[]>>((acc, c) => ({ ...acc, [c]: [] }), {});
  for (const t of tasks ?? []) {
    if (COLUMNS.includes(t.status as typeof COLUMNS[number])) {
      grouped[t.status].push(t);
    }
  }

  return (
    <div className="container py-4">
      <nav className="mb-3">
        <Link to="/">← Projects</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/vision`}>Vision</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/feedback`}>Feedback</Link>
      </nav>
      <h2>{slug}</h2>
      {error && <div className="alert alert-danger">{error}</div>}
      {tasks === null && !error && <p>Loading…</p>}
      <div className="row g-3 mt-3">
        {COLUMNS.map((c) => (
          <BoardColumn key={c} title={COLUMN_LABELS[c]} tasks={grouped[c]} />
        ))}
      </div>
    </div>
  );
}
