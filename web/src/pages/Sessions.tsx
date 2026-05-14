import { useEffect, useState, useCallback, Fragment } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
// Link kept for session SID links and task links inside the table
import { api, SessionRow, Task, VisionFile } from "../api";
import { Modal } from "../components/Modal";

import { PageHelp } from "../components/PageHelp";
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Render a status badge consistent with the new vocabulary:
//   active     — live pane, not paused        → green LED + ok badge
//   paused/idle — live pane, user pressed pause → grey LED + dim badge ("idle")
//   suspended  — pane gone, resurrectable     → faint dot + dim badge
function StatusBadge({ status }: { status: string }) {
  if (status === "active") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
        <span className="mc-dot mc-dot-active" />
        <span className="mc-badge mc-badge-ok">active</span>
      </span>
    );
  }
  if (status === "paused" || status === "idle") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
        <span className="mc-dot mc-dot-idle" />
        <span className="mc-badge mc-badge-dim">idle</span>
      </span>
    );
  }
  // suspended (or anything unknown)
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", opacity: 0.7 }}>
      <span className="mc-dot mc-dot-idle" />
      <span className="mc-badge mc-badge-dim">suspended</span>
    </span>
  );
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
  const [searchParams, setSearchParams] = useSearchParams();

  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // New session modal
  const [modalOpen, setModalOpen] = useState(false);
  const [newRole, setNewRole] = useState<"teamlead" | "dev" | null>(null);
  const [newWindow, setNewWindow] = useState("");
  const [newPrompt, setNewPrompt] = useState("");
  const [newTaskId, setNewTaskId] = useState("");
  const [newInitiative, setNewInitiative] = useState("");
  const [newTlSid, setNewTlSid] = useState("");
  const [newInstructions, setNewInstructions] = useState("");
  const [backlog, setBacklog] = useState<Task[]>([]);
  const [initiatives, setInitiatives] = useState<VisionFile[]>([]);
  const [modalError, setModalError] = useState<string | null>(null);
  const [modalInfo, setModalInfo] = useState<string | null>(null);
  const [spawning, setSpawning] = useState(false);

  // Send-message modal (cross-session bus)
  const [sendOpen, setSendOpen] = useState(false);
  const [sendTarget, setSendTarget] = useState<string>("");
  const [sendText, setSendText] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const [sendInfo, setSendInfo] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [meUsername, setMeUsername] = useState<string>("stakeholder");

  // Row expansion (click-through cwd / metadata detail) and archived
  // section toggle.
  const [expandedSids, setExpandedSids] = useState<Set<string>>(new Set());
  const [showArchived, setShowArchived] = useState(false);

  function toggleRow(sid: string) {
    setExpandedSids((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) next.delete(sid);
      else next.add(sid);
      return next;
    });
  }

  useEffect(() => {
    // Best-effort username lookup so the from_sid prefix matches the cookie.
    // Falls back to "stakeholder" if /auth/me isn't reachable.
    fetch("/api/auth/me", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data: { username?: string } | null) => {
        if (data && data.username) setMeUsername(data.username);
      })
      .catch(() => {
        /* keep default */
      });
  }, []);

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

  async function handleSuspend(sid: string) {
    setActionError(null);
    try {
      await api.suspendSession(slug, sid);
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

  async function handleArchive(sid: string) {
    setActionError(null);
    try {
      await api.archiveSession(slug, sid);
      load();
    } catch (e: unknown) {
      setActionError(String(e));
    }
  }

  async function handleResurrectFromArchive(sid: string) {
    setActionError(null);
    try {
      // Unarchive first so the resumed session appears in the active list.
      await api.unarchiveSession(slug, sid);
      await api.resumeSession(slug, sid);
      load();
    } catch (e: unknown) {
      setActionError(String(e));
    }
  }

  function openSendModal(sid: string) {
    setSendTarget(sid);
    setSendText("");
    setSendError(null);
    setSendInfo(null);
    setSendOpen(true);
  }

  async function handleSend() {
    if (!sendText.trim()) {
      setSendError("Message text is required");
      return;
    }
    setSending(true);
    setSendError(null);
    setSendInfo(null);
    try {
      // from_sid is informational; format S-<user>-ui-p0 so the API's
      // user-prefix check passes for the logged-in user.
      const fromSid = `S-${meUsername}-ui-p0`;
      const result = await api.peerSend(slug, fromSid, sendTarget, sendText.trim());
      setSendInfo(`Delivered to: ${result.delivered_to.join(", ") || "(none)"}`);
      setSendText("");
    } catch (e: unknown) {
      setSendError(String(e));
    } finally {
      setSending(false);
    }
  }

  function openModal(prefill?: {
    role?: "teamlead" | "dev";
    taskId?: string;
    initiative?: string;
  }) {
    setNewRole(prefill?.role ?? null);
    setNewWindow("");
    setNewPrompt("");
    setNewTaskId(prefill?.taskId ?? "");
    setNewInitiative(prefill?.initiative ?? "");
    setNewTlSid("");
    setNewInstructions("");
    setModalError(null);
    setModalInfo(null);
    setModalOpen(true);
    api.backlog(slug)
      .then((rows) => setBacklog(rows.filter((t) => t.status === "open" || t.status === "in_progress" || t.status === "reopened")))
      .catch(() => setBacklog([]));
    api.vision(slug)
      .then((files) => setInitiatives(
        files.filter((f) => f.name.startsWith("initiatives/") && !f.name.endsWith("/_TEMPLATE.md")),
      ))
      .catch(() => setInitiatives([]));
  }

  // Phase 6: deep-link entry. Project / Roadmap pages navigate here with
  // ?role=dev&task=T-NNNN  or  ?role=teamlead&initiative=foo.md to open the
  // New Session modal pre-filled. Clear the params afterwards so refresh
  // doesn't keep re-opening the modal.
  useEffect(() => {
    const roleParam = searchParams.get("role");
    if (roleParam !== "dev" && roleParam !== "teamlead") return;
    const taskId = searchParams.get("task") ?? undefined;
    const initiative = searchParams.get("initiative") ?? undefined;
    openModal({ role: roleParam, taskId, initiative });
    setSearchParams({}, { replace: true });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Active TLs are sessions with no task_id and status === "active".
  const activeTeamleads: SessionRow[] = (sessions ?? []).filter(
    (s) =>
      s.status === "active" &&
      !(s.task_id && s.task_id !== "") &&
      !s.archived,
  );

  // Split visible vs archived for the two-section layout.
  const visibleSessions: SessionRow[] = (sessions ?? []).filter((s) => !s.archived);
  const archivedSessions: SessionRow[] = (sessions ?? []).filter((s) => !!s.archived);

  // ---------------- Bound-link column ----------------
  // For TL sessions (no task_id): link to the primary initiative on the
  // roadmap page, plus a "+N" badge if extras are bound. For dev sessions
  // (with task_id): link to TaskDetail of the primary task plus "+N"
  // badge for extras. Idle/suspended rows render dimmer.
  function renderBoundCell(s: SessionRow) {
    const isDev = !!(s.task_id && s.task_id !== "" && s.task_id !== "~");
    if (isDev) {
      const primary = (s.task_id ?? "").trim();
      const extras = (s.extra_task_ids ?? []).filter((t) => t && t !== "~");
      if (!primary) {
        return <span style={{ color: "var(--mc-text-dim)", fontSize: "0.72rem" }}>—</span>;
      }
      return (
        <span style={{ fontSize: "0.78rem" }}>
          <Link
            to={`/p/${slug}/t/${primary}`}
            className="mc-badge mc-badge-info"
            style={{ textDecoration: "none" }}
            onClick={(e) => e.stopPropagation()}
          >
            {primary}
          </Link>
          {extras.length > 0 && (
            <span
              className="mc-badge mc-badge-dim"
              style={{ marginLeft: "0.3rem" }}
              title={`Extra bound tasks: ${extras.join(", ")}`}
            >
              +{extras.length}
            </span>
          )}
        </span>
      );
    }
    // TL session — link to initiative on the roadmap.
    const primaryInit = (s.initiative ?? "").trim();
    const extras = (s.extra_initiatives ?? []).filter((i) => i && i !== "~");
    if (!primaryInit) {
      if (extras.length === 0) {
        return <span style={{ color: "var(--mc-text-dim)", fontSize: "0.72rem" }}>—</span>;
      }
      // No primary but extras exist — show first extra as the anchor.
      const [first, ...rest] = extras;
      return (
        <span style={{ fontSize: "0.78rem" }}>
          <Link
            to={`/p/${slug}/vision#${encodeURIComponent(first)}`}
            className="mc-badge mc-badge-info"
            style={{ textDecoration: "none" }}
            onClick={(e) => e.stopPropagation()}
          >
            {first.replace(/\.md$/, "")}
          </Link>
          {rest.length > 0 && (
            <span
              className="mc-badge mc-badge-dim"
              style={{ marginLeft: "0.3rem" }}
              title={`Extra: ${rest.join(", ")}`}
            >
              +{rest.length}
            </span>
          )}
        </span>
      );
    }
    return (
      <span style={{ fontSize: "0.78rem" }}>
        <Link
          to={`/p/${slug}/vision#${encodeURIComponent(primaryInit)}`}
          className="mc-badge mc-badge-info"
          style={{ textDecoration: "none" }}
          onClick={(e) => e.stopPropagation()}
        >
          {primaryInit.replace(/\.md$/, "")}
        </Link>
        {extras.length > 0 && (
          <span
            className="mc-badge mc-badge-dim"
            style={{ marginLeft: "0.3rem" }}
            title={`Extra initiatives: ${extras.join(", ")}`}
          >
            +{extras.length}
          </span>
        )}
      </span>
    );
  }

  // Click-through detail row — exposes cwd, claude_uuid, full SID, and
  // timestamps that were too noisy for the main columns.
  function renderDetailRow(s: SessionRow, colSpan: number) {
    return (
      <tr style={{ background: "var(--mc-surface-deep)" }}>
        <td colSpan={colSpan} style={{ padding: "0.5rem 1rem" }}>
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "max-content 1fr",
              gap: "0.25rem 1rem",
              fontFamily: "var(--mc-mono)",
              fontSize: "0.74rem",
              color: "var(--mc-text-dim)",
              margin: 0,
            }}
          >
            <dt>SID</dt>
            <dd style={{ margin: 0, wordBreak: "break-all", color: "var(--mc-text)" }}>{s.sid}</dd>
            <dt>cwd</dt>
            <dd
              style={{ margin: 0, wordBreak: "break-all", color: "var(--mc-text)" }}
              title={s.cwd}
            >
              {s.cwd || "—"}
            </dd>
            <dt>claude_uuid</dt>
            <dd style={{ margin: 0, color: "var(--mc-text)" }}>{s.claude_uuid || "—"}</dd>
            <dt>started_at</dt>
            <dd style={{ margin: 0, color: "var(--mc-text)" }}>{s.started_at || "—"}</dd>
            {s.paused_at && (
              <>
                <dt>paused_at</dt>
                <dd style={{ margin: 0, color: "var(--mc-text)" }}>{s.paused_at}</dd>
              </>
            )}
            {s.suspended_at && (
              <>
                <dt>suspended_at</dt>
                <dd style={{ margin: 0, color: "var(--mc-text)" }}>{s.suspended_at}</dd>
              </>
            )}
            {(s.linked_tasks ?? []).length > 0 && (
              <>
                <dt>linked tasks</dt>
                <dd style={{ margin: 0, color: "var(--mc-text)" }}>
                  {(s.linked_tasks ?? []).join(", ")}
                </dd>
              </>
            )}
          </dl>
        </td>
      </tr>
    );
  }

  function renderSessionRow(s: SessionRow) {
    const isOpen = expandedSids.has(s.sid);
    const isSuspended = s.status === "suspended";
    return (
      <Fragment key={s.sid}>
        <tr
          style={{ cursor: "pointer" }}
          onClick={() => toggleRow(s.sid)}
        >
          {/* Expand chevron */}
          <td
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.75rem",
              color: "var(--mc-text-dim)",
              width: "1.5rem",
            }}
          >
            {isOpen ? "▾" : "▸"}
          </td>

          {/* SID */}
          <td onClick={(e) => e.stopPropagation()}>
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

          {/* Role */}
          <td>
            {((s.task_id && s.task_id !== "" && s.task_id !== "~") ||
              (s.linked_tasks ?? []).length > 0) ? (
              <span className="mc-badge mc-badge-info">Dev</span>
            ) : (
              <span className="mc-badge mc-badge-ok">Teamlead</span>
            )}
          </td>

          {/* Bound (initiative for TL / task for dev) */}
          <td>{renderBoundCell(s)}</td>

          {/* Status */}
          <td>
            <StatusBadge status={s.status} />
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
          <td onClick={(e) => e.stopPropagation()}>
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
          <td onClick={(e) => e.stopPropagation()}>
            <div className="d-flex gap-1 flex-wrap">
              {s.status === "active" && (
                <>
                  <button
                    type="button"
                    className="btn btn-outline-warning btn-sm"
                    style={{ fontSize: "0.72rem" }}
                    onClick={() => handlePause(s.sid)}
                  >
                    Pause
                  </button>
                  <button
                    type="button"
                    className="btn btn-outline-secondary btn-sm"
                    style={{ fontSize: "0.72rem" }}
                    onClick={() => handleSuspend(s.sid)}
                  >
                    Suspend
                  </button>
                </>
              )}
              {s.status === "paused" && (
                <>
                  <button
                    type="button"
                    className="btn btn-outline-success btn-sm"
                    style={{ fontSize: "0.72rem" }}
                    onClick={() => handleResume(s.sid)}
                  >
                    Resume
                  </button>
                  <button
                    type="button"
                    className="btn btn-outline-secondary btn-sm"
                    style={{ fontSize: "0.72rem" }}
                    onClick={() => handleSuspend(s.sid)}
                  >
                    Suspend
                  </button>
                </>
              )}
              {s.status === "suspended" && (
                <button
                  type="button"
                  className="btn btn-outline-success btn-sm"
                  style={{ fontSize: "0.72rem" }}
                  onClick={() => handleResume(s.sid)}
                >
                  Resurrect
                </button>
              )}
              <button
                type="button"
                className="btn btn-outline-info btn-sm"
                style={{ fontSize: "0.72rem" }}
                onClick={() => openSendModal(s.sid)}
              >
                Send msg
              </button>
              {/* Archive: only enabled for suspended sessions. */}
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                style={{ fontSize: "0.72rem" }}
                disabled={!isSuspended}
                title={
                  isSuspended
                    ? "Archive this suspended session"
                    : "Suspend the session first"
                }
                onClick={() => handleArchive(s.sid)}
              >
                Archive
              </button>
            </div>
          </td>
        </tr>
        {isOpen && renderDetailRow(s, 10)}
      </Fragment>
    );
  }

  function renderArchivedRow(s: SessionRow) {
    const isOpen = expandedSids.has(s.sid);
    return (
      <Fragment key={s.sid}>
        <tr
          style={{ cursor: "pointer" }}
          onClick={() => toggleRow(s.sid)}
        >
          <td
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.75rem",
              color: "var(--mc-text-dim)",
              width: "1.5rem",
            }}
          >
            {isOpen ? "▾" : "▸"}
          </td>
          <td onClick={(e) => e.stopPropagation()}>
            <code
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.78rem",
                color: "var(--mc-text-dim)",
              }}
            >
              {s.sid}
            </code>
          </td>
          <td style={{ fontSize: "0.83rem", color: "var(--mc-text-dim)" }}>{s.window}</td>
          <td>
            {((s.task_id && s.task_id !== "" && s.task_id !== "~") ||
              (s.linked_tasks ?? []).length > 0) ? (
              <span className="mc-badge mc-badge-dim">Dev</span>
            ) : (
              <span className="mc-badge mc-badge-dim">Teamlead</span>
            )}
          </td>
          <td>{renderBoundCell(s)}</td>
          <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
            {relativeTime(s.started_at)}
          </td>
          <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
            {relativeTime(s.last_prompt_at)}
          </td>
          <td onClick={(e) => e.stopPropagation()}>
            <div className="d-flex gap-1 flex-wrap">
              <button
                type="button"
                className="btn btn-outline-success btn-sm"
                style={{ fontSize: "0.72rem" }}
                onClick={() => handleResurrectFromArchive(s.sid)}
              >
                Resurrect
              </button>
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                style={{ fontSize: "0.72rem" }}
                onClick={() => api.unarchiveSession(slug, s.sid).then(load).catch((e) => setActionError(String(e)))}
              >
                Unarchive
              </button>
            </div>
          </td>
        </tr>
        {isOpen && renderDetailRow(s, 8)}
      </Fragment>
    );
  }

  async function handleSpawnTeamlead() {
    if (!newWindow.trim()) {
      setModalError("Window name is required");
      return;
    }
    setSpawning(true);
    setModalError(null);
    try {
      await api.spawnSession(
        slug,
        newWindow.trim(),
        newPrompt.trim() || undefined,
        undefined,
        newInitiative || undefined,
      );
      setModalOpen(false);
      load();
    } catch (e: unknown) {
      setModalError(String(e));
    } finally {
      setSpawning(false);
    }
  }

  async function handleDevSpawnRequest() {
    if (!newTlSid) {
      setModalError("Pick a teamlead to delegate to");
      return;
    }
    if (!newInstructions.trim()) {
      setModalError("Instructions for the teamlead are required");
      return;
    }
    setSpawning(true);
    setModalError(null);
    setModalInfo(null);
    try {
      const result = await api.devSpawnRequest(
        slug,
        newTlSid,
        newTaskId || undefined,
        newInstructions.trim(),
      );
      setModalInfo(
        `Request sent to teamlead ${result.delivered_to.join(", ")}. Watch their session for the spawn.`,
      );
      // Reset just the dev-specific inputs; user can dismiss when ready.
      setNewInstructions("");
      setNewTaskId("");
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
          Agent sessions
          <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
        </h2>
        <button type="button" className="btn btn-primary btn-sm" onClick={() => openModal()}>
          + New session
        </button>
      </div>
      <PageHelp>
        Claude tmux sessions whose CWD is this project&apos;s repo.
        <strong> Pause</strong> interrupts (Ctrl-C) — pane stays open, you
        can keep typing.
        <strong> Suspend</strong> closes the window to free resources; the
        registry keeps the UUID.
        <strong> Resurrect</strong> spawns a new pane with <code>claude
        --resume &lt;uuid&gt;</code>.
        <strong> + New session</strong> opens a fresh pane in the repo.
        <div className="mt-2">
          Reply to a Telegram <code>[SID] needs your input</code> notification —
          your reply lands in that session. Or use <code>/sessions</code>,
          {" "}<code>/say &lt;sid&gt; &lt;text&gt;</code> via the bot.
        </div>
      </PageHelp>

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
            onClick={() => openModal()}
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
                <th style={{ width: "1.5rem" }}></th>
                <th>SID</th>
                <th>Window</th>
                <th>Role</th>
                <th>Target</th>
                <th>Status</th>
                <th>Started</th>
                <th>Last activity</th>
                <th>Tasks</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visibleSessions.map((s) => renderSessionRow(s))}
            </tbody>
          </table>
        </div>
      )}

      {/* Archived section — collapsible, off by default. */}
      {sessions !== null && archivedSessions.length > 0 && (
        <details
          className="mt-3"
          open={showArchived}
          onToggle={(e) => setShowArchived((e.target as HTMLDetailsElement).open)}
          style={{ borderTop: "1px solid var(--mc-border)", paddingTop: "0.5rem" }}
        >
          <summary
            style={{
              cursor: "pointer",
              fontFamily: "var(--mc-mono)",
              fontSize: "0.74rem",
              color: "var(--mc-text-dim)",
              textTransform: "uppercase",
              letterSpacing: "0.08em",
              padding: "0.35rem 0",
            }}
          >
            Archived ({archivedSessions.length})
          </summary>
          <div className="table-responsive mt-2">
            <table className="table table-hover align-middle" style={{ opacity: 0.85 }}>
              <thead>
                <tr>
                  <th style={{ width: "1.5rem" }}></th>
                  <th>SID</th>
                  <th>Window</th>
                  <th>Role</th>
                  <th>Target</th>
                  <th>Started</th>
                  <th>Last activity</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {archivedSessions.map((s) => renderArchivedRow(s))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {/* New session modal */}
      <Modal
        open={modalOpen}
        title="New session"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setModalOpen(false)}>
              {newRole && modalInfo ? "Close" : "Cancel"}
            </button>
            {newRole === "teamlead" && (
              <button type="button" className="btn btn-primary" onClick={handleSpawnTeamlead} disabled={spawning}>
                {spawning ? "Spawning…" : "Spawn teamlead"}
              </button>
            )}
            {newRole === "dev" && (
              <button type="button" className="btn btn-primary" onClick={handleDevSpawnRequest} disabled={spawning}>
                {spawning ? "Sending…" : "Send to teamlead"}
              </button>
            )}
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        {modalInfo && <div className="alert alert-success">{modalInfo}</div>}

        {/* Step 1: Role picker — always visible */}
        <div className="mb-3">
          <div style={{ fontSize: "0.85rem", color: "var(--mc-text-mid)", marginBottom: "0.5rem" }}>
            What kind of session do you want to start?
          </div>
          <div className="d-flex gap-2">
            <button
              type="button"
              className={`btn ${newRole === "teamlead" ? "btn-primary" : "btn-outline-primary"} flex-fill`}
              onClick={() => { setNewRole("teamlead"); setModalError(null); setModalInfo(null); }}
            >
              Teamlead
              <div style={{ fontSize: "0.72rem", fontWeight: 400, opacity: 0.8, marginTop: "0.15rem" }}>
                drives an initiative, spawns devs
              </div>
            </button>
            <button
              type="button"
              className={`btn ${newRole === "dev" ? "btn-primary" : "btn-outline-primary"} flex-fill`}
              onClick={() => { setNewRole("dev"); setModalError(null); setModalInfo(null); }}
            >
              Dev worker
              <div style={{ fontSize: "0.72rem", fontWeight: 400, opacity: 0.8, marginTop: "0.15rem" }}>
                delegated through a teamlead
              </div>
            </button>
          </div>
        </div>

        {/* Step 2a: Teamlead form */}
        {newRole === "teamlead" && (
          <>
            <hr style={{ borderColor: "var(--mc-border)" }} />
            <div className="mb-3">
              <label className="form-label">
                Initiative{" "}
                <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>
                  (optional — overrides the project default for this session)
                </span>
              </label>
              <select
                className="form-select"
                value={newInitiative}
                onChange={(e) => setNewInitiative(e.target.value)}
              >
                <option value="">— Use project default —</option>
                {initiatives.map((f) => {
                  const base = f.name.replace(/^initiatives\//, "");
                  return (
                    <option key={f.name} value={base}>
                      {base}{f.active ? " (currently active)" : ""}
                    </option>
                  );
                })}
              </select>
            </div>
            <div className="mb-3">
              <label className="form-label">
                Window name <span style={{ color: "var(--mc-accent-danger)" }}>*</span>
              </label>
              <input
                className="form-control"
                value={newWindow}
                onChange={(e) => setNewWindow(e.target.value)}
                placeholder="e.g. v0_7_tl, heatmaps_tl"
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
          </>
        )}

        {/* Step 2b: Dev worker form */}
        {newRole === "dev" && (
          <>
            <hr style={{ borderColor: "var(--mc-border)" }} />
            {activeTeamleads.length === 0 ? (
              <div className="alert alert-warning" style={{ fontSize: "0.85rem" }}>
                No active teamleads — start one first.{" "}
                <a
                  href="#"
                  onClick={(e) => {
                    e.preventDefault();
                    setNewRole("teamlead");
                    setModalError(null);
                  }}
                  style={{ color: "var(--mc-accent)" }}
                >
                  Switch to teamlead role
                </a>
              </div>
            ) : (
              <>
                <div className="mb-3">
                  <label className="form-label">
                    Target teamlead <span style={{ color: "var(--mc-accent-danger)" }}>*</span>
                  </label>
                  <select
                    className="form-select"
                    value={newTlSid}
                    onChange={(e) => setNewTlSid(e.target.value)}
                  >
                    <option value="">— Pick a teamlead —</option>
                    {activeTeamleads.map((tl) => (
                      <option key={tl.sid} value={tl.sid}>
                        {tl.window} ({tl.sid})
                      </option>
                    ))}
                  </select>
                </div>
                <div className="mb-3">
                  <label className="form-label">
                    Backlog task{" "}
                    <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>
                      (optional)
                    </span>
                  </label>
                  <select
                    className="form-select"
                    value={newTaskId}
                    onChange={(e) => setNewTaskId(e.target.value)}
                  >
                    <option value="">— None (let TL find or create one) —</option>
                    {backlog.map((t) => (
                      <option key={t.id} value={t.id}>{t.id} · {t.title}</option>
                    ))}
                  </select>
                </div>
                <div className="mb-3">
                  <label className="form-label">
                    Instructions for teamlead <span style={{ color: "var(--mc-accent-danger)" }}>*</span>
                  </label>
                  <textarea
                    className="form-control"
                    rows={5}
                    value={newInstructions}
                    onChange={(e) => setNewInstructions(e.target.value)}
                    placeholder="Describe what we're spawning a dev worker for. The teamlead will find a matching task in the backlog or create a new one if needed."
                  />
                </div>
              </>
            )}
          </>
        )}
      </Modal>

      {/* Send-message modal (cross-session bus) */}
      <Modal
        open={sendOpen}
        title={`Send message to ${sendTarget}`}
        onClose={() => setSendOpen(false)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setSendOpen(false)}>
              Close
            </button>
            <button type="button" className="btn btn-primary" onClick={handleSend} disabled={sending}>
              {sending ? "Sending…" : "Send"}
            </button>
          </>
        }
      >
        {sendError && <div className="alert alert-danger">{sendError}</div>}
        {sendInfo && <div className="alert alert-success">{sendInfo}</div>}
        <div className="mb-2" style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
          Lands in <code>{sendTarget}</code>&apos;s inbox via the peer message bus. The
          target session sees it on its next <code>peer_inbox_read</code> or when its
          armed <code>peer_inbox_wait</code> wakes.
        </div>
        <div className="mb-3">
          <label className="form-label">Message</label>
          <textarea
            className="form-control"
            rows={5}
            value={sendText}
            onChange={(e) => setSendText(e.target.value)}
            placeholder="Type a message for the agent…"
            autoFocus
          />
        </div>
      </Modal>
    </div>
  );
}
