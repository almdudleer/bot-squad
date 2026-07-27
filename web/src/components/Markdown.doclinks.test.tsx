/**
 * T-0756 — render lock for cross-doc links in a real markdown body.
 *
 * `docLinks.test.ts` pins the resolution RULE; this pins what the reader
 * actually gets on screen, which is where the defect lived: the old renderer
 * emitted `<a href="01-sessions-task-manager.md" target="_blank">`, the browser
 * resolved that against the PATH, the `?doc=` param vanished and the reader
 * landed on /p/bot-squad — the project board, silently.
 *
 * renderToStaticMarkup, no jsdom (this project has none — see
 * DocsSection.test.tsx). Effects never fire under SSR, so the index arrives
 * through `DocIndexContext`, which is exactly the seam it was added for.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { DocSummary } from "../api";
import { Markdown } from "./Markdown";
import { buildDocIndex } from "./docLinks";
import { DocIndexContext } from "./useDocIndex";

const DOCS: DocSummary[] = [
  {
    id: "01-sessions-task-manager",
    title: "Sessions / Task-Manager",
    category: "roadmap",
    status: "",
    related_tickets: [],
    stem: "01-sessions-task-manager",
  },
  {
    id: "D-0057",
    title: "UI declutter wave 3",
    category: "design",
    status: "",
    related_tickets: [],
    stem: "D-0057-t-0637-ui-declutter-wave-3",
  },
];

function render(body: string, opts: { index?: boolean; baseCategory?: string } = {}): string {
  const index = opts.index === false ? null : buildDocIndex(DOCS);
  return renderToStaticMarkup(
    <StaticRouter location="/p/bot-squad/docs">
      <DocIndexContext.Provider value={index}>
        <Markdown source={body} slug="bot-squad" baseCategory={opts.baseCategory} />
      </DocIndexContext.Provider>
    </StaticRouter>,
  );
}

describe("Markdown cross-doc links", () => {
  test("a relative .md link becomes the docs route — never a path-relative href", () => {
    const html = render("See [chapter 1](01-sessions-task-manager.md).", {
      baseCategory: "roadmap",
    });
    expect(html).toContain('href="/p/bot-squad/docs?doc=01-sessions-task-manager"');
    // The exact shape that produced the silent board landing.
    expect(html).not.toContain('href="01-sessions-task-manager.md"');
  });

  test("it is an in-app link, not a new tab", () => {
    // A cross-doc link is navigation within the pane you are reading in.
    const html = render("[c1](01-sessions-task-manager.md)", { baseCategory: "roadmap" });
    expect(html).not.toContain('target="_blank"');
  });

  test("a link across categories resolves through the stem the BE derived", () => {
    const html = render("[wave 3](../design/D-0057-t-0637-ui-declutter-wave-3.md)", {
      baseCategory: "roadmap",
    });
    expect(html).toContain('href="/p/bot-squad/docs?doc=D-0057"');
  });

  test("a fragment survives the rewrite", () => {
    const html = render("[why](01-sessions-task-manager.md#the-model)", {
      baseCategory: "roadmap",
    });
    expect(html).toContain('href="/p/bot-squad/docs?doc=01-sessions-task-manager#the-model"');
  });

  test("a dead relative link is VISIBLY dead and does not navigate", () => {
    const html = render("[gap matrix](gap-matrix.md)", { baseCategory: "roadmap" });
    expect(html).toContain("mc-doclink-broken");
    expect(html).toContain("⚠");
    // No href at all: the harm being fixed is a link that goes somewhere
    // plausible, so a dead one must go NOWHERE.
    expect(html).not.toContain("href=");
  });

  test("before the index loads a relative link is inert, not accused", () => {
    const html = render("[chapter 1](01-sessions-task-manager.md)", { index: false });
    expect(html).toContain("mc-doclink-pending");
    expect(html).not.toContain("mc-doclink-broken");
    expect(html).not.toContain("href=");
  });

  test("absolute app paths and external links are untouched", () => {
    const html = render(
      "[T-0244](/p/bot-squad/t/T-0244) and [ext](https://example.com/x.md)",
      { baseCategory: "roadmap" },
    );
    expect(html).toContain('href="/p/bot-squad/t/T-0244"');
    expect(html).toContain('href="https://example.com/x.md"');
    expect(html).not.toContain("mc-doclink-broken");
  });

  test("T-/D- mention linkification still works alongside", () => {
    const html = render("see T-0244 and D-0057", { baseCategory: "roadmap" });
    expect(html).toContain('href="/p/bot-squad/t/T-0244"');
    expect(html).toContain('href="/p/bot-squad/docs?doc=D-0057"');
  });
});
