import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, SessionRow, VisionFile } from "../api";
import { PageHelp } from "../components/PageHelp";

// T-0100: pure helper mirroring worker `_find_owner` (sessions.py). Used by
// the roadmap to decide which TL sessions are bound to which initiatives.
//
// Rules (must match _find_owner / _full_initiative_set in the worker):
//   - A TL is any session with no primary task_id (or task_id == "~").
//   - A TL is bound to initiative X iff X appears in primary `initiative`
//     OR in `extra_initiatives` (Phase 9 multi-binding).
//   - Status is NOT a filter: a paused/suspended TL still owns its bindings
//     (otherwise the roadmap claims "no TL" while the worker still routes
//     peer_send to it and blocks re-binding).
//   - Archived sessions ARE filtered: archived means "gone for real" — the
//     binding chip would be stale and misleading.
//
// Returns { tlsByInitiative, candidateTls } where candidateTls is the flat
// list of TLs (any status, unarchived) used to populate the bind dropdown.
export function computeTlBindings(sessions: SessionRow[]): {
  tlsByInitiative: Map<string, SessionRow[]>;
  candidateTls: SessionRow[];
} {
  const tlsByInitiative = new Map<string, SessionRow[]>();
  const candidateTls: SessionRow[] = [];
  for (const s of sessions) {
    if (s.archived) continue;
    const tid = (s.task_id ?? "").trim();
    if (tid && tid !== "~") continue;
    candidateTls.push(s);
    const bound: string[] = [];
    const init = (s.initiative ?? "").trim();
    if (init && init !== "~") bound.push(init);
    for (const e of s.extra_initiatives ?? []) {
      const cleaned = (e ?? "").trim();
      if (cleaned && cleaned !== "~") bound.push(cleaned);
    }
    for (const b of bound) {
      const list = tlsByInitiative.get(b) ?? [];
      list.push(s);
      tlsByInitiative.set(b, list);
    }
  }
  return { tlsByInitiative, candidateTls };
}

// T-0411 (PASS-2 P2-13): an initiative is a PERSISTENT constant-team job when
// its frontmatter carries `constant_team: <truthy>`. Parsed FE-side from the
// already-loaded VisionFile.content (no new endpoint) — mirrors the worker's
// `_truthy(fm.get("constant_team"))` (constant_teams.py).
// T-0428 (dogfood): the initiative rows rendered the RAW filename
// ("initiatives/multi-server-installation-process.md", CSS-uppercased) — a file
// path, not a name. Strip the "initiatives/" prefix + ".md", dash→space, and
// titlecase each word, preserving all-caps/numeric tokens (so "INI-01" stays
// "INI 01", not "Ini 01"). Display-only; the underlying f.name is unchanged.
export function initiativeDisplayName(name: string): string {
  const base = name.replace(/^initiatives\//, "").replace(/\.md$/, "");
  return base
    .split("-")
    .map((w) => (/^[A-Z0-9]+$/.test(w) ? w : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

// T-0354: initiative_kind: persistent|one-shot on the kind:initiative task
// backing this entry — replaces the T-0411 constant_team-in-body regex
// above (dead since T-0480 moved initiatives into backlog tasks: no
// kind:initiative task ever carried a constant_team line in its body, so
// this badge/control never fired). D-0059.
export function isPersistentInitiative(f: Pick<VisionFile, "initiative_kind"> | undefined | null): boolean {
  return f?.initiative_kind === "persistent";
}

export function Vision() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [files, setFiles] = useState<VisionFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionRow[]>([]);

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
    // Phase 6: pull TLs so the initiative rows can show which lead (if any)
    // each is bound to. Silent on error — the binding chip just won't show.
    api
      .sessions(slug)
      .then(setSessions)
      .catch(() => setSessions([]));
  }

  useEffect(() => {
    reload();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  const product = files?.find((f) => f.name === "product.md") ?? null;
  const initiatives = (files ?? []).filter((f) => f.name.startsWith("initiatives/"));
  const activeInitiatives = initiatives.filter((f) => f.active && !f.finished);
  const finishedInitiatives = initiatives
    .filter((f) => f.finished)
    .filter((f) => !f.name.endsWith("/_TEMPLATE.md"));
  const inactive = initiatives
    .filter((f) => !f.active && !f.finished)
    .filter((f) => !f.name.endsWith("/_TEMPLATE.md"));

  // T-0100: mirror the worker's `_find_owner` resolver so the roadmap's
  // notion of "who is the TL for initiative X" matches what `peer_send
  // to=teamlead` and `bind_initiative` would resolve to. The worker does
  // NOT filter by status — a paused/suspended TL still owns its bindings
  // (resurrect-able). The previous status==="active" filter caused the
  // "no TL" bug for any TL that wasn't currently running.
  const { tlsByInitiative } = computeTlBindings(sessions);

  function renderFileBody(f: VisionFile) {
    return <pre className="mc-pre">{f.content}</pre>;
  }

  // T-0709: this page is observability-only (D-0057 §8) — binding/unbinding
  // a lead is a write action and lives in TG/CLI (`bsq spawn --initiative`),
  // not here. This renders the current binding, read-only.
  function renderTlBindings(base: string) {
    const tls = tlsByInitiative.get(base) ?? [];
    if (tls.length === 0) {
      return (
        <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.65rem", color: "var(--mc-text-dim)" }}>
          no lead
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
              padding: "0 4px",
            }}
            title={`bound lead: ${s.sid}`}
          >
            <span
              onClick={(e) => {
                e.stopPropagation();
                // T-0099: deep-link to the bound TL row so /sessions
                // scrolls + highlights it instead of opening cold.
                navigate(`/p/${slug}/sessions?sid=${encodeURIComponent(s.sid)}`);
              }}
              style={{ cursor: "pointer" }}
            >
              ● {s.window}
            </span>
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
                  {isOpen ? "▾" : "▸"} {initiativeDisplayName(f.name)}
                </button>
                {/* T-0411: surface constant-team initiatives so item-16's
                    staffing kill-switch isn't operating off-screen. */}
                {isPersistentInitiative(f) && (
                  <span
                    className="mc-badge mc-badge-info"
                    style={{ fontSize: "0.6rem" }}
                    title="Persistent constant-team initiative — staffed continuously; it doesn't reach a normal 'finished' state."
                  >
                    PERSISTENT
                  </span>
                )}
                <div className="d-flex gap-2 align-items-center flex-wrap">
                  {opts.kind === "active" && renderTlBindings(base)}
                </div>
              </div>
              {isOpen && renderFileBody(f)}
            </div>
          );
        })}
      </section>
    );
  }

  function fileHeader(f: VisionFile) {
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
      </div>
    );
  }

  return (
    <div className="container py-4" style={{ maxWidth: "860px" }}>
      <div className="d-flex justify-content-between align-items-center mb-4">
        {/* T-0366 #3: title matches the "Vision" nav label (was "Roadmap"). */}
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
          Vision
          <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
        </h2>
      </div>

      <PageHelp>
        Short product description plus the project&apos;s initiatives. Any
        number can be <strong>active</strong> at once — each active initiative
        gets at most one bound teamlead session (its dev workers cascade from
        that TL). Inactive initiatives are not piped into agent context.
        Read-only observability view — creating/editing initiatives, binding
        a lead, and finishing/retiring/reopening are TG/CLI actions
        (<code>bsq initiative new</code>, <code>bsq spawn --initiative</code>,
        <code>bsq ticket update</code>), not controls on this page.
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
    </div>
  );
}
