import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type Analytics as AnalyticsData, type DayCount } from "../api";
import { RouteSkeleton } from "../components/RouteSkeleton";
import { LIVE_STATUSES } from "../utils/sessionStatus";

/**
 * T-0147 — product-analytics dashboard (internal-usage / "B" variant).
 *
 * A thin, read-only view of bot-squad's own operational numbers, computed
 * server-side from the project's data dir (sessions, backlog, deploy jobs).
 * Intentionally slim: KPI cards + a few sparkline-ish bar charts, no
 * date-range controls or drill-downs yet.
 */

// T-0428 (dogfood): a sub-day duration like "0.16d" is hard to read. Show
// hours under a day (and minutes under an hour), days otherwise.
function fmtDuration(days: number | null): string {
  if (days === null) return "—";
  if (days >= 1) return `${days}d`;
  const hrs = days * 24;
  if (hrs >= 1) return `${hrs.toFixed(1)}h`;
  return `${Math.max(1, Math.round(hrs * 60))}m`;
}

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

// T-0360: per-widget time-scope label. The page mixed three time semantics
// (all-time totals, a 14d day window, an 8-week deploy window) under one global
// "last 14d window" header — misleading. Each widget now states its own scope.
function ScopeLabel({ scope }: { scope: string }) {
  return (
    <span
      style={{ fontFamily: "var(--mc-mono)", fontSize: "0.64rem", color: "var(--mc-text-dim)", fontWeight: 400 }}
      title="Time scope of this widget"
    >
      {scope}
    </span>
  );
}

/** Vertical bar chart over a day/week series. */
function BarChart({
  title,
  data,
  color = "var(--mc-cyan)",
  scope,
}: {
  title: string;
  data: { label: string; value: number; tip: string }[];
  color?: string;
  scope?: string;
}) {
  const max = Math.max(1, ...data.map((d) => d.value));
  const total = data.reduce((a, d) => a + d.value, 0);
  return (
    <div className="mc-an-chart">
      <div className="mc-an-chart-head">
        <span>{title}{scope && <> · <ScopeLabel scope={scope} /></>}</span>
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

// T-0340: the canonical liveness vocabulary is live | suspended (archived is
// an orthogonal flag shown parenthetically).
// Roll the per-status session counts up to that category. `live` sums the
// LIVE_STATUSES (active + paused); everything else that isn't archived is
// `suspended`.
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
      </div>

      {error && <div className="alert alert-danger">{error}</div>}
      {/* T-0365: meaningful skeleton while the analytics payload loads. */}
      {data === null && !error && <RouteSkeleton />}

      {data && (
        <>
          <div style={{ fontFamily: "var(--mc-mono)", fontSize: "0.66rem", color: "var(--mc-text-dim)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "0.4rem" }}>
            All-time totals
          </div>
          <div className="mc-an-cards">
            <StatCard
              label="PROCESSES"
              value={String(data.sessions.total)}
              // Sub-counts partition the headline total along the canonical
              // liveness category (T-0340): live (running|idle|paused) +
              // suspended === total. `archived` is an orthogonal frontmatter
              // flag (a subset that mostly overlaps suspended), so it stays a
              // parenthetical annotation rather than a third additive bucket.
              // "live" is the SAME word the sidebar/Sessions board use.
              // (T-0256 / T-0340)
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
              value={fmtDuration(data.tickets.time_to_close.median_days)}
              sub={
                data.tickets.time_to_close.median_days === null
                  ? "no dated closes"
                  : `median · mean ${fmtDuration(data.tickets.time_to_close.mean_days)} (n=${data.tickets.time_to_close.closed_measured})`
              }
            />
          </div>

          <div className="mc-an-grid">
            <BarChart
              title="Processes started / day"
              scope={`last ${data.window_days}d`}
              data={toBars(data.sessions.per_day)}
              color="var(--mc-cyan)"
            />
            <BarChart
              title="Tickets closed / day"
              scope={`last ${data.window_days}d`}
              data={toBars(data.tickets.closed_per_day)}
              color="var(--mc-green)"
            />
            <BarChart
              title="Deploys / week"
              scope={`last ${data.deploy_weeks ?? 8} weeks`}
              data={data.deploys.per_week.map((w) => ({
                label: w.week.replace(/^\d+-/, ""),
                value: w.ok + w.fail,
                tip: `${w.week}: ${w.ok} ok, ${w.fail} fail`,
              }))}
              color="var(--mc-amber)"
            />
          </div>
        </>
      )}
    </div>
  );
}
