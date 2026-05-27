// Onboarding step ids — naming convention srv.<beat>.<noun> for the server
// view (Chapter I §§9.x) and proj.<beat>.<noun> for the project view
// (Chapter I §12, T-0053).
//
// The framework treats step ids as opaque strings; this union is the lexicon
// shared by all spotlight tasks so downstream beats pick from one place. To
// add a beat, add a literal here.
//
// Beat numbers render as N_M (not N.M) so the dot-separated segments stay
// addressable.
//
// proj.has_created_any is a marker (not a coachmark step): Picker writes it
// after a successful project create, and Project.tsx gates the whole proj.*
// tour on it — so a user landing on someone else's project before they've
// created their own doesn't get the spotlight (the server-view §9.x chain
// handles them instead). Persists server-side via the same seen_steps API
// so it crosses devices.

export type OnboardingStepId =
  | "srv.intro"
  | "srv.9_1.help_spotlight"
  | "srv.9_2.detach_admin"
  | "srv.9_3.cross_server"
  | "srv.9_4.existing_projects"
  | "srv.9_5.tg_binding"
  | "srv.9_6.project_create"
  | "proj.has_created_any"
  | "proj.13_1.roles"
  | "proj.13_2.tasks_vs_initiatives"
  | "proj.13_3.operator_attach"
  | "proj.13_4.close";

export const SKIP_ALL_SENTINEL = "__skip_all__";
