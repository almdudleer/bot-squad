import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type SchedulerState,
  type Transparency as TransparencyData,
  type SessionRow,
} from "../api";
import { Markdown } from "./Markdown";
import {
  sessionActivity,
  sessionRole,
  sessionRoleLabel,
} from "../utils/sessionStatus";

/**
 * T-0593 (T-0588a, D-0046) — top-level observability on the project home.
 *
 *   "весь этот юай это в целом больше про обсурдобилити" — the WHOLE UI is
 *   the observability layer (T-0587), so the separate /p/:slug/transparency
 *   tab was a mistake.
 *
 * Ported from the retired Transparency page (T-0511): a compact always-visible
 * summary strip (quota/pace cards + live-session count) with expandable detail
 * — the who-does-what session table, the operator state-doc, and the scheduler
 * panel (merged there from the retired /scheduler page, T-0572). The old
 * "Backlog" counts section was dropped: the board below IS the backlog, and
 * CanonicalSummary already shows the counts. Convergence rule: the home owns
 * live state, Analytics owns history. Strictly read-only.
 *
 * NOTE: uses the global `api` singleton (as the Transparency page did) — the
 * transparency/scheduler reads aren't part of the mothership-proxied
 * ProjectApi surface.
 */

function fmtFuture(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (isNaN(ts)) return "—";
  const diff = Math.floor((ts - Date.now()) / 1000);
  if (diff < 0) return "now";
  if (diff < 60) return `in ${diff}s`;
  if (diff < 3600) return `in ${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `in ${Math.floor(diff / 3600)}h`;
  return `in ${Math.floor(diff / 86400)}d`;
}

function fmtRel(epoch: number | null): string {
  if (!epoch) return "—";
  const diff = Math.floor(Date.now() / 1000 - epoch);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

/**
 * Collapsible detail section — the expandable half of the panel. Native
 * <details> so the collapsed state needs no React state; the summary reuses
 * the retired page's SectionTitle look.
 */
function DetailSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <details style={{ marginTop: "0.6rem" }}>
      <summary
        style={{
          fontFamily: "var(--mc-mono)",
          fontSize: "0.66rem",
          color: "var(--mc-text-dim)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          cursor: "pointer",
          userSelect: "none",
        }}
      >
        {title}
      </summary>
      <div style={{ margin: "0.5rem 0 0.25rem" }}>{children}</div>
    </details>
  );
}

/**
 * The always-visible summary strip: the T-0482 quota/pace cards plus the
 * live-session count (the headline of the who-does-what table below).
 */
function SummaryStrip({
  quota,
  liveSessions,
}: {
  quota: TransparencyData["quota"];
  liveSessions: number;
}) {
  const cap = quota.max_in_progress;
  const capLabel = cap === 0 ? "∞" : String(cap);
  const overCap = cap > 0 && quota.in_progress > cap;
  return (
    <div className="mc-an-cards">
      <div className="mc-an-card">
        <div className="mc-an-card-label">IN PROGRESS</div>
        <div
          className="mc-an-card-value"
          style={{ color: overCap ? "var(--mc-red)" : "var(--mc-text)" }}
        >
          {quota.in_progress} / {capLabel}
        </div>
        <div className="mc-an-card-sub">
          {cap === 0 ? "no max-in-progress cap" : "max-in-progress cap"}
        </div>
      </div>
      <div className="mc-an-card">
        <div className="mc-an-card-label">RE-DRIVE</div>
        <div
          className="mc-an-card-value"
          style={{ color: quota.paused ? "var(--mc-amber)" : "var(--mc-green)" }}
        >
          {quota.paused ? "PAUSED" : "RUNNING"}
        </div>
        <div className="mc-an-card-sub">
          {quota.paused
            ? "operator re-drive is user-paused"
            : "operator re-drive active"}
        </div>
      </div>
      <div className="mc-an-card">
        <div className="mc-an-card-label">INITIATIVE PACE</div>
        <div className="mc-an-card-value">
          {Object.keys(quota.initiatives).length}
        </div>
        <div className="mc-an-card-sub">configured initiatives</div>
      </div>
      <div className="mc-an-card">
        <div className="mc-an-card-label">LIVE SESSIONS</div>
        <div className="mc-an-card-value">{liveSessions}</div>
        <div className="mc-an-card-sub">who-does-what below</div>
      </div>
    </div>
  );
}

function SessionTree({
  sessions,
  slug,
}: {
  sessions: SessionRow[];
  slug: string;
}) {
  if (sessions.length === 0) {
    return (
      <div className="text-muted" style={{ fontSize: "0.85rem" }}>
        No live sessions reported.
      </div>
    );
  }
  return (
    <table className="table table-sm mc-table" style={{ fontSize: "0.82rem" }}>
      <thead>
        <tr>
          <th>Session</th>
          <th>Role</th>
          <th>Activity</th>
          <th>Task</th>
        </tr>
      </thead>
      <tbody>
        {sessions.map((s) => (
          <tr key={s.sid}>
            <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem" }}>
              {s.pinned && "📌 "}
              {s.sid}
            </td>
            <td>{sessionRoleLabel(sessionRole(s))}</td>
            <td>{sessionActivity(s)}</td>
            <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem" }}>
              {s.task_id ? (
                <Link to={`/p/${slug}/t/${s.task_id}`}>{s.task_id}</Link>
              ) : (
                "—"
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * T-0572 (Occam pass, D-0046): the standalone /scheduler page merged into the
 * transparency surface, now riding along onto the project home. Own fetch +
 * own error chrome so a scheduler hiccup never blanks the rest of the panel.
 */
function SchedulerPanel() {
  const [state, setState] = useState<SchedulerState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .scheduler()
        .then((s) => {
          if (!cancelled) {
            setState(s);
            setError(null);
          }
        })
        .catch((e) => {
          if (!cancelled) setError(String(e));
        });
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const heartbeatAge = state?.last_heartbeat_age_seconds ?? null;
  const heartbeatOk = heartbeatAge !== null && heartbeatAge < 120;

  if (error)
    return (
      <div className="text-muted" style={{ fontSize: "0.85rem" }}>
        Scheduler state unavailable: {error}
      </div>
    );
  if (state === null)
    return <div className="mc-loading">Loading scheduler state</div>;

  return (
    <>
      <div style={{ fontSize: "0.82rem", marginBottom: "0.5rem" }}>
        <span className={heartbeatOk ? "mc-dot mc-dot-active" : "mc-dot mc-dot-error"} />{" "}
        <span className={`mc-badge ${heartbeatOk ? "mc-badge-ok" : "mc-badge-danger"}`}>
          {heartbeatOk ? "worker healthy" : "worker heartbeat stale"}
        </span>{" "}
        <span style={{ color: "var(--mc-text-dim)", fontFamily: "var(--mc-mono)", fontSize: "0.74rem" }}>
          {heartbeatAge !== null ? `last heartbeat ${Math.floor(heartbeatAge)}s ago` : "no heartbeat yet"}
          {" · "}
          {state.jobs.length} scheduled job{state.jobs.length === 1 ? "" : "s"}
        </span>
      </div>
      {state.jobs.length > 0 && (
        <table className="table table-sm mc-table" style={{ fontSize: "0.78rem" }}>
          <thead>
            <tr>
              <th>Job</th>
              <th>Trigger</th>
              <th>Next run</th>
            </tr>
          </thead>
          <tbody>
            {state.jobs.map((job) => (
              <tr key={job.id}>
                <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem" }}>{job.id}</td>
                <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>
                  {job.trigger}
                </td>
                <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>
                  <span title={job.next_run ?? undefined}>{fmtFuture(job.next_run)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

/**
 * The pure, data-driven body — every section is a function of the already-
 * loaded transparency payload, so it renders without any fetch (the fetch +
 * loading/error chrome and the self-fetching SchedulerPanel live in
 * {@link ObservabilityPanel}). Exported so the render can be exercised
 * directly against a real-derived payload.
 */
export function ObservabilityView({
  data,
  slug,
}: {
  data: TransparencyData;
  slug: string;
}) {
  // The payload carries the FULL session history (341 rows on the live
  // install, ~97% suspended/archived). The design brief asks for the LIVE
  // who-does-what — the working set. Curation of the full history is the
  // Processes page's job.
  const live = data.sessions.filter((s) => !s.archived && s.status === "active");
  return (
    <>
      {/* 1 — the always-visible summary strip (quota/pace + session count) */}
      <SummaryStrip quota={data.quota} liveSessions={live.length} />

      {/* 2 — who does what (the T-0511 session tree, live rows only) */}
      <DetailSection title={`Who does what (${live.length})`}>
        <SessionTree sessions={live} slug={slug} />
        <div className="text-muted" style={{ fontSize: "0.74rem" }}>
          Full session history and controls live on the{" "}
          <Link to={`/p/${slug}/sessions`}>Processes page</Link>.
        </div>
      </DetailSection>

      {/* 3 — operator state-doc (T-0473) */}
      <DetailSection title="Operator state-doc">
        {data.operator_state.exists && data.operator_state.content ? (
          <div
            style={{
              padding: "1rem",
              border: "1px solid var(--mc-border)",
              borderRadius: "6px",
              background: "var(--mc-surface)",
            }}
          >
            <div
              className="text-muted mb-2"
              style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }}
            >
              {data.operator_state.path} · updated{" "}
              {fmtRel(data.operator_state.updated_at)}
            </div>
            <Markdown source={data.operator_state.content} slug={slug} />
          </div>
        ) : (
          <div className="alert alert-secondary" style={{ fontSize: "0.85rem" }}>
            The operator hasn&apos;t written a state-doc yet{" "}
            (<code>{data.operator_state.path}</code>). It is written on autocompact
            and on major changes; until then there is no carried state to show.
          </div>
        )}
      </DetailSection>
    </>
  );
}

/**
 * Fetch wrapper rendered at the top of the project home (Project.tsx). One
 * transparency read per mount (the page-level cadence the Transparency page
 * had); the scheduler section keeps its own 30s poll.
 */
export function ObservabilityPanel({ slug }: { slug: string }) {
  const [data, setData] = useState<TransparencyData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .transparency(slug)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  return (
    <div style={{ marginBottom: "1rem" }}>
      {error && (
        <div className="text-muted" style={{ fontSize: "0.85rem" }}>
          System state unavailable: {error}
        </div>
      )}
      {data === null && !error && (
        <div className="mc-loading">Loading system state</div>
      )}
      {data && <ObservabilityView data={data} slug={slug} />}

      {/* scheduler (merged from the retired /scheduler page, T-0572) */}
      <DetailSection title="Scheduler">
        <SchedulerPanel />
      </DetailSection>
    </div>
  );
}
