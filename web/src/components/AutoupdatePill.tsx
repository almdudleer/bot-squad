import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AutoupdateStatus } from "../api";
import { useAnchorRect, anchoredBelowLeft } from "./useAnchorRect";

/**
 * AutoupdatePill — consumer-side header chip surfacing the install's
 * autoupdate state (T-0089).
 *
 * Three visible states the pill paints:
 *  - **default** — "v<installed> · checked Nm ago · next in Mm"
 *  - **applying** — "applying v<pending>…" with a pulsing badge
 *  - **failed**  — red badge "v<x> failed at <step> — open" linking the alert
 *
 * Click opens a small popover with three operator levers:
 *  - pause / unpause toggle (T-0089 flag file)
 *  - "check now" → POST /api/autoupdate/check_now (re-fires the poller tick)
 *  - "release notes" → links to <mothership>/api/releases/<version>
 *
 * Tree-shake on the mothership build: this file imports nothing
 * mothership-specific, but the Shell only renders <AutoupdatePill /> when
 * VITE_MOTHERSHIP !== "1" so the bundle on a mothership install never
 * fetches /api/autoupdate/* (which would 404 anyway).
 */
const POLL_MS = 30_000;
const CHECK_NOW_RECOVER_MS = 5_000;

function formatAge(iso: string | null, now: number): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "never";
  const sec = Math.max(0, Math.floor((now - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  return `${d}d ago`;
}

function formatCountdown(iso: string | null, now: number): string {
  if (!iso) return "soon";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "soon";
  const sec = Math.max(0, Math.floor((t - now) / 1000));
  if (sec === 0) return "now";
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  return `${hr}h`;
}

type PillKind = "ok" | "active" | "danger" | "warn" | "dim";

function pickKind(status: AutoupdateStatus | null): PillKind {
  if (!status) return "dim";
  if (status.alert) return "danger";
  if (status.pending_apply_version && !status.paused) return "active";
  if (status.paused) return "warn";
  if (status.last_apply_outcome.startsWith("failed:")) return "danger";
  return "ok";
}

function pillLabel(status: AutoupdateStatus | null, now: number): string {
  if (!status) return "AUTOUPDATE · loading…";
  if (status.alert) {
    return `${status.alert.version} failed at ${status.alert.step} — open`;
  }
  if (status.pending_apply_version && !status.paused) {
    return `applying ${status.pending_apply_version}…`;
  }
  const v = status.installed_version ?? "—";
  const checked = formatAge(status.last_check_at, now);
  if (status.paused) {
    return `${v} · paused · checked ${checked}`;
  }
  const next = formatCountdown(status.next_check_at, now);
  return `${v} · checked ${checked} · next in ${next}`;
}

export function AutoupdatePill() {
  const [status, setStatus] = useState<AutoupdateStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  // Pending action state — separate from status so a click can disable the
  // button without waiting for the next poll cycle to confirm.
  const [busy, setBusy] = useState<"pause" | "check" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  // T-0140: fixed-position the popover off the trigger so the sidebar overflow
  // clip can't crop it at the rail edge (see useAnchorRect).
  const anchorRect = useAnchorRect(triggerRef, open);

  const refresh = useCallback(async () => {
    try {
      const next = await api.autoupdateStatus();
      setStatus(next);
    } catch {
      // 404 on mothership build is filtered upstream (Shell doesn't mount
      // the pill). Any other failure: keep the last good status; the next
      // poll retries on its own.
      setStatus(null);
    }
  }, []);

  // Initial fetch + 30s poll.
  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  // Tick `now` once a second so the countdown text doesn't go stale between
  // polls. Cheap because it only re-renders this tiny component, not the
  // whole shell.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  // Close popover on outside click + Escape.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (!popoverRef.current) return;
      if (popoverRef.current.contains(e.target as Node)) return;
      setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function togglePause() {
    if (!status) return;
    setBusy("pause");
    setError(null);
    try {
      const next = !status.paused;
      await api.autoupdatePause(next);
      // Optimistic patch so the popover reflects immediately; refresh
      // reconciles with server truth.
      setStatus({ ...status, paused: next });
      void refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function checkNow() {
    setBusy("check");
    setError(null);
    try {
      await api.autoupdateCheckNow();
      // The tick runs async on the worker; give it a beat to update
      // last_check_at, then refresh. The 30s poll covers slower cases.
      window.setTimeout(() => void refresh(), CHECK_NOW_RECOVER_MS);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  // Don't render anything until the first /status call resolves — avoids
  // a blank pill flashing in the header on every page load.
  if (status === null) return null;

  const kind = pickKind(status);
  const label = pillLabel(status, now);
  const releaseNotesHref =
    status.mothership_url && status.installed_version
      ? `${status.mothership_url}/api/releases/${status.installed_version}`
      : null;

  return (
    <div className="mc-autoupdate-pill" ref={popoverRef}>
      <button
        type="button"
        ref={triggerRef}
        className={`mc-badge mc-badge-${kind} mc-autoupdate-pill-trigger`}
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title={status.alert ? "Autoupdate failed — click for details" : "Autoupdate status"}
      >
        {label}
      </button>
      {open && (
        <div
          className="mc-autoupdate-popover"
          role="dialog"
          aria-label="Autoupdate controls"
          style={anchoredBelowLeft(anchorRect)}
        >
          <div className="mc-autoupdate-popover-row">
            <span className="mc-autoupdate-popover-label">installed</span>
            <span className="mc-mono">{status.installed_version ?? "—"}</span>
          </div>
          {status.alert && (
            <div className="mc-autoupdate-popover-row mc-autoupdate-popover-alert">
              <span className="mc-autoupdate-popover-label">failed</span>
              <span className="mc-mono">
                {status.alert.version} at {status.alert.step}
              </span>
            </div>
          )}
          {status.pending_apply_version && !status.alert && (
            <div className="mc-autoupdate-popover-row">
              <span className="mc-autoupdate-popover-label">pending</span>
              <span className="mc-mono">{status.pending_apply_version}</span>
            </div>
          )}
          <div className="mc-autoupdate-popover-row">
            <span className="mc-autoupdate-popover-label">last check</span>
            <span className="mc-mono">{formatAge(status.last_check_at, now)}</span>
          </div>

          <div className="mc-autoupdate-popover-divider" />

          <button
            type="button"
            className="mc-autoupdate-popover-button"
            onClick={togglePause}
            disabled={busy !== null}
          >
            {status.paused ? "Resume autoupdate" : "Pause autoupdate"}
          </button>
          <button
            type="button"
            className="mc-autoupdate-popover-button"
            onClick={checkNow}
            disabled={busy !== null}
          >
            {busy === "check" ? "Checking…" : "Check now"}
          </button>
          {releaseNotesHref && (
            <a
              className="mc-autoupdate-popover-button mc-autoupdate-popover-link"
              href={releaseNotesHref}
              target="_blank"
              rel="noreferrer noopener"
            >
              Release notes ↗
            </a>
          )}
          {error && <div className="mc-autoupdate-popover-error">{error}</div>}
        </div>
      )}
    </div>
  );
}
