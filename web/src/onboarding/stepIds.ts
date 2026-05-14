// Onboarding step ids — naming convention srv.<beat>.<noun>.
//
// The framework treats step ids as opaque strings; this union is the lexicon
// shared by all spotlight tasks (T-0014/T-0015/T-0017/T-0018/T-0020/T-0022)
// so downstream beats pick from one place. To add a beat, add a literal here.
//
// Beat number is rendered as 9_N (not 9.N) so the dot-separated segments stay
// addressable.

export type OnboardingStepId =
  | "srv.intro"
  | "srv.9_1.help_spotlight"
  | "srv.9_2.detach_admin"
  | "srv.9_3.cross_server"
  | "srv.9_4.existing_projects"
  | "srv.9_5.tg_binding"
  | "srv.9_6.project_create";

export const SKIP_ALL_SENTINEL = "__skip_all__";
