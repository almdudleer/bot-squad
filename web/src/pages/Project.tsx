import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  isNotFoundError,
  type SessionRow,
  type Task,
  type VisionFile,
} from "../api";
import { useApiClient } from "../apiContext";
import { taskInitiativeKey } from "../initiativeTasks";
import { BoardColumn, sortByPriority } from "../components/BoardColumn";
import { CopyableTmuxAttach } from "../components/CopyableTmuxAttach";
import { ObservabilityPanel } from "../components/ObservabilityPanel";
import { Select } from "../components/Select";
import { TaskCard } from "../components/TaskCard";
import { sessionActivity } from "../utils/sessionStatus";
import { Coachmark, hasSeen, useOnboardingState } from "../onboarding";
import {
  PROJECT_ROLE_BLURBS,
  STEP_13_1_TITLE,
  STEP_13_2_TITLE,
  STEP_13_3_TITLE,
  STEP_13_4_TITLE,
} from "../onboarding/copy";

import { PageHelp } from "../components/PageHelp";
import { CANONICAL_LABELS, CANONICAL_STATE } from "../canonicalStatus";
// T-0479: column order tracks the canonical 4-state model (backlog →
// in-progress → validating → done), so the internal statuses that roll up into
// the same canonical state sit adjacent. backlog = planned/open/reopened;
// in-progress = in_progress; validating = totest; done = closed.
const COLUMNS = ["planned", "open", "reopened", "in_progress", "totest", "closed"] as const;
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

// T-0512 (M9 / Part A): subtask nesting. A task whose `parent_task` points at
// another VISIBLE task renders nested under that parent (and is suppressed as a
// top-level card). A child whose parent isn't in the current view (filtered out
// / orphaned) keeps showing standalone. Returns the parent→children map and the
// set of child ids to suppress at top level.
export function buildNesting(tasks: Task[]): {
  subtasksByParent: Record<string, Task[]>;
  nestedChildIds: Set<string>;
} {
  const visibleIds = new Set(tasks.map((t) => t.id));
  const subtasksByParent: Record<string, Task[]> = {};
  const nestedChildIds = new Set<string>();
  for (const t of tasks) {
    const parent = (t.parent_task ?? "").trim();
    if (parent && visibleIds.has(parent)) {
      (subtasksByParent[parent] ||= []).push(t);
      nestedChildIds.add(t.id);
    }
  }
  return { subtasksByParent, nestedChildIds };
}

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
  const api = useApiClient();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [vision, setVision] = useState<VisionFile[]>([]);
  // T-0104: per-sid session map for client-side activity enrichment.
  // /backlog's task.session doesn't run the worker activity probe, so
  // we join with /sessions here and stamp `task.session.activity` on
  // each card. Empty map = the fetch hasn't landed yet (or failed
  // silently); TaskCard falls back to mapping the raw md status.
  const [sessionsBySid, setSessionsBySid] = useState<Record<string, SessionRow>>({});
  const [error, setError] = useState<string | null>(null);
  // T-0138: gate the chrome on the project actually existing. `null` =
  // checking; `false` = backlog 404'd, render a not-found panel instead of
  // the full sidebar/board so per-project polling doesn't fire stray 404s.
  const [slugMissing, setSlugMissing] = useState<boolean | null>(null);

  // T-0039: view controls. Defaults reproduce the pre-T-0039 board exactly.
  // T-0097: persisted in URL query params (`group`, `view`, `init`) so
  // refresh / deep-link / open-in-new-tab all reproduce the same view.
  // URL is the source of truth — no useState, no sync drift. Default
  // values are omitted from the URL to keep deep-links clean.
  const [searchParams, setSearchParams] = useSearchParams();
  const groupBy: GroupBy =
    searchParams.get("group") === "initiative" ? "initiative" : "none";
  const viewMode: ViewMode =
    searchParams.get("view") === "list" ? "list" : "board";
  // Filter is a single initiative basename, UNATTACHED, ACTIVE_ONLY, or "" for all.
  const filterInit: string = searchParams.get("init") ?? "";

  // Toggle a single param, dropping it when the value matches the default
  // (keeps the URL minimal). `replace: true` so back-button doesn't ladder
  // through every selector flip.
  function updateParam(key: string, value: string, defaultValue: string) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value === defaultValue) next.delete(key);
        else next.set(key, value);
        return next;
      },
      { replace: true },
    );
  }
  const setGroupBy = (v: GroupBy) => updateParam("group", v, "none");
  const setViewMode = (v: ViewMode) => updateParam("view", v, "board");
  const setFilterInit = (v: string) => updateParam("init", v, "");

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
  // T-0272: toggle takes the lane's *effective* collapsed state (which may
  // be a default, not an explicit entry) and writes its inverse, so the
  // first click on a default-collapsed done/empty lane always expands it.
  function toggleLane(key: string, currentCollapsed: boolean) {
    setCollapsedLanes((prev) => ({ ...prev, [key]: !currentCollapsed }));
  }

  // T-0058: which rail (planned|closed) is currently expanded. null = both
  // collapsed to thin strips. Shared across the ungrouped board AND every
  // initiative lane, so toggling one place toggles them all (consistent UX).
  const [expandedRail, setExpandedRail] = useState<typeof COLUMNS[number] | null>(null);
  function toggleRail(status: typeof COLUMNS[number]) {
    setExpandedRail((prev) => (prev === status ? null : status));
  }

  // T-0104: refresh the sessions map so card pills track the
  // worker-derived `running`/`idle` flip in near-real-time. Poll
  // matches the Sessions page cadence (10s) so a typed prompt lights
  // up the card within one poll window.
  const reloadSessions = () => {
    api
      .sessions(slug)
      .then((rows) => {
        const map: Record<string, SessionRow> = {};
        for (const r of rows) map[r.sid] = r;
        setSessionsBySid(map);
      })
      .catch(() => {
        /* silent — TaskCard handles missing rows via its fallback. */
      });
  };

  // T-0138: probe the slug via /backlog. 404 → not-found panel; any other
  // outcome opens the gate for the periodic polls below.
  useEffect(() => {
    let cancelled = false;
    setSlugMissing(null);
    setError(null);
    api.backlog(slug)
      .then((rows) => {
        if (cancelled) return;
        setTasks(rows);
        setSlugMissing(false);
      })
      .catch((e) => {
        if (cancelled) return;
        if (isNotFoundError(e)) setSlugMissing(true);
        else { setSlugMissing(false); setError(String(e)); }
      });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  useEffect(() => {
    if (slugMissing !== false) return;
    api.vision(slug).then(setVision).catch(() => setVision([]));
    reloadSessions();
    const id = setInterval(reloadSessions, 10_000);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, slugMissing]);

  // T-0104: enrich task.session.activity by joining with the sessions
  // map. We do NOT mutate the original task; useMemo builds a new
  // array so React picks up the activity flip on the next poll.
  const enrichedTasks = useMemo<Task[] | null>(() => {
    if (tasks === null) return null;
    return tasks.map((t) => {
      if (!t.session) return t;
      const live = sessionsBySid[t.session.sid];
      if (!live) return t;
      return {
        ...t,
        // T-0404: also stamp the live awaiting_input so the card can badge a
        // task whose bound process is blocked on the operator.
        // T-0437: stamp the live pin signal so the card can show a 📌 marker.
        session: {
          ...t.session,
          activity: sessionActivity(live),
          awaiting_input: live.awaiting_input,
          pinned: live.pinned,
          pinned_by: live.pinned_by,
        },
      };
    });
  }, [tasks, sessionsBySid]);

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
    const init = taskInitiativeKey(t);
    if (filterInit === UNATTACHED) return !init;
    if (filterInit === ACTIVE_ONLY) return Boolean(init) && activeInitiativeKeys.has(init);
    return init === filterInit;
  }

  // Tasks keyed by initiative basename (or UNATTACHED).
  // T-0104: read from enrichedTasks (with activity stamped) so cards
  // display the canonical session label.
  const tasksByInit = useMemo<Record<string, Task[]>>(() => {
    const out: Record<string, Task[]> = { [UNATTACHED]: [] };
    for (const m of initiativeMeta) out[m.key] = [];
    for (const t of enrichedTasks ?? []) {
      // T-0295 (c): shared with the Vision page's bound-tasks list, so the
      // expanded initiative row shows exactly this lane's contents.
      const init = taskInitiativeKey(t);
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
  }, [enrichedTasks, initiativeMeta]);

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

  // Ungrouped — current 5-column behavior. T-0104: enrichedTasks
  // so the activity-flip propagates without a board reload.
  const grouped = COLUMNS.reduce<Record<string, Task[]>>((acc, c) => ({ ...acc, [c]: [] }), {});
  const ungroupedTasks = (enrichedTasks ?? []).filter(passesFilter);
  for (const t of ungroupedTasks) {
    if (COLUMNS.includes(t.status as typeof COLUMNS[number])) {
      grouped[t.status].push(t);
    }
  }
  // T-0512 (M9): subtask nesting for the ungrouped board.
  const ungroupedNesting = buildNesting(ungroupedTasks);

  // T-0272: group-by-initiative previously rendered every lane (incl. all
  // the `done` and empty ones) as a full expanded 6-col board — a wall of
  // mostly-empty columns. Done and empty lanes now default to collapsed so
  // the few active lanes are visible without heavy scrolling. An explicit
  // per-lane toggle (persisted to localStorage) always overrides the default.
  function laneCollapsed(lane: InitiativeMeta): boolean {
    const explicit = collapsedLanes[lane.key];
    if (explicit !== undefined) return explicit;
    const count = (tasksByInit[lane.key] ?? []).length;
    return lane.status === "done" || count === 0;
  }

  // T-0096: the card chip is redundant whenever the board view already
  // disambiguates initiative. That's true when grouping by initiative
  // (each lane = one initiative) OR when filtering to a specific
  // initiative basename / UNATTACHED (every visible card shares the
  // same binding). ACTIVE_ONLY still mixes initiatives — keep the chip.
  const hideInitiativeChip =
    groupBy === "initiative" ||
    (filterInit !== "" && filterInit !== ACTIVE_ONLY);

  if (slugMissing === true) {
    return (
      <div className="container py-4">
        <div className="alert alert-warning">
          Project <code>{slug}</code> not found.
        </div>
        <Link to="/" style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}>
          ← back to all projects
        </Link>
      </div>
    );
  }

  if (slugMissing === null) {
    return (
      <div className="container py-4"><div className="mc-loading">Loading</div></div>
    );
  }

  return (
    <div className="container py-4">
      <ProjectOnboarding slug={slug} sessions={Object.values(sessionsBySid)} />
      <div className="d-flex justify-content-between align-items-center mb-3">
        {/* T-0366 #3: title matches the "Board" nav label (was "Backlog"). */}
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>Board</h2>
      </div>

      {/* T-0674 (D-0057 §4/§8): the board is pure read-first now — task
          create/edit/comment/delete and the project-level autopilot trigger
          are cut. Task mutation happens via the TG dialog (R5). */}
      <PageHelp>
        Open work for this project across the canonical lifecycle — <strong>Backlog →
        In progress → Validating → Done</strong> (the overview strip), refined into six
        internal statuses in the columns. <strong>Click</strong> a card to open its full
        detail. Creating, editing, or commenting on a task happens via the Telegram
        dialog — this board is read-only.
      </PageHelp>

      {/* T-0230: resource telemetry panel relocated to the Agent sessions page. */}

      {/* T-0593 (T-0588a): top-level observability — the retired
          /p/:slug/transparency page dissolved into the home. Quota strip +
          live-session count always visible; who-does-what / operator
          state-doc / scheduler expandable below. */}
      <ObservabilityPanel slug={slug} />

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
          onboardingAnchor="board-group-by"
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
          <Select
            value={filterInit}
            onChange={setFilterInit}
            style={{ minWidth: "12rem", fontSize: "0.75rem" }}
            ariaLabel="filter by initiative"
            options={[
              { value: "", label: "all initiatives" },
              { value: ACTIVE_ONLY, label: "(active initiatives)" },
              ...initiativeMeta.map((m) => ({
                value: m.key,
                label: m.title,
                hint: m.status,
              })),
              { value: UNATTACHED, label: "(unattached)" },
            ]}
          />
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
                canonical={CANONICAL_LABELS[CANONICAL_STATE[c]]}
                tasks={grouped[c]}
                slug={slug}
                // T-0674: board is pure read-first — no menu/drag mutation.
                // TaskCard renders no kebab and isn't draggable, so these
                // handlers are unreachable; kept as no-ops only to satisfy
                // BoardColumn's prop contract.
                onMenuAction={() => {}}
                onMove={() => {}}
                onReorder={() => {}}
                railMode={
                  RAIL_STATUSES.has(c)
                    ? (expandedRail === c ? "expanded" : "collapsed")
                    : null
                }
                onToggleRail={RAIL_STATUSES.has(c) ? () => toggleRail(c) : undefined}
                hideInitiative={hideInitiativeChip}
                nestedChildIds={ungroupedNesting.nestedChildIds}
                subtasksByParent={ungroupedNesting.subtasksByParent}
              />
            ))}
          </div>
        ) : (
          <ListBoard
            tasks={ungroupedTasks}
            slug={slug}
            hideInitiative={hideInitiativeChip}
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
              collapsed={laneCollapsed(lane)}
              onToggleCollapsed={() => toggleLane(lane.key, laneCollapsed(lane))}
              expandedRail={expandedRail}
              onToggleRail={toggleRail}
            />
          ))}
        </div>
      )}
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
  // T-0053: optional data-onboarding-anchor on the inner btn-group so a
  // coachmark can anchor on the toggle itself (not the label) — the toggle
  // is what the user clicks, so the spotlight rect should hug the buttons.
  onboardingAnchor?: string;
}

function SegmentedToggle({
  label,
  value,
  onChange,
  options,
  onboardingAnchor,
}: SegmentedToggleProps) {
  return (
    <div className="d-flex align-items-center gap-2">
      <span style={{ fontFamily: "var(--mc-mono)", color: "var(--mc-text-dim)" }}>
        {label}:
      </span>
      <div
        className="btn-group btn-group-sm"
        role="group"
        data-onboarding-anchor={onboardingAnchor}
      >
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
  // T-0512 (M9): subtask nesting within this lane's task set.
  const nesting = buildNesting(tasks);

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
          title="planned / open / reopened / in-progress / to-test / closed"
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
              canonical={CANONICAL_LABELS[CANONICAL_STATE[c]]}
              tasks={grouped[c]}
              slug={slug}
              // T-0674: pure read-first — see the ungrouped board's comment.
              onMenuAction={() => {}}
              onMove={() => {}}
              onReorder={() => {}}
              railMode={
                RAIL_STATUSES.has(c)
                  ? (expandedRail === c ? "expanded" : "collapsed")
                  : null
              }
              onToggleRail={RAIL_STATUSES.has(c) ? () => onToggleRail(c) : undefined}
              hideInitiative
              nestedChildIds={nesting.nestedChildIds}
              subtasksByParent={nesting.subtasksByParent}
            />
          ))}
        </div>
      ) : (
        <ListBoard
          tasks={tasks}
          slug={slug}
          hideInitiative
        />
      ))}
    </div>
  );
}

interface ListBoardProps {
  tasks: Task[];
  slug: string;
  // T-0096: forwarded to every TaskCard rendered by the list.
  hideInitiative?: boolean;
}

/**
 * Compact list view: one section per status, tasks rendered as cards but
 * stacked into a single column. Pure read (T-0674) — no drag, no menu.
 */
function ListBoard({ tasks, slug, hideInitiative = false }: ListBoardProps) {
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
            <div className="mc-board-canonical-kicker">{CANONICAL_LABELS[CANONICAL_STATE[c]]}</div>
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
                    hideInitiative={hideInitiative}
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

// ===========================================================================
// T-0053 — project-section onboarding (Chapter I §12, 4 beats)
// ===========================================================================

interface ProjectOnboardingProps {
  slug: string;
  // Live sessions from the Project page's poll. Used to surface the
  // per-project operator session (window=operator) for beat 3. Empty
  // array on a cold mount — the coachmark renders a fallback in that
  // case (sessions tab pointer) instead of waiting.
  sessions: SessionRow[];
}

function ProjectOnboarding({ slug, sessions }: ProjectOnboardingProps) {
  // Subscribe to onboarding state so the gate re-renders when seen_steps
  // loads (without this, hasSeen runs once against the empty default and
  // every beat stays dark).
  const state = useOnboardingState();

  // Gate: only fire for users who've created at least one project. The
  // marker is written by Picker on a successful createProject. Until it's
  // set, the proj.* tour stays dark — the server-view §9.x chain handles
  // a user who hasn't yet created.
  if (!state.loaded) return null;
  if (!hasSeen("proj.has_created_any")) return null;

  // Find the operator session (window=operator) for beat 3. There can be
  // only one live operator per project (the scaffold spawns it on create);
  // if it was suspended / never spawned, we fall back to a sessions-tab
  // pointer inside the coachmark body.
  const operator = sessions.find(
    (s) => s.window === "operator" && !s.archived,
  );

  // Sequence the beats — beat N is gated on the previous beat being seen
  // so the user walks through them in order instead of getting four
  // stacked backdrops at once. Skip-all collapses the whole chain via
  // the framework's `skipped` flag, which hasSeen already honours.
  const beat1Seen = hasSeen("proj.13_1.roles");
  const beat2Seen = hasSeen("proj.13_2.tasks_vs_initiatives");
  const beat3Seen = hasSeen("proj.13_3.operator_attach");

  return (
    <>
      <Coachmark
        stepId="proj.13_1.roles"
        title={STEP_13_1_TITLE}
        body={
          <div>
            <div style={{ marginBottom: "0.5rem" }}>
              Four roles drive a bot-squad project. You only chat with the
              first one directly — the rest are spawned and coordinated by
              the operator and TLs:
            </div>
            <ul style={{ paddingLeft: "1.1rem", margin: 0 }}>
              {PROJECT_ROLE_BLURBS.map((r) => (
                <li key={r.key} style={{ marginBottom: "0.35rem" }}>
                  <strong>{r.label}.</strong> {r.blurb}
                </li>
              ))}
            </ul>
          </div>
        }
      />

      {beat1Seen && (
      <Coachmark
        stepId="proj.13_2.tasks_vs_initiatives"
        title={STEP_13_2_TITLE}
        anchorSelector='[data-onboarding-anchor="board-group-by"]'
        placement="bottom"
        body={
          <>
            Two units of work live on this board:{" "}
            <strong>tickets</strong> (single shippable changes, prefixed{" "}
            <code>T-NNNN</code>) and <strong>initiatives</strong>{" "}
            (multi-week scopes that contain many tickets). Flip{" "}
            <em>Group by → initiative</em> to see how tickets fan out across
            initiatives, or filter to a single initiative to focus on one
            scope at a time.
          </>
        }
      />
      )}

      {beat2Seen && (
      <Coachmark
        stepId="proj.13_3.operator_attach"
        title={STEP_13_3_TITLE}
        anchorSelector='[data-onboarding-anchor="sessions-nav"]'
        placement="right"
        body={
          operator ? (
            <>
              <div style={{ marginBottom: "0.5rem" }}>
                Your project operator is running. Attach in a terminal and
                start talking — "what's the state of X", "let's start work
                on Y", whatever you've got:
              </div>
              <CopyableTmuxAttach
                session={slug}
                window="operator"
                size="md"
              />
              <div
                style={{
                  marginTop: "0.4rem",
                  fontSize: "0.7rem",
                  color: "var(--mc-text-dim)",
                }}
              >
                Operator SID:{" "}
                <code style={{ fontFamily: "var(--mc-mono)" }}>
                  {operator.sid}
                </code>
              </div>
            </>
          ) : (
            <>
              No operator session is currently running for this project. Open
              the{" "}
              <Link to={`/p/${slug}/sessions`}>sessions tab</Link> and spawn
              one (window name: <code>operator</code>) — that's your
              day-to-day chat surface for this project.
            </>
          )
        }
      />
      )}

      {beat3Seen && (
      <Coachmark
        stepId="proj.13_4.close"
        title={STEP_13_4_TITLE}
        final
        body={
          <>
            That's the whole tour. From here, hop into the operator's tmux
            pane and hand it the first real task — it'll spawn TLs and dev
            workers as needed. You can always re-find the operator on the{" "}
            <Link to={`/p/${slug}/sessions`}>sessions tab</Link>.
          </>
        }
      />
      )}
    </>
  );
}
