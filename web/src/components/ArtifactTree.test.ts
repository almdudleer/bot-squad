import { describe, it, expect } from "vitest";
import { buildArtifactIndex, kindRoute, seedCollapsed, sortSections, sectionRank, feedbackTitle, ArtifactNode } from "./ArtifactTree";

// T-0283 (Pillar C / D-0029): the cross-store nesting tree is the core of the
// ticket. These lock the uniform tree logic across the three stores.

function doc(id: string, parent: string | null = null, category = "design"): ArtifactNode {
  return { id, title: id, kind: "doc", parent_doc_id: parent, routeId: id, section: category };
}
function fb(id: string, name: string, parent: string | null = null): ArtifactNode {
  return { id, title: id, kind: "feedback", parent_doc_id: parent, routeId: name, section: "feedback" };
}

// T-0671 (Lane D): the use_case store is deleted — cross-store coverage below
// now exercises doc <-> feedback nesting only (was doc/use_case/feedback).
describe("buildArtifactIndex — cross-store nesting", () => {
  it("nests evidence docs under a feedback theme (cross-store)", () => {
    const idx = buildArtifactIndex([fb("F-1", "F-1.md"), doc("D-2", "F-1")]);
    expect(idx.childrenOf.get("F-1")?.map((n) => n.id)).toEqual(["D-2"]);
    expect(idx.rootsBySection.get("feedback")!.map((n) => n.id)).toEqual(["F-1"]);
  });

  it("treats a parent_doc_id that names a missing artifact as a root", () => {
    const idx = buildArtifactIndex([doc("D-3", "GHOST")]);
    expect(idx.rootsBySection.get("design")!.map((n) => n.id)).toEqual(["D-3"]);
  });

  it("groups doc roots by category, feedback by store", () => {
    const idx = buildArtifactIndex([doc("D-a", null, "design"), doc("D-b", null, "runbook"), fb("F-2", "F-2.md")]);
    expect([...idx.rootsBySection.keys()].sort()).toEqual(["design", "feedback", "runbook"]);
  });

  it("descendantsOf walks cross-store and parentOptions excludes the own subtree (cycle-safe picker)", () => {
    // D-1 -> F-1 -> D-2 (a chain that crosses stores both directions)
    const idx = buildArtifactIndex([doc("D-1"), fb("F-1", "F-1.md", "D-1"), doc("D-2", "F-1")]);
    expect([...idx.descendantsOf("D-1")].sort()).toEqual(["D-1", "D-2", "F-1"]);
    // a re-parent picker for D-1 must NOT offer F-1 or D-2 (would cycle).
    expect(idx.parentOptions("D-1").map((n) => n.id)).toEqual([]);
    // but F-1's options exclude F-1 + D-2 (its descendant), leaving D-1.
    expect(idx.parentOptions("F-1").map((n) => n.id)).toEqual(["D-1"]);
  });
});

describe("sortSections — operator-facing first, agent-internal then stores last (T-0352/T-0364)", () => {
  it("ranks operator-facing categories first, agent-internal specs after, stores last", () => {
    // runbook/support/operator are operator-facing (rank 0); architecture/design
    // /roadmap are agent-internal (rank 1); use cases (2); feedback (3).
    expect(sortSections(["feedback", "runbook", "use cases", "design", "architecture", "support", "roadmap"]))
      .toEqual(["runbook", "support", "architecture", "design", "roadmap", "use cases", "feedback"]);
  });
});

describe("sectionRank — deprioritize agent-internal specs (T-0364)", () => {
  it("operator-facing < agent-internal < use cases < feedback", () => {
    expect(sectionRank("support")).toBe(0);
    expect(sectionRank("runbook")).toBe(0);
    expect(sectionRank("design")).toBe(1);
    expect(sectionRank("roadmap")).toBe(1);
    expect(sectionRank("architecture")).toBe(1);
    expect(sectionRank("use cases")).toBe(2);
    expect(sectionRank("feedback")).toBe(3);
  });
});

describe("feedbackTitle — no double-render of the slug (T-0364)", () => {
  it("returns the first markdown heading as the title", () => {
    expect(feedbackTitle("F-0001-x.md", "# Shared-tree commit races\nbody")).toBe("Shared-tree commit races");
  });
  it("returns '' (not the de-slugged filename) when there is no heading — id renders alone", () => {
    expect(feedbackTitle("F-2026-06-02-inbox-62e1f0b77f.md", "just prose, no heading")).toBe("");
  });
});

describe("seedCollapsed — default section collapse policy (T-0352)", () => {
  it("collapses the feedback section by default in the mixed All view (the F-* wall fix)", () => {
    const sections = sortSections(["design", "architecture", "feedback"]);
    expect(seedCollapsed(sections, ["feedback"])).toEqual({
      architecture: false,
      design: false,
      feedback: true,
    });
  });

  it("collapses nothing when no defaults are given (single-kind filter — user asked for it)", () => {
    expect(seedCollapsed(["feedback"], [])).toEqual({ feedback: false });
  });

  it("only marks sections that are actually present", () => {
    // a default naming an absent section never invents a key
    expect(seedCollapsed(["design"], ["feedback"])).toEqual({ design: false });
  });
});

describe("kindRoute — per-store icon/route", () => {
  it("routes each kind to its section page with the selection query param", () => {
    expect(kindRoute("bot-squad", doc("D-1"))).toBe("/p/bot-squad/docs?doc=D-1");
    expect(kindRoute("bot-squad", fb("F-1", "F-1-x.md"))).toBe("/p/bot-squad/docs/feedback?fb=F-1-x.md");
  });
});
