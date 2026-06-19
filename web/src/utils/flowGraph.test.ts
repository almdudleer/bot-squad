import { describe, expect, test } from "vitest";

import {
  emptyGraph,
  graphToMermaid,
  nextNodeId,
  parseGraphFromRaw,
  serializeFlowMd,
  validateGraph,
  type FlowGraph,
} from "./flowGraph";

const SAMPLE: FlowGraph = {
  nodes: [
    { id: "n1", label: "Open settings", selector: "#settings", expected: "panel shows" },
    { id: "n2", label: "Save", assertion: "toast appears" },
  ],
  edges: [{ from: "n1", to: "n2", label: "click save" }],
};

const RAW = `---
id: UF-0001
uc_id: UC-0001
title: "Demo flow"
status: draft
---

# Demo flow

## Steps

1. TBD

## Mermaid

\`\`\`mermaid
graph TD
  A[start] --> B[TBD]
\`\`\`

(prose)
`;

describe("nextNodeId", () => {
  test("skips taken ids", () => {
    expect(nextNodeId(emptyGraph())).toBe("n1");
    expect(nextNodeId({ nodes: [{ id: "n1", label: "x" }], edges: [] })).toBe("n2");
    expect(
      nextNodeId({ nodes: [{ id: "n1", label: "x" }, { id: "n3", label: "y" }], edges: [] }),
    ).toBe("n2");
  });
});

describe("validateGraph", () => {
  test("clean graph → no errors", () => {
    expect(validateGraph(SAMPLE)).toEqual([]);
  });
  test("flags duplicate ids, empty labels, and dangling edges", () => {
    const bad: FlowGraph = {
      nodes: [{ id: "n1", label: "" }, { id: "n1", label: "dup" }],
      edges: [{ from: "n1", to: "ghost" }],
    };
    const errs = validateGraph(bad);
    expect(errs.some((e) => e.includes("duplicate node id"))).toBe(true);
    expect(errs.some((e) => e.includes("empty label"))).toBe(true);
    expect(errs.some((e) => e.includes("unknown node: ghost"))).toBe(true);
  });
});

describe("graphToMermaid", () => {
  test("emits graph TD with labelled nodes + edges", () => {
    expect(graphToMermaid(SAMPLE)).toBe(
      [
        "graph TD",
        '  n1["Open settings"]',
        '  n2["Save"]',
        "  n1 -->|click save| n2",
      ].join("\n"),
    );
  });
  test("edge without label uses a bare arrow", () => {
    const g: FlowGraph = { nodes: [{ id: "a", label: "A" }, { id: "b", label: "B" }], edges: [{ from: "a", to: "b" }] };
    expect(graphToMermaid(g)).toContain("  a --> b");
  });
  test("escapes quotes + sanitizes unsafe ids", () => {
    const g: FlowGraph = { nodes: [{ id: "1 bad", label: 'say "hi"' }], edges: [] };
    const out = graphToMermaid(g);
    expect(out).toContain("&quot;hi&quot;");
    expect(out).toContain("n_1_bad");
  });
  test("deterministic — re-render is byte-identical", () => {
    expect(graphToMermaid(SAMPLE)).toBe(graphToMermaid(SAMPLE));
  });
});

describe("serializeFlowMd + parseGraphFromRaw round-trip", () => {
  test("upserts single-line graph json into frontmatter, preserving other keys", () => {
    const out = serializeFlowMd(RAW, SAMPLE);
    expect(out).toMatch(/^---\n/);
    expect(out).toContain("id: UF-0001");
    expect(out).toContain('title: "Demo flow"');
    // single-line graph key
    const graphLines = out.split("\n").filter((l) => l.startsWith("graph:"));
    expect(graphLines).toHaveLength(1);
    expect(parseGraphFromRaw(out)).toEqual(SAMPLE);
  });

  test("replaces the existing mermaid block with the derived diagram", () => {
    const out = serializeFlowMd(RAW, SAMPLE);
    expect(out).not.toContain("A[start] --> B[TBD]");
    expect(out).toContain('n1["Open settings"]');
    // exactly one mermaid block
    expect(out.match(/```mermaid/g)).toHaveLength(1);
  });

  test("re-serializing is idempotent (no diff churn)", () => {
    const once = serializeFlowMd(RAW, SAMPLE);
    const twice = serializeFlowMd(once, parseGraphFromRaw(once)!);
    expect(twice).toBe(once);
  });

  test("empty graph drops the key and leaves hand mermaid untouched (back-compat)", () => {
    const withGraph = serializeFlowMd(RAW, SAMPLE);
    const cleared = serializeFlowMd(withGraph, emptyGraph());
    expect(cleared.split("\n").some((l) => l.startsWith("graph:"))).toBe(false);
    expect(parseGraphFromRaw(cleared)).toBeNull();
  });

  test("appends a ## Mermaid section when the flow had none", () => {
    const noMermaid = `---\nid: UF-0002\n---\n\n# Bare\n\n## Steps\n\n1. x\n`;
    const out = serializeFlowMd(noMermaid, SAMPLE);
    expect(out).toContain("## Mermaid");
    expect(out).toContain('n1["Open settings"]');
  });

  test("markdown-only flow parses to null graph", () => {
    expect(parseGraphFromRaw(RAW)).toBeNull();
  });
});
