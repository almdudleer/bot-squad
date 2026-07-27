/**
 * T-0295 (b): constant-team health join.
 *
 * `constantTeamMembers` is the FE mirror of the worker's `_live_member_count`
 * (worker/bot_squad_worker/constant_teams.py) — the same mirroring contract
 * `computeTlBindings` has with `_find_owner`. If it drifts, the Vision page
 * reports a member count the tick disagrees with, which is worse than showing
 * nothing: the operator would trust a number the staffing logic never used.
 */
import { describe, expect, test } from "vitest";

import type { ConstantTeam, SessionRow } from "./api";
import {
  constantTeamFor,
  constantTeamMembers,
  initiativeStem,
  unmatchedConstantTeams,
} from "./constantTeams";

function sess(overrides: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-x",
    status: "active",
    window: "w",
    cwd: "/tmp",
    task_id: null,
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  } as SessionRow;
}

function team(overrides: Partial<ConstantTeam> = {}): ConstantTeam {
  return {
    name: "triage",
    stem: "triage",
    initiative: "initiatives/triage.md",
    configured: true,
    config_source: "vision/initiatives/triage.md",
    team_size: 2,
    team_role: "dev",
    window_prefix: "triage",
    consume: "feedback/inbox.log",
    consume_kind: "log",
    queue_depth: 0,
    cursor_lines: 0,
    last_spawn_at: null,
    last_spawn_iso: null,
    state_file: "_worker/constant_teams/triage.json",
    state_present: false,
    retired: false,
    finished: false,
    staffable: true,
    not_staffable_reason: null,
    ...overrides,
  };
}

describe("initiativeStem", () => {
  test("strips the initiatives/ prefix and the .md suffix", () => {
    expect(initiativeStem("initiatives/prod-support.md")).toBe("prod-support");
    expect(initiativeStem("prod-support.md")).toBe("prod-support");
    expect(initiativeStem("prod-support")).toBe("prod-support");
  });

  test("treats the worker's `~` null-sentinel and blanks as unbound", () => {
    expect(initiativeStem("~")).toBe("");
    expect(initiativeStem(null)).toBe("");
    expect(initiativeStem(undefined)).toBe("");
    expect(initiativeStem("  ")).toBe("");
  });
});

describe("constantTeamMembers", () => {
  test("counts a session bound by its initiative binding", () => {
    const members = constantTeamMembers(
      [sess({ sid: "S-a", initiative: "initiatives/triage.md" })],
      team(),
    );
    expect(members.map((s) => s.sid)).toEqual(["S-a"]);
  });

  test("counts a session by window prefix when the binding hasn't settled", () => {
    const members = constantTeamMembers(
      [sess({ sid: "S-b", window: "triage-2", initiative: null })],
      team(),
    );
    expect(members.map((s) => s.sid)).toEqual(["S-b"]);
  });

  test("counts paused members — the worker's _LIVE_STATUSES is active+paused", () => {
    const members = constantTeamMembers(
      [sess({ sid: "S-c", status: "paused", initiative: "triage.md" })],
      team(),
    );
    expect(members.map((s) => s.sid)).toEqual(["S-c"]);
  });

  test("excludes suspended/archived-status members (not staffing anything)", () => {
    const members = constantTeamMembers(
      [
        sess({ sid: "S-d", status: "suspended", initiative: "triage.md" }),
        sess({ sid: "S-e", status: "suspended", window: "triage-9" }),
      ],
      team(),
    );
    expect(members).toEqual([]);
  });

  test("excludes another initiative's sessions", () => {
    const members = constantTeamMembers(
      [sess({ sid: "S-f", window: "other", initiative: "initiatives/ui-polish.md" })],
      team(),
    );
    expect(members).toEqual([]);
  });

  test("an empty window prefix never matches every window", () => {
    // "".startsWith-style fallbacks are how a prefix join turns into "count
    // every live session in the project".
    const members = constantTeamMembers(
      [sess({ sid: "S-g", window: "unrelated", initiative: null })],
      team({ window_prefix: "" }),
    );
    expect(members).toEqual([]);
  });
});

describe("constantTeamFor", () => {
  test("matches an initiative row basename to its team", () => {
    expect(constantTeamFor([team()], "triage.md")?.name).toBe("triage");
  });

  test("returns undefined when the initiative has no tick config", () => {
    // The live case for every task-backed persistent initiative post-T-0480.
    expect(constantTeamFor([team()], "ui-polish.md")).toBeUndefined();
  });
});

describe("unmatchedConstantTeams", () => {
  test("surfaces tick state with no initiative row on the page", () => {
    const orphan = team({ name: "prod-support", stem: "prod-support", configured: false });
    const out = unmatchedConstantTeams([team(), orphan], ["initiatives/triage.md"]);
    expect(out.map((t) => t.name)).toEqual(["prod-support"]);
  });
});
