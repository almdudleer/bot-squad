import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, isNotFoundError, SessionRow, Task, VisionFile } from "../api";
import { Select, type SelectOption } from "../components/Select";
import {
  isRunning,
  sessionActivity,
  sessionGlyph,
  sessionLabel,
} from "../utils/sessionStatus";

const STATUS_OPTIONS: { value: Task["status"]; label: string }[] = [
  { value: "planned", label: "Planned" },
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "totest", label: "To Test" },
  { value: "reopened", label: "Reopened" },
  { value: "closed", label: "Closed" },
];

// T-0238: at-a-glance stage label/colour for the user-facing header pill.
// Covers every status (incl. planned/closed which aren't in STATUS_OPTIONS).
const STAGE_LABELS: Record<Task["status"], string> = {
  planned: "Planned",
  open: "Open",
  in_progress: "In progress",
  totest: "To Test",
  reopened: "Reopened",
  closed: "Closed",
};
function stageColor(s: Task["status"]): string {
  switch (s) {
    case "in_progress":
      return "var(--mc-cyan)";
    case "totest":
    case "reopened":
      return "var(--mc-amber)";
    case "closed":
      return "var(--mc-text-dim)";
    default:
      return "var(--mc-text-mid)";
  }
}

export type ProgressEntry = { ts: string; sid: string; text: string };

export function parseProgressList(progress: string): ProgressEntry[] {
  if (!progress) return [];
  // Each line: "- <ts> · <sid> · <text>"
  return progress
    .split("\n")
    .map((ln) => ln.trim())
    .filter((ln) => ln.startsWith("- "))
    .map((ln) => {
      const rest = ln.slice(2);
      const parts = rest.split(" · ");
      if (parts.length < 3) return { ts: "", sid: "", text: rest };
      return { ts: parts[0], sid: parts[1], text: parts.slice(2).join(" · ") };
    });
}

// T-0238: the user (stakeholder) is the author of comments dropped into the
// working area. Progress notes from a real session carry an S-<...> SID; the
// stakeholder's comments carry this sentinel so the feed can label them
// "you" rather than as an agent.
export const STAKEHOLDER_SID = "S-stakeholder";

export function TaskDetail() {
  const { slug = "", id = "" } = useParams();
  const navigate = useNavigate();

  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  // T-0139: distinguish three terminal states for the initial load —
  // "loading" → spinner; "not_found" → titled panel with back-link;
  // "network_error" → retryable banner. Replaces the previous single
  // `error` string which conflated 404 with transient failures.
  const [loadState, setLoadState] = useState<"loading" | "ok" | "not_found" | "network_error">("loading");
  const [saving, setSaving] = useState(false);
  const [activeDevs, setActiveDevs] = useState<SessionRow[]>([]);
  // T-0104: keep every session row keyed by sid so we can look up the
  // worker-derived activity for the bound dev session below. The
  // /backlog endpoint that drove `task.session` doesn't enrich with
  // the activity probe, so we join client-side from /sessions.
  const [sessionsBySid, setSessionsBySid] = useState<Record<string, SessionRow>>({});
  // T-0038 follow-up: surface initiative binding here. We load every
  // initiative file (active+draft+done) so the operator can bind a task
  // to e.g. a draft initiative without first activating it.
  const [initiatives, setInitiatives] = useState<VisionFile[]>([]);

  const [editingTitle, setEditingTitle] = useState(false);
  const [titleValue, setTitleValue] = useState("");
  const titleRef = useRef<HTMLInputElement>(null);

  const [statusValue, setStatusValue] = useState<Task["status"]>("open");

  // Section edit state — each section saves independently.
  const [editingVerbatim, setEditingVerbatim] = useState(false);
  const [verbatimValue, setVerbatimValue] = useState("");

  const [editingContext, setEditingContext] = useState(false);
  const [contextValue, setContextValue] = useState("");

  const [progressText, setProgressText] = useState("");
  const [progressError, setProgressError] = useState<string | null>(null);
  const [progressSaving, setProgressSaving] = useState(false);

  const [actionError, setActionError] = useState<string | null>(null);

  // T-0172: related docs (ticket→doc half of the bidirectional mention).
  const [docToLink, setDocToLink] = useState("");

  function loadTask() {
    setError(null);
    setLoadState("loading");
    api
      .backlog(slug)
      .then((tasks) => {
        const found = tasks.find((t) => t.id === id);
        if (!found) { setLoadState("not_found"); return; }
        setTask(found);
        setTitleValue(found.title);
        setStatusValue(found.status);
        setVerbatimValue(found.verbatim ?? "");
        setContextValue(found.context ?? "");
        setLoadState("ok");
        // Side-loads only fire once the parent project is known to exist
        // (i.e. backlog returned 200). Keeps the not-found path quiet —
        // no extra 404s on /sessions or /vision.
        api.sessions(slug)
          .then((rows) => {
            setActiveDevs(rows.filter((s) => {
              if (s.status !== "active") return false;
              const tid = (s.task_id ?? "").trim();
              return Boolean(tid) && tid !== "~";
            }));
            const map: Record<string, SessionRow> = {};
            for (const r of rows) map[r.sid] = r;
            setSessionsBySid(map);
          })
          .catch(() => {
            setActiveDevs([]);
            setSessionsBySid({});
          });
        api.vision(slug)
          .then((files) =>
            setInitiatives(
              files.filter(
                (f) =>
                  f.name.startsWith("initiatives/") && !f.name.endsWith("/_TEMPLATE.md"),
              ),
            ),
          )
          .catch(() => setInitiatives([]));
      })
      .catch((e) => {
        if (isNotFoundError(e)) { setLoadState("not_found"); return; }
        setError(String(e));
        setLoadState("network_error");
      });
  }

  // Build the option list once per `initiatives` change. Sort active first,
  // draft second, done last — matches the swimlane order on the board.
  const initiativeOptions = useMemo(() => {
    const list = initiatives.map((f) => ({
      basename: f.name.replace(/^initiatives\//, ""),
      active: Boolean(f.active),
      finished: Boolean(f.finished),
    }));
    const rank = (i: { active: boolean; finished: boolean }) =>
      i.finished ? 2 : i.active ? 0 : 1;
    list.sort((a, b) => {
      const ra = rank(a);
      const rb = rank(b);
      if (ra !== rb) return ra - rb;
      return a.basename.localeCompare(b.basename);
    });
    return list;
  }, [initiatives]);

  async function saveInitiative(value: string | null) {
    if (!task) return;
    setSaving(true);
    setActionError(null);
    try {
      // value="" from the select means "unattached" — send null to clear.
      await api.patchTask(slug, id, {
        initiative: value && value.length > 0 ? value : null,
      });
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function bindToDev(sid: string) {
    if (!task) return;
    try {
      await api.bindTask(slug, sid, task.id);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    }
  }

  async function unassignSession() {
    if (!task || !task.session) return;
    // unbind_task only works on extras — for the session's primary task
    // we'd be erasing the session's identity, which the worker refuses.
    if (!window.confirm(`Unbind ${task.id} from ${task.session.sid}?`)) return;
    try {
      await api.unbindTask(slug, task.session.sid, task.id);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    }
  }

  useEffect(() => {
    loadTask();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, id]);

  useEffect(() => {
    if (editingTitle && titleRef.current) titleRef.current.focus();
  }, [editingTitle]);

  function composeBody(verbatim: string, context: string, progress: string): string {
    const parts: string[] = [];
    const v = verbatim.trim();
    const c = context.trim();
    const p = progress.trim();
    if (v) parts.push(`## Verbatim request\n\n${v}\n`);
    if (c) parts.push(`## Context\n\n${c}\n`);
    if (p) parts.push(`## Progress\n\n${p}\n`);
    return parts.join("\n");
  }

  async function saveTitle() {
    if (!task || titleValue.trim() === task.title) { setEditingTitle(false); return; }
    if (!titleValue.trim()) { setEditingTitle(false); return; }
    setSaving(true);
    setActionError(null);
    try {
      await api.patchTask(slug, id, { title: titleValue.trim() });
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
      setEditingTitle(false);
    }
  }

  async function saveStatus(newStatus: Task["status"]) {
    setStatusValue(newStatus);
    setSaving(true);
    setActionError(null);
    try {
      await api.patchTask(slug, id, { status: newStatus });
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function saveVerbatim() {
    if (!task) return;
    setSaving(true);
    setActionError(null);
    try {
      const body = composeBody(verbatimValue, task.context ?? "", task.progress ?? "");
      await api.patchTask(slug, id, { body });
      setEditingVerbatim(false);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function saveContext() {
    if (!task) return;
    setSaving(true);
    setActionError(null);
    try {
      const body = composeBody(task.verbatim ?? "", contextValue, task.progress ?? "");
      await api.patchTask(slug, id, { body });
      setEditingContext(false);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function postProgress() {
    if (!progressText.trim()) { setProgressError("Comment cannot be empty"); return; }
    setProgressSaving(true);
    setProgressError(null);
    try {
      // T-0238: the stakeholder's comment is recorded into the working-area
      // feed (progress notes) — the reused, no-new-schema comment channel.
      await api.addProgress(slug, id, STAKEHOLDER_SID, progressText.trim());
      setProgressText("");
      loadTask();
    } catch (e) {
      setProgressError(String(e));
    } finally {
      setProgressSaving(false);
    }
  }

  async function linkDoc() {
    if (!task) return;
    const d = docToLink.trim().toUpperCase();
    if (!/^D-\d{4}$/.test(d)) { setActionError("Doc must look like D-0123."); return; }
    setActionError(null);
    try {
      // Bidirectional: also writes this doc-id into task.related_docs.
      await api.linkDoc(slug, d, task.id);
      setDocToLink("");
      loadTask();
    } catch (e) {
      setActionError(String(e));
    }
  }

  async function unlinkDoc(docId: string) {
    if (!task) return;
    setActionError(null);
    try {
      await api.unlinkDoc(slug, docId, task.id);
      loadTask();
    } catch (e) {
      setActionError(String(e));
    }
  }

  async function deleteTask() {
    if (!task) return;
    if (!confirm(`Delete task ${task.id}: "${task.title}"?`)) return;
    try {
      await api.deleteTask(slug, id);
      navigate(`/p/${slug}`);
    } catch (e) {
      setActionError(String(e));
    }
  }

  if (loadState === "not_found") {
    return (
      <div className="container py-4">
        <h4 style={{ fontSize: "1.05rem", fontWeight: 600, marginBottom: "0.75rem" }}>
          Task {id} not found
        </h4>
        <p style={{ fontSize: "0.85rem", color: "var(--mc-text-dim)" }}>
          No backlog entry for <code>{id}</code> in project <code>{slug}</code>.
        </p>
        <Link to={`/p/${slug}`} style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}>
          ← back to backlog
        </Link>
      </div>
    );
  }

  if (loadState === "network_error") {
    return (
      <div className="container py-4">
        <div className="alert alert-danger">
          Couldn't load task — {error}
        </div>
        <button type="button" className="btn btn-sm btn-outline-secondary" onClick={loadTask}>
          Retry
        </button>
      </div>
    );
  }

  if (loadState === "loading" || !task) {
    return (
      <div className="container py-4">
        <div className="mc-loading">Loading</div>
      </div>
    );
  }

  const progressEntries = parseProgressList(task.progress ?? "").reverse();

  return (
    <div className="container py-4" style={{ maxWidth: "800px" }}>
      {/* Back link */}
      <div className="mb-2">
        <Link
          to={`/p/${slug}`}
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.72rem",
            color: "var(--mc-text-dim)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            textDecoration: "none",
          }}
        >
          ← back to board
        </Link>
      </div>

      {actionError && <div className="alert alert-danger">{actionError}</div>}

      {/* ==================================================================
          USER-FACING HEADER (T-0238) — what the user asked for + the stage.
          The header is the user's summary; agents work in the area below.
          ================================================================== */}
      <div className="mc-task-zone mc-zone-header">
        <div className="mc-zone-tag">▸ User-facing — what you asked for</div>

        {/* Title row — id, editable title, at-a-glance stage pill */}
        <div className="d-flex align-items-start gap-2 mb-3">
          <span
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.72rem",
              color: "var(--mc-text-dim)",
              paddingTop: "0.35rem",
            }}
          >
            {task.id}
          </span>
          <div className="flex-grow-1">
            {editingTitle ? (
              <input
                ref={titleRef}
                className="form-control fw-semibold"
                style={{ fontSize: "1.05rem" }}
                value={titleValue}
                onChange={(e) => setTitleValue(e.target.value)}
                onBlur={saveTitle}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { e.preventDefault(); saveTitle(); }
                  if (e.key === "Escape") { setEditingTitle(false); setTitleValue(task.title); }
                }}
                disabled={saving}
              />
            ) : (
              <h4
                style={{
                  cursor: "text",
                  marginBottom: 0,
                  fontSize: "1.05rem",
                  fontWeight: 600,
                  color: "var(--mc-text)",
                }}
                title="Click to edit title"
                onClick={() => setEditingTitle(true)}
              >
                {task.title}
              </h4>
            )}
          </div>
          <span
            title={`Current stage: ${STAGE_LABELS[statusValue]}`}
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.62rem",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              color: stageColor(statusValue),
              background: "var(--mc-surface-raised)",
              border: `1px solid ${stageColor(statusValue)}`,
              borderRadius: "2px",
              padding: "1px 6px",
              whiteSpace: "nowrap",
              flexShrink: 0,
            }}
          >
            {STAGE_LABELS[statusValue]}
          </span>
        </div>

        {/* Stage + initiative controls — how the user steers the task. */}
        <div className="d-flex flex-wrap align-items-center gap-3 mb-3" style={{ fontSize: "0.78rem" }}>
          <div className="d-flex align-items-center gap-2">
            <label
              htmlFor="task-status"
              style={{ color: "var(--mc-text-dim)", fontFamily: "var(--mc-mono)", margin: 0 }}
            >
              stage:
            </label>
            <Select
              id="task-status"
              value={statusValue}
              onChange={(v) => saveStatus(v as Task["status"])}
              disabled={saving}
              style={{ minWidth: "9rem" }}
              ariaLabel="task status"
              options={STATUS_OPTIONS.map((s) => ({ value: s.value, label: s.label }))}
            />
          </div>
          {/* T-0038: initiative binding. */}
          <div className="d-flex align-items-center gap-2">
            <label
              htmlFor="task-initiative"
              style={{ color: "var(--mc-text-dim)", fontFamily: "var(--mc-mono)", margin: 0 }}
            >
              initiative:
            </label>
            {(() => {
              const orphan: SelectOption | null =
                task.initiative &&
                !initiativeOptions.some((i) => i.basename === task.initiative)
                  ? { value: task.initiative, label: `${task.initiative} (orphan)` }
                  : null;
              const options: SelectOption[] = [
                { value: "", label: "— unattached —" },
                ...(orphan ? [orphan] : []),
                ...initiativeOptions.map((i) => {
                  const tag = i.finished ? "done" : i.active ? "active" : "draft";
                  return {
                    value: i.basename,
                    label: i.basename.replace(/\.md$/, ""),
                    hint: tag,
                  };
                }),
              ];
              return (
                <Select
                  id="task-initiative"
                  value={task.initiative ?? ""}
                  onChange={(v) => saveInitiative(v || null)}
                  disabled={saving}
                  style={{ minWidth: "14rem", maxWidth: "26rem" }}
                  ariaLabel="initiative binding"
                  options={options}
                />
              );
            })()}
          </div>
        </div>

        {/* The ask — verbatim, source of truth */}
        <div className="d-flex justify-content-between align-items-center mb-2">
          <div className="mc-section-title" style={{ margin: 0, border: "none", padding: 0 }}>
            The ask — source of truth
          </div>
          {!editingVerbatim && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.72rem" }}
              onClick={() => { setVerbatimValue(task.verbatim ?? ""); setEditingVerbatim(true); }}
            >
              Edit
            </button>
          )}
        </div>
        <div
          style={{
            fontSize: "0.7rem",
            color: "var(--mc-text-dim)",
            marginBottom: "0.4rem",
          }}
        >
          The anti-broken-phone record. Sessions do not edit this — only you.
          They record their work and your comments in the working area below.
        </div>
        {editingVerbatim ? (
          <>
            <textarea
              className="form-control"
              rows={6}
              value={verbatimValue}
              onChange={(e) => setVerbatimValue(e.target.value)}
              autoFocus
            />
            <div className="mt-2 d-flex gap-2">
              <button type="button" className="btn btn-primary btn-sm" onClick={saveVerbatim} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => { setEditingVerbatim(false); setVerbatimValue(task.verbatim ?? ""); }}
              >
                Cancel
              </button>
            </div>
          </>
        ) : (
          <pre
            className="mc-pre"
            style={{
              borderLeft: "3px solid var(--mc-accent-success, #4ade80)",
              paddingLeft: "0.75rem",
              marginBottom: 0,
            }}
          >
            {task.verbatim?.trim() ? task.verbatim : (
              <span style={{ color: "var(--mc-text-dim)", fontStyle: "italic" }}>
                (no request recorded)
              </span>
            )}
          </pre>
        )}
      </div>

      {/* ==================================================================
          AGENT WORKING AREA (T-0238) — the progress-notes feed reused as the
          agents' working / negotiation log AND where the user's comments are
          recorded. No new schema field (operator fork 2026-06-19).
          ================================================================== */}
      <div className="mc-task-zone mc-zone-work">
        <div className="mc-zone-tag">⚙ Agent working area — working / negotiation log + your comments</div>
        <div
          style={{
            fontSize: "0.7rem",
            color: "var(--mc-text-dim)",
            marginBottom: "0.6rem",
          }}
        >
          Where sessions record progress and negotiate the work. Drop a comment
          here and it's recorded in the same log — newest first.
        </div>

        {progressEntries.length === 0 && (
          <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
            Nothing recorded yet. Add the first comment below.
          </p>
        )}
        {progressEntries.map((p, i) => {
          const mine = p.sid === STAKEHOLDER_SID;
          return (
            <div
              key={i}
              style={{
                background: "var(--mc-surface-raised)",
                border: "1px solid var(--mc-border)",
                borderLeft: mine ? "3px solid var(--mc-cyan)" : "1px solid var(--mc-border)",
                borderRadius: "3px",
                padding: "0.4rem 0.625rem",
                marginBottom: "0.35rem",
                fontSize: "0.78rem",
                color: "var(--mc-text-mid)",
              }}
            >
              <span
                style={{
                  fontFamily: "var(--mc-mono)",
                  fontSize: "0.68rem",
                  color: mine ? "var(--mc-cyan)" : "var(--mc-text-dim)",
                  marginRight: "0.5rem",
                }}
              >
                {p.ts} · {mine ? "you" : p.sid}
              </span>
              {p.text}
            </div>
          );
        })}

        <div className="mt-3">
          {progressError && <div className="alert alert-danger py-1 small">{progressError}</div>}
          <input
            type="text"
            className="form-control form-control-sm"
            placeholder="Add a comment — recorded in the working log (cap 240 chars)…"
            maxLength={240}
            value={progressText}
            onChange={(e) => setProgressText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); postProgress(); } }}
          />
          <button
            type="button"
            className="btn btn-sm btn-primary mt-2"
            onClick={postProgress}
            disabled={progressSaving}
          >
            {progressSaving ? "Posting…" : "Add comment"}
          </button>
        </div>
      </div>

      {/* ==================================================================
          TASK DETAILS — plumbing the user rarely touches. Process binding,
          TL context, related docs, and the session-history audit trail.
          ================================================================== */}
      <div className="mc-task-zone mc-zone-details">
        <div className="mc-zone-tag">Task details</div>

        {/* Unified dev-session select. */}
        <div className="mb-3 d-flex align-items-center gap-2 flex-wrap" style={{ fontSize: "0.78rem" }}>
          <label
            htmlFor="task-session"
            style={{
              color: "var(--mc-text-dim)",
              fontFamily: "var(--mc-mono)",
              minWidth: "5.5rem",
              margin: 0,
            }}
          >
            dev session:
          </label>
          {(() => {
            // Surface the current binding even if it's not in activeDevs
            // (paused/suspended) so the select reflects reality. T-0104:
            // label via the canonical activity formatter so this row reads
            // the same vocabulary as the rest of the page.
            const orphanSession: SelectOption | null =
              task.session && !activeDevs.some((d) => d.sid === task.session!.sid)
                ? {
                    value: task.session.sid,
                    label: `${task.session.sid} (${sessionLabel(
                      sessionActivity(sessionsBySid[task.session.sid] ?? task.session),
                    )})`,
                  }
                : null;
            const options: SelectOption[] = [
              { value: "", label: "— none —", disabled: true },
              ...(orphanSession ? [orphanSession] : []),
              ...activeDevs.map((d) => ({
                value: d.sid,
                label: `${d.window || d.sid} (${d.sid})`,
              })),
              {
                action: true,
                key: "__new__",
                label: "+ Create new dev session…",
                onSelect: () =>
                  navigate(`/p/${slug}/sessions?role=dev&task=${encodeURIComponent(task.id)}`),
              },
            ];
            return (
              <Select
                id="task-session"
                value={task.session ? task.session.sid : ""}
                onChange={(v) => {
                  if (v) bindToDev(v);
                }}
                disabled={saving}
                style={{ minWidth: "16rem", maxWidth: "30rem" }}
                ariaLabel="dev session binding"
                options={options}
              />
            );
          })()}
          {task.session && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.7rem" }}
              title="Clear the dev-session binding for this task"
              onClick={unassignSession}
              disabled={saving}
            >
              Unassign
            </button>
          )}
          {task.session && (() => {
            // T-0104: prefer the worker-derived activity (from /sessions
            // join via sessionsBySid) over the raw md status. Falls back
            // to the status-derived mapping in sessionActivity() when
            // the session isn't in the live list (e.g. suspended).
            const live = sessionsBySid[task.session.sid];
            const act = sessionActivity(live ?? task.session);
            const green = isRunning(act);
            return (
              <span
                style={{
                  fontFamily: "var(--mc-mono)",
                  fontSize: "0.7rem",
                  color: green
                    ? "var(--mc-accent-success, #4ade80)"
                    : "var(--mc-text-dim)",
                }}
                title={`session status: ${act}`}
              >
                {sessionGlyph(act)} {sessionLabel(act)}
              </span>
            );
          })()}
        </div>

        {/* Context — optional TL clarification */}
        <div className="mb-4">
          <div className="d-flex justify-content-between align-items-center mb-2">
            <div className="mc-section-title" style={{ margin: 0 }}>Context (optional)</div>
            {!editingContext && (
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                style={{ fontSize: "0.72rem" }}
                onClick={() => { setContextValue(task.context ?? ""); setEditingContext(true); }}
              >
                Edit
              </button>
            )}
          </div>
          {editingContext ? (
            <>
              <textarea
                className="form-control"
                rows={4}
                value={contextValue}
                onChange={(e) => setContextValue(e.target.value)}
                autoFocus
                placeholder="Short TL clarification — keep it brief."
              />
              <div className="mt-2 d-flex gap-2">
                <button type="button" className="btn btn-primary btn-sm" onClick={saveContext} disabled={saving}>
                  {saving ? "Saving…" : "Save"}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => { setEditingContext(false); setContextValue(task.context ?? ""); }}
                >
                  Cancel
                </button>
              </div>
            </>
          ) : task.context?.trim() ? (
            <pre className="mc-pre">{task.context}</pre>
          ) : (
            <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>(no context yet)</p>
          )}
        </div>

        {/* Related docs — ticket→doc half of the T-0172 bidirectional mention */}
        <div className="mb-4">
          <div className="mc-section-title">Related docs ({(task.related_docs ?? []).length})</div>
          <div className="d-flex flex-wrap gap-2 align-items-center mb-2">
            {(task.related_docs ?? []).length === 0 && (
              <span style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
                No docs linked. Link an architecture/design/support doc so agents find the context.
              </span>
            )}
            {(task.related_docs ?? []).map((d) => (
              <span key={d} className="d-inline-flex align-items-center gap-1" style={{ fontSize: "0.78rem" }}>
                <Link to={`/p/${slug}/docs?doc=${encodeURIComponent(d)}`} style={{ fontFamily: "var(--mc-mono)" }}>{d}</Link>
                <button type="button" className="btn btn-link btn-sm p-0" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }} title="Unlink" onClick={() => unlinkDoc(d)}>✕</button>
              </span>
            ))}
          </div>
          <div className="d-flex gap-2" style={{ maxWidth: "20rem" }}>
            <input
              type="text"
              className="form-control form-control-sm"
              style={{ fontFamily: "var(--mc-mono)", fontSize: "0.76rem" }}
              placeholder="D-0123"
              value={docToLink}
              onChange={(e) => setDocToLink(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); linkDoc(); } }}
            />
            <button type="button" className="btn btn-outline-primary btn-sm" style={{ fontSize: "0.7rem" }} onClick={linkDoc}>
              Link doc
            </button>
          </div>
        </div>

        {/* Session history — T-0106. Frontmatter `session_history` is an
            append-only ordered list of SIDs that have ever bound this task
            (spawn / bind_task / resume). Render chronologically; each row
            links to the Sessions page deep-linked to that SID, and shows the
            session's `started_at` (joined from the live sessions list) as a
            first-touch proxy. */}
        <div>
          <div className="mc-section-title">
            Session history ({(task.session_history ?? []).length})
          </div>
          {(task.session_history ?? []).length === 0 ? (
            <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
              No sessions have touched this task yet.
            </p>
          ) : (
            <div>
              {(task.session_history ?? []).map((sid, i) => {
                const row = sessionsBySid[sid];
                const startedAt = row?.started_at;
                return (
                  <div
                    key={`${sid}-${i}`}
                    style={{
                      background: "var(--mc-surface-raised)",
                      border: "1px solid var(--mc-border)",
                      borderRadius: "3px",
                      padding: "0.4rem 0.625rem",
                      marginBottom: "0.35rem",
                      fontSize: "0.78rem",
                      display: "flex",
                      alignItems: "center",
                      gap: "0.75rem",
                    }}
                  >
                    <Link
                      to={`/p/${slug}/sessions?sid=${encodeURIComponent(sid)}`}
                      style={{
                        fontFamily: "var(--mc-mono)",
                        fontSize: "0.78rem",
                        color: "var(--mc-accent)",
                        textDecoration: "none",
                      }}
                      title="Open this session on the Sessions page"
                    >
                      {sid}
                    </Link>
                    <span
                      style={{
                        fontFamily: "var(--mc-mono)",
                        fontSize: "0.7rem",
                        color: "var(--mc-text-dim)",
                        marginLeft: "auto",
                      }}
                      title={
                        startedAt
                          ? `Session started_at: ${startedAt}`
                          : "Session not in the current registry (suspended/archived/legacy)"
                      }
                    >
                      {startedAt
                        ? new Date(startedAt).toLocaleString()
                        : "—"}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Actions */}
      <div
        className="d-flex gap-2 align-items-center"
        style={{ borderTop: "1px solid var(--mc-border)", paddingTop: "1rem" }}
      >
        <button type="button" className="btn btn-sm btn-outline-danger" onClick={deleteTask}>
          Delete task
        </button>
        {task.from && (
          <Link to={`/p/${slug}/feedback`} className="btn btn-sm btn-outline-secondary">
            View source feedback ({task.from})
          </Link>
        )}
        {task.updated && (
          <span
            style={{
              marginLeft: "auto",
              fontFamily: "var(--mc-mono)",
              fontSize: "0.7rem",
              color: "var(--mc-text-dim)",
            }}
          >
            updated {new Date(task.updated).toLocaleString()}
          </span>
        )}
      </div>
    </div>
  );
}
