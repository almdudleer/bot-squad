/**
 * Copy constants for onboarding spotlights. Kept here so a wording tweak
 * doesn't touch the coachmark engine. Per T-0017: "Keep the copy in a small
 * constants file." Extend as later beats land.
 */

export const STEP_9_3_TITLE = "Mothership perks";

export const STEP_9_3_BULLETS: ReadonlyArray<string> = [
  "All your servers, one list — open ALL PROJECTS to fan out across everything attached.",
  "Quick project switcher — the sidebar dropdown carries live status for every project.",
  "@bot_squad_bot routes notifications from every attached server to one TG chat.",
  "Preferences sync across servers (coming soon).",
];

// T-0053 / Chapter I §12 — project-section onboarding (proj.13_*).
//
// Role blurbs are condensed from `vision/roles/*.md` in the install. They
// live here because vision/roles/ is per-project data (not in the
// codebase tree), and a freshly-created project's vision/roles/ is empty
// — there's nothing for ?raw to import. Source-of-truth pointers below.
export type RoleBlurb = {
  key: string;
  label: string;
  source: string;   // path inside vision/roles for traceability
  blurb: string;    // 1-2 sentences, condensed from the source md
};

export const PROJECT_ROLE_BLURBS: ReadonlyArray<RoleBlurb> = [
  {
    key: "operator",
    label: "Project operator",
    source: "vision/roles/operator.md",
    blurb:
      "Your day-to-day chat surface for this project. Translates your intent " +
      "into concrete moves: spawns dev TLs for initiatives, spawns dev workers " +
      "for ad-hoc tasks, curates the roadmap. Does not write feature code.",
  },
  {
    key: "teamlead",
    label: "Team-lead (dev TL)",
    source: "vision/roles/teamlead.md",
    blurb:
      "Owns an initiative. Splits your asks into named subtasks and spawns " +
      "dev workers. Listens on the peer bus, coordinates handoffs, never " +
      "kills workers on its own.",
  },
  {
    key: "dev",
    label: "Dev worker",
    source: "vision/roles/dev.md",
    blurb:
      "Owns one task end-to-end. Builds, tests, commits on the shared dev " +
      "tree (no worktrees). Flips the task to totest when DoD is green and " +
      "peer_sends READY to its TL.",
  },
  {
    key: "prod-teamlead",
    label: "Prod team-lead",
    source: "vision/roles/prod-teamlead.md",
    blurb:
      "Lives in the prod clone (master). Cuts releases when dev TLs signal " +
      "READY, runs hotfixes and rollbacks. No feature work, no long " +
      "refactors — those go back to a dev TL.",
  },
  {
    key: "qa",
    label: "QA",
    source: "vision/roles/qa.md",
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
