/**
 * T-0629 (D-0056) — render lock for the scheduler-jobs table relocated onto
 * /system-settings from ObservabilityPanel (lane A/T-0627 removed it there).
 * Mirrors the ObservabilityPanel.test.tsx pattern: exercise the pure render
 * function directly via renderToStaticMarkup, no fetch/DOM needed.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";

import { SchedulerJobsView } from "./SystemSettings";
import type { SchedulerJob } from "../api";

describe("SchedulerJobsView", () => {
  test("renders id/trigger/next-run columns, no heartbeat dot", () => {
    const jobs: SchedulerJob[] = [
      { id: "gc_stale_sessions", trigger: "interval[0:01:00]", next_run: "2099-01-01T00:00:00Z" },
    ];
    const html = renderToStaticMarkup(<SchedulerJobsView jobs={jobs} />);

    expect(html).toContain("gc_stale_sessions");
    expect(html).toContain("interval[0:01:00]");
    expect(html).toContain("in ");
    // WorkerHealthPill owns worker health (D-0056) — no heartbeat dot/badge here.
    expect(html).not.toContain("mc-dot");
    expect(html).not.toContain("heartbeat");
  });

  test("renders an empty-state message with no jobs", () => {
    const html = renderToStaticMarkup(<SchedulerJobsView jobs={[]} />);
    expect(html).toContain("No scheduled jobs.");
  });
});
