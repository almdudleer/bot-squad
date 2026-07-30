import { useEffect, useMemo, useRef, useState, useCallback, Fragment } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
// Link kept for session SID links and task links inside the table
import type {
  SessionRow,
  Task,
  TelemetryResponse,
  TelemetrySession,
  VisionFile,
  WorkerFanoutError,
  SessionsScope,
} from "../api";
import { useApiClient } from "../apiContext";
import { CopyableTmuxAttach } from "../components/CopyableTmuxAttach";
import { Select } from "../components/Select";
import {
  sessionActivity,
  sessionLabel,
  sessionLiveness,
  sessionNeedsInput,
  sessionRole,
  sessionRoleLabel,
} from "../utils/sessionStatus";
import { contextCeilingOf, keepLastGoodTelemetry } from "../utils/telemetry";

import { PageHelp } from "../components/PageHelp";
import { PeerInbox } from "../components/PeerInbox";
import { ResourceCapsPanel } from "../components/ResourceCapsPanel";
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// T-0104: render a badge keyed off the canonical `activity` enum
// (worker-derived from jsonl mtime). `running` is the only "green LED"
// state — a live-but-quiet pane is `idle`, never `running`. Same
// vocabulary is used by TaskCard / TaskDetail so card and detail no
// longer disagree on the label.
export function StatusBadge({ row }: { row: SessionRow }) {
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
  // T-0444: when the worker stamped WHY an auto-close happened, surface it next
  // to the suspended badge so a surprise auto-cleanup is visible (not silent).
  // Absent on user/API suspends + legacy rows → badge unchanged.
  const reason = row.suspend_reason;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", opacity: 0.7 }}>
      <span className="mc-dot mc-dot-idle" />
      <span className="mc-badge mc-badge-dim">{sessionLabel(a)}</span>
      {reason && (
        <span
          title={row.suspend_source ? `${reason} (${row.suspend_source})` : reason}
          style={{ fontSize: "0.66rem", color: "var(--mc-text-dim)", fontStyle: "italic" }}
        >
          · {reason}
        </span>
      )}
    </span>
  );
}

// T-0285: explicit "this one is waiting on you" badge, driven by the worker's
// `awaiting_input` flag (the tg_stall blocked marker — agent peer_send'd an
// operator and got no reply). Amber, distinct from the activity StatusBadge so
// a blocked-but-still-"running" pane is glanceable. Renders nothing when not
// blocked, so callers can drop it inline next to the status with no layout cost.
export function AwaitingInputBadge({ row }: { row: SessionRow }) {
  if (!row.awaiting_input) return null;
  return (
    <span
      className="mc-badge mc-badge-warn"
      title="Waiting on you — this session pinged the operator and hasn't had a reply (tg_stall blocked marker)."
      style={{ display: "inline-flex", alignItems: "center", gap: "0.25rem" }}
    >
      ⏳ Awaiting input
    </span>
  );
}

// T-0628 (D-0056): TelemetryPanel dissolves — per-session context-token usage
// joins the main table as a cell on live rows instead of a second
// session-keyed table. Bucket a context % into the ok/warn/danger vocabulary
// TelemetryPanel used (carried over verbatim so the meaning doesn't drift).
export function contextBadgeKind(pct: number): "ok" | "warn" | "danger" {
  if (pct >= 100) return "danger";
  if (pct >= 80) return "warn";
  return "ok";
}

// Roll large token counts into k/M/B tiers (T-0267, carried over from the
// dissolved TelemetryPanel.fmtTokens).
export function fmtContextTokens(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e11 ? 0 : 1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k`;
  return String(n);
}

// T-0264 (carried over): normalize every row's bar + % to the single,
// caller-supplied ceiling (the max ceiling across sampled sessions) rather
// than the row's own possibly-stale context.ceiling, so bars share one
// denominator. Rows with no telemetry sample yet (not live, or worker hasn't
// ticked) render a dim placeholder rather than a misleading empty bar.
function ContextCell({
  telemetry,
  ceiling,
}: {
  telemetry: TelemetrySession | undefined;
  ceiling: number;
}) {
  if (!telemetry) {
    return <span style={{ color: "var(--mc-text-dim)", fontSize: "0.72rem" }}>—</span>;
  }
  const pct = ceiling > 0 ? (telemetry.context.tokens / ceiling) * 100 : 0;
  const kind = contextBadgeKind(pct);
  const barColor =
    kind === "danger" ? "var(--mc-danger, #d33)"
      : kind === "warn" ? "var(--mc-warn, #e0a000)"
        : "var(--mc-ok, #3a8)";
  return (
    <div
      style={{ minWidth: 96 }}
      title={`${telemetry.context.tokens.toLocaleString()} / ${ceiling.toLocaleString()} tokens`}
    >
      <div
        style={{
          position: "relative", height: 6, borderRadius: 2,
          background: "var(--mc-border)", overflow: "hidden",
        }}
      >
        <div style={{
          position: "absolute", inset: 0, width: `${Math.min(100, pct)}%`,
          background: barColor,
        }} />
      </div>
      <div style={{ fontSize: "0.6rem", color: "var(--mc-muted, #888)", marginTop: 2 }}>
        {fmtContextTokens(telemetry.context.tokens)} · {Math.round(pct)}%
        {telemetry.rate_limited && <span title="hit a 429"> 🚫</span>}
      </div>
    </div>
  );
}

// T-0141: role badge keyed off the worker-derived `role` (falls back to the
// legacy task_id inference for a pre-T-0141 worker). Teamlead = green,
// operator = amber, dev = blue. T-0197: prod-teamlead = red (prod caution),
// qa = active/purple. T-0727: user-conversation = magenta (system-spawned
// user-intake session — must not read as a dev worker).
// `dim` mutes the badge for archived rows.
export function RoleBadge({ row, dim = false }: { row: SessionRow; dim?: boolean }) {
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
            // T-0727: user-conversation = magenta, never dev-blue.
            : role === "user-conversation"
              ? "mc-badge mc-badge-user"
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
export function sessionLastActivity(s: SessionRow): string {
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
//
// T-0340: the precedence now lives in the shared `sessionLiveness` helper
// (utils/sessionStatus.ts) so the "live" CATEGORY word + count is defined in
// ONE place across sidebar / sessions / analytics. This predicate is the
// boolean projection of that category and stays the canonical liveness probe
// other surfaces import (resourceCaps).
// ---------------------------------------------------------------------------
export function isLiveSession(s: SessionRow): boolean {
  return sessionLiveness(s) === "live";
}

// T-0658: an all-worker-socket-timeout poll tick returns `rows: []` with a
// populated `errors` array — that's an UNKNOWN session count, not a
// confirmed-empty project. Pulled out as a pure function (mirrors
// `isLiveSession` above) so the empty-vs-uncertain classification is unit
// testable without rendering the page.
//
// T-0772 adds the THIRD reading of the same empty list, and it is the one this
// page is the destination for: the owner gate filtered every row out. This page
// is where the board's LIVE SESSIONS card links, so a non-admin who clicked a
// zero used to land on "No sessions for bot-squad" — the bare copy confirming
// the false impression the card had just created. `scope` comes from the server
// (it is the only party that knows whether it filtered); an UNKNOWN scope keeps
// the neutral "none" wording rather than guessing.
//
// PRECEDENCE IS DELIBERATE: "uncertain" still wins over "none-own". An
// unreachable worker also yields zero rows, and reporting that as "you own
// none" would state a per-user fact about a tick where nothing was measured.
export type SessionsEmptyState = "none" | "none-own" | "uncertain" | null;

export function sessionsEmptyState(
  sessions: SessionRow[] | null,
  fanoutErrors: WorkerFanoutError[],
  scope: SessionsScope = null,
): SessionsEmptyState {
  if (sessions === null || sessions.length > 0) return null;
  if (fanoutErrors.length > 0) return "uncertain";
  return scope === "own" ? "none-own" : "none";
}

// ---------------------------------------------------------------------------
// T-0803 — a MOMENTARY socket gap must not read like a dead worker.
//
// Measured on 20.6 days of worker journal: the socket has two unrelated
// failure modes, and this page rendered them identically.
//
//   * RESTART GAP — 72 worker restarts (one per 6.8h), socket dead a median
//     of 1s (p90 4s). Nothing is wrong; the worker is coming back. With a 10s
//     poll each restart has roughly a 10-40% chance of landing inside one
//     tick.
//   * PROCESS STALL — 15 events where the worker was alive but silent for
//     >=30s (max 2h15m; the longest ended exactly at a host OOM-kill). Here
//     the sessions really are unobservable, for minutes to hours.
//
// The old behaviour made the FIRST failing poll blank the table and raise
// "Session list may be incomplete". A 1-second restart gap therefore cost ten
// seconds of empty list plus an alarming banner, which then cleared itself —
// the flapping the stakeholder reported ("это состояние flapping туда-сюда").
//
// Two rules, both pure and unit-tested:
//
//  1. classifyFanoutPhase — one bad poll is "transient" (say so quietly, keep
//     the rows); two consecutive bad polls is "sustained" (>=~10s of real
//     unreachability, which a restart gap essentially never reaches but a
//     stall always does). This is the never-reachable / momentarily-unreachable
//     distinction the ticket asks for, and it is derived from the measured
//     duration of each mode rather than guessed.
//
//  2. retainRowsThroughFanoutGap — ONLY a poll that ADMITS a fan-out failure
//     backfills rows. A clean poll is authoritative and always wins, so a
//     genuinely-ended session still disappears immediately. Carried-over rows
//     are stamped `retained_stale` so "we are showing you last-known state" is
//     visible in the row, never implied. This is the direct answer to "a
//     flapping list that silently drops rows is worse than one that says it
//     does not know".
// ---------------------------------------------------------------------------
export type FanoutPhase = "ok" | "transient" | "sustained";

/** Consecutive failing polls before the hard "unreachable" banner. */
export const FANOUT_SUSTAINED_POLLS = 2;

export function classifyFanoutPhase(consecutiveFailedPolls: number): FanoutPhase {
  if (consecutiveFailedPolls <= 0) return "ok";
  return consecutiveFailedPolls >= FANOUT_SUSTAINED_POLLS ? "sustained" : "transient";
}

export function retainRowsThroughFanoutGap(
  prevRows: SessionRow[] | null,
  freshRows: SessionRow[],
  hasFanoutErrors: boolean,
): SessionRow[] {
  // A poll that reached every socket is the truth, including about absences.
  if (!hasFanoutErrors) return freshRows;
  const seen = new Set(freshRows.map((r) => r.sid));
  const carried = (prevRows ?? [])
    .filter((r) => !seen.has(r.sid))
    .map((r) => ({ ...r, retained_stale: true }));
  return [...freshRows, ...carried];
}

// T-0347: the per-row tmux-attach affordance. Only a LIVE session has a tmux
// pane to attach to — a suspended/archived row has none, and offering the copy
// there emitted a broken `tmux a -t …:<window>` that never attached (it ties to
// the unified liveness vocab, T-0340). Non-live rows get a dim placeholder so
// the column stays aligned and reads "nothing to attach to" rather than handing
// the operator a dead command.
function AttachAffordance({
  s,
  slug,
  iconOnly,
  size,
}: {
  s: SessionRow;
  slug: string;
  iconOnly?: boolean;
  size?: "sm" | "md";
}) {
  if (!isLiveSession(s)) {
    return (
      <span
        title="No live tmux session to attach to"
        style={{
          color: "var(--mc-text-dim)",
          fontFamily: "var(--mc-mono)",
          fontSize: "0.74rem",
        }}
      >
        —
      </span>
    );
  }
  return (
    <CopyableTmuxAttach
      session={s.tmux_session || slug}
      window={s.window}
      iconOnly={iconOnly}
      size={size}
    />
  );
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
  // T-0628 (D-0056): the Pinned section (a duplicate session-keyed table)
  // dissolves — a pinned root instead floats to the top of the root list,
  // ahead of the operator/TL/rest tiers, so "pin = surfaces first" survives
  // without a second rendering. The role tiers still order everything else,
  // and the sort stays stable so unpinned same-rank roots are unaffected.
  roots.sort((a, b) => {
    const pa = a.pinned ? 0 : 1;
    const pb = b.pinned ? 0 : 1;
    if (pa !== pb) return pa - pb;
    return rootRank(a) - rootRank(b);
  });

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
  // T-0601 (F5): per-user worker sockets that failed during the sessions
  // fan-out — rendered as a warning banner so a partial (or empty) list is
  // never mistaken for "no sessions".
  const [fanoutErrors, setFanoutErrors] = useState<WorkerFanoutError[]>([]);
  // T-0803: consecutive polls whose fan-out reported a socket failure. The ref
  // is the source of truth (the poll closure must read the value it just
  // wrote, without waiting for a re-render); the state mirror is what renders.
  const fanoutStreakRef = useRef(0);
  const [fanoutStreak, setFanoutStreak] = useState(0);
  // T-0772: whether the server owner-filtered the rows above. Drives the
  // empty-state copy so a scoped-empty list stops reading as an idle project.
  const [sessionsScope, setSessionsScope] = useState<SessionsScope>(null);

  // meUsername feeds PeerInbox (the kept reply surface, T-0127).
  const [meUsername, setMeUsername] = useState<string>("stakeholder");

  // T-0572 (Occam pass, D-0046) + T-0594/T-0675 (D-0057 §8, full 2b): the
  // page is pure read-first observability — steering (pause/suspend/resume,
  // archive/unarchive, pin/unpin) lives in the TG dialog / CLI now. The old
  // kebab-behind-a-toggle lifecycle controls are cut entirely; their API
  // routes remain the TG/CLI control plane's substrate.

  // Row expansion (click-through cwd / metadata detail) and archived
  // section toggle.
  const [expandedSids, setExpandedSids] = useState<Set<string>>(new Set());
  // T-0389/audit item 19: ONE dead-tail escape hatch. The board is LIVE-only by
  // default; a single "history" toggle reveals BOTH the suspended (non-archived)
  // rows inline AND the archived section — replacing the old pair of separate
  // controls (a `showSuspended` button + a `showArchived` <details>).
  const [showHistory, setShowHistory] = useState(false);

  // T-0346: when arrived here from a home `needs-input` project card
  // (?needs_input=1), surface a banner pinpointing the waiting session(s) +
  // their attach command so the operator goes from the home signal straight to
  // WHAT needs input — not the generic board. Dismissable for the page life.
  const [needsInputDismissed, setNeedsInputDismissed] = useState(false);

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
      .sessionsDetail(slug)
      .then(({ rows, errors, scope }) => {
        // T-0803: a fan-out failure is a 200 with `errors` populated, not a
        // rejection — so this, not the catch below, is the flapping path.
        const failed = errors.length > 0;
        fanoutStreakRef.current = failed ? fanoutStreakRef.current + 1 : 0;
        setFanoutStreak(fanoutStreakRef.current);
        setSessions((prev) => retainRowsThroughFanoutGap(prev, rows, failed));
        setFanoutErrors(errors);
        setSessionsScope(scope);
        setError(null);
      })
      .catch((e: unknown) => {
        setError(String(e));
        // T-0803: the whole request failed, so we learned nothing about the
        // sockets — reset the streak rather than let a network blip escalate
        // the banner to "sustained" and accuse a worker that may be fine.
        fanoutStreakRef.current = 0;
        setFanoutStreak(0);
        // T-0609: a total load failure means the fan-out picture is unknown —
        // keeping the previous poll's banner would name sockets we can no
        // longer vouch for, alongside the error alert.
        setFanoutErrors([]);
        // T-0772: same rule for the scope — a failed load did not tell us what
        // the server would have filtered, so drop the claim rather than keep a
        // stale one that would explain this tick's blank list as "yours only".
        setSessionsScope(null);
      });
  }, [slug]);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  // T-0210/T-0628 (D-0056): per-session resource telemetry — the dissolved
  // TelemetryPanel's own poll, same 10s cadence as the sessions list so the
  // context% cell never lags the row it's attached to.
  const [telemetry, setTelemetry] = useState<TelemetryResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    const loadTelemetry = () => {
      api
        .telemetry(slug)
        .then((d) => {
          if (cancelled) return;
          // KEEP-LAST-GOOD (T-0726) — the SAME rule ResourceCapsPanel applies
          // to caps/quota, shared via utils/telemetry so a third consumer
          // can't drift again. An empty `sessions` array is the route's
          // worker-timeout 200, not a reading; writing it through blanked
          // every row's Context cell and collapsed contextCeiling (the shared
          // denominator, T-0264) to 0 for up to one 10s tick — one row below a
          // caps strip still correctly reporting N live sessions.
          setTelemetry((prev) => keepLastGoodTelemetry(prev, d));
        })
        .catch(() => { /* silent — cell just shows "—" until next poll */ });
    };
    loadTelemetry();
    const id = setInterval(loadTelemetry, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [slug, api]);
  const telemetryBySid = useMemo(() => {
    const m = new Map<string, TelemetrySession>();
    for (const s of telemetry?.sessions ?? []) m.set(s.sid, s);
    return m;
  }, [telemetry]);
  // T-0230/T-0264 (carried over): the contract ceiling is tunable and worker-
  // stamped per session — derive the column's shared denominator from the
  // payload itself rather than hard-coding it. T-0726 moved the derivation
  // into utils/telemetry alongside the keep-last-good rule that feeds it.
  const contextCeiling = useMemo(() => contextCeilingOf(telemetry), [telemetry]);

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
  // Backlog cached here for the always-on tree render. A 30s refresh is
  // enough — task↔initiative bindings change far less often than session
  // activity.
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

    // Drop any filter that would hide the row. T-0389 item 19: a suspended OR
    // archived target is revealed by the single history toggle.
    setFilterInit("");
    if (found.archived || found.status === "suspended") setShowHistory(true);
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
  // T-0389 item 19: one history toggle reveals suspended (inline) + archived.
  const hiddenHistoryCount = hiddenSuspendedCount + archivedSessions.length;
  const boardSessions: SessionRow[] = showHistory ? visibleSessions : liveSessions;

  // T-0346: needs-input deep-link landing. The home/project card for a
  // needs-input project routes here with ?needs_input=1; pinpoint the waiting
  // session(s) — paused or awaiting_input (sessionNeedsInput mirrors the
  // project-level quick_status rollup, T-0375 canonical) — so the operator lands on WHAT needs
  // input + its attach command, not the generic board.
  const needsInputView = searchParams.get("needs_input") != null;
  const waitingSessions: SessionRow[] = useMemo(
    () => (sessions ?? []).filter((s) => !s.archived && sessionNeedsInput(s)),
    [sessions],
  );
  // Reuse the existing ?sid= flash/scroll machinery to jump to a waiting row in
  // the table while preserving the needs_input param.
  function jumpToWaiting(sid: string) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("sid", sid);
        return next;
      },
      { replace: true },
    );
  }

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
            <dd style={{ margin: 0, wordBreak: "break-all", color: "var(--mc-text)" }}>{s.sid_label ?? s.sid}</dd>
            {/* T-0281: surface role + what this process is bound to (task /
                initiative) right in the process panel, so one place answers
                "what is this process and what is it working on". */}
            <dt>role</dt>
            <dd style={{ margin: 0, color: "var(--mc-text)" }}>{sessionRoleLabel(sessionRole(s))}</dd>
            <dt>task</dt>
            <dd style={{ margin: 0, color: "var(--mc-text)" }}>
              {s.task_id && s.task_id !== "~"
                ? [s.task_id, ...(s.extra_task_ids ?? [])].join(", ")
                : "—"}
            </dd>
            <dt>initiative</dt>
            <dd style={{ margin: 0, color: "var(--mc-text)" }}>
              {s.initiative && s.initiative !== "~"
                ? [s.initiative, ...(s.extra_initiatives ?? [])].join(", ")
                : "—"}
            </dd>
            {s.awaiting_input && (
              <>
                <dt>awaiting input</dt>
                <dd style={{ margin: 0, color: "var(--mc-amber)" }}>
                  ⏳ waiting on operator reply
                </dd>
              </>
            )}
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

  function renderSessionRow(s: SessionRow, level = 0) {
    const isOpen = expandedSids.has(s.sid);
    const isFlashing = flashSid === s.sid;
    const isTL = sessionRole(s) === "teamlead";
    return (
      <Fragment key={s.sid}>
        <tr
          id={`sess-row-${s.sid}`}
          className={isFlashing ? "mc-row-flash" : undefined}
          // T-0803: a row carried over an unreachable-socket poll is dimmed and
          // says so on hover. Retaining it is only an improvement over dropping
          // it if the reader can tell it is last-known rather than observed.
          data-retained-stale={s.retained_stale ? "true" : undefined}
          title={
            s.retained_stale
              ? "Last known state — the worker socket was unreachable on the latest poll"
              : undefined
          }
          style={{
            cursor: "pointer",
            ...(s.retained_stale ? { opacity: 0.55 } : {}),
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
            {/* T-0437: pin marker — a session the user works closely with. */}
            {s.pinned && (
              <span
                title={s.pinned_by ? `Pinned by ${s.pinned_by}` : "Pinned"}
                style={{ marginRight: "0.3rem", fontSize: "0.72rem" }}
              >
                📌
              </span>
            )}
            {s.claude_uuid ? (
              <Link
                to={`/p/${slug}/sessions/${encodeURIComponent(s.claude_uuid)}/messages`}
                style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-accent)" }}
              >
                {s.sid_label ?? s.sid}
              </Link>
            ) : (
              <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-text-mid)" }}>
                {s.sid_label ?? s.sid}
              </code>
            )}
          </td>

          {/* T-0363: Window column cut — the SID already encodes the window
              (S-<user>-<window>-p<N>), so a separate Window column was a pure
              duplicate. The full SID (with window) stays in the SID cell + the
              expandable detail. */}

          {/* Attach — T-0141: the copyable command targets the tmux SESSION
              (`tmux a -t <tmux_session>:<window>`), not the SID. Passing the
              SID built `tmux a -t S-…:<window>` which never attached.
              T-0347: gated to live sessions via AttachAffordance. */}
          <td onClick={(e) => e.stopPropagation()} style={{ width: "1px", whiteSpace: "nowrap" }}>
            <AttachAffordance s={s} slug={slug} iconOnly />
          </td>

          {/* Role — T-0141: worker-derived, no longer "task-less ⟹ TL". */}
          <td>
            <RoleBadge row={s} />
          </td>

          {/* Bound (initiative for TL / task for dev) */}
          <td>{renderBoundCell(s)}</td>

          {/* Status */}
          <td>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "0.35rem" }}>
              <StatusBadge row={s} />
              <AwaitingInputBadge row={s} />
            </div>
          </td>

          {/* Context — T-0628 (D-0056): merged in from the dissolved
              TelemetryPanel, one cell on the row that already IS this
              session's home. */}
          <td>
            <ContextCell telemetry={telemetryBySid.get(s.sid)} ceiling={contextCeiling} />
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
        </tr>
        {isOpen && renderDetailRow(s, 9)}
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
              {s.sid_label ?? s.sid}
            </code>
          </td>
          {/* T-0363: Window column cut (SID already encodes the window). */}
          {/* T-0347: archived rows are never live → AttachAffordance renders the
              dim placeholder, never a dead `tmux a -t` command. */}
          <td onClick={(e) => e.stopPropagation()} style={{ width: "1px", whiteSpace: "nowrap" }}>
            <AttachAffordance s={s} slug={slug} iconOnly />
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
        </tr>
        {isOpen && renderDetailRow(s, 7)}
      </Fragment>
    );
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
              {alive} live · {suspended} suspended
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
  // T-0340: "live" is the canonical category — read it off the shared
  // `sessionLiveness` helper so the lane "N live" counts match the board's
  // live-only `liveSessions` count exactly (no more "alive" vs "live" drift).
  function isAliveRow(s: SessionRow): boolean {
    return sessionLiveness(s) === "live";
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
              {alive} live · {suspended} suspended
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
            {/* T-0594 (T-0588b): the lane "Autopilot…" kebab was cut with the
                rest of the autopilot web affordances (Board keeps its own). */}
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
        {/* T-0383: single-brain process vocab — "Agent sessions" → "Processes". */}
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
          Processes
          <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
        </h2>
      </div>
      <PageHelp>
        Live status board for the Claude tmux sessions whose CWD is this
        project&apos;s repo — the system spawns, reuses and reaps them for
        you. Steer day-to-day via the Telegram dialog: reply to a
        <code> [SID] needs your input</code> notification and your reply lands
        in that session, or use <code>/sessions</code>,
        {" "}<code>/say &lt;sid&gt; &lt;text&gt;</code> via the bot.
      </PageHelp>

      {/* T-0339 (reframe Pillar A item 5 + T-0306): consolidated caps/budget
          control — the operator's Task-Manager limits surfaced RIGHT IN the
          process view where you watch and constrain the brain, instead of buried
          in server admin. Read+set affordance here; server-level enforcement
          (worker spawn-time checks) stays the source of truth underneath.
          T-0628 (D-0056): TelemetryPanel's header facts (429 badge, burn
          rate) merged in here too — ONE resources panel instead of two;
          its per-session rows joined the main table as the Context column. */}
      <ResourceCapsPanel slug={slug} />

      {/* Errors */}
      {error && <div className="alert alert-danger">{error}</div>}
      {/* T-0601 (F5): worker fan-out failures — the list below is PARTIAL
          (or empty) because these per-user worker sockets were unreachable.
          Without this banner a dead socket read as "No sessions". */}
      {/* T-0803: a single failing poll is now a quiet "re-checking" note that
          KEEPS the last-known rows, because the measured median restart gap is
          1s and the old banner turned that into 10s of empty table. Only a
          second consecutive failure — ~10s+ of real unreachability, which a
          restart gap essentially never reaches — escalates to the original
          warning. */}
      {fanoutErrors.length > 0 && classifyFanoutPhase(fanoutStreak) === "transient" && (
        <div
          className="alert alert-secondary"
          role="status"
          data-testid="fanout-transient-banner"
          style={{ fontSize: "0.8rem" }}
        >
          Re-checking the worker socket
          {fanoutErrors.length === 1 ? "" : "s"} — showing the last known
          session list.
        </div>
      )}
      {fanoutErrors.length > 0 && classifyFanoutPhase(fanoutStreak) === "sustained" && (
        <div
          className="alert alert-warning"
          role="status"
          data-testid="fanout-errors-banner"
        >
          <strong style={{ fontSize: "0.85rem" }}>
            Session list may be incomplete
          </strong>
          <div style={{ fontSize: "0.8rem", marginTop: "0.25rem" }}>
            The worker socket{fanoutErrors.length === 1 ? "" : "s"} for{" "}
            {fanoutErrors.map((e) => (
              <code key={e.user} title={e.detail} style={{ marginRight: "0.3rem" }}>
                {e.user}
              </code>
            ))}
            {fanoutErrors.length === 1 ? "is" : "are"} unreachable for{" "}
            {fanoutStreak} polls — rows still shown for{" "}
            {fanoutErrors.length === 1 ? "this user" : "these users"} are the
            last known state, not current.
          </div>
        </div>
      )}
      {/* T-0346: needs-input deep-link banner. Shown when the operator arrived
          from a home `needs-input` project card. Lists the waiting session(s)
          with their attach command so "needs input" on home leads in one
          click to WHAT needs input — reply via TG/CLI attach (T-0675/D-0057
          §8: web dropped the in-place Resume control, pure lookup now). */}
      {needsInputView && !needsInputDismissed && sessions !== null && (
        <div
          className="alert alert-warning"
          role="status"
          data-testid="needs-input-banner"
          style={{ borderLeft: "4px solid var(--mc-amber, #fbbf24)" }}
        >
          <div className="d-flex justify-content-between align-items-start gap-2">
            <strong style={{ fontSize: "0.9rem" }}>
              {waitingSessions.length > 0
                ? `${waitingSessions.length} session${
                    waitingSessions.length === 1 ? "" : "s"
                  } waiting for your input`
                : "Nothing is waiting for input right now"}
            </strong>
            <button
              type="button"
              className="btn-close"
              aria-label="Dismiss"
              style={{ filter: "invert(1) opacity(0.5)" }}
              onClick={() => setNeedsInputDismissed(true)}
            />
          </div>
          {waitingSessions.length === 0 ? (
            <div style={{ fontSize: "0.82rem", marginTop: "0.35rem" }}>
              This project was flagged <code>needs-input</code>, but no session is
              currently paused or waiting at a prompt — it may have just been
              answered.
            </div>
          ) : (
            <div
              style={{
                marginTop: "0.5rem",
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
              }}
            >
              <div style={{ fontSize: "0.8rem", color: "var(--mc-text-mid)" }}>
                A process finished its turn and needs you. Attach to see the
                question and reply:
              </div>
              {waitingSessions.map((s) => {
                const paused = s.status === "paused";
                return (
                  <div
                    key={s.sid}
                    className="d-flex align-items-center justify-content-between flex-wrap gap-2"
                    style={{
                      border: "1px solid var(--mc-border)",
                      borderRadius: "6px",
                      padding: "0.4rem 0.6rem",
                      background: "var(--mc-surface-raised)",
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <code style={{ color: "var(--mc-accent)", fontSize: "0.8rem" }}>
                        {s.window}
                      </code>
                      <span
                        className="mc-badge mc-badge-warn"
                        style={{ marginLeft: "0.4rem" }}
                      >
                        {paused ? "paused" : "at prompt"}
                      </span>
                      <div style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
                        <button
                          type="button"
                          className="btn btn-link p-0"
                          style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }}
                          onClick={() => jumpToWaiting(s.sid)}
                          title="Jump to this session in the table"
                        >
                          {s.sid_label ?? s.sid}
                        </button>
                      </div>
                    </div>
                    <div className="d-flex align-items-center gap-2">
                      {/* T-0347: gated to live sessions (these are always live by
                          construction — paused/at-prompt — but stay consistent). */}
                      <AttachAffordance s={s} slug={slug} size="md" />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Loading */}
      {sessions === null && !error && (
        <div className="mc-loading">Loading sessions</div>
      )}

      {/* Empty state — T-0658: distinguishes a confirmed-empty project from
          an all-worker-socket-timeout tick (the fanout-errors banner above
          already names the unreachable sockets). */}
      {sessionsEmptyState(sessions, fanoutErrors, sessionsScope) === "none" && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          <div>No sessions for <strong>{slug}</strong></div>
        </div>
      )}
      {/* T-0772: the owner gate emptied the list — say so. "No sessions for
          <slug>" here is a statement about the PROJECT, and this page is where
          the board's LIVE SESSIONS card lands. */}
      {sessionsEmptyState(sessions, fanoutErrors, sessionsScope) === "none-own" && (
        <div className="mc-empty" data-testid="scoped-empty-state">
          <div className="mc-empty-icon">◯</div>
          <div>
            You don&apos;t own any sessions in <strong>{slug}</strong>
          </div>
          <div style={{ fontSize: "0.8rem", marginTop: "0.35rem" }}>
            This list shows only sessions you own — the project may have others
            running.
          </div>
        </div>
      )}
      {sessionsEmptyState(sessions, fanoutErrors, sessionsScope) === "uncertain" && (
        <div className="mc-empty" data-testid="fanout-uncertain-state">
          <div className="mc-empty-icon">◇</div>
          <div>
            Couldn't confirm sessions for <strong>{slug}</strong> — worker
            sockets unreachable, see banner above
          </div>
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
          {/* T-0232 / T-0389 item 19: the board is live-only by default. ONE
              history toggle reveals the dead tail — suspended rows (inline) AND
              the archived section — so they stay reachable without two separate
              escape hatches. */}
          {(hiddenHistoryCount > 0 || showHistory) && (
            <button
              type="button"
              className={`btn btn-sm ${showHistory ? "btn-secondary" : "btn-outline-secondary"}`}
              style={{ fontSize: "0.72rem", padding: "0.15rem 0.55rem" }}
              onClick={() => setShowHistory((v) => !v)}
              title={
                showHistory
                  ? "Hide history (show live sessions only)"
                  : "Reveal history — suspended + archived sessions"
              }
            >
              {showHistory
                ? "hide history"
                : `${hiddenHistoryCount} in history — show`}
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

      {/* T-0437/T-0628 (D-0056): the separate Pinned table dissolved — a
          pinned row's 📌 marker (renderSessionRow) is the surviving signal,
          and buildSessionTree floats a pinned root to the top of the tree
          below instead of duplicating its row in a second table. */}

      {/* Session table */}
      {sessions !== null && sessions.length > 0 && (
        <div className="table-responsive">
          <table className="table table-hover align-middle">
            <thead>
              <tr>
                <th style={{ width: "1.5rem" }}></th>
                <th>SID</th>
                <th>Attach</th>
                <th>Role</th>
                <th>Target</th>
                <th>Status</th>
                <th>Context</th>
                <th>Started</th>
                <th title={LAST_ACTIVITY_TOOLTIP}>Last activity</th>
              </tr>
            </thead>
            <tbody>
              {groupBy === "tmux"
                ? groupSessionsByTmux(applySessFilter(boardSessions)).flatMap((g) => {
                    const collapsed = tmuxLaneCollapsed(g.key, g.rows);
                    const nodes: React.ReactNode[] = [
                      renderTmuxLaneHeaderRow(g.key, g.rows, 9, collapsed),
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
                      renderUserHeaderRow(ug.key, ug.rows, 9),
                    ];
                    for (const g of groupSessionsByTmux(ug.rows)) {
                      const collapsed = tmuxLaneCollapsed(g.key, g.rows);
                      nodes.push(renderTmuxLaneHeaderRow(g.key, g.rows, 9, collapsed));
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
                        renderLaneHeaderRow(lane, laneRows.length, 9),
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

      {/* Archived section — revealed by the SAME single history toggle as the
          suspended rows (T-0389 item 19: one escape hatch, not a separate
          <details> disclosure). */}
      {sessions !== null && archivedSessions.length > 0 && showHistory && (
        <div
          className="mt-3"
          style={{ borderTop: "1px solid var(--mc-border)", paddingTop: "0.5rem" }}
        >
          <div
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.74rem",
              color: "var(--mc-text-dim)",
              textTransform: "uppercase",
              letterSpacing: "0.08em",
              padding: "0.35rem 0",
            }}
          >
            Archived ({archivedSessions.length})
          </div>
          <div className="table-responsive mt-2">
            <table className="table table-hover align-middle" style={{ opacity: 0.85 }}>
              <thead>
                <tr>
                  <th style={{ width: "1.5rem" }}></th>
                  <th>SID</th>
                  <th>Attach</th>
                  <th>Role</th>
                  <th>Target</th>
                  <th>Started</th>
                  <th title={LAST_ACTIVITY_TOOLTIP}>Last activity</th>
                </tr>
              </thead>
              <tbody>
                {groupBy === "tmux"
                  ? groupSessionsByTmux(applySessFilter(archivedSessions)).flatMap((g) => {
                      const collapsed = tmuxLaneCollapsed(g.key, g.rows);
                      const nodes: React.ReactNode[] = [
                        renderTmuxLaneHeaderRow(g.key, g.rows, 7, collapsed),
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
                          renderLaneHeaderRow(lane, laneRows.length, 7),
                        ];
                        if (!collapsed) {
                          for (const s of laneRows) nodes.push(renderArchivedRow(s));
                        }
                        return nodes;
                      })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* T-0431: the PeerInbox FAB is fixed bottom-right; at narrow widths
          (~390px) it overlapped the last table row's ATTACH tap target
          (F-2026-06-21-inbox-02d3cb26cc). An in-flow spacer reserves bottom
          clearance so the last row always scrolls above the FAB band. NOTE: a
          paddingBottom on the .container is futile — Bootstrap's `.py-4` sets
          `padding-bottom: 1.5rem !important`, which an inline style can't beat;
          a real element is the robust fix. Harmless on desktop (FAB sits in the
          wide right margin). */}
      <div aria-hidden style={{ height: "5rem" }} />
    </div>
  );
}
