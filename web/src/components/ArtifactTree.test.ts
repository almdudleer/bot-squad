import { describe, it, expect } from "vitest";
import { buildArtifactIndex, kindRoute, seedCollapsed, sortSections, ArtifactNode } from "./ArtifactTree";

// T-0283 (Pillar C / D-0029): the cross-store nesting tree is the core of the
// ticket. These lock the uniform tree logic across the three stores.

function doc(id: string, parent: string | null = null, category = "design"): ArtifactNode {
  return { id, title: id, kind: "doc", parent_doc_id: parent, routeId: id, section: category };
}
function uc(id: string, parent: string | null = null): ArtifactNode {
  return { id, title: id, kind: "use_case", parent_doc_id: parent, routeId: id, section: "use cases" };
}
function fb(id: string, name: string, parent: string | null = null): ArtifactNode {
  return { id, title: id, kind: "feedback", parent_doc_id: parent, routeId: name, section: "feedback" };
}

describe("buildArtifactIndex — cross-store nesting", () => {
  it("nests a doc child under a use-case mother (the live scenario)", () => {
    const idx = buildArtifactIndex([uc("UC-1"), doc("D-1", "UC-1")]);
    expect(idx.childrenOf.get("UC-1")?.map((n) => n.id)).toEqual(["D-1"]);
    // UC-1 is a root (no parent); D-1 is NOT a root (nested under UC-1).
    expect([...idx.rootsBySection.get("use cases")!].map((n) => n.id)).toEqual(["UC-1"]);
    expect(idx.rootsBySection.get("design")).toBeUndefined();
  });

  it("nests evidence docs under a feedback theme (cross-store)", () => {
    const idx = buildArtifactIndex([fb("F-1", "F-1.md"), doc("D-2", "F-1")]);
    expect(idx.childrenOf.get("F-1")?.map((n) => n.id)).toEqual(["D-2"]);
    expect(idx.rootsBySection.get("feedback")!.map((n) => n.id)).toEqual(["F-1"]);
  });

  it("treats a parent_doc_id that names a missing artifact as a root", () => {
    const idx = buildArtifactIndex([doc("D-3", "GHOST")]);
    expect(idx.rootsBySection.get("design")!.map((n) => n.id)).toEqual(["D-3"]);
  });

  it("groups doc roots by category, UC/feedback by store", () => {
    const idx = buildArtifactIndex([doc("D-a", null, "design"), doc("D-b", null, "runbook"), uc("UC-2"), fb("F-2", "F-2.md")]);
    expect([...idx.rootsBySection.keys()].sort()).toEqual(["design", "feedback", "runbook", "use cases"]);
  });

  it("descendantsOf walks cross-store and parentOptions excludes the own subtree (cycle-safe picker)", () => {
    // UC-1 -> D-1 -> F-1 (chain across all three stores)
    const idx = buildArtifactIndex([uc("UC-1"), doc("D-1", "UC-1"), fb("F-1", "F-1.md", "D-1")]);
    expect([...idx.descendantsOf("UC-1")].sort()).toEqual(["D-1", "F-1", "UC-1"]);
    // a re-parent picker for UC-1 must NOT offer D-1 or F-1 (would cycle).
    expect(idx.parentOptions("UC-1").map((n) => n.id)).toEqual([]);
    // but D-1 may be re-parented under F-1's... no: F-1 is its descendant. D-1's
    // options exclude D-1 + F-1, leaving UC-1.
    expect(idx.parentOptions("D-1").map((n) => n.id)).toEqual(["UC-1"]);
  });
});

describe("sortSections — doc categories first, store sections last (T-0352)", () => {
  it("sorts doc categories alphabetically, then use cases, then feedback last", () => {
    expect(sortSections(["feedback", "runbook", "use cases", "design", "architecture"]))
      .toEqual(["architecture", "design", "runbook", "use cases", "feedback"]);
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
    expect(kindRoute("bot-squad", uc("UC-1"))).toBe("/p/bot-squad/docs/usecases?uc=UC-1");
    expect(kindRoute("bot-squad", fb("F-1", "F-1-x.md"))).toBe("/p/bot-squad/docs/feedback?fb=F-1-x.md");
  });
});
