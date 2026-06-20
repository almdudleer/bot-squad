import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type Analytics as AnalyticsData, type DayCount } from "../api";
import { PageHelp } from "../components/PageHelp";
import { LIVE_STATUSES } from "../utils/sessionStatus";

/**
 * T-0147 — product-analytics dashboard (internal-usage / "B" variant).
 *
 * A thin, read-only view of bot-squad's own operational numbers, computed
 * server-side from the project's data dir (sessions, backlog, deploy jobs).
 * Intentionally slim: KPI cards + a few sparkline-ish bar charts, no
 * date-range controls or drill-downs yet.
 */

function fmtRelDays(iso: string | null): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function StatCard({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "ok" | "warn" | "danger";
}) {
  const color =
    tone === "ok"
      ? "var(--mc-green)"
      : tone === "warn"
        ? "var(--mc-amber)"
        : tone === "danger"
          ? "var(--mc-red)"
          : "var(--mc-text)";
  return (
    <div className="mc-an-card">
      <div className="mc-an-card-label">{label}</div>
      <div className="mc-an-card-value" style={{ color }}>
        {value}
      </div>
      {sub && <div className="mc-an-card-sub">{sub}</div>}
    </div>
  );
}

/** Vertical bar chart over a day/week series. */
function BarChart({
  title,
  data,
  color = "var(--mc-cyan)",
}: {
  title: string;
  data: { label: string; value: number; tip: string }[];
  color?: string;
}) {
  const max = Math.max(1, ...data.map((d) => d.value));
  const total = data.reduce((a, d) => a + d.value, 0);
  return (
    <div className="mc-an-chart">
      <div className="mc-an-chart-head">
        <span>{title}</span>
        <span className="mc-an-chart-total">{total} total</span>
      </div>
      <div className="mc-an-bars">
        {data.map((d, i) => (
          <div className="mc-an-bar-col" key={i} title={d.tip}>
            <div className="mc-an-bar-track">
              <div
                className="mc-an-bar-fill"
                style={{
                  height: `${(d.value / max) * 100}%`,
                  background: color,
                  opacity: d.value === 0 ? 0.15 : 1,
                }}
              />
            </div>
            <div className="mc-an-bar-label">{d.label}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Horizontal breakdown of a status→count map. */
function StatusBreakdown({
  title,
  counts,
  palette,
}: {
  title: string;
  counts: Record<string, number>;
  palette: Record<string, string>;
}) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, c]) => c));
  return (
    <div className="mc-an-chart">
      <div className="mc-an-chart-head">
        <span>{title}</span>
      </div>
      <div className="mc-an-status-list">
        {entries.length === 0 && <div className="mc-an-card-sub">no data</div>}
        {entries.map(([k, c]) => (
          <div className="mc-an-status-row" key={k}>
            <span className="mc-an-status-name">{k}</span>
            <span className="mc-an-status-track">
              <span
                className="mc-an-status-fill"
                style={{
                  width: `${(c / max) * 100}%`,
                  background: palette[k] ?? "var(--mc-text-dim)",
                }}
              />
            </span>
            <span className="mc-an-status-count">{c}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const TICKET_PALETTE: Record<string, string> = {
  closed: "var(--mc-green)",
  totest: "var(--mc-cyan)",
  in_progress: "var(--mc-amber)",
  reopened: "var(--mc-red)",
  open: "var(--mc-text-dim)",
  planned: "var(--mc-text-faint)",
};

// T-0340: the canonical liveness vocabulary is live | suspended (archived is
// an orthogonal flag shown parenthetically). The breakdown rolls the raw md
// statuses up to that category so the chart can't disagree with the headline
// "N live · M suspended" — see livenessRollup below.
const SESSION_PALETTE: Record<string, string> = {
  live: "var(--mc-green)",
  suspended: "var(--mc-text-dim)",
};

// Roll the per-status session counts up to the canonical liveness category
// (T-0340). `live` sums the LIVE_STATUSES (active + paused); everything else
// that isn't archived is `suspended`. Keyed identically to SESSION_PALETTE so
// the "Sessions by status" breakdown and the headline read off ONE truth.
function livenessRollup(byStatus: Record<string, number>): {
  live: number;
  suspended: number;
} {
  let live = 0;
  let suspended = 0;
  for (const [status, count] of Object.entries(byStatus)) {
    if (LIVE_STATUSES.has(status)) live += count;
    else suspended += count;
  }
  return { live, suspended };
}

function dayLabel(iso: string): string {
  // "2026-05-27" → "05/27"
  const [, m, d] = iso.split("-");
  return `${m}/${d}`;
}

function toBars(series: DayCount[]): { label: string; value: number; tip: string }[] {
  return series.map((d) => ({
    label: dayLabel(d.date),
    value: d.count,
    tip: `${d.date}: ${d.count}`,
  }));
}

export function Analytics() {
  const { slug = "" } = useParams();
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .analytics(slug)
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
          Analytics
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
        {data && (
          <span
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.72rem",
              color: "var(--mc-text-dim)",
            }}
          >
            last {data.window_days}d window
          </span>
        )}
      </div>

      <PageHelp>
        Internal-usage analytics for bot-squad itself — sessions, ticket flow,
        and deploys computed server-side from this project&apos;s data dir.
        Numbers are live (recomputed each load); the day/week charts cover a
        trailing window.
      </PageHelp>

      {error && <div className="alert alert-danger">{error}</div>}
      {data === null && !error && (
        <div className="mc-loading">Loading analytics</div>
      )}

      {data && (
        <>
          <div className="mc-an-cards">
            <StatCard
              label="SESSIONS"
              value={String(data.sessions.total)}
              // Sub-counts partition the headline total along the canonical
              // liveness category (T-0340): live (running|idle|paused) +
              // suspended === total. `archived` is an orthogonal frontmatter
              // flag (a subset that mostly overlaps suspended), so it stays a
              // parenthetical annotation rather than a third additive bucket.
              // "live" is the SAME word the sidebar/Sessions board use, and the
              // count rolls up via livenessRollup so it can't disagree with the
              // "Sessions by status" breakdown below. (T-0256 / T-0340)
              sub={(() => {
                const { live, suspended } = livenessRollup(
                  data.sessions.by_status,
                );
                return `${live} live · ${suspended} suspended (${data.sessions.archived} archived)`;
              })()}
            />
            <StatCard
              label="TICKETS CLOSED"
              value={String(data.tickets.by_status.closed ?? 0)}
              sub={`of ${data.tickets.total} total`}
              tone="ok"
            />
            <StatCard
              label="DEPLOYS"
              value={String(data.deploys.total)}
              sub={
                data.deploys.success_rate === null
                  ? "no deploys yet"
                  : `${Math.round(data.deploys.success_rate * 100)}% ok · last ${fmtRelDays(
                      data.deploys.last_deploy_at,
                    )}`
              }
              tone={
                data.deploys.success_rate === null
                  ? undefined
                  : data.deploys.success_rate >= 0.8
                    ? "ok"
                    : "warn"
              }
            />
            <StatCard
              label="TIME-TO-CLOSE"
              value={
                data.tickets.time_to_close.median_days === null
                  ? "—"
                  : `${data.tickets.time_to_close.median_days}d`
              }
              sub={
                data.tickets.time_to_close.median_days === null
                  ? "no dated closes"
                  : `median · mean ${data.tickets.time_to_close.mean_days}d (n=${data.tickets.time_to_close.closed_measured})`
              }
            />
          </div>

          <div className="mc-an-grid">
            <BarChart
              title="Sessions started / day"
              data={toBars(data.sessions.per_day)}
              color="var(--mc-cyan)"
            />
            <BarChart
              title="Tickets closed / day"
              data={toBars(data.tickets.closed_per_day)}
              color="var(--mc-green)"
            />
            <BarChart
              title="Deploys / week"
              data={data.deploys.per_week.map((w) => ({
                label: w.week.replace(/^\d+-/, ""),
                value: w.ok + w.fail,
                tip: `${w.week}: ${w.ok} ok, ${w.fail} fail`,
              }))}
              color="var(--mc-amber)"
            />
          </div>

          <div className="mc-an-grid">
            <StatusBreakdown
              title="Tickets by status"
              counts={data.tickets.by_status}
              palette={TICKET_PALETTE}
            />
            <StatusBreakdown
              title="Sessions by liveness"
              counts={livenessRollup(data.sessions.by_status)}
              palette={SESSION_PALETTE}
            />
          </div>
        </>
      )}
    </div>
  );
}
