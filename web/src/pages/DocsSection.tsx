import { useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ArtifactKind } from "../api";
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
// ONE "Docs & Artifacts" view: a single shared left rail (type filter + one
// "+ New" picker + the cross-store tree) beside an <Outlet> that renders the
// per-kind detail pane. The tree is created here once and handed to the detail
// pages via Outlet context so a mutation in any pane refreshes the one rail.
// Each kind stays distinguishable by its icon (📄 doc · 🎯 use-case · 💬 feedback),
// so the dropped breadcrumb word is no loss. Deep-links (?doc=/?uc=/?fb=) and
// the legacy /feedback + /usecases redirects (App.tsx) are unchanged — clicking
// a node still routes to its kind's sub-page via kindRoute.

// Shared with the three detail pages through the router <Outlet>.
// T-0572 (Occam pass, D-0046): `manage` is the section-wide write toggle —
// the tree is a READ view by default; create/edit/promote/dismiss/delete
// affordances only render when the user explicitly flips it on (those
// actions fit the TG dialog / bsq CLI better than always-on web chrome).
export type DocsOutletContext = { tree: ArtifactTreeData; manage: boolean };

type FilterKind = ArtifactKind | "all";

const FILTERS: { key: FilterKind; label: string }[] = [
  { key: "all", label: "All" },
  { key: "doc", label: "📄 Docs" },
  { key: "use_case", label: "🎯 Use cases" },
  { key: "feedback", label: "💬 Feedback" },
];

export function DocsSection() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  // The ONE cross-store artifact tree the whole section shares.
  // item-12: feedback default-hides closed (promoted/dismissed); this toggle
  // re-fetches the tree with them included so closed items stay reviewable.
  const [showClosedFeedback, setShowClosedFeedback] = useState(false);
  const tree = useArtifacts(slug, 0, showClosedFeedback);

  const [filter, setFilter] = useState<FilterKind>("all");

  // T-0572: read-first — write affordances hidden until explicitly revealed.
  const [manage, setManage] = useState(false);

  // Unified "+ New" (replaces the old per-tab "+ New doc" / "+ New use case"
  // split). A type picker chooses doc vs use-case; feedback is collected
  // out-of-band (no in-UI create), so it's not offered.
  const [newOpen, setNewOpen] = useState(false);
  const [newKind, setNewKind] = useState<"doc" | "use_case" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // doc-create form fields (only used when newKind === "doc")
  const [categories, setCategories] = useState<string[]>([]);
  const [newCategory, setNewCategory] = useState("design");
  const [newTitle, setNewTitle] = useState("");
  const [newParent, setNewParent] = useState("");

  useEffect(() => {
    api.docCategories(slug).then(setCategories).catch(() => setCategories([]));
  }, [slug]);

  // Which kind/id the active sub-route has selected — drives the rail highlight.
  // index (/docs) → doc?doc=, /docs/feedback → feedback?fb=, /docs/usecases → uc?uc=
  let selectedKind: ArtifactKind = "doc";
  let selectedId: string | null = searchParams.get("doc");
  if (location.pathname.endsWith("/feedback")) {
    selectedKind = "feedback";
    selectedId = searchParams.get("fb");
  } else if (location.pathname.endsWith("/usecases")) {
    selectedKind = "use_case";
    selectedId = searchParams.get("uc");
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

  function pickNew(kind: "doc" | "use_case") {
    setNewOpen(false);
    setError(null);
    if (kind === "doc") {
      setNewKind("doc");
      setNewTitle("");
      setNewParent("");
    } else {
      createUseCase();
    }
  }

  async function createUseCase() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.createUseCase(slug, "New use case");
      tree.reload();
      navigate(`/p/${slug}/docs/usecases?uc=${encodeURIComponent(res.id)}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function createDoc() {
    const title = newTitle.trim();
    if (!title) {
      setError("Title required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.createDoc(slug, newCategory, title, newParent || null);
      tree.reload();
      setNewKind(null);
      setNewTitle("");
      setNewParent("");
      navigate(`/p/${slug}/docs?doc=${encodeURIComponent(res.id)}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="container py-4" style={{ maxWidth: "980px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Docs &amp; Artifacts
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        One view over every project artifact — docs (📄), use-cases (🎯) and user
        feedback (💬) — joined into a single cross-store tree (T-0283) that nests by{" "}
        <code>parent_doc_id</code>. Use the type filter to narrow the rail to one kind;
        click any node to open its detail. The tree is read-first —{" "}
        <strong>✎ Manage</strong> reveals the write affordances (create, edit,
        promote/dismiss, delete) when you need to curate by hand; day-to-day
        artifact writes flow in from the agents and the TG dialog.
      </PageHelp>

      {error && <div className="alert alert-danger py-1 small">{error}</div>}

      <div className="d-flex gap-4">
        {/* Shared left rail: manage toggle · type filter · the one cross-store tree */}
        <div style={{ minWidth: "240px", flex: "0 0 240px" }}>
          {/* T-0572: the section-wide write toggle (read-first by default). */}
          <div className="mb-2">
            <button
              type="button"
              className={`btn btn-sm w-100 ${manage ? "btn-secondary" : "btn-outline-secondary"}`}
              style={{ fontSize: "0.72rem" }}
              aria-pressed={manage}
              title="Reveal create/edit/promote/dismiss/delete affordances"
              onClick={() => { setManage((v) => !v); setNewOpen(false); setNewKind(null); }}
            >
              ✎ Manage
            </button>
          </div>
          {/* Unified "+ New" with a type picker (manage-gated, T-0572) */}
          {manage && (
          <div className="mb-2" style={{ position: "relative" }}>
            <button
              type="button"
              className="btn btn-outline-primary btn-sm w-100"
              style={{ fontSize: "0.72rem" }}
              disabled={busy}
              onClick={() => setNewOpen((v) => !v)}
            >
              {busy ? "Working…" : "+ New ▾"}
            </button>
            {newOpen && (
              <div
                className="d-flex flex-column gap-1 mt-1"
                style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.4rem" }}
              >
                <button type="button" className="btn btn-sm btn-outline-secondary text-start" style={{ fontSize: "0.72rem" }} onClick={() => pickNew("doc")}>
                  📄 Doc
                </button>
                <button type="button" className="btn btn-sm btn-outline-secondary text-start" style={{ fontSize: "0.72rem" }} onClick={() => pickNew("use_case")}>
                  🎯 Use case
                </button>
              </div>
            )}
          </div>
          )}

          {/* doc-create form (only when "+ New → Doc" picked) */}
          {manage && newKind === "doc" && (
            <div className="mb-3" style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.6rem" }}>
              <label className="form-label" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>Category</label>
              <select className="form-select form-select-sm mb-2" style={{ fontSize: "0.74rem" }} value={newCategory} onChange={(e) => setNewCategory(e.target.value)}>
                {categories.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <label className="form-label" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>Title</label>
              <input className="form-control form-control-sm mb-2" style={{ fontSize: "0.74rem" }} value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Doc title" autoFocus />
              {/* T-0235/T-0283: optionally attach the new doc under any artifact. */}
              <label className="form-label" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>Attach under (optional)</label>
              <select className="form-select form-select-sm mb-2" style={{ fontSize: "0.74rem" }} value={newParent} onChange={(e) => setNewParent(e.target.value)}>
                <option value="">— none (mother doc) —</option>
                {(tree.nodes ?? []).map((n) => <option key={`${n.kind}:${n.id}`} value={n.id}>{n.id} · {n.title}</option>)}
              </select>
              <div className="d-flex gap-2">
                <button type="button" className="btn btn-primary btn-sm flex-fill" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={createDoc}>
                  {busy ? "Creating…" : "Create"}
                </button>
                <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem" }} onClick={() => setNewKind(null)}>
                  Cancel
                </button>
              </div>
            </div>
          )}

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
          <Outlet context={{ tree, manage } satisfies DocsOutletContext} />
        </div>
      </div>
    </div>
  );
}
