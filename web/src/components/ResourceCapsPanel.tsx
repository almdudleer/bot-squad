/**
 * T-0339 (reframe Pillar A item 5 + T-0306): consolidated caps/budget control
 * for the process (sessions) view.
 *
 * The caps/budget story used to be split across three places — System Settings
 * "Resource caps", the per-session telemetry burn/anchor bar, and nothing in
 * Project settings. The reframe makes resource caps the operator's core
 * Task-Manager controls on the project brain, so this panel surfaces a read+set
 * affordance RIGHT IN the process view where you watch and constrain the brain.
 * Server-level enforcement stays the source of truth underneath (the worker
 * enforces both caps at spawn-time; this panel reads/writes the same
 * /api/system-settings the admin page does).
 *
 * ONE coherent model — caps ↔ budget anchor reconciled:
 *   • Resource caps = the hard limits the system enforces at spawn-time
 *     (server-wide): max simultaneously-live sessions + a per-quota-period
 *     output-token budget.
 *   • Budget anchor = the baseline both the token cap and the burn→exhaustion
 *     projection key off. The token cap is "output tokens since the last
 *     anchor" and FREES when the anchor is reset; the burn estimate only
 *     resolves into an exhaustion projection once an anchor is set.
 * The anchor is set server-side in system_settings [quota] (no FE setter yet —
 * the worker owns it); this panel surfaces its STATUS and the relationship.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import type { TelemetryQuota, TelemetryCaps } from "../api";
import {
  PARALLEL_SESSION_CEILING,
  ProjectUtilization,
  aggregateUtilization,
  capInputError,
  capSoftWarning,
  isOverCap,
  sanitizeCapInput,
  utilizationRatio,
} from "../pages/resourceCaps";

// Roll large token counts into k/M/B tiers (mirrors TelemetryPanel.fmtTokens).
function fmtTokens(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e11 ? 0 : 1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k`;
  return String(n);
}

/**
 * Task-Manager-style utilization meter — current usage vs the configured cap.
 * `used === null` while utilization is still loading; an unlimited cap (0)
 * renders no bar fill and an "Unlimited" target.
 *
 * Exported so SystemSettings.tsx reuses the SAME bar (DRY — T-0339 moved it out
 * of that page so the admin view and the process view can't drift).
 */
export function CapMeter({
  label,
  used,
  cap,
  format = (n: number) => n.toLocaleString(),
}: {
  label: string;
  used: number | null;
  cap: number;
  format?: (n: number) => string;
}) {
  const ratio = used === null ? null : utilizationRatio(used, cap);
  const over = used !== null && isOverCap(used, cap);
  const fill = ratio === null ? 0 : Math.min(100, ratio * 100);
  const barColor = over
    ? "var(--mc-accent-danger, #d33)"
    : fill >= 80
      ? "var(--mc-accent-warn, #e0a000)"
      : "var(--mc-accent, #2f6feb)";
  return (
    <div style={{ marginBottom: "0.6rem" }}>
      <div
        className="d-flex justify-content-between"
        style={{ fontSize: "0.72rem", marginBottom: 3 }}
      >
        <span style={{ color: "var(--mc-text-mid)" }}>{label}</span>
        <span
          style={{
            fontFamily: "var(--mc-mono)",
            color: over ? "var(--mc-accent-danger, #d33)" : "var(--mc-text-mid)",
          }}
        >
          {used === null ? "—" : format(used)} / {cap === 0 ? "Unlimited" : format(cap)}
        </span>
      </div>
      <div
        style={{
          position: "relative",
          height: 8,
          borderRadius: 2,
          background: "var(--mc-border)",
          overflow: "hidden",
        }}
        title={cap === 0 ? "Unlimited (no cap)" : `${used ?? "—"} of ${cap}`}
      >
        <div style={{ position: "absolute", inset: 0, width: `${fill}%`, background: barColor }} />
      </div>
    </div>
  );
}

/** Compact UTC stamp for the anchor / projection (drops T and Z). */
function fmtStamp(raw?: string | null): string | null {
  if (!raw) return null;
  return raw.replace("T", " ").replace("Z", "") + " UTC";
}

export function ResourceCapsPanel({ slug }: { slug: string }) {
  // Caps held as raw STRINGS so an empty field (invalid) is distinguishable from
  // an explicit "0" (= unlimited) — mirrors SystemSettings (T-0310).
  const [maxParallel, setMaxParallel] = useState<string>("0");
  const [maxTokens, setMaxTokens] = useState<string>("0");
  // T-0408: idle-suspend window (seconds; 0 = OFF).
  const [maxIdleSec, setMaxIdleSec] = useState<string>("0");
  const [loaded, setLoaded] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const [util, setUtil] = useState<{ liveSessions: number; totalTokens: number } | null>(null);
  const [quota, setQuota] = useState<TelemetryQuota | null>(null);
  // T-0389/audit items 7+22: the ENFORCED caps (server-wide) ride /telemetry —
  // available on mount (no lazy fan-out needed), drive the live-utilization
  // strip ('12/15, throttled to 8') + the token meter (output_since_anchor).
  const [caps, setCaps] = useState<TelemetryCaps | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const parallelNum = maxParallel.trim() === "" ? null : Number.parseInt(maxParallel, 10);
  const tokensNum = maxTokens.trim() === "" ? null : Number.parseInt(maxTokens, 10);
  const idleNum = maxIdleSec.trim() === "" ? null : Number.parseInt(maxIdleSec, 10);
  const parallelErr = capInputError(maxParallel, "Max parallel sessions");
  const tokensErr = capInputError(maxTokens, "Max total tokens");
  const idleErr = capInputError(maxIdleSec, "Idle-suspend window");
  const parallelWarn =
    parallelNum === null
      ? null
      : capSoftWarning(parallelNum, PARALLEL_SESSION_CEILING, "Max parallel sessions");

  // Caps + admin flag: cheap, needed for the always-visible summary header.
  useEffect(() => {
    let alive = true;
    api
      .getSystemSettings()
      .then((s) => {
        if (!alive) return;
        setMaxParallel(String(s.caps.max_parallel_sessions));
        setMaxTokens(String(s.caps.max_total_tokens));
        setMaxIdleSec(String(s.caps.idle_suspend_sec ?? 0));
        setLoaded(true);
      })
      .catch(() => {
        /* leave defaults; panel still renders the relationship copy */
      });
    api
      .me()
      .then((m) => {
        if (alive) setIsAdmin(Boolean(m.is_admin));
      })
      .catch(() => {
        /* non-admin / anon — inputs stay read-only */
      });
    // This project's quota rollup for the anchor/projection reconciliation.
    api
      .telemetry(slug)
      .then((d) => {
        if (alive) {
          setQuota(d.quota ?? null);
          setCaps(d.caps ?? null); // server-wide enforced caps (items 7+22)
        }
      })
      .catch(() => {
        /* no telemetry yet — anchor status shows "unknown/none" */
      });
    return () => {
      alive = false;
    };
  }, [slug]);

  // Server-wide utilization fan-out (caps are a server-wide policy). Lazy: only
  // fetched once the operator expands the panel, so a collapsed panel stays
  // cheap on the sessions page.
  const loadUtilization = useCallback(() => {
    api
      .projects()
      .then(async (projects) => {
        const perProject: ProjectUtilization[] = await Promise.all(
          projects.map(async (p) => ({
            sessions: await api.sessions(p.slug).catch(() => null),
            telemetry: await api.telemetry(p.slug).catch(() => null),
          })),
        );
        setUtil(aggregateUtilization(perProject));
      })
      .catch(() => {
        /* best-effort */
      });
  }, []);

  useEffect(() => {
    if (expanded && util === null) loadUtilization();
  }, [expanded, util, loadUtilization]);

  async function save() {
    setNotice(null);
    setError(null);
    const capErr = parallelErr ?? tokensErr ?? idleErr;
    if (capErr !== null) {
      setError(capErr);
      return;
    }
    setSaving(true);
    try {
      // Caps-only partial update — leaves tg/session/admin settings untouched.
      const result = await api.putSystemSettings({
        caps: { max_parallel_sessions: parallelNum ?? 0, max_total_tokens: tokensNum ?? 0, idle_suspend_sec: idleNum ?? 0 },
      });
      setMaxParallel(String(result.caps.max_parallel_sessions));
      setMaxTokens(String(result.caps.max_total_tokens));
      setMaxIdleSec(String(result.caps.idle_suspend_sec ?? 0));
      // P2-01-FE: the BE restart_required is now the source of truth (P2-01-BE,
      // 5bc63ae) — it flags true ONLY for boot-cached fields (bot_token/proxy_url)
      // and false for a caps-only save (caps are fresh-read per spawn). Trust the
      // flag again; the item-15 hardcoded 'no restart needed' band-aid that masked
      // the old unconditional flag is deleted.
      setNotice(
        result.restart_required
          ? "Saved. Restart the worker for these settings to take effect."
          : "Saved. New caps apply to the next spawn.",
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  const anchorSet = !!quota?.anchor;
  const projection = fmtStamp(quota?.projected_exhaustion_at);
  const burn = quota?.burn_tokens_per_hr;

  // T-0389 items 22+7: derive the live utilization + AIMD throttle from the
  // server-wide enforced caps. hardCap=0 means unlimited; effective_limit below
  // hardCap means the WS-4 backoff governor has throttled admission ('… → 8').
  const hardCap = caps?.max_parallel_sessions ?? parallelNum ?? 0;
  const effLimit = caps?.effective_limit ?? 0;
  const liveCount = caps?.live_sessions;
  // P2-02: hardCap==0 means UNLIMITED (∞), not "no throttle". The shipped
  // default is caps 0/0 with backoff ON, so the AIMD governor can depress an
  // otherwise-unlimited ceiling to a finite effective_limit — that IS a throttle
  // and must be visible. Treat hardCap 0 as Infinity so the badge renders
  // whenever a finite effective_limit sits below the (possibly ∞) ceiling.
  // (effective_limit 0 = unlimited/no pressure → not throttled.)
  const throttled = effLimit > 0 && effLimit < (hardCap || Infinity);
  const parallelText =
    liveCount != null
      ? `${liveCount}/${hardCap === 0 ? "∞" : hardCap}`
      : hardCap === 0
        ? "∞"
        : String(hardCap);

  return (
    <div
      className="mc-card"
      data-testid="process-resource-caps"
      style={{
        border: "1px solid var(--mc-border)",
        borderRadius: 4,
        padding: "0.5rem 0.75rem",
        marginBottom: "0.75rem",
        fontSize: "0.72rem",
      }}
    >
      {/* Always-visible summary: caps + budget-anchor status at a glance. */}
      <div
        className="d-flex justify-content-between align-items-center"
        style={{ gap: "0.5rem", flexWrap: "wrap" }}
      >
        <div className="d-flex align-items-center" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
          <strong style={{ fontSize: "0.74rem" }}>Resource caps &amp; budget</strong>
          {/* T-0389 item 22: live count / hard ceiling + AIMD throttle. */}
          <span
            className={`mc-badge ${throttled ? "mc-badge-warn" : "mc-badge-dim"}`}
            title="Live processes / max simultaneously-live allowed. T-0417: the cap value is shared config, but the live count + enforcement are per-worker-user (this Linux user's worker counts its own sessions). 0 = unlimited."
          >
            parallel {parallelText}
          </span>
          {throttled && (
            <span
              className="mc-badge mc-badge-warn"
              title="The WS-4 backoff governor has throttled admission below the hard cap (resource pressure / 429s). New spawns admit up to this effective limit until pressure clears."
            >
              throttled to {effLimit}
            </span>
          )}
          <span
            className="mc-badge mc-badge-dim"
            title="Per-quota-period output-token budget (output since the last anchor). Enforced at spawn-time; frees on anchor reset. 0 = unlimited."
          >
            tokens {tokensNum === 0 ? "∞" : tokensNum != null ? fmtTokens(tokensNum) : "—"}
          </span>
          {anchorSet ? (
            projection ? (
              <span
                className="mc-badge mc-badge-warn"
                title="Projected against the operator-set budget anchor."
              >
                exhausts ~{projection}
              </span>
            ) : (
              <span className="mc-badge mc-badge-ok" title="A budget anchor is set.">
                anchor set
              </span>
            )
          ) : (
            <span
              className="mc-badge mc-badge-dim"
              title="No budget anchor set: the token cap budget never frees on its own and the burn estimate stays a spot figure with no exhaustion projection. Set one in System Settings [quota]."
            >
              no budget anchor
            </span>
          )}
        </div>
        <button
          type="button"
          className="btn btn-link btn-sm p-0"
          style={{ fontSize: "0.68rem", textDecoration: "none" }}
          onClick={() => setExpanded((e) => !e)}
          data-testid="caps-toggle"
        >
          {expanded ? "▾ hide" : "▸ set caps"}
        </button>
      </div>

      {expanded && (
        <div style={{ marginTop: "0.5rem" }}>
          {error && (
            <div className="alert alert-danger py-1 px-2 mb-2" style={{ fontSize: "0.7rem" }}>
              {error}
            </div>
          )}
          {notice && (
            <div className="alert alert-success py-1 px-2 mb-2" style={{ fontSize: "0.7rem" }}>
              {notice}
            </div>
          )}

          {/* Task-Manager meters — the ENFORCED server-wide numbers (T-0389
              items 7+22): live count vs the cap, and output_since_anchor (the
              actual enforced numerator) vs the token budget — NOT the cumulative
              total that's never what's enforced. Falls back to the FE
              aggregation if the worker didn't supply caps. */}
          <div style={{ maxWidth: "26rem", marginBottom: "0.6rem" }}>
            <CapMeter
              label="Parallel sessions (live, this worker-user)"
              used={caps?.live_sessions ?? (util ? util.liveSessions : null)}
              cap={caps?.max_parallel_sessions ?? parallelNum ?? 0}
            />
            <CapMeter
              label="Output tokens this quota period (enforced vs budget)"
              used={caps?.output_since_anchor ?? (util ? util.totalTokens : null)}
              cap={caps?.max_total_tokens ?? tokensNum ?? 0}
              format={fmtTokens}
            />
          </div>

          {/* The one-model reconciliation: caps ↔ budget anchor. */}
          <div
            style={{
              fontSize: "0.68rem",
              color: "var(--mc-text-dim)",
              lineHeight: 1.5,
              marginBottom: "0.6rem",
            }}
          >
            <div>
              <strong>Resource caps</strong> are the hard limits the system enforces at
              spawn-time: max live sessions + a per-quota-period output-token budget. The
              cap values are shared config; the live session count + enforcement are
              <strong> per-worker-user</strong> (each Linux user's worker counts its own).{" "}
              <strong>0 = unlimited.</strong>
            </div>
            <div style={{ marginTop: "0.25rem" }}>
              The <strong>budget anchor</strong> is the baseline both the token cap and the
              burn→exhaustion projection key off: the token cap is "output since the last
              anchor" and <strong>frees when the anchor is reset</strong>; the burn estimate
              only becomes an exhaustion projection once an anchor is set.{" "}
              {anchorSet ? (
                <span>
                  An anchor is set
                  {quota?.anchor?.budget_tokens
                    ? ` (budget ${fmtTokens(quota.anchor.budget_tokens)})`
                    : ""}
                  {projection ? ` — projected exhaustion ~${projection}.` : "."}
                </span>
              ) : (
                <span>
                  No anchor is set, so the token-cap budget never frees on its own and burn
                  {burn != null ? ` (~${fmtTokens(Math.round(burn))}/hr, spot)` : ""} has no
                  projection. Set one in System Settings <code>[quota]</code>.
                </span>
              )}
            </div>
          </div>

          {/* Set affordance — admin-only, mirrors the SystemSettings validation. */}
          <div className="d-flex gap-3 align-items-start flex-wrap">
            <div>
              <label
                htmlFor="pc-cap-parallel"
                className="form-label"
                style={{ fontSize: "0.7rem" }}
              >
                Max parallel sessions
              </label>
              <input
                id="pc-cap-parallel"
                type="text"
                inputMode="numeric"
                className="form-control form-control-sm"
                data-testid="process-cap-parallel"
                value={maxParallel}
                disabled={!isAdmin || !loaded}
                aria-invalid={parallelErr !== null}
                onChange={(e) => setMaxParallel(sanitizeCapInput(e.target.value))}
                style={{ width: "9rem" }}
              />
              {parallelErr && (
                <div style={{ color: "var(--mc-accent-danger, #d33)", fontSize: "0.66rem", marginTop: 2, maxWidth: "11rem" }}>
                  {parallelErr}
                </div>
              )}
              {!parallelErr && parallelWarn && (
                <div style={{ color: "var(--mc-accent-warn, #e0a000)", fontSize: "0.66rem", marginTop: 2, maxWidth: "11rem" }}>
                  {parallelWarn}
                </div>
              )}
            </div>
            <div>
              <label htmlFor="pc-cap-tokens" className="form-label" style={{ fontSize: "0.7rem" }}>
                Max total tokens
              </label>
              <input
                id="pc-cap-tokens"
                type="text"
                inputMode="numeric"
                className="form-control form-control-sm"
                data-testid="process-cap-tokens"
                value={maxTokens}
                disabled={!isAdmin || !loaded}
                aria-invalid={tokensErr !== null}
                onChange={(e) => setMaxTokens(sanitizeCapInput(e.target.value))}
                style={{ width: "11rem" }}
              />
              {tokensErr && (
                <div style={{ color: "var(--mc-accent-danger, #d33)", fontSize: "0.66rem", marginTop: 2, maxWidth: "11rem" }}>
                  {tokensErr}
                </div>
              )}
              {/* T-0418: arming a token cap auto-anchors its budget period at
                  save (server-side), so it measures spend since you armed it —
                  not since run-start — and can't ratchet into a permanent block. */}
              {!tokensErr && (
                <div style={{ color: "var(--mc-text-dim)", fontSize: "0.64rem", marginTop: 2, maxWidth: "11.5rem" }}>
                  Arming a cap auto-anchors its budget period to now — it can't ratchet into a permanent block.
                </div>
              )}
            </div>
            {/* T-0408: idle-suspend window. */}
            <div>
              <label htmlFor="pc-cap-idle" className="form-label" style={{ fontSize: "0.7rem" }}>
                Idle-suspend window (sec)
              </label>
              <input
                id="pc-cap-idle"
                type="text"
                inputMode="numeric"
                className="form-control form-control-sm"
                data-testid="process-cap-idle"
                value={maxIdleSec}
                disabled={!isAdmin || !loaded}
                aria-invalid={idleErr !== null}
                onChange={(e) => setMaxIdleSec(sanitizeCapInput(e.target.value))}
                style={{ width: "9rem" }}
              />
              {idleErr ? (
                <div style={{ color: "var(--mc-accent-danger, #d33)", fontSize: "0.66rem", marginTop: 2, maxWidth: "11.5rem" }}>
                  {idleErr}
                </div>
              ) : (
                <div style={{ color: "var(--mc-text-dim)", fontSize: "0.64rem", marginTop: 2, maxWidth: "11.5rem" }}>
                  0 = off. 12h = 43200. Suspends an idle-but-live dev to reclaim a slot; in-progress devs are spared.
                </div>
              )}
            </div>
            <div style={{ alignSelf: "flex-end" }}>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                data-testid="process-cap-save"
                onClick={save}
                disabled={saving || !isAdmin || !loaded || parallelErr !== null || tokensErr !== null || idleErr !== null}
                title={isAdmin ? "Save caps (server-wide; fresh-read at each spawn — no restart needed)" : "Admin-only"}
              >
                {saving ? "Saving…" : "Save caps"}
              </button>
            </div>
          </div>
          <small
            style={{ display: "block", color: "var(--mc-text-dim)", marginTop: "0.4rem", fontSize: "0.66rem" }}
          >
            Caps are a <strong>server-wide</strong> policy enforced at spawn-time; restart the
            worker after saving.{!isAdmin && " Admin-only."} Also editable in{" "}
            <Link to="/system-settings">System Settings</Link>; the budget anchor lives in
            System Settings <code>[quota]</code>.
          </small>
        </div>
      )}
    </div>
  );
}
