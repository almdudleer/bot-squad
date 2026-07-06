import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  isNotFoundError,
  type OperatorPauseResult,
  type OperatorResumeResult,
  type ProjectApi,
  type Transparency as TransparencyData,
  type WorkerModel,
} from "../api";
import { useApiClient } from "../apiContext";
import { Markdown } from "./Markdown";
import { Select } from "./Select";

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
 * The RE-DRIVE card earns its keep by becoming the T-0620 control surface
 * (pause/resume + fleet default model) instead of just a status readout.
 *
 * NOTE: the transparency read still uses the global `api` singleton (as the
 * retired Transparency page did) — it isn't part of the mothership-proxied
 * ProjectApi surface. The NEW operator/model controls below DO go through
 * `useApiClient()` (ProjectApi) so they proxy correctly for a mothership-
 * attached server.
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

// T-0630 fleet-model allowlist (kept in sync with the worker's
// fleet_model_set validation). "" clears the override → builtin default.
const MODEL_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "(default)" },
  { value: "claude-sonnet-5", label: "Sonnet 5" },
  { value: "claude-opus-4-8", label: "Opus 4.8" },
  { value: "opus[1m]", label: "Opus [1M]" },
  { value: "claude-fable-5", label: "Fable 5" },
];
const CUSTOM_MODEL = "__custom__";

// ---------------------------------------------------------------------------
// T-0620/T-0630 operator controls — pure async wrappers, extracted out of the
// components so happy/error/unavailable paths are unit-testable without a DOM
// (this project's vitest runs node-environment only, no jsdom/testing-library
// — see ObservabilityPanel.test.tsx). Each maps the 3 outcomes the ticket's
// error handling requires: `ok` (success), `unavailable` (404 — the T-0630
// routes haven't landed yet, card degrades to read-only), `error` (any other
// failure, surfaced inline).
// ---------------------------------------------------------------------------
export type ActionOutcome<T> =
  | { kind: "ok"; data: T }
  | { kind: "unavailable" }
  | { kind: "error"; message: string };

async function runAction<T>(fn: () => Promise<T>): Promise<ActionOutcome<T>> {
  try {
    return { kind: "ok", data: await fn() };
  } catch (e) {
    if (isNotFoundError(e)) return { kind: "unavailable" };
    return { kind: "error", message: String(e) };
  }
}

export function fetchFleetModel(
  api: Pick<ProjectApi, "getWorkerModel">,
  slug: string,
): Promise<ActionOutcome<WorkerModel>> {
  return runAction(() => api.getWorkerModel(slug));
}

export function setFleetModel(
  api: Pick<ProjectApi, "putWorkerModel">,
  slug: string,
  model: string,
): Promise<ActionOutcome<WorkerModel>> {
  return runAction(() => api.putWorkerModel(slug, model));
}

export function pauseOperator(
  api: Pick<ProjectApi, "operatorPause">,
  slug: string,
  reason?: string,
): Promise<ActionOutcome<OperatorPauseResult>> {
  return runAction(() => api.operatorPause(slug, reason));
}

export function resumeOperator(
  api: Pick<ProjectApi, "operatorResume">,
  slug: string,
): Promise<ActionOutcome<OperatorResumeResult>> {
  return runAction(() => api.operatorResume(slug));
}

/**
 * T-0620 seam: the fleet-default-model line on the RE-DRIVE card.
 * Admin-only edit control; a plain read for everyone else. Any GET/PUT 404
 * (route not deployed yet, e.g. T-0630 landing after this lane) degrades to
 * a quiet "not available" line instead of a broken control.
 */
function FleetModelLine({
  slug,
  isAdmin,
}: {
  slug: string;
  isAdmin: boolean;
}) {
  const api = useApiClient();
  const [model, setModel] = useState<string | null>(null);
  const [available, setAvailable] = useState(true);
  const [choice, setChoice] = useState<string>("");
  const [customValue, setCustomValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchFleetModel(api, slug).then((outcome) => {
      if (cancelled) return;
      if (outcome.kind === "unavailable") {
        setAvailable(false);
      } else if (outcome.kind === "error") {
        setError(outcome.message);
      } else {
        const r = outcome.data.model;
        setModel(r);
        const known = MODEL_OPTIONS.some((o) => o.value === r);
        setChoice(known ? r : CUSTOM_MODEL);
        setCustomValue(known ? "" : r);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [api, slug]);

  async function save() {
    const next = choice === CUSTOM_MODEL ? customValue.trim() : choice;
    setSaving(true);
    setError(null);
    const outcome = await setFleetModel(api, slug, next);
    if (outcome.kind === "unavailable") {
      setAvailable(false);
    } else if (outcome.kind === "error") {
      setError(outcome.message);
    } else {
      setModel(outcome.data.model);
    }
    setSaving(false);
  }

  if (!available) return null;
  if (model === null && !error) {
    return (
      <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginTop: "0.35rem" }}>
        Loading model…
      </div>
    );
  }

  const currentLabel =
    MODEL_OPTIONS.find((o) => o.value === model)?.label ?? model ?? "(default)";

  if (!isAdmin) {
    return (
      <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginTop: "0.35rem" }}>
        model: <span style={{ fontFamily: "var(--mc-mono)" }}>{currentLabel}</span>
      </div>
    );
  }

  const dirty =
    (choice === CUSTOM_MODEL ? customValue.trim() : choice) !== (model ?? "");

  return (
    <div style={{ marginTop: "0.4rem" }}>
      <div className="d-flex align-items-center gap-2 flex-wrap">
        <span style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", fontFamily: "var(--mc-mono)" }}>
          model:
        </span>
        <Select
          value={choice}
          onChange={setChoice}
          ariaLabel="fleet default model"
          style={{ fontSize: "0.72rem", minWidth: "9rem" }}
          options={[
            ...MODEL_OPTIONS,
            { value: CUSTOM_MODEL, label: "custom…" },
          ]}
        />
        {choice === CUSTOM_MODEL && (
          <input
            className="form-control form-control-sm"
            style={{ width: "10rem", fontSize: "0.72rem" }}
            value={customValue}
            onChange={(e) => setCustomValue(e.target.value)}
            placeholder="model id"
          />
        )}
        <button
          type="button"
          className="btn btn-outline-secondary btn-sm"
          style={{ fontSize: "0.68rem" }}
          disabled={saving || !dirty}
          onClick={save}
        >
          {saving ? "Saving…" : "Set"}
        </button>
      </div>
      {error && (
        <div className="text-danger" style={{ fontSize: "0.68rem", marginTop: "0.2rem" }}>
          {error}
        </div>
      )}
    </div>
  );
}

/**
 * T-0620 seam: the RE-DRIVE card is the only place that displays operator
 * posture, so the pause/resume control + fleet model line land here instead
 * of a new panel. `paused` seeds from the transparency payload's quota flag
 * (unaffected by this control existing or not) and flips optimistically on a
 * successful pause/resume.
 */
function ReDriveCard({ slug, initiallyPaused }: { slug: string; initiallyPaused: boolean }) {
  const projectApi = useApiClient();
  const [paused, setPaused] = useState(initiallyPaused);
  useEffect(() => setPaused(initiallyPaused), [initiallyPaused]);
  const [busy, setBusy] = useState(false);
  const [controlAvailable, setControlAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((m) => {
        if (!cancelled) setIsAdmin(Boolean(m.is_admin));
      })
      .catch(() => {
        /* non-admin / anon — model edit stays hidden */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggle() {
    setError(null);
    if (paused) {
      setBusy(true);
      const outcome = await resumeOperator(projectApi, slug);
      if (outcome.kind === "unavailable") setControlAvailable(false);
      else if (outcome.kind === "error") setError(outcome.message);
      else setPaused(false);
      setBusy(false);
      return;
    }
    const reason = window.prompt("Pause reason (optional):");
    if (reason === null) return; // cancelled — leave running
    setBusy(true);
    const outcome = await pauseOperator(projectApi, slug, reason || undefined);
    if (outcome.kind === "unavailable") setControlAvailable(false);
    else if (outcome.kind === "error") setError(outcome.message);
    else setPaused(true);
    setBusy(false);
  }

  return (
    <div className="mc-an-card">
      <div className="mc-an-card-label">RE-DRIVE</div>
      <div
        className="mc-an-card-value"
        style={{ color: paused ? "var(--mc-amber)" : "var(--mc-green)" }}
      >
        {paused ? "PAUSED" : "RUNNING"}
      </div>
      <div className="mc-an-card-sub">
        {paused ? "operator re-drive is user-paused" : "operator re-drive active"}
      </div>
      {controlAvailable && (
        <button
          type="button"
          className="btn btn-outline-secondary btn-sm"
          style={{ fontSize: "0.68rem", marginTop: "0.35rem" }}
          disabled={busy}
          onClick={toggle}
        >
          {busy ? "…" : paused ? "Resume" : "Pause"}
        </button>
      )}
      {error && (
        <div className="text-danger" style={{ fontSize: "0.68rem", marginTop: "0.2rem" }}>
          {error}
        </div>
      )}
      <FleetModelLine slug={slug} isAdmin={isAdmin} />
    </div>
  );
}

/**
 * The always-visible summary strip: quota (IN PROGRESS), the operator
 * RE-DRIVE control, and a LIVE SESSIONS card that links to the Processes
 * page (T-0627: the who-does-what table died — Processes is that fact's
 * home; this card carries a count + link, not a second rendering).
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
      <ReDriveCard slug={slug} initiallyPaused={quota.paused} />
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
