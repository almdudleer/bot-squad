import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, AutonomousState } from "../api";
import { Modal } from "../components/Modal";

import { PageHelp } from "../components/PageHelp";
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

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    idle:      "mc-badge-dim",
    working:   "mc-badge-active",
    reviewing: "mc-badge-info",
    sleeping:  "mc-badge-warn",
  };
  const dotMap: Record<string, string> = {
    idle:      "mc-dot-idle",
    working:   "mc-dot-active",
    reviewing: "mc-dot-active",
    sleeping:  "mc-dot-warn",
  };
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
      <span className={`mc-dot ${dotMap[status] ?? "mc-dot-idle"}`} />
      <span className={`mc-badge ${map[status] ?? "mc-badge-dim"}`}>{status}</span>
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

  const [showEnableModal, setShowEnableModal] = useState(false);

  const [sleepStart, setSleepStart] = useState<number>(22);
  const [sleepEnd, setSleepEnd] = useState<number>(8);
  const [settingsDirty, setSettingsDirty] = useState(false);

  const load = useCallback(() => {
    api
      .autonomousStatus(slug)
      .then((s) => {
        setState(s);
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
        await api.autonomousEnable(slug, sleepStart, sleepEnd);
      } else {
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
      {/* Page header */}
      <div className="d-flex justify-content-between align-items-center mb-1">
        <div className="d-flex align-items-center gap-3">
          <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>Autonomous team</h2>
          {state && <StatusBadge status={state.status} />}
        </div>
        <div className="d-flex align-items-center gap-2">
          <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
            auto-refresh 15s
            {lastRefresh && (
              <span style={{ marginLeft: "0.4rem" }}>
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
      <PageHelp>
        One-task-at-a-time orchestrator. When enabled, picks the next open backlog task,
        spawns a Claude pane to work it, reviews against Definition-of-Done, marks
        <code> closed</code> or reopens with feedback. Honors a sleep window
        (default 22:00–08:00 UTC). Defaults to <strong>disabled</strong>.
      </PageHelp>

      {/* Errors */}
      {error && <div className="alert alert-danger">{error}</div>}
      {actionError && (
        <div className="alert alert-warning d-flex justify-content-between align-items-center">
          <span>{actionError}</span>
          <button
            type="button"
            className="btn-close"
            style={{ filter: "invert(1) opacity(0.5)" }}
            onClick={() => setActionError(null)}
          />
        </div>
      )}

      {/* Loading */}
      {state === null && !error && <div className="mc-loading">Loading</div>}

      {state !== null && (
        <>
          {/* Working banner */}
          {state.status === "working" && state.current_task_id && (
            <div className="alert alert-primary d-flex align-items-center gap-2 mb-4">
              <span className="mc-dot mc-dot-active" />
              <span>
                Working on{" "}
                <Link to={`/p/${slug}/t/${state.current_task_id}`} style={{ fontWeight: 600 }}>
                  {state.current_task_id}
                </Link>
                {state.current_pane_id && (
                  <span
                    style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", color: "var(--mc-accent-dim)", marginLeft: "0.5rem" }}
                  >
                    · pane {state.current_pane_id}
                  </span>
                )}
                {state.current_started_at && (
                  <span style={{ fontSize: "0.78rem", color: "var(--mc-accent-dim)", marginLeft: "0.5rem" }}>
                    · started {relTime(state.current_started_at)}
                  </span>
                )}
              </span>
            </div>
          )}

          {/* Status cards */}
          <div className="row g-3 mb-4">
            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <div className="card-subtitle mb-2">Status</div>
                  <div style={{ fontFamily: "var(--mc-mono)", fontSize: "1rem", fontWeight: 600, textTransform: "capitalize" }}>
                    {state.status}
                  </div>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <div className="card-subtitle mb-2">Current task</div>
                  <div style={{ fontFamily: "var(--mc-mono)", fontSize: "1rem", fontWeight: 600 }}>
                    {state.current_task_id ? (
                      <Link to={`/p/${slug}/t/${state.current_task_id}`} style={{ color: "var(--mc-accent)" }}>
                        {state.current_task_id}
                      </Link>
                    ) : (
                      <span style={{ color: "var(--mc-text-dim)" }}>—</span>
                    )}
                  </div>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <div className="card-subtitle mb-2">Last tick</div>
                  <div style={{ fontFamily: "var(--mc-mono)", fontSize: "1rem", fontWeight: 600 }}>
                    {relTime(state.last_tick_at)}
                  </div>
                </div>
              </div>
            </div>

            <div className="col-sm-6 col-md-3">
              <div className="card h-100">
                <div className="card-body">
                  <div className="card-subtitle mb-2">Enabled</div>
                  <div style={{ marginTop: "0.3rem", display: "flex", alignItems: "center", gap: "0.35rem" }}>
                    <span className={`mc-dot ${isEnabled ? "mc-dot-active" : "mc-dot-idle"}`} />
                    <span className={`mc-badge ${isEnabled ? "mc-badge-ok" : "mc-badge-dim"}`}>
                      {isEnabled ? "yes" : "no"}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Settings card */}
          <div className="card mb-4">
            <div className="card-header">Sleep window settings</div>
            <div className="card-body">
              <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)", marginBottom: "1rem" }}>
                No new tasks are spawned during the sleep window. In-flight tasks complete normally.
              </p>
              <div className="row g-3 align-items-end">
                <div className="col-auto">
                  <label className="form-label">Sleep start (UTC hour)</label>
                  <input
                    type="number"
                    className="form-control form-control-sm"
                    min={0}
                    max={23}
                    value={sleepStart}
                    style={{ width: "80px" }}
                    onChange={(e) => { setSleepStart(Number(e.target.value)); setSettingsDirty(true); }}
                  />
                </div>
                <div className="col-auto">
                  <label className="form-label">Sleep end (UTC hour)</label>
                  <input
                    type="number"
                    className="form-control form-control-sm"
                    min={0}
                    max={23}
                    value={sleepEnd}
                    style={{ width: "80px" }}
                    onChange={(e) => { setSleepEnd(Number(e.target.value)); setSettingsDirty(true); }}
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
                  <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.75rem", color: "var(--mc-text-dim)" }}>
                    {state.sleep_start_hour}:00 – {state.sleep_end_hour}:00 UTC
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Tick log */}
          <div className="card">
            <div
              className="card-header d-flex justify-content-between align-items-center"
              style={{ textTransform: "none", letterSpacing: 0, fontSize: "0.78rem" }}
            >
              <span>Recent tick log</span>
              <span style={{ color: "var(--mc-text-dim)", fontSize: "0.7rem" }}>
                last {state.tick_log.length} entries
              </span>
            </div>
            <div className="card-body p-0">
              {state.tick_log.length === 0 ? (
                <div className="mc-empty" style={{ padding: "1.5rem" }}>
                  <div>No ticks recorded yet.</div>
                </div>
              ) : (
                <div className="mc-tick-log">
                  {[...state.tick_log].reverse().map((entry, i) => (
                    <div key={i} className="mc-tick-entry">
                      <span className="mc-tick-ts">
                        {entry.ts
                          ? new Date(entry.ts).toLocaleTimeString([], {
                              hour: "2-digit",
                              minute: "2-digit",
                              second: "2-digit",
                            })
                          : "—"}
                      </span>
                      <span className="mc-tick-msg">{entry.msg}</span>
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
        <p style={{ fontSize: "0.85rem", color: "var(--mc-text-mid)" }}>
          When enabled, the orchestrator will automatically pick open backlog tasks, spawn
          claude sessions to implement them, and run DOD reviews — all without your
          involvement. In-flight tasks cannot be stopped mid-flight.
        </p>
        <p style={{ fontSize: "0.85rem", color: "var(--mc-text-mid)", marginBottom: 0 }}>
          The orchestrator will respect the configured sleep window ({sleepStart}:00 –{" "}
          {sleepEnd}:00 UTC).
        </p>
      </Modal>
    </div>
  );
}
