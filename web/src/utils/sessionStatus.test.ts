import { describe, expect, it } from "vitest";
import {
  isOperatorWindow,
  operatorWindow,
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
