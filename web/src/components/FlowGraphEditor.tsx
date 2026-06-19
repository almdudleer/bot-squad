/**
 * T-0226 — structured node-graph editor for a use-case user flow.
 *
 * Form-based (per ticket: "form is fine; drag-drop optional"). The graph is the
 * SSOT; a live mermaid preview is DERIVED from it. On save we serialize the
 * graph into the flow md frontmatter + regenerate the `## Mermaid` block
 * (`serializeFlowMd`) and hand the new raw md back to the caller, which persists
 * it via the existing `putFlow` (no new backend endpoint — see flowGraph.ts).
 */
import { useState } from "react";
import { Mermaid } from "./Mermaid";
import {
  FlowGraph,
  FlowNode,
  emptyGraph,
  graphToMermaid,
  nextNodeId,
  serializeFlowMd,
  validateGraph,
} from "../utils/flowGraph";

const NODE_FIELDS: { key: keyof FlowNode; placeholder: string }[] = [
  { key: "label", placeholder: "step label (required)" },
  { key: "selector", placeholder: "playwright selector (optional)" },
  { key: "expected", placeholder: "expected UI/state (optional)" },
  { key: "assertion", placeholder: "manual assertion (optional)" },
];

export function FlowGraphEditor({
  initial,
  rawMd,
  busy,
  onSave,
  onCancel,
}: {
  initial: FlowGraph | undefined;
  rawMd: string;
  busy: boolean;
  onSave: (newRaw: string) => void;
  onCancel: () => void;
}) {
  const [graph, setGraph] = useState<FlowGraph>(() =>
    initial && initial.nodes ? structuredClone(initial) : emptyGraph(),
  );

  function patchNode(id: string, field: keyof FlowNode, value: string) {
    setGraph((g) => ({
      ...g,
      nodes: g.nodes.map((n) => (n.id === id ? { ...n, [field]: value } : n)),
    }));
  }
  function addNode() {
    setGraph((g) => ({ ...g, nodes: [...g.nodes, { id: nextNodeId(g), label: "" }] }));
  }
  function removeNode(id: string) {
    setGraph((g) => ({
      nodes: g.nodes.filter((n) => n.id !== id),
      edges: g.edges.filter((e) => e.from !== id && e.to !== id),
    }));
  }
  function addEdge() {
    setGraph((g) => {
      if (g.nodes.length < 1) return g;
      const first = g.nodes[0].id;
      const second = g.nodes[1]?.id ?? first;
      return { ...g, edges: [...g.edges, { from: first, to: second }] };
    });
  }
  function patchEdge(i: number, field: "from" | "to" | "label", value: string) {
    setGraph((g) => ({
      ...g,
      edges: g.edges.map((e, j) => (j === i ? { ...e, [field]: value } : e)),
    }));
  }
  function removeEdge(i: number) {
    setGraph((g) => ({ ...g, edges: g.edges.filter((_, j) => j !== i) }));
  }

  const errs = validateGraph(graph);
  const canSave = errs.length === 0 && !busy;

  return (
    <div style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.75rem" }}>
      <div className="d-flex justify-content-between align-items-center mb-2">
        <div style={{ fontSize: "0.78rem", fontWeight: 600 }}>Graph editor</div>
        <div style={{ fontSize: "0.68rem", color: "var(--mc-text-dim)" }}>
          mermaid is derived — graph is the source of truth
        </div>
      </div>

      {/* Nodes */}
      <div className="mc-section-title" style={{ margin: "0 0 0.4rem" }}>
        Nodes ({graph.nodes.length})
      </div>
      {graph.nodes.length === 0 && (
        <p style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
          No nodes yet — add the first step. Saving with zero nodes reverts this
          flow to markdown-only.
        </p>
      )}
      {graph.nodes.map((n) => (
        <div
          key={n.id}
          className="mb-2"
          style={{ paddingLeft: "0.5rem", borderLeft: "2px solid var(--mc-border)" }}
        >
          <div className="d-flex align-items-center gap-2 mb-1">
            <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>
              {n.id}
            </span>
            <button
              type="button"
              className="btn btn-outline-danger btn-sm py-0"
              style={{ fontSize: "0.64rem" }}
              onClick={() => removeNode(n.id)}
            >
              remove
            </button>
          </div>
          <div className="d-flex flex-column gap-1">
            {NODE_FIELDS.map((f) => (
              <input
                key={f.key}
                type="text"
                className="form-control form-control-sm"
                style={{ fontSize: "0.72rem" }}
                placeholder={f.placeholder}
                value={(n[f.key] as string) ?? ""}
                onChange={(e) => patchNode(n.id, f.key, e.target.value)}
              />
            ))}
          </div>
        </div>
      ))}
      <button
        type="button"
        className="btn btn-outline-primary btn-sm mb-3"
        style={{ fontSize: "0.7rem" }}
        onClick={addNode}
      >
        + Add node
      </button>

      {/* Edges */}
      <div className="mc-section-title" style={{ margin: "0 0 0.4rem" }}>
        Edges ({graph.edges.length})
      </div>
      {graph.edges.map((e, i) => (
        <div key={i} className="d-flex align-items-center gap-1 mb-1">
          <select
            className="form-select form-select-sm"
            style={{ fontSize: "0.7rem", width: "auto" }}
            value={e.from}
            onChange={(ev) => patchEdge(i, "from", ev.target.value)}
          >
            {graph.nodes.map((n) => (
              <option key={n.id} value={n.id}>{n.id}</option>
            ))}
          </select>
          <span style={{ color: "var(--mc-text-dim)" }}>→</span>
          <select
            className="form-select form-select-sm"
            style={{ fontSize: "0.7rem", width: "auto" }}
            value={e.to}
            onChange={(ev) => patchEdge(i, "to", ev.target.value)}
          >
            {graph.nodes.map((n) => (
              <option key={n.id} value={n.id}>{n.id}</option>
            ))}
          </select>
          <input
            type="text"
            className="form-control form-control-sm"
            style={{ fontSize: "0.7rem", width: "12rem" }}
            placeholder="branch/condition (optional)"
            value={e.label ?? ""}
            onChange={(ev) => patchEdge(i, "label", ev.target.value)}
          />
          <button
            type="button"
            className="btn btn-outline-danger btn-sm py-0"
            style={{ fontSize: "0.64rem" }}
            onClick={() => removeEdge(i)}
          >
            remove
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn btn-outline-primary btn-sm mb-3"
        style={{ fontSize: "0.7rem" }}
        onClick={addEdge}
        disabled={graph.nodes.length < 1}
        title={graph.nodes.length < 1 ? "Add a node first" : "Add an edge"}
      >
        + Add edge
      </button>

      {/* Live derived preview */}
      {graph.nodes.length > 0 && (
        <div className="mb-2">
          <div className="mc-section-title" style={{ margin: "0 0 0.4rem" }}>
            Derived mermaid (preview)
          </div>
          <Mermaid code={graphToMermaid(graph)} />
        </div>
      )}

      {errs.length > 0 && (
        <div className="alert alert-warning py-1 small mb-2">
          {errs.map((e, i) => (
            <div key={i}>• {e}</div>
          ))}
        </div>
      )}

      <div className="d-flex gap-2">
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={!canSave}
          onClick={() => onSave(serializeFlowMd(rawMd, graph))}
        >
          {busy ? "Saving…" : "Save graph"}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}
