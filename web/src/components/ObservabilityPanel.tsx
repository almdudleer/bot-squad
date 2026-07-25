import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Transparency as TransparencyData } from "../api";
import { Markdown } from "./Markdown";

/**
 * T-0593 (T-0588a, D-0046) — top-level observability on the project home.
 *
 *   "весь этот юай это в целом больше про обсурдобилити" — the WHOLE UI is
 *   the observability layer (T-0587), so the separate /p/:slug/transparency
 *   tab was a mistake.
 *
 * T-0627 (D-0056 IA audit): trimmed to a slim 3-card strip (IN PROGRESS,
 * RE-DRIVE, LIVE SESSIONS) + the operator state-doc collapsible. The
 * who-does-what table, INITIATIVE PACE card, and Scheduler section died —
 * each duplicated a fact that already has a home elsewhere (Processes page,
 * Vision page, /system-settings respectively; see D-0056's fact-home table).
 *
 * T-0674 (D-0057 §4/§8, Q2 = full 2b): the RE-DRIVE pause/resume + fleet-
 * model control (T-0620) is CUT ENTIRELY, superseding T-0620's shipped web
 * lever — the stakeholder's 2026-07-25 verdict named exactly one surviving
 * web control (ResourceCapsPanel on Sessions), so this one goes too. Operator
 * pause/resume + model steering now live in TG/CLI only (R8: the terminal,
 * not the web, is the real outage fallback). The strip is just IN PROGRESS +
 * LIVE SESSIONS now — pure read.
 *
 * NOTE: the transparency read uses the global `api` singleton (as the
 * retired Transparency page did) — it isn't part of the mothership-proxied
 * ProjectApi surface.
 */

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
 * The always-visible summary strip: quota (IN PROGRESS) and a LIVE SESSIONS
 * card that links to the Processes page (T-0627: the who-does-what table
 * died — Processes is that fact's home; this card carries a count + link,
 * not a second rendering). T-0674: the RE-DRIVE pause/resume + model control
 * that used to sit here is cut entirely — pure read strip now.
 */
function SummaryStrip({
  slug,
  quota,
  liveSessions,
}: {
  slug: string;
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
      <Link
        to={`/p/${slug}/sessions`}
        className="mc-an-card"
        style={{ textDecoration: "none", color: "inherit", display: "block" }}
      >
        <div className="mc-an-card-label">LIVE SESSIONS</div>
        <div className="mc-an-card-value">{liveSessions}</div>
        <div className="mc-an-card-sub">→ Processes page</div>
      </Link>
    </div>
  );
}

/**
 * The pure, data-driven body — every section is a function of the already-
 * loaded transparency payload, so it renders without any fetch (the fetch +
 * loading/error chrome live in {@link ObservabilityPanel}).
 */
export function ObservabilityView({
  data,
  slug,
}: {
  data: TransparencyData;
  slug: string;
}) {
  // The payload carries the FULL session history (341 rows on the live
  // install, ~97% suspended/archived). Live count feeds the LIVE SESSIONS
  // card; the row-level detail lives on the Processes page (T-0627).
  const live = data.sessions.filter((s) => !s.archived && s.status === "active");
  return (
    <>
      {/* 1 — the always-visible summary strip (quota/pace + session count) */}
      <SummaryStrip slug={slug} quota={data.quota} liveSessions={live.length} />

      {/* 2 — operator state-doc (T-0473) */}
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
 * had).
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
    </div>
  );
}
