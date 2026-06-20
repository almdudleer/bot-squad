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

import { useApiClient } from "../apiContext";
import type { TelemetryResponse, TelemetrySession } from "../api";

function fmtTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k`;
  return String(n);
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
  const [collapsed, setCollapsed] = useState(false);

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
          <span className="mc-badge mc-badge-dim" title="Estimated output-token burn across live sessions">
            burn {q.burn_tokens_per_hr != null ? `${fmtTokens(Math.round(q.burn_tokens_per_hr))}/hr` : "—"}
          </span>
          {q.projected_exhaustion_at ? (
            <span className="mc-badge mc-badge-warn" title="Projected against the operator-set budget anchor">
              exhausts ~{q.projected_exhaustion_at.replace("T", " ").replace("Z", "")} UTC
            </span>
          ) : (
            <span className="mc-badge mc-badge-dim" title="Set a budget anchor in system_settings [quota] to project exhaustion">
              projection: anchor unset
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
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.sid} style={{ borderTop: "1px solid var(--mc-border)" }}>
                <td style={{ padding: "3px 6px 3px 0", fontFamily: "var(--mc-mono)", whiteSpace: "nowrap" }}>
                  {s.rate_limited && <span title="hit a 429">🚫 </span>}
                  {s.sid}
                </td>
                <td style={{ padding: "3px 6px" }}><ContextBar s={s} ceiling={ceiling} /></td>
                <td style={{ padding: "3px 6px", whiteSpace: "nowrap" }}>
                  {fmtTokens(s.memory.tokens_est)} tok
                  <span style={{ color: "var(--mc-muted, #888)" }}> · {s.memory.files}f</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
