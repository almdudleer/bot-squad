import { useMemo, useState } from "react";
import { Outlet, useLocation, useParams, useSearchParams } from "react-router-dom";
import { ArtifactKind } from "../api";
import { PageHelp } from "../components/PageHelp";
import {
  ArtifactTreeData,
  ArtifactTreeView,
  buildArtifactIndex,
  sortSections,
  useArtifacts,
} from "../components/ArtifactTree";

// T-0337 (reframe Pillar C): the Docs section USED to be three tabs — DOCS /
// USER FEEDBACK / USE CASES — that each rendered the SAME unified cross-store
// artifact tree (T-0283) over a different detail pane. To a user the tabs were
// indistinguishable (only the breadcrumb word changed). This wrapper now owns
// ONE "Docs & Artifacts" view: a single shared left rail (type filter + the
// cross-store tree) beside an <Outlet> that renders the per-kind detail pane.
// The tree is created here once and handed to the detail pages via Outlet
// context so a mutation in any pane refreshes the one rail.
// Each kind stays distinguishable by its icon (📄 doc · 💬 feedback), so the
// dropped breadcrumb word is no loss. Deep-links (?doc=/?fb=) are unchanged —
// clicking a node still routes to its kind's sub-page via kindRoute.
// T-0671 (Lane D): the use_case entity + its /usecases sub-page are deleted —
// the type filter and selected-kind resolution below no longer offer it.

// Shared with the three detail pages through the router <Outlet>.
// T-0670 (D-0057 §4/§8, T-0637 Lane C): the Docs section is now a pure
// read/lookup surface — create/edit/promote/dismiss/delete all moved to the
// TG dialog (R5). The former T-0572 `manage` write-toggle is gone; there is
// nothing left for it to gate.
export type DocsOutletContext = { tree: ArtifactTreeData };

type FilterKind = ArtifactKind | "all";

const FILTERS: { key: FilterKind; label: string }[] = [
  { key: "all", label: "All" },
  { key: "doc", label: "📄 Docs" },
  { key: "feedback", label: "💬 Feedback" },
];

export function DocsSection() {
  const { slug = "" } = useParams();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  // The ONE cross-store artifact tree the whole section shares.
  // item-12: feedback default-hides closed (promoted/dismissed); this toggle
  // re-fetches the tree with them included so closed items stay reviewable.
  const [showClosedFeedback, setShowClosedFeedback] = useState(false);
  const tree = useArtifacts(slug, 0, showClosedFeedback);

  const [filter, setFilter] = useState<FilterKind>("all");

  // Which kind/id the active sub-route has selected — drives the rail highlight.
  // index (/docs) → doc?doc=, /docs/feedback → feedback?fb=
  let selectedKind: ArtifactKind = "doc";
  let selectedId: string | null = searchParams.get("doc");
  if (location.pathname.endsWith("/feedback")) {
    selectedKind = "feedback";
    selectedId = searchParams.get("fb");
  }

  // Type filter: rebuild a (flat) index from the kind-filtered node set so the
  // shared <ArtifactTreeView> renders only that kind. "all" keeps the full
  // cross-store nesting. (buildArtifactIndex is the same pure builder the hook
  // uses — no fork of the tree component.)
  const filteredNodes =
    tree.nodes === null ? null : filter === "all" ? tree.nodes : tree.nodes.filter((n) => n.kind === filter);
  const view: ArtifactTreeData =
    filter === "all"
      ? tree
      : { ...buildArtifactIndex(filteredNodes ?? []), nodes: filteredNodes, error: tree.error, reload: tree.reload };

  // T-0364: the merged "All" view was a ~6600px flat wall (every group
  // expanded). Start ALL groups collapsed in the All view so the page opens as
  // a compact, counted index — operator-facing categories first (sortSections),
  // agent-internal specs + feedback below. A single-kind filter stays expanded
  // (the user explicitly narrowed to that kind).
  const allCollapsedSections = useMemo(
    () => sortSections([...view.rootsBySection.keys()]),
    [view.rootsBySection],
  );

  return (
    <div className="container py-4" style={{ maxWidth: "980px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Docs &amp; Artifacts
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        One view over every project artifact — docs (📄) and user feedback (💬)
        — joined into a single cross-store tree (T-0283) that nests by{" "}
        <code>parent_doc_id</code>. Use the type filter to narrow the rail to one kind;
        click any node to open its detail. This is a read/lookup surface — create,
        edit, promote/dismiss and delete all happen via the TG dialog.
      </PageHelp>

      <div className="d-flex gap-4">
        {/* Shared left rail: type filter · the one cross-store tree */}
        <div style={{ minWidth: "240px", flex: "0 0 240px" }}>
          {/* Type filter (replaces the 3 tabs) */}
          <div className="btn-group btn-group-sm d-flex mb-2" role="group" aria-label="Artifact type filter">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                className={`btn ${filter === f.key ? "btn-secondary" : "btn-outline-secondary"}`}
                style={{ fontSize: "0.68rem", padding: "0.2rem 0.3rem" }}
                aria-pressed={filter === f.key}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
              </button>
            ))}
          </div>

          {/* item-12: reveal closed (promoted/dismissed) feedback, hidden by
              default. Shown when feedback is in view (All or Feedback filter). */}
          {(filter === "all" || filter === "feedback") && (
            <label
              className="d-flex align-items-center gap-1 mb-2"
              style={{ fontSize: "0.66rem", color: "var(--mc-text-dim)", cursor: "pointer" }}
            >
              <input
                type="checkbox"
                checked={showClosedFeedback}
                onChange={(e) => setShowClosedFeedback(e.target.checked)}
              />
              Show closed feedback (promoted / dismissed)
            </label>
          )}

          {/* T-0352/T-0364: in the mixed "All" view start EVERY group collapsed
              (was just feedback) so the page opens as a compact counted index
              instead of a ~6600px wall. A single-kind filter stays expanded (the
              user narrowed to that kind). `key={filter}` remounts the tree on a
              filter switch, re-seeding the collapse defaults. */}
          <ArtifactTreeView
            key={filter}
            slug={slug}
            data={view}
            selectedKind={selectedKind}
            selectedId={selectedId}
            defaultCollapsedSections={filter === "all" ? allCollapsedSections : []}
          />
        </div>

        {/* Detail pane — the active kind's page renders here, reading the shared
            tree from Outlet context. */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <Outlet context={{ tree } satisfies DocsOutletContext} />
        </div>
      </div>
    </div>
  );
}
