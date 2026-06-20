/**
 * T-0210: resource-telemetry panel for the project page.
 *
 * Surfaces the worker-sampled per-session context-token usage (against the
 * tunable contract ceiling the worker stamps per session), per-agent memory
 * footprint, and the project-level
 * quota burndown estimate (burn rate + optional projection + 429 throttle
 * flag). Polls /telemetry on the same 10s cadence as the sessions map.
 *
 * Quota is NOT live-queryable on Max plan: burn is estimated from output
 * tokens and the projection only resolves when the operator sets a budget
 * anchor — the panel says so explicitly rather than faking a number.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useApiClient } from "../apiContext";
import type { TelemetryResponse, TelemetrySession } from "../api";

function fmtTokens(n: number): string {
  // T-0267: roll up large values into M/B tiers so multi-million burns are
  // glanceable — a 7,479,374/hr burn now reads "7.5M" instead of "7479k".
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e11 ? 0 : 1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k`;
  return String(n);
}

// T-0269: a row sampled days ago must not look identical to a fresh live one.
// Render the worker's `sampled_at` as a relative age + flag staleness so an
// operator never trusts a stale context%. `ageSec` lets the caller dim/strike.
const STALE_AFTER_SEC = 60; // > ~6 poll cycles (10s cadence) ⇒ likely stale
function relativeSampled(raw: string | null | undefined): { label: string; ageSec: number | null } {
  if (!raw) return { label: "—", ageSec: null };
  const ts = Date.parse(raw);
  if (isNaN(ts)) return { label: "—", ageSec: null };
  const ageSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (ageSec < 60) return { label: `${ageSec}s ago`, ageSec };
  const m = Math.floor(ageSec / 60);
  if (m < 60) return { label: `${m}m ago`, ageSec };
  const h = Math.floor(m / 60);
  if (h < 24) return { label: `${h}h ago`, ageSec };
  return { label: `${Math.floor(h / 24)}d ago`, ageSec };
}

/** Bucket a context % into a badge colour: <80 ok, 80–100 warn, >=100 danger. */
function contextKind(pct: number): "ok" | "warn" | "danger" {
  if (pct >= 100) return "danger";
  if (pct >= 80) return "warn";
  return "ok";
}

function ContextBar({ s, ceiling }: { s: TelemetrySession; ceiling: number }) {
  // T-0264: normalize the bar width + % to the single header ceiling rather than
  // the row's own (possibly stale) s.context.ceiling. Older sessions retain the
  // pre-700k ceiling the worker stamped, so a mix of 500k/700k denominators was
  // rendering under one "/700k" header — bars weren't comparable. The header
  // ceiling (max across rows) is the live contract ceiling, so pct = tokens /
  // headerCeiling gives every row one denominator (and corrects the staleness).
  const pct = ceiling > 0 ? (s.context.tokens / ceiling) * 100 : 0;
  const kind = contextKind(pct);
  const barColor =
    kind === "danger" ? "var(--mc-danger, #d33)"
      : kind === "warn" ? "var(--mc-warn, #e0a000)"
        : "var(--mc-ok, #3a8)";
  return (
    <div style={{ minWidth: 120 }}>
      <div
        title={`${s.context.tokens.toLocaleString()} / ${ceiling.toLocaleString()} tokens`}
        style={{
          position: "relative", height: 8, borderRadius: 2,
          background: "var(--mc-border)", overflow: "hidden",
        }}
      >
        <div style={{
          position: "absolute", inset: 0, width: `${Math.min(100, pct)}%`,
          background: barColor,
        }} />
      </div>
      <div style={{ fontSize: "0.62rem", color: "var(--mc-muted, #888)", marginTop: 2 }}>
        {fmtTokens(s.context.tokens)} · {Math.round(pct)}%
      </div>
    </div>
  );
}

export function TelemetryPanel({ slug }: { slug: string }) {
  const api = useApiClient();
  const [data, setData] = useState<TelemetryResponse | null>(null);
  // T-0268: default COLLAPSED so this secondary "resource" widget doesn't push
  // the primary sessions table (and its toolbar) below the fold — the panel
  // listed every sampled session expanded-by-default, filling a laptop
  // viewport. The burn/throttle summary stays visible in the header; the toggle
  // re-expands the per-session rows on demand.
  const [collapsed, setCollapsed] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api.telemetry(slug)
        .then((d) => { if (!cancelled) setData(d); })
        .catch(() => { /* silent — panel just shows nothing until next poll */ });
    };
    load();
    const id = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [slug, api]);

  // Nothing sampled yet (worker hasn't ticked / no live sessions) → hide.
  if (!data || (data.sessions.length === 0 && !data.quota?.burn_tokens_per_hr)) {
    return null;
  }

  const q = data.quota || {};
  // T-0264: sort by raw tokens so row order matches the now-normalized bar
  // lengths (sorting by the per-row pct would order rows against a denominator
  // the bars no longer use).
  const sessions = [...data.sessions].sort(
    (a, b) => (b.context?.tokens ?? 0) - (a.context?.tokens ?? 0),
  );
  // T-0230: derive the context-column ceiling from the payload itself rather
  // than hard-coding "500k" — the contract ceiling is tunable (raised to 700k)
  // and the worker stamps the live value on every session, so the header label
  // tracks it automatically and never goes stale again.
  const ceiling = Math.max(0, ...sessions.map((s) => s.context?.ceiling ?? 0));
  const ceilingLabel = ceiling > 0 ? fmtTokens(ceiling) : "—";

  return (
    <div
      className="mc-card"
      style={{
        border: "1px solid var(--mc-border)", borderRadius: 4,
        padding: "0.5rem 0.75rem", marginBottom: "0.75rem",
        fontSize: "0.72rem",
      }}
    >
      <div className="d-flex justify-content-between align-items-center" style={{ gap: "0.5rem" }}>
        <div className="d-flex align-items-center" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
          <strong style={{ fontSize: "0.74rem" }}>Resource telemetry</strong>
          {q.throttled && (
            <span className="mc-badge mc-badge-danger" title="A session hit a 429 rate-limit">
              🚫 rate-limited
            </span>
          )}
          {/* T-0266: label burn as an INSTANTANEOUS spot estimate, not a stable
              rate. The backend figure is Δ output-tokens over a ~30-min window
              ×3600/dt, so it swings sharply (observed 7.5M/hr → 500k/hr between
              polls) when a session's cumulative counter jumps — e.g. a resume
              re-reads its transcript. The "~ / spot" wording + tooltip tell the
              operator not to read it as an actionable rate. True smoothing
              (EWMA / counter-reset clamp) lives in the backend (telemetry.py
              compute_burn) and is out of scope for this FE-clarity fix. */}
          <span
            className="mc-badge mc-badge-dim"
            title="Instantaneous output-token burn — a volatile spot estimate (Δ output tokens over a ~30-min window, ×3600). It can swing sharply when a session's cumulative counter jumps (e.g. a resume re-reads its transcript), so treat it as a rough estimate, not a smoothed rate. Set a budget anchor in system_settings [quota] to turn it into an exhaustion projection."
          >
            burn {q.burn_tokens_per_hr != null
              ? <>~{fmtTokens(Math.round(q.burn_tokens_per_hr))}/hr <span style={{ opacity: 0.7 }}>· spot</span></>
              : "—"}
          </span>
          {q.projected_exhaustion_at ? (
            <span className="mc-badge mc-badge-warn" title="Projected against the operator-set budget anchor">
              exhausts ~{q.projected_exhaustion_at.replace("T", " ").replace("Z", "")} UTC
            </span>
          ) : (
            <span
              className="mc-badge mc-badge-dim"
              title="No exhaustion projection: a budget anchor is not set. The burn badge is only an instantaneous spot estimate — set a budget anchor in system_settings [quota] to project exhaustion."
            >
              no projection (set budget anchor)
            </span>
          )}
        </div>
        <button
          type="button"
          className="btn btn-link btn-sm p-0"
          style={{ fontSize: "0.68rem", textDecoration: "none" }}
          onClick={() => setCollapsed((c) => !c)}
        >
          {collapsed ? `▸ ${sessions.length} sessions` : "▾ hide"}
        </button>
      </div>

      {!collapsed && sessions.length > 0 && (
        <table style={{ width: "100%", marginTop: "0.4rem", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: "var(--mc-muted, #888)", fontSize: "0.62rem", textAlign: "left" }}>
              <th style={{ fontWeight: 500, padding: "1px 6px 1px 0" }}>session</th>
              <th style={{ fontWeight: 500, padding: "1px 6px" }}>context (/{ceilingLabel})</th>
              <th style={{ fontWeight: 500, padding: "1px 6px" }}>memory</th>
              <th style={{ fontWeight: 500, padding: "1px 6px" }}>sampled</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => {
              // T-0269: link the SID to the matching table row below via the
              // existing ?sid= deep-link (scroll-to + flash) — the same target
              // the sessions-table SID resolves to, so a hot telemetry row is
              // now clickable instead of dead plain text.
              const sampled = relativeSampled(s.sampled_at);
              const stale = sampled.ageSec != null && sampled.ageSec > STALE_AFTER_SEC;
              return (
                <tr key={s.sid} style={{ borderTop: "1px solid var(--mc-border)" }}>
                  <td style={{ padding: "3px 6px 3px 0", fontFamily: "var(--mc-mono)", whiteSpace: "nowrap" }}>
                    {s.rate_limited && <span title="hit a 429">🚫 </span>}
                    <Link
                      to={{ search: `?sid=${encodeURIComponent(s.sid)}` }}
                      style={{ color: "var(--mc-accent)", textDecoration: "none" }}
                      title="Jump to this session in the table below"
                    >
                      {s.sid}
                    </Link>
                  </td>
                  <td style={{ padding: "3px 6px" }}><ContextBar s={s} ceiling={ceiling} /></td>
                  <td style={{ padding: "3px 6px", whiteSpace: "nowrap" }}>
                    {fmtTokens(s.memory.tokens_est)} tok
                    <span style={{ color: "var(--mc-muted, #888)" }}> · {s.memory.files}f</span>
                  </td>
                  <td
                    style={{
                      padding: "3px 6px", whiteSpace: "nowrap",
                      color: stale ? "var(--mc-warn, #e0a000)" : "var(--mc-muted, #888)",
                      textDecoration: stale ? "line-through" : undefined,
                    }}
                    title={
                      stale
                        ? `Stale: last sampled ${sampled.label} (> ${STALE_AFTER_SEC}s) — context% may be out of date`
                        : `Last sampled ${sampled.label}`
                    }
                  >
                    {sampled.label}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
