/**
 * T-0602 (T-0588d, N3) — render test for the bogus-slug not-found panel the
 * SlugAliasGuard swaps in for ALL /p/:slug/* children once /api/projects
 * positively excludes the slug (the Shell suppresses the project rail off the
 * same useProjectExists signal). Automated AFTER the manual walkthrough
 * (scenarios/T-0602-*.md) per the manual-first rule (T-0158).
 */
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, test } from "vitest";

import { ProjectNotFound } from "./App";

describe("ProjectNotFound", () => {
  test("renders the not-found notice + a home link, no project chrome", () => {
    const html = renderToStaticMarkup(
      <StaticRouter location="/p/bogus-slug/sessions">
        <ProjectNotFound slug="bogus-slug" />
      </StaticRouter>,
    );
    expect(html).toContain("bogus-slug");
    expect(html).toContain("not found");
    expect(html).toContain('href="/"');
    expect(html).toContain("back to all projects");
  });
});
