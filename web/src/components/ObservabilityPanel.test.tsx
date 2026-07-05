/**
 * T-0593 (T-0588a) — render test for the project-home observability panel,
 * ported from the retired Transparency page's test (T-0511).
 *
 * Renders the pure `ObservabilityView` against the REAL-derived payload
 * observed from the live data dir (operator-state absent → degrade notice;
 * re-drive PAUSED; no pace cap → ∞; a 2-row session tree). The backlog
 * asserts died with the backlog section (the board below IS the backlog).
 * `renderToStaticMarkup` needs no DOM, so this runs in the existing node-env
 * vitest. This is the automated lock placed AFTER the manual walkthrough
 * (scenarios/T-0593-*.md) per the manual-first rule (T-0158).
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { ObservabilityView } from "./ObservabilityPanel";
import type { Transparency as TransparencyData } from "../api";

// Mirrors the real bot-squad state observed 2026-06-27 (operator-state absent,
// paused=true, no pace.json, 20 in_progress), trimmed to the panel's inputs.
const REAL_DERIVED: TransparencyData = {
  slug: "bot-squad",
  operator_state: {
    exists: false,
    content: null,
    updated_at: null,
    path: "artifacts/operator-state.md",
  },
  sessions: [
    {
      sid: "S-almdudleer-lifecycle-roles-p235",
      status: "active",
      activity: "idle",
      role: "teamlead",
      task_id: null,
      window: "w",
      cwd: "/x",
      pinned: false,
    } as TransparencyData["sessions"][number],
    {
      sid: "S-almdudleer-m11-system-transparency-p265",
      status: "active",
      activity: "running",
      role: "dev",
      task_id: "T-0511",
      window: "w",
      cwd: "/x",
      pinned: true,
    } as TransparencyData["sessions"][number],
    // T-0593: the live payload is ~97% suspended/archived history — the panel
    // must keep those OUT of the live who-does-what (caught on the manual
    // walkthrough: a straight port read "LIVE SESSIONS 341").
    {
      sid: "S-almdudleer-dead-worker-p1",
      status: "suspended",
      activity: "suspended",
      role: "dev",
      task_id: null,
      window: "w",
      cwd: "/x",
      pinned: false,
      archived: true,
    } as TransparencyData["sessions"][number],
  ],
  backlog: {
    counts: {
      planned: 54,
      open: 5,
      in_progress: 20,
      totest: 10,
      reopened: 0,
      closed: 423,
    },
    tasks: [
      { id: "T-0511", title: "M11 transparency", status: "in_progress" },
      { id: "T-0473", title: "operator state-doc", status: "closed" },
    ] as TransparencyData["backlog"]["tasks"],
  },
  quota: { max_in_progress: 0, in_progress: 20, paused: true, initiatives: {} },
};

function renderView(data: TransparencyData): string {
  return renderToStaticMarkup(
    <StaticRouter location={`/p/${data.slug}`}>
      <ObservabilityView data={data} slug={data.slug} />
    </StaticRouter>,
  );
}

describe("ObservabilityView", () => {
  test("renders the summary strip + detail sections, live-only, no backlog", () => {
    const html = renderView(REAL_DERIVED);

    // Summary strip: quota cards + the live-session count.
    expect(html).toContain("IN PROGRESS");
    expect(html).toContain("RE-DRIVE");
    expect(html).toContain("INITIATIVE PACE");
    expect(html).toContain("LIVE SESSIONS");

    // Quota: paused + unlimited cap (∞) + in_progress count.
    expect(html).toContain("PAUSED");
    expect(html).toContain("20 / ∞");

    // Live count = 2 (the archived/suspended row is filtered out).
    expect(html).toContain("Who does what (2)");
    expect(html).not.toContain("S-almdudleer-dead-worker-p1");

    // Session tree: both live rows, the pinned marker, a task deep-link, and
    // the pointer to the full history on the Processes page.
    expect(html).toContain("S-almdudleer-lifecycle-roles-p235");
    expect(html).toContain("📌");
    expect(html).toContain("/p/bot-squad/t/T-0511");
    expect(html).toContain("/p/bot-squad/sessions");

    // Operator state-doc absent → degrade notice (not a markdown body).
    expect(html).toContain("hasn&#x27;t written a state-doc yet");
    expect(html).toContain("artifacts/operator-state.md");

    // The backlog counts section DIED (T-0588a) — the board below IS the
    // backlog and CanonicalSummary shows the counts.
    expect(html).not.toContain("Backlog");
    expect(html).not.toContain("423");
  });

  test("renders the state-doc body when present, and RUNNING when not paused", () => {
    const html = renderView({
      ...REAL_DERIVED,
      operator_state: {
        exists: true,
        content: "## Priorities\n\nShip the transparency surface.",
        updated_at: 1_700_000_000,
        path: "artifacts/operator-state.md",
      },
      quota: { ...REAL_DERIVED.quota, paused: false, max_in_progress: 13 },
    });
    expect(html).toContain("Ship the transparency surface.");
    expect(html).toContain("RUNNING");
    expect(html).toContain("20 / 13");
    expect(html).not.toContain("hasn&#x27;t written a state-doc yet");
  });
});
