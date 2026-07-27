/**
 * T-0733 — render lock for the user-facing body block.
 *
 * The bug this pins: 17 tickets (12 live, several initiative-level) have no
 * `## Verbatim request` heading, so the parser's legacy fallback hands their
 * WHOLE body to the "what you asked for" slot. T-0553 is 11,415 chars of
 * planning document rendered as raw markdown inside the green ask block —
 * presented as the stakeholder's words when it is a working document. The fix
 * is the LABEL first; markdown rendering and the collapse ride on top.
 *
 * So the invariants worth locking are the two branches NOT looking alike:
 *   - legacy  → captioned "Ticket body — no separate request recorded",
 *               markdown rendered, long bodies collapsed behind an expander.
 *   - recorded ask → byte-faithful <pre> behind the green rule, no caption,
 *               no expander, markdown syntax left as literal text.
 *
 * `renderToStaticMarkup` (no jsdom/testing-library in this project — see
 * ObservabilityPanel.test.tsx) gives the initial render only, which is where
 * every one of those invariants lives. The expand/collapse CLICK is covered by
 * the manual playwright walkthrough on the T-0553/T-0546 live bodies, not here.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { Task } from "../api";
import { BODY_COLLAPSE_CHARS } from "../components/MarkdownBody";
import { TaskBodyBlock } from "./TaskDetail";

const CAPTION = "Ticket body — no separate request recorded";

function render(task: Partial<Task>): string {
  return renderToStaticMarkup(
    <StaticRouter location="/p/bot-squad/t/T-0001">
      <TaskBodyBlock
        task={{ id: "T-0001", title: "t", status: "open", body: "", ...task } as Task}
        slug="bot-squad"
      />
    </StaticRouter>,
  );
}

describe("legacy body (no `## Verbatim request` heading)", () => {
  const long = `## 0. Headline framing\n\n${"planning prose. ".repeat(200)}`;

  test("is captioned as the ticket body, never as the stakeholder's ask", () => {
    const html = render({ verbatim: long, verbatim_is_legacy: true });
    expect(html).toContain(CAPTION);
  });

  test("renders the markdown instead of dumping raw `## ` headings", () => {
    const html = render({ verbatim: long, verbatim_is_legacy: true });
    expect(html).toContain("<h2>");
    expect(html).toContain("0. Headline framing");
    expect(html).not.toContain("## 0. Headline framing");
  });

  test("a wall of text is collapsed, and the expander names its real size", () => {
    const html = render({ verbatim: long, verbatim_is_legacy: true });
    expect(html).toContain("max-height:18rem");
    expect(html).toContain("mc-md-body-fade");
    expect(html).toContain(`Show full body (${long.trim().length.toLocaleString()} chars)`);
  });

  test("a short legacy body is labelled but gets no expander it doesn't need", () => {
    const short = "run-survival-hardening\n\n(filed via initiative_new)";
    expect(short.length).toBeLessThan(BODY_COLLAPSE_CHARS);
    const html = render({ verbatim: short, verbatim_is_legacy: true });
    expect(html).toContain(CAPTION);
    expect(html).not.toContain("Show full body");
    expect(html).not.toContain("max-height");
  });

  test("does NOT wear the green rule that means 'your ask'", () => {
    const html = render({ verbatim: long, verbatim_is_legacy: true });
    expect(html).not.toContain("--mc-accent-success");
  });
});

describe("a recorded ask (the `## Verbatim request` branch) is untouched", () => {
  // Long enough to trip the collapse threshold if the branches ever got crossed.
  const ask = `I want **X**, and I mean it. ${"and also this. ".repeat(120)}`;

  test("stays a byte-faithful <pre> behind the green rule", () => {
    const html = render({ verbatim: ask, verbatim_is_legacy: false });
    expect(html).toContain("<pre");
    expect(html).toContain("--mc-accent-success");
    expect(html).toContain("I want **X**, and I mean it.");
  });

  test("is never markdown-rendered — the stakeholder's syntax is his text", () => {
    const html = render({ verbatim: ask, verbatim_is_legacy: false });
    expect(html).not.toContain("<strong>");
    expect(html).not.toContain("mc-md-body");
    expect(html).not.toContain("mc-legacy-body");
  });

  test("is never captioned or collapsed, however long it runs", () => {
    expect(ask.length).toBeGreaterThan(BODY_COLLAPSE_CHARS);
    const html = render({ verbatim: ask, verbatim_is_legacy: false });
    expect(html).not.toContain(CAPTION);
    expect(html).not.toContain("Show full body");
  });
});

describe("no body at all", () => {
  test.each([
    ["flagged legacy", true],
    ["not flagged", false],
  ])("%s → the empty-state <pre>, not a stray caption", (_label, legacy) => {
    const html = render({ verbatim: "   ", verbatim_is_legacy: legacy });
    expect(html).toContain("(no request recorded)");
    expect(html).not.toContain(CAPTION);
    expect(html).not.toContain("Show full body");
  });

  test("an absent verbatim field doesn't crash the block", () => {
    expect(render({ verbatim_is_legacy: true })).toContain("(no request recorded)");
  });
});
