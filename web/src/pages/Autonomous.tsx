import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, AutonomousState } from "../api";
import { Modal } from "../components/Modal";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (isNaN(ts)) return "—";
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 0) return `in ${Math.abs(diff)}s`;
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function statusBadge(status: string) {
  const classes: Record<string, string> = {
    idle: "bg-secondary",
    working: "bg-primary",
    reviewing: "bg-info text-dark",
    sleeping: "bg-warning text-dark",
  };
  return (
    <span className={`badge ${classes[status] ?? "bg-secondary"} ms-2`} style={{ fontSize: "0.8rem" }}>
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Autonomous() {
  const { slug = "" } = useParams();
  const [state, setState] = useState<AutonomousState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  // Enable-confirmation modal
  const [showEnableModal, setShowEnableModal] = useState(false);

  // Settings form
  const [sleepStart, setSleepStart] = useState<number>(22);
  const [sleepEnd, setSleepEnd] = useState<number>(8);
  const [settingsDirty, setSettingsDirty] = useState(false);

  const load = useCallback(() => {
    api
      .autonomousStatus(slug)
      .then((s) => {
        setState(s);
        // Only sync form values on first load or when not dirty
        if (!settingsDirty) {
          setSleepStart(s.sleep_start_hour);
          setSleepEnd(s.sleep_end_hour);
        }
        setLastRefresh(new Date());
        setError(null);
      })
      .catch((e: unknown) => setError(String(e)));
  }, [slug, settingsDirty]);

  useEffect(() => {
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [load]);

  async function handleEnable() {
    setShowEnableModal(false);
    setSaving(true);
    setActionError(null);
    try {
      await api.autonomousEnable(slug, sleepStart, sleepEnd);
      load();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleDisable() {
    setSaving(true);
    setActionError(null);
    try {
      await api.autonomousDisable(slug);
      load();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleSaveSettings() {
    setSaving(true);
    setActionError(null);
    try {
      if (state?.enabled) {
        // Re-enable with new sleep hours to update them
        await api.autonomousEnable(slug, sleepStart, sleepEnd);
      } else {
        // Just update via enable (won't start the orchestrator if already disabled — we re-disable)
        await api.autonomousEnable(slug, sleepStart, sleepEnd);
        await api.autonomousDisable(slug);
      }
      setSettingsDirty(false);
      load();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setSaving(false);
    }
  }

  const isEnabled = state?.enabled ?? false;

  return (
    <div className="container py-4">
      {/* Breadcrumb */}
      <nav className="mb-3 small">
        <Link to="/">← Projects</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}`}>{slug}</Link>
        <span className="mx-2 text-muted">|</span>
        <strong>Autonomous</strong>
      </nav>

      {/* Page header */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div className="d-flex align-items-center gap-2">
          <h2 className="mb-0">Autonomous orchestrator</h2>
          {state && statusBadge(state.status)}
        </div>
        <div className="d-flex align-items-center gap-2">
          <span className="text-muted small">
            <span
              className="spinner-border spinner-border-sm me-1 text-secondary"
              role="status"
              aria-hidden="true"
              style={{ width: "0.7rem", height: "0.7rem", borderWidth: "0.1em" }}
            />
            Auto-refresh 15s
            {lastRefresh && (
              <span className="ms-2">
                · {lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
            )}
          </span>
          {state && (
            isEnabled ? (
              <button
                className="btn btn-sm btn-outline-danger"
                onClick={handleDisable}
                disabled={saving}
              >
                {saving ? "Disabling…" : "Disable"}
              </button>
            ) : (
              <button
                className="btn btn-sm btn-success"
                onClick={() => setShowEnableModal(true)}
                disabled={saving}
              >
                {saving ? "Enabling…" : "Enable"}
              </button>
            )
          )}
        </div>
      </div>

      {/* Errors */}
      {error && <div className="alert alert-danger">{error}</div>}
      {actionError && (
        <div className="alert alert-warning alert-dismissible">
          {actionError}
          <button type="button" className="btn-close" onClick={() => setActionError(null)} />
        </div>
      )}

      {/* Loading */}
      {state === null && !error && <p className="text-muted">Loading…</p>}

      {state !== null && (
        <>
          {/* Working banner */}
          {state.status === "working" && state.current_task_id && (
            <div className="alert alert-primary d-flex align-items-center gap-2 mb-4">
              <span className="spinner-border spinner-border-sm" role="status" aria-hidden="true" />
              <span>
                Working on{" "}
                <Link to={`/p/${slug}/t/${state.current_task_id}`} className="fw-semibold">
                  {state.current_task_id}
                </Link>
                {state.current_pane_id && (
                  <span className="ms-2 text-muted" style={{ fontFamily: "monospace", fontSize: "0.85rem" }}>
                    · pane {state.current_pane_id}
                  </span>
                )}
                {state.current_started_at && (
                  <span className="ms-2 text-muted" style={{ fontSize: "0.85rem" }}>
                    · started {relTime(state.current_started_at)}
                  </span>
                )}
              </span>
            </div>
          )}

          {/* Status card */}
          <div className="row g-3 mb-4">
            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <h6 className="card-subtitle text-muted mb-1">Status</h6>
                  <p className="card-text fs-5 fw-semibold mb-0 text-capitalize">
                    {state.status}
                  </p>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <h6 className="card-subtitle text-muted mb-1">Current task</h6>
                  <p className="card-text fs-5 fw-semibold mb-0">
                    {state.current_task_id ? (
                      <Link to={`/p/${slug}/t/${state.current_task_id}`}>
                        {state.current_task_id}
                      </Link>
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </p>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <h6 className="card-subtitle text-muted mb-1">Last tick</h6>
                  <p className="card-text fs-5 fw-semibold mb-0">
                    {relTime(state.last_tick_at)}
                  </p>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <h6 className="card-subtitle text-muted mb-1">Enabled</h6>
                  <p className="card-text fs-5 fw-semibold mb-0">
                    <span className={`badge ${isEnabled ? "bg-success" : "bg-secondary"}`}>
                      {isEnabled ? "Yes" : "No"}
                    </span>
                  </p>
                </div>
              </div>
            </div>
          </div>

          {/* Settings card */}
          <div className="card mb-4">
            <div className="card-header fw-semibold">Sleep window settings</div>
            <div className="card-body">
              <p className="text-muted small mb-3">
                No new tasks are spawned during the sleep window. In-flight tasks complete normally.
              </p>
              <div className="row g-3 align-items-end">
                <div className="col-auto">
                  <label className="form-label small fw-semibold mb-1">
                    Sleep start (UTC hour)
                  </label>
                  <input
                    type="number"
                    className="form-control form-control-sm"
                    min={0}
                    max={23}
                    value={sleepStart}
                    style={{ width: "80px" }}
                    onChange={(e) => {
                      setSleepStart(Number(e.target.value));
                      setSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="col-auto">
                  <label className="form-label small fw-semibold mb-1">
                    Sleep end (UTC hour)
                  </label>
                  <input
                    type="number"
                    className="form-control form-control-sm"
                    min={0}
                    max={23}
                    value={sleepEnd}
                    style={{ width: "80px" }}
                    onChange={(e) => {
                      setSleepEnd(Number(e.target.value));
                      setSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="col-auto">
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={handleSaveSettings}
                    disabled={saving || !settingsDirty}
                  >
                    {saving ? "Saving…" : "Save"}
                  </button>
                </div>
                <div className="col-auto">
                  <small className="text-muted">
                    Currently: {state.sleep_start_hour}:00 – {state.sleep_end_hour}:00 UTC
                  </small>
                </div>
              </div>
            </div>
          </div>

          {/* Tick log */}
          <div className="card">
            <div className="card-header fw-semibold d-flex justify-content-between align-items-center">
              <span>Recent tick log</span>
              <small className="text-muted fw-normal">last {state.tick_log.length} entries</small>
            </div>
            <div className="card-body p-0">
              {state.tick_log.length === 0 ? (
                <p className="text-muted p-3 mb-0">No ticks recorded yet.</p>
              ) : (
                <div
                  style={{
                    fontFamily: "monospace",
                    fontSize: "0.8rem",
                    maxHeight: "360px",
                    overflowY: "auto",
                  }}
                >
                  {[...state.tick_log].reverse().map((entry, i) => (
                    <div
                      key={i}
                      className="px-3 py-1 border-bottom"
                      style={{ lineHeight: "1.6" }}
                    >
                      <span className="text-muted me-2">
                        {entry.ts ? new Date(entry.ts).toLocaleTimeString([], {
                          hour: "2-digit", minute: "2-digit", second: "2-digit",
                        }) : "—"}
                      </span>
                      <span>{entry.msg}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* Enable confirmation modal */}
      <Modal
        open={showEnableModal}
        title="Enable autonomous mode"
        onClose={() => setShowEnableModal(false)}
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setShowEnableModal(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-success"
              onClick={handleEnable}
              disabled={saving}
            >
              {saving ? "Enabling…" : "Enable"}
            </button>
          </>
        }
      >
        <div className="alert alert-warning mb-3">
          <strong>Agents will work autonomously.</strong>
        </div>
        <p>
          When enabled, the orchestrator will automatically pick open backlog tasks, spawn
          claude sessions to implement them, and run DOD reviews — all without your
          involvement. In-flight tasks cannot be stopped mid-flight.
        </p>
        <p className="mb-0">
          The orchestrator will respect the configured sleep window ({sleepStart}:00 –{" "}
          {sleepEnd}:00 UTC).
        </p>
      </Modal>
    </div>
  );
}
