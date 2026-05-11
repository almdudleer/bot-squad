import { useEffect, useState, useCallback } from "react";
import { Link, useParams } from "react-router-dom";
import { api, SessionRow } from "../api";
import { Modal } from "../components/Modal";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function truncateCwd(cwd: string, max = 40): string {
  if (cwd.length <= max) return cwd;
  const half = Math.floor((max - 3) / 2);
  return cwd.slice(0, half) + "…" + cwd.slice(cwd.length - (max - half - 1));
}

function relativeTime(raw: string | number | null | undefined): string {
  if (raw == null) return "—";
  const ts = typeof raw === "number" ? raw * 1000 : Date.parse(raw);
  if (isNaN(ts)) return "—";
  const diffMs = Date.now() - ts;
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Sessions() {
  const { slug = "" } = useParams();

  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // New session modal
  const [modalOpen, setModalOpen] = useState(false);
  const [newWindow, setNewWindow] = useState("");
  const [newPrompt, setNewPrompt] = useState("");
  const [modalError, setModalError] = useState<string | null>(null);
  const [spawning, setSpawning] = useState(false);

  const load = useCallback(() => {
    api
      .sessions(slug)
      .then((rows) => {
        setSessions(rows);
        setError(null);
      })
      .catch((e: unknown) => setError(String(e)));
  }, [slug]);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  async function handlePause(sid: string) {
    setActionError(null);
    try {
      await api.pauseSession(slug, sid);
      load();
    } catch (e: unknown) {
      setActionError(String(e));
    }
  }

  async function handleResume(sid: string) {
    setActionError(null);
    try {
      await api.resumeSession(slug, sid);
      load();
    } catch (e: unknown) {
      setActionError(String(e));
    }
  }

  function openModal() {
    setNewWindow("");
    setNewPrompt("");
    setModalError(null);
    setModalOpen(true);
  }

  async function handleSpawn() {
    if (!newWindow.trim()) {
      setModalError("Window name is required");
      return;
    }
    setSpawning(true);
    setModalError(null);
    try {
      await api.spawnSession(slug, newWindow.trim(), newPrompt.trim() || undefined);
      setModalOpen(false);
      load();
    } catch (e: unknown) {
      setModalError(String(e));
    } finally {
      setSpawning(false);
    }
  }

  return (
    <div className="container py-4">
      {/* Breadcrumb nav */}
      <nav className="mb-3">
        <Link to="/">← Projects</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}`}>Backlog</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/vision`}>Vision</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/feedback`}>Feedback</Link>
        <span className="mx-2 text-muted">|</span>
        <strong>Sessions</strong>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/runs`}>Runs</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to="/scheduler">Scheduler</Link>
      </nav>

      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 className="mb-0">
          Sessions
          <span className="text-muted fw-normal fs-5 ms-2">/ {slug}</span>
        </h2>
        <button type="button" className="btn btn-primary btn-sm" onClick={openModal}>
          + New session
        </button>
      </div>

      {/* TG reply hint (spec #7) */}
      <div className="alert alert-info py-2 small mb-3">
        💬 Tip: reply to a Telegram <code>[SID] needs your input</code> notification —
        your reply lands in that session. Or use <code>/sessions</code>, <code>/say &lt;sid&gt; &lt;text&gt;</code> via the bot.
      </div>

      {/* Errors */}
      {error && <div className="alert alert-danger">{error}</div>}
      {actionError && (
        <div className="alert alert-warning alert-dismissible">
          {actionError}
          <button
            type="button"
            className="btn-close"
            onClick={() => setActionError(null)}
          />
        </div>
      )}

      {/* Loading */}
      {sessions === null && !error && (
        <p className="text-muted">Loading sessions…</p>
      )}

      {/* Empty state */}
      {sessions !== null && sessions.length === 0 && (
        <div className="text-center py-5">
          <p className="text-muted mb-3">
            No active or paused sessions for <strong>{slug}</strong>.
          </p>
          <button type="button" className="btn btn-outline-primary" onClick={openModal}>
            + New session
          </button>
        </div>
      )}

      {/* Session table */}
      {sessions !== null && sessions.length > 0 && (
        <div className="table-responsive">
          <table className="table table-hover align-middle">
            <thead className="table-light">
              <tr>
                <th>SID</th>
                <th>Window</th>
                <th>CWD</th>
                <th>Status</th>
                <th>Started</th>
                <th>Last activity</th>
                <th>Tasks</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.sid}>
                  {/* SID — monospace; links to messages if claude_uuid available */}
                  <td>
                    {s.claude_uuid ? (
                      <Link
                        to={`/p/${slug}/sessions/${encodeURIComponent(s.claude_uuid)}/messages`}
                        className="text-body"
                        style={{ fontSize: "0.8rem", fontFamily: "monospace" }}
                      >
                        {s.sid}
                      </Link>
                    ) : (
                      <code className="text-body" style={{ fontSize: "0.8rem" }}>
                        {s.sid}
                      </code>
                    )}
                  </td>

                  {/* Window */}
                  <td>{s.window}</td>

                  {/* CWD — truncated */}
                  <td>
                    <span
                      title={s.cwd}
                      className="text-muted"
                      style={{ fontSize: "0.85rem", fontFamily: "monospace" }}
                    >
                      {truncateCwd(s.cwd)}
                    </span>
                  </td>

                  {/* Status badge */}
                  <td>
                    {s.status === "active" ? (
                      <span className="badge bg-success">active</span>
                    ) : (
                      <span className="badge bg-secondary">paused</span>
                    )}
                  </td>

                  {/* Started */}
                  <td className="text-muted" style={{ fontSize: "0.85rem" }}>
                    {relativeTime(s.started_at)}
                  </td>

                  {/* Last activity */}
                  <td className="text-muted" style={{ fontSize: "0.85rem" }}>
                    {relativeTime(s.last_prompt_at)}
                  </td>

                  {/* Linked tasks */}
                  <td>
                    <div className="d-flex flex-wrap gap-1">
                      {(s.linked_tasks ?? []).map((tid) => (
                        <Link
                          key={tid}
                          to={`/p/${slug}/t/${tid}`}
                          className="badge bg-light text-primary border border-primary text-decoration-none"
                          style={{ fontSize: "0.75rem" }}
                        >
                          {tid}
                        </Link>
                      ))}
                    </div>
                  </td>

                  {/* Actions */}
                  <td>
                    {s.status === "active" ? (
                      <button
                        type="button"
                        className="btn btn-outline-warning btn-sm"
                        onClick={() => handlePause(s.sid)}
                      >
                        Pause
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="btn btn-outline-success btn-sm"
                        onClick={() => handleResume(s.sid)}
                      >
                        Resume
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* New session modal */}
      <Modal
        open={modalOpen}
        title="New session"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setModalOpen(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleSpawn}
              disabled={spawning}
            >
              {spawning ? "Spawning…" : "Spawn"}
            </button>
          </>
        }
      >
        {modalError && (
          <div className="alert alert-danger">{modalError}</div>
        )}
        <div className="mb-3">
          <label className="form-label">
            Window name <span className="text-danger">*</span>
          </label>
          <input
            className="form-control"
            value={newWindow}
            onChange={(e) => setNewWindow(e.target.value)}
            placeholder="e.g. spec6-work"
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">
            Initial prompt{" "}
            <span className="text-muted fw-normal">(optional)</span>
          </label>
          <textarea
            className="form-control"
            rows={4}
            value={newPrompt}
            onChange={(e) => setNewPrompt(e.target.value)}
            placeholder="Type a message to send immediately after claude starts…"
          />
        </div>
      </Modal>
    </div>
  );
}
