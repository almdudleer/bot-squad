import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, DocSummary, DocDetail, errorDetail } from "../api";
import { PageHelp } from "../components/PageHelp";
import { Markdown } from "../components/Markdown";

export function Docs() {
  const { slug = "" } = useParams();
  const [searchParams] = useSearchParams();

  const [items, setItems] = useState<DocSummary[] | null>(null);
  const [categories, setCategories] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocDetail | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  // + New doc form
  const [newCategory, setNewCategory] = useState("design");
  const [newTitle, setNewTitle] = useState("");
  const [newParent, setNewParent] = useState(""); // T-0235: optional mother doc
  const [showNew, setShowNew] = useState(false);

  // link-a-ticket input
  const [linkTicket, setLinkTicket] = useState("");

  function reload() {
    api.docs(slug).then(setItems).catch((e) => setError(String(e)));
  }

  useEffect(() => {
    reload();
    api.docCategories(slug).then(setCategories).catch(() => setCategories([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // Deep-link: /p/:slug/docs?doc=D-NNNN opens that doc (from ticket pages).
  useEffect(() => {
    const d = searchParams.get("doc");
    if (d && d !== selected) open(d);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, slug]);

  // T-0235: nested-docs tree. A doc is a ROOT (mother) when it has no
  // parent_doc_id (or its parent isn't in this set); children nest under their
  // mother regardless of their own category. Roots are grouped by category for
  // the left rail; descendants render indented beneath their mother.
  const byId = useMemo(() => {
    const m = new Map<string, DocSummary>();
    for (const d of items ?? []) m.set(d.id, d);
    return m;
  }, [items]);
  const childrenOf = useMemo(() => {
    const m = new Map<string, DocSummary[]>();
    for (const d of items ?? []) {
      const p = d.parent_doc_id;
      if (p && byId.has(p)) {
        if (!m.has(p)) m.set(p, []);
        m.get(p)!.push(d);
      }
    }
    return m;
  }, [items, byId]);
  const isRoot = (d: DocSummary) => !d.parent_doc_id || !byId.has(d.parent_doc_id);
  // Roots grouped by category (only roots head a category section; children
  // appear nested under their mother).
  const byCategory = useMemo(() => {
    const m = new Map<string, DocSummary[]>();
    for (const d of items ?? []) {
      if (!isRoot(d)) continue;
      if (!m.has(d.category)) m.set(d.category, []);
      m.get(d.category)!.push(d);
    }
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, byId]);

  // Descendant set of a doc (self + all nested children) — used to keep the
  // "attach under" picker from offering a cycle (backend rejects too).
  function descendantsOf(id: string): Set<string> {
    const out = new Set<string>([id]);
    const stack = [id];
    while (stack.length) {
      const cur = stack.pop()!;
      for (const c of childrenOf.get(cur) ?? []) {
        if (!out.has(c.id)) {
          out.add(c.id);
          stack.push(c.id);
        }
      }
    }
    return out;
  }

  async function setParent(childId: string, parentId: string | null) {
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      await api.setDocParent(slug, childId, parentId);
      reload();
      if (selected) await open(selected);
      setFlash(parentId ? `Attached ${childId} under ${parentId}.` : `Detached ${childId}.`);
    } catch (e) {
      setError(String(e));
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
      reload();
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
  // (window.confirm gate, as in Users.tsx) — no new are-you-sure modal. The BE
  // refuses (409) a mother doc that still has children; surface that detail
  // (which names the blocking children) inline via errorDetail.
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
      reload();
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
      reload();
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
      reload();
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
      reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // T-0235: render a doc + its nested children (recursive, indented).
  function renderDocNode(d: DocSummary, level: number) {
    const kids = childrenOf.get(d.id) ?? [];
    return (
      <li key={d.id} className="mb-1" style={{ paddingLeft: level > 0 ? "0.7rem" : 0 }}>
        <button
          type="button"
          className={`btn btn-sm w-100 text-start ${selected === d.id ? "btn-secondary" : "btn-outline-secondary"}`}
          style={{ fontSize: "0.74rem" }}
          onClick={() => open(d.id)}
        >
          <span style={{ fontFamily: "var(--mc-mono)" }}>
            {level > 0 && <span style={{ color: "var(--mc-text-dim)" }}>└ </span>}
            {d.id}
          </span>
          {kids.length > 0 && (
            <span style={{ color: "var(--mc-text-dim)", fontSize: "0.68rem" }} title={`${kids.length} attached`}>
              {" "}📎{kids.length}
            </span>
          )}
          <br />
          <span style={{ color: "var(--mc-text-dim)", fontSize: "0.7rem" }}>{d.title}</span>
        </button>
        {kids.length > 0 && (
          <ul className="list-unstyled m-0 mt-1">{kids.map((k) => renderDocNode(k, level + 1))}</ul>
        )}
      </li>
    );
  }

  return (
    <div className="container py-4" style={{ maxWidth: "980px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Docs
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        Project docs stored as <code>data/&lt;slug&gt;/docs/&lt;category&gt;/D-NNNN-&lt;slug&gt;.md</code> and
        symlinked into the dev/master clones (<code>bsq docs sync</code>) so agents reach them at
        <code> docs/&lt;category&gt;/…</code>. Categories: <strong>product</strong> /
        <strong> architecture</strong> (fixed) / <strong>design</strong> (blueprints) /
        <strong> support</strong> (agent-facing) / <strong>runbook</strong>. Docs link
        bidirectionally to tickets (T-0172).
      </PageHelp>

      {error && <div className="alert alert-danger py-1 small">{error}</div>}
      {flash && <div className="alert alert-success py-1 small">{flash}</div>}

      <div className="d-flex gap-4">
        {/* List, grouped by category */}
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
              {/* T-0235: optionally attach the new doc under a mother doc. */}
              <label className="form-label" style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)" }}>Attach under (optional)</label>
              <select className="form-select form-select-sm mb-2" style={{ fontSize: "0.74rem" }} value={newParent} onChange={(e) => setNewParent(e.target.value)}>
                <option value="">— none (mother doc) —</option>
                {(items ?? []).map((d) => <option key={d.id} value={d.id}>{d.id} · {d.title}</option>)}
              </select>
              <button type="button" className="btn btn-primary btn-sm w-100" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={createDoc}>
                {busy ? "Creating…" : "Create"}
              </button>
            </div>
          )}
          {items === null && !error && <div className="mc-loading">Loading</div>}
          {items?.length === 0 && <div className="text-muted small">No docs yet.</div>}
          {[...byCategory.keys()].sort().map((cat) => (
            <div key={cat} className="mb-2">
              <div className="mc-sidebar-subsection" style={{ marginTop: 0 }}>{cat}</div>
              <ul className="list-unstyled m-0">
                {byCategory.get(cat)!.map((d) => renderDocNode(d, 0))}
              </ul>
            </div>
          ))}
        </div>

        {/* Detail / editor */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {selected === null && <div className="text-muted small">Select a doc, or create one.</div>}

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

              {/* T-0235: nesting — mother (parent) + attached artifacts (children)
                  + adopt/disown controls (PUT /docs/{id}/parent). */}
              <div className="mb-3" style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.6rem" }}>
                <div className="mc-section-title" style={{ margin: "0 0 0.4rem 0" }}>Nesting</div>
                {detail.parent_doc_id ? (
                  <div className="d-flex align-items-center gap-2 mb-2" style={{ fontSize: "0.74rem" }}>
                    <span style={{ color: "var(--mc-text-dim)" }}>Attached under</span>
                    <button type="button" className="btn btn-link btn-sm p-0" style={{ fontFamily: "var(--mc-mono)" }} onClick={() => open(detail.parent_doc_id!)}>
                      {detail.parent_doc_id}
                    </button>
                    <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.7rem" }} disabled={busy} onClick={() => setParent(detail.id, null)}>
                      Detach
                    </button>
                  </div>
                ) : (
                  <div className="mb-2" style={{ fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>
                    Mother doc (no parent).
                  </div>
                )}
                {(detail.child_doc_ids ?? []).length > 0 && (
                  <div className="mb-2">
                    <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginBottom: "0.25rem" }}>
                      Attached artifacts ({detail.child_doc_ids!.length}):
                    </div>
                    <div className="d-flex flex-wrap gap-2">
                      {detail.child_doc_ids!.map((cid) => (
                        <button key={cid} type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }} onClick={() => open(cid)}>
                          📎 {cid}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                <select
                  className="form-select form-select-sm"
                  style={{ fontSize: "0.74rem", maxWidth: "22rem" }}
                  value=""
                  disabled={busy}
                  aria-label="Attach this doc under a mother doc"
                  onChange={(e) => { if (e.target.value) setParent(detail.id, e.target.value); }}
                >
                  <option value="">Attach under…</option>
                  {(items ?? [])
                    .filter((d) => !descendantsOf(detail.id).has(d.id) && d.id !== detail.parent_doc_id)
                    .map((d) => (
                      <option key={d.id} value={d.id}>{d.id} · {d.title}</option>
                    ))}
                </select>
              </div>

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

              {/* T-0275: render the doc body as markdown (frontmatter stripped,
                  T-/D- mentions linkified) instead of a raw <pre> dump. */}
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
