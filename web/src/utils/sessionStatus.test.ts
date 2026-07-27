import { describe, expect, it } from "vitest";
import {
  isOperatorWindow,
  isProdTeamleadWindow,
  isQaWindow,
  operatorWindow,
  prodTeamleadWindow,
  qaWindow,
  sessionNeedsInput,
  sessionRole,
  sessionRoleLabel,
} from "./sessionStatus";

// T-0141 — the UI must prefer the worker-derived `role` and only fall back to
// the legacy "task-less ⟹ teamlead" inference when the field is absent.

describe("sessionRole", () => {
  it("prefers the worker-derived role over task_id inference", () => {
    // Worker says dev even though there's no task_id — trust the worker.
    expect(sessionRole({ role: "dev", task_id: "~" })).toBe("dev");
    expect(sessionRole({ role: "teamlead", task_id: "T-1" })).toBe("teamlead");
    expect(sessionRole({ role: "operator", task_id: "~" })).toBe("operator");
  });

  it("falls back to dev when role is absent (pre-T-0141 worker), never teamlead", () => {
    // T-0175: a missing/unknown role must default to dev, not teamlead. The old
    // "task-less ⟹ teamlead" fallback leaked nearly every finished dev as a TL.
    expect(sessionRole({ task_id: "T-9" })).toBe("dev");
    expect(sessionRole({ task_id: "~" })).toBe("dev");
    expect(sessionRole({})).toBe("dev");
  });

  it("ignores unknown role strings and falls back", () => {
    expect(sessionRole({ role: "bogus", task_id: "T-2" })).toBe("dev");
  });

  it("recognises the T-0197 prod-teamlead + qa roles", () => {
    expect(sessionRole({ role: "prod-teamlead", task_id: "~" })).toBe("prod-teamlead");
    expect(sessionRole({ role: "qa", task_id: "~" })).toBe("qa");
  });

  // T-0727: the FE union fell behind the worker enum — `user-conversation`
  // (T-0478 / M2 F2.4, derived in sessions.py::_derive_role) reached the
  // unknown-role fallback and every intake session was badged "Dev" on the
  // Processes surface. This pins the role so that drift can't recur silently.
  it("recognises the T-0478 user-conversation role", () => {
    expect(sessionRole({ role: "user-conversation", task_id: "~" })).toBe("user-conversation");
    expect(sessionRole({ role: "user-conversation", task_id: null })).not.toBe("dev");
  });

  it("handles null/undefined input", () => {
    expect(sessionRole(null)).toBe("dev");
    expect(sessionRole(undefined)).toBe("dev");
  });
});

describe("sessionRoleLabel", () => {
  it("maps roles to display labels", () => {
    expect(sessionRoleLabel("teamlead")).toBe("Teamlead");
    expect(sessionRoleLabel("dev")).toBe("Dev");
    expect(sessionRoleLabel("operator")).toBe("Operator");
    expect(sessionRoleLabel("prod-teamlead")).toBe("Prod-TL");
    expect(sessionRoleLabel("qa")).toBe("QA");
    // T-0727: distinct from "Dev" — it is a system-spawned user-facing intake
    // session, not a dev worker the operator can assign.
    expect(sessionRoleLabel("user-conversation")).toBe("User chat");
  });
});

// T-0041 — the Operator spawn option must guarantee a window that resolves to
// the operator role (mirrors the worker _OPERATOR_WINDOW_RE = (?:^|[-_])operator$).
describe("operatorWindow / isOperatorWindow", () => {
  it("appends the -operator marker when absent", () => {
    expect(operatorWindow("bot-squad")).toBe("bot-squad-operator");
    expect(operatorWindow("ops")).toBe("ops-operator");
  });

  it("leaves an already-marked window untouched (any case / separator)", () => {
    expect(operatorWindow("operator")).toBe("operator");
    expect(operatorWindow("ops-operator")).toBe("ops-operator");
    expect(operatorWindow("ops_operator")).toBe("ops_operator");
    expect(operatorWindow("Ops_OPERATOR")).toBe("Ops_OPERATOR");
  });

  it("defaults an empty/whitespace window to 'operator'", () => {
    expect(operatorWindow("")).toBe("operator");
    expect(operatorWindow("   ")).toBe("operator");
  });

  it("isOperatorWindow recognises the marker, rejects coincidental suffixes", () => {
    expect(isOperatorWindow("bot-squad-operator")).toBe(true);
    expect(isOperatorWindow("operator")).toBe(true);
    expect(isOperatorWindow("xoperator")).toBe(false); // no separator ⟹ not a marker
    expect(isOperatorWindow("operator-foo")).toBe(false); // marker must be a suffix
  });
});

// T-0197 — the Prod-TL / QA spawn options must guarantee a window that resolves
// to the matching role (mirrors the worker _PROD_TL_WINDOW_RE / _QA_WINDOW_RE).
describe("prodTeamleadWindow / isProdTeamleadWindow", () => {
  it("appends the -prod-tl marker when absent", () => {
    expect(prodTeamleadWindow("bot-squad")).toBe("bot-squad-prod-tl");
    expect(prodTeamleadWindow("ops")).toBe("ops-prod-tl");
  });

  it("leaves an already-marked window untouched (any case / separator / form)", () => {
    expect(prodTeamleadWindow("prod-tl")).toBe("prod-tl");
    expect(prodTeamleadWindow("ops_prod_teamlead")).toBe("ops_prod_teamlead");
    expect(prodTeamleadWindow("Ops-PROD-TL")).toBe("Ops-PROD-TL");
  });

  it("defaults an empty/whitespace window to 'prod-tl'", () => {
    expect(prodTeamleadWindow("")).toBe("prod-tl");
    expect(prodTeamleadWindow("   ")).toBe("prod-tl");
  });

  it("isProdTeamleadWindow recognises the marker, rejects plain-TL / coincidental", () => {
    expect(isProdTeamleadWindow("bot-squad-prod-tl")).toBe(true);
    expect(isProdTeamleadWindow("prod-teamlead")).toBe(true);
    expect(isProdTeamleadWindow("prod-ops-tl")).toBe(false); // no prod adjacent to -tl
    expect(isProdTeamleadWindow("multi_server-TL")).toBe(false); // plain TL ≠ prod-TL
  });
});

describe("qaWindow / isQaWindow", () => {
  it("appends the -qa marker when absent", () => {
    expect(qaWindow("bot-squad")).toBe("bot-squad-qa");
    expect(qaWindow("verify")).toBe("verify-qa");
  });

  it("leaves an already-marked window untouched (any case / separator)", () => {
    expect(qaWindow("qa")).toBe("qa");
    expect(qaWindow("bot_squad_qa")).toBe("bot_squad_qa");
    expect(qaWindow("Bot-Squad-QA")).toBe("Bot-Squad-QA");
  });

  it("defaults an empty/whitespace window to 'qa'", () => {
    expect(qaWindow("")).toBe("qa");
    expect(qaWindow("   ")).toBe("qa");
  });

  it("isQaWindow recognises the marker, rejects coincidental suffixes", () => {
    expect(isQaWindow("bot-squad-qa")).toBe(true);
    expect(isQaWindow("qa")).toBe(true);
    expect(isQaWindow("vodqa")).toBe(false); // no separator ⟹ not a marker
    expect(isQaWindow("qa-runner")).toBe(false); // marker must be a suffix
  });
});

// T-0346 — the per-row "waiting for input" predicate that powers the home
// needs-input deep-link. Must mirror the project-level quick_status rollup:
// paused OR active-at-prompt counts as waiting; anything else does not.
describe("sessionNeedsInput", () => {
  it("flags a paused (Ctrl-C'd) session", () => {
    expect(sessionNeedsInput({ status: "paused" })).toBe(true);
  });

  it("flags an active session blocked awaiting operator input (T-0375 canonical signal)", () => {
    expect(
      sessionNeedsInput({ status: "active", awaiting_input: true }),
    ).toBe(true);
  });

  it("does NOT flag a merely idle-at-prompt active pane (T-0375 dropped active_at_prompt)", () => {
    // active_at_prompt no longer drives needs-input — a finished autonomous dev
    // parked at ❯ but NOT blocked must read as idle, not needs-input.
    expect(
      sessionNeedsInput({ status: "active", awaiting_input: false }),
    ).toBe(false);
    expect(sessionNeedsInput({ status: "active" })).toBe(false);
  });

  it("does NOT flag suspended sessions", () => {
    expect(sessionNeedsInput({ status: "suspended" })).toBe(false);
  });

  it("does NOT flag a suspended/dead sid carrying a STALE awaiting_input marker (P2-05 liveness guard)", () => {
    // item-1 dropped the active-guard; a dead sid with a not-yet-cleared
    // tg_stall marker must NOT read needs-input forever.
    expect(sessionNeedsInput({ status: "suspended", awaiting_input: true })).toBe(false);
  });

  it("never flags an archived row even if it would otherwise qualify", () => {
    expect(
      sessionNeedsInput({ status: "paused", archived: true }),
    ).toBe(false);
    expect(
      sessionNeedsInput({
        status: "active",
        awaiting_input: true,
        archived: true,
      }),
    ).toBe(false);
  });

  it("is safe on null/empty input", () => {
    expect(sessionNeedsInput(null)).toBe(false);
    expect(sessionNeedsInput(undefined)).toBe(false);
    expect(sessionNeedsInput({})).toBe(false);
  });
});
