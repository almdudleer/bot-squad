/**
 * T-0737 — the shared stored-md-body renderer.
 *
 * TaskDetail.bodyBlock.test.tsx already locks this treatment as it reaches a
 * legacy TICKET body. What THIS file locks is the piece that was the bug: the
 * treatment is a component with its own contract, so a second surface gets it
 * by importing rather than by re-deriving it (Vision spent a night rendering
 * `## Origin` literally because the decision lived in one page's JSX).
 *
 * `renderToStaticMarkup` (no jsdom/testing-library here — see
 * ObservabilityPanel.test.tsx) covers initial render, where every invariant
 * below lives. The expander CLICK is covered by the manual playwright
 * walkthrough on the live 20.2k-char T-0551 body.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { BODY_COLLAPSE_CHARS, MarkdownBody } from "./MarkdownBody";

function render(props: Parameters<typeof MarkdownBody>[0]): string {
  return renderToStaticMarkup(
    <StaticRouter location="/p/bot-squad/vision">
      <MarkdownBody {...props} />
    </StaticRouter>,
  );
}

const LONG = `# Operator UX & Session Management Overhaul\n\n## Origin\n\n${"initiative prose. ".repeat(200)}\n\n## Tickets\n`;

describe("MarkdownBody renders, never dumps", () => {
  test("markdown becomes structure — no literal '## ' survives", () => {
    const html = render({ source: LONG, slug: "bot-squad" });

    expect(html).toContain("<h1>");
    expect(html).toContain("<h2>");
    expect(html).toContain("Operator UX &amp; Session Management Overhaul");
    expect(html).not.toContain("# Operator UX");
    expect(html).not.toContain("## Origin");
  });

  test("it is the boxed shared block, not a raw <pre> dump", () => {
    const html = render({ source: LONG, slug: "bot-squad" });

    expect(html).toContain("mc-md-body");
    expect(html).not.toContain('class="mc-pre"');
  });

  test("an empty body renders nothing rather than crashing", () => {
    expect(() => render({ source: null, slug: "bot-squad" })).not.toThrow();
    expect(() => render({ source: undefined })).not.toThrow();
  });
});

describe("collapse is the caller's call, and only above the threshold", () => {
  test("collapsible + long → clipped, faded, expander names the real size", () => {
    const html = render({ source: LONG, slug: "bot-squad", collapsible: true });

    expect(LONG.trim().length).toBeGreaterThan(BODY_COLLAPSE_CHARS);
    expect(html).toContain("max-height:18rem");
    expect(html).toContain("mc-md-body-fade");
    expect(html).toContain(`Show full body (${LONG.trim().length.toLocaleString()} chars)`);
  });

  test("collapsible + short → no control it doesn't need", () => {
    const short = "# run-survival-hardening\n\n(filed via initiative_new)";

    expect(short.length).toBeLessThan(BODY_COLLAPSE_CHARS);
    const html = render({ source: short, slug: "bot-squad", collapsible: true });
    expect(html).not.toContain("Show full body");
    expect(html).not.toContain("max-height");
  });

  test("NOT collapsible → shown whole however long (Vision's product lede)", () => {
    const html = render({ source: LONG, slug: "bot-squad" });

    expect(html).not.toContain("Show full body");
    expect(html).not.toContain("max-height");
    expect(html).not.toContain("mc-md-body-fade");
  });
});
