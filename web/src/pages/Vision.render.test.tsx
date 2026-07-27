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

import { VisionFile } from "../api";
import { Vision, VisionFileBody } from "./Vision";

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

/**
 * T-0737 — the initiative/product body is RENDERED markdown, not a raw dump.
 *
 * The row bodies only exist after the async files fetch, so they're out of
 * reach of renderVision() above; VisionFileBody is exported precisely so the
 * decision "this content class gets rendered" is locked at the seam where it
 * was wrong. What it must NOT do is grow its own renderer — hence the
 * assertion on the shared block's class.
 */
describe("vision file body", () => {
  const body = "# Operator UX & Session Management Overhaul\n\n## Origin\n\nfiled 2026-07.\n\n## Tickets\n";

  function renderBody(collapsible?: boolean): string {
    return renderToStaticMarkup(
      <StaticRouter location="/p/bot-squad/vision">
        <VisionFileBody
          file={{ name: "initiatives/x.md", content: body } as VisionFile}
          slug="bot-squad"
          collapsible={collapsible}
        />
      </StaticRouter>,
    );
  }

  test("renders the markdown — no literal '# '/'## ' on the page", () => {
    const html = renderBody(true);

    expect(html).toContain("<h1>");
    expect(html).toContain("<h2>");
    expect(html).not.toContain("## Origin");
    expect(html).not.toContain("## Tickets");
  });

  test("goes through the shared block, not a second renderer", () => {
    const html = renderBody(true);

    expect(html).toContain("mc-md-body");
    expect(html).not.toContain('class="mc-pre"');
  });

  test("read-only — the body carries no write affordance (T-0709 holds)", () => {
    const html = renderBody(true);

    expect(html).not.toContain("<textarea");
    expect(html).not.toContain("Save");
    expect(html).not.toContain("Edit");
  });
});
