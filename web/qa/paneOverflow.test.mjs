/**
 * T-0757 — the content-pane check judges the real measurements.
 *
 * The numbers in these cases are not invented: they are what the live install
 * actually reported at 390px before and after the fix (throwaway API over the
 * real data dir). The point of the suite is that the check FAILS on the
 * before-state — a check that could not have caught T-0757 is worth nothing,
 * and the ticket says the check outlives the fix.
 */
import { describe, expect, test } from "vitest";

import { DEFAULT_BUDGET, findViolations } from "./paneOverflow.mjs";

/** Measured on the live install, docs?doc=README at 390px, BEFORE the fix. */
const T0757_BEFORE = {
  paneSelector: ".mc-docs-detail",
  found: true,
  viewportWidth: 390,
  pageOverflow: 0,
  bodyScrollWidth: 390,
  paneClientWidth: 102,
  paneScrollWidth: 352,
  paneContentRight: 378,
  escapes: [{ tag: "TABLE", className: "", right: 628, text: "surface | state" }],
  escapeCount: 1,
};

/** Same page, same width, AFTER the fix. */
const T0757_AFTER = {
  paneSelector: ".mc-docs-detail",
  found: true,
  viewportWidth: 390,
  pageOverflow: 0,
  bodyScrollWidth: 390,
  paneClientWidth: 366,
  paneScrollWidth: 366,
  paneContentRight: 378,
  escapes: [],
  escapeCount: 0,
};

describe("findViolations", () => {
  test("catches T-0757 — the bug every page-level check passed on", () => {
    const kinds = findViolations(T0757_BEFORE).map((v) => v.kind);
    expect(kinds).toContain("pane-starved");
    expect(kinds).toContain("pane-overflow");
    expect(kinds).toContain("child-escapes-pane");
  });

  test("the page-level signals it would have relied on are CLEAN in that state", () => {
    // This is the whole reason the check had to move down a level: body
    // scrollWidth was exactly the viewport and page overflow was 0 while the
    // content pane held 102 of 366 available px.
    expect(T0757_BEFORE.pageOverflow).toBe(0);
    expect(T0757_BEFORE.bodyScrollWidth).toBe(T0757_BEFORE.viewportWidth);
    expect(findViolations(T0757_BEFORE)).not.toHaveLength(0);
  });

  test("passes the fixed state", () => {
    expect(findViolations(T0757_AFTER)).toEqual([]);
  });

  test("a roomy pane that lets a child overhang still fails", () => {
    // Stacking alone is not the fix: a full-width pane with an uncontained
    // 900px table is still broken.
    const v = findViolations({
      ...T0757_AFTER,
      paneScrollWidth: 900,
      escapes: [{ tag: "TABLE", className: "", right: 900, text: "x" }],
      escapeCount: 1,
    });
    expect(v.map((x) => x.kind)).toEqual(["pane-overflow", "child-escapes-pane"]);
  });

  test("a starved pane that contains its children still fails (the board's shape)", () => {
    // The project board has overflow-x on its row, so nothing escapes — but its
    // columns get 36-54px for content needing 96-198px. Containment without
    // width is not a fix, so the starvation check must fire on its own.
    expect(findViolations({ ...T0757_AFTER, paneClientWidth: 54, paneScrollWidth: 54 })).toEqual([
      expect.objectContaining({ kind: "pane-starved" }),
    ]);
  });

  test("sub-pixel rounding is not a violation", () => {
    expect(findViolations({ ...T0757_AFTER, paneScrollWidth: 367 })).toEqual([]);
  });

  test("a missing pane is reported, not silently passed", () => {
    // A renamed class must break the check loudly — a selector that matches
    // nothing measuring nothing would report PASS forever.
    expect(findViolations({ paneSelector: ".gone", found: false })).toEqual([
      { kind: "pane-missing", detail: "no element matches .gone" },
    ]);
  });

  test("the budget floor is the phone-readable one", () => {
    expect(DEFAULT_BUDGET.minPaneWidthPx).toBe(320);
    // 102px must be nowhere near passing; 366px (390px viewport, container
    // padding removed) must pass.
    const atWidth = (w) => findViolations({ ...T0757_AFTER, paneClientWidth: w, paneScrollWidth: w });
    expect(atWidth(319)).toEqual([expect.objectContaining({ kind: "pane-starved" })]);
    expect(atWidth(320)).toEqual([]);
  });
});
