import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ConstantTeam, SessionRow, Task, VisionFile } from "../api";
import { PageHelp } from "../components/PageHelp";
import { CANONICAL_LABELS, canonicalOf } from "../canonicalStatus";
import { constantTeamFor, constantTeamMembers, unmatchedConstantTeams } from "../constantTeams";
import { sortBoundTasks, splitDone, tasksForInitiative } from "../initiativeTasks";

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

// T-0295 (a): the ENDING-vs-PERSISTENT kind badge, per the paradigm reframe
// (Pillar B) — "initiatives can be ending or persistent". EVERY initiative row
// gets exactly one badge: before this, only persistent rows were badged, so a
// missing badge was ambiguous between "ends" and "the flag never reached the
// FE" (which post-T-0480 it usually hadn't). PERSISTENT ⇒ a standing
// responsibility with no finished state; ENDING ⇒ it ships and closes.
// Kind is decided server-side (routes_constant_teams / routes_vision
// `_initiative_kind`), which is also where the legacy `constant_team:` flag is
// folded in; the FE only maps kind → label.
export function initiativeKindBadge(
  f: Pick<VisionFile, "initiative_kind"> | undefined | null,
): { label: "PERSISTENT" | "ENDING"; cls: string; title: string } {
  if (isPersistentInitiative(f)) {
    return {
      label: "PERSISTENT",
      cls: "mc-badge mc-badge-info",
      title:
        "Persistent initiative — a standing responsibility staffed by a constant team; it doesn't reach a 'finished' state.",
    };
  }
  return {
    label: "ENDING",
    cls: "mc-badge mc-badge-dim",
    title: "Ending initiative — scoped work that ships and then finishes.",
  };
}

// T-0295 (b): "3/1 members" needs the qualifier that 3 > cap. Pure so the
// over-cap wording is testable without a browser.
export function memberCountLabel(live: number, teamSize: number | null): string {
  if (teamSize === null || teamSize === undefined) return `${live} member(s)`;
  const suffix = live > teamSize ? " (over cap)" : "";
  return `${live}/${teamSize} members${suffix}`;
}

export function Vision() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [files, setFiles] = useState<VisionFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  // T-0295 (b)+(c): constant-team tick state and the backlog, so an expanded
  // initiative can show its team health and the tasks bound to it.
  const [teams, setTeams] = useState<ConstantTeam[]>([]);
  const [tasks, setTasks] = useState<Task[] | null>(null);

  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  // T-0295 (c): per-initiative "show the done ones too" toggle.
  const [doneExpanded, setDoneExpanded] = useState<Record<string, boolean>>({});

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
    // T-0295: both are supporting detail for the expanded row — a failure here
    // must not blank the initiative list, so each degrades to its own empty
    // state (the detail says "unavailable" rather than "none").
    api
      .constantTeams(slug)
      .then((r) => setTeams(r.teams ?? []))
      .catch(() => setTeams([]));
    api
      .backlog(slug)
      .then(setTasks)
      .catch(() => setTasks([]));
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

  // T-0295 (b): tick state with no initiative row to hang off (archived or
  // retired team) — rendered in its own block at the bottom.
  const orphanTeams = unmatchedConstantTeams(
    teams,
    initiatives.map((f) => f.name),
  );

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

  const dimMono = {
    fontFamily: "var(--mc-mono)",
    fontSize: "0.65rem",
    color: "var(--mc-text-dim)",
  } as const;

  const detailBlock = {
    border: "1px solid var(--mc-border, #444)",
    borderRadius: "2px",
    padding: "6px 8px",
    marginBottom: "8px",
  } as const;

  function detailHeading(text: string) {
    return (
      <div
        style={{
          ...dimMono,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          marginBottom: "4px",
        }}
      >
        {text}
      </div>
    );
  }

  // T-0295 (b): constant-team live health for one initiative row — member count
  // vs team_size, queue depth, last spawn, consume source. Rendered ONLY for
  // persistent rows and rows that actually have tick config: an ending
  // initiative has no team to be healthy or unhealthy.
  function renderTeamHealth(f: VisionFile, base: string) {
    const team = constantTeamFor(teams, base);
    if (!team && !isPersistentInitiative(f)) return null;
    if (!team) {
      // The honest answer for a persistent initiative the tick can't see. This
      // is EVERY task-backed persistent initiative today: T-0480 moved
      // initiatives into backlog tasks and the constant-team tick still reads
      // vision/initiatives/*.md. Saying "0 members" here would imply a team
      // that is merely empty, rather than one that was never configured.
      return (
        <div style={detailBlock}>
          {detailHeading("Constant-team health")}
          <div style={{ ...dimMono, fontSize: "0.7rem" }}>
            No constant-team tick config for <code>{base}</code> — the worker tick
            reads <code>vision/initiatives/&lt;stem&gt;.md</code> with{" "}
            <code>constant_team: true</code>. This initiative is not staffed by
            the constant-team tick.
          </div>
        </div>
      );
    }
    const members = constantTeamMembers(sessions, team);
    return (
      <div style={detailBlock}>
        {detailHeading("Constant-team health")}
        <div className="d-flex gap-3 flex-wrap align-items-center" style={{ fontFamily: "var(--mc-mono)", fontSize: "0.7rem" }}>
          <span title="live (active/paused) sessions bound to this team vs its team_size cap">
            👥 {memberCountLabel(members.length, team.team_size)}
          </span>
          <span title={team.consume ? `consume source: ${team.consume}` : "no consume source configured"}>
            📥 queue {team.queue_depth ?? "—"}
          </span>
          <span title={team.last_spawn_iso ? `last spawn: ${team.last_spawn_iso}` : "this team has never spawned a member"}>
            ⏱ last spawn {team.last_spawn_iso ?? "never"}
          </span>
          <span style={dimMono} title={`state file: ${team.state_file}`}>
            {team.consume ? `${team.consume_kind}: ${team.consume}` : "no consume source"}
          </span>
        </div>
        {!team.staffable && team.not_staffable_reason && (
          <div style={{ ...dimMono, fontSize: "0.7rem", marginTop: "4px" }}>
            ⚠ not staffed — {team.not_staffable_reason}
          </div>
        )}
        {members.length > 0 && (
          <div className="d-flex gap-2 flex-wrap" style={{ marginTop: "4px" }}>
            {members.map((s) => (
              <span
                key={s.sid}
                style={{ ...dimMono, cursor: "pointer" }}
                title={`member: ${s.sid} (${s.status})`}
                onClick={() =>
                  navigate(`/p/${slug}/sessions?sid=${encodeURIComponent(s.sid)}`)
                }
              >
                ● {s.window || s.sid}
              </span>
            ))}
          </div>
        )}
      </div>
    );
  }

  // T-0295 (c): the tasks carrying `initiative: <this>` — the same query the
  // Board's group-by-initiative lane uses (shared `tasksForInitiative`), so the
  // two surfaces can't disagree about what work is bound here.
  function renderBoundTasks(base: string) {
    if (tasks === null) {
      return (
        <div style={detailBlock}>
          {detailHeading("Bound tasks")}
          <div style={{ ...dimMono, fontSize: "0.7rem" }}>Loading</div>
        </div>
      );
    }
    const bound = tasksForInitiative(tasks, base);
    // Live work first, done behind an expander (T-0295 walkthrough finding: a
    // mature initiative is ~95% closed, so an id-ordered list opens with 128
    // DONE rows and buries both the live work and the initiative body). Same
    // rationale as the Board's thin/expandable Closed column (T-0058) —
    // nothing is dropped, only folded.
    const { live, done } = splitDone(bound);
    const doneOpen = !!doneExpanded[base];
    return (
      <div style={detailBlock}>
        {detailHeading(`Bound tasks (${bound.length})`)}
        {bound.length === 0 ? (
          <div style={{ ...dimMono, fontSize: "0.7rem" }}>
            No tasks carry <code>initiative: {base}</code>.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
            {live.length === 0 && (
              <div style={{ ...dimMono, fontSize: "0.7rem" }}>
                No live work — every bound task is done.
              </div>
            )}
            {sortBoundTasks(live).map(renderBoundTaskRow)}
            {done.length > 0 && (
              <button
                type="button"
                className="btn btn-link p-0"
                style={{ ...dimMono, fontSize: "0.65rem", textAlign: "left" }}
                onClick={() => setDoneExpanded((s) => ({ ...s, [base]: !s[base] }))}
              >
                {doneOpen ? "▾" : "▸"} {done.length} done
              </button>
            )}
            {doneOpen && sortBoundTasks(done).map(renderBoundTaskRow)}
          </div>
        )}
      </div>
    );
  }

  function renderBoundTaskRow(t: Task) {
    return (
      <div
        key={t.id}
        className="d-flex gap-2 align-items-baseline"
        style={{ fontFamily: "var(--mc-mono)", fontSize: "0.7rem", cursor: "pointer" }}
        onClick={() => navigate(`/p/${slug}/t/${t.id}`)}
        title={`${t.id} — ${t.status}`}
      >
        <span style={{ color: "var(--mc-text-dim)" }}>{t.id}</span>
        <span
          className="mc-badge mc-badge-dim"
          style={{ fontSize: "0.55rem" }}
          title={`internal status: ${t.status}`}
        >
          {CANONICAL_LABELS[canonicalOf(t.status)]} · {t.status}
        </span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {t.title}
        </span>
      </div>
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
                    staffing kill-switch isn't operating off-screen.
                    T-0295 (a): badge BOTH kinds — ending initiatives were
                    previously unlabelled, which read as "unknown". */}
                {(() => {
                  const badge = initiativeKindBadge(f);
                  return (
                    <span className={badge.cls} style={{ fontSize: "0.6rem" }} title={badge.title}>
                      {badge.label}
                    </span>
                  );
                })()}
                <div className="d-flex gap-2 align-items-center flex-wrap">
                  {opts.kind === "active" && renderTlBindings(base)}
                </div>
              </div>
              {isOpen && (
                <>
                  {renderTeamHealth(f, base)}
                  {renderBoundTasks(base)}
                  {renderFileBody(f)}
                </>
              )}
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
        Each row is badged <strong>ENDING</strong> (ships, then finishes) or{" "}
        <strong>PERSISTENT</strong> (a standing responsibility). Expand a row
        for the tasks bound to it, and — for persistent ones — its
        constant-team health.
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

      {/* T-0295 (b): constant-team tick state on disk that maps to no
          initiative row above — a state file whose initiative was archived, or
          a retired team. Listing it is the point: an unreferenced json under
          _worker/constant_teams/ that no surface mentions is exactly the blind
          spot this endpoint was added to close. */}
      {orphanTeams.length > 0 && (
        <section className="mb-4">
          <div
            style={{
              ...dimMono,
              fontSize: "0.72rem",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}
            className="mb-2"
          >
            Constant-team tick state without an initiative ({orphanTeams.length})
          </div>
          {orphanTeams.map((t) => (
            <div
              key={t.state_file}
              className="d-flex gap-3 flex-wrap align-items-center py-1"
              style={{ fontFamily: "var(--mc-mono)", fontSize: "0.7rem", borderTop: "1px solid var(--mc-border, #444)" }}
            >
              <span>{t.name}</span>
              {t.retired && (
                <span className="mc-badge mc-badge-warn" style={{ fontSize: "0.55rem" }} title="code-level denylist — the tick refuses to staff this team">
                  RETIRED
                </span>
              )}
              <span style={dimMono}>⏱ last spawn {t.last_spawn_iso ?? "never"}</span>
              <span style={dimMono} title={`state file: ${t.state_file}`}>{t.state_file}</span>
              {t.not_staffable_reason && <span style={dimMono}>⚠ {t.not_staffable_reason}</span>}
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
