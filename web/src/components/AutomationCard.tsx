import { useCallback, useEffect, useState } from "react";
import { api, type AutomationSnapshot, type DriveState } from "../api";

/**
 * T-0929 — the project's AUTOMATION card: is anything automatic running, and
 * the one switch that stops all of it.
 *
 * > "And there should be explicit UI state where I could easily turn them off
 * > for a project, just stop any automatic activity all at once. And explicitly
 * > see if it's happening in the first place. Now that's all very unclear there
 * > and uncontrollable, and there are many ways in which the system operates
 * > around this mechanism. But that should be the ultimate switch."
 * > — the stakeholder, 2026-08-28.
 *
 * ⚠ **This supersedes T-0674 for this one control.** That ticket's 2026-07-25
 * verdict cut every web-side operator lever ("pause/resume now live in TG/CLI
 * only") and named exactly one survivor. This ask is LATER and names this
 * control in his own words, so it wins here; the rest of that cut stands, and
 * this file adds no second lever beyond the one he asked for.
 *
 * THREE THINGS THE CARD MUST DO, in this order — the order is the design:
 *
 * 1. **Say whether it is happening.** RUNNING / STOPPED comes before the mode,
 *    because a mode printed beside an engaged switch is the reported confusion.
 * 2. **Say what the switch covers.** The mechanism list is served by the worker
 *    (one registry, `automation.MECHANISMS`), never re-listed here — a
 *    hand-maintained copy in the UI would drift and then LIE about what STOP
 *    stopped, which is this ticket's own root cause with a new face.
 * 3. **Be one click.** STOP is a single button, and picking any mode is what
 *    turns it back on. There is no separate "resume".
 */

/** The button copy for each settable state. Order = his own bullet order. */
const STATE_BUTTONS: { state: DriveState; label: string; hint: string }[] = [
  {
    state: "all_tasks",
    label: "All tasks",
    hint: "drive the backlog end to end while tasks exist",
  },
  {
    state: "finish_up",
    label: "Finish up",
    hint: "close what is in progress, take nothing new",
  },
  {
    state: "one_task",
    label: "One task",
    hint: "finish one before starting another (max-in-progress 1)",
  },
];

/**
 * The card's headline copy, as a pure function of the snapshot. Exported and
 * pure for the same reason `driveModeCopy` is: the WORDING is the deliverable,
 * so it has to be assertable without rendering.
 *
 * A missing snapshot is NOT reported as "stopped". The worker being unreachable
 * means we do not know, and a confident "nothing is running" that turns out to
 * be wrong is exactly the class of statement this ticket exists to remove.
 */
export function automationCopy(snap: AutomationSnapshot | null | undefined): {
  status: "running" | "stopped" | "unknown";
  headline: string;
  sub: string;
} {
  if (!snap) {
    return {
      status: "unknown",
      headline: "UNKNOWN",
      sub: "the worker did not answer — this says nothing about whether work is running",
    };
  }
  if (!snap.running) {
    return {
      status: "stopped",
      headline: "STOPPED",
      sub: "nothing automatic runs — pick a mode below to start again",
    };
  }
  const covered = snap.mechanisms.filter((m) => m.gated).length;
  const aps = snap.autopilots.length;
  return {
    status: "running",
    headline: "RUNNING",
    sub:
      `${covered} automatic mechanisms are live` +
      (aps ? ` · ${aps} autopilot${aps === 1 ? "" : "s"} driving a session` : ""),
  };
}

const STATUS_COLOR: Record<string, string> = {
  running: "var(--mc-green, #3fb950)",
  stopped: "var(--mc-red)",
  unknown: "var(--mc-text-dim)",
};

export function AutomationCard({ slug }: { slug: string }) {
  const [snap, setSnap] = useState<AutomationSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<DriveState | null>(null);

  const load = useCallback(() => {
    let cancelled = false;
    api
      .automation(slug)
      .then((d) => {
        if (!cancelled) {
          setSnap(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  useEffect(() => {
    setSnap(null);
    setError(null);
    return load();
  }, [slug, load]);

  // The POST returns the RESULTING snapshot, so the card renders what the
  // system now IS rather than what we asked for. "changes state without my
  // confirmation" is a report about surfaces that claim a state they do not
  // have; echoing the request back would reproduce it.
  const setState = (state: DriveState) => {
    setBusy(state);
    api
      .setDriveState(slug, state)
      .then((d) => {
        setSnap(d);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(null));
  };

  const copy = automationCopy(snap);
  const current = snap?.state;

  return (
    <div
      className="mc-an-card"
      style={{ gridColumn: "1 / -1", textAlign: "left" }}
      data-testid="automation-card"
    >
      <div className="mc-an-card-label">AUTOMATION</div>

      {/* 1 — is it happening at all */}
      <div
        style={{
          fontFamily: "var(--mc-mono)",
          fontSize: "1.05rem",
          fontWeight: 600,
          color: STATUS_COLOR[copy.status],
          marginTop: "0.15rem",
        }}
        data-testid="automation-status"
      >
        {copy.headline}
      </div>
      <div className="mc-an-card-sub">{copy.sub}</div>

      {snap && (
        <div
          className="mc-an-card-sub"
          style={{ marginTop: "0.25rem" }}
          data-testid="automation-state"
        >
          drive state: <strong>{snap.state}</strong> — {snap.label}
          {/* PROVENANCE BELONGS TO THE REQUEST, NOT THE RESULT (T-0828's rule,
              and the walkthrough for this ticket caught this surface breaking
              it). `source_text` is the words that set the CONFIGURED state; the
              effective state is `off` whenever the switch is engaged, and
              printing "off — … from «закончить всё что в опен»" attributes a
              stop to words that asked for a mode. So while off, the line says
              what the switch will return TO instead. */}
          {snap.paused ? (
            <> · returns to {snap.drive?.state ?? "all_tasks"} when restarted</>
          ) : snap.drive?.source_text ? (
            <> · from «{snap.drive.source_text}»</>
          ) : null}
        </div>
      )}

      {/* The caps + targets that go WITH the state. He named them in the same
          sentence as the states, and they live in two different stores — which
          is exactly why they belong on one line here. An absent signal says so
          rather than being omitted: a missing line reads as "no target", which
          is a different fact. */}
      {snap && (
        <div className="mc-an-card-sub" data-testid="automation-quota">
          cap: max-in-progress{" "}
          {snap.quota.max_in_progress ? snap.quota.max_in_progress : "∞"} · weekly
          quota target{" "}
          {snap.quota.weekly_target_pct !== null
            ? `${snap.quota.weekly_target_pct}%`
            : "(none set)"}
          {" · "}spent{" "}
          {snap.quota.spend_pct !== null
            ? `${snap.quota.spend_pct.toFixed(1)}%`
            : "unknown (no quota anchor)"}
          {snap.quota.verdict ? ` · ${snap.quota.verdict} pace` : ""}
        </div>
      )}

      {/* 2 — the switch, and the modes. Picking a mode IS turning it on, so
          there is no separate resume control to get out of step with STOP. */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.4rem",
          marginTop: "0.6rem",
        }}
      >
        <button
          type="button"
          className="btn btn-sm btn-danger"
          disabled={busy !== null || current === "off"}
          onClick={() => setState("off")}
          data-testid="automation-stop"
          title="Stop every automatic mechanism for this project, at once"
        >
          {busy === "off" ? "stopping…" : "STOP EVERYTHING"}
        </button>
        {STATE_BUTTONS.map((b) => (
          <button
            key={b.state}
            type="button"
            className={
              current === b.state
                ? "btn btn-sm btn-primary"
                : "btn btn-sm btn-outline-secondary"
            }
            disabled={busy !== null}
            onClick={() => setState(b.state)}
            data-testid={`automation-state-${b.state}`}
            title={b.hint}
          >
            {busy === b.state ? "…" : b.label}
          </button>
        ))}
      </div>

      {error && (
        <div
          style={{
            fontSize: "0.72rem",
            color: "var(--mc-red)",
            marginTop: "0.4rem",
            wordBreak: "break-word",
          }}
          data-testid="automation-error"
        >
          ⚠ {error}
        </div>
      )}

      {/* 3 — what the switch actually covers. Served by the worker's single
          registry; never re-listed in this file. */}
      {snap && (
        <details style={{ marginTop: "0.5rem" }}>
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
            what this switch covers
          </summary>
          <ul
            style={{
              fontSize: "0.74rem",
              margin: "0.4rem 0 0",
              paddingLeft: "1.1rem",
            }}
            data-testid="automation-mechanisms"
          >
            {snap.mechanisms.map((m) => (
              <li
                key={m.key}
                style={{
                  color: m.active ? "var(--mc-text)" : "var(--mc-text-dim)",
                }}
              >
                <code>{m.key}</code>{" "}
                {m.gated ? (m.active ? "· on" : "· OFF") : "· on (not covered)"}{" "}
                — {m.why}
              </li>
            ))}
          </ul>
        </details>
      )}

      {snap && snap.autopilots.length > 0 && (
        <div
          className="mc-an-card-sub"
          style={{ marginTop: "0.4rem" }}
          data-testid="automation-autopilots"
        >
          running autopilots:{" "}
          {snap.autopilots
            .map((a) => `${a.key} → ${a.target_sid}`)
            .join(", ")}
        </div>
      )}
    </div>
  );
}
