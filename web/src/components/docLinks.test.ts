/**
 * T-0756 — relative cross-doc links resolve to the docs route, and an
 * unresolvable one fails VISIBLY.
 *
 * The fixture is the real live docs tree shape (roadmap chapters keyed by their
 * own stem after T-0751, D-NNNN docs whose id is a PREFIX of their stem), so a
 * regression here is a regression against the install, not against a fiction.
 *
 * Written after the manual walkthrough (T-0158): the defect is invisible to
 * anything that does not follow the navigation, so it was clicked on the live
 * install first — `a[href="01-sessions-task-manager.md"]` opened a new tab on
 * /p/bot-squad, the project BOARD. These lock that shut.
 */
import { describe, expect, test } from "vitest";

import { DocSummary } from "../api";
import { buildDocIndex, classifyDocHref, docRoute } from "./docLinks";

const doc = (id: string, category: string, stem?: string): DocSummary => ({
  id,
  title: id,
  category,
  status: "",
  related_tickets: [],
  stem: stem ?? id,
});

const LIVE_SHAPE = buildDocIndex([
  doc("01-sessions-task-manager", "roadmap"),
  doc("07-multi-server-mothership", "roadmap"),
  doc("guidance-corpus", "roadmap"),
  doc("provenance", "roadmap"),
  doc("README", "roadmap"),
  // The id is NOT the stem here — this is exactly the case a TypeScript
  // re-implementation of `artifact_nesting.id_from_stem` would be needed for,
  // and exactly why the API ships the stem instead.
  doc("D-0057", "design", "D-0057-t-0637-ui-declutter-wave-3-screen-by-screen-keep-cut-collaps"),
  doc("phase2-remote-recon-report", "qa"),
]);

describe("classifyDocHref — the 12 broken links in roadmap/README", () => {
  test("a bare sibling filename resolves to the docs route", () => {
    expect(classifyDocHref("01-sessions-task-manager.md", LIVE_SHAPE, "roadmap")).toEqual({
      kind: "doc",
      id: "01-sessions-task-manager",
      fragment: "",
    });
  });

  test("it resolves with no base category too (ticket/vision bodies)", () => {
    expect(classifyDocHref("guidance-corpus.md", LIVE_SHAPE)).toEqual({
      kind: "doc",
      id: "guidance-corpus",
      fragment: "",
    });
  });

  test("the route carries the query param the raw href dropped", () => {
    // The defect in one line: an href resolved against the PATH loses `?doc=`
    // and lands on /p/<slug>, the board.
    expect(docRoute("bot-squad", "01-sessions-task-manager")).toBe(
      "/p/bot-squad/docs?doc=01-sessions-task-manager",
    );
  });

  test("a D-NNNN file resolves by STEM to its derived id — no second rule", () => {
    // The web side never computes "D-0057-…-collaps" -> "D-0057"; it looks up
    // the stem the BE derived that id FROM (artifact_nesting.id_from_stem,
    // T-0751). Re-deriving it here is what this test exists to prevent.
    expect(
      classifyDocHref(
        "../design/D-0057-t-0637-ui-declutter-wave-3-screen-by-screen-keep-cut-collaps.md",
        LIVE_SHAPE,
        "roadmap",
      ),
    ).toEqual({ kind: "doc", id: "D-0057", fragment: "" });
  });
});

describe("classifyDocHref — cross-category and fragments", () => {
  test("a link into another category resolves", () => {
    expect(classifyDocHref("../qa/phase2-remote-recon-report.md", LIVE_SHAPE, "roadmap")).toEqual({
      kind: "doc",
      id: "phase2-remote-recon-report",
      fragment: "",
    });
  });

  test("an explicit same-directory link resolves", () => {
    expect(classifyDocHref("./provenance.md", LIVE_SHAPE, "roadmap")).toEqual({
      kind: "doc",
      id: "provenance",
      fragment: "",
    });
  });

  test("a category-qualified path resolves without a base", () => {
    expect(classifyDocHref("roadmap/provenance.md", LIVE_SHAPE)).toEqual({
      kind: "doc",
      id: "provenance",
      fragment: "",
    });
  });

  test("a fragment is carried through to the docs route, not dropped", () => {
    const got = classifyDocHref("provenance.md#the-contract", LIVE_SHAPE, "roadmap");
    expect(got).toEqual({ kind: "doc", id: "provenance", fragment: "#the-contract" });
    expect(docRoute("bot-squad", "provenance", "#the-contract")).toBe(
      "/p/bot-squad/docs?doc=provenance#the-contract",
    );
  });

  test("a bare #fragment stays an in-page anchor", () => {
    expect(classifyDocHref("#section-4", LIVE_SHAPE, "roadmap")).toEqual({ kind: "fragment" });
  });

  test("a percent-encoded filename resolves", () => {
    expect(classifyDocHref("01-sessions-task-manager.md", LIVE_SHAPE, "roadmap")).toEqual(
      classifyDocHref("01-sessions%2Dtask-manager.md".replace("%2D", "-"), LIVE_SHAPE, "roadmap"),
    );
    expect(classifyDocHref("guidance%2Dcorpus.md", LIVE_SHAPE)).toEqual({
      kind: "doc",
      id: "guidance-corpus",
      fragment: "",
    });
  });
});

describe("classifyDocHref — must FAIL VISIBLY, never navigate somewhere plausible", () => {
  test("a .md target with no doc behind it is missing, not external", () => {
    // roadmap/README links to `gap-matrix.md`, which is a DIRECTORY on disk.
    // Before this fix it silently landed the reader on the board.
    expect(classifyDocHref("gap-matrix.md", LIVE_SHAPE, "roadmap")).toEqual({
      kind: "missing",
      target: "gap-matrix.md",
    });
  });

  test("a relative link to a directory is missing", () => {
    expect(classifyDocHref("_corpus-parts/", LIVE_SHAPE, "roadmap")).toEqual({
      kind: "missing",
      target: "_corpus-parts/",
    });
  });

  test("a file the API does not serve (nested deeper than one category) is missing", () => {
    // `artifact_nesting.iter_docs` walks exactly one level of category dirs, so
    // roadmap/gap-matrix/README.md is not a doc — 56 of the 130 md files under
    // the live docs/ tree sit at that depth.
    expect(classifyDocHref("gap-matrix/README.md", LIVE_SHAPE, "roadmap")).toEqual({
      kind: "missing",
      target: "gap-matrix/README.md",
    });
  });

  test("a target that climbs out of the docs tree is missing, never resolved", () => {
    expect(classifyDocHref("../../backlog/T-0757.md", LIVE_SHAPE, "roadmap")).toEqual({
      kind: "missing",
      target: "../../backlog/T-0757.md",
    });
  });

  test("an ambiguous bare stem does not silently pick one", () => {
    const twoReadmes = buildDocIndex([doc("README", "roadmap"), doc("README", "qa")]);
    expect(classifyDocHref("README.md", twoReadmes)).toEqual({
      kind: "missing",
      target: "README.md",
    });
    // …but with a base category it is not ambiguous at all.
    expect(classifyDocHref("README.md", twoReadmes, "qa")).toEqual({
      kind: "doc",
      id: "README",
      fragment: "",
    });
  });
});

describe("classifyDocHref — what it must NOT touch", () => {
  test.each([
    "https://example.com/x.md",
    "http://example.com",
    "//example.com/x.md",
    "mailto:someone@example.com",
    // The ONE link in roadmap/README that already worked: an absolute app path.
    // T-0756 is about relative links; this must keep behaving exactly as before.
    "/p/bot-squad/t/T-0244",
    "/p/bot-squad/docs?doc=D-0057",
  ])("%s stays external", (href) => {
    expect(classifyDocHref(href, LIVE_SHAPE, "roadmap")).toEqual({ kind: "external" });
  });
});

describe("buildDocIndex", () => {
  test("falls back to the id when the BE has no stem (pre-T-0756 backend)", () => {
    const legacy = buildDocIndex([
      { id: "provenance", title: "p", category: "roadmap", status: "", related_tickets: [] },
    ]);
    expect(classifyDocHref("provenance.md", legacy, "roadmap")).toEqual({
      kind: "doc",
      id: "provenance",
      fragment: "",
    });
  });
});
