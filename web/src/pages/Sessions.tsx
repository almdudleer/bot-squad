import { useEffect, useState, useCallback } from "react";
import { Link, useParams } from "react-router-dom";
// Link kept for session SID links and task links inside the table
import { api, SessionRow } from "../api";
import { Modal } from "../components/Modal";

import { PageHelp } from "../components/PageHelp";
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
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
          Sessions
          <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
        </h2>
        <button type="button" className="btn btn-primary btn-sm" onClick={openModal}>
          + New session
        </button>
      </div>
      <PageHelp>
        Active and paused Claude tmux sessions whose CWD is this project&apos;s repo.
        <strong> Pause</strong> kills the pane after saving the session UUID;
        <strong> Resume</strong> spawns a new pane with <code>claude --resume &lt;uuid&gt;</code>;
        <strong> + New session</strong> opens a fresh pane in the repo.
      </PageHelp>

      {/* TG reply hint */}
      <div className="alert alert-info py-2 mb-3" style={{ fontSize: "0.78rem" }}>
        Reply to a Telegram <code>[SID] needs your input</code> notification — your reply lands
        in that session. Or use <code>/sessions</code>, <code>/say &lt;sid&gt; &lt;text&gt;</code> via the bot.
      </div>

      {/* Errors */}
      {error && <div className="alert alert-danger">{error}</div>}
      {actionError && (
        <div className="alert alert-warning d-flex justify-content-between align-items-center">
          <span>{actionError}</span>
          <button
            type="button"
            className="btn-close"
            style={{ filter: "invert(1) opacity(0.5)" }}
            onClick={() => setActionError(null)}
          />
        </div>
      )}

      {/* Loading */}
      {sessions === null && !error && (
        <div className="mc-loading">Loading sessions</div>
      )}

      {/* Empty state */}
      {sessions !== null && sessions.length === 0 && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          <div>No sessions for <strong>{slug}</strong></div>
          <button
            type="button"
            className="btn btn-outline-primary btn-sm mt-3"
            onClick={openModal}
          >
            + New session
          </button>
        </div>
      )}

      {/* Session table */}
      {sessions !== null && sessions.length > 0 && (
        <div className="table-responsive">
          <table className="table table-hover align-middle">
            <thead>
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
                  {/* SID */}
                  <td>
                    {s.claude_uuid ? (
                      <Link
                        to={`/p/${slug}/sessions/${encodeURIComponent(s.claude_uuid)}/messages`}
                        style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-accent)" }}
                      >
                        {s.sid}
                      </Link>
                    ) : (
                      <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-mid)" }}>
                        {s.sid}
                      </code>
                    )}
                  </td>

                  {/* Window */}
                  <td style={{ fontSize: "0.83rem" }}>{s.window}</td>

                  {/* CWD */}
                  <td>
                    <span
                      title={s.cwd}
                      style={{ fontSize: "0.78rem", fontFamily: "var(--mc-mono)", color: "var(--mc-text-dim)" }}
                    >
                      {truncateCwd(s.cwd)}
                    </span>
                  </td>

                  {/* Status */}
                  <td>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
                      <span className={s.status === "active" ? "mc-dot mc-dot-active" : "mc-dot mc-dot-idle"} />
                      <span className={s.status === "active" ? "mc-badge mc-badge-ok" : "mc-badge mc-badge-dim"}>
                        {s.status}
                      </span>
                    </span>
                  </td>

                  {/* Started */}
                  <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
                    {relativeTime(s.started_at)}
                  </td>

                  {/* Last activity */}
                  <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
                    {relativeTime(s.last_prompt_at)}
                  </td>

                  {/* Linked tasks */}
                  <td>
                    <div className="d-flex flex-wrap gap-1">
                      {(s.linked_tasks ?? []).map((tid) => (
                        <Link
                          key={tid}
                          to={`/p/${slug}/t/${tid}`}
                          className="mc-badge mc-badge-info"
                          style={{ textDecoration: "none" }}
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
                        style={{ fontSize: "0.72rem" }}
                        onClick={() => handlePause(s.sid)}
                      >
                        Pause
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="btn btn-outline-success btn-sm"
                        style={{ fontSize: "0.72rem" }}
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
            <button type="button" className="btn btn-secondary" onClick={() => setModalOpen(false)}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={handleSpawn} disabled={spawning}>
              {spawning ? "Spawning…" : "Spawn"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <div className="mb-3">
          <label className="form-label">
            Window name <span style={{ color: "var(--mc-accent-danger)" }}>*</span>
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
            <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>(optional)</span>
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
