/**
 * Copy constants for onboarding spotlights. Kept here so a wording tweak
 * doesn't touch the coachmark engine. Per T-0017: "Keep the copy in a small
 * constants file." Extend as later beats land.
 */

export const STEP_9_3_TITLE = "Mothership perks";

// T-0357: re-worded off the retired "ALL PROJECTS" cross-server picker. The
// fleet view is now a SERVERS overview (the ▦ "Fleet / admin" door); you pick a
// project to operate from Home (`/`).
export const STEP_9_3_BULLETS: ReadonlyArray<string> = [
  "Every attached server in one fleet view — open a server to drill into its projects.",
  "Quick project switcher — the sidebar dropdown carries live status for every project.",
  "@bot_squad_bot routes notifications from every attached server to one TG chat.",
  "Preferences sync across servers (coming soon).",
];

// T-0053 / Chapter I §12 — project-section onboarding (proj.13_*).
//
// Role blurbs are condensed from the git role-contract SSOT,
// `api/app/resources/roles/*.md` (D-0043). They live here because that
// directory is API-image content the web bundle can't `?raw`-import, and
// the per-project copy the install serves is display-only + drifts.
// Source-of-truth pointers below.
export type RoleBlurb = {
  key: string;
  label: string;
  source: string;   // SSOT path the blurb was condensed from (traceability)
  blurb: string;    // 1-2 sentences, condensed from the source md
};

export const PROJECT_ROLE_BLURBS: ReadonlyArray<RoleBlurb> = [
  {
    key: "operator",
    label: "Project operator",
    source: "api/app/resources/roles/operator.md",
    blurb:
      "Your day-to-day chat surface for this project. Translates your intent " +
      "into concrete moves: spawns dev TLs for initiatives, spawns dev workers " +
      "for ad-hoc tasks, curates the roadmap. Does not write feature code.",
  },
  {
    key: "teamlead",
    label: "Team-lead (dev TL)",
    source: "api/app/resources/roles/teamlead.md",
    blurb:
      "Owns an initiative. Splits your asks into named subtasks and spawns " +
      "dev workers. Listens on the peer bus, coordinates handoffs, never " +
      "kills workers on its own.",
  },
  {
    key: "dev",
    label: "Dev worker",
    source: "api/app/resources/roles/dev.md",
    blurb:
      "Owns one task end-to-end. Builds, tests, commits on the shared dev " +
      "tree (no worktrees). Flips the task to totest when DoD is green and " +
      "peer_sends READY to its TL.",
  },
  {
    key: "prod-teamlead",
    label: "Prod team-lead",
    source: "api/app/resources/roles/prod-teamlead.md",
    blurb:
      "Lives in the prod clone (master). Cuts releases when dev TLs signal " +
      "READY, runs hotfixes and rollbacks. No feature work, no long " +
      "refactors — those go back to a dev TL.",
  },
  {
    key: "qa",
    label: "QA",
    source: "api/app/resources/roles/qa.md",
    blurb:
      "Verifies totest tickets against their DoD and reports back to the TL " +
      "— VERIFIED to close, or REOPEN with a follow-on ticket. Files " +
      "regressions, does not write the fix.",
  },
];

export const STEP_13_1_TITLE = "Who works on this project";
export const STEP_13_2_TITLE = "Tasks vs initiatives";
export const STEP_13_3_TITLE = "Talk to your operator";
export const STEP_13_4_TITLE = "You're set";
