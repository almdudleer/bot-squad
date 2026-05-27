// T-0051 — pure helpers for the +New project wizard.
//
// The Picker.tsx modal owns the React state; this module owns the
// shape, the validator, and the POST-payload builder so they can be
// unit-tested without jsdom (matches Welcome.tsx's pattern).

export type ProjectCreateMode =
  | "new_from_scratch"
  | "paths_as_they_are"
  | "attach_destructive";

export interface ProjectCreateState {
  display_name: string;
  slug: string;
  slug_touched: boolean;
  mode: ProjectCreateMode | null;

  // mode=new_from_scratch
  mother_dir: string;
  git_remote: string;

  // mode=paths_as_they_are
  repo_path: string;
  repo_master: string;

  // mode=attach_destructive (UI gated; backend 501 today)
  existing_path: string;
  existing_becomes: "dev" | "master";
  confirm_destructive_move: boolean;
}

export function emptyWizardState(): ProjectCreateState {
  return {
    display_name: "",
    slug: "",
    slug_touched: false,
    mode: null,
    mother_dir: "",
    git_remote: "",
    repo_path: "",
    repo_master: "",
    existing_path: "",
    existing_becomes: "dev",
    confirm_destructive_move: false,
  };
}

// Slug derivation from display name. Lowercase, runs of non-alnum
// become `-`, strip leading non-letter chars to satisfy
// ^[a-z][a-z0-9_-]*$ on the server.
export function deriveSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .replace(/-+$/g, "");
}

// Return a list of human-readable validation problems. Empty list =
// the state can be submitted. The wizard renders the first error as
// the modal-level alert.
export function validateWizard(state: ProjectCreateState): string[] {
  const errs: string[] = [];
  if (!state.display_name.trim()) errs.push("display name required");
  if (!state.slug.trim()) errs.push("slug required");
  if (state.mode === null) {
    // The minimal-create back-compat mode (T-0021). Nothing else to
    // check — the API only requires slug + display_name.
    return errs;
  }

  if (state.mode === "attach_destructive") {
    errs.push(
      "destructive move is not implemented yet — see the T-0051 follow-up",
    );
    return errs;
  }

  if (!state.mother_dir.trim()) errs.push("mother dir required");
  else if (!state.mother_dir.trim().startsWith("/"))
    errs.push("mother dir must be an absolute path");

  if (state.mode === "paths_as_they_are") {
    if (!state.repo_path.trim()) errs.push("dev repo path required");
    else if (!state.repo_path.trim().startsWith("/"))
      errs.push("dev repo path must be an absolute path");
    if (!state.repo_master.trim()) errs.push("master repo path required");
    else if (!state.repo_master.trim().startsWith("/"))
      errs.push("master repo path must be an absolute path");
  }
  // new_from_scratch: git_remote is optional (blank = git init locally).

  return errs;
}

// Build the JSON body for POST /api/projects from wizard state.
// Strips empty optional fields so we don't send empty strings the
// server would have to special-case as "not provided".
export function payloadFromWizard(state: ProjectCreateState): Record<string, unknown> {
  const base: Record<string, unknown> = {
    slug: state.slug.trim(),
    display_name: state.display_name.trim(),
  };
  if (state.mode === null) return base;

  base.mode = state.mode;

  if (state.mode === "new_from_scratch") {
    base.mother_dir = state.mother_dir.trim();
    if (state.git_remote.trim()) base.git_remote = state.git_remote.trim();
  } else if (state.mode === "paths_as_they_are") {
    base.mother_dir = state.mother_dir.trim();
    base.repo_path = state.repo_path.trim();
    base.repo_master = state.repo_master.trim();
  } else if (state.mode === "attach_destructive") {
    base.mother_dir = state.mother_dir.trim();
    base.existing_path = state.existing_path.trim();
    base.existing_becomes = state.existing_becomes;
    base.confirm_destructive_move = state.confirm_destructive_move;
  }
  return base;
}

// Mode metadata for the wizard UI — kept here so the labels are
// testable + tweakable in one place. The "rationale" text comes from
// the API (see Picker's useEffect that fetches /api/projects/_/create-modes)
// — the single source of truth lives in api/app/data/project-create-modes.md.
export interface ModeOption {
  key: ProjectCreateMode;
  label: string;
  hint: string;
  recommended?: boolean;
  disabled?: boolean;
  disabled_reason?: string;
}

export function modeOptions(): ModeOption[] {
  return [
    {
      key: "new_from_scratch",
      label: "New project from scratch",
      hint: "Create a fresh dev + master clone. Optional: clone from a remote URL.",
    },
    {
      key: "paths_as_they_are",
      label: "Attach existing, keep paths as-they-are",
      hint: "Keep your dev/master clones where they are. Create only the mother dir + .bot-squad.toml.",
      recommended: true,
    },
    {
      key: "attach_destructive",
      label: "Attach existing, move into structure",
      hint: "Rename your existing repo into <mother>/dev or <mother>/master. Destructive — peeled to follow-up.",
      disabled: true,
      disabled_reason: "Not yet implemented (T-0051 follow-up).",
    },
  ];
}
