/**
 * T-0670 (D-0057 §4/§8, T-0637 Lane C) — render lock for the Docs & Artifacts
 * rail: the T-0572 "✎ Manage" write-toggle and "+ New" create UI are gone
 * (pure read/lookup surface, R5), and the use_case filter/kind is gone
 * (T-0671, Lane D — folded into this commit).
 *
 * renderToStaticMarkup needs no DOM/effects (no jsdom/testing-library in this
 * project, per ObservabilityPanel.test.tsx) — the rail chrome (type filter,
 * closed-feedback toggle) renders synchronously before the data-fetching
 * effect ever fires, which is exactly what this test locks down.
 *
 * This is the automated lock placed AFTER the manual walkthrough
 * (scenarios/T-0670-*.md) per the manual-first rule (T-0158).
 */
import { renderToStaticMarkup } from "react-dom/server";
import { Route, Routes } from "react-router-dom";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { DocsSection } from "./DocsSection";

function renderDocsSection(): string {
  return renderToStaticMarkup(
    <StaticRouter location="/p/bot-squad/docs">
      <Routes>
        <Route path="/p/:slug/docs" element={<DocsSection />} />
      </Routes>
    </StaticRouter>,
  );
}

describe("DocsSection", () => {
  test("no Manage toggle, no +New create UI — pure read/lookup rail", () => {
    const html = renderDocsSection();

    expect(html).not.toContain("Manage");
    expect(html).not.toContain("+ New");
  });

  test("type filter offers only All/Docs/Feedback — use_case is gone (T-0671)", () => {
    const html = renderDocsSection();

    expect(html).toContain("Docs &amp; Artifacts");
    expect(html).toContain("All");
    expect(html).toContain("📄 Docs");
    expect(html).toContain("💬 Feedback");
    expect(html).not.toContain("🎯");
    expect(html).not.toContain("Use case");
  });
});
