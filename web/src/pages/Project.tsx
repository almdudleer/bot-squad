import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, Task, VisionFile } from "../api";
import { BoardColumn, sortByPriority } from "../components/BoardColumn";
import { Modal } from "../components/Modal";
import { MenuAction, TaskCard } from "../components/TaskCard";

import { PageHelp } from "../components/PageHelp";
const COLUMNS = ["planned", "open", "in_progress", "totest", "reopened", "closed"] as const;
const COLUMN_LABELS: Record<typeof COLUMNS[number], string> = {
  planned: "Planned",
  open: "Open",
  in_progress: "In progress",
  totest: "To Test",
  reopened: "Reopened",
  closed: "Closed",
};
// T-0058: the two "rail" columns — render as a thin drop-strip by default,
// expand on click. Only one is expanded at a time (other auto-collapses).
const RAIL_STATUSES: ReadonlySet<typeof COLUMNS[number]> = new Set(["planned", "closed"]);

type ModalKind = "create" | "editBody" | "addComment" | "setInitiative" | null;
type GroupBy = "none" | "initiative";
type ViewMode = "board" | "list";

// T-0039: sentinel for the "no initiative" lane. Real initiatives are
// vision/initiatives/<basename>.md so this prefix can't collide.
const UNATTACHED = "__unattached__";
// T-0038 stakeholder follow-up #2: synthetic filter value that means
// "only tickets whose initiative is currently in vision/active_initiatives".
// Same collision-proof prefix as UNATTACHED.
const ACTIVE_ONLY = "__active__";

type InitiativeStatus = "active" | "draft" | "done";

type InitiativeMeta = {
  // basename (e.g. "multi-server-installation-process.md"). `UNATTACHED`
  // for the synthetic lane.
  key: string;
  title: string;       // human label (basename minus .md)
  status: InitiativeStatus;
};

function initiativeBasename(visionName: string): string {
  // VisionFile.name is "initiatives/<basename>.md"
  return visionName.replace(/^initiatives\//, "");
}

function statusFromVision(v: VisionFile): InitiativeStatus {
  if (v.finished) return "done";
  if (v.active) return "active";
  return "draft";
}

function STATUS_PILL_COLOR(s: InitiativeStatus): { color: string; bg: string; border: string } {
  switch (s) {
    case "active":
      return {
        color: "var(--mc-accent-success, #4ade80)",
        bg: "rgba(74, 222, 128, 0.08)",
        border: "var(--mc-accent-success, #4ade80)",
      };
    case "done":
      return {
        color: "var(--mc-text-dim)",
        bg: "var(--mc-surface-raised)",
        border: "var(--mc-border)",
      };
    default:
      return {
        color: "var(--mc-amber, #fbbf24)",
        bg: "rgba(251, 191, 36, 0.08)",
        border: "var(--mc-amber, #fbbf24)",
      };
  }
}

export function Project() {
  const { slug = "" } = useParams();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [vision, setVision] = useState<VisionFile[]>([]);
  const [error, setError] = useState<string | null>(null);

  // T-0039: view controls. Defaults reproduce the pre-T-0039 board exactly.
  const [groupBy, setGroupBy] = useState<GroupBy>("none");
  const [viewMode, setViewMode] = useState<ViewMode>("board");
  // Filter is a single initiative basename, UNATTACHED, or "" for all.
  const [filterInit, setFilterInit] = useState<string>("");

  // Per-lane collapsed state. Key = initiative basename or UNATTACHED.
  // Persisted to localStorage per project so a folded set of "done"
  // initiatives stays folded across reloads.
  const collapsedStorageKey = `bs.collapsedLanes.${slug}`;
  const [collapsedLanes, setCollapsedLanes] = useState<Record<string, boolean>>(() => {
    try {
      const raw = localStorage.getItem(collapsedStorageKey);
      return raw ? (JSON.parse(raw) as Record<string, boolean>) : {};
    } catch {
      return {};
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(collapsedStorageKey, JSON.stringify(collapsedLanes));
    } catch {
      /* quota exceeded, private mode, etc. — silent. */
    }
  }, [collapsedStorageKey, collapsedLanes]);
  function toggleLane(key: string) {
    setCollapsedLanes((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  // T-0058: which rail (planned|closed) is currently expanded. null = both
  // collapsed to thin strips. Shared across the ungrouped board AND every
  // initiative lane, so toggling one place toggles them all (consistent UX).
  const [expandedRail, setExpandedRail] = useState<typeof COLUMNS[number] | null>(null);
  function toggleRail(status: typeof COLUMNS[number]) {
    setExpandedRail((prev) => (prev === status ? null : status));
  }

  // modal state
  const [modalKind, setModalKind] = useState<ModalKind>(null);
  const [activeTask, setActiveTask] = useState<Task | null>(null);

  // create form
  const [newTitle, setNewTitle] = useState("");
  const [newBody, setNewBody] = useState("");
  const [newStatus, setNewStatus] = useState<Task["status"]>("open");

  // edit body form
  const [editBody, setEditBody] = useState("");

  // comment form
  const [commentText, setCommentText] = useState("");

  // set-initiative form. "" = unattached.
  const [initiativeChoice, setInitiativeChoice] = useState<string>("");

  const [saving, setSaving] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const reload = () => {
    api.backlog(slug).then(setTasks).catch((e) => setError(String(e)));
  };

  useEffect(() => {
    reload();
    api.vision(slug).then(setVision).catch(() => setVision([]));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // ---- Initiative meta + lane build ----
  // Build the canonical lane list: every initiative file (active+draft+done)
  // gets a lane, plus an Unattached lane at the end. Empty lanes still
  // render (forces backlog hygiene — "we have nothing on update-delivery
  // yet" is visible, not an inference from absence).
  const initiativeMeta = useMemo<InitiativeMeta[]>(() => {
    const list = vision
      .filter((v) => v.name.startsWith("initiatives/") && !v.name.endsWith("/_TEMPLATE.md"))
      .map((v) => {
        const key = initiativeBasename(v.name);
        return {
          key,
          title: key.replace(/\.md$/, ""),
          status: statusFromVision(v),
        };
      });
    // Active first, draft second, done last — keeps the eye on live work.
    list.sort((a, b) => {
      const rank = { active: 0, draft: 1, done: 2 } as const;
      if (rank[a.status] !== rank[b.status]) return rank[a.status] - rank[b.status];
      return a.title.localeCompare(b.title);
    });
    return list;
  }, [vision]);

  // Set of initiative basenames currently in vision/active_initiatives.
  const activeInitiativeKeys = useMemo<Set<string>>(() => {
    return new Set(initiativeMeta.filter((m) => m.status === "active").map((m) => m.key));
  }, [initiativeMeta]);

  // Single source of truth for "does this task pass the current filter".
  function passesFilter(t: Task): boolean {
    if (!filterInit) return true;
    const init = (t.initiative ?? "").trim();
    if (filterInit === UNATTACHED) return !init;
    if (filterInit === ACTIVE_ONLY) return Boolean(init) && activeInitiativeKeys.has(init);
    return init === filterInit;
  }

  // Tasks keyed by initiative basename (or UNATTACHED).
  const tasksByInit = useMemo<Record<string, Task[]>>(() => {
    const out: Record<string, Task[]> = { [UNATTACHED]: [] };
    for (const m of initiativeMeta) out[m.key] = [];
    for (const t of tasks ?? []) {
      const init = (t.initiative ?? "").trim();
      if (init && out[init] !== undefined) {
        out[init].push(t);
      } else if (init) {
        // Task tagged with an initiative basename we don't have a vision
        // file for — bucket it under that basename so the orphan stays
        // visible. Synthesize a lane on render.
        (out[init] ||= []).push(t);
      } else {
        out[UNATTACHED].push(t);
      }
    }
    return out;
  }, [tasks, initiativeMeta]);

  // Final lane list (after applying the filter). Always include the lane
  // matching the active filter even if empty; otherwise show all.
  const visibleLanes = useMemo<InitiativeMeta[]>(() => {
    const synthesized: InitiativeMeta[] = [];
    const known = new Set(initiativeMeta.map((m) => m.key));
    for (const key of Object.keys(tasksByInit)) {
      if (key === UNATTACHED || known.has(key)) continue;
      synthesized.push({ key, title: `${key.replace(/\.md$/, "")} (orphan)`, status: "draft" });
    }
    const all: InitiativeMeta[] = [
      ...initiativeMeta,
      ...synthesized,
      { key: UNATTACHED, title: "Unattached", status: "draft" },
    ];
    if (!filterInit) return all;
    if (filterInit === ACTIVE_ONLY) return all.filter((m) => activeInitiativeKeys.has(m.key));
    return all.filter((m) => m.key === filterInit);
  }, [initiativeMeta, tasksByInit, filterInit, activeInitiativeKeys]);

  // Ungrouped — current 5-column behavior.
  const grouped = COLUMNS.reduce<Record<string, Task[]>>((acc, c) => ({ ...acc, [c]: [] }), {});
  const ungroupedTasks = (tasks ?? []).filter(passesFilter);
  for (const t of ungroupedTasks) {
    if (COLUMNS.includes(t.status as typeof COLUMNS[number])) {
      grouped[t.status].push(t);
    }
  }

  function openCreate() {
    setNewTitle("");
    setNewBody("");
    setNewStatus("open");
    setModalError(null);
    setModalKind("create");
  }

  function closeModal() {
    setModalKind(null);
    setActiveTask(null);
    setModalError(null);
  }

  async function handleCreate() {
    if (!newTitle.trim()) { setModalError("Title is required"); return; }
    setSaving(true);
    setModalError(null);
    try {
      // newBody is the stakeholder's verbatim request — composed by the API
      // into a canonical body with the `## Verbatim request` heading.
      await api.createTask(slug, {
        title: newTitle.trim(),
        verbatim_request: newBody,
        status: newStatus,
      });
      closeModal();
      reload();
    } catch (e) {
      setModalError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleEditBody() {
    if (!activeTask) return;
    setSaving(true);
    setModalError(null);
    try {
      await api.patchTask(slug, activeTask.id, { body: editBody });
      closeModal();
      reload();
    } catch (e) {
      setModalError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleAddComment() {
    if (!activeTask) return;
    if (!commentText.trim()) { setModalError("Comment cannot be empty"); return; }
    setSaving(true);
    setModalError(null);
    try {
      await api.addComment(slug, activeTask.id, commentText.trim());
      closeModal();
      reload();
    } catch (e) {
      setModalError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleMove(taskId: string, fromStatus: Task["status"], toStatus: Task["status"]) {
    if (fromStatus === toStatus) return;
    // Optimistic update so the card moves immediately
    setTasks((prev) =>
      prev ? prev.map((t) => (t.id === taskId ? { ...t, status: toStatus } : t)) : prev,
    );
    try {
      await api.patchTask(slug, taskId, { status: toStatus });
      reload();
    } catch (e) {
      setError(String(e));
      reload();   // revert to server truth
    }
  }

  // Phase 8: within-column reorder. Sparse-int scheme: pick a value that
  // slots the dragged task between its new neighbors; renumber the column
  // when there's no gap to expand into.
  async function handleReorder(taskId: string, status: Task["status"], targetIndex: number) {
    const current = tasks;
    if (!current) return;
    const columnSorted = sortByPriority(current.filter((t) => t.status === status));
    const without = columnSorted.filter((t) => t.id !== taskId);
    const dragged = columnSorted.find((t) => t.id === taskId);
    if (!dragged) return;

    // Translate the visual target index into the position within `without`.
    // If dragging downward in the same column the index above the original
    // position shifts by one. computeDropIndex used the pre-removal indices,
    // so clamp to the post-removal range.
    const oldIndex = columnSorted.findIndex((t) => t.id === taskId);
    let insertAt = targetIndex;
    if (oldIndex !== -1 && targetIndex > oldIndex) insertAt = targetIndex - 1;
    if (insertAt === oldIndex) return;     // dropped on itself — no-op
    if (insertAt < 0) insertAt = 0;
    if (insertAt > without.length) insertAt = without.length;

    const before = insertAt > 0 ? without[insertAt - 1] : null;
    const after = insertAt < without.length ? without[insertAt] : null;
    const beforeP = before && typeof before.priority === "number" ? before.priority : null;
    const afterP = after && typeof after.priority === "number" ? after.priority : null;

    // Compute the new priority. If we can't pick a clean gap, renumber.
    let newPriority: number | null = null;
    let needsRenumber = false;
    if (beforeP === null && afterP === null) {
      // Empty column (or all neighbors null) → start at 100.
      newPriority = 100;
    } else if (beforeP === null && afterP !== null) {
      // Dropping above all: need top - 100 ≥ 0.
      if (afterP > 100) newPriority = afterP - 100;
      else needsRenumber = true;
    } else if (beforeP !== null && afterP === null) {
      // Dropping below all (priority-wise).
      newPriority = beforeP + 100;
    } else if (beforeP !== null && afterP !== null) {
      if (afterP - beforeP >= 2) newPriority = Math.floor((beforeP + afterP) / 2);
      else needsRenumber = true;
    }

    // Compose the post-move order locally for optimistic UI + renumber.
    const newOrder = [...without.slice(0, insertAt), dragged, ...without.slice(insertAt)];

    if (needsRenumber) {
      // Renumber 100, 200, 300, ...
      const updates = newOrder.map((t, i) => ({ id: t.id, priority: (i + 1) * 100 }));
      setTasks((prev) => {
        if (!prev) return prev;
        const byId = new Map(updates.map((u) => [u.id, u.priority]));
        return prev.map((t) => (byId.has(t.id) ? { ...t, priority: byId.get(t.id) as number } : t));
      });
      try {
        await Promise.all(
          updates.map((u) => api.patchTaskPriority(slug, u.id, u.priority)),
        );
        reload();
      } catch (e) {
        setError(String(e));
        reload();
      }
      return;
    }

    if (newPriority === null) return;
    const finalPriority = newPriority;
    setTasks((prev) =>
      prev ? prev.map((t) => (t.id === taskId ? { ...t, priority: finalPriority } : t)) : prev,
    );
    try {
      await api.patchTaskPriority(slug, taskId, finalPriority);
      reload();
    } catch (e) {
      setError(String(e));
      reload();
    }
  }

  async function handleMenuAction(task: Task, action: MenuAction) {
    if (action.kind === "status") {
      try {
        await api.patchTask(slug, task.id, { status: action.status });
        reload();
      } catch (e) {
        setError(String(e));
      }
    } else if (action.kind === "editBody") {
      setActiveTask(task);
      setEditBody(task.body);
      setModalError(null);
      setModalKind("editBody");
    } else if (action.kind === "addComment") {
      setActiveTask(task);
      setCommentText("");
      setModalError(null);
      setModalKind("addComment");
    } else if (action.kind === "setInitiative") {
      setActiveTask(task);
      setInitiativeChoice((task.initiative ?? "").trim());
      setModalError(null);
      setModalKind("setInitiative");
    } else if (action.kind === "delete") {
      if (!confirm(`Delete task ${task.id}: "${task.title}"?`)) return;
      try {
        await api.deleteTask(slug, task.id);
        reload();
      } catch (e) {
        setError(String(e));
      }
    }
  }

  async function handleSetInitiative() {
    if (!activeTask) return;
    setSaving(true);
    setModalError(null);
    try {
      await api.patchTask(slug, activeTask.id, {
        initiative: initiativeChoice ? initiativeChoice : null,
      });
      closeModal();
      reload();
    } catch (e) {
      setModalError(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="container py-4">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>Backlog</h2>
        <button type="button" className="btn btn-primary btn-sm" onClick={openCreate}>
          + New task
        </button>
      </div>
      <PageHelp>
        Open work for this project across four statuses. <strong>Drag</strong> a card
        to change status, <strong>click</strong> a card for full detail, or <strong>⋯</strong>
        for the quick menu (status / edit body / comment / delete).
      </PageHelp>

      {error && <div className="alert alert-danger mt-2">{error}</div>}
      {tasks === null && !error && <div className="mc-loading">Loading</div>}

      {/* T-0039: view-control bar. Defaults to none + board for backwards
          compatibility with the pre-T-0039 board. */}
      <div
        className="d-flex flex-wrap align-items-center gap-2 mb-2"
        style={{ fontSize: "0.75rem" }}
      >
        <SegmentedToggle
          label="Group by"
          value={groupBy}
          onChange={(v) => setGroupBy(v as GroupBy)}
          options={[
            { value: "none", label: "none" },
            { value: "initiative", label: "initiative" },
          ]}
        />
        <SegmentedToggle
          label="View"
          value={viewMode}
          onChange={(v) => setViewMode(v as ViewMode)}
          options={[
            { value: "board", label: "board" },
            { value: "list", label: "list" },
          ]}
        />
        <div className="d-flex align-items-center gap-2 ms-auto">
          <span style={{ fontFamily: "var(--mc-mono)", color: "var(--mc-text-dim)" }}>
            filter:
          </span>
          <select
            className="form-select form-select-sm"
            style={{ width: "auto", minWidth: "12rem", fontSize: "0.75rem" }}
            value={filterInit}
            onChange={(e) => setFilterInit(e.target.value)}
          >
            <option value="">all initiatives</option>
            <option value={ACTIVE_ONLY}>(active initiatives)</option>
            {initiativeMeta.map((m) => (
              <option key={m.key} value={m.key}>
                {m.title} · {m.status}
              </option>
            ))}
            <option value={UNATTACHED}>(unattached)</option>
          </select>
        </div>
      </div>

      {groupBy === "none" ? (
        viewMode === "board" ? (
          <div className="mc-board-row mt-1">
            {COLUMNS.map((c) => (
              <BoardColumn
                key={c}
                title={COLUMN_LABELS[c]}
                status={c}
                tasks={grouped[c]}
                slug={slug}
                onMenuAction={handleMenuAction}
                onMove={handleMove}
                onReorder={handleReorder}
                railMode={
                  RAIL_STATUSES.has(c)
                    ? (expandedRail === c ? "expanded" : "collapsed")
                    : null
                }
                onToggleRail={RAIL_STATUSES.has(c) ? () => toggleRail(c) : undefined}
              />
            ))}
          </div>
        ) : (
          <ListBoard
            tasks={ungroupedTasks}
            slug={slug}
            onMenuAction={handleMenuAction}
          />
        )
      ) : (
        <div className="mt-1">
          {visibleLanes.map((lane) => (
            <InitiativeLane
              key={lane.key}
              meta={lane}
              tasks={tasksByInit[lane.key] ?? []}
              slug={slug}
              viewMode={viewMode}
              collapsed={Boolean(collapsedLanes[lane.key])}
              onToggleCollapsed={() => toggleLane(lane.key)}
              onMenuAction={handleMenuAction}
              onMove={handleMove}
              onReorder={handleReorder}
              expandedRail={expandedRail}
              onToggleRail={toggleRail}
            />
          ))}
        </div>
      )}

      {/* Create task modal */}
      <Modal
        open={modalKind === "create"}
        title="New task"
        onClose={closeModal}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={handleCreate} disabled={saving}>
              {saving ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <div className="mb-3">
          <label className="form-label">Title *</label>
          <input
            className="form-control"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Verbatim request — what you literally want (preserved exactly)</label>
          <textarea
            className="form-control"
            rows={5}
            value={newBody}
            onChange={(e) => setNewBody(e.target.value)}
            placeholder="Write your request in your own words. Sessions cannot rewrite this."
          />
          <div
            style={{
              fontSize: "0.7rem",
              color: "var(--mc-text-dim)",
              marginTop: "0.25rem",
            }}
          >
            This is the source-of-truth artifact for the task. Sessions cannot rewrite
            it; they append progress notes below.
          </div>
        </div>
        <div className="mb-3">
          <label className="form-label">Status</label>
          <select
            className="form-select"
            value={newStatus}
            onChange={(e) => setNewStatus(e.target.value as Task["status"])}
          >
            <option value="planned">Planned</option>
            <option value="open">Open</option>
            <option value="in_progress">In progress</option>
            <option value="totest">To Test</option>
            <option value="reopened">Reopened</option>
            <option value="closed">Closed</option>
          </select>
        </div>
      </Modal>

      {/* Edit body modal */}
      <Modal
        open={modalKind === "editBody"}
        title={`Edit body — ${activeTask?.id ?? ""}`}
        onClose={closeModal}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={handleEditBody} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <textarea
          className="form-control"
          rows={8}
          value={editBody}
          onChange={(e) => setEditBody(e.target.value)}
          autoFocus
        />
      </Modal>

      {/* Add comment modal */}
      <Modal
        open={modalKind === "addComment"}
        title={`Add comment — ${activeTask?.id ?? ""}`}
        onClose={closeModal}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={handleAddComment} disabled={saving}>
              {saving ? "Posting…" : "Post"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <textarea
          className="form-control"
          rows={4}
          placeholder="Write your comment…"
          value={commentText}
          onChange={(e) => setCommentText(e.target.value)}
          autoFocus
        />
      </Modal>

      {/* Set initiative modal — T-0038 follow-up. Quick assign from the
          three-dots menu. The TaskDetail page has the same control inline. */}
      <Modal
        open={modalKind === "setInitiative"}
        title={`Set initiative — ${activeTask?.id ?? ""}`}
        onClose={closeModal}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={handleSetInitiative} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        {modalError && <div className="alert alert-danger">{modalError}</div>}
        <select
          className="form-select"
          value={initiativeChoice}
          onChange={(e) => setInitiativeChoice(e.target.value)}
          autoFocus
        >
          <option value="">— unattached —</option>
          {/* If the current binding isn't in the vision list (orphan: file
              deleted), surface it so saving is still a deliberate act. */}
          {activeTask?.initiative &&
            !initiativeMeta.some((m) => m.key === activeTask.initiative) && (
              <option value={activeTask.initiative}>
                {activeTask.initiative} (orphan)
              </option>
            )}
          {initiativeMeta.map((m) => (
            <option key={m.key} value={m.key}>
              {m.title} · {m.status}
            </option>
          ))}
        </select>
      </Modal>
    </div>
  );
}

// ===========================================================================
// T-0039 helpers
// ===========================================================================

interface SegmentedToggleProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}

function SegmentedToggle({ label, value, onChange, options }: SegmentedToggleProps) {
  return (
    <div className="d-flex align-items-center gap-2">
      <span style={{ fontFamily: "var(--mc-mono)", color: "var(--mc-text-dim)" }}>
        {label}:
      </span>
      <div className="btn-group btn-group-sm" role="group">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            className={`btn ${
              value === o.value ? "btn-secondary" : "btn-outline-secondary"
            }`}
            style={{ fontSize: "0.72rem", padding: "0.15rem 0.55rem" }}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

interface InitiativeLaneProps {
  meta: InitiativeMeta;
  tasks: Task[];
  slug: string;
  viewMode: ViewMode;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onMenuAction: (task: Task, action: MenuAction) => void;
  onMove: (taskId: string, from: Task["status"], to: Task["status"]) => void;
  onReorder: (taskId: string, status: Task["status"], targetIndex: number) => void;
  // T-0058: rail expansion is shared across lanes — the parent owns the state.
  expandedRail: typeof COLUMNS[number] | null;
  onToggleRail: (status: typeof COLUMNS[number]) => void;
}

function InitiativeLane({
  meta,
  tasks,
  slug,
  viewMode,
  collapsed,
  onToggleCollapsed,
  onMenuAction,
  onMove,
  onReorder,
  expandedRail,
  onToggleRail,
}: InitiativeLaneProps) {
  const navigate = useNavigate();
  const counts = COLUMNS.reduce<Record<string, number>>(
    (acc, c) => ({ ...acc, [c]: 0 }),
    {},
  );
  for (const t of tasks) {
    if (COLUMNS.includes(t.status as typeof COLUMNS[number])) counts[t.status]++;
  }
  const pill = STATUS_PILL_COLOR(meta.status);

  const grouped = COLUMNS.reduce<Record<string, Task[]>>(
    (acc, c) => ({ ...acc, [c]: [] }),
    {},
  );
  for (const t of tasks) {
    if (COLUMNS.includes(t.status as typeof COLUMNS[number])) {
      grouped[t.status].push(t);
    }
  }

  const isUnattached = meta.key === UNATTACHED;
  const titleClickable = !isUnattached;

  return (
    <div
      style={{
        marginBottom: "1.25rem",
        borderTop: "1px solid var(--mc-border)",
        paddingTop: "0.75rem",
      }}
    >
      <div
        className="d-flex align-items-center gap-2 mb-2 flex-wrap"
        style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}
      >
        <button
          type="button"
          onClick={onToggleCollapsed}
          aria-expanded={!collapsed}
          aria-label={collapsed ? `Expand ${meta.title}` : `Collapse ${meta.title}`}
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
          onClick={() => {
            if (titleClickable) navigate(`/p/${slug}/vision`);
          }}
          style={{
            fontWeight: 700,
            color: titleClickable ? "var(--mc-text)" : "var(--mc-text-dim)",
            cursor: titleClickable ? "pointer" : "default",
            fontSize: "0.85rem",
            letterSpacing: "0.03em",
          }}
          title={titleClickable ? "Open initiative spec" : "Tasks without an initiative"}
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
          title="planned / open / in-progress / to-test / reopened / closed"
        >
          {COLUMNS.map((c) => `${counts[c]}`).join(" / ")}
        </span>
        <span
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.6rem",
            color: "var(--mc-text-faint)",
            marginLeft: "0.5rem",
          }}
        >
          ({tasks.length} total)
        </span>
      </div>
      {!collapsed && (viewMode === "board" ? (
        <div className="mc-board-row">
          {COLUMNS.map((c) => (
            <BoardColumn
              key={c}
              title={COLUMN_LABELS[c]}
              status={c}
              tasks={grouped[c]}
              slug={slug}
              onMenuAction={onMenuAction}
              onMove={onMove}
              onReorder={onReorder}
              railMode={
                RAIL_STATUSES.has(c)
                  ? (expandedRail === c ? "expanded" : "collapsed")
                  : null
              }
              onToggleRail={RAIL_STATUSES.has(c) ? () => onToggleRail(c) : undefined}
            />
          ))}
        </div>
      ) : (
        <ListBoard
          tasks={tasks}
          slug={slug}
          onMenuAction={onMenuAction}
        />
      ))}
    </div>
  );
}

interface ListBoardProps {
  tasks: Task[];
  slug: string;
  onMenuAction: (task: Task, action: MenuAction) => void;
}

/**
 * Compact list view: one section per status, tasks rendered as cards but
 * stacked into a single column. DnD reordering is omitted to keep the
 * list lean — use the board view when reordering matters.
 */
function ListBoard({ tasks, slug, onMenuAction }: ListBoardProps) {
  const grouped = COLUMNS.reduce<Record<string, Task[]>>(
    (acc, c) => ({ ...acc, [c]: [] }),
    {},
  );
  for (const t of tasks) {
    if (COLUMNS.includes(t.status as typeof COLUMNS[number])) {
      grouped[t.status].push(t);
    }
  }
  return (
    <div className="mt-1">
      {COLUMNS.map((c) => {
        const sorted = sortByPriority(grouped[c]);
        return (
          <div key={c} className="mb-3">
            <div className="mc-board-col-header" style={{ marginBottom: "0.35rem" }}>
              <span>{COLUMN_LABELS[c]}</span>
              <span className="mc-board-count">{sorted.length}</span>
            </div>
            {sorted.length === 0 ? (
              <div className="mc-empty-col">▢ empty</div>
            ) : (
              <div>
                {sorted.map((t) => (
                  <TaskCard
                    key={t.id}
                    task={t}
                    slug={slug}
                    onMenuAction={onMenuAction}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
