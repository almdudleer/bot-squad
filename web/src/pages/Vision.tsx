import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, SessionRow, VisionFile } from "../api";
import { Modal } from "../components/Modal";

import { PageHelp } from "../components/PageHelp";
interface EditState {
  name: string;
  draft: string;
}

export function Vision() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [files, setFiles] = useState<VisionFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionRow[]>([]);

  const [editing, setEditing] = useState<EditState | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  // Deep-link entry: /p/<slug>/vision#<basename.md> from Sessions page.
  // Auto-expand the matching initiative and scroll it into view once the
  // initiative list has loaded.
  useEffect(() => {
    if (!files || !location.hash) return;
    const target = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (!target) return;
    const match = files.find(
      (f) => f.name === target || f.name.endsWith(`/${target}`),
    );
    if (!match) return;
    setExpanded((s) => ({ ...s, [match.name]: true }));
    // Defer scroll to next tick so the expand has rendered the body.
    requestAnimationFrame(() => {
      const el = document.getElementById(`init-${target}`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, [files, location.hash]);

  // New initiative modal
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState("");
  const [newContent, setNewContent] = useState("");
  const [newError, setNewError] = useState<string | null>(null);
  const [newSaving, setNewSaving] = useState(false);

  function reload() {
    api
      .vision(slug)
      .then((list) => {
        // constitution.md lives in the WORKFLOW tab. _archive/* never comes
        // back from the API (filtered server-side). north-star/strategy/
        // tactical were moved into _archive 2026-05-12.
        setFiles(list.filter((f) => f.name !== "constitution.md"));
      })
      .catch((e) => setError(String(e)));
    // Phase 6: pull active TLs so we can hide the "Start teamlead" button
    // for initiatives that already have one bound. Silent on error — the
    // button will just appear for everything if the call fails.
    api
      .sessions(slug)
      .then(setSessions)
      .catch(() => setSessions([]));
  }

  useEffect(() => {
    reload();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  function startEdit(f: VisionFile) {
    setEditing({ name: f.name, draft: f.content });
    setSaveError(null);
  }

  function cancelEdit() {
    setEditing(null);
    setSaveError(null);
  }

  async function saveEdit() {
    if (!editing) return;
    setSaving(true);
    setSaveError(null);
    try {
      await api.putVision(slug, editing.name, editing.draft);
      setEditing(null);
      reload();
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function activate(f: VisionFile) {
    const base = f.name.replace(/^initiatives\//, "");
    try {
      await api.activateInitiative(slug, base);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  async function deactivate(f: VisionFile) {
    const base = f.name.replace(/^initiatives\//, "");
    try {
      await api.deactivateInitiative(slug, base);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  async function markFinished(f: VisionFile) {
    const base = f.name.replace(/^initiatives\//, "");
    try {
      await api.markInitiativeFinished(slug, base);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  async function reopen(f: VisionFile) {
    const base = f.name.replace(/^initiatives\//, "");
    try {
      await api.reopenInitiative(slug, base);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  async function createInitiative() {
    if (!newName.trim()) { setNewError("Name is required"); return; }
    setNewSaving(true);
    setNewError(null);
    try {
      await api.newInitiative(slug, newName.trim(), newContent || `# ${newName.trim()}\n`);
      setShowNew(false);
      setNewName("");
      setNewContent("");
      reload();
    } catch (e) {
      setNewError(String(e));
    } finally {
      setNewSaving(false);
    }
  }

  const product = files?.find((f) => f.name === "product.md") ?? null;
  const initiatives = (files ?? []).filter((f) => f.name.startsWith("initiatives/"));
  const activeInitiatives = initiatives.filter((f) => f.active && !f.finished);
  const finishedInitiatives = initiatives
    .filter((f) => f.finished)
    .filter((f) => !f.name.endsWith("/_TEMPLATE.md"));
  const inactive = initiatives
    .filter((f) => !f.active && !f.finished)
    .filter((f) => !f.name.endsWith("/_TEMPLATE.md"));

  // For each initiative basename, the list of bound active TL sessions.
  // A TL is a session with no task_id and status === "active". A TL is
  // bound to initiative X iff X is in {primary} ∪ extra_initiatives.
  // Phase 9: multi-binding — extras are usually empty but a TL can hold many.
  const tlsByInitiative = new Map<string, SessionRow[]>();
  const activeTls: SessionRow[] = [];
  for (const s of sessions) {
    if (s.status !== "active") continue;
    const tid = (s.task_id ?? "").trim();
    if (tid && tid !== "~") continue;   // dev session, not a TL
    activeTls.push(s);
    const bound: string[] = [];
    const init = (s.initiative ?? "").trim();
    if (init && init !== "~") bound.push(init);
    for (const e of s.extra_initiatives ?? []) {
      const cleaned = e.trim();
      if (cleaned && cleaned !== "~") bound.push(cleaned);
    }
    for (const b of bound) {
      const list = tlsByInitiative.get(b) ?? [];
      list.push(s);
      tlsByInitiative.set(b, list);
    }
  }

  async function bindInitiativeTo(sid: string, base: string) {
    try {
      await api.bindInitiative(slug, sid, base);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  async function unbindInitiativeFrom(sid: string, base: string, windowName: string) {
    // Cheap confirm — destructive enough that we don't want one-click-oops,
    // but no need for a full modal.
    if (!window.confirm(`Unbind ${base} from ${windowName || sid}?`)) return;
    try {
      await api.unbindInitiative(slug, sid, base);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  function startTeamleadFor(fileName: string) {
    const base = fileName.replace(/^initiatives\//, "");
    navigate(`/p/${slug}/sessions?role=teamlead&initiative=${encodeURIComponent(base)}`);
  }

  function renderFileBody(f: VisionFile) {
    if (editing?.name === f.name) {
      return (
        <>
          {saveError && <div className="alert alert-danger py-1 small">{saveError}</div>}
          <textarea
            className="form-control mb-2"
            rows={12}
            value={editing.draft}
            onChange={(e) => setEditing({ ...editing, draft: e.target.value })}
            autoFocus
          />
          <div className="d-flex gap-2">
            <button type="button" className="btn btn-primary btn-sm" onClick={saveEdit} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
            <button type="button" className="btn btn-secondary btn-sm" onClick={cancelEdit}>
              Cancel
            </button>
          </div>
        </>
      );
    }
    return <pre className="mc-pre">{f.content}</pre>;
  }

  function renderTlBindings(base: string) {
    const tls = tlsByInitiative.get(base) ?? [];
    if (tls.length === 0) {
      return (
        <span
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.65rem",
            color: "var(--mc-text-dim)",
            background: "var(--mc-surface-raised)",
            border: "1px dashed var(--mc-border)",
            borderRadius: "2px",
            padding: "0 4px",
          }}
        >
          no TL
        </span>
      );
    }
    return (
      <>
        {tls.map((s) => (
          <span
            key={s.sid}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "2px",
              fontFamily: "var(--mc-mono)",
              fontSize: "0.65rem",
              color: "var(--mc-accent-success, #4ade80)",
              background: "var(--mc-surface-raised)",
              border: "1px solid var(--mc-accent-success, #4ade80)",
              borderRadius: "2px",
              padding: "0 2px 0 4px",
            }}
            title={`bound TL: ${s.sid}`}
          >
            <span
              onClick={(e) => { e.stopPropagation(); navigate(`/p/${slug}/sessions`); }}
              style={{ cursor: "pointer" }}
            >
              ● {s.window}
            </span>
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); unbindInitiativeFrom(s.sid, base, s.window); }}
              title="Unbind this initiative from the teamlead"
              aria-label={`unbind ${base} from ${s.window}`}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--mc-text-dim)",
                cursor: "pointer",
                fontSize: "0.7rem",
                lineHeight: 1,
                padding: "0 2px",
              }}
            >
              ✕
            </button>
          </span>
        ))}
      </>
    );
  }

  function renderInitiativeGroup(opts: {
    label: string;
    items: VisionFile[];
    kind: "active" | "inactive" | "finished";
  }) {
    if (opts.items.length === 0) return null;
    return (
      <section className="mb-4">
        <div
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.72rem",
            color: "var(--mc-text-dim)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
          className="mb-2"
        >
          {opts.label}
        </div>
        {opts.items.map((f) => {
          const isOpen = !!expanded[f.name];
          const base = f.name.replace(/^initiatives\//, "");
          const covered = (tlsByInitiative.get(base) ?? []).length > 0;
          return (
            <div
              key={f.name}
              id={`init-${base}`}
              className="mb-2"
              style={{ borderTop: "1px solid var(--mc-border, #444)", scrollMarginTop: "1rem" }}
            >
              <div className="d-flex justify-content-between align-items-center py-2 gap-2 flex-wrap">
                <button
                  type="button"
                  className="btn btn-link p-0"
                  style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", textAlign: "left" }}
                  onClick={() => setExpanded((s) => ({ ...s, [f.name]: !s[f.name] }))}
                >
                  {isOpen ? "▾" : "▸"} {f.name}
                </button>
                <div className="d-flex gap-2 align-items-center flex-wrap">
                  {opts.kind === "active" && renderTlBindings(base)}
                  {opts.kind === "active" && !covered && (
                    <>
                      <button
                        type="button"
                        className="btn btn-outline-success btn-sm"
                        style={{ fontSize: "0.72rem" }}
                        title="Spawn a teamlead session bound to this initiative"
                        onClick={() => startTeamleadFor(f.name)}
                      >
                        Start teamlead
                      </button>
                      {activeTls.length > 0 && (
                        <select
                          className="form-select form-select-sm"
                          style={{ fontSize: "0.7rem", width: "auto", maxWidth: "160px" }}
                          defaultValue=""
                          title="Bind this initiative to an existing teamlead session"
                          onChange={(e) => {
                            const sid = e.target.value;
                            e.target.selectedIndex = 0;
                            if (sid) bindInitiativeTo(sid, base);
                          }}
                        >
                          <option value="" disabled>or bind ▾</option>
                          {activeTls.map((tl) => (
                            <option key={tl.sid} value={tl.sid}>{tl.window || tl.sid}</option>
                          ))}
                        </select>
                      )}
                    </>
                  )}
                  {opts.kind === "active" && (
                    <>
                      <button
                        type="button"
                        className="btn btn-outline-secondary btn-sm"
                        style={{ fontSize: "0.72rem" }}
                        onClick={() => deactivate(f)}
                      >
                        Deactivate
                      </button>
                      <button
                        type="button"
                        className="btn btn-outline-secondary btn-sm"
                        style={{ fontSize: "0.72rem" }}
                        onClick={() => markFinished(f)}
                      >
                        Mark finished
                      </button>
                    </>
                  )}
                  {opts.kind === "inactive" && (
                    <>
                      <button
                        type="button"
                        className="btn btn-outline-success btn-sm"
                        style={{ fontSize: "0.72rem" }}
                        onClick={() => activate(f)}
                      >
                        Activate
                      </button>
                      <button
                        type="button"
                        className="btn btn-outline-secondary btn-sm"
                        style={{ fontSize: "0.72rem" }}
                        onClick={() => markFinished(f)}
                      >
                        Mark finished
                      </button>
                    </>
                  )}
                  {opts.kind === "finished" && (
                    <button
                      type="button"
                      className="btn btn-outline-secondary btn-sm"
                      style={{ fontSize: "0.72rem" }}
                      onClick={() => reopen(f)}
                    >
                      Reopen
                    </button>
                  )}
                  {isOpen && editing?.name !== f.name && (
                    <button
                      type="button"
                      className="btn btn-outline-secondary btn-sm"
                      style={{ fontSize: "0.72rem" }}
                      onClick={() => startEdit(f)}
                    >
                      Edit
                    </button>
                  )}
                </div>
              </div>
              {isOpen && renderFileBody(f)}
            </div>
          );
        })}
      </section>
    );
  }

  function fileHeader(f: VisionFile, opts: { rightSlot?: React.ReactNode } = {}) {
    return (
      <div className="d-flex justify-content-between align-items-center mb-2">
        <div
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.72rem",
            color: "var(--mc-text-dim)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
        >
          {f.name}
        </div>
        <div className="d-flex gap-2 align-items-center">
          {opts.rightSlot}
          {editing?.name !== f.name && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.72rem" }}
              onClick={() => startEdit(f)}
            >
              Edit
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="container py-4" style={{ maxWidth: "860px" }}>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
          Roadmap
          <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
        </h2>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={() => { setShowNew(true); setNewError(null); }}
        >
          + New initiative
        </button>
      </div>

      <PageHelp>
        Short product description plus the project&apos;s initiatives. Any
        number can be <strong>active</strong> at once — each active initiative
        gets at most one bound teamlead session (its dev workers cascade from
        that TL). Inactive initiatives are not piped into agent context.
        Edit constitution + roles in the <strong>Workflow</strong> tab.
      </PageHelp>

      {error && <div className="alert alert-danger">{error}</div>}
      {files === null && !error && <div className="mc-loading">Loading</div>}

      {/* Product description */}
      {product && (
        <section className="mb-4">
          {fileHeader(product)}
          {renderFileBody(product)}
        </section>
      )}

      {renderInitiativeGroup({
        label: `Active initiatives (${activeInitiatives.length})`,
        items: activeInitiatives,
        kind: "active",
      })}

      {renderInitiativeGroup({
        label: `Other initiatives (${inactive.length}) — hidden from agents`,
        items: inactive,
        kind: "inactive",
      })}

      {renderInitiativeGroup({
        label: `Finished initiatives (${finishedInitiatives.length})`,
        items: finishedInitiatives,
        kind: "finished",
      })}

      {/* New initiative modal */}
      <Modal
        open={showNew}
        title="New initiative"
        onClose={() => setShowNew(false)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setShowNew(false)}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={createInitiative} disabled={newSaving}>
              {newSaving ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {newError && <div className="alert alert-danger">{newError}</div>}
        <div className="mb-3">
          <label className="form-label">Name *</label>
          <input
            className="form-control"
            placeholder="e.g. API gateway rollout"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Initial content</label>
          <textarea
            className="form-control"
            rows={5}
            placeholder="(optional — defaults to a heading)"
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
          />
        </div>
      </Modal>
    </div>
  );
}
