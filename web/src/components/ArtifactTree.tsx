import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, ArtifactKind } from "../api";

// T-0352 (ui-polish / dogfood D-0033): in the merged "All" view the ~33
// materialized feedback (F-*) roots dominate the rail and bury the docs. The
// tree sections are now collapsible; the caller seeds which sections start
// collapsed (feedback, in the All view) via `defaultCollapsedSections`. This
// pure seed builder maps each visible section to its initial collapsed flag —
// extracted so the default policy is unit-testable without a DOM.
export function seedCollapsed(
  sections: string[],
  defaultCollapsed: string[],
): Record<string, boolean> {
  const def = new Set(defaultCollapsed);
  const out: Record<string, boolean> = {};
  for (const s of sections) out[s] = def.has(s);
  return out;
}

// Doc categories first (alphabetical), then the two store sections last so the
// legacy docs view stays familiar. Shared by the view + tests.
export function sortSections(sections: string[]): string[] {
  const tail = ["use cases", "feedback"];
  return [...sections].sort((a, b) => {
    const ai = tail.indexOf(a), bi = tail.indexOf(b);
    if (ai !== -1 || bi !== -1) return (ai === -1 ? -1 : ai) - (bi === -1 ? -1 : bi);
    return a.localeCompare(b);
  });
}

// T-0283 (Pillar C / D-0029): the unified cross-store artifact tree. Docs,
// use-cases and feedback are all nestable artifacts joined by a single
// `parent_doc_id` edge that may cross stores (a UC mother can have doc
// children; a feedback theme can have evidence children). This module owns the
// ONE tree implementation the three Docs-section pages share, so nesting code
// is uniform across stores and each node picks its icon + route from `kind`.

export type ArtifactNode = {
  // Canonical artifact id used by the `parent_doc_id` edge (doc D-NNNN, use-case
  // UC-NNNN, or feedback filename stem). Children point at THIS.
  id: string;
  title: string;
  kind: ArtifactKind;
  parent_doc_id?: string | null;
  // The id/name a node's own page uses for routing + selection. Same as `id`
  // for docs/use-cases; the full filename for feedback.
  routeId: string;
  // Left-rail grouping header for ROOT nodes (doc category, or the store name).
  section: string;
};

const KIND_ICON: Record<ArtifactKind, string> = {
  doc: "📄",
  use_case: "🎯",
  feedback: "💬",
};

// Per-store route + selection key. Each page reads its selection from these
// query params (?doc= / ?uc= / ?fb=).
export function kindRoute(slug: string, node: ArtifactNode): string {
  const base = `/p/${slug}/docs`;
  const v = encodeURIComponent(node.routeId);
  if (node.kind === "use_case") return `${base}/usecases?uc=${v}`;
  if (node.kind === "feedback") return `${base}/feedback?fb=${v}`;
  return `${base}?doc=${v}`;
}

function feedbackTitle(name: string, content: string): string {
  const m = content.match(/^#\s+(.+)$/m);
  if (m) return m[1].trim();
  // F-0001-2026-05-18-shared-tree-commit-races.md -> readable-ish fallback
  return name.replace(/\.md$/, "").replace(/[-_]/g, " ");
}

export type ArtifactIndex = {
  byId: Map<string, ArtifactNode>;
  childrenOf: Map<string, ArtifactNode[]>;
  rootsBySection: Map<string, ArtifactNode[]>;
  // self + all (cross-store) descendants — used to keep a re-parent picker from
  // offering a cycle (the BE rejects too).
  descendantsOf: (id: string) => Set<string>;
  // Candidate parents for `id`: every artifact except its own subtree.
  parentOptions: (id: string) => ArtifactNode[];
};

export type ArtifactTreeData = ArtifactIndex & {
  nodes: ArtifactNode[] | null;
  error: string | null;
  reload: () => void;
};

// Pure cross-store index builder (no React) — the heart of T-0283. A node is a
// ROOT when its parent_doc_id is absent or names a node not in the set; children
// nest under their parent regardless of which store either lives in.
export function buildArtifactIndex(nodes: ArtifactNode[]): ArtifactIndex {
  const byId = new Map<string, ArtifactNode>();
  for (const n of nodes) byId.set(n.id, n);

  const childrenOf = new Map<string, ArtifactNode[]>();
  for (const n of nodes) {
    const p = n.parent_doc_id;
    if (p && byId.has(p)) {
      if (!childrenOf.has(p)) childrenOf.set(p, []);
      childrenOf.get(p)!.push(n);
    }
  }

  const isRoot = (n: ArtifactNode) => !n.parent_doc_id || !byId.has(n.parent_doc_id);
  const rootsBySection = new Map<string, ArtifactNode[]>();
  for (const n of nodes) {
    if (!isRoot(n)) continue;
    if (!rootsBySection.has(n.section)) rootsBySection.set(n.section, []);
    rootsBySection.get(n.section)!.push(n);
  }

  function descendantsOf(id: string): Set<string> {
    const out = new Set<string>([id]);
    const stack = [id];
    while (stack.length) {
      const cur = stack.pop()!;
      for (const c of childrenOf.get(cur) ?? []) {
        if (!out.has(c.id)) { out.add(c.id); stack.push(c.id); }
      }
    }
    return out;
  }

  function parentOptions(id: string): ArtifactNode[] {
    const banned = descendantsOf(id);
    return nodes.filter((n) => !banned.has(n.id));
  }

  return { byId, childrenOf, rootsBySection, descendantsOf, parentOptions };
}

// Load + index all three stores into one cross-store node set. `reloadKey`
// bumps re-fetch after a mutation.
export function useArtifacts(slug: string, reloadKey: number): ArtifactTreeData {
  const [nodes, setNodes] = useState<ArtifactNode[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setNodes(null);
    setError(null);
    Promise.allSettled([api.docs(slug), api.useCases(slug), api.feedback(slug)])
      .then(([docsR, ucR, fbR]) => {
        if (!alive) return;
        const out: ArtifactNode[] = [];
        if (docsR.status === "fulfilled") {
          for (const d of docsR.value)
            out.push({ id: d.id, title: d.title, kind: "doc", parent_doc_id: d.parent_doc_id ?? null, routeId: d.id, section: d.category });
        }
        if (ucR.status === "fulfilled") {
          for (const u of ucR.value)
            out.push({ id: u.id, title: u.title, kind: "use_case", parent_doc_id: u.parent_doc_id ?? null, routeId: u.id, section: "use cases" });
        }
        if (fbR.status === "fulfilled") {
          for (const f of fbR.value) {
            const id = f.id ?? f.name.replace(/\.md$/, "");
            out.push({ id, title: feedbackTitle(f.name, f.content), kind: "feedback", parent_doc_id: f.parent_doc_id ?? null, routeId: f.name, section: "feedback" });
          }
        }
        // Surface a store error only if EVERY store failed (one being slow/empty
        // shouldn't blank the rail).
        const errs = [docsR, ucR, fbR].filter((r) => r.status === "rejected");
        if (errs.length === 3) setError(String((errs[0] as PromiseRejectedResult).reason));
        setNodes(out);
      });
    return () => { alive = false; };
  }, [slug, reloadKey, tick]);

  const index = useMemo(() => buildArtifactIndex(nodes ?? []), [nodes]);

  return { nodes, error, ...index, reload: () => setTick((t) => t + 1) };
}

// Recursive node row — a Link to the node's per-store route. Same renderer for
// every store (uniform tree code); the icon + route come from `kind`.
function TreeNode({
  slug, node, data, selectedKind, selectedId, level,
}: {
  slug: string;
  node: ArtifactNode;
  data: ArtifactTreeData;
  selectedKind: ArtifactKind;
  selectedId: string | null;
  level: number;
}) {
  const kids = data.childrenOf.get(node.id) ?? [];
  const selected = node.kind === selectedKind && node.routeId === selectedId;
  return (
    <li className="mb-1" style={{ paddingLeft: level > 0 ? "0.7rem" : 0 }}>
      <Link
        to={kindRoute(slug, node)}
        className={`btn btn-sm w-100 text-start ${selected ? "btn-secondary" : "btn-outline-secondary"}`}
        style={{ fontSize: "0.74rem" }}
      >
        <span style={{ fontFamily: "var(--mc-mono)" }}>
          {level > 0 && <span style={{ color: "var(--mc-text-dim)" }}>└ </span>}
          <span title={node.kind} style={{ marginRight: "0.25rem" }}>{KIND_ICON[node.kind]}</span>
          {node.id}
        </span>
        {kids.length > 0 && (
          <span style={{ color: "var(--mc-text-dim)", fontSize: "0.68rem" }} title={`${kids.length} attached`}>
            {" "}📎{kids.length}
          </span>
        )}
        <br />
        <span style={{ color: "var(--mc-text-dim)", fontSize: "0.7rem" }}>{node.title}</span>
      </Link>
      {kids.length > 0 && (
        <ul className="list-unstyled m-0 mt-1">
          {kids.map((k) => (
            <TreeNode key={`${k.kind}:${k.id}`} slug={slug} node={k} data={data} selectedKind={selectedKind} selectedId={selectedId} level={level + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

// The shared left-rail tree: root artifacts grouped by section (doc categories,
// then use-cases, then feedback), each recursing its cross-store children.
export function ArtifactTreeView({
  slug, data, selectedKind, selectedId, defaultCollapsedSections = [],
}: {
  slug: string;
  data: ArtifactTreeData;
  selectedKind: ArtifactKind;
  selectedId: string | null;
  // T-0352: sections that should start collapsed (e.g. ["feedback"] in the
  // mixed All view so the F-* wall doesn't bury docs). Empty when the rail is
  // filtered to a single kind — the user explicitly asked for that kind.
  defaultCollapsedSections?: string[];
}) {
  const sections = useMemo(
    () => sortSections([...data.rootsBySection.keys()]),
    [data.rootsBySection],
  );
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() =>
    seedCollapsed(sections, defaultCollapsedSections),
  );
  // Seed the default collapsed flag for any section that appears after the
  // async store load — without clobbering the user's manual toggles. (On a
  // filter change the parent remounts this view via `key`, resetting toggles.)
  useEffect(() => {
    setCollapsed((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const s of sections) {
        if (!(s in next)) { next[s] = defaultCollapsedSections.includes(s); changed = true; }
      }
      return changed ? next : prev;
    });
  }, [sections, defaultCollapsedSections]);

  if (data.error) return <div className="alert alert-danger py-1 small">{data.error}</div>;
  if (data.nodes === null) return <div className="mc-loading">Loading</div>;
  if (data.nodes.length === 0) return <div className="text-muted small">No artifacts yet.</div>;

  return (
    <>
      {sections.map((sec) => {
        const roots = data.rootsBySection.get(sec)!;
        const isCollapsed = collapsed[sec] ?? false;
        return (
          <div key={sec} className="mb-2">
            <button
              type="button"
              className="mc-sidebar-subsection w-100 text-start d-flex align-items-center"
              style={{ marginTop: 0, background: "none", border: "none", cursor: "pointer", padding: 0 }}
              aria-expanded={!isCollapsed}
              onClick={() => setCollapsed((c) => ({ ...c, [sec]: !isCollapsed }))}
            >
              <span style={{ width: "0.9rem", display: "inline-block", color: "var(--mc-text-dim)" }}>
                {isCollapsed ? "▸" : "▾"}
              </span>
              {sec}
              <span style={{ color: "var(--mc-text-dim)", marginLeft: "0.35rem", fontWeight: 400 }}>
                ({roots.length})
              </span>
            </button>
            {!isCollapsed && (
              <ul className="list-unstyled m-0">
                {roots.map((n) => (
                  <TreeNode key={`${n.kind}:${n.id}`} slug={slug} node={n} data={data} selectedKind={selectedKind} selectedId={selectedId} level={0} />
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </>
  );
}

// A small "move under / set parent" control shared by the three detail panes.
// Renders the current parent (a cross-store link), a Detach button, and an
// "Attach under…" picker (own subtree filtered out). The caller wires the
// actual PUT .../parent call + inline cycle-error surfacing.
export function ReparentControl({
  slug, data, selfId, currentParentId, busy, onSetParent,
}: {
  slug: string;
  data: ArtifactTreeData;
  selfId: string;
  currentParentId: string | null | undefined;
  busy: boolean;
  onSetParent: (parentId: string | null) => void;
}) {
  const parentNode = currentParentId ? data.byId.get(currentParentId) : undefined;
  return (
    <div className="mb-3" style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.6rem" }}>
      <div className="mc-section-title" style={{ margin: "0 0 0.4rem 0" }}>Nesting</div>
      {currentParentId ? (
        <div className="d-flex align-items-center gap-2 mb-2" style={{ fontSize: "0.74rem" }}>
          <span style={{ color: "var(--mc-text-dim)" }}>Attached under</span>
          {parentNode ? (
            <Link to={kindRoute(slug, parentNode)} style={{ fontFamily: "var(--mc-mono)" }}>
              {KIND_ICON[parentNode.kind]} {parentNode.id}
            </Link>
          ) : (
            <span style={{ fontFamily: "var(--mc-mono)" }}>{currentParentId}</span>
          )}
          <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.7rem" }} disabled={busy} onClick={() => onSetParent(null)}>
            Detach
          </button>
        </div>
      ) : (
        <div className="mb-2" style={{ fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>
          Root artifact (no parent).
        </div>
      )}
      <select
        className="form-select form-select-sm"
        style={{ fontSize: "0.74rem", maxWidth: "22rem" }}
        value=""
        disabled={busy}
        aria-label="Attach this artifact under a mother artifact"
        onChange={(e) => { if (e.target.value) onSetParent(e.target.value); }}
      >
        <option value="">Move under…</option>
        {data.parentOptions(selfId)
          .filter((n) => n.id !== currentParentId)
          .map((n) => (
            <option key={`${n.kind}:${n.id}`} value={n.id}>{KIND_ICON[n.kind]} {n.id} · {n.title}</option>
          ))}
      </select>
    </div>
  );
}
