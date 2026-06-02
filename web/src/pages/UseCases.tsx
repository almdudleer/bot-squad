import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, UseCaseSummary, UseCaseDetail } from "../api";
import { PageHelp } from "../components/PageHelp";

const NEW_TEMPLATE = `---
id: UC-new-flow
title: New flow
user_persona: <who is doing this — role + context>
goal: <one sentence: what they're trying to accomplish>
preconditions: <state / data / auth that must exist first>
success_criteria: <observable outcome that means it worked>
related_tickets: <T-NNNN>
status: active
---

# New flow

## Steps
1. <action>
2. <action>

## Feedback
`;

export function UseCases() {
  const { slug = "" } = useParams();

  const [items, setItems] = useState<UseCaseSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<UseCaseDetail | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  function reload() {
    api.useCases(slug).then(setItems).catch((e) => setError(String(e)));
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  async function open(id: string) {
    setSelected(id);
    setDetail(null);
    setDraft(null);
    setFlash(null);
    try {
      setDetail(await api.useCase(slug, id));
    } catch (e) {
      setError(String(e));
    }
  }

  function startNew() {
    setSelected("");
    setDetail(null);
    setFlash(null);
    setDraft(NEW_TEMPLATE);
  }

  function idFromDraft(text: string): string {
    const m = text.match(/^id:\s*(.+)$/m);
    return m ? m[1].trim() : "";
  }

  async function save() {
    const isNew = selected === "";
    const id = isNew ? idFromDraft(draft ?? "") : (selected ?? "");
    if (!id) { setError("Set an `id:` in the frontmatter first."); return; }
    const content = draft ?? "";
    setBusy(true);
    setError(null);
    try {
      await api.putUseCase(slug, id, content);
      setDraft(null);
      reload();
      await open(id);
      setFlash("Saved.");
    } catch (e) {
      setError(String(e));
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

  return (
    <div className="container py-4" style={{ maxWidth: "980px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Use cases
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        User flows stored as <code>data/&lt;slug&gt;/use_cases/&lt;id&gt;.md</code>. Pick one to
        view/edit; <strong>Run</strong> spawns a testing-dev session pre-briefed on the flow
        (playwright, manual-first per T-0158). Feedback submitted with
        <code>bsq feedback submit --usecase &lt;id&gt;</code> attaches to its <code>## Feedback</code>
        section and is replayed into the next run.
      </PageHelp>

      {error && <div className="alert alert-danger py-1 small">{error}</div>}
      {flash && <div className="alert alert-success py-1 small">{flash}</div>}

      <div className="d-flex gap-4">
        {/* List */}
        <div style={{ minWidth: "240px", flex: "0 0 240px" }}>
          <button type="button" className="btn btn-outline-primary btn-sm w-100 mb-2" style={{ fontSize: "0.72rem" }} onClick={startNew}>
            + New use case
          </button>
          {items === null && !error && <div className="mc-loading">Loading</div>}
          {items?.length === 0 && <div className="text-muted small">No use cases yet.</div>}
          <ul className="list-unstyled m-0">
            {items?.map((uc) => (
              <li key={uc.id} className="mb-1">
                <button
                  type="button"
                  className={`btn btn-sm w-100 text-start ${selected === uc.id ? "btn-secondary" : "btn-outline-secondary"}`}
                  style={{ fontSize: "0.74rem" }}
                  onClick={() => open(uc.id)}
                >
                  <span style={{ fontFamily: "var(--mc-mono)" }}>{uc.id}</span>
                  <br />
                  <span style={{ color: "var(--mc-text-dim)", fontSize: "0.7rem" }}>{uc.title}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        {/* Detail / editor */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {selected === null && <div className="text-muted small">Select a use case, or create one.</div>}

          {selected !== null && draft === null && detail && (
            <>
              <div className="d-flex justify-content-between align-items-center mb-2">
                <div style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", fontWeight: 600 }}>{detail.id}</div>
                <div className="d-flex gap-2">
                  <button type="button" className="btn btn-outline-secondary btn-sm" style={{ fontSize: "0.72rem" }} onClick={() => setDraft(detail.raw)}>
                    Edit
                  </button>
                  <button type="button" className="btn btn-primary btn-sm" style={{ fontSize: "0.72rem" }} disabled={busy} onClick={() => run(detail.id)}>
                    {busy ? "Running…" : "Run test"}
                  </button>
                </div>
              </div>
              <pre className="mc-pre">{detail.raw}</pre>
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
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => { setDraft(null); if (selected === "") setSelected(null); }}>
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
