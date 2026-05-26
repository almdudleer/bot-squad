import { useEffect, useMemo, useRef, useState, useCallback, Fragment } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
// Link kept for session SID links and task links inside the table
import type { SessionRow, Task, VisionFile } from "../api";
import { useApiClient } from "../apiContext";
import { CopyableTmuxAttach } from "../components/CopyableTmuxAttach";
import { Modal } from "../components/Modal";
import { Select } from "../components/Select";
import { sessionActivity, sessionLabel } from "../utils/sessionStatus";

import { PageHelp } from "../components/PageHelp";
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// T-0104: render a badge keyed off the canonical `activity` enum
// (worker-derived from jsonl mtime). `running` is the only "green LED"
// state — a live-but-quiet pane is `idle`, never `running`. Same
// vocabulary is used by TaskCard / TaskDetail so card and detail no
// longer disagree on the label.
function StatusBadge({ row }: { row: SessionRow }) {
  const a = sessionActivity(row);
  if (a === "running") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
        <span className="mc-dot mc-dot-active" />
        <span className="mc-badge mc-badge-ok">{sessionLabel(a)}</span>
      </span>
    );
  }
  if (a === "idle" || a === "paused") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
        <span className="mc-dot mc-dot-idle" />
        <span className="mc-badge mc-badge-dim">{sessionLabel(a)}</span>
      </span>
    );
  }
  // suspended (or anything unknown)
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", opacity: 0.7 }}>
      <span className="mc-dot mc-dot-idle" />
      <span className="mc-badge mc-badge-dim">{sessionLabel(a)}</span>
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

// T-0037: "Last activity" must be per-pane. Prefer the worker-derived
// `activity_at` (jsonl mtime + peer-bus heartbeat — distinct per claude_uuid)
// over `last_prompt_at`, which sources from `.claude/last_user_prompt_ts`
// shared across every pane in the repo and so reads the same for every row.
// For suspended rows the worker leaves activity_at null and stuffs
// suspended_at/paused_at into last_prompt_at; fall through to that.
function sessionLastActivity(s: SessionRow): string {
  if (s.activity_at != null) return relativeTime(s.activity_at);
  if (s.last_prompt_at != null) return relativeTime(s.last_prompt_at);
  return "never";
}

const LAST_ACTIVITY_TOOLTIP =
  "Most recent per-pane activity: claude turn or tool call (jsonl mtime) " +
  "plus peer_inbox_read/wait heartbeat. 'never' = pane hasn't written yet. " +
  "Suspended rows show the suspend/pause timestamp.";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Sessions() {
  const { slug = "" } = useParams();
  const api = useApiClient();
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

  // T-0006: post-spawn toast surfacing the copyable tmux attach for the
  // session that just appeared. Computed by diffing the SID set before and
  // after the spawn call so we don't need a return-value contract change on
  // api.spawnSession.
  const [spawnNotice, setSpawnNotice] = useState<{ sid: string; window: string } | null>(null);

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

  // T-0099: deep-link target — when the URL carries ?sid=S-..., scroll
  // that row into view, expand its detail row, and flash a transient
  // highlight that fades after 2s. `flashedSidRef` guards against the
  // 10s poll re-firing the flash on every refresh.
  const [flashSid, setFlashSid] = useState<string | null>(null);
  const flashedSidRef = useRef<string | null>(null);

  // T-0039 follow-up: group sessions by initiative on this page too.
  // Default = none (preserves the pre-group view).
  type SessGroupBy = "none" | "initiative";
  const SESS_UNATTACHED = "__unattached__";
  const [groupBy, setGroupBy] = useState<SessGroupBy>("none");
  const [filterInit, setFilterInit] = useState<string>(""); // "" = all
  // Initiatives for grouping (separate from modal's `initiatives` so the
  // grouping view doesn't depend on the modal being opened).
  const [groupingInitiatives, setGroupingInitiatives] = useState<VisionFile[]>([]);
  const collapsedSessLanesKey = `bs.collapsedSessionLanes.${slug}`;
  const [collapsedSessLanes, setCollapsedSessLanes] = useState<Record<string, boolean>>(() => {
    try {
      const raw = localStorage.getItem(collapsedSessLanesKey);
      return raw ? (JSON.parse(raw) as Record<string, boolean>) : {};
    } catch {
      return {};
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(collapsedSessLanesKey, JSON.stringify(collapsedSessLanes));
    } catch {
      /* silent */
    }
  }, [collapsedSessLanesKey, collapsedSessLanes]);
  function toggleSessLane(key: string) {
    setCollapsedSessLanes((prev) => ({ ...prev, [key]: !prev[key] }));
  }

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

  // Initiative list for grouping/filter. Loaded once per slug; cheap to
  // refetch occasionally but we don't need real-time refreshes here.
  useEffect(() => {
    api
      .vision(slug)
      .then((files) =>
        setGroupingInitiatives(
          files.filter(
            (f) => f.name.startsWith("initiatives/") && !f.name.endsWith("/_TEMPLATE.md"),
          ),
        ),
      )
      .catch(() => setGroupingInitiatives([]));
  }, [slug]);

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

  // T-0099: deep-link to a specific session. The URL carries ?sid=S-...;
  // once sessions are loaded we make sure the row will render (clear any
  // active filter, open the archived <details> if needed, expand the
  // containing lane in grouped mode), expand the row's detail panel,
  // scroll it into view, and flash a transient highlight. The
  // `flashedSidRef` guard keeps the 10s poll from re-firing the flash.
  const targetSid = searchParams.get("sid");
  useEffect(() => {
    if (!targetSid) return;
    if (sessions === null) return;
    if (flashedSidRef.current === targetSid) return;
    const found = sessions.find((s) => s.sid === targetSid);
    if (!found) return;

    flashedSidRef.current = targetSid;

    // Drop any filter that would hide the row.
    setFilterInit("");
    if (found.archived) setShowArchived(true);
    if (groupBy === "initiative") {
      const primary = (found.initiative ?? "").trim();
      const extras = (found.extra_initiatives ?? []).filter((i) => i && i !== "~");
      const laneKey =
        primary && primary !== "~"
          ? primary
          : extras.length > 0
            ? extras[0]
            : SESS_UNATTACHED;
      setCollapsedSessLanes((prev) =>
        prev[laneKey] ? { ...prev, [laneKey]: false } : prev,
      );
    }

    // Expand the row's detail panel so the deep-link is informative.
    setExpandedSids((prev) => {
      if (prev.has(targetSid)) return prev;
      const next = new Set(prev);
      next.add(targetSid);
      return next;
    });

    // Wait two animation frames so the section toggles + expansions
    // are committed to the DOM before we measure + scroll.
    let timeoutId: number | undefined;
    const rafId = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const el = document.getElementById(`sess-row-${targetSid}`);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        setFlashSid(targetSid);
        timeoutId = globalThis.window?.setTimeout(() => {
          setFlashSid((cur) => (cur === targetSid ? null : cur));
        }, 2200);
      });
    });

    return () => {
      cancelAnimationFrame(rafId);
      if (timeoutId !== undefined) globalThis.window?.clearTimeout(timeoutId);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetSid, sessions]);

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
    const isFlashing = flashSid === s.sid;
    return (
      <Fragment key={s.sid}>
        <tr
          id={`sess-row-${s.sid}`}
          className={isFlashing ? "mc-row-flash" : undefined}
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
          <td
            style={{
              fontSize: "0.83rem",
              maxWidth: "14rem",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={s.window}
          >
            {s.window}
          </td>

          {/* Attach (T-0006 / collapsed to icon button per T-0098) */}
          <td onClick={(e) => e.stopPropagation()} style={{ width: "1px", whiteSpace: "nowrap" }}>
            <CopyableTmuxAttach session={s.sid} window={s.window} iconOnly />
          </td>

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
            <StatusBadge row={s} />
          </td>

          {/* Started */}
          <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
            {relativeTime(s.started_at)}
          </td>

          {/* Last activity (T-0037: per-pane) */}
          <td
            style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}
            title={LAST_ACTIVITY_TOOLTIP}
          >
            {sessionLastActivity(s)}
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
        {isOpen && renderDetailRow(s, 11)}
      </Fragment>
    );
  }

  function renderArchivedRow(s: SessionRow) {
    const isOpen = expandedSids.has(s.sid);
    const isFlashing = flashSid === s.sid;
    return (
      <Fragment key={s.sid}>
        <tr
          id={`sess-row-${s.sid}`}
          className={isFlashing ? "mc-row-flash" : undefined}
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
          <td onClick={(e) => e.stopPropagation()} style={{ width: "1px", whiteSpace: "nowrap" }}>
            <CopyableTmuxAttach session={s.sid} window={s.window} iconOnly />
          </td>
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
          <td
            style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-dim)" }}
            title={LAST_ACTIVITY_TOOLTIP}
          >
            {sessionLastActivity(s)}
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
        {isOpen && renderDetailRow(s, 9)}
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
      const before = new Set((sessions ?? []).map((s) => s.sid));
      await api.spawnSession(
        slug,
        newWindow.trim(),
        newPrompt.trim() || undefined,
        undefined,
        newInitiative || undefined,
      );
      // T-0006: reload inline so we can diff old/new SIDs and surface the
      // attach command for the freshly spawned session.
      try {
        const after = await api.sessions(slug);
        setSessions(after);
        setError(null);
        const fresh = after.find((s) => !before.has(s.sid) && !s.archived);
        if (fresh) {
          setSpawnNotice({ sid: fresh.sid, window: fresh.window });
        }
      } catch {
        // Best-effort: if the post-spawn fetch fails, fall back to the
        // regular poll loop.
        load();
      }
      setModalOpen(false);
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

  // ---- Initiative grouping for the sessions table ----
  type SessInitMeta = {
    key: string;
    title: string;
    status: "active" | "draft" | "done";
  };
  const sessInitiativeMeta = useMemo<SessInitMeta[]>(() => {
    const list = groupingInitiatives.map((f) => {
      const key = f.name.replace(/^initiatives\//, "");
      const status: SessInitMeta["status"] = f.finished
        ? "done"
        : f.active
          ? "active"
          : "draft";
      return { key, title: key.replace(/\.md$/, ""), status };
    });
    const rank = { active: 0, draft: 1, done: 2 } as const;
    list.sort((a, b) => {
      if (rank[a.status] !== rank[b.status]) return rank[a.status] - rank[b.status];
      return a.title.localeCompare(b.title);
    });
    return list;
  }, [groupingInitiatives]);

  function sessionLaneKey(s: SessionRow): string {
    const primary = (s.initiative ?? "").trim();
    if (primary && primary !== "~") return primary;
    const extras = (s.extra_initiatives ?? []).filter((i) => i && i !== "~");
    if (extras.length > 0) return extras[0];
    return SESS_UNATTACHED;
  }

  // Returns sessions filtered by the current filterInit. When filterInit
  // is empty, returns the input unchanged.
  function applySessFilter(rows: SessionRow[]): SessionRow[] {
    if (!filterInit) return rows;
    return rows.filter((s) => sessionLaneKey(s) === filterInit);
  }

  // Group: lane key → sessions in that lane.
  function groupSessionsByLane(rows: SessionRow[]): Record<string, SessionRow[]> {
    const out: Record<string, SessionRow[]> = { [SESS_UNATTACHED]: [] };
    for (const m of sessInitiativeMeta) out[m.key] = [];
    for (const s of rows) {
      const k = sessionLaneKey(s);
      (out[k] ||= []).push(s);
    }
    return out;
  }

  // Final visible lane list under current filter. Surface orphan lanes
  // (sessions referencing an initiative file we don't have loaded yet).
  function buildVisibleLanes(rows: SessionRow[]): SessInitMeta[] {
    const grouped = groupSessionsByLane(rows);
    const known = new Set(sessInitiativeMeta.map((m) => m.key));
    const synthesized: SessInitMeta[] = [];
    for (const key of Object.keys(grouped)) {
      if (key === SESS_UNATTACHED || known.has(key)) continue;
      synthesized.push({
        key,
        title: `${key.replace(/\.md$/, "")} (orphan)`,
        status: "draft",
      });
    }
    const all: SessInitMeta[] = [
      ...sessInitiativeMeta,
      ...synthesized,
      { key: SESS_UNATTACHED, title: "Unattached", status: "draft" },
    ];
    if (!filterInit) return all;
    return all.filter((m) => m.key === filterInit);
  }

  function sessLanePillStyle(status: SessInitMeta["status"]) {
    if (status === "active") {
      return {
        color: "var(--mc-accent-success, #4ade80)",
        bg: "rgba(74, 222, 128, 0.08)",
        border: "var(--mc-accent-success, #4ade80)",
      };
    }
    if (status === "done") {
      return {
        color: "var(--mc-text-dim)",
        bg: "var(--mc-surface-raised)",
        border: "var(--mc-border)",
      };
    }
    return {
      color: "var(--mc-amber, #fbbf24)",
      bg: "rgba(251, 191, 36, 0.08)",
      border: "var(--mc-amber, #fbbf24)",
    };
  }

  // Renders a colspan'd lane-header row inside an existing table. The
  // header is the only piece visible when the lane is collapsed.
  function renderLaneHeaderRow(
    meta: SessInitMeta,
    rowCount: number,
    colSpan: number,
  ) {
    const pill = sessLanePillStyle(meta.status);
    const collapsed = Boolean(collapsedSessLanes[meta.key]);
    const isUnattached = meta.key === SESS_UNATTACHED;
    return (
      <tr
        key={`lane-${meta.key}`}
        style={{
          background: "var(--mc-surface-deep)",
          borderTop: "1px solid var(--mc-border)",
        }}
      >
        <td
          colSpan={colSpan}
          style={{
            padding: "0.45rem 0.75rem",
            fontFamily: "var(--mc-mono)",
            fontSize: "0.78rem",
          }}
        >
          <div className="d-flex align-items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => toggleSessLane(meta.key)}
              aria-expanded={!collapsed}
              aria-label={
                collapsed ? `Expand ${meta.title}` : `Collapse ${meta.title}`
              }
              title={collapsed ? "Expand lane" : "Collapse lane"}
              style={{
                background: "none",
                border: "none",
                color: "var(--mc-text-dim)",
                cursor: "pointer",
                fontFamily: "var(--mc-mono)",
                fontSize: "0.8rem",
                padding: "0 0.15rem",
                lineHeight: 1,
                width: "1.1rem",
              }}
            >
              {collapsed ? "▸" : "▾"}
            </button>
            <span
              style={{
                fontWeight: 700,
                color: isUnattached ? "var(--mc-text-dim)" : "var(--mc-text)",
                fontSize: "0.85rem",
                letterSpacing: "0.03em",
              }}
            >
              {meta.title}
            </span>
            {!isUnattached && (
              <span
                style={{
                  fontFamily: "var(--mc-mono)",
                  fontSize: "0.62rem",
                  color: pill.color,
                  background: pill.bg,
                  border: `1px solid ${pill.border}`,
                  borderRadius: "2px",
                  padding: "0 5px",
                  textTransform: "uppercase",
                  letterSpacing: "0.06em",
                }}
              >
                {meta.status}
              </span>
            )}
            <span
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.65rem",
                color: "var(--mc-text-dim)",
                marginLeft: "0.5rem",
              }}
            >
              {rowCount} session{rowCount === 1 ? "" : "s"}
            </span>
          </div>
        </td>
      </tr>
    );
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

      {/* T-0006: post-spawn toast. Dismisses on click of the close button,
          stays sticky until then so the user has time to copy the command. */}
      {spawnNotice && (
        <div
          className="alert alert-success d-flex justify-content-between align-items-center flex-wrap gap-2"
          role="status"
        >
          <span style={{ fontSize: "0.85rem" }}>
            Spawned <code style={{ fontFamily: "var(--mc-mono)" }}>{spawnNotice.window}</code>.
            Attach with:{" "}
            <CopyableTmuxAttach
              session={spawnNotice.sid}
              window={spawnNotice.window}
              size="md"
            />
          </span>
          <button
            type="button"
            className="btn-close"
            aria-label="Dismiss"
            style={{ filter: "invert(1) opacity(0.5)" }}
            onClick={() => setSpawnNotice(null)}
          />
        </div>
      )}

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

      {/* Group / filter toolbar */}
      {sessions !== null && sessions.length > 0 && (
        <div
          className="d-flex flex-wrap align-items-center gap-2 mb-2"
          style={{ fontSize: "0.75rem" }}
        >
          <span style={{ fontFamily: "var(--mc-mono)", color: "var(--mc-text-dim)" }}>
            group by:
          </span>
          <div className="btn-group btn-group-sm" role="group">
            {(["none", "initiative"] as const).map((v) => (
              <button
                key={v}
                type="button"
                className={`btn ${groupBy === v ? "btn-secondary" : "btn-outline-secondary"}`}
                style={{ fontSize: "0.72rem", padding: "0.15rem 0.55rem" }}
                onClick={() => setGroupBy(v)}
              >
                {v}
              </button>
            ))}
          </div>
          <div className="d-flex align-items-center gap-2 ms-auto">
            <span style={{ fontFamily: "var(--mc-mono)", color: "var(--mc-text-dim)" }}>
              filter:
            </span>
            <Select
              value={filterInit}
              onChange={setFilterInit}
              style={{ minWidth: "12rem", fontSize: "0.75rem" }}
              ariaLabel="filter by initiative"
              options={[
                { value: "", label: "all initiatives" },
                ...sessInitiativeMeta.map((m) => ({
                  value: m.key,
                  label: m.title,
                  hint: m.status,
                })),
                { value: SESS_UNATTACHED, label: "(unattached)" },
              ]}
            />
          </div>
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
                <th>Attach</th>
                <th>Role</th>
                <th>Target</th>
                <th>Status</th>
                <th>Started</th>
                <th title={LAST_ACTIVITY_TOOLTIP}>Last activity</th>
                <th>Tasks</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {groupBy === "none"
                ? applySessFilter(visibleSessions).map((s) => renderSessionRow(s))
                : buildVisibleLanes(visibleSessions).flatMap((lane) => {
                    const laneRows = groupSessionsByLane(visibleSessions)[lane.key] ?? [];
                    const collapsed = Boolean(collapsedSessLanes[lane.key]);
                    const nodes: React.ReactNode[] = [
                      renderLaneHeaderRow(lane, laneRows.length, 11),
                    ];
                    if (!collapsed) {
                      for (const s of laneRows) nodes.push(renderSessionRow(s));
                    }
                    return nodes;
                  })}
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
                  <th>Attach</th>
                  <th>Role</th>
                  <th>Target</th>
                  <th>Started</th>
                  <th title={LAST_ACTIVITY_TOOLTIP}>Last activity</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {groupBy === "none"
                  ? applySessFilter(archivedSessions).map((s) => renderArchivedRow(s))
                  : buildVisibleLanes(archivedSessions).flatMap((lane) => {
                      const laneRows = groupSessionsByLane(archivedSessions)[lane.key] ?? [];
                      const collapsed = Boolean(collapsedSessLanes[lane.key]);
                      // Skip empty lanes here — archived view is already
                      // off-by-default so noise-suppression matters more.
                      if (laneRows.length === 0) return [];
                      const nodes: React.ReactNode[] = [
                        renderLaneHeaderRow(lane, laneRows.length, 9),
                      ];
                      if (!collapsed) {
                        for (const s of laneRows) nodes.push(renderArchivedRow(s));
                      }
                      return nodes;
                    })}
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
              <Select
                value={newInitiative}
                onChange={setNewInitiative}
                style={{ width: "100%" }}
                ariaLabel="initiative for new teamlead"
                options={[
                  { value: "", label: "— Use project default —" },
                  ...initiatives.map((f) => {
                    const base = f.name.replace(/^initiatives\//, "");
                    return {
                      value: base,
                      label: base,
                      hint: f.active ? "active" : undefined,
                    };
                  }),
                ]}
              />
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
                  <Select
                    value={newTlSid}
                    onChange={setNewTlSid}
                    placeholder="— Pick a teamlead —"
                    style={{ width: "100%" }}
                    ariaLabel="target teamlead"
                    options={activeTeamleads.map((tl) => ({
                      value: tl.sid,
                      label: `${tl.window} (${tl.sid})`,
                    }))}
                  />
                </div>
                <div className="mb-3">
                  <label className="form-label">
                    Backlog task{" "}
                    <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>
                      (optional)
                    </span>
                  </label>
                  <Select
                    value={newTaskId}
                    onChange={setNewTaskId}
                    style={{ width: "100%" }}
                    ariaLabel="backlog task"
                    options={[
                      { value: "", label: "— None (let TL find or create one) —" },
                      ...backlog.map((t) => ({
                        value: t.id,
                        label: `${t.id} · ${t.title}`,
                      })),
                    ]}
                  />
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
