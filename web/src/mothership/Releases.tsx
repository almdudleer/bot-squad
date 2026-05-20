import { useCallback, useEffect, useMemo, useState } from "react";
import {
  releasesApi,
  type ReleaseEntry,
  type ReleaseTelemetryRow,
} from "./api";

/**
 * T-0087: Releases tab on the mothership build.
 *
 * Three sections, top to bottom:
 *
 *   1. Release history — full manifest list (newest-first) from
 *      /api/releases/_all. Notes are markdown source rendered as a
 *      preformatted block, collapsed by default behind a per-row toggle.
 *   2. Cut a release — operator-facing textarea + button. On submit:
 *        a. POST /api/releases/_notes_draft (stages notes for prod.sh).
 *        b. POST /api/projects/bot-squad/deploy with target=prod.
 *        c. Poll /api/releases/_all until a new version appears or 60s.
 *   3. Installs grid — fan-out telemetry rows from /api/releases/_telemetry,
 *      one per attached server.
 *
 * Mothership-only: this module is only imported by App.tsx behind
 * VITE_MOTHERSHIP=1 (lazy-loaded via mothership/routes.tsx), and the four
 * API endpoints it consumes all 404 when MOTHERSHIP=0 — so a detached
 * single-install never renders this view nor talks to its endpoints.
 */

const POLL_AFTER_CUT_MS = 4_000;
const POLL_MAX_MS = 60_000;

function shortSha(sha: string | null | undefined, len = 8): string {
  if (!sha) return "—";
  return sha.length > len ? sha.slice(0, len) : sha;
}

function fmtCreatedAt(iso: string): string {
  // Plain "YYYY-MM-DD HH:MM UTC" — no relative-time noise on a history
  // table that operators scan vertically. Parsing failure → echo the
  // input so a malformed manifest entry doesn't crash the row.
  const ts = Date.parse(iso);
  if (isNaN(ts)) return iso;
  const d = new Date(ts);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    ` ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
  );
}

function NotesBlock({ notes }: { notes: string }) {
  // No markdown library in the FE deps — we render the raw source in a
  // <pre> block. The author writes the notes themselves, so a faithful
  // verbatim render is more useful than a half-rendered HTML conversion
  // here. CSS class is reused from the rest of the mothership UI.
  return (
    <pre
      style={{
        whiteSpace: "pre-wrap",
        fontFamily: "var(--mc-mono)",
        fontSize: 12,
        background: "var(--mc-surface)",
        border: "1px solid var(--mc-border)",
        borderRadius: 3,
        padding: "0.6rem 0.75rem",
        margin: 0,
        color: "var(--mc-text-mid)",
      }}
    >
      {notes || (
        <span style={{ color: "var(--mc-text-dim)" }}>
          (no notes recorded)
        </span>
      )}
    </pre>
  );
}

function ReleaseRow({ entry }: { entry: ReleaseEntry }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr>
        <td style={{ fontFamily: "var(--mc-mono)", fontSize: 13 }}>
          {entry.version}
        </td>
        <td
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: 12,
            color: "var(--mc-text-dim)",
          }}
        >
          {fmtCreatedAt(entry.created_at)}
        </td>
        <td>
          <code
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: 12,
              color: "var(--mc-text-dim)",
            }}
            title={entry.git_sha}
          >
            {shortSha(entry.git_sha)}
          </code>
        </td>
        <td>
          <code
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: 12,
              color: "var(--mc-text-dim)",
            }}
            title={entry.sha256}
          >
            {shortSha(entry.sha256)}
          </code>
        </td>
        <td>
          <button
            type="button"
            className="btn btn-sm btn-link p-0"
            onClick={() => setOpen((v) => !v)}
            data-testid={`notes-toggle-${entry.version}`}
          >
            {open ? "hide" : "show"} notes
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} style={{ paddingTop: 0 }}>
            <NotesBlock notes={entry.notes} />
          </td>
        </tr>
      )}
    </>
  );
}

function ReleaseHistory({
  releases,
  loading,
  error,
}: {
  releases: ReleaseEntry[] | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <section style={{ marginTop: "1.5rem" }}>
      <div className="mc-section-title">Release history</div>
      {error && <div className="alert alert-danger mt-2">{error}</div>}
      {loading && !error && (
        <div className="mc-loading" style={{ marginTop: "0.5rem" }}>
          Loading releases
        </div>
      )}
      {!loading && !error && releases !== null && releases.length === 0 && (
        <div className="mc-empty" style={{ marginTop: "0.75rem" }}>
          <div className="mc-empty-icon">◇</div>
          <div>No releases cut yet.</div>
        </div>
      )}
      {!error && releases !== null && releases.length > 0 && (
        <div className="table-responsive" style={{ marginTop: "0.5rem" }}>
          <table className="table table-sm align-middle">
            <thead>
              <tr>
                <th style={{ width: "12rem" }}>Version</th>
                <th style={{ width: "12rem" }}>Created</th>
                <th style={{ width: "6rem" }}>Git SHA</th>
                <th style={{ width: "6rem" }}>SHA-256</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {releases.map((entry) => (
                <ReleaseRow key={entry.version} entry={entry} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function CutRelease({
  releases,
  onCut,
}: {
  releases: ReleaseEntry[] | null;
  /** Called after the FE has confirmed the new version appears in the
   *  history (or polling timed out). Lets the parent refresh telemetry
   *  + history so the new row paints inline. */
  onCut: () => Promise<void>;
}) {
  const [notes, setNotes] = useState("");
  const [status, setStatus] = useState<
    | { kind: "idle" }
    | { kind: "submitting"; step: "draft" | "deploy" | "polling" }
    | { kind: "done"; version: string }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  const baselineVersions = useMemo(
    () => new Set((releases ?? []).map((r) => r.version)),
    [releases],
  );

  const submitting = status.kind === "submitting";

  async function pollForNewVersion(
    baseline: Set<string>,
  ): Promise<string | null> {
    const start = Date.now();
    while (Date.now() - start < POLL_MAX_MS) {
      await new Promise((res) => setTimeout(res, POLL_AFTER_CUT_MS));
      try {
        const fresh = await releasesApi.all();
        for (const entry of fresh) {
          if (!baseline.has(entry.version)) return entry.version;
        }
      } catch {
        // Transient — the queue worker might be writing the manifest.
        // Keep polling until POLL_MAX_MS.
      }
    }
    return null;
  }

  async function handleCut() {
    if (!notes.trim()) {
      setStatus({
        kind: "error",
        message: "notes can't be empty — type the release summary first",
      });
      return;
    }
    try {
      setStatus({ kind: "submitting", step: "draft" });
      await releasesApi.saveNotesDraft(notes);
      setStatus({ kind: "submitting", step: "deploy" });
      await releasesApi.cutDeploy("cut release");
      setStatus({ kind: "submitting", step: "polling" });
      const newVersion = await pollForNewVersion(baselineVersions);
      await onCut();
      if (newVersion) {
        setStatus({ kind: "done", version: newVersion });
        setNotes("");
      } else {
        setStatus({
          kind: "error",
          message:
            "Deploy queued but no new version appeared within 60s. " +
            "Check /p/bot-squad/runs for the deploy log.",
        });
      }
    } catch (e) {
      setStatus({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }

  return (
    <section style={{ marginTop: "1.5rem" }}>
      <div className="mc-section-title">Cut a release</div>
      <p
        style={{
          color: "var(--mc-text-dim)",
          fontSize: "0.85rem",
          marginTop: "0.4rem",
        }}
      >
        Write the release notes (markdown), then queue a prod deploy. The
        next vYYYY.MM.DD.N is computed from today's UTC date + same-day
        counter — matches{" "}
        <code style={{ fontFamily: "var(--mc-mono)" }}>prod.sh</code>'s
        own counter exactly.
      </p>
      <textarea
        rows={6}
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder={"# What's new\n\n- ...\n"}
        disabled={submitting}
        style={{
          width: "100%",
          fontFamily: "var(--mc-mono)",
          fontSize: 13,
          background: "var(--mc-surface)",
          color: "var(--mc-text)",
          border: "1px solid var(--mc-border)",
          borderRadius: 3,
          padding: "0.6rem 0.75rem",
        }}
        data-testid="release-notes-textarea"
      />
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          marginTop: "0.5rem",
        }}
      >
        <button
          type="button"
          className="mc-badge mc-badge-info"
          style={{
            border: "none",
            padding: "6px 14px",
            cursor: submitting ? "wait" : "pointer",
            opacity: submitting ? 0.65 : 1,
          }}
          onClick={handleCut}
          disabled={submitting}
          data-testid="cut-release-button"
        >
          {submitting ? "Cutting…" : "Cut release"}
        </button>
        {status.kind === "submitting" && (
          <span
            style={{ color: "var(--mc-text-dim)", fontSize: 12 }}
            data-testid="cut-status"
          >
            {status.step === "draft" && "Staging notes…"}
            {status.step === "deploy" && "Queueing deploy…"}
            {status.step === "polling" && "Waiting for prod.sh…"}
          </span>
        )}
        {status.kind === "done" && (
          <span
            className="mc-badge mc-badge-ok"
            data-testid="cut-status-done"
          >
            cut {status.version}
          </span>
        )}
      </div>
      {status.kind === "error" && (
        <div className="alert alert-danger mt-2" data-testid="cut-error">
          {status.message}
        </div>
      )}
    </section>
  );
}

function InstallsGrid({
  rows,
  loading,
  error,
}: {
  rows: ReleaseTelemetryRow[] | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <section style={{ marginTop: "1.5rem" }}>
      <div className="mc-section-title">Installs</div>
      {error && <div className="alert alert-danger mt-2">{error}</div>}
      {loading && !error && (
        <div className="mc-loading" style={{ marginTop: "0.5rem" }}>
          Loading installs
        </div>
      )}
      {!loading && !error && rows !== null && rows.length === 0 && (
        <div className="mc-empty" style={{ marginTop: "0.75rem" }}>
          <div className="mc-empty-icon">◯</div>
          <div>No attached installs yet.</div>
        </div>
      )}
      {!error && rows !== null && rows.length > 0 && (
        <div className="table-responsive" style={{ marginTop: "0.5rem" }}>
          <table className="table table-sm align-middle">
            <thead>
              <tr>
                <th>Install</th>
                <th>Installed version</th>
                <th>Last apply outcome</th>
                <th>Last check</th>
                <th>Git SHA</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.install_id}>
                  <td style={{ fontFamily: "var(--mc-mono)", fontSize: 13 }}>
                    {row.install_name || row.install_id}
                  </td>
                  <td style={{ fontFamily: "var(--mc-mono)", fontSize: 13 }}>
                    {row.installed_version ?? (
                      <span style={{ color: "var(--mc-text-dim)" }}>
                        never
                      </span>
                    )}
                  </td>
                  <td>
                    <OutcomeBadge outcome={row.last_apply_outcome} />
                  </td>
                  <td
                    style={{
                      fontFamily: "var(--mc-mono)",
                      fontSize: 12,
                      color: "var(--mc-text-dim)",
                    }}
                  >
                    {row.last_check_at ? fmtCreatedAt(row.last_check_at) : "—"}
                  </td>
                  <td>
                    <code
                      style={{
                        fontFamily: "var(--mc-mono)",
                        fontSize: 12,
                        color: "var(--mc-text-dim)",
                      }}
                      title={row.current_git_sha ?? undefined}
                    >
                      {shortSha(row.current_git_sha)}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function OutcomeBadge({ outcome }: { outcome: string | null }) {
  if (!outcome || outcome === "never") {
    return <span className="mc-badge mc-badge-dim">never</span>;
  }
  if (outcome === "success") {
    return <span className="mc-badge mc-badge-ok">success</span>;
  }
  // `failed:<step>` per T-0085 spec — the colon-prefixed form is the
  // canonical "didn't succeed" payload.
  return (
    <span className="mc-badge mc-badge-danger" title={outcome}>
      {outcome.startsWith("failed:") ? outcome : `outcome: ${outcome}`}
    </span>
  );
}

export function Releases() {
  const [releases, setReleases] = useState<ReleaseEntry[] | null>(null);
  const [releasesError, setReleasesError] = useState<string | null>(null);
  const [telemetry, setTelemetry] = useState<ReleaseTelemetryRow[] | null>(
    null,
  );
  const [telemetryError, setTelemetryError] = useState<string | null>(null);

  const loadReleases = useCallback(async () => {
    setReleasesError(null);
    try {
      const rows = await releasesApi.all();
      setReleases(rows);
    } catch (e) {
      setReleasesError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadTelemetry = useCallback(async () => {
    setTelemetryError(null);
    try {
      const rows = await releasesApi.telemetry();
      setTelemetry(rows);
    } catch (e) {
      setTelemetryError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([loadReleases(), loadTelemetry()]);
  }, [loadReleases, loadTelemetry]);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  return (
    <div className="container py-4" style={{ maxWidth: 1100 }}>
      <div className="mc-section-title" style={{ margin: 0 }}>
        Releases
      </div>
      <p
        style={{
          color: "var(--mc-text-dim)",
          fontSize: "0.85rem",
          marginTop: "0.5rem",
        }}
      >
        Mothership-only. Cut a release artifact for attached servers and
        watch the per-install version roll-up.
      </p>

      <ReleaseHistory
        releases={releases}
        loading={releases === null && !releasesError}
        error={releasesError}
      />

      <CutRelease releases={releases} onCut={refreshAll} />

      <InstallsGrid
        rows={telemetry}
        loading={telemetry === null && !telemetryError}
        error={telemetryError}
      />
    </div>
  );
}

export default Releases;
