/**
 * T-0226 — Use-cases v3 flow node-graph model + pure serde/derivation.
 *
 * The flow graph is the single source of truth for a structured user flow; the
 * `## Mermaid` diagram is DERIVED from it (parent T-0182 DoD). Storage rides in
 * the flow md FRONTMATTER as ONE line — `graph: {json}` — because JSON is valid
 * YAML flow syntax, so the backend's `yaml.safe_load` parses it natively (and
 * `get_flow` already surfaces it via `{**meta}`), and the Team-3 walker
 * (`bsq uc flow run`) reads the SAME key. No new backend endpoint: the existing
 * `put_flow` persists the raw md verbatim, so the FE owns graph persistence.
 *
 * Back-compat: a flow with no `graph:` key is a markdown-only flow and renders
 * exactly as before; clearing all nodes drops the key and reverts to that.
 */

export type FlowNode = {
  id: string; // stable within the flow (mermaid-safe: [A-Za-z0-9_])
  label: string; // human step name
  selector?: string; // playwright selector the walker drives
  expected?: string; // expected UI/state after this step
  assertion?: string; // agent-manual assertion (natural language, T-0158)
};

export type FlowEdge = {
  from: string; // FlowNode.id
  to: string; // FlowNode.id
  label?: string; // branch / transition condition
};

export type FlowGraph = { nodes: FlowNode[]; edges: FlowEdge[] };

export function emptyGraph(): FlowGraph {
  return { nodes: [], edges: [] };
}

export function isEmptyGraph(g: FlowGraph | null | undefined): boolean {
  return !g || g.nodes.length === 0;
}

/** Allocate the next `n<N>` id not already taken by a node. */
export function nextNodeId(g: FlowGraph): string {
  const taken = new Set(g.nodes.map((n) => n.id));
  let i = 1;
  while (taken.has(`n${i}`)) i += 1;
  return `n${i}`;
}

/** Structural problems that should block a save. Empty array == OK. */
export function validateGraph(g: FlowGraph): string[] {
  const errs: string[] = [];
  const ids = g.nodes.map((n) => n.id);
  const seen = new Set<string>();
  for (const id of ids) {
    if (!id.trim()) errs.push("a node has an empty id");
    else if (seen.has(id)) errs.push(`duplicate node id: ${id}`);
    seen.add(id);
  }
  for (const n of g.nodes) {
    if (!n.label.trim()) errs.push(`node ${n.id} has an empty label`);
  }
  const idSet = new Set(ids);
  for (const e of g.edges) {
    if (!idSet.has(e.from)) errs.push(`edge references unknown node: ${e.from}`);
    if (!idSet.has(e.to)) errs.push(`edge references unknown node: ${e.to}`);
  }
  return errs;
}

// --- mermaid derivation -----------------------------------------------------

/** Mermaid node-id must be a bare identifier; sanitize defensively. */
function safeId(id: string): string {
  const s = id.replace(/[^A-Za-z0-9_]/g, "_");
  return /^[A-Za-z_]/.test(s) ? s : `n_${s}`;
}

/** Escape a mermaid bracket label: kill the chars that break `id["..."]`. */
function mermaidLabel(text: string): string {
  return text.replace(/"/g, "&quot;").replace(/[\r\n]+/g, " ").trim();
}

/** Derive a `graph TD` mermaid source from the node graph. Deterministic so a
 *  re-save with no change produces byte-identical output (no diff churn). */
export function graphToMermaid(g: FlowGraph): string {
  const lines = ["graph TD"];
  for (const n of g.nodes) {
    lines.push(`  ${safeId(n.id)}["${mermaidLabel(n.label || n.id)}"]`);
  }
  for (const e of g.edges) {
    const lbl = (e.label ?? "").trim();
    const arrow = lbl ? `-->|${mermaidLabel(lbl)}|` : "-->";
    lines.push(`  ${safeId(e.from)} ${arrow} ${safeId(e.to)}`);
  }
  return lines.join("\n");
}

// --- raw-md frontmatter + body serde ---------------------------------------

const FM_RE = /^---\n([\s\S]*?)\n---\n?([\s\S]*)$/;
const MERMAID_BLOCK_RE = /```mermaid\s*\n[\s\S]*?```/;

/** Read the graph out of a raw flow md's frontmatter. Tolerant: returns null
 *  when there's no frontmatter / no `graph:` / malformed JSON (markdown-only
 *  flow). The live page prefers `flowDetail.graph` (already parsed server-side);
 *  this exists for tests + a pure round-trip. */
export function parseGraphFromRaw(raw: string): FlowGraph | null {
  const m = raw.match(FM_RE);
  if (!m) return null;
  const line = m[1].split("\n").find((l) => l.startsWith("graph:"));
  if (!line) return null;
  try {
    const parsed = JSON.parse(line.slice("graph:".length).trim());
    if (!parsed || !Array.isArray(parsed.nodes) || !Array.isArray(parsed.edges)) {
      return null;
    }
    return parsed as FlowGraph;
  } catch {
    return null;
  }
}

/**
 * Produce updated raw flow md persisting `graph` and a DERIVED mermaid block.
 * - upserts a single-line `graph: {json}` in the frontmatter (drops it when the
 *   graph is empty → reverts to a markdown-only flow);
 * - replaces the body's ```mermaid``` block with the derived diagram (or appends
 *   a `## Mermaid` section if none exists). An empty graph leaves the body's
 *   hand-authored mermaid untouched (back-compat).
 */
export function serializeFlowMd(raw: string, graph: FlowGraph): string {
  const m = raw.match(FM_RE);
  // No frontmatter (shouldn't happen for real flows) — wrap minimally.
  const fmText = m ? m[1] : "";
  let body = m ? m[2] : raw;

  const fmLines = fmText.split("\n").filter((l) => !l.startsWith("graph:"));
  if (!isEmptyGraph(graph)) {
    fmLines.push(`graph: ${JSON.stringify(graph)}`);
  }
  const fm = fmLines.filter((l) => l.length > 0).join("\n");

  if (!isEmptyGraph(graph)) {
    const derived = "```mermaid\n" + graphToMermaid(graph) + "\n```";
    if (MERMAID_BLOCK_RE.test(body)) {
      body = body.replace(MERMAID_BLOCK_RE, derived);
    } else {
      const sep = body.endsWith("\n") ? "" : "\n";
      body = `${body}${sep}\n## Mermaid\n\n${derived}\n`;
    }
  }

  return `---\n${fm}\n---\n\n${body.replace(/^\n+/, "")}`;
}
