/**
 * T-0593 (T-0588a) — render test for the project-home observability panel,
 * ported from the retired Transparency page's test (T-0511).
 *
 * Renders the pure `ObservabilityView` against the REAL-derived payload
 * observed from the live data dir (operator-state absent; no pace cap → ∞).
 * `renderToStaticMarkup` needs no DOM, so this runs in the existing node-env
 * vitest (no jsdom/testing-library in this project).
 *
 * T-0627 (D-0056 IA audit): who-does-what table, INITIATIVE PACE card, and
 * Scheduler section died.
 *
 * T-0674 (D-0057 §4/§8): the RE-DRIVE pause/resume + fleet-model control
 * (T-0620) is cut entirely — its happy/error/unavailable async-helper tests
 * went with it. The strip is IN PROGRESS + LIVE SESSIONS only now.
 *
 * This is the automated lock placed AFTER the manual walkthrough
 * (scenarios/T-0593-*.md, scenarios/T-0627-*.md) per the manual-first rule.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import {
  ObservabilityView,
  inProgressCardCopy,
  liveSessionsCardCopy,
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

    // Summary strip: IN PROGRESS + LIVE SESSIONS only — RE-DRIVE cut (T-0674).
    expect(html).toContain("IN PROGRESS");
    expect(html).not.toContain("RE-DRIVE");
    expect(html).toContain("LIVE SESSIONS");

    // Quota: the in_progress count, and a cap line that names its own unit.
    // T-0966 removed the "20 / ∞" ratio — the two are different units.
    expect(html).toContain(">20<");
    expect(html).toContain("tickets labelled in_progress · no cap set");
    expect(html).not.toContain("20 / ∞");
    expect(html).not.toContain("Resume");
    expect(html).not.toContain("PAUSED");

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

    // Work-state doc absent → degrade notice (not a markdown body). T-0942
    // renamed it and widened who writes it, so the copy no longer says "the
    // operator hasn't" — the doc is the PROJECT's and any project-level role
    // holder writes it.
    expect(html).toContain("Nobody has written the work-state doc yet");
    // The PATH still reads as the legacy name here on purpose: this fixture has
    // `exists: false`, and the api falls back to `operator-state.md` when
    // `work-state.md` is not on disk yet, which is the pre-migration state.
    expect(html).toContain("artifacts/operator-state.md");

    // The backlog counts section stays dead (T-0588a) — the board below IS
    // the backlog and the column kickers show the counts.
    expect(html).not.toContain("Backlog");
    expect(html).not.toContain("423");
  });

  test("renders the state-doc body when present", () => {
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
    expect(html).toContain(">20<");
    expect(html).toContain("cap 13 counts live dev sessions");
    expect(html).not.toContain("20 / 13");
    expect(html).not.toContain("Nobody has written the work-state doc yet");
  });

  // T-0942 — A DATE IS NOT A WARNING.
  //
  // The board used to render a five-week-old work-state doc exactly like a
  // fresh one: same card, same body, a relative timestamp in small grey type.
  // On 2026-09-06 an operator booted from a doc last written 2026-07-31 and
  // acted on it as current fact, and the reason generalises past the boot
  // prompt — nothing on any read surface SAID it was stale. So the payload now
  // carries a verdict and this surface has to spend it.
  test("a stale doc is called stale, and names its age and last writer", () => {
    const html = renderView({
      ...REAL_DERIVED,
      operator_state: {
        exists: true,
        content: "## Priorities\n\nA July release is in flight.",
        updated_at: 1_785_000_000,
        path: "artifacts/work-state.md",
        updated_by: "S-almdudleer-operator-p533",
        rev: 4,
        age_seconds: 37 * 24 * 3600,
        stale: true,
        stale_after_hours: 24,
      },
    });
    expect(html).toContain("This doc is stale");
    expect(html).toContain("read it as history");
    expect(html).toContain("S-almdudleer-operator-p533");
    expect(html).toContain("rev 4");
    // The body is still rendered — it is the only record there is. The warning
    // frames it; it does not replace it.
    expect(html).toContain("A July release is in flight.");
  });

  test("a CURRENT doc gets no warning — or the warning stops meaning anything", () => {
    const html = renderView({
      ...REAL_DERIVED,
      operator_state: {
        exists: true,
        // NOT a ticket id: the markdown renderer turns T-NNNN into an <a>, so a
        // literal "Ship T-0942." never appears in the output. Pinning a string
        // the producer does not emit is a test of my model, not of the surface.
        content: "## Priorities\n\nShip the work-state doc.",
        updated_at: 1_785_000_000,
        path: "artifacts/work-state.md",
        updated_by: "S-almdudleer-operator-p640",
        rev: 9,
        age_seconds: 600,
        stale: false,
        stale_after_hours: 24,
      },
    });
    expect(html).not.toContain("This doc is stale");
    expect(html).not.toContain("read it as history");
    expect(html).toContain("Ship the work-state doc.");
    expect(html).toContain("S-almdudleer-operator-p640");
    // The AGE must come from the same source as the verdict. This fixture has a
    // stale MTIME and a current verdict — the shape a copied data dir produces —
    // and the card must not render "42d ago" next to no warning.
    expect(html).toContain("10m ago");
    expect(html).not.toContain("42d ago");
  });

  // An older api does not send the verdict fields at all. Absent must read as
  // "no verdict", never as "stale" — a board that cried stale against every
  // pre-T-0942 server would be the same defect pointed the other way.
  test("a pre-T-0942 payload with no verdict fields renders no warning", () => {
    const html = renderView({
      ...REAL_DERIVED,
      operator_state: {
        exists: true,
        content: "## Priorities\n\nold server, no verdict.",
        updated_at: 1_700_000_000,
        path: "artifacts/operator-state.md",
      },
    });
    expect(html).not.toContain("This doc is stale");
    expect(html).toContain("old server, no verdict.");
  });
});

/**
 * T-0772 — the two cards in this strip come from ONE payload and obey TWO
 * policies. `quota.in_progress` is counted off the whole backlog; `sessions` is
 * owner-scoped per user. Rendered side by side under one unqualified label, a
 * non-admin read "IN PROGRESS 6" beside "LIVE SESSIONS 0" and could only
 * conclude the install was wedged.
 *
 * Fixtures below are the payload MEASURED on the live install (75dc01a,
 * 2026-07-29) for the two real accounts, negative controls (garbage cookie
 * 401 / no cookie 401) run first and identities confirmed from /api/auth/me:
 *
 *   aqice  (global_member, is_admin false) → sessions [] · in_progress 6
 *   alexey (is_admin true)                 → 3 live rows · in_progress 6
 */
describe("T-0772: the LIVE SESSIONS card says whose count it is", () => {
  // aqice's live shape: the owner gate filtered every row out.
  const SCOPED_EMPTY: TransparencyData = {
    ...REAL_DERIVED,
    sessions: [],
    sessions_scope: "own",
    quota: { ...REAL_DERIVED.quota, in_progress: 6 },
  };

  test("a non-admin's zero is labelled as HIS, not as the project's", () => {
    const html = renderView(SCOPED_EMPTY);

    // The label is the fix. `>LIVE SESSIONS<` (not `toContain("LIVE
    // SESSIONS")`) because "YOUR LIVE SESSIONS" contains it as a substring —
    // the assertion has to distinguish the two labels, not match both.
    expect(html).toContain(">YOUR LIVE SESSIONS<");
    expect(html).not.toContain(">LIVE SESSIONS<");

    // ...and the broad number is still right beside it. This pairing IS the
    // recorded defect; the test would pass vacuously if the strip lost a card.
    expect(html).toContain(">6<");
  });

  test("NO GATE MOVED: the value is still the owner-scoped zero", () => {
    // The ticket forbids widening the owner scope — globalBusyHelpers.isMyInFlight
    // is defined own-only, so a wider list would light the global busy indicator
    // for other people's work. Only the RENDERING was wrong.
    const html = renderView(SCOPED_EMPTY);
    expect(html).toContain('<div class="mc-an-card-value">0</div>');
  });

  test("an ADMIN's card is unchanged — the count is the project's", () => {
    const html = renderView({ ...REAL_DERIVED, sessions_scope: "all" });
    expect(html).toContain(">LIVE SESSIONS<");
    expect(html).not.toContain("YOUR LIVE SESSIONS");
    expect(html).toContain(">2<"); // same live count as before this ticket
  });

  test("UNKNOWN scope keeps today's neutral copy — never a false 'YOUR'", () => {
    // A pre-T-0772 server (or one reached through the mothership proxy) sends
    // no scope. Claiming the rows are the viewer's own would be a new false
    // statement, in the same shape as the one being fixed.
    const html = renderView({ ...REAL_DERIVED, sessions_scope: undefined });
    expect(html).toContain(">LIVE SESSIONS<");
    expect(html).not.toContain("YOUR LIVE SESSIONS");
  });

  test("liveSessionsCardCopy: only an explicit 'own' narrows the label", () => {
    expect(liveSessionsCardCopy("own").label).toBe("YOUR LIVE SESSIONS");
    expect(liveSessionsCardCopy("all").label).toBe("LIVE SESSIONS");
    expect(liveSessionsCardCopy(null).label).toBe("LIVE SESSIONS");
    expect(liveSessionsCardCopy(undefined).label).toBe("LIVE SESSIONS");
    // The sub-line points at Processes in every case — the card stays a link.
    for (const s of ["own", "all", null, undefined] as const) {
      expect(liveSessionsCardCopy(s).sub).toContain("Processes");
    }
  });
});


// ---------------------------------------------------------------------------
// T-0966 — the IN PROGRESS card stopped rendering two units as one ratio
// ---------------------------------------------------------------------------

describe("T-0966: the cap names the unit it counts", () => {
  test("says what the cap counts, which is not the number beside it", () => {
    // The measured install: cap 7 (set from «таргет параллелизма 7»), 1 ticket
    // carrying the in_progress label, 8 live dev sessions. The old copy read
    // "1 / 7" — six lanes of headroom, while the system was one lane over.
    expect(inProgressCardCopy(7)).toBe(
      "tickets labelled in_progress · cap 7 counts live dev sessions",
    );
  });

  test("an unset cap says so rather than printing ∞ as a denominator", () => {
    expect(inProgressCardCopy(0)).toBe("tickets labelled in_progress · no cap set");
  });

  test("never renders the board count and the cap as a ratio", () => {
    const html = renderView({
      ...REAL_DERIVED,
      quota: { ...REAL_DERIVED.quota, max_in_progress: 7, in_progress: 1 },
    });
    expect(html).not.toContain("1 / 7");
    expect(html).toContain("cap 7 counts live dev sessions");
  });
});
