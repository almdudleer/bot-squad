import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Project } from "../api";
import { Modal } from "../components/Modal";
import { Coachmark, Typewriter, useOnboardingStep } from "../onboarding";

// Auto-derive a slug from a display name on the fly so users only have to
// type one of the two. Lowercase, replace runs of non-alnum with '-', strip
// leading non-letter chars to satisfy the server's ^[a-z][a-z0-9_-]*$.
function deriveSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .replace(/-+$/g, "");
}

interface NewProjectState {
  display_name: string;
  slug: string;
  slug_touched: boolean;
  repo_path: string;
}

// Mirror T-0025's statusBadgeClass so single-server and cross-server views
// paint the same colours from the same enum. Unknown strings fall back to
// the dim pill rather than going invisible — same forward-compat policy.
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

export function Picker() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState<boolean>(false);
  const [creating, setCreating] = useState<NewProjectState | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSaving, setCreateSaving] = useState(false);

  // T-0018 / §9.4: the slug of the first project on the picker that hosts a
  // session NOT prefixed with the current user's `S-<linux_user>-…` SID.
  // When set, the §9.4 Coachmark mounts on that card; when null, the beat is
  // suppressed (sole-creator / empty installation never sees the spotlight).
  const [alienSlug, setAlienSlug] = useState<string | null>(null);
  const existingStep = useOnboardingStep("srv.9_4.existing_projects");

  function reload() {
    api.projects().then(setProjects).catch((e) => setError(String(e)));
  }

  useEffect(() => {
    reload();
    api.me().then((m) => setIsAdmin(Boolean(m.is_admin))).catch(() => {});
  }, []);

  async function submitCreate() {
    if (!creating) return;
    if (!creating.display_name.trim() || !creating.slug.trim()) {
      setCreateError("display name and slug required");
      return;
    }
    setCreateSaving(true);
    setCreateError(null);
    try {
      await api.createProject(
        creating.slug.trim(),
        creating.display_name.trim(),
        creating.repo_path.trim() || undefined,
      );
      setCreating(null);
      reload();
    } catch (e) {
      setCreateError(String(e));
    } finally {
      setCreateSaving(false);
    }
  }

  // Alien-detection scan: only runs while §9.4 is still pending (gated on
  // existingStep.visible) so dismissing the coachmark also stops the
  // per-project sessions fan-out on subsequent Picker visits. Stops on first
  // alien match — typical onboarding hits it in the first project.
  useEffect(() => {
    if (!existingStep.visible) return;
    if (!projects || projects.length === 0) return;
    let cancelled = false;
    (async () => {
      try {
        const me = await api.me();
        if (cancelled) return;
        const myPrefix = `S-${me.linux_user}-`;
        for (const p of projects) {
          if (cancelled) return;
          try {
            const rows = await api.sessions(p.slug);
            if (cancelled) return;
            const alien = rows.find(
              (s) => !s.archived && !s.sid.startsWith(myPrefix),
            );
            if (alien) {
              setAlienSlug(p.slug);
              return;
            }
          } catch {
            /* permission denied / unreachable — skip this project */
          }
        }
      } catch {
        /* anonymous / me lookup failed — bail */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projects, existingStep.visible]);

  return (
    <div className="container py-4" style={{ maxWidth: "900px" }}>
      {/* §9.1 — first onboarding beat for a fresh server-view visit. Anchors
          on the sidebar HELP link via the data-onboarding-anchor convention
          (see Shell.tsx); future §9.2..9.6 beats reuse the same attribute. */}
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

      {/* §9.6 — closing beat. Anchors on the admin-only "+ New project"
          button (mounted below). `final` flips the dismiss to write
          __skip_all__ so any future-added §9.x beat doesn't re-trigger the
          tour for users who already finished it. The spotlight just points;
          clicking it does NOT open the modal — the user clicks the button
          themselves (per T-0022 DoD note). */}
      {isAdmin && (
        <Coachmark
          stepId="srv.9_6.project_create"
          title="Create your first project"
          anchorSelector='[data-onboarding-anchor="create-project"]'
          placement="bottom"
          final
          body={<>Create your first project here.</>}
        />
      )}

      {/* §9.4 — discovery spotlight on the first non-self project. Only
          mounts when alien-detection found one (see useEffect above); on a
          sole-creator or empty installation this is silent. The CTA points
          at the project's sessions list, where every row carries the
          copyable `tmux a -t` shipped by T-0006. */}
      {alienSlug && (
        <Coachmark
          stepId="srv.9_4.existing_projects"
          title="Other people's projects"
          anchorSelector='[data-onboarding-anchor="existing-projects"]'
          placement="bottom"
          body={
            <>
              There are already projects from other people on this server. You can
              open any of them and inspect their sessions — every session row has a
              copyable <code>tmux a -t</code> you can run over SSH to attach.{" "}
              <Link to={`/p/${alienSlug}/sessions`}>Open the sessions list →</Link>
            </>
          }
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
            onClick={() =>
              setCreating({
                display_name: "",
                slug: "",
                slug_touched: false,
                repo_path: "",
              })
            }
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
          <div>No projects configured.</div>
        </div>
      )}

      <div className="row g-3">
        {projects?.map((p) => (
          <div className="col-md-4" key={p.slug}>
            <Link
              to={`/p/${p.slug}`}
              className="mc-project-card"
              data-onboarding-anchor={p.slug === alienSlug ? "existing-projects" : undefined}
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
                    className={statusBadgeClass(p.status)}
                    title={p.status_since ? `since ${p.status_since}` : undefined}
                  >
                    {p.status}
                  </span>
                )}
              </div>
            </Link>
          </div>
        ))}
      </div>

      <Modal
        open={creating !== null}
        title="New project"
        onClose={() => setCreating(null)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setCreating(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submitCreate}
              disabled={createSaving}
            >
              {createSaving ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
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
          <label className="form-label">Repo path</label>
          <input
            className="form-control"
            style={{ fontFamily: "var(--mc-mono)" }}
            value={creating?.repo_path ?? ""}
            onChange={(e) =>
              creating && setCreating({ ...creating, repo_path: e.target.value })
            }
            placeholder="optional — fill in projects.toml later"
          />
        </div>
      </Modal>
    </div>
  );
}
