import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type DriveMode,
  type SessionsScope,
  type Transparency as TransparencyData,
} from "../api";
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
 * T-0772 — the LIVE SESSIONS card's copy, as a function of what the server
 * said it did to the list. Pure + exported so the wording is unit-testable
 * without rendering the panel (mirrors `sessionsEmptyState` on the Processes
 * page, which pulled its own ambiguity out for the same reason).
 *
 * THE DEFECT THIS CLOSES: the two cards in this strip come from ONE payload but
 * obey TWO policies — `quota.in_progress` is counted off the whole backlog
 * (broad) while `sessions` is owner-scoped per user (T-0080/T-0321). Rendered
 * side by side with one unqualified label, a non-admin read "IN PROGRESS 6"
 * next to "LIVE SESSIONS 0" and the only available conclusion was that six
 * tasks were stalled — a false statement about system health, not a withheld
 * number. Naming the scope is the whole fix: the VALUE does not move and no
 * gate is touched (widening the owner scope would light the global busy
 * indicator for other people's work — see components/globalBusyHelpers.ts).
 *
 * An UNKNOWN scope (legacy server, failed fan-out) keeps today's neutral copy.
 * We can only claim the count is the viewer's when the server says so.
 */
export function liveSessionsCardCopy(
  scope: SessionsScope | undefined,
): { label: string; sub: string } {
  if (scope === "own") {
    return { label: "YOUR LIVE SESSIONS", sub: "sessions you own → Processes" };
  }
  return { label: "LIVE SESSIONS", sub: "→ Processes page" };
}

/**
 * T-0828 / D-0069 — the DRIVE MODE panel's copy, as a pure function of the
 * payload's `quota.drive`. Exported and pure for the same reason
 * {@link liveSessionsCardCopy} is: the WORDING is the deliverable here, so it
 * has to be assertable without rendering the panel.
 *
 * WHY THIS EXISTS. The stakeholder, 2026-07-30T07:54:16Z:
 *
 *   "нужно более чёткое понимание для меня, какой режим драйва щас стоит, я
 *    просил закончить всё что в опен, но видимо это не интерпретировалось как
 *    переключить режим драйва"
 *
 * «щас стоит» = *is currently set*. He is asking to READ a standing setting,
 * and «для меня» is why this lives in the web UI at all — `bsq pace show` and
 * the operator brief are surfaces AGENTS read.
 *
 * THREE STATES, and collapsing any two of them re-creates the defect:
 *
 *  - `undefined`  — a pre-T-0828 server did not send the field. UNKNOWN, and it
 *    must NOT render as "no mode set": that would be a claim about his settings
 *    the server never made (the silent-None failure D-0069 names).
 *  - `configured: false` — the server looked and there is genuinely no drive
 *    block. Defaults are in effect.
 *  - `configured: true` — a mode is set. Both of the last two read
 *    `scope: "all"` when he chose the widest, so only this flag separates
 *    "never set" from "deliberately everything".
 */
export function driveModeCopy(drive: DriveMode | undefined): {
  state: "unknown" | "unset" | "set";
  headline: string;
  provenance: string | null;
  warnings: string[];
} {
  if (!drive) {
    return {
      state: "unknown",
      headline: "unknown — this server does not report a drive mode",
      provenance: null,
      warnings: [],
    };
  }

  // An out-of-set stored value: the effective axis has ALREADY fallen back to
  // its default server-side, so the job here is to say so loudly rather than to
  // substitute again. Rendering the fallback as though it were the setting is
  // exactly "I set it and it quietly ignored me".
  const warnings = Object.entries(drive.invalid ?? {}).map(
    ([field, raw]) =>
      `${field}: ${JSON.stringify(raw)} is not a recognised value — ` +
      `the default "${drive[field as "scope" | "stop_when" | "on_stop"]}" is in effect`,
  );

  // ⚠ PROVENANCE BELONGS TO THE REQUEST, NOT TO THE RESULT.
  //
  // This is the rule the T-0828 walkthrough produced, and re-fusing the two is
  // the mistake to avoid here. With a hand-edited `scope: "opne"` the surface
  // rendered:
  //
  //     scope: all — set 2026-07-30 15:14 from «закончить всё что в опен»
  //
  // Every field in that line is individually correct and the sentence is a lie:
  // his words asked for `open_reopened`, the stored value was rejected, and
  // `all` is OUR fallback — so the line asserts a causal link that does not
  // exist, attributing a value he never chose to words he did say. No per-field
  // unit test catches it; only the assembled line is wrong, which is exactly
  // what T-0158's walk-first rule exists to surface.
  //
  // So when an axis was rejected the line carries BOTH facts — what he asked
  // for (raw, so he sees his own typo) and what is in effect because we could
  // not read it — and the provenance verb below attaches to the REQUEST.
  const invalid = drive.invalid ?? {};
  const hasInvalid = Object.keys(invalid).length > 0;
  const axis = (field: "scope" | "stop_when" | "on_stop", label: string) =>
    field in invalid
      ? `${label}: ${JSON.stringify(invalid[field])} requested (not recognised) ` +
        `→ "${drive[field]}" in effect`
      : `${label}: ${drive[field]}`;
  const headline = [
    axis("scope", "scope"),
    axis("stop_when", "stop when"),
    axis("on_stop", "on stop"),
  ].join(" · ");

  if (!drive.configured) {
    return { state: "unset", headline, provenance: null, warnings };
  }

  // The provenance line — D-0069 calls `source_text` "not decoration and not a
  // log, it is the visibility half". It answers "did my instruction land"
  // directly instead of leaving him to infer it from behaviour.
  const verb = hasInvalid ? "requested" : "set";
  const bits: string[] = [];
  if (drive.set_at) bits.push(`${verb} ${fmtSetAt(drive.set_at)}`);
  if (drive.set_by) bits.push(`by ${drive.set_by}`);
  let provenance = bits.length ? bits.join(" ") : null;
  if (drive.source_text) {
    provenance = `${provenance ?? verb} from «${drive.source_text}»`;
  }

  return { state: "set", headline, provenance, warnings };
}

/**
 * `2026-07-30T14:52:31Z` -> `2026-07-30 14:52`, the shape D-0069's example line
 * prints. An unparseable stamp is passed through verbatim rather than dropped —
 * hiding it would be the same silent omission this panel exists to close.
 */
function fmtSetAt(setAt: string): string {
  const t = Date.parse(setAt);
  if (Number.isNaN(t)) return setAt;
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}`
  );
}

/**
 * The DRIVE MODE card (T-0828 DoD-7). Always visible, never behind a
 * <details> — a standing setting he has to click to discover is not "более
 * чёткое понимание".
 */
function DriveModeCard({ drive }: { drive: DriveMode | undefined }) {
  const copy = driveModeCopy(drive);
  const dim = copy.state !== "set";
  return (
    <div
      className="mc-an-card"
      style={{ gridColumn: "1 / -1", textAlign: "left" }}
      data-testid="drive-mode-card"
    >
      <div className="mc-an-card-label">DRIVE MODE</div>
      <div
        style={{
          fontFamily: "var(--mc-mono)",
          fontSize: "0.8rem",
          color: dim ? "var(--mc-text-dim)" : "var(--mc-text)",
          marginTop: "0.15rem",
          wordBreak: "break-word",
        }}
      >
        {copy.state === "unset" ? "not set — " : ""}
        {copy.headline}
      </div>
      {copy.state === "unset" && (
        <div className="mc-an-card-sub">
          no standing mode — defaults in effect (everything is in play)
        </div>
      )}
      {copy.provenance && (
        <div
          className="mc-an-card-sub"
          style={{ wordBreak: "break-word" }}
          data-testid="drive-mode-provenance"
        >
          {copy.provenance}
        </div>
      )}
      {copy.warnings.map((w) => (
        <div
          key={w}
          style={{
            fontSize: "0.72rem",
            color: "var(--mc-red)",
            marginTop: "0.2rem",
          }}
        >
          ⚠ {w}
        </div>
      ))}
    </div>
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
  sessionsScope,
}: {
  slug: string;
  quota: TransparencyData["quota"];
  liveSessions: number;
  sessionsScope: SessionsScope | undefined;
}) {
  const liveCopy = liveSessionsCardCopy(sessionsScope);
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
        <div className="mc-an-card-label">{liveCopy.label}</div>
        <div className="mc-an-card-value">{liveSessions}</div>
        <div className="mc-an-card-sub">{liveCopy.sub}</div>
      </Link>
      {/* T-0828: the standing drive mode, on the surface HE looks at. Spans the
          full row — it is a sentence, not a counter. */}
      <DriveModeCard drive={quota.drive} />
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
      {/* 1 — the always-visible summary strip (quota/pace + session count).
          T-0772: the strip is fed the payload's `sessions_scope` so the session
          card can say whose count it is — the rows above are owner-scoped while
          `quota` next to them is install-wide. */}
      <SummaryStrip
        slug={slug}
        quota={data.quota}
        liveSessions={live.length}
        sessionsScope={data.sessions_scope}
      />

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
