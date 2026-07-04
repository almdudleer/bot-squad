import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  type SchedulerState,
  type Transparency as TransparencyData,
  type SessionRow,
} from "../api";
import { Markdown } from "../components/Markdown";
import { PageHelp } from "../components/PageHelp";
import { RouteSkeleton } from "../components/RouteSkeleton";
import {
  sessionActivity,
  sessionRole,
  sessionRoleLabel,
} from "../utils/sessionStatus";

/**
 * T-0511 (M11 / F11.4) — unified read-only system-transparency surface.
 *
 *   "the whole system is completely transparent to the user, and he can drive
 *    the system from anywhere" — clarification-03.
 *
 * ONE page that shows where the system IS, readable WITHOUT talking to the
 * operator: the operator state-doc (T-0473) + the session tree + the backlog +
 * the quota/pace (T-0482). Strictly read-only — every write affordance lives on
 * its own page (Board, Processes, settings); this view only aggregates.
 */

const STATUS_ORDER = [
  "planned",
  "open",
  "in_progress",
  "totest",
  "reopened",
  "closed",
] as const;

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

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontFamily: "var(--mc-mono)",
        fontSize: "0.66rem",
        color: "var(--mc-text-dim)",
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        margin: "1.4rem 0 0.5rem",
      }}
    >
      {children}
    </div>
  );
}

function QuotaPanel({ quota }: { quota: TransparencyData["quota"] }) {
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
 * T-0572 (Occam pass, D-0046): the standalone /scheduler page merged in here
 * as one more read-only section — worker heartbeat + the time-driven job
 * table. Own fetch + own error chrome so a scheduler hiccup never blanks the
 * rest of the system-state view.
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

export function Transparency() {
  const { slug = "" } = useParams();
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
    <div className="container py-4">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
          System state
          <span
            style={{
              fontFamily: "var(--mc-mono)",
              fontWeight: 400,
              color: "var(--mc-text-dim)",
              fontSize: "0.78rem",
              marginLeft: "0.5rem",
            }}
          >
            / {slug}
          </span>
        </h2>
      </div>

      <PageHelp>
        The whole system in one read-only view — the operator&apos;s
        future-focused state-doc, the live session tree, the backlog, and the
        quota/pace. It is meant to be readable <strong>without talking to the
        operator</strong>: a fresh operator or the stakeholder can see where the
        project IS and where it&apos;s going from here. Nothing on this page
        writes — use the Board, Processes, and settings for changes.
      </PageHelp>

      {error && <div className="alert alert-danger">{error}</div>}
      {data === null && !error && <RouteSkeleton />}

      {data && <TransparencyView data={data} slug={slug} />}

      {/* 5 — scheduler (merged from the retired /scheduler page, T-0572) */}
      <SectionTitle>Scheduler</SectionTitle>
      <SchedulerPanel />
    </div>
  );
}

/**
 * The pure, data-driven body of the transparency surface — every section is a
 * function of the already-loaded payload, so it renders without any fetch (the
 * fetch + loading/error chrome lives in {@link Transparency}). Exported so the
 * render can be exercised directly against a real-derived payload.
 */
export function TransparencyView({
  data,
  slug,
}: {
  data: TransparencyData;
  slug: string;
}) {
  return (
    <>
      {/* 1 — operator state-doc (T-0473) */}
      <SectionTitle>Operator state-doc</SectionTitle>
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

      {/* 2 — quota / pace (T-0482) */}
      <SectionTitle>Quota &amp; pace</SectionTitle>
      <QuotaPanel quota={data.quota} />

      {/* 3 — session tree */}
      <SectionTitle>Session tree</SectionTitle>
      <SessionTree sessions={data.sessions} slug={slug} />

      {/* 4 — backlog */}
      <SectionTitle>Backlog</SectionTitle>
      <div className="mc-an-cards">
        {STATUS_ORDER.map((st) => (
          <div className="mc-an-card" key={st}>
            <div className="mc-an-card-label">{st.toUpperCase()}</div>
            <div className="mc-an-card-value">{data.backlog.counts[st] ?? 0}</div>
          </div>
        ))}
      </div>
      <div className="text-muted mt-2" style={{ fontSize: "0.78rem" }}>
        {data.backlog.tasks.length} tasks total ·{" "}
        <Link to={`/p/${slug}`}>open the Board</Link> to act on them.
      </div>
    </>
  );
}
