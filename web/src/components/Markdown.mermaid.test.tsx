/**
 * T-0290 (c) — a ```mermaid fence renders as a DIAGRAM, everywhere.
 *
 * The invariant is WHERE this lives, not just that it works. Docs, Vision and
 * legacy ticket bodies all reach the screen through `Markdown` (Docs -> Markdown;
 * Vision/TaskDetail -> MarkdownBody -> Markdown), so the fence handling sits in
 * that one renderer. A second copy on a page — the shape T-0714/T-0719/T-0729/
 * T-0731/T-0743 each shipped broken — is what these tests exist to keep out:
 * the MarkdownBody case below asserts a surface that never mentions mermaid
 * still gets diagrams, purely by delegating.
 *
 * `renderToStaticMarkup` (no jsdom here — see MarkdownBody.test.tsx) means
 * effects never run, so `Mermaid` is caught at its pre-effect state: the
 * loading placeholder. That is exactly the right assertion target for
 * "diverted to the diagram component" — the mermaid bundle itself is a lazy
 * dynamic import fired from that effect, and the manual playwright walkthrough
 * covers the rendered SVG plus the "fence-free doc requests no mermaid chunk"
 * half, which no static render can observe.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { Markdown } from "./Markdown";
import { MarkdownBody } from "./MarkdownBody";

function render(source: string): string {
  return renderToStaticMarkup(
    <StaticRouter location="/p/bot-squad/docs">
      <Markdown source={source} slug="bot-squad" />
    </StaticRouter>,
  );
}

const DIAGRAM = ["```mermaid", "flowchart LR", "  A[start] --> B[end]", "```"].join("\n");

describe("a mermaid fence becomes a diagram, not source", () => {
  test("the fence is handed to <Mermaid>, and its source is not dumped as code", () => {
    const html = render(`# Doc\n\n${DIAGRAM}\n`);

    expect(html).toContain("mc-loading"); // <Mermaid>'s pre-effect state
    expect(html).not.toContain("language-mermaid");
    expect(html).not.toContain("flowchart LR"); // no raw fence body on screen
  });

  test("every surface gets it by delegating — MarkdownBody names no mermaid", () => {
    const html = renderToStaticMarkup(
      <StaticRouter location="/p/bot-squad/t/T-0546">
        <MarkdownBody source={`## Body\n\n${DIAGRAM}\n`} slug="bot-squad" />
      </StaticRouter>,
    );

    expect(html).toContain("mc-loading");
    expect(html).not.toContain("language-mermaid");
  });

  test("a doc with several diagrams renders each one", () => {
    const html = render(`${DIAGRAM}\n\ntext between\n\n${DIAGRAM}\n`);

    expect(html.match(/mc-loading/g)).toHaveLength(2);
  });
});

describe("only mermaid fences are diverted", () => {
  test("another language still renders as a code block", () => {
    const html = render('```python\ndef hello():\n    return "code"\n```\n');

    expect(html).toContain("language-python");
    expect(html).toContain("def hello");
    expect(html).not.toContain("mc-loading");
  });

  test("an unlabelled fence still renders as a code block", () => {
    const html = render("```\nplain preformatted text\n```\n");

    expect(html).toContain("<pre>");
    expect(html).toContain("plain preformatted text");
    expect(html).not.toContain("mc-loading");
  });

  test("an EMPTY mermaid fence falls through instead of parking a spinner", () => {
    // There is no diagram to draw, and <Mermaid> bails out of its effect on
    // blank input — diverting would leave "Rendering diagram…" on screen forever.
    const html = render("```mermaid\n```\n");

    expect(html).not.toContain("mc-loading");
    expect(html).toContain("language-mermaid");
  });

  test("inline `code` spans are untouched", () => {
    const html = render("a `mermaid` word inline\n");

    expect(html).toContain("<code>mermaid</code>");
    expect(html).not.toContain("mc-loading");
  });
});

describe("the rest of the renderer still works around a diagram", () => {
  test("markdown structure and T-/D- mention links survive alongside a fence", () => {
    const html = render(`# Title\n\nSee T-0290 and D-0045.\n\n${DIAGRAM}\n`);

    expect(html).toContain("<h1>");
    expect(html).toContain('href="/p/bot-squad/t/T-0290"');
    expect(html).toContain('href="/p/bot-squad/docs?doc=D-0045"');
    expect(html).toContain("mc-loading");
  });
});
