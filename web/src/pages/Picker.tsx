import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, cachedProjects, Project } from "../api";
import { CopyableTmuxAttach } from "../components/CopyableTmuxAttach";
import { Modal } from "../components/Modal";
import { Coachmark, Typewriter, markSeen, useOnboardingStep } from "../onboarding";
import {
  deriveSlug,
  emptyWizardState,
  modeOptions,
  payloadFromWizard,
  validateWizard,
  type ProjectCreateMode,
  type ProjectCreateState,
} from "./projectCreateWizard";

// T-0052: post-create success surface. The wizard switches from the
// edit pane to this once the API has scaffolded the project + spawned
// (or attempted to spawn) the per-project operator session.
type ProjectCreateResult = {
  slug: string;
  operator_sid: string | null;
  spawn_error: string | null;
};

// Mirror T-0025's statusBadgeClass so single-server and cross-server views
// paint the same colours from the same enum. Unknown strings fall back to
// the dim pill rather than going invisible — same forward-compat policy.
//
// T-0340: these project-card pills (working | needs-input | idle) are a
// PROJECT-level rollup (quick_status.py / D-0018-quick-status.md), a distinct
// concept from per-session liveness (live | suspended | archived). "working"
// means a session is actively crunching; "needs-input" rolls up paused +
// at-prompt sessions; "idle" means no live session. It deliberately carries
// the extra needs-input signal the liveness vocab doesn't, so it is left as-is
// here rather than collapsed into live/suspended. (The mothership fleet cards
// in AllProjects.tsx share this enum and are tracked as the T-0342 follow-up.)
function statusBadgeClass(status: string): string {
  switch (status) {
    case "working":
      return "mc-badge mc-badge-active";
    case "needs-input":
      return "mc-badge mc-badge-warn";
    case "idle":
      return "mc-badge mc-badge-dim";
    default:
      return "mc-badge mc-badge-dim";
  }
}

// T-0349: which server-view onboarding beat (if any) the Picker should show.
// Encodes two invariants the single-brain landing must hold:
//   1. AT MOST ONE beat at a time — never the old 3-popover stack.
//   2. Onboarding fires ONLY on a genuinely-empty install. An active operator
//      who already runs projects sees NONE — his project cards are foregrounded
//      instead of buried under "create your first project" / "other people's
//      projects" first-run spotlights that contradict the fact he operates them.
// Order on an empty install: help spotlight first, then (admin-only) the
// create-project prompt once help is dismissed.
export type ServerOnboardingBeat = "help" | "create" | null;
export function pickServerOnboardingBeat(opts: {
  isEmptyInstall: boolean;
  isAdmin: boolean;
  helpVisible: boolean;
  createVisible: boolean;
}): ServerOnboardingBeat {
  if (!opts.isEmptyInstall) return null;
  if (opts.helpVisible) return "help";
  if (opts.isAdmin && opts.createVisible) return "create";
  return null;
}

export function Picker() {
  // T-0365: seed from the shared project cache for an instant landing paint;
  // the effect still revalidates via api.projects().
  const [projects, setProjects] = useState<Project[] | null>(() => cachedProjects());
  const [error, setError] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState<boolean>(false);
  const [creating, setCreating] = useState<ProjectCreateState | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSaving, setCreateSaving] = useState(false);
  // T-0052: when set, the modal flips to the success view (operator attach
  // command + spawn-error nudge if any) instead of the create form.
  const [createResult, setCreateResult] = useState<ProjectCreateResult | null>(null);
  // Rationale expander state — fetched on first open of the modal,
  // cached for the page lifetime. The SSOT lives at
  // api/app/data/project-create-modes.md (T-0051) so the UI text
  // can't drift from the per-user bot-squad-manager AGENT_INSTRUCTIONS.
  const [rationaleOpen, setRationaleOpen] = useState(false);
  const [rationaleText, setRationaleText] = useState<string | null>(null);
  const [rationaleError, setRationaleError] = useState<string | null>(null);

  // T-0349: the §9 server-view onboarding is gated to a GENUINELY-EMPTY install
  // and shown one beat at a time. The single-brain operator already runs
  // projects — landing on the Picker, his own project cards must be foregrounded,
  // not buried under stacked first-run spotlights ("create your first project",
  // "other people's projects") that contradict the fact that he operates them.
  // The §9.4 "other people's projects" discovery beat (and its per-project
  // sessions fan-out) is dropped entirely: it mislabelled the operator's OWN
  // projects as belonging to others whenever any session in them was owned by a
  // different linux user.
  const helpStep = useOnboardingStep("srv.9_1.help_spotlight");
  const createStep = useOnboardingStep("srv.9_6.project_create");
  // Genuinely-empty install = the only state that should see onboarding. While
  // projects load (null) we show nothing rather than flash a first-run beat.
  const isEmptyInstall = projects !== null && projects.length === 0;
  // Show AT MOST ONE beat at a time (see pickServerOnboardingBeat).
  const activeBeat = pickServerOnboardingBeat({
    isEmptyInstall,
    isAdmin,
    helpVisible: helpStep.visible,
    createVisible: createStep.visible,
  });

  // T-0763: `fresh` after a create — api.projects() now shares a concurrent
  // in-flight request, and on a single-install build the GlobalBusyIndicator
  // polls the same endpoint every 8s, so a reload that joined a request issued
  // BEFORE the POST would paint a list missing the project just created. The
  // mount reload has no such ordering requirement and shares by design.
  function reload(fresh = false) {
    api.projects({ fresh }).then(setProjects).catch((e) => setError(String(e)));
  }

  useEffect(() => {
    reload();
    api.me().then((m) => setIsAdmin(Boolean(m.is_admin))).catch(() => {});
  }, []);

  async function submitCreate() {
    if (!creating) return;
    const errs = validateWizard(creating);
    if (errs.length > 0) {
      setCreateError(errs[0]);
      return;
    }
    setCreateSaving(true);
    setCreateError(null);
    try {
      const resp = await api.createProject(payloadFromWizard(creating));
      // T-0053: mark the user as past the first-project threshold so the
      // proj.13_* tour can fire on their next /p/<slug> visit. Both create
      // shapes (minimal and deep-flow) count — a user who created via the
      // minimal path is still a project-author.
      void markSeen("proj.has_created_any");
      // Minimal-create (mode=null) doesn't spawn — close immediately as
      // before. Deep-flow scaffolds get the success step.
      if (creating.mode === null) {
        setCreating(null);
        reload(true);
      } else {
        setCreateResult({
          slug: resp.slug,
          operator_sid: resp.operator_sid ?? null,
          spawn_error: resp.spawn_error ?? null,
        });
        reload(true);
      }
    } catch (e) {
      setCreateError(String(e));
    } finally {
      setCreateSaving(false);
    }
  }

  function closeWizard() {
    setCreating(null);
    setCreateResult(null);
    setCreateError(null);
  }

  function openRationale() {
    setRationaleOpen(true);
    if (rationaleText !== null || rationaleError !== null) return;
    api
      .createModesDoc()
      .then((r) => setRationaleText(r.content))
      .catch((e) => setRationaleError(String(e)));
  }

  return (
    <div className="container py-4" style={{ maxWidth: "900px" }}>
      {/* §9.1 — first onboarding beat, shown only on a genuinely-empty install
          (see activeBeat above). Anchors on the sidebar HELP link via the
          data-onboarding-anchor convention (see Shell.tsx). One beat renders at
          a time; this one yields to §9.6 once dismissed. */}
      {activeBeat === "help" && (
        <Coachmark
          stepId="srv.9_1.help_spotlight"
          title="Need a hand?"
          anchorSelector='[data-onboarding-anchor="help-nav"]'
          placement="right"
          body={
            <Typewriter
              text="You can always see the tmux cheatsheet here."
              trailing={
                <>
                  {" "}
                  <Link to="/help#tmux-cheatsheet">Open it →</Link>
                </>
              }
            />
          }
        />
      )}

      {/* §9.6 — closing beat. Anchors on the admin-only "+ New project"
          button (mounted below). `final` flips the dismiss to write
          __skip_all__ so any future-added §9.x beat doesn't re-trigger the
          tour for users who already finished it. Only shown on an empty install
          and only after §9.1 is dismissed (activeBeat sequencing) — never nag a
          single-brain operator who already runs projects to "create his first".
          The spotlight just points; clicking it does NOT open the modal — the
          user clicks the button themselves (per T-0022 DoD note). */}
      {activeBeat === "create" && (
        <Coachmark
          stepId="srv.9_6.project_create"
          title="Create your first project"
          anchorSelector='[data-onboarding-anchor="create-project"]'
          placement="bottom"
          final
          body={<>Create your first project here.</>}
        />
      )}

      <div className="d-flex align-items-center justify-content-between mb-4">
        <div className="mc-section-title" style={{ margin: 0 }}>Projects</div>
        {isAdmin && (
          <button
            type="button"
            className="btn btn-outline-primary btn-sm"
            style={{ fontSize: "0.72rem" }}
            data-onboarding-anchor="create-project"
            onClick={() => setCreating(emptyWizardState())}
          >
            + New project
          </button>
        )}
      </div>

      {error && <div className="alert alert-danger">{error}</div>}

      {projects === null && !error && (
        <div className="mc-loading">Loading</div>
      )}

      {projects !== null && projects.length === 0 && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          {/* T-0437/next-wave fork-A: a bare "No projects configured" was a
              dead-end for a non-admin (project creation is admin-provisioned —
              the intended model). Explain WHO provisions, so it reads as "by
              design", not "broken". Admins get pointed at the create affordance. */}
          {isAdmin ? (
            <>
              <div>No projects yet.</div>
              <div style={{ marginTop: "0.4rem", fontSize: "0.85rem", color: "var(--mc-text-dim)", maxWidth: "32rem" }}>
                Use <strong>+ New project</strong> above to create one.
              </div>
            </>
          ) : (
            <>
              <div>No projects yet.</div>
              <div style={{ marginTop: "0.4rem", fontSize: "0.85rem", color: "var(--mc-text-dim)", maxWidth: "32rem" }}>
                Projects are provisioned by an admin/operator — ask yours to set
                one up, then it&apos;ll appear here.
              </div>
            </>
          )}
        </div>
      )}

      <div className="row g-3">
        {projects?.map((p) => {
          // T-0346: a `needs-input` project is the one signal an operator
          // should act on, but routing it to the board (backlog list) was a
          // dead-end — it never surfaced WHICH session is blocked or HOW to
          // respond. Deep-link the whole card (and therefore its pill) straight
          // to the process view's needs-input landing, which lists the waiting
          // session(s) + their `tmux a -t` attach command. Other statuses keep
          // routing to the board as before.
          const needsInput = p.status === "needs-input";
          const cardTo = needsInput
            ? `/p/${p.slug}/sessions?needs_input=1`
            : `/p/${p.slug}`;
          return (
          <div className="col-md-4" key={p.slug}>
            <Link
              to={cardTo}
              className="mc-project-card"
              title={
                needsInput
                  ? "A session is waiting for your input — open it to see what & respond"
                  : undefined
              }
            >
              <div className="mc-project-name">{p.display_name}</div>
              <div
                className="mc-project-slug"
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: "0.5rem",
                }}
              >
                <span>{p.slug}</span>
                {p.status && (
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.3rem",
                    }}
                  >
                    {needsInput && (
                      <span
                        style={{ fontSize: "0.62rem", color: "var(--mc-text-dim)" }}
                      >
                        respond →
                      </span>
                    )}
                    <span
                      className={statusBadgeClass(p.status)}
                      title={p.status_since ? `since ${p.status_since}` : undefined}
                    >
                      {p.status}
                    </span>
                  </span>
                )}
              </div>
            </Link>
          </div>
          );
        })}
      </div>

      <Modal
        open={creating !== null}
        title={createResult ? "Project created" : "New project"}
        onClose={closeWizard}
        footer={
          createResult ? (
            <button
              type="button"
              className="btn btn-primary"
              onClick={closeWizard}
              data-testid="project-create-done"
            >
              Done
            </button>
          ) : (
            <>
              <button type="button" className="btn btn-secondary" onClick={closeWizard}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={submitCreate}
                disabled={
                  createSaving ||
                  (creating?.mode === "attach_destructive" &&
                    !creating?.confirm_destructive_move)
                }
                data-testid="project-create-submit"
              >
                {createSaving ? "Creating…" : "Create"}
              </button>
            </>
          )
        }
      >
        {createResult ? (
          <div data-testid="project-create-success">
            <p style={{ fontSize: "0.9rem", marginBottom: "0.75rem" }}>
              Project <code>{createResult.slug}</code> is created.
            </p>
            {createResult.operator_sid ? (
              <>
                <p style={{ fontSize: "0.85rem", marginBottom: "0.5rem" }}>
                  A per-project <strong>operator</strong> session is spawned
                  in the project's tmux session. This is your day-to-day
                  chat surface for the project — attach and start talking:
                </p>
                <div style={{ marginBottom: "0.75rem" }}>
                  <CopyableTmuxAttach
                    session={createResult.slug}
                    window="operator"
                    size="md"
                  />
                </div>
                <p className="text-muted" style={{ fontSize: "0.72rem", margin: 0 }}>
                  Operator SID:{" "}
                  <code style={{ fontFamily: "var(--mc-mono)" }}>
                    {createResult.operator_sid}
                  </code>
                </p>
              </>
            ) : (
              <>
                <div className="alert alert-warning" style={{ fontSize: "0.82rem" }}>
                  The project is created, but the operator session could
                  not be spawned automatically.
                  {createResult.spawn_error && (
                    <>
                      {" "}
                      <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}>
                        {createResult.spawn_error}
                      </span>
                    </>
                  )}
                </div>
                <p style={{ fontSize: "0.82rem", marginBottom: 0 }}>
                  You can spawn it by hand from the project page's{" "}
                  <Link to={`/p/${createResult.slug}/sessions`}>
                    sessions tab
                  </Link>{" "}
                  (window name: <code>operator</code>).
                </p>
              </>
            )}
          </div>
        ) : (
        <>
        {createError && <div className="alert alert-danger">{createError}</div>}
        <div className="mb-3">
          <label className="form-label">Display name *</label>
          <input
            className="form-control"
            value={creating?.display_name ?? ""}
            onChange={(e) => {
              if (!creating) return;
              const display_name = e.target.value;
              setCreating({
                ...creating,
                display_name,
                slug: creating.slug_touched
                  ? creating.slug
                  : deriveSlug(display_name),
              });
            }}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Slug *</label>
          <input
            className="form-control"
            style={{ fontFamily: "var(--mc-mono)" }}
            value={creating?.slug ?? ""}
            onChange={(e) =>
              creating && setCreating({ ...creating, slug: e.target.value, slug_touched: true })
            }
            placeholder="lowercase, letters/digits/-/_"
          />
        </div>

        <div className="mb-3">
          <div className="form-label d-flex align-items-center justify-content-between">
            <span>Setup mode</span>
            <button
              type="button"
              className="btn btn-link btn-sm p-0"
              style={{ fontSize: "0.72rem" }}
              data-onboarding-anchor="rationale-expand"
              onClick={() => (rationaleOpen ? setRationaleOpen(false) : openRationale())}
            >
              {rationaleOpen ? "hide" : "why does bot-squad require this?"}
            </button>
          </div>
          {rationaleOpen && (
            <div
              className="border rounded p-2 mb-2"
              style={{
                fontSize: "0.78rem",
                whiteSpace: "pre-wrap",
                background: "var(--mc-bg-soft, #fafafa)",
                maxHeight: "260px",
                overflowY: "auto",
              }}
              data-testid="project-create-rationale"
            >
              {rationaleText === null && rationaleError === null && "Loading…"}
              {rationaleError && (
                <span className="text-danger">Could not load: {rationaleError}</span>
              )}
              {rationaleText !== null && rationaleText}
            </div>
          )}

          {modeOptions().map((opt) => {
            const checked = creating?.mode === opt.key;
            const id = `proj-mode-${opt.key}`;
            return (
              <div className="form-check" key={opt.key}>
                <input
                  className="form-check-input"
                  type="radio"
                  id={id}
                  name="proj-mode"
                  disabled={opt.disabled}
                  checked={checked}
                  onChange={() =>
                    creating &&
                    setCreating({ ...creating, mode: opt.key as ProjectCreateMode })
                  }
                />
                <label className="form-check-label" htmlFor={id}>
                  <span>
                    {opt.label}
                    {opt.recommended && (
                      <span className="badge bg-success ms-2" style={{ fontSize: "0.62rem" }}>
                        recommended
                      </span>
                    )}
                  </span>
                  <div className="text-muted" style={{ fontSize: "0.72rem" }}>
                    {opt.hint}
                    {opt.disabled && opt.disabled_reason && (
                      <> — <em>{opt.disabled_reason}</em></>
                    )}
                  </div>
                </label>
              </div>
            );
          })}
        </div>

        {creating?.mode === "new_from_scratch" && (
          <>
            <div className="mb-3">
              <label className="form-label">Mother dir *</label>
              <input
                className="form-control"
                style={{ fontFamily: "var(--mc-mono)" }}
                value={creating.mother_dir}
                onChange={(e) => setCreating({ ...creating, mother_dir: e.target.value })}
                placeholder={`/home/<user>/${creating.slug || "<slug>"}`}
              />
            </div>
            <div className="mb-3">
              <label className="form-label">Git remote URL</label>
              <input
                className="form-control"
                style={{ fontFamily: "var(--mc-mono)" }}
                value={creating.git_remote}
                onChange={(e) => setCreating({ ...creating, git_remote: e.target.value })}
                placeholder="optional — leave blank to git init locally"
              />
            </div>
          </>
        )}

        {creating?.mode === "paths_as_they_are" && (
          <>
            <div className="mb-3">
              <label className="form-label">Mother dir *</label>
              <input
                className="form-control"
                style={{ fontFamily: "var(--mc-mono)" }}
                value={creating.mother_dir}
                onChange={(e) => setCreating({ ...creating, mother_dir: e.target.value })}
                placeholder={`/home/<user>/${creating.slug || "<slug>"}`}
              />
            </div>
            <div className="mb-3">
              <label className="form-label">Dev clone path *</label>
              <input
                className="form-control"
                style={{ fontFamily: "var(--mc-mono)" }}
                value={creating.repo_path}
                onChange={(e) => setCreating({ ...creating, repo_path: e.target.value })}
                placeholder="/path/to/existing/dev-clone"
              />
            </div>
            <div className="mb-3">
              <label className="form-label">Master clone path *</label>
              <input
                className="form-control"
                style={{ fontFamily: "var(--mc-mono)" }}
                value={creating.repo_master}
                onChange={(e) => setCreating({ ...creating, repo_master: e.target.value })}
                placeholder="/path/to/existing/master-clone"
              />
            </div>
          </>
        )}

        {creating?.mode === "attach_destructive" && (
          <>
            <div className="mb-3">
              <label className="form-label">Mother dir *</label>
              <input
                className="form-control"
                style={{ fontFamily: "var(--mc-mono)" }}
                value={creating.mother_dir}
                onChange={(e) => setCreating({ ...creating, mother_dir: e.target.value })}
                placeholder={`/home/<user>/${creating.slug || "<slug>"}`}
              />
            </div>
            <div className="mb-3">
              <label className="form-label">Existing repo path *</label>
              <input
                className="form-control"
                style={{ fontFamily: "var(--mc-mono)" }}
                value={creating.existing_path}
                onChange={(e) => setCreating({ ...creating, existing_path: e.target.value })}
                placeholder="/path/to/your/existing/repo"
              />
            </div>
            <div className="mb-3">
              <label className="form-label">Existing repo becomes *</label>
              {(["dev", "master"] as const).map((side) => {
                const id = `proj-destr-becomes-${side}`;
                return (
                  <div className="form-check" key={side}>
                    <input
                      className="form-check-input"
                      type="radio"
                      id={id}
                      name="proj-destr-becomes"
                      checked={creating.existing_becomes === side}
                      onChange={() =>
                        setCreating({ ...creating, existing_becomes: side })
                      }
                    />
                    <label className="form-check-label" htmlFor={id}>
                      <code>{side}</code>{" "}
                      <span className="text-muted" style={{ fontSize: "0.72rem" }}>
                        — your repo becomes <code>{side}</code>, the other side
                        is freshly cloned from it
                      </span>
                    </label>
                  </div>
                );
              })}
            </div>
            <div
              className="alert alert-warning"
              style={{ fontSize: "0.82rem" }}
              data-testid="project-create-destructive-confirm"
            >
              <div style={{ marginBottom: "0.5rem" }}>
                <strong>DESTRUCTIVE:</strong> this will rename your repo from{" "}
                <code>{creating.existing_path || "<existing>"}</code> to{" "}
                <code>
                  {(creating.mother_dir || "<mother>") + "/" + creating.existing_becomes}
                </code>
                . Rollback:{" "}
                <code>
                  mv{" "}
                  {(creating.mother_dir || "<mother>") + "/" + creating.existing_becomes}{" "}
                  {creating.existing_path || "<existing>"}
                </code>
              </div>
              <div className="form-check">
                <input
                  className="form-check-input"
                  type="checkbox"
                  id="proj-destr-confirm"
                  checked={creating.confirm_destructive_move}
                  onChange={(e) =>
                    setCreating({
                      ...creating,
                      confirm_destructive_move: e.target.checked,
                    })
                  }
                  data-testid="project-create-destructive-checkbox"
                />
                <label className="form-check-label" htmlFor="proj-destr-confirm">
                  I understand, proceed
                </label>
              </div>
            </div>
          </>
        )}
        </>
        )}
      </Modal>
    </div>
  );
}
