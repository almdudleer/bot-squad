import { useEffect, useState } from "react";
import { Modal } from "./Modal";
import { useApiClient } from "../apiContext";
import type { AutopilotKind } from "../api";

// T-0153: the Autopilot dialog — opened from the kebab popover on a team lane,
// a single session row, or the project header. Collects duration + prompt +
// early-exit condition (+ optional stall threshold) and kicks off a time-boxed
// autonomous run whose brief is delivered to the target TL (spawned if needed).

export type AutopilotTarget = {
  kind: AutopilotKind;
  ref?: string; // team name / session SID; omitted (→ slug) for project
  label: string; // human-readable target for the dialog title
};

export function AutopilotDialog({
  open,
  slug,
  target,
  onClose,
  onStarted,
}: {
  open: boolean;
  slug: string;
  target: AutopilotTarget | null;
  onClose: () => void;
  onStarted?: (key: string, targetSid: string) => void;
}) {
  const api = useApiClient();
  const [durationHours, setDurationHours] = useState("8");
  const [prompt, setPrompt] = useState("");
  const [earlyExit, setEarlyExit] = useState("");
  const [stallMinutes, setStallMinutes] = useState("60");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  // Reset the form each time the dialog opens for a (new) target.
  useEffect(() => {
    if (open) {
      setDurationHours("8");
      setPrompt("");
      setEarlyExit("");
      setStallMinutes("60");
      setError(null);
      setInfo(null);
      setBusy(false);
    }
  }, [open, target?.kind, target?.ref]);

  async function handleStart() {
    if (!target) return;
    const dur = parseFloat(durationHours);
    if (!Number.isFinite(dur) || dur <= 0) {
      setError("Duration must be a positive number of hours");
      return;
    }
    if (!prompt.trim()) {
      setError("Autopilot prompt is required");
      return;
    }
    const stall = parseInt(stallMinutes, 10);
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      const res = await api.autopilotStart(slug, {
        kind: target.kind,
        ref: target.ref,
        prompt: prompt.trim(),
        early_exit: earlyExit.trim() || undefined,
        duration_hours: dur,
        stall_minutes: Number.isFinite(stall) && stall > 0 ? stall : undefined,
      });
      setInfo(
        `Autopilot started → ${res.target_sid}${res.spawned ? " (spawned new TL)" : ""}. ` +
          `Ends ${res.expires_at}.`,
      );
      onStarted?.(res.key, res.target_sid);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title={`Autopilot — ${target?.label ?? ""}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {info ? "Close" : "Cancel"}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleStart}
            disabled={busy || !!info}
          >
            {busy ? "Starting…" : "Start autopilot"}
          </button>
        </>
      }
    >
      {error && <div className="alert alert-danger">{error}</div>}
      {info && <div className="alert alert-success">{info}</div>}
      <div className="mb-2" style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
        The target {target?.kind} TL receives this brief plainly (live tmux pane +
        peer inbox). A stall watchdog re-pings if no progress (commits / progress
        notes) is seen within the threshold, and the run auto-ends at the duration —
        notifying you either way. The TL exits early the moment the early-exit
        condition is met.
      </div>

      <div className="mb-3">
        <label className="form-label">Duration (hours)</label>
        <input
          type="number"
          min="0.1"
          step="0.5"
          className="form-control"
          value={durationHours}
          onChange={(e) => setDurationHours(e.target.value)}
        />
      </div>

      <div className="mb-3">
        <label className="form-label">Autopilot prompt</label>
        <textarea
          className="form-control"
          rows={5}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="What should the TL drive autonomously for this run? e.g. 'Clear the operator-ux backlog: pick the highest-priority open ticket, spawn a dev, review + ship, repeat.'"
          autoFocus
        />
      </div>

      <div className="mb-3">
        <label className="form-label">Quit autopilot early if…</label>
        <textarea
          className="form-control"
          rows={2}
          value={earlyExit}
          onChange={(e) => setEarlyExit(e.target.value)}
          placeholder="Early-exit condition the TL checks each tick, e.g. 'the backlog has no open tickets left' or 'a deploy fails twice'."
        />
      </div>

      <div className="mb-2">
        <label className="form-label">
          Stall threshold (minutes){" "}
          <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>— optional, default 60</span>
        </label>
        <input
          type="number"
          min="1"
          step="1"
          className="form-control"
          value={stallMinutes}
          onChange={(e) => setStallMinutes(e.target.value)}
        />
      </div>
    </Modal>
  );
}
