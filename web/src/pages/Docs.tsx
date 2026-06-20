import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, DocDetail, errorDetail } from "../api";
import { PageHelp } from "../components/PageHelp";
import { Markdown } from "../components/Markdown";
import { ArtifactTreeView, ReparentControl, useArtifacts } from "../components/ArtifactTree";

export function Docs() {
  const { slug = "" } = useParams();
  const [searchParams] = useSearchParams();

  // T-0283 (Pillar C): the left rail is now the UNIFIED cross-store artifact
  // tree (docs + use-cases + feedback, nested by parent_doc_id). Clicking a UC
  // or feedback node routes to its own section page; docs open in place.
  const tree = useArtifacts(slug, 0);

  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocDetail | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  // + New doc form
  const [categories, setCategories] = useState<string[]>([]);
  const [newCategory, setNewCategory] = useState("design");
  const [newTitle, setNewTitle] = useState("");
  const [newParent, setNewParent] = useState(""); // T-0235/T-0283: optional mother artifact (cross-store)
  const [showNew, setShowNew] = useState(false);

  // link-a-ticket input
  const [linkTicket, setLinkTicket] = useState("");

  useEffect(() => {
    api.docCategories(slug).then(setCategories).catch(() => setCategories([]));
  }, [slug]);

  // Deep-link: /p/:slug/docs?doc=D-NNNN opens that doc (from ticket pages + the
  // shared tree's doc nodes).
  useEffect(() => {
    const d = searchParams.get("doc");
    if (d && d !== selected) open(d);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, slug]);

  async function setParent(childId: string, parentId: string | null) {
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      await api.setDocParent(slug, childId, parentId);
      tree.reload();
      if (selected) await open(selected);
      setFlash(parentId ? `Attached ${childId} under ${parentId}.` : `Detached ${childId}.`);
    } catch (e) {
      setError(errorDetail(e)); // T-0283: surface the BE 4xx cycle/validation detail inline
    } finally {
      setBusy(false);
    }
  }

  async function open(id: string) {
    setSelected(id);
    setDetail(null);
    setDraft(null);
    setFlash(null);
    setLinkTicket("");
    try {
      setDetail(await api.doc(slug, id));
    } catch (e) {
      setError(String(e));
    }
  }

  async function createDoc() {
    const title = newTitle.trim();
    if (!title) { setError("Title required."); return; }
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      const res = await api.createDoc(slug, newCategory, title, newParent || null);
      tree.reload();
      const d = await api.doc(slug, res.id);
      setSelected(res.id);
      setDetail(d);
      setDraft(d.raw);
      setShowNew(false);
      setNewTitle("");
      setNewParent("");
      setFlash(
        newParent
          ? `Created ${res.id} attached under ${newParent} — edit below, then Save.`
          : `Created ${res.id} in ${res.category} — edit below, then Save.`,
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // T-0276: delete a doc. Matches the app's destructive-action pattern
  // (window.confirm gate). The BE refuses (409) a mother doc that still has
  // children; surface that detail inline via errorDetail.
  async function removeDoc() {
    if (!selected) return;
    const id = selected;
    if (!window.confirm(`Delete ${id}? This cannot be undone (the id is retired, not reused).`)) return;
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      await api.deleteDoc(slug, id);
      setSelected(null);
      setDetail(null);
      setDraft(null);
      tree.reload();
      setFlash(`Deleted ${id}.`);
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await api.putDoc(slug, selected, draft ?? "");
      setDraft(null);
      tree.reload();
      await open(selected);
      setFlash("Saved.");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function link() {
    if (!selected) return;
    const t = linkTicket.trim().toUpperCase();
    if (!/^T-\d{4}$/.test(t)) { setError("Ticket must look like T-0123."); return; }
    setBusy(true);
    setError(null);
    try {
      await api.linkDoc(slug, selected, t);
      setLinkTicket("");
      await open(selected);
      setFlash(`Linked ${t}.`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function unlink(ticket: string) {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await api.unlinkDoc(slug, selected, ticket);
      await open(selected);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // T-0283/D-0029: read the cross-store superset, falling back to the legacy
  // doc-only key.
  const childIds = detail ? (detail.child_artifact_ids ?? detail.child_doc_ids ?? []) : [];

  return (
    <div className="container py-4" style={{ maxWidth: "980px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Docs
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        Project docs stored as <code>data/&lt;slug&gt;/docs/&lt;category&gt;/D-NNNN-&lt;slug&gt;.md</code> and
        symlinked into the dev/master clones (<code>bsq docs sync</code>) so agents reach them at
        <code> docs/&lt;category&gt;/…</code>. The left rail is the unified artifact tree (T-0283): docs,
        use-cases (🎯) and feedback (💬) nest under one another by <code>parent_doc_id</code>, across stores.
        Docs link bidirectionally to tickets (T-0172).
      </PageHelp>

      {error && <div className="alert alert-danger py-1 small">{error}</div>}
      {flash && <div className="alert alert-success py-1 small">{flash}</div>}

      <div className="d-flex gap-4">
        {/* Unified cross-store artifact tree */}
        <div style={{ minWidth: "240px", flex: "0 0 240px" }}>
          <button type="button" className="btn btn-outline-primary btn-sm w-100 mb-2" style={{ fontSize: "0.72rem" }} onClick={() => setShowNew((v) => !v)}>
            + New doc
          </button>
          {showNew && (
            <div className="mb-3" style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.6rem" }}>
              <label className="form-label" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>Category</label>
              <select className="form-select form-select-sm mb-2" style={{ fontSize: "0.74rem" }} value={newCategory} onChange={(e) => setNewCategory(e.target.value)}>
                {categories.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <label className="form-label" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>Title</label>
              <input className="form-control form-control-sm mb-2" style={{ fontSize: "0.74rem" }} value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Doc title" />
              {/* T-0235/T-0283: optionally attach the new doc under any artifact. */}
              <label className="form-label" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>Attach under (optional)</label>
              <select className="form-select form-select-sm mb-2" style={{ fontSize: "0.74rem" }} value={newParent} onChange={(e) => setNewParent(e.target.value)}>
                <option value="">— none (mother doc) —</option>
                {(tree.nodes ?? []).map((n) => <option key={`${n.kind}:${n.id}`} value={n.id}>{n.id} · {n.title}</option>)}
              </select>
              <button type="button" className="btn btn-primary btn-sm w-100" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={createDoc}>
                {busy ? "Creating…" : "Create"}
              </button>
            </div>
          )}
          <ArtifactTreeView slug={slug} data={tree} selectedKind="doc" selectedId={selected} />
        </div>

        {/* Detail / editor */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {selected === null && <div className="text-muted small">Select an artifact, or create a doc.</div>}

          {selected !== null && draft === null && detail && (
            <>
              <div className="d-flex justify-content-between align-items-center mb-2">
                <div style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", fontWeight: 600 }}>
                  {detail.id}
                  <span style={{ color: "var(--mc-text-dim)", marginLeft: "0.5rem" }}>{detail.category}</span>
                </div>
                <div className="d-flex gap-2">
                  <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem" }} onClick={() => setDraft(detail.raw)}>
                    Edit
                  </button>
                  <button type="button" className="btn btn-outline-danger btn-sm" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={removeDoc}>
                    Delete
                  </button>
                </div>
              </div>

              {/* T-0235/T-0283: cross-store nesting — set/clear this doc's parent
                  (PUT /docs/{id}/parent, cycle-safe) + list its attached
                  artifacts (the cross-store child_artifact_ids superset). */}
              <ReparentControl
                slug={slug}
                data={tree}
                selfId={detail.id}
                currentParentId={detail.parent_doc_id}
                busy={busy}
                onSetParent={(pid) => setParent(detail.id, pid)}
              />
              {childIds.length > 0 && (
                <div className="mb-3">
                  <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginBottom: "0.25rem" }}>
                    Attached artifacts ({childIds.length}):
                  </div>
                  <div className="d-flex flex-wrap gap-2">
                    {childIds.map((cid) => {
                      const n = tree.byId.get(cid);
                      return (
                        <button key={cid} type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }} onClick={() => open(cid)} disabled={!!n && n.kind !== "doc"} title={n ? n.title : cid}>
                          📎 {cid}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Related tickets — the doc→ticket half of the bidirectional mention */}
              <div className="mb-3">
                <div className="mc-section-title" style={{ margin: "0 0 0.4rem 0" }}>Related tickets</div>
                <div className="d-flex flex-wrap gap-2 align-items-center mb-2">
                  {(detail.related_tickets ?? []).length === 0 && (
                    <span style={{ fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>none yet</span>
                  )}
                  {(detail.related_tickets ?? []).map((t) => (
                    <span key={t} className="d-inline-flex align-items-center gap-1" style={{ fontSize: "0.74rem" }}>
                      <Link to={`/p/${slug}/t/${t}`} style={{ fontFamily: "var(--mc-mono)" }}>{t}</Link>
                      <button type="button" className="btn btn-link btn-sm p-0" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }} title="Unlink" disabled={busy} onClick={() => unlink(t)}>✕</button>
                    </span>
                  ))}
                </div>
                <div className="d-flex gap-2" style={{ maxWidth: "20rem" }}>
                  <input className="form-control form-control-sm" style={{ fontSize: "0.74rem" }} placeholder="T-0123" value={linkTicket} onChange={(e) => setLinkTicket(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); link(); } }} />
                  <button type="button" className="btn btn-outline-primary btn-sm" style={{ fontSize: "0.7rem" }} disabled={busy} onClick={link}>Link</button>
                </div>
              </div>

              {/* T-0275: render the doc body as markdown. */}
              <Markdown source={detail.raw} slug={slug} />
            </>
          )}

          {draft !== null && (
            <>
              <textarea
                className="form-control mb-2"
                rows={24}
                value={draft}
                style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}
                onChange={(e) => setDraft(e.target.value)}
                autoFocus
              />
              <div className="d-flex gap-2">
                <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={busy}>
                  {busy ? "Saving…" : "Save"}
                </button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setDraft(null)}>
                  Cancel
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
