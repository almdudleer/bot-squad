/**
 * T-0672 (Lane E, D-0057 R6) — render lock: the permanent "what is this
 * page?" toggle (PageHelp) is onboarding clutter for a daily operator and
 * is swept off the Deployment Queue. renderToStaticMarkup never runs
 * effects, so the page's own api.runs() fetch never fires — no mocking
 * needed, this exercises the same initial synchronous render the browser
 * paints first.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { Runs } from "./Runs";

describe("Runs page — onboarding chrome sweep (T-0672)", () => {
  test("renders with no PageHelp toggle/panel", () => {
    const html = renderToStaticMarkup(
      <StaticRouter location="/p/bot-squad/runs">
        <Runs />
      </StaticRouter>,
    );

    expect(html).not.toContain("What is this page?");
    expect(html).not.toContain("mc-page-help");

    // The page itself still renders (title survives the sweep).
    expect(html).toContain("Deployment queue");
  });
});
