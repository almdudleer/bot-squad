/**
 * T-0709 (D-0057 §8 override) — render lock for the Vision page: the
 * create-initiative control is gone (pure read/observability surface, matches
 * the T-0670/DocsSection precedent). Per-row controls (Edit, Mark finished /
 * Retire, Reopen, bind-lead) are gated behind the async files/sessions fetch
 * and don't render pre-effect, so those are covered by removal of their
 * handlers + JSX in Vision.tsx (tsc catches orphaned refs) rather than here.
 *
 * renderToStaticMarkup needs no DOM/effects (no jsdom/testing-library in this
 * project, per ObservabilityPanel.test.tsx) — the header chrome renders
 * synchronously before the data-fetching effect ever fires, which is exactly
 * what this test locks down.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { Route, Routes } from "react-router-dom";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { Vision } from "./Vision";

function renderVision(): string {
  return renderToStaticMarkup(
    <StaticRouter location="/p/bot-squad/vision">
      <Routes>
        <Route path="/p/:slug/vision" element={<Vision />} />
      </Routes>
    </StaticRouter>,
  );
}

describe("Vision", () => {
  test("no + New initiative control — pure read/observability surface", () => {
    const html = renderVision();

    expect(html).not.toContain("New initiative");
  });

  test("page still mounts its header chrome", () => {
    const html = renderVision();

    expect(html).toContain("Vision");
    expect(html).toContain("/ bot-squad");
  });
});
