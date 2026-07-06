/**
 * T-0593 (T-0588a) — render test for the project-home observability panel,
 * ported from the retired Transparency page's test (T-0511).
 *
 * Renders the pure `ObservabilityView` against the REAL-derived payload
 * observed from the live data dir (operator-state absent → degrade notice;
 * re-drive PAUSED; no pace cap → ∞). `renderToStaticMarkup` needs no DOM, so
 * this runs in the existing node-env vitest (no jsdom/testing-library in this
 * project — effects don't fire during a static render, so the interactive
 * RE-DRIVE/model controls render their SYNCHRONOUS initial state only).
 *
 * T-0627 (D-0056 IA audit): who-does-what table, INITIATIVE PACE card, and
 * Scheduler section died. The RE-DRIVE card gained pause/resume + fleet
 * model — those async happy/error/unavailable paths are unit-tested
 * directly against the extracted `fetchFleetModel`/`setFleetModel`/
 * `pauseOperator`/`resumeOperator` helpers below (no DOM needed).
 *
 * This is the automated lock placed AFTER the manual walkthrough
 * (scenarios/T-0593-*.md, scenarios/T-0627-*.md) per the manual-first rule.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import {
  fetchFleetModel,
  ObservabilityView,
  pauseOperator,
  resumeOperator,
  setFleetModel,
} from "./ObservabilityPanel";
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
    // must keep those OUT of the LIVE SESSIONS count (caught on the manual
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
  test("renders the slimmed summary strip + operator state-doc, no dead sections", () => {
    const html = renderView(REAL_DERIVED);

    // Summary strip: IN PROGRESS, RE-DRIVE (now a control), LIVE SESSIONS.
    expect(html).toContain("IN PROGRESS");
    expect(html).toContain("RE-DRIVE");
    expect(html).toContain("LIVE SESSIONS");

    // Quota: paused + unlimited cap (∞) + in_progress count. Pause control
    // shows a "Resume" button (initial synchronous state from quota.paused).
    expect(html).toContain("PAUSED");
    expect(html).toContain("20 / ∞");
    expect(html).toContain("Resume");

    // Live count = 2 (the archived/suspended row is filtered out) surfaces on
    // the LIVE SESSIONS card, which links to the Processes page — the
    // who-does-what table itself died (T-0627; Processes owns session rows).
    expect(html).toContain(">2<");
    expect(html).toContain("/p/bot-squad/sessions");
    expect(html).not.toContain("Who does what");
    expect(html).not.toContain("S-almdudleer-lifecycle-roles-p235");

    // INITIATIVE PACE card and Scheduler section died (T-0627 — Vision page
    // and /system-settings own those facts respectively).
    expect(html).not.toContain("INITIATIVE PACE");
    expect(html).not.toContain("Scheduler");
    expect(html).not.toContain("configured initiatives");

    // Operator state-doc absent → degrade notice (not a markdown body).
    expect(html).toContain("hasn&#x27;t written a state-doc yet");
    expect(html).toContain("artifacts/operator-state.md");

    // The backlog counts section stays dead (T-0588a) — the board below IS
    // the backlog and the column kickers show the counts.
    expect(html).not.toContain("Backlog");
    expect(html).not.toContain("423");
  });

  test("renders the state-doc body when present, and RUNNING (Pause button) when not paused", () => {
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
    expect(html).toContain("Pause");
    expect(html).not.toContain("hasn&#x27;t written a state-doc yet");
  });
});

// ---------------------------------------------------------------------------
// T-0620/T-0630 operator controls — happy/error/unavailable paths against a
// mocked api client. No DOM/click simulation needed: the component handlers
// are thin wrappers over these exported pure async functions.
// ---------------------------------------------------------------------------
describe("fetchFleetModel", () => {
  test("ok: returns the current model", async () => {
    const outcome = await fetchFleetModel(
      { getWorkerModel: async () => ({ model: "claude-sonnet-5" }) },
      "bot-squad",
    );
    expect(outcome).toEqual({ kind: "ok", data: { model: "claude-sonnet-5" } });
  });

  test("unavailable: a 404 (route not deployed yet) degrades gracefully, not an error", async () => {
    const outcome = await fetchFleetModel(
      {
        getWorkerModel: async () => {
          throw new Error("API error 404: not found");
        },
      },
      "bot-squad",
    );
    expect(outcome).toEqual({ kind: "unavailable" });
  });

  test("error: a non-404 failure surfaces as an error", async () => {
    const outcome = await fetchFleetModel(
      {
        getWorkerModel: async () => {
          throw new Error("API error 500: worker unreachable");
        },
      },
      "bot-squad",
    );
    expect(outcome.kind).toBe("error");
    expect((outcome as { message: string }).message).toContain("500");
  });
});

describe("setFleetModel", () => {
  test("ok: echoes back the saved model", async () => {
    const outcome = await setFleetModel(
      { putWorkerModel: async (_slug, model) => ({ model }) },
      "bot-squad",
      "claude-opus-4-8",
    );
    expect(outcome).toEqual({ kind: "ok", data: { model: "claude-opus-4-8" } });
  });

  test("error: a rejected (non-allowlisted) model surfaces the server message", async () => {
    const outcome = await setFleetModel(
      {
        putWorkerModel: async () => {
          throw new Error("API error 400: model not in allowlist");
        },
      },
      "bot-squad",
      "gpt-5",
    );
    expect(outcome.kind).toBe("error");
  });
});

describe("pauseOperator / resumeOperator", () => {
  test("pauseOperator ok: returns the pause meta", async () => {
    const outcome = await pauseOperator(
      {
        operatorPause: async (_slug, reason) => ({
          ok: true,
          paused: { paused_by: "almdudleer", reason: reason ?? "", paused_at: 1_700_000_000 },
          was_already_paused: false,
        }),
      },
      "bot-squad",
      "hit a rate limit",
    );
    expect(outcome.kind).toBe("ok");
    if (outcome.kind === "ok") {
      expect(outcome.data.paused.reason).toBe("hit a rate limit");
    }
  });

  test("pauseOperator unavailable: 404 before T-0630 lands", async () => {
    const outcome = await pauseOperator(
      {
        operatorPause: async () => {
          throw new Error("API error 404: not found");
        },
      },
      "bot-squad",
    );
    expect(outcome).toEqual({ kind: "unavailable" });
  });

  test("resumeOperator ok: reports was_paused", async () => {
    const outcome = await resumeOperator(
      { operatorResume: async () => ({ ok: true, was_paused: true }) },
      "bot-squad",
    );
    expect(outcome).toEqual({ kind: "ok", data: { ok: true, was_paused: true } });
  });

  test("resumeOperator error: a non-404 failure surfaces inline", async () => {
    const outcome = await resumeOperator(
      {
        operatorResume: async () => {
          throw new Error("API error 403: not a project member");
        },
      },
      "bot-squad",
    );
    expect(outcome.kind).toBe("error");
  });
});
