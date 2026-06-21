import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, CloneStatus, CloneView, errorDetail } from "../api";
import { RouteSkeleton } from "../components/RouteSkeleton";

// T-0296: per-project "clones" page — the v0.9 "Installation ≠ Project"
// legibility deliverable. Surfaces the dev (repo_path) + prod (repo_master)
// clone topology of a project (branch, ahead/behind vs origin/master,
// clean/dirty, present/configured), the workspace + master/deploy branches,
// and the last deploy. An admin-only "Pull master (ff-only)" button asks the
// worker to fast-forward the prod clone to origin.
//
// The git ops live in the worker (only it has on-host clone access); this page
// reads the proxied read-model and triggers the admin-gated action. The worker
// actions are NEW — until the worker is restarted on a host the read endpoint
// 502/404s; the page surfaces that as an error with a retry rather than a
// white screen.

// Render any nullable scalar field as an em-dash placeholder so an offline /
// unfetched clone degrades gracefully instead of showing "null"/blank.
function dash(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

function fmtAt(epochSeconds: number | null | undefined): string {
  if (!epochSeconds || !Number.isFinite(epochSeconds)) return "—";
  try {
    return new Date(epochSeconds * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

function CloneCard({ title, view }: { title: string; view: CloneView }) {
  // Not a second clone on this install (e.g. a single-clone project).
  if (!view.configured) {
    return (
      <div className="card" style={{ flex: "1 1 280px", minWidth: "260px" }}>
        <div className="card-body">
          <div className="d-flex justify-content-between align-items-center mb-2">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, margin: 0 }}>{title}</h3>
            <span className="mc-badge mc-badge-dim">not configured</span>
          </div>
          <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", margin: 0 }}>
            No separate clone configured for this project.
          </p>
        </div>
      </div>
    );
  }

  const present = view.present === true;
  const clean = view.clean;
  const ahead = view.ahead;
  const behind = view.behind;

  return (
    <div className="card" style={{ flex: "1 1 280px", minWidth: "260px" }}>
      <div className="card-body">
        <div className="d-flex justify-content-between align-items-center mb-2">
          <h3 style={{ fontSize: "0.85rem", fontWeight: 600, margin: 0 }}>{title}</h3>
          <span className={`mc-badge ${present ? "mc-badge-ok" : "mc-badge-danger"}`}>
            {present ? "present" : "missing"}
          </span>
        </div>

        {!present ? (
          <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", margin: 0 }}>
            Clone not present at{" "}
            <code style={{ fontFamily: "var(--mc-mono)" }}>{dash(view.path)}</code>.
          </p>
        ) : (
          <dl style={{ margin: 0, fontSize: "0.78rem" }}>
            <Row label="Branch">
              <code style={{ fontFamily: "var(--mc-mono)" }}>{dash(view.branch)}</code>
            </Row>
            <Row label="vs origin/master">
              <span style={{ fontFamily: "var(--mc-mono)" }}>
                {ahead == null && behind == null ? (
                  <span style={{ color: "var(--mc-text-dim)" }}>offline / unfetched</span>
                ) : (
                  <>
                    <span
                      className={`mc-badge ${ahead ? "mc-badge-info" : "mc-badge-dim"}`}
                      style={{ marginRight: 6 }}
                    >
                      ↑ {dash(ahead)} ahead
                    </span>
                    <span className={`mc-badge ${behind ? "mc-badge-warn" : "mc-badge-dim"}`}>
                      ↓ {dash(behind)} behind
                    </span>
                  </>
                )}
              </span>
            </Row>
            <Row label="Working tree">
              {clean == null ? (
                <span style={{ color: "var(--mc-text-dim)" }}>—</span>
              ) : (
                <span className={`mc-badge ${clean ? "mc-badge-ok" : "mc-badge-warn"}`}>
                  {clean ? "clean" : "dirty"}
                </span>
              )}
            </Row>
            <Row label="Path">
              <code
                style={{
                  fontFamily: "var(--mc-mono)",
                  fontSize: "0.72rem",
                  wordBreak: "break-all",
                  color: "var(--mc-text-dim)",
                }}
              >
                {dash(view.path)}
              </code>
            </Row>
          </dl>
        )}
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="d-flex justify-content-between align-items-start py-1" style={{ gap: "0.75rem" }}>
      <dt style={{ color: "var(--mc-text-dim)", fontWeight: 400, whiteSpace: "nowrap" }}>{label}</dt>
      <dd style={{ margin: 0, textAlign: "right" }}>{children}</dd>
    </div>
  );
}

export function Clones() {
  const { slug = "" } = useParams<{ slug: string }>();
  const [data, setData] = useState<CloneStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [isAdmin, setIsAdmin] = useState<boolean>(false);

  const [pulling, setPulling] = useState(false);
  const [pullNotice, setPullNotice] = useState<string | null>(null);
  const [pullError, setPullError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    setLoading(true);
    api
      .getClones(slug)
      .then((d) => setData(d))
      .catch((e) => setError(errorDetail(e)))
      .finally(() => setLoading(false));
  }, [slug]);

  useEffect(() => {
    load();
    api
      .me()
      .then((m) => setIsAdmin(Boolean(m.is_admin)))
      .catch(() => {
        /* anonymous / load error — leave isAdmin false so the action stays hidden */
      });
  }, [load]);

  async function pullMaster() {
    setPullNotice(null);
    setPullError(null);
    setPulling(true);
    try {
      const r = await api.pullMaster(slug);
      if (r.ok) {
        const range =
          r.from_sha && r.to_sha ? ` (${r.from_sha} → ${r.to_sha})` : "";
        setPullNotice(`${r.detail}${range}`);
        load();
      } else {
        setPullError(r.detail);
      }
    } catch (e) {
      // 403 for a non-admin, or 502 when the worker action isn't live yet.
      setPullError(errorDetail(e));
    } finally {
      setPulling(false);
    }
  }

  const ld = data?.last_deploy ?? null;

  return (
    <div className="container py-4" style={{ maxWidth: "820px" }}>
      <div className="d-flex justify-content-between align-items-center mb-1">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
          {slug} — clones
        </h2>
        <button
          type="button"
          className="btn btn-outline-secondary btn-sm"
          onClick={load}
          disabled={loading}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>
      <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "1rem" }}>
        Dev + prod clone topology for this installation — the "Installation ≠
        Project" view. Ahead/behind are measured against{" "}
        <code style={{ fontFamily: "var(--mc-mono)" }}>origin/master</code> after
        a best-effort fetch.
      </p>

      {error && (
        <div className="alert alert-danger">
          {error}
          <div style={{ fontSize: "0.72rem", marginTop: "0.4rem", opacity: 0.85 }}>
            The clone read-model is served by a worker action; if it was just
            shipped, the worker may need a restart before this resolves.
          </div>
        </div>
      )}

      {/* T-0365: meaningful skeleton on first load (the git-measured fields can
          be slow); the header Refresh button shows "Loading…" during revalidation. */}
      {loading && data === null && !error && <RouteSkeleton />}

      {data && (
        <>
          {/* Workspace + branch summary */}
          <section className="card mb-3">
            <div className="card-body">
              <dl style={{ margin: 0, fontSize: "0.78rem" }}>
                <Row label="Workspace">
                  {data.repo_workspace == null ? (
                    <span style={{ color: "var(--mc-text-dim)" }}>—</span>
                  ) : (
                    <span>
                      <code
                        style={{
                          fontFamily: "var(--mc-mono)",
                          fontSize: "0.72rem",
                          wordBreak: "break-all",
                        }}
                      >
                        {data.repo_workspace}
                      </code>{" "}
                      <span
                        className={`mc-badge ${
                          data.workspace_present === true
                            ? "mc-badge-ok"
                            : data.workspace_present === false
                              ? "mc-badge-danger"
                              : "mc-badge-dim"
                        }`}
                      >
                        {data.workspace_present === true
                          ? "present"
                          : data.workspace_present === false
                            ? "missing"
                            : "—"}
                      </span>
                    </span>
                  )}
                </Row>
                <Row label="Master branch">
                  <code style={{ fontFamily: "var(--mc-mono)" }}>
                    {dash(data.master_branch)}
                  </code>
                </Row>
                <Row label="Deploy branch">
                  <code style={{ fontFamily: "var(--mc-mono)" }}>
                    {dash(data.deploy_branch)}
                  </code>
                </Row>
              </dl>
            </div>
          </section>

          {/* Dev + prod clone cards */}
          <div className="d-flex flex-wrap gap-3 mb-3">
            <CloneCard title="Dev clone (working)" view={data.dev} />
            <CloneCard title="Prod clone (master)" view={data.prod} />
          </div>

          {/* Last deploy */}
          <section className="card mb-3">
            <div className="card-body">
              <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.6rem" }}>
                Last deploy
              </h3>
              {ld === null ? (
                <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", margin: 0 }}>
                  No deploys recorded yet.
                </p>
              ) : (
                <dl style={{ margin: 0, fontSize: "0.78rem" }}>
                  <Row label="Outcome">
                    <span
                      className={`mc-badge ${
                        ld.ok === true
                          ? "mc-badge-ok"
                          : ld.ok === false
                            ? "mc-badge-danger"
                            : "mc-badge-dim"
                      }`}
                    >
                      {ld.ok === true
                        ? "ok"
                        : ld.ok === false
                          ? "failed"
                          : "unknown"}
                      {ld.returncode != null ? ` · rc ${ld.returncode}` : ""}
                    </span>
                  </Row>
                  <Row label="When">{fmtAt(ld.at)}</Row>
                  <Row label="Target">{dash(ld.target)}</Row>
                  <Row label="Requested by">{dash(ld.requested_by)}</Row>
                  <Row label="Reason">{dash(ld.reason)}</Row>
                  <Row label="Run id">
                    <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.72rem" }}>
                      {dash(ld.run_id)}
                    </code>
                  </Row>
                </dl>
              )}
            </div>
          </section>

          {/* Admin-only pull-master action */}
          {isAdmin && (
            <section className="card">
              <div className="card-body">
                <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.4rem" }}>
                  Fast-forward prod clone
                </h3>
                <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "0.75rem" }}>
                  Fetch + <code style={{ fontFamily: "var(--mc-mono)" }}>merge --ff-only</code>{" "}
                  the prod (master) clone to origin. Refuses a diverged or dirty
                  prod clone — it never rewrites history. Admin-only.
                </p>

                {pullNotice && <div className="alert alert-success py-2">{pullNotice}</div>}
                {pullError && <div className="alert alert-danger py-2">{pullError}</div>}

                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={pullMaster}
                  disabled={pulling}
                >
                  {pulling ? "Pulling…" : "Pull master (ff-only)"}
                </button>
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
