/**
 * T-0051 — pure-helper tests for the +New project wizard.
 *
 * The Picker.tsx modal is React; the validator + payload builder
 * are pure exports from projectCreateWizard.ts so they can be
 * unit-tested without jsdom (this repo runs vitest with no DOM
 * environment — see Welcome.test.ts for the pattern).
 */
import { describe, expect, test } from "vitest";

import {
  deriveSlug,
  emptyWizardState,
  modeOptions,
  payloadFromWizard,
  validateWizard,
  type ProjectCreateState,
} from "./projectCreateWizard";

function stateForMode(mode: ProjectCreateState["mode"]): ProjectCreateState {
  return { ...emptyWizardState(), display_name: "X", slug: "x", mode };
}

describe("deriveSlug", () => {
  test("lowercases, dashes runs of non-alnum, strips leading non-letter", () => {
    expect(deriveSlug("My Project")).toBe("my-project");
    expect(deriveSlug("123 Foo")).toBe("foo");
    expect(deriveSlug("AB CD!")).toBe("ab-cd");
  });
  test("trailing dashes are stripped", () => {
    expect(deriveSlug("Foo ")).toBe("foo");
    expect(deriveSlug("Foo !!!")).toBe("foo");
  });
});

describe("validateWizard", () => {
  test("requires display_name + slug regardless of mode", () => {
    const s = emptyWizardState();
    const errs = validateWizard(s);
    expect(errs).toContain("display name required");
    expect(errs).toContain("slug required");
  });

  test("mode=null accepts a complete minimal payload (T-0021 back-compat)", () => {
    const s: ProjectCreateState = {
      ...emptyWizardState(),
      display_name: "X",
      slug: "x",
    };
    expect(validateWizard(s)).toEqual([]);
  });

  test("mode=new_from_scratch requires mother_dir, git_remote optional", () => {
    const s = stateForMode("new_from_scratch");
    expect(validateWizard(s)).toContain("mother dir required");

    s.mother_dir = "/home/u/proj";
    expect(validateWizard(s)).toEqual([]); // git_remote may be blank
    s.git_remote = "git@github.com:foo/bar.git";
    expect(validateWizard(s)).toEqual([]);
  });

  test("mode=paths_as_they_are requires mother + both repo paths", () => {
    const s = stateForMode("paths_as_they_are");
    s.mother_dir = "/m";
    expect(validateWizard(s)).toContain("dev repo path required");
    s.repo_path = "/d";
    expect(validateWizard(s)).toContain("master repo path required");
    s.repo_master = "/M";
    expect(validateWizard(s)).toEqual([]);
  });

  test("mother_dir + repo paths must be absolute", () => {
    const s = stateForMode("paths_as_they_are");
    s.mother_dir = "relative";
    s.repo_path = "dev";
    s.repo_master = "M";
    const errs = validateWizard(s);
    expect(errs.some((e) => e.includes("mother dir must be an absolute path"))).toBe(true);
    expect(errs.some((e) => e.includes("dev repo path must be an absolute path"))).toBe(true);
    expect(errs.some((e) => e.includes("master repo path must be an absolute path"))).toBe(true);
  });

  test("mode=attach_destructive always errors (not implemented)", () => {
    const s = stateForMode("attach_destructive");
    s.mother_dir = "/m";
    s.existing_path = "/e";
    s.confirm_destructive_move = true;
    const errs = validateWizard(s);
    expect(errs.some((e) => e.includes("not implemented"))).toBe(true);
  });
});

describe("payloadFromWizard", () => {
  test("mode=null sends only slug + display_name (T-0021 back-compat)", () => {
    const s: ProjectCreateState = {
      ...emptyWizardState(),
      display_name: "X",
      slug: "x",
    };
    expect(payloadFromWizard(s)).toEqual({ slug: "x", display_name: "X" });
  });

  test("trims fields before building payload", () => {
    const s: ProjectCreateState = {
      ...emptyWizardState(),
      display_name: "  X  ",
      slug: "  x  ",
      mode: "new_from_scratch",
      mother_dir: "  /m  ",
    };
    expect(payloadFromWizard(s)).toEqual({
      slug: "x",
      display_name: "X",
      mode: "new_from_scratch",
      mother_dir: "/m",
    });
  });

  test("mode=new_from_scratch with git_remote", () => {
    const s = stateForMode("new_from_scratch");
    s.mother_dir = "/m";
    s.git_remote = "git@github.com:foo/bar.git";
    expect(payloadFromWizard(s)).toEqual({
      slug: "x",
      display_name: "X",
      mode: "new_from_scratch",
      mother_dir: "/m",
      git_remote: "git@github.com:foo/bar.git",
    });
  });

  test("mode=new_from_scratch blank git_remote is omitted (not empty string)", () => {
    const s = stateForMode("new_from_scratch");
    s.mother_dir = "/m";
    const payload = payloadFromWizard(s);
    expect(payload).not.toHaveProperty("git_remote");
  });

  test("mode=paths_as_they_are sends mother + both repo paths", () => {
    const s = stateForMode("paths_as_they_are");
    s.mother_dir = "/m";
    s.repo_path = "/d";
    s.repo_master = "/M";
    expect(payloadFromWizard(s)).toEqual({
      slug: "x",
      display_name: "X",
      mode: "paths_as_they_are",
      mother_dir: "/m",
      repo_path: "/d",
      repo_master: "/M",
    });
  });

  test("mode=attach_destructive includes the confirm flag", () => {
    const s = stateForMode("attach_destructive");
    s.mother_dir = "/m";
    s.existing_path = "/e";
    s.existing_becomes = "master";
    s.confirm_destructive_move = true;
    expect(payloadFromWizard(s)).toEqual({
      slug: "x",
      display_name: "X",
      mode: "attach_destructive",
      mother_dir: "/m",
      existing_path: "/e",
      existing_becomes: "master",
      confirm_destructive_move: true,
    });
  });
});

describe("modeOptions", () => {
  test("returns the three modes in wizard order, paths_as_they_are recommended", () => {
    const opts = modeOptions();
    expect(opts.map((o) => o.key)).toEqual([
      "new_from_scratch",
      "paths_as_they_are",
      "attach_destructive",
    ]);
    expect(opts.find((o) => o.key === "paths_as_they_are")?.recommended).toBe(true);
    // attach_destructive disabled with a reason — UI renders it greyed
    // out so the user can see the option exists but is not available.
    const destr = opts.find((o) => o.key === "attach_destructive");
    expect(destr?.disabled).toBe(true);
    expect(destr?.disabled_reason).toMatch(/follow-up|not yet/i);
  });
});
