import { describe, expect, it } from "vitest";
import { automationCopy } from "./AutomationCard";
import type { AutomationSnapshot } from "../api";

/**
 * T-0929 — the AUTOMATION card's headline copy.
 *
 * The WORDING is the deliverable here: he asked to "explicitly see if it's
 * happening in the first place". So the sentence is pinned, not just the data
 * that feeds it — the T-0828 lesson is that all surfaces can normalise a config
 * identically and still print contradictory sentences.
 */

const base: AutomationSnapshot = {
  state: "all_tasks",
  label: "all tasks — drive the backlog end to end",
  running: true,
  paused: false,
  max_in_progress: 0,
  drive: {
    scope: "all",
    stop_when: "scope_exhausted",
    on_stop: "nothing",
    set_by: null,
    set_at: null,
    source_text: null,
    configured: true,
    invalid: {},
    state: "all_tasks",
  },
  quota: {
    max_in_progress: 0,
    weekly_target_pct: null,
    spend_pct: null,
    verdict: null,
  },
  autopilots: [],
  mechanisms: [
    { key: "operator_redrive", why: "respawns the operator", gated: true, active: true },
    { key: "autopilot", why: "re-pings a session", gated: true, active: true },
    { key: "telemetry", why: "samples usage", gated: false, active: true },
  ],
};

describe("automationCopy", () => {
  it("says RUNNING and counts what the switch covers", () => {
    const copy = automationCopy(base);
    expect(copy.status).toBe("running");
    expect(copy.headline).toBe("RUNNING");
    // only the GATED rows are counted — quoting the total would overstate what
    // STOP would actually stop
    expect(copy.sub).toContain("2 automatic mechanisms");
  });

  it("names the autopilots driving a session, because that answers 'why is this working'", () => {
    const copy = automationCopy({
      ...base,
      autopilots: [
        { key: "session-S-x", kind: "session", ref: "S-x", target_sid: "S-x", expires_at: "" },
      ],
    });
    expect(copy.sub).toContain("1 autopilot driving a session");
  });

  it("says STOPPED and points at the way back", () => {
    const copy = automationCopy({
      ...base,
      running: false,
      paused: true,
      state: "off",
      label: "off — nothing automatic runs",
      mechanisms: base.mechanisms.map((m) =>
        m.gated ? { ...m, active: false } : m,
      ),
    });
    expect(copy.status).toBe("stopped");
    expect(copy.headline).toBe("STOPPED");
    expect(copy.sub).toContain("nothing automatic runs");
    expect(copy.sub).toContain("pick a mode");
  });

  it("reports UNKNOWN, never 'stopped', when the worker did not answer", () => {
    // THE point of this case: a confident "nothing is running" that turns out
    // to be wrong is the class of statement this ticket exists to remove. An
    // unreachable worker is an absence of evidence, not evidence of absence.
    for (const missing of [null, undefined]) {
      const copy = automationCopy(missing);
      expect(copy.status).toBe("unknown");
      expect(copy.headline).toBe("UNKNOWN");
      expect(copy.sub).not.toContain("nothing automatic runs");
    }
  });
});
