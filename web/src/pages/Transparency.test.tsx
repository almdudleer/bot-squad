/**
 * T-0511 (M11-F11.4) — render test for the unified transparency surface.
 *
 * Renders the pure `TransparencyView` against the REAL-derived payload observed
 * from the live data dir (operator-state absent → degrade notice; re-drive
 * PAUSED; no pace cap → ∞; 512-task backlog counts; a 2-row session tree).
 * `renderToStaticMarkup` needs no DOM, so this runs in the existing node-env
 * vitest. This is the automated lock placed AFTER the manual walkthrough
 * (scenarios/T-0511-*.md) per the manual-first rule (T-0158).
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { TransparencyView } from "./Transparency";
import type { Transparency as TransparencyData } from "../api";

// Mirrors the real bot-squad state observed 2026-06-27 (operator-state absent,
// paused=true, no pace.json, 512-task backlog with 20 in_progress).
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
    <StaticRouter location={`/p/${data.slug}/transparency`}>
      <TransparencyView data={data} slug={data.slug} />
    </StaticRouter>,
  );
}

describe("TransparencyView", () => {
  test("renders all four sections + the live degrade/quota states", () => {
    const html = renderView(REAL_DERIVED);
    // Manual observation aid: dump the rendered text for the walkthrough.
    // eslint-disable-next-line no-console
    console.log("RENDERED:", html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim());

    // Four section headers.
    expect(html).toContain("Operator state-doc");
    expect(html).toContain("Quota");
    expect(html).toContain("Session tree");
    expect(html).toContain("Backlog");

    // Operator state-doc absent → degrade notice (not a markdown body).
    expect(html).toContain("hasn&#x27;t written a state-doc yet");
    expect(html).toContain("artifacts/operator-state.md");

    // Quota: paused + unlimited cap (∞) + in_progress count.
    expect(html).toContain("PAUSED");
    expect(html).toContain("20 / ∞");

    // Session tree: both rows, the pinned marker, and a task deep-link.
    expect(html).toContain("S-almdudleer-lifecycle-roles-p235");
    expect(html).toContain("📌");
    expect(html).toContain("/p/bot-squad/t/T-0511");

    // Backlog counts surfaced.
    expect(html).toContain("423"); // closed
    expect(html).toContain("2 tasks total");
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
