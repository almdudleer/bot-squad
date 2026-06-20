import { useEffect, useState } from "react";
import { useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { api, FlowDetail, FlowSummary, UseCaseDetail, errorDetail } from "../api";
import { Mermaid, extractMermaid } from "../components/Mermaid";
import { FlowGraphEditor } from "../components/FlowGraphEditor";
import { Markdown } from "../components/Markdown";
import { ReparentControl } from "../components/ArtifactTree";
import { DocsOutletContext } from "./DocsSection";

// T-0337: UseCases is now the DETAIL pane of the unified "Docs & Artifacts"
// view. The shared cross-store tree + filter live in DocsSection; this page
// reads that tree from the Outlet context and renders only the use-case
// detail/editor (with its flows) on the right. Creating a use-case moved to
// DocsSection's unified "+ New". Deep-link (?uc=UC-NNNN) is unchanged.
export function UseCases() {
  const { slug = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { tree } = useOutletContext<DocsOutletContext>();

  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<UseCaseDetail | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  // T-0173: user flows attached to the selected use case.
  const [flows, setFlows] = useState<FlowSummary[] | null>(null);
  const [openFlowId, setOpenFlowId] = useState<string | null>(null);
  const [flowDetail, setFlowDetail] = useState<FlowDetail | null>(null);
  const [flowDraft, setFlowDraft] = useState<string | null>(null);
  // T-0226: structured node-graph editor mode (parallel to the raw-md editor).
  const [graphEditing, setGraphEditing] = useState(false);

  // Deep-link: /p/:slug/docs/usecases?uc=UC-NNNN opens that use case (from the
  // shared artifact tree's use-case nodes).
  useEffect(() => {
    const u = searchParams.get("uc");
    if (u && u !== selected) open(u);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, slug]);

  function reloadFlows(ucId: string) {
    api.flows(slug, ucId).then(setFlows).catch(() => setFlows([]));
  }

  async function open(id: string) {
    setSelected(id);
    setDetail(null);
    setDraft(null);
    setFlash(null);
    setFlows(null);
    setOpenFlowId(null);
    setFlowDetail(null);
    setFlowDraft(null);
    try {
      setDetail(await api.useCase(slug, id));
      reloadFlows(id);
    } catch (e) {
      setError(String(e));
    }
  }

  async function save() {
    const id = selected ?? "";
    if (!id) { setError("No use case selected."); return; }
    const content = draft ?? "";
    setBusy(true);
    setError(null);
    try {
      await api.putUseCase(slug, id, content);
      setDraft(null);
      tree.reload();
      await open(id);
      setFlash("Saved.");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // T-0276: delete a use case (the BE cascades its owned flows subtree).
  // Matches the app's destructive-action pattern (window.confirm gate, as in
  // Users.tsx) — no new are-you-sure modal. UC-NNNN is tombstoned, not reused.
  async function removeUseCase() {
    if (!selected) return;
    const id = selected;
    if (!window.confirm(`Delete ${id} and its flows? This cannot be undone (the id is retired, not reused).`)) return;
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      await api.deleteUseCase(slug, id);
      setSelected(null);
      setDetail(null);
      setDraft(null);
      setFlows(null);
      setOpenFlowId(null);
      setFlowDetail(null);
      setFlowDraft(null);
      tree.reload();
      setFlash(`Deleted ${id}.`);
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }

  // T-0283/D-0029: re-parent this use-case under any artifact (cross-store,
  // cycle-safe on the BE; surface a 4xx cycle/validation detail inline).
  async function setUcParent(parentId: string | null) {
    if (!selected) return;
    const id = selected;
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      await api.setUseCaseParent(slug, id, parentId);
      tree.reload();
      await open(id);
      setFlash(parentId ? `Attached ${id} under ${parentId}.` : `Detached ${id}.`);
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }

  async function run(id: string) {
    setBusy(true);
    setError(null);
    setFlash(null);
    try {
      const res = await api.runUseCase(slug, id);
      setFlash(`Launched testing session${res.sid ? ` ${res.sid}` : ""} (window ${res.window}).`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // ---- flows ----
  async function openFlow(flowId: string) {
    if (!selected) return;
    setOpenFlowId(flowId);
    setFlowDetail(null);
    setFlowDraft(null);
    setGraphEditing(false);
    try {
      setFlowDetail(await api.flow(slug, selected, flowId));
    } catch (e) {
      setError(String(e));
    }
  }

  async function startNewFlow() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.createFlow(slug, selected, "New flow");
      reloadFlows(selected);
      const fd = await api.flow(slug, selected, res.id);
      setOpenFlowId(res.id);
      setFlowDetail(fd);
      setFlowDraft(fd.raw);
      setFlash(`Created flow ${res.id} — edit steps + mermaid, then Save.`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveFlow() {
    if (!selected || !openFlowId) return;
    setBusy(true);
    setError(null);
    try {
      await api.putFlow(slug, selected, openFlowId, flowDraft ?? "");
      setFlowDraft(null);
      reloadFlows(selected);
      setFlowDetail(await api.flow(slug, selected, openFlowId));
      setFlash("Flow saved.");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // T-0226: persist the structured graph — the editor already serialized it
  // into the flow md (frontmatter graph: + derived mermaid); we just PUT raw.
  async function saveFlowGraph(newRaw: string) {
    if (!selected || !openFlowId) return;
    setBusy(true);
    setError(null);
    try {
      await api.putFlow(slug, selected, openFlowId, newRaw);
      setGraphEditing(false);
      reloadFlows(selected);
      setFlowDetail(await api.flow(slug, selected, openFlowId));
      setFlash("Flow graph saved — mermaid regenerated.");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const mermaidSrc = flowDetail ? extractMermaid(flowDetail.body) : null;

  return (
    <>
      {error && <div className="alert alert-danger py-1 small">{error}</div>}
      {flash && <div className="alert alert-success py-1 small">{flash}</div>}

      {selected === null && <div className="text-muted small">Select a use case, or create one with + New.</div>}

      {selected !== null && draft === null && detail && (
        <>
          <div className="d-flex justify-content-between align-items-center mb-2">
            <div style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", fontWeight: 600 }}>
              <span title="use case" style={{ marginRight: "0.3rem" }}>🎯</span>
              {detail.id}
            </div>
            <div className="d-flex gap-2">
              <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem" }} onClick={() => setDraft(detail.raw)}>
                Edit
              </button>
              <button type="button" className="btn btn-primary btn-sm" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={() => run(detail.id)}>
                {busy ? "Running…" : "Run test"}
              </button>
              <button type="button" className="btn btn-outline-danger btn-sm" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={removeUseCase}>
                Delete
              </button>
            </div>
          </div>
          {/* T-0283/D-0029: cross-store nesting — set/clear this use-case's
              parent (PUT /use_cases/{id}/parent, cycle-safe) + list its
              attached artifacts (child_artifact_ids superset). */}
          <ReparentControl
            slug={slug}
            data={tree}
            selfId={detail.id}
            currentParentId={detail.parent_doc_id}
            busy={busy}
            onSetParent={setUcParent}
          />
          {(() => {
            const childIds = detail.child_artifact_ids ?? detail.child_doc_ids ?? [];
            return childIds.length > 0 ? (
              <div className="mb-3">
                <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginBottom: "0.25rem" }}>
                  Attached artifacts ({childIds.length}):
                </div>
                <div className="d-flex flex-wrap gap-2">
                  {childIds.map((cid) => (
                    <span key={cid} className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }} title={tree.byId.get(cid)?.title ?? cid}>
                      📎 {cid}
                    </span>
                  ))}
                </div>
              </div>
            ) : null;
          })()}

          {/* T-0275: render the use-case body as markdown (frontmatter
              stripped, T-/D- mentions linkified) instead of a raw <pre>. */}
          <Markdown source={detail.raw} slug={slug} />

          {/* T-0173: attached user flows */}
          <div className="mt-4">
            <div className="d-flex justify-content-between align-items-center mb-2">
              <div className="mc-section-title" style={{ margin: 0 }}>
                User flows ({flows?.length ?? 0})
              </div>
              <button type="button" className="btn btn-outline-primary btn-sm" style={{ fontSize: "0.7rem" }} disabled={busy} onClick={startNewFlow}>
                + New flow
              </button>
            </div>
            {flows === null && <div className="mc-loading">Loading flows</div>}
            {flows?.length === 0 && (
              <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
                No flows yet. A flow is a numbered walkthrough + a mermaid diagram an agent can follow.
              </p>
            )}
            <div className="d-flex flex-wrap gap-2 mb-2">
              {flows?.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  className={`btn btn-sm ${openFlowId === f.id ? "btn-secondary" : "btn-outline-secondary"}`}
                  style={{ fontSize: "0.72rem" }}
                  onClick={() => openFlow(f.id)}
                >
                  <span style={{ fontFamily: "var(--mc-mono)" }}>{f.id}</span> · {f.title}
                </button>
              ))}
            </div>

            {openFlowId && flowDraft === null && !graphEditing && flowDetail && (
              <div style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.75rem" }}>
                <div className="d-flex justify-content-between align-items-center mb-2">
                  <div style={{ fontFamily: "var(--mc-mono)", fontSize: "0.76rem", fontWeight: 600 }}>
                    {flowDetail.id} — {flowDetail.title}
                    {flowDetail.graph && flowDetail.graph.nodes.length > 0 && (
                      <span className="badge ms-2" style={{ background: "var(--mc-accent, #2f6feb)", fontSize: "0.6rem" }}>
                        graph · {flowDetail.graph.nodes.length} nodes
                      </span>
                    )}
                  </div>
                  <div className="d-flex gap-2">
                    {/* T-0226: structured graph editor (mermaid derived). */}
                    <button type="button" className="btn btn-outline-primary btn-sm" style={{ fontSize: "0.7rem" }} onClick={() => setGraphEditing(true)}>
                      {flowDetail.graph && flowDetail.graph.nodes.length > 0 ? "Edit graph" : "Build graph"}
                    </button>
                    <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.7rem" }} onClick={() => setFlowDraft(flowDetail.raw)}>
                      Edit md
                    </button>
                  </div>
                </div>
                {mermaidSrc ? (
                  <Mermaid code={mermaidSrc} />
                ) : (
                  <div style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
                    (no <code>```mermaid</code> block in this flow yet)
                  </div>
                )}
                <pre className="mc-pre mt-2">{flowDetail.body}</pre>
              </div>
            )}

            {openFlowId && graphEditing && flowDetail && (
              <FlowGraphEditor
                initial={flowDetail.graph}
                rawMd={flowDetail.raw}
                busy={busy}
                onSave={saveFlowGraph}
                onCancel={() => setGraphEditing(false)}
              />
            )}

            {openFlowId && flowDraft !== null && (
              <div style={{ border: "1px solid var(--mc-border)", borderRadius: "4px", padding: "0.75rem" }}>
                <textarea
                  className="form-control mb-2"
                  rows={18}
                  value={flowDraft}
                  style={{ fontFamily: "var(--mc-mono)", fontSize: "0.76rem" }}
                  onChange={(e) => setFlowDraft(e.target.value)}
                  autoFocus
                />
                <div className="d-flex gap-2">
                  <button type="button" className="btn btn-primary btn-sm" onClick={saveFlow} disabled={busy}>
                    {busy ? "Saving…" : "Save flow"}
                  </button>
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => setFlowDraft(null)}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        </>
      )}

      {draft !== null && (
        <>
          <textarea
            className="form-control mb-2"
            rows={22}
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
    </>
  );
}
