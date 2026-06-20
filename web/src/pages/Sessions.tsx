import { useEffect, useMemo, useRef, useState, useCallback, Fragment } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
// Link kept for session SID links and task links inside the table
import type { ReuseDecision, SessionRow, Task, VisionFile } from "../api";
// T-0280: the reuse-vs-spawn recommendation isn't on the shared ProjectApi
// surface yet (the mothership proxy mirror would need a matching method), so
// the reuse lookup uses the single-install singleton directly. On single
// install this is the same object useApiClient() returns; resume/spawn still
// go through the context client.
import { api as singleInstallApi } from "../api";
import { useApiClient } from "../apiContext";
import { CopyableTmuxAttach } from "../components/CopyableTmuxAttach";
import { Modal } from "../components/Modal";
import { RowActionsMenu, type RowAction } from "../components/RowActionsMenu";
import { AutopilotDialog, type AutopilotTarget } from "../components/AutopilotDialog";
import { Select } from "../components/Select";
import {
  operatorWindow,
  prodTeamleadWindow,
  qaWindow,
  sessionActivity,
  sessionLabel,
  sessionRole,
  sessionRoleLabel,
} from "../utils/sessionStatus";

import { PageHelp } from "../components/PageHelp";
import { PeerInbox } from "../components/PeerInbox";
import { TelemetryPanel } from "../components/TelemetryPanel";
import { uiSidFor } from "../peerInbox";
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

// T-0141: role badge keyed off the worker-derived `role` (falls back to the
// legacy task_id inference for a pre-T-0141 worker). Teamlead = green,
// operator = amber, dev = blue. T-0197: prod-teamlead = red (prod caution),
// qa = active/purple. `dim` mutes the badge for archived rows.
function RoleBadge({ row, dim = false }: { row: SessionRow; dim?: boolean }) {
  const role = sessionRole(row);
  // T-0220: the worker neutralized an elevated window-derived role on this
  // suspended row because its persisted cwd didn't match the project (the role
  // is already the safe "dev" fallback). Surface it subtly — a ⚠ glyph + title
  // — so the row stays auditable without any layout churn.
  const mismatch = row.role_cwd_mismatch === true;
  const title = mismatch
    ? "Role validated: window claimed an elevated role but the suspended session's cwd did not match this project — shown as dev"
    : undefined;
  const label = (
    <>
      {sessionRoleLabel(role)}
      {mismatch ? " ⚠" : ""}
    </>
  );
  if (dim)
    return (
      <span className="mc-badge mc-badge-dim" title={title}>
        {label}
      </span>
    );
  const cls =
    role === "teamlead"
      ? "mc-badge mc-badge-ok"
      : role === "operator"
        ? "mc-badge mc-badge-warn"
        : role === "prod-teamlead"
          ? "mc-badge mc-badge-danger"
          : role === "qa"
            ? "mc-badge mc-badge-active"
            : "mc-badge mc-badge-info";
  return (
    <span className={cls} title={title}>
      {label}
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
// Session tree (T-0040 / T-0128 / T-0222)
//
// A dev row is one carrying a primary task_id. Children are visually indented
// under their parent session.
//
// T-0128: the worker persists `parent_sid` (the SID that requested the spawn)
// at spawn time, so we PREFER it — a row with a resolvable `parent_sid` nests
// under that exact session. For dev rows we fall back to the legacy heuristic
// (dev.task_id → task.initiative → TL bound to that initiative) when the field
// is absent or its parent isn't visible in this slice.
//
// T-0222: nesting is no longer dev-only. ANY task-less row (qa, prod-TL, an
// ad-hoc child) whose persisted `parent_sid` resolves to a visible parent now
// nests under that spawner too — previously only task-bearing dev rows nested
// and every task-less session flattened to the root even when it carried a
// genuine spawn-time parent. Genuine roots (the operator, an unparented TL, or
// any row whose `parent_sid` is blank / self / not visible in this slice) keep
// no parent → render at level 0. Because parents may themselves be children,
// the tree can now be deeper than two levels (operator → TL → dev): we build a
// parent map over all rows and emit it with a recursive, cycle-safe DFS so any
// row unreachable from a root still falls back to the root rather than
// vanishing.
//
// Exported as a pure function (taking the task→initiative map) so it is
// unit-testable, mirroring computeTlBindings in Vision.tsx.
// ---------------------------------------------------------------------------
export function isDevRow(s: SessionRow): boolean {
  return !!(s.task_id && s.task_id !== "" && s.task_id !== "~");
}

// ---------------------------------------------------------------------------
// T-0232 (Pillar A) — live-only sessions view.
//
// The board now shows only sessions that are alive in tmux. The stakeholder's
// core de-clutter pain was the long tail of dead rows (3 live vs 175 suspended).
// Suspended/archived rows are retained in the registry but dropped from the
// default view (revealable on demand via the "show suspended" toggle).
//
// Liveness precedence:
//   1. archived            → never live (it lives in the Archived disclosure).
//   2. paused              → live: a Ctrl-C interrupt whose pane is still open
//                            and Resume-able from the row menu. Team-1's `live`
//                            flag is running/idle only, so we add paused here
//                            rather than strand the Resume action.
//   3. explicit `live` flag → preferred (Team-1 stamps it = activity ∈ {running,idle}).
//   4. fallback            → the activity probe (pre-T-0232 worker without the flag):
//                            running/idle are live, suspended is dead.
// Following `sessionActivity` (not the raw md status) means a zombie row
// (status=active, activity=suspended — T-0104) correctly drops out.
// ---------------------------------------------------------------------------
export function isLiveSession(s: SessionRow): boolean {
  if (s.archived) return false;
  if (sessionActivity(s) === "paused") return true;
  if (typeof s.live === "boolean") return s.live;
  const a = sessionActivity(s);
  return a === "running" || a === "idle";
}

export function buildSessionTree(
  rows: SessionRow[],
  taskInitiative: Map<string, string>,
): { row: SessionRow; level: number }[] {
  const tlByInitiative = new Map<string, SessionRow>();
  const nonDevSids = new Set<string>();
  const visibleSids = new Set<string>();
  for (const s of rows) {
    visibleSids.add(s.sid);
    if (isDevRow(s)) continue;
    nonDevSids.add(s.sid);
    const inits = [(s.initiative ?? "").trim(), ...(s.extra_initiatives ?? [])]
      .filter((i) => i && i !== "~");
    for (const init of inits) {
      if (!tlByInitiative.has(init)) tlByInitiative.set(init, s);
    }
  }

  // Resolve each row's parent SID (undefined = genuine root).
  const parentOf = new Map<string, string | undefined>();
  for (const s of rows) {
    const ps = (s.parent_sid ?? "").trim();
    // A genuine, persisted spawn-time parent that is visible and not self.
    const resolvedParent =
      ps && ps !== "~" && ps !== s.sid && visibleSids.has(ps) ? ps : undefined;
    if (isDevRow(s)) {
      // T-0128: prefer the persisted parent; else the task→initiative→TL
      // heuristic for legacy sessions lacking the field.
      if (resolvedParent) {
        parentOf.set(s.sid, resolvedParent);
      } else {
        const init = taskInitiative.get(s.task_id!) ?? "";
        const tl = init ? tlByInitiative.get(init) : undefined;
        parentOf.set(s.sid, tl && tl.sid !== s.sid ? tl.sid : undefined);
      }
    } else {
      // T-0222: a task-less row nests under its persisted spawner when that
      // parent is visible; genuine roots keep parent undefined.
      parentOf.set(s.sid, resolvedParent);
    }
  }

  // Bucket children under each parent, preserving original row order.
  const childrenOf = new Map<string, SessionRow[]>();
  const roots: SessionRow[] = [];
  for (const s of rows) {
    const p = parentOf.get(s.sid);
    if (p === undefined) {
      roots.push(s);
    } else {
      const list = childrenOf.get(p) ?? [];
      list.push(s);
      childrenOf.set(p, list);
    }
  }

  // T-0231: pin the operator(s) to the top, then team-leads, then everyone
  // else — so the process-hierarchy view always reads operator → TL →
  // teammates regardless of the order the API returned rows in. Only the
  // ROOT order is normalised; children keep their original (spawn) order, and
  // the sort is stable so same-rank roots stay in their incoming order.
  const rootRank = (s: SessionRow): number => {
    const role = sessionRole(s);
    if (role === "operator") return 0;
    if (role === "teamlead" || role === "prod-teamlead") return 1;
    return 2;
  };
  roots.sort((a, b) => rootRank(a) - rootRank(b));

  const out: { row: SessionRow; level: number }[] = [];
  const emitted = new Set<string>();
  const emit = (s: SessionRow, level: number) => {
    if (emitted.has(s.sid)) return; // cycle / dupe guard
    emitted.add(s.sid);
    out.push({ row: s, level });
    for (const child of childrenOf.get(s.sid) ?? []) emit(child, level + 1);
  };
  for (const r of roots) emit(r, 0);
  // Cycle backstop: any row not reachable from a root (parent chain loops)
  // renders at root so it can never silently disappear from the table.
  for (const s of rows) if (!emitted.has(s.sid)) emit(s, 0);
  return out;
}

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
  const [newRole, setNewRole] = useState<
    "operator" | "teamlead" | "dev" | "prod-teamlead" | "qa" | null
  >(null);
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
  // T-0280: reuse-before-spawn — the worker's reuse-vs-spawn recommendation
  // for the task selected in the dev form. Drives the candidate strip.
  const [reuse, setReuse] = useState<ReuseDecision | null>(null);
  const [reuseLoading, setReuseLoading] = useState(false);
  const [reuseError, setReuseError] = useState<string | null>(null);
  const [resumingSid, setResumingSid] = useState<string | null>(null);

  // T-0006: post-spawn toast surfacing the copyable tmux attach for the
  // session that just appeared. Computed by diffing the SID set before and
  // after the spawn call so we don't need a return-value contract change on
  // api.spawnSession.
  const [spawnNotice, setSpawnNotice] = useState<{ sid: string; window: string; tmuxSession: string } | null>(null);

  // Send-message modal (cross-session bus)
  const [sendOpen, setSendOpen] = useState(false);
  const [sendTarget, setSendTarget] = useState<string>("");
  const [sendText, setSendText] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const [sendInfo, setSendInfo] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [meUsername, setMeUsername] = useState<string>("stakeholder");

  // T-0153: autopilot dialog (kebab on a team lane or a single session row).
  const [autopilotOpen, setAutopilotOpen] = useState(false);
  const [autopilotTarget, setAutopilotTarget] = useState<AutopilotTarget | null>(null);

  // Row expansion (click-through cwd / metadata detail) and archived
  // section toggle.
  const [expandedSids, setExpandedSids] = useState<Set<string>>(new Set());
  const [showArchived, setShowArchived] = useState(false);
  // T-0232: the view is LIVE-only by default; this toggle reveals the
  // suspended (non-archived) rows on demand without re-cluttering the board.
  const [showSuspended, setShowSuspended] = useState(false);

  // T-0099: deep-link target — when the URL carries ?sid=S-..., scroll
  // that row into view, expand its detail row, and flash a transient
  // highlight that fades after 2s. `flashedSidRef` guards against the
  // 10s poll re-firing the flash on every refresh.
  const [flashSid, setFlashSid] = useState<string | null>(null);
  const flashedSidRef = useRef<string | null>(null);

  // T-0039 follow-up: group sessions by initiative on this page too.
  // T-0141: tmux grouping WAS the default (stakeholder's mental model, notes
  // 5 + 13) — the page grouped by tmux session with the TL highlighted.
  // T-0157: "user" groups by linux_user → then tmux session, for multi-user
  // projects where several Linux users work in one project from their own tmux.
  // T-0231 (paradigm reframe, Pillar A): the DEFAULT is now "none" — the
  // process-hierarchy view (operator → team-leads → their teammates, nested
  // via parent_sid by buildSessionTree). One glance = who's running / idle /
  // what each is doing, like a Task Manager. tmux/user/initiative remain
  // available as explicit group-by toggles.
  type SessGroupBy = "tmux" | "user" | "none" | "initiative";
  const SESS_UNATTACHED = "__unattached__";
  const TMUX_NONE = "(no tmux session)";
  const USER_NONE = "(unknown user)"; // T-0157: rows with no parseable linux_user
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
  // `effective` lets a caller flip a lane whose displayed state is a computed
  // default (not yet an explicit entry in the map) — e.g. the tmux zero-alive
  // lanes that render collapsed by default. Without it, the first click on a
  // default-collapsed lane would write `true` and appear to do nothing.
  function toggleSessLane(key: string, effective?: boolean) {
    setCollapsedSessLanes((prev) => ({
      ...prev,
      [key]: effective === undefined ? !prev[key] : !effective,
    }));
  }
  // T-0141: a tmux lane with no live (active/paused) session is historical
  // noise (mostly legacy suspended mds without a tmux_session field, which
  // pile into "(no tmux session)"). Default it collapsed so the page opens
  // showing the live tmux sessions — "the actual recent tmux tabs" the
  // stakeholder expects (note 3) — while an explicit user toggle still wins.
  function tmuxLaneCollapsed(key: string, rows: SessionRow[]): boolean {
    if (key in collapsedSessLanes) return Boolean(collapsedSessLanes[key]);
    return !rows.some((r) => r.status === "active" || r.status === "paused");
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

  // T-0040: task→initiative map for the dev-under-TL tree heuristic.
  // Backlog is also loaded inside openModal but cached here for the always-on
  // tree render. A 30s refresh is enough — task↔initiative bindings change
  // far less often than session activity.
  const [taskBacklog, setTaskBacklog] = useState<Task[]>([]);
  useEffect(() => {
    let alive = true;
    const refresh = () => {
      api.backlog(slug)
        .then((rows) => { if (alive) setTaskBacklog(rows); })
        .catch(() => { if (alive) setTaskBacklog([]); });
    };
    refresh();
    const id = setInterval(refresh, 30_000);
    return () => { alive = false; clearInterval(id); };
  }, [slug]);
  const taskInitiative = useMemo(() => {
    const m = new Map<string, string>();
    for (const t of taskBacklog) {
      const init = (t.initiative ?? "").trim();
      if (init) m.set(t.id, init);
    }
    return m;
  }, [taskBacklog]);

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

  // T-0280: fetch the reuse-vs-spawn recommendation for the dev form's
  // selected task so we can offer "resume before spawn". Keyed on the task
  // (the worker derives its initiative); cleared when no task is picked.
  useEffect(() => {
    if (!modalOpen || newRole !== "dev" || !newTaskId) {
      setReuse(null);
      setReuseError(null);
      setReuseLoading(false);
      return;
    }
    let cancelled = false;
    setReuseLoading(true);
    setReuseError(null);
    singleInstallApi
      .reuseCandidates(slug, newTaskId)
      .then((r) => {
        if (!cancelled) setReuse(r);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setReuse(null);
          setReuseError(String(e));
        }
      })
      .finally(() => {
        if (!cancelled) setReuseLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [modalOpen, newRole, newTaskId, slug]);

  // T-0280: resume an existing live pro instead of spawning a fresh one.
  // Calls the existing resume endpoint, then closes the modal + reloads.
  async function handleResumeFromModal(sid: string) {
    setReuseError(null);
    setResumingSid(sid);
    try {
      await api.resumeSession(slug, sid);
      setModalOpen(false);
      load();
    } catch (e: unknown) {
      setReuseError(String(e));
    } finally {
      setResumingSid(null);
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

  // T-0153: open the autopilot dialog for a target (session / team / project).
  function openAutopilot(target: AutopilotTarget) {
    setAutopilotTarget(target);
    setAutopilotOpen(true);
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
      const fromSid = uiSidFor(meUsername);
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
    role?: "operator" | "teamlead" | "dev" | "prod-teamlead" | "qa";
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
    if (
      roleParam !== "dev" &&
      roleParam !== "teamlead" &&
      roleParam !== "operator" &&
      roleParam !== "prod-teamlead" &&
      roleParam !== "qa"
    )
      return;
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

  // Active TLs: role === "teamlead" and live. T-0141: keys off the
  // authoritative role instead of the old "no task_id" inference, so the
  // dev-spawn target picker no longer offers every task-less session.
  const activeTeamleads: SessionRow[] = (sessions ?? []).filter(
    (s) => s.status === "active" && sessionRole(s) === "teamlead" && !s.archived,
  );

  // Split visible vs archived for the two-section layout.
  const visibleSessions: SessionRow[] = (sessions ?? []).filter((s) => !s.archived);
  const archivedSessions: SessionRow[] = (sessions ?? []).filter((s) => !!s.archived);

  // T-0232 (Pillar A): the main board shows LIVE-only rows (alive in tmux —
  // running/idle/paused). Suspended (non-archived) rows are retained in the
  // registry but dropped from the default view; the `showSuspended` toggle
  // reveals them on demand. `liveSessions` is what the table renders unless
  // the toggle is on, in which case it falls back to all non-archived rows.
  const liveSessions: SessionRow[] = visibleSessions.filter(isLiveSession);
  const hiddenSuspendedCount = visibleSessions.length - liveSessions.length;
  const boardSessions: SessionRow[] = showSuspended ? visibleSessions : liveSessions;

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
          </dl>
        </td>
      </tr>
    );
  }

  // T-0141: assemble the action set for a row's overflow (kebab) menu so the
  // row stays short. Mirrors the prior inline button logic exactly.
  function rowActions(s: SessionRow): RowAction[] {
    const isSuspended = s.status === "suspended";
    const acts: RowAction[] = [];
    if (s.status === "active") {
      acts.push({ label: "Pause", onClick: () => handlePause(s.sid), variant: "warning" });
      acts.push({ label: "Suspend", onClick: () => handleSuspend(s.sid) });
    } else if (s.status === "paused") {
      acts.push({ label: "Resume", onClick: () => handleResume(s.sid), variant: "success" });
      acts.push({ label: "Suspend", onClick: () => handleSuspend(s.sid) });
    } else if (s.status === "suspended") {
      acts.push({ label: "Resurrect", onClick: () => handleResume(s.sid), variant: "success" });
    }
    acts.push({ label: "Send msg", onClick: () => openSendModal(s.sid) });
    // T-0153: per-session autopilot — hand this session a time-boxed brief.
    if (!isSuspended) {
      acts.push({
        label: "Autopilot…",
        onClick: () => openAutopilot({ kind: "session", ref: s.sid, label: s.sid }),
      });
    }
    acts.push({
      label: "Archive",
      onClick: () => handleArchive(s.sid),
      disabled: !isSuspended,
      title: isSuspended ? "Archive this suspended session" : "Suspend the session first",
    });
    return acts;
  }

  function renderSessionRow(s: SessionRow, level = 0) {
    const isOpen = expandedSids.has(s.sid);
    const isFlashing = flashSid === s.sid;
    const isTL = sessionRole(s) === "teamlead";
    return (
      <Fragment key={s.sid}>
        <tr
          id={`sess-row-${s.sid}`}
          className={isFlashing ? "mc-row-flash" : undefined}
          style={{
            cursor: "pointer",
            // T-0141: highlight the TL row within its tmux group.
            ...(isTL && level === 0
              ? {
                  borderLeft: "3px solid var(--mc-accent-success, #4ade80)",
                  background: "rgba(74, 222, 128, 0.05)",
                }
              : {}),
          }}
          onClick={() => toggleRow(s.sid)}
        >
          {/* Expand chevron (T-0040: paddingLeft scales with tree depth) */}
          <td
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.75rem",
              color: "var(--mc-text-dim)",
              width: `${1.5 + level * 1.25}rem`,
              paddingLeft: `${0.5 + level * 1.25}rem`,
              whiteSpace: "nowrap",
            }}
          >
            {isOpen ? "▾" : "▸"}
          </td>

          {/* SID — prefix with a faint tree branch glyph when nested.
              T-0141: one-line + ellipsis so a long SID can't wrap the row
              past 48px (the full SID stays in the expandable detail row). */}
          <td
            onClick={(e) => e.stopPropagation()}
            title={s.sid}
            style={{
              maxWidth: "16rem",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {level > 0 && (
              <span
                aria-hidden
                style={{
                  color: "var(--mc-text-dim)",
                  fontFamily: "var(--mc-mono)",
                  marginRight: "0.3rem",
                  fontSize: "0.78rem",
                }}
              >
                └
              </span>
            )}
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

          {/* Attach — T-0141: the copyable command targets the tmux SESSION
              (`tmux a -t <tmux_session>:<window>`), not the SID. Passing the
              SID built `tmux a -t S-…:<window>` which never attached. */}
          <td onClick={(e) => e.stopPropagation()} style={{ width: "1px", whiteSpace: "nowrap" }}>
            <CopyableTmuxAttach session={s.tmux_session || slug} window={s.window} iconOnly />
          </td>

          {/* Role — T-0141: worker-derived, no longer "task-less ⟹ TL". */}
          <td>
            <RoleBadge row={s} />
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

          {/* Actions — T-0141: collapsed behind a kebab so the row stays
              ≤48px (stakeholder note 10). */}
          <td onClick={(e) => e.stopPropagation()} style={{ width: "1px", whiteSpace: "nowrap" }}>
            <RowActionsMenu actions={rowActions(s)} ariaLabel={`Actions for ${s.sid}`} />
          </td>
        </tr>
        {isOpen && renderDetailRow(s, 10)}
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
          <td
            onClick={(e) => e.stopPropagation()}
            title={s.sid}
            style={{
              maxWidth: "16rem",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
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
            <CopyableTmuxAttach session={s.tmux_session || slug} window={s.window} iconOnly />
          </td>
          <td>
            <RoleBadge row={s} dim />
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
          <td onClick={(e) => e.stopPropagation()} style={{ width: "1px", whiteSpace: "nowrap" }}>
            <RowActionsMenu
              ariaLabel={`Actions for ${s.sid}`}
              actions={[
                { label: "Resurrect", onClick: () => handleResurrectFromArchive(s.sid), variant: "success" },
                {
                  label: "Unarchive",
                  onClick: () =>
                    api.unarchiveSession(slug, s.sid).then(load).catch((e) => setActionError(String(e))),
                },
              ]}
            />
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
          setSpawnNotice({ sid: fresh.sid, window: fresh.window, tmuxSession: fresh.tmux_session || slug });
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

  // T-0041: spawn an operator session. The window is normalised so it always
  // carries the operator marker (…-operator) and therefore resolves to
  // operator.md via the worker's _derive_role + the SessionStart hook — never a
  // silent dev. Operators are not bound to a task or initiative.
  async function handleSpawnOperator() {
    const window = operatorWindow(newWindow);
    setSpawning(true);
    setModalError(null);
    try {
      const before = new Set((sessions ?? []).map((s) => s.sid));
      await api.spawnSession(slug, window, newPrompt.trim() || undefined);
      try {
        const after = await api.sessions(slug);
        setSessions(after);
        setError(null);
        const fresh = after.find((s) => !before.has(s.sid) && !s.archived);
        if (fresh) {
          setSpawnNotice({ sid: fresh.sid, window: fresh.window, tmuxSession: fresh.tmux_session || slug });
        }
      } catch {
        load();
      }
      setModalOpen(false);
    } catch (e: unknown) {
      setModalError(String(e));
    } finally {
      setSpawning(false);
    }
  }

  // T-0197: spawn a prod-teamlead session. Like the operator, the window is
  // normalised so it always carries the -prod-tl marker and therefore resolves
  // to prod-teamlead.md via _derive_role + the SessionStart hook — never a
  // silent dev. Prod-TLs are not bound to a task or initiative (they live in
  // the prod clone and watch the deploy queue).
  async function handleSpawnProdTeamlead() {
    const window = prodTeamleadWindow(newWindow);
    setSpawning(true);
    setModalError(null);
    try {
      const before = new Set((sessions ?? []).map((s) => s.sid));
      await api.spawnSession(slug, window, newPrompt.trim() || undefined);
      try {
        const after = await api.sessions(slug);
        setSessions(after);
        setError(null);
        const fresh = after.find((s) => !before.has(s.sid) && !s.archived);
        if (fresh) {
          setSpawnNotice({ sid: fresh.sid, window: fresh.window, tmuxSession: fresh.tmux_session || slug });
        }
      } catch {
        load();
      }
      setModalOpen(false);
    } catch (e: unknown) {
      setModalError(String(e));
    } finally {
      setSpawning(false);
    }
  }

  // T-0197: spawn a QA session. The window is normalised to carry the -qa
  // marker so it resolves to qa.md. QA lives in the dev clone and picks up
  // totest tickets; it is not bound to a single task at spawn time.
  async function handleSpawnQa() {
    const window = qaWindow(newWindow);
    setSpawning(true);
    setModalError(null);
    try {
      const before = new Set((sessions ?? []).map((s) => s.sid));
      await api.spawnSession(slug, window, newPrompt.trim() || undefined);
      try {
        const after = await api.sessions(slug);
        setSessions(after);
        setError(null);
        const fresh = after.find((s) => !before.has(s.sid) && !s.archived);
        if (fresh) {
          setSpawnNotice({ sid: fresh.sid, window: fresh.window, tmuxSession: fresh.tmux_session || slug });
        }
      } catch {
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

  // ---- T-0157: linux-user marking + grouping (multi-user projects) ----
  // The owning linux user is the explicit `linux_user` field, falling back to
  // the SID prefix (S-<user>-…) for pre-T-0157 rows.
  function sessionUserKey(s: SessionRow): string {
    const u = (s.linux_user ?? "").trim();
    if (u && u !== "~") return u;
    const sid = s.sid ?? "";
    if (sid.startsWith("S-")) {
      const parts = sid.split("-");
      if (parts.length >= 2 && parts[1]) return parts[1];
    }
    return USER_NONE;
  }
  // Distinct linux users present in a set of rows — used for the per-lane mark.
  function laneUsers(rows: SessionRow[]): string[] {
    const seen = new Set<string>();
    for (const r of rows) {
      const u = sessionUserKey(r);
      if (u !== USER_NONE) seen.add(u);
    }
    return Array.from(seen).sort();
  }
  // Group rows by linux user. Groups with a live session sort first, then
  // alphabetical; the "(unknown user)" bucket is last. Mirrors groupSessionsByTmux.
  function groupSessionsByUser(rows: SessionRow[]): { key: string; rows: SessionRow[] }[] {
    const map = new Map<string, SessionRow[]>();
    for (const s of rows) {
      const k = sessionUserKey(s);
      const list = map.get(k) ?? [];
      list.push(s);
      map.set(k, list);
    }
    const groups = Array.from(map.entries()).map(([key, gr]) => ({ key, rows: gr }));
    groups.sort((a, b) => {
      if (a.key === USER_NONE) return 1;
      if (b.key === USER_NONE) return -1;
      const al = a.rows.some(isAliveRow) ? 0 : 1;
      const bl = b.rows.some(isAliveRow) ? 0 : 1;
      if (al !== bl) return al - bl;
      return a.key.localeCompare(b.key);
    });
    return groups;
  }
  function renderUserHeaderRow(key: string, rows: SessionRow[], colSpan: number) {
    const alive = rows.filter(isAliveRow).length;
    const suspended = rows.filter((r) => r.status === "suspended").length;
    const isNone = key === USER_NONE;
    return (
      <tr
        key={`user-section-${key}`}
        style={{ background: "var(--mc-surface-deep)", borderTop: "2px solid var(--mc-border)" }}
      >
        <td colSpan={colSpan} style={{ padding: "0.5rem 0.75rem", fontFamily: "var(--mc-mono)", fontSize: "0.8rem" }}>
          <div className="d-flex align-items-center gap-2 flex-wrap">
            <span
              className="badge"
              style={{
                background: isNone ? "var(--mc-surface-raised)" : "var(--mc-accent, #3b82f6)",
                color: isNone ? "var(--mc-text-dim)" : "#fff",
                fontFamily: "var(--mc-mono)",
                fontSize: "0.72rem",
                fontWeight: 700,
              }}
            >
              {isNone ? "(unknown user)" : `👤 ${key}`}
            </span>
            <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.65rem", color: "var(--mc-text-dim)" }}>
              {alive} alive · {suspended} suspended
            </span>
          </div>
        </td>
      </tr>
    );
  }

  // ---- T-0141: tmux-session grouping (the default view) ----
  function sessionTmuxKey(s: SessionRow): string {
    const t = (s.tmux_session ?? "").trim();
    return t && t !== "~" ? t : TMUX_NONE;
  }
  function isAliveRow(s: SessionRow): boolean {
    return s.status === "active" || s.status === "paused";
  }
  // Group rows by tmux session. Groups with a live (active/paused) session
  // sort first, then alphabetical; the "(no tmux session)" bucket is last.
  function groupSessionsByTmux(rows: SessionRow[]): { key: string; rows: SessionRow[] }[] {
    const map = new Map<string, SessionRow[]>();
    for (const s of rows) {
      const k = sessionTmuxKey(s);
      const list = map.get(k) ?? [];
      list.push(s);
      map.set(k, list);
    }
    const groups = Array.from(map.entries()).map(([key, gr]) => ({ key, rows: gr }));
    groups.sort((a, b) => {
      if (a.key === TMUX_NONE) return 1;
      if (b.key === TMUX_NONE) return -1;
      const al = a.rows.some(isAliveRow) ? 0 : 1;
      const bl = b.rows.some(isAliveRow) ? 0 : 1;
      if (al !== bl) return al - bl;
      return a.key.localeCompare(b.key);
    });
    return groups;
  }
  // Within a tmux group: operator + TL(s) at root (TL highlighted in the row
  // renderer), every other session nested one level under — matching the
  // stakeholder's mental model of "a team with the teamlead, N alive, M
  // suspended" (note 13). When the group has no TL, all rows sit at root.
  function buildTmuxGroupTree(rows: SessionRow[]): { row: SessionRow; level: number }[] {
    const ops = rows.filter((r) => sessionRole(r) === "operator");
    const leads = rows.filter((r) => sessionRole(r) === "teamlead");
    const rest = rows.filter((r) => {
      const role = sessionRole(r);
      return role !== "operator" && role !== "teamlead";
    });
    const hasLead = leads.length > 0;
    const out: { row: SessionRow; level: number }[] = [];
    for (const o of ops) out.push({ row: o, level: 0 });
    for (const l of leads) out.push({ row: l, level: 0 });
    for (const d of rest) out.push({ row: d, level: hasLead ? 1 : 0 });
    return out;
  }
  function renderTmuxLaneHeaderRow(
    key: string,
    rows: SessionRow[],
    colSpan: number,
    collapsed: boolean,
  ) {
    const alive = rows.filter(isAliveRow).length;
    const suspended = rows.filter((r) => r.status === "suspended").length;
    const isNone = key === TMUX_NONE;
    return (
      <tr
        key={`tmux-lane-${key}`}
        style={{ background: "var(--mc-surface-deep)", borderTop: "1px solid var(--mc-border)" }}
      >
        <td colSpan={colSpan} style={{ padding: "0.45rem 0.75rem", fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}>
          <div className="d-flex align-items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => toggleSessLane(key, collapsed)}
              aria-expanded={!collapsed}
              aria-label={collapsed ? `Expand ${key}` : `Collapse ${key}`}
              title={collapsed ? "Expand tmux session" : "Collapse tmux session"}
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
            <span style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }} aria-hidden>
              ▣
            </span>
            <code
              style={{
                fontWeight: 700,
                color: isNone ? "var(--mc-text-dim)" : "var(--mc-text)",
                fontSize: "0.85rem",
                fontFamily: "var(--mc-mono)",
              }}
            >
              {isNone ? "(no tmux session)" : `tmux a -t ${key}`}
            </code>
            <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.65rem", color: "var(--mc-text-dim)", marginLeft: "0.5rem" }}>
              {alive} alive · {suspended} suspended
            </span>
            {/* T-0157: mark the owning linux user(s) on the lane so the default
                tmux view is user-marked too (multi-user projects). */}
            {laneUsers(rows).map((u) => (
              <span
                key={`lane-user-${key}-${u}`}
                className="badge"
                title={`linux user: ${u}`}
                style={{
                  background: "var(--mc-accent, #3b82f6)",
                  color: "#fff",
                  fontFamily: "var(--mc-mono)",
                  fontSize: "0.6rem",
                  fontWeight: 700,
                }}
              >
                👤 {u}
              </span>
            ))}
            {/* T-0153: team-level autopilot — kebab on the tmux-session lane. */}
            {!isNone && (
              <div className="ms-auto" onClick={(e) => e.stopPropagation()}>
                <RowActionsMenu
                  actions={[
                    {
                      label: "Autopilot…",
                      onClick: () => openAutopilot({ kind: "team", ref: key, label: `team ${key}` }),
                    },
                  ]}
                  ariaLabel={`Team actions for ${key}`}
                />
              </div>
            )}
          </div>
        </td>
      </tr>
    );
  }

  // T-0040/T-0128 tree — thin wrapper that binds the memoized task→initiative
  // map to the module-level pure `buildSessionTree` (exported for unit tests).
  function buildSessionTreeLocal(
    rows: SessionRow[],
  ): { row: SessionRow; level: number }[] {
    return buildSessionTree(rows, taskInitiative);
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
      {/* T-0127: in-UI peer-reply inbox (coexists with the TG mirror). */}
      <PeerInbox slug={slug} username={meUsername} />
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

      {/* T-0210/T-0230: resource telemetry — per-session context/memory/quota
          for live sessions. Relocated here from the board page (it's per-session
          resource data, a natural fit alongside the sessions list). */}
      <TelemetryPanel slug={slug} />

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
              session={spawnNotice.tmuxSession}
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
            {(["tmux", "user", "none", "initiative"] as const).map((v) => (
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
          {/* T-0232: the board is live-only by default. When suspended
              (non-archived) rows are being hidden, surface a subtle count +
              toggle so they stay reachable without re-cluttering the view. */}
          {(hiddenSuspendedCount > 0 || showSuspended) && (
            <button
              type="button"
              className={`btn btn-sm ${showSuspended ? "btn-secondary" : "btn-outline-secondary"}`}
              style={{ fontSize: "0.72rem", padding: "0.15rem 0.55rem" }}
              onClick={() => setShowSuspended((v) => !v)}
              title={
                showSuspended
                  ? "Hide suspended sessions (show live only)"
                  : "Reveal suspended (non-archived) sessions"
              }
            >
              {showSuspended
                ? "hide suspended"
                : `${hiddenSuspendedCount} suspended hidden — show`}
            </button>
          )}
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
                <th></th>
              </tr>
            </thead>
            <tbody>
              {groupBy === "tmux"
                ? groupSessionsByTmux(applySessFilter(boardSessions)).flatMap((g) => {
                    const collapsed = tmuxLaneCollapsed(g.key, g.rows);
                    const nodes: React.ReactNode[] = [
                      renderTmuxLaneHeaderRow(g.key, g.rows, 10, collapsed),
                    ];
                    if (!collapsed) {
                      for (const { row, level } of buildTmuxGroupTree(g.rows)) {
                        nodes.push(renderSessionRow(row, level));
                      }
                    }
                    return nodes;
                  })
                : groupBy === "user"
                ? // T-0157: linux user → tmux session → tree. Each user section
                  // header marks the owner; tmux lanes nest inside it.
                  groupSessionsByUser(applySessFilter(boardSessions)).flatMap((ug) => {
                    const nodes: React.ReactNode[] = [
                      renderUserHeaderRow(ug.key, ug.rows, 10),
                    ];
                    for (const g of groupSessionsByTmux(ug.rows)) {
                      const collapsed = tmuxLaneCollapsed(g.key, g.rows);
                      nodes.push(renderTmuxLaneHeaderRow(g.key, g.rows, 10, collapsed));
                      if (!collapsed) {
                        for (const { row, level } of buildTmuxGroupTree(g.rows)) {
                          nodes.push(renderSessionRow(row, level));
                        }
                      }
                    }
                    return nodes;
                  })
                : groupBy === "none"
                  ? buildSessionTreeLocal(applySessFilter(boardSessions)).map(
                      ({ row, level }) => renderSessionRow(row, level),
                    )
                  : buildVisibleLanes(boardSessions).flatMap((lane) => {
                      const laneRows = groupSessionsByLane(boardSessions)[lane.key] ?? [];
                      const collapsed = Boolean(collapsedSessLanes[lane.key]);
                      const nodes: React.ReactNode[] = [
                        renderLaneHeaderRow(lane, laneRows.length, 11),
                      ];
                      if (!collapsed) {
                        // Within each initiative lane the tree is also useful — TL at
                        // top, devs indented under it. Orphans (no TL bound to the
                        // lane initiative) render at root within the lane.
                        for (const { row, level } of buildSessionTreeLocal(laneRows)) {
                          nodes.push(renderSessionRow(row, level));
                        }
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
                {groupBy === "tmux"
                  ? groupSessionsByTmux(applySessFilter(archivedSessions)).flatMap((g) => {
                      const collapsed = tmuxLaneCollapsed(g.key, g.rows);
                      const nodes: React.ReactNode[] = [
                        renderTmuxLaneHeaderRow(g.key, g.rows, 9, collapsed),
                      ];
                      if (!collapsed) {
                        for (const s of g.rows) nodes.push(renderArchivedRow(s));
                      }
                      return nodes;
                    })
                  : groupBy === "none"
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
            {newRole === "operator" && (
              <button type="button" className="btn btn-primary" onClick={handleSpawnOperator} disabled={spawning}>
                {spawning ? "Spawning…" : "Spawn operator"}
              </button>
            )}
            {newRole === "teamlead" && (
              <button type="button" className="btn btn-primary" onClick={handleSpawnTeamlead} disabled={spawning}>
                {spawning ? "Spawning…" : "Spawn teamlead"}
              </button>
            )}
            {newRole === "prod-teamlead" && (
              <button type="button" className="btn btn-primary" onClick={handleSpawnProdTeamlead} disabled={spawning}>
                {spawning ? "Spawning…" : "Spawn prod-TL"}
              </button>
            )}
            {newRole === "qa" && (
              <button type="button" className="btn btn-primary" onClick={handleSpawnQa} disabled={spawning}>
                {spawning ? "Spawning…" : "Spawn QA"}
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
              className={`btn ${newRole === "operator" ? "btn-primary" : "btn-outline-primary"} flex-fill`}
              onClick={() => { setNewRole("operator"); setModalError(null); setModalInfo(null); }}
            >
              Operator
              <div style={{ fontSize: "0.72rem", fontWeight: 400, opacity: 0.8, marginTop: "0.15rem" }}>
                drives projects, spawns teamleads
              </div>
            </button>
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
          {/* T-0197: second row — the prod-ops + QA roles, less common than the
              feature-dev trio above but now first-class spawnable. */}
          <div className="d-flex gap-2 mt-2">
            <button
              type="button"
              className={`btn ${newRole === "prod-teamlead" ? "btn-primary" : "btn-outline-primary"} flex-fill`}
              onClick={() => { setNewRole("prod-teamlead"); setModalError(null); setModalInfo(null); }}
            >
              Prod-Teamlead
              <div style={{ fontSize: "0.72rem", fontWeight: 400, opacity: 0.8, marginTop: "0.15rem" }}>
                cuts releases from the prod clone
              </div>
            </button>
            <button
              type="button"
              className={`btn ${newRole === "qa" ? "btn-primary" : "btn-outline-primary"} flex-fill`}
              onClick={() => { setNewRole("qa"); setModalError(null); setModalInfo(null); }}
            >
              QA
              <div style={{ fontSize: "0.72rem", fontWeight: 400, opacity: 0.8, marginTop: "0.15rem" }}>
                verifies totest tickets vs DoD
              </div>
            </button>
          </div>
        </div>

        {/* Step 2 (operator): Operator form. The window is normalised to carry
            the -operator marker so the session resolves to operator.md (T-0041). */}
        {newRole === "operator" && (
          <>
            <hr style={{ borderColor: "var(--mc-border)" }} />
            <div className="mb-3">
              <label className="form-label">
                Window name{" "}
                <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>
                  (optional — defaults to "operator")
                </span>
              </label>
              <input
                className="form-control"
                value={newWindow}
                onChange={(e) => setNewWindow(e.target.value)}
                placeholder="e.g. operator, bot-squad"
                autoFocus
              />
              <div style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)", marginTop: "0.25rem" }}>
                Spawns as{" "}
                <code style={{ color: "var(--mc-accent)" }}>{operatorWindow(newWindow)}</code>{" "}
                → resolves to the operator role (operator.md).
              </div>
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

        {/* Step 2 (prod-teamlead): like operator, the window is normalised to
            carry the -prod-tl marker so the session resolves to
            prod-teamlead.md (T-0197). Not bound to a task/initiative. */}
        {newRole === "prod-teamlead" && (
          <>
            <hr style={{ borderColor: "var(--mc-border)" }} />
            <div className="mb-3">
              <label className="form-label">
                Window name{" "}
                <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>
                  (optional — defaults to "prod-tl")
                </span>
              </label>
              <input
                className="form-control"
                value={newWindow}
                onChange={(e) => setNewWindow(e.target.value)}
                placeholder="e.g. prod-tl, bot-squad"
                autoFocus
              />
              <div style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)", marginTop: "0.25rem" }}>
                Spawns as{" "}
                <code style={{ color: "var(--mc-accent)" }}>{prodTeamleadWindow(newWindow)}</code>{" "}
                → resolves to the prod-teamlead role (prod-teamlead.md).
              </div>
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

        {/* Step 2 (qa): window normalised to carry the -qa marker so the
            session resolves to qa.md (T-0197). QA picks up totest tickets;
            not bound to a single task at spawn. */}
        {newRole === "qa" && (
          <>
            <hr style={{ borderColor: "var(--mc-border)" }} />
            <div className="mb-3">
              <label className="form-label">
                Window name{" "}
                <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>
                  (optional — defaults to "qa")
                </span>
              </label>
              <input
                className="form-control"
                value={newWindow}
                onChange={(e) => setNewWindow(e.target.value)}
                placeholder="e.g. qa, bot-squad"
                autoFocus
              />
              <div style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)", marginTop: "0.25rem" }}>
                Spawns as{" "}
                <code style={{ color: "var(--mc-accent)" }}>{qaWindow(newWindow)}</code>{" "}
                → resolves to the QA role (qa.md).
              </div>
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
                {/* T-0280: reuse-before-spawn strip. When a task is picked we
                    show the worker's reuse-vs-spawn recommendation above the
                    spawn (Send-to-teamlead) button so the operator can resume
                    an existing live pro instead of spawning a fresh session. */}
                {newTaskId && (
                  <div className="mb-3">
                    <label className="form-label">
                      Reuse before spawn{" "}
                      <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>
                        (resume a live session bound to {newTaskId}&apos;s initiative)
                      </span>
                    </label>
                    {reuseLoading && (
                      <div style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
                        Checking for reusable sessions…
                      </div>
                    )}
                    {!reuseLoading && reuseError && (
                      <div className="alert alert-danger" style={{ fontSize: "0.8rem" }}>
                        Couldn&apos;t load reuse candidates: {reuseError}
                      </div>
                    )}
                    {!reuseLoading && !reuseError && reuse && (() => {
                      const eligible = reuse.candidates
                        .filter((c) => c.eligible)
                        .sort((a, b) => a.context_pct - b.context_pct);
                      if (eligible.length === 0) {
                        return (
                          <div style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
                            No reusable session — {reuse.reason}. Spawn a fresh one below.
                          </div>
                        );
                      }
                      return (
                        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
                          <div style={{ fontSize: "0.75rem", color: "var(--mc-text-dim)" }}>
                            {reuse.reason}
                          </div>
                          {eligible.map((c) => (
                            <div
                              key={c.sid}
                              className="d-flex align-items-center justify-content-between"
                              style={{
                                border: "1px solid var(--mc-border)",
                                borderRadius: "6px",
                                padding: "0.4rem 0.6rem",
                                gap: "0.6rem",
                              }}
                            >
                              <div style={{ minWidth: 0 }}>
                                <code style={{ color: "var(--mc-accent)" }}>{c.sid}</code>
                                {c.sid === reuse.target_sid && (
                                  <span className="mc-badge mc-badge-ok" style={{ marginLeft: "0.4rem" }}>
                                    recommended
                                  </span>
                                )}
                                <div style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
                                  {c.role ?? "dev"} · {c.context_pct}% context ·{" "}
                                  {c.idle ? "idle" : "busy"}
                                  {c.initiative_match ? " · same initiative" : ""}
                                </div>
                              </div>
                              <button
                                type="button"
                                className="btn btn-outline-primary btn-sm"
                                disabled={resumingSid !== null}
                                onClick={() => handleResumeFromModal(c.sid)}
                              >
                                {resumingSid === c.sid ? "Resuming…" : "Resume"}
                              </button>
                            </div>
                          ))}
                        </div>
                      );
                    })()}
                  </div>
                )}
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

      {/* Autopilot dialog (T-0153) — team-lane or single-session target. */}
      <AutopilotDialog
        open={autopilotOpen}
        slug={slug}
        target={autopilotTarget}
        onClose={() => setAutopilotOpen(false)}
        onStarted={() => load()}
      />
    </div>
  );
}
