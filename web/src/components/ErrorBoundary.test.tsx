/**
 * T-0602 (T-0588d, F3) — render test for the app-level error-boundary
 * fallback. SSR renderers don't support error boundaries (a throw during
 * renderToStaticMarkup propagates), so the lock is on the exported fallback
 * card plus the boundary's derived-state contract. Automated AFTER the manual
 * walkthrough (scenarios/T-0602-*.md) per the manual-first rule (T-0158).
 */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";

import { ErrorBoundary, ErrorFallback } from "./ErrorBoundary";

describe("ErrorFallback", () => {
  test("renders the something-broke card with the error and a reload button", () => {
    const html = renderToStaticMarkup(
      <ErrorFallback error={new Error("T-0602 boundary probe")} />,
    );
    expect(html).toContain("Something broke in the UI.");
    expect(html).toContain("T-0602 boundary probe");
    expect(html).toContain("Reload");
  });
});

describe("ErrorBoundary", () => {
  test("derives the error state from a thrown error", () => {
    const err = new Error("kaboom");
    expect(ErrorBoundary.getDerivedStateFromError(err)).toEqual({ error: err });
  });

  test("renders children while no error is set", () => {
    const html = renderToStaticMarkup(
      <ErrorBoundary>
        <div>healthy subtree</div>
      </ErrorBoundary>,
    );
    expect(html).toContain("healthy subtree");
    expect(html).not.toContain("Something broke");
  });
});
